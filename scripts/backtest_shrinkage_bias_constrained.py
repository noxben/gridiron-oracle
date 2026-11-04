#!/usr/bin/env python3
"""FFL Oracle: select shrinkage attenuation under a development-only bias constraint.

Uses rolling-origin validation only within development weeks. Among attenuation
levels whose absolute weighted validation bias is at or below --max-abs-bias,
select the lowest-MAE level (RMSE, then lower attenuation break ties). If no
candidate meets the constraint, select the candidate with the smallest absolute
bias, then MAE, RMSE, and attenuation. The later holdout is never used to select
attenuation.

Input columns: week, position, baseline_projection, position_mean_pts, actual_pts.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import numpy as np
import pandas as pd
import backtest_shrinkage_attenuation as attenuation_base
import backtest_shrinkage_holdout as base

DEFAULT_ATTENUATIONS = tuple(round(float(x), 2) for x in np.arange(0.0, 1.0001, 0.05))


def select_bias_constrained(summary: pd.DataFrame, max_abs_bias: float) -> tuple[pd.DataFrame, float]:
    out = summary.copy()
    out["abs_weighted_bias"] = out["weighted_bias"].abs()
    out["bias_feasible"] = out["abs_weighted_bias"] <= max_abs_bias + 1e-12
    feasible = out.loc[out.bias_feasible]
    if not feasible.empty:
        ordered = feasible.sort_values(
            ["weighted_mae", "weighted_rmse", "attenuation"],
            ascending=[True, True, True],
        )
        selection_rule = "lowest MAE among candidates satisfying bias constraint"
    else:
        ordered = out.sort_values(
            ["abs_weighted_bias", "weighted_mae", "weighted_rmse", "attenuation"],
            ascending=[True, True, True, True],
        )
        selection_rule = "no feasible candidate; minimum absolute bias fallback"
    chosen = float(ordered.iloc[0]["attenuation"])
    out["selection_rule"] = selection_rule
    out["selected"] = np.isclose(out["attenuation"], chosen)
    return out.sort_values(
        ["selected", "bias_feasible", "weighted_mae"],
        ascending=[False, False, True],
    ).reset_index(drop=True), chosen


def run(input_csv: Path, holdout_start_week: int, bootstrap_reps: int, seed: int,
        attenuations: tuple[float, ...], min_prior_weeks: int, max_abs_bias: float):
    # Reuse the tested rolling-origin folds and frozen holdout scoring implementation.
    cv_rows, cv_summary, _mae_selected = attenuation_base.rolling_origin_cv(
        _load_development(input_csv, holdout_start_week), attenuations, min_prior_weeks
    )
    selection, chosen = select_bias_constrained(cv_summary, max_abs_bias)

    df = pd.read_csv(input_csv)
    df = base._numeric_frame(df).dropna(
        subset=["week", "position", "baseline_projection", "position_mean_pts", "actual_pts"]
    ).copy()
    df["week"] = df["week"].astype(int)
    development = df.loc[df.week < holdout_start_week].copy()
    holdout = df.loc[df.week >= holdout_start_week].copy()
    if development.empty or holdout.empty:
        raise ValueError("Need development and holdout rows on opposite sides of holdout-start-week.")

    fitted = base.fit_hierarchy(development)
    scored = attenuation_base.apply_fitted(holdout, fitted)
    scored["chosen_attenuation"] = chosen
    scored["bias_constrained_projection"] = scored["baseline_projection"] + chosen * (
        scored["full_shrinkage_projection"] - scored["baseline_projection"]
    )
    scored["global_shrinkage_085_projection"] = scored["position_mean_pts"] + 0.85 * (
        scored["baseline_projection"] - scored["position_mean_pts"]
    )

    metric_rows = []
    models = (
        ("baseline_projection", "baseline"),
        ("global_shrinkage_085_projection", "fixed_global_shrinkage_0.85"),
        ("full_shrinkage_projection", "full_position_bucket_shrinkage"),
        ("bias_constrained_projection", f"bias_constrained_attenuation_{chosen:.2f}"),
    )
    for position in ("ALL", *base.POSITIONS):
        group = scored if position == "ALL" else scored.loc[scored.position == position]
        for col, label in models:
            metric_rows.append(base.metric_row(group, col, label, position))
    metrics = pd.DataFrame(metric_rows)

    weekly_rows = []
    for week, group in scored.groupby("week", sort=True):
        actual = group.actual_pts
        base_metrics = attenuation_base.score_predictions(group.baseline_projection, actual)
        row = {"week": int(week), "n": int(base_metrics["n"]), "baseline_mae": base_metrics["mae"], "baseline_rmse": base_metrics["rmse"]}
        for col, label in (("bias_constrained_projection", "selected"), ("full_shrinkage_projection", "full"), ("global_shrinkage_085_projection", "global_085")):
            m = attenuation_base.score_predictions(group[col], actual)
            row[f"{label}_mae"] = m["mae"]
            row[f"{label}_rmse"] = m["rmse"]
            row[f"{label}_bias"] = m["bias"]
        row["selected_mae_delta"] = row["selected_mae"] - row["baseline_mae"]
        weekly_rows.append(row)
    weekly = pd.DataFrame(weekly_rows)
    paired = weekly.rename(columns={"selected_mae_delta": "mae_delta_shrinkage_minus_baseline"})
    bootstrap = base.bootstrap_week_delta(paired, bootstrap_reps, seed)
    bootstrap.update({
        "holdout_start_week": int(holdout_start_week), "chosen_attenuation": chosen,
        "max_abs_development_bias": max_abs_bias,
        "development_weeks": ",".join(map(str, sorted(development.week.unique()))),
        "holdout_weeks": ",".join(map(str, sorted(holdout.week.unique()))),
        "n_holdout_rows": int(len(holdout)), "n_training_rows": int(len(development)),
    })
    return cv_rows, selection, metrics, weekly, scored, bootstrap


def _load_development(input_csv: Path, holdout_start_week: int) -> pd.DataFrame:
    df = pd.read_csv(input_csv)
    required = {"week", "position", "baseline_projection", "position_mean_pts", "actual_pts"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError("Input CSV missing required fields: " + ", ".join(sorted(missing)))
    df = base._numeric_frame(df).dropna(
        subset=["week", "position", "baseline_projection", "position_mean_pts", "actual_pts"]
    ).copy()
    df["week"] = df["week"].astype(int)
    development = df.loc[df.week < holdout_start_week].copy()
    if development.empty or df.loc[df.week >= holdout_start_week].empty:
        raise ValueError("Need development and holdout rows on opposite sides of holdout-start-week.")
    return development


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("--holdout-start-week", type=int, default=13)
    parser.add_argument("--bootstrap-reps", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20261013)
    parser.add_argument("--min-prior-weeks", type=int, default=3)
    parser.add_argument("--max-abs-bias", type=float, default=0.25,
                        help="Maximum absolute row-weighted bias allowed on development CV; default 0.25 points")
    parser.add_argument("--attenuations", type=float, nargs="+", default=list(DEFAULT_ATTENUATIONS))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/shrinkage_bias_constrained"))
    args = parser.parse_args()
    if args.bootstrap_reps < 1000:
        parser.error("Use at least 1,000 bootstrap replicates.")
    if args.max_abs_bias < 0:
        parser.error("--max-abs-bias must be non-negative.")
    if any(a < 0 or a > 1 for a in args.attenuations):
        parser.error("Attenuations must be between 0 and 1.")
    if len(set(args.attenuations)) != len(args.attenuations):
        parser.error("Attenuations must be unique.")
    result = run(args.input_csv, args.holdout_start_week, args.bootstrap_reps, args.seed,
                 tuple(sorted(args.attenuations)), args.min_prior_weeks, args.max_abs_bias)
    cv, selection, metrics, weekly, scored, bootstrap = result
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    cv.to_csv(out / "development_bias_constrained_folds.csv", index=False)
    selection.to_csv(out / "development_bias_constrained_selection.csv", index=False)
    metrics.to_csv(out / "holdout_bias_constrained_metrics.csv", index=False)
    weekly.to_csv(out / "holdout_bias_constrained_weekly.csv", index=False)
    scored.to_csv(out / "holdout_bias_constrained_scored_rows.csv", index=False)
    pd.DataFrame([bootstrap]).to_csv(out / "holdout_bias_constrained_bootstrap.csv", index=False)
    print("\nDEVELOPMENT-ONLY BIAS-CONSTRAINED ATTENUATION SELECTION")
    print(selection.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"\nSelected attenuation: {bootstrap['chosen_attenuation']:.2f}; development CV |bias| <= {args.max_abs_bias:.3f}")
    print("\nFIXED HOLDOUT METRICS")
    print(metrics.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nHOLDOUT WEEKLY RESULTS")
    print(weekly.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nWEEK-CLUSTER BOOTSTRAP: selected shrinkage MAE minus baseline MAE")
    print(pd.DataFrame([bootstrap]).to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"\nWrote six reports to: {out.resolve()}")

if __name__ == "__main__":
    main()
