#!/usr/bin/env python3
"""FFL Oracle: select position-specific shrinkage attenuation under bias constraints.

Every position's attenuation is selected using rolling-origin folds contained
strictly in the development period. For each position, choose the lowest-MAE
attenuation whose absolute row-weighted validation bias is <= --max-abs-bias.
If no candidate is feasible for a position, choose the smallest absolute bias,
then MAE/RMSE/lower attenuation. Freeze all four choices before scoring holdout.

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


def load_frame(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"week", "position", "baseline_projection", "position_mean_pts", "actual_pts"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError("Input CSV missing required fields: " + ", ".join(sorted(missing)))
    df = base._numeric_frame(df).dropna(subset=list(required)).copy()
    df["week"] = df["week"].astype(int)
    df["position"] = df["position"].astype(str).str.upper()
    return df


def position_cv(development: pd.DataFrame, attenuations: tuple[float, ...], min_prior_weeks: int):
    rows = []
    weeks = sorted(development.week.unique().tolist())
    for validation_week in weeks:
        prior = development.loc[development.week < validation_week]
        if prior.week.nunique() < min_prior_weeks:
            continue
        validation = development.loc[development.week == validation_week].copy()
        fitted = base.fit_hierarchy(prior)
        scored = attenuation_base.apply_fitted(validation, fitted)
        for position in base.POSITIONS:
            group = scored.loc[scored.position == position]
            if group.empty:
                continue
            for alpha in attenuations:
                pred = group.baseline_projection + alpha * (group.full_shrinkage_projection - group.baseline_projection)
                metrics = attenuation_base.score_predictions(pred, group.actual_pts)
                rows.append({"validation_week": int(validation_week), "position": position,
                             "attenuation": alpha, **metrics})
    cv = pd.DataFrame(rows)
    if cv.empty:
        raise ValueError("No rolling-origin folds. Lower --min-prior-weeks or provide more development weeks.")
    summary_rows = []
    for (position, alpha), group in cv.groupby(["position", "attenuation"], sort=True):
        valid = group.loc[group.n > 0]
        n = int(valid.n.sum())
        summary_rows.append({
            "position": position, "attenuation": float(alpha), "n": n,
            "weighted_mae": float(np.average(valid.mae, weights=valid.n)) if n else np.nan,
            "weighted_rmse": float(np.sqrt(np.average(np.square(valid.rmse), weights=valid.n))) if n else np.nan,
            "weighted_bias": float(np.average(valid.bias, weights=valid.n)) if n else np.nan,
            "validation_weeks": int(valid.validation_week.nunique()),
        })
    return cv, pd.DataFrame(summary_rows)


def select_by_position(summary: pd.DataFrame, max_abs_bias: float):
    outputs, choices = [], {}
    for position in base.POSITIONS:
        out = summary.loc[summary.position == position].copy()
        if out.empty:
            raise ValueError(f"No development CV rows for position {position}.")
        out["abs_weighted_bias"] = out.weighted_bias.abs()
        out["bias_feasible"] = out.abs_weighted_bias <= max_abs_bias + 1e-12
        feasible = out.loc[out.bias_feasible]
        if not feasible.empty:
            ordered = feasible.sort_values(["weighted_mae", "weighted_rmse", "attenuation"], ascending=True)
            rule = "lowest MAE among candidates satisfying position bias constraint"
        else:
            ordered = out.sort_values(["abs_weighted_bias", "weighted_mae", "weighted_rmse", "attenuation"], ascending=True)
            rule = "no feasible candidate; minimum absolute bias fallback"
        chosen = float(ordered.iloc[0].attenuation)
        choices[position] = chosen
        out["selection_rule"] = rule
        out["selected"] = np.isclose(out.attenuation, chosen)
        outputs.append(out)
    selection = pd.concat(outputs, ignore_index=True)
    selection = selection.sort_values(["position", "selected", "bias_feasible", "weighted_mae"], ascending=[True, False, False, True]).reset_index(drop=True)
    return selection, choices


def run(input_csv: Path, holdout_start_week: int, bootstrap_reps: int, seed: int,
        attenuations: tuple[float, ...], min_prior_weeks: int, max_abs_bias: float):
    df = load_frame(input_csv)
    development = df.loc[df.week < holdout_start_week].copy()
    holdout = df.loc[df.week >= holdout_start_week].copy()
    if development.empty or holdout.empty:
        raise ValueError("Need development and holdout rows on opposite sides of --holdout-start-week.")
    cv, cv_summary = position_cv(development, attenuations, min_prior_weeks)
    selection, choices = select_by_position(cv_summary, max_abs_bias)

    fitted = base.fit_hierarchy(development)
    scored = attenuation_base.apply_fitted(holdout, fitted)
    scored["chosen_attenuation"] = scored.position.map(choices)
    scored["position_bias_constrained_projection"] = scored.baseline_projection + scored.chosen_attenuation * (
        scored.full_shrinkage_projection - scored.baseline_projection)
    scored["global_shrinkage_085_projection"] = scored.position_mean_pts + 0.85 * (
        scored.baseline_projection - scored.position_mean_pts)

    models = (("baseline_projection", "baseline"),
              ("global_shrinkage_085_projection", "fixed_global_shrinkage_0.85"),
              ("full_shrinkage_projection", "full_position_bucket_shrinkage"),
              ("position_bias_constrained_projection", "position_bias_constrained"))
    metric_rows = []
    for position in ("ALL", *base.POSITIONS):
        group = scored if position == "ALL" else scored.loc[scored.position == position]
        for col, label in models:
            row = base.metric_row(group, col, label, position)
            if label == "position_bias_constrained":
                row["chosen_attenuation"] = np.nan if position == "ALL" else choices[position]
            metric_rows.append(row)
    metrics = pd.DataFrame(metric_rows)

    weekly_rows = []
    for week, group in scored.groupby("week", sort=True):
        row = {"week": int(week), "n": int(len(group))}
        for col, label in (("baseline_projection", "baseline"),
                           ("position_bias_constrained_projection", "selected"),
                           ("full_shrinkage_projection", "full"),
                           ("global_shrinkage_085_projection", "global_085")):
            m = attenuation_base.score_predictions(group[col], group.actual_pts)
            row[f"{label}_mae"] = m["mae"]
            row[f"{label}_rmse"] = m["rmse"]
            row[f"{label}_bias"] = m["bias"]
        row["selected_mae_delta"] = row["selected_mae"] - row["baseline_mae"]
        weekly_rows.append(row)
    weekly = pd.DataFrame(weekly_rows)
    paired = weekly.rename(columns={"selected_mae_delta": "mae_delta_shrinkage_minus_baseline"})
    bootstrap = base.bootstrap_week_delta(paired, bootstrap_reps, seed)
    bootstrap.update({"holdout_start_week": int(holdout_start_week),
                      "chosen_attenuation_by_position": ";".join(f"{p}:{choices[p]:.2f}" for p in base.POSITIONS),
                      "max_abs_development_bias": max_abs_bias,
                      "development_weeks": ",".join(map(str, sorted(development.week.unique()))),
                      "holdout_weeks": ",".join(map(str, sorted(holdout.week.unique()))),
                      "n_holdout_rows": int(len(holdout)), "n_training_rows": int(len(development))})
    return cv, selection, metrics, weekly, scored, bootstrap


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("--holdout-start-week", type=int, default=13)
    parser.add_argument("--bootstrap-reps", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20261013)
    parser.add_argument("--min-prior-weeks", type=int, default=3)
    parser.add_argument("--max-abs-bias", type=float, default=0.25)
    parser.add_argument("--attenuations", type=float, nargs="+", default=list(DEFAULT_ATTENUATIONS))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/shrinkage_position_bias_constrained"))
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
    cv.to_csv(out / "development_position_bias_folds.csv", index=False)
    selection.to_csv(out / "development_position_bias_selection.csv", index=False)
    metrics.to_csv(out / "holdout_position_bias_metrics.csv", index=False)
    weekly.to_csv(out / "holdout_position_bias_weekly.csv", index=False)
    scored.to_csv(out / "holdout_position_bias_scored_rows.csv", index=False)
    pd.DataFrame([bootstrap]).to_csv(out / "holdout_position_bias_bootstrap.csv", index=False)
    print("\nDEVELOPMENT-ONLY POSITION-SPECIFIC BIAS-CONSTRAINED SELECTION")
    print(selection.loc[selection.selected].to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nFROZEN POSITION ATTENUATIONS: " + ", ".join(f"{p}={choices:.2f}" for p, choices in sorted(scored.groupby('position').chosen_attenuation.first().items())))
    print("\nFIXED HOLDOUT METRICS")
    print(metrics.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nHOLDOUT WEEKLY RESULTS")
    print(weekly.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nWEEK-CLUSTER BOOTSTRAP: selected shrinkage MAE minus baseline MAE")
    print(pd.DataFrame([bootstrap]).to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"\nWrote six reports to: {out.resolve()}")

if __name__ == "__main__":
    main()
