#!/usr/bin/env python3
"""FFL Oracle: choose shrinkage attenuation by rolling-origin development CV.

The position/baseline-bucket shrinkage coefficients are fitted only on weeks
before each validation week. A single attenuation factor is selected using
rolling-origin validation within development weeks, then frozen before the
later holdout is scored. The holdout is not used to choose attenuation.

Input: detailed CSV emitted by backtest_projections.py with columns week,
position,baseline_projection,position_mean_pts,actual_pts.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import numpy as np
import pandas as pd
import backtest_shrinkage_holdout as base

DEFAULT_ATTENUATIONS = (0.0, 0.25, 0.50, 0.75, 1.0)


def score_predictions(pred: pd.Series, actual: pd.Series) -> dict:
    valid = pred.notna() & actual.notna()
    err = pred.loc[valid] - actual.loc[valid]
    if err.empty:
        return {"n": 0, "mae": np.nan, "rmse": np.nan, "bias": np.nan}
    return {"n": int(len(err)), "mae": float(err.abs().mean()),
            "rmse": float(np.sqrt(np.square(err).mean())), "bias": float(err.mean())}


def apply_fitted(frame: pd.DataFrame, fitted: dict) -> pd.DataFrame:
    out = frame.copy()
    resolved = out.apply(lambda row: base.resolve_coefficient(row, fitted), axis=1, result_type="expand")
    resolved.columns = ["coefficient", "coefficient_source", "coefficient_training_n"]
    out[resolved.columns] = resolved
    out["full_shrinkage_projection"] = out["position_mean_pts"] + out["coefficient"] * (
        out["baseline_projection"] - out["position_mean_pts"])
    return out


def rolling_origin_cv(development: pd.DataFrame, attenuations: tuple[float, ...], min_prior_weeks: int) -> tuple[pd.DataFrame, float]:
    weeks = sorted(development["week"].unique().tolist())
    rows = []
    for validation_week in weeks:
        prior = development.loc[development.week < validation_week]
        if prior["week"].nunique() < min_prior_weeks:
            continue
        validation = development.loc[development.week == validation_week].copy()
        fitted = base.fit_hierarchy(prior)
        scored = apply_fitted(validation, fitted)
        actual = scored["actual_pts"]
        for alpha in attenuations:
            # alpha=0 is baseline; alpha=1 is the fully fitted shrinkage rule.
            pred = scored["baseline_projection"] + alpha * (
                scored["full_shrinkage_projection"] - scored["baseline_projection"])
            metrics = score_predictions(pred, actual)
            rows.append({"validation_week": int(validation_week), "attenuation": alpha, **metrics})
    cv = pd.DataFrame(rows)
    if cv.empty:
        raise ValueError("No rolling-origin folds. Lower --min-prior-weeks or provide more development weeks.")
    # Aggregate row-weighted errors across validation weeks; all candidates use identical rows.
    summary = cv.groupby("attenuation", as_index=False).apply(
        lambda g: pd.Series({
            "n": int(g["n"].sum()),
            "weighted_mae": float(np.average(g["mae"], weights=g["n"])),
            "weighted_rmse": float(np.sqrt(np.average(np.square(g["rmse"]), weights=g["n"]))),
            "weighted_bias": float(np.average(g["bias"], weights=g["n"])),
            "validation_weeks": int(g["validation_week"].nunique()),
        }), include_groups=False
    ).reset_index(drop=True)
    summary = summary.sort_values(["weighted_mae", "weighted_rmse", "attenuation"], ascending=[True, True, True]).reset_index(drop=True)
    chosen = float(summary.iloc[0]["attenuation"])
    return cv, summary, chosen


def run(input_csv: Path, holdout_start_week: int, bootstrap_reps: int, seed: int, attenuations: tuple[float, ...], min_prior_weeks: int):
    df = pd.read_csv(input_csv)
    required = {"week", "position", "baseline_projection", "position_mean_pts", "actual_pts"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError("Input CSV missing required fields: " + ", ".join(sorted(missing)))
    df = base._numeric_frame(df).dropna(subset=["week", "position", "baseline_projection", "position_mean_pts", "actual_pts"]).copy()
    df["week"] = df["week"].astype(int)
    development = df.loc[df.week < holdout_start_week].copy()
    holdout = df.loc[df.week >= holdout_start_week].copy()
    if development.empty or holdout.empty:
        raise ValueError("Need development and holdout rows on opposite sides of holdout-start-week.")
    cv_rows, cv_summary, chosen = rolling_origin_cv(development, attenuations, min_prior_weeks)
    fitted = base.fit_hierarchy(development)
    scored = apply_fitted(holdout, fitted)
    scored["chosen_attenuation"] = chosen
    scored["attenuated_shrinkage_projection"] = scored["baseline_projection"] + chosen * (
        scored["full_shrinkage_projection"] - scored["baseline_projection"])
    scored["global_shrinkage_085_projection"] = scored["position_mean_pts"] + 0.85 * (
        scored["baseline_projection"] - scored["position_mean_pts"])

    metric_rows = []
    for position in ("ALL", *base.POSITIONS):
        group = scored if position == "ALL" else scored.loc[scored.position == position]
        for col, label in (("baseline_projection", "baseline"),
                            ("global_shrinkage_085_projection", "fixed_global_shrinkage_0.85"),
                            ("full_shrinkage_projection", "full_position_bucket_shrinkage"),
                            ("attenuated_shrinkage_projection", f"cv_attenuation_{chosen:.2f}")):
            metric_rows.append(base.metric_row(group, col, label, position))
    metrics = pd.DataFrame(metric_rows)
    weekly = []
    for week, group in scored.groupby("week", sort=True):
        actual = group.actual_pts
        row = {"week": int(week), "n": int(len(group))}
        for col, label in (("baseline_projection", "baseline"), ("attenuated_shrinkage_projection", "attenuated"), ("full_shrinkage_projection", "full")):
            m = score_predictions(group[col], actual)
            row[f"{label}_mae"] = m["mae"]
            row[f"{label}_rmse"] = m["rmse"]
            if label == "attenuated": row["attenuated_mae_delta"] = m["mae"] - score_predictions(group.baseline_projection, actual)["mae"]
        weekly.append(row)
    weekly = pd.DataFrame(weekly)
    paired = weekly.rename(columns={"attenuated_mae_delta": "mae_delta_shrinkage_minus_baseline"})
    bootstrap = base.bootstrap_week_delta(paired, bootstrap_reps, seed)
    bootstrap.update({"holdout_start_week": int(holdout_start_week), "chosen_attenuation": chosen,
                      "development_weeks": ",".join(map(str, sorted(development.week.unique()))),
                      "holdout_weeks": ",".join(map(str, sorted(holdout.week.unique()))),
                      "n_holdout_rows": int(len(holdout)), "n_training_rows": int(len(development))})
    return cv_rows, cv_summary, metrics, weekly, scored, bootstrap


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("--holdout-start-week", type=int, default=13)
    parser.add_argument("--bootstrap-reps", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20261013)
    parser.add_argument("--min-prior-weeks", type=int, default=3)
    parser.add_argument("--attenuations", type=float, nargs="+", default=list(DEFAULT_ATTENUATIONS))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/shrinkage_attenuation"))
    args = parser.parse_args()
    if args.bootstrap_reps < 1000: parser.error("Use at least 1,000 bootstrap replicates.")
    if any(a < 0 or a > 1 for a in args.attenuations): parser.error("Attenuations must be between 0 and 1.")
    if len(set(args.attenuations)) != len(args.attenuations): parser.error("Attenuations must be unique.")
    cv, summary, metrics, weekly, scored, bootstrap = run(args.input_csv, args.holdout_start_week,
        args.bootstrap_reps, args.seed, tuple(sorted(args.attenuations)), args.min_prior_weeks)
    out = args.output_dir; out.mkdir(parents=True, exist_ok=True)
    cv.to_csv(out / "development_rolling_origin_folds.csv", index=False)
    summary.to_csv(out / "development_attenuation_selection.csv", index=False)
    metrics.to_csv(out / "holdout_attenuation_metrics.csv", index=False)
    weekly.to_csv(out / "holdout_attenuation_weekly.csv", index=False)
    scored.to_csv(out / "holdout_attenuation_scored_rows.csv", index=False)
    pd.DataFrame([bootstrap]).to_csv(out / "holdout_attenuation_bootstrap.csv", index=False)
    print("\nDEVELOPMENT-ONLY ROLLING-ORIGIN ATTENUATION SELECTION")
    print(summary.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"\nSelected attenuation: {bootstrap['chosen_attenuation']:.2f} (chosen without holdout outcomes)")
    print("\nFIXED HOLDOUT METRICS")
    print(metrics.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nHOLDOUT WEEKLY RESULTS")
    print(weekly.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nWEEK-CLUSTER BOOTSTRAP: attenuated shrinkage MAE minus baseline MAE")
    print(pd.DataFrame([bootstrap]).to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"\nWrote six reports to: {out.resolve()}")

if __name__ == "__main__": main()
