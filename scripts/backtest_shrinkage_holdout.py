#!/usr/bin/env python3
"""FFL Oracle: fixed temporal holdout test for projection shrinkage.

Reads the detailed CSV emitted by backtest_projections.py --output. Fits
shrinkage coefficients on development weeks only, freezes them, and evaluates
later holdout weeks without updating on holdout outcomes. Projection buckets
are always derived from the original baseline projection. Paired uncertainty
is estimated by resampling whole weeks, not individual player rows.

Example:
  python scripts/backtest_projections.py --season 2025 --start-week 5 --end-week 18 \
      --output outputs/backtest_2025_detailed.csv
  python scripts/backtest_shrinkage_holdout.py outputs/backtest_2025_detailed.csv \
      --holdout-start-week 13 --bootstrap-reps 10000 --output-dir outputs/shrinkage_holdout
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

POSITIONS = ("QB", "RB", "WR", "TE")
CALIBRATION_BINS = (0, 5, 10, 15, 20, np.inf)
CALIBRATION_LABELS = ("<5", "5-10", "10-15", "15-20", "20+")
COEFFICIENT_GRID = tuple(round(x, 2) for x in np.arange(0.70, 1.001, 0.05))
MIN_BUCKET_N = 25
MIN_GROUP_N = 25
DEFAULT_SHRINKAGE = 0.85


def calibration_bucket(values: pd.Series) -> pd.Series:
    return pd.cut(
        pd.to_numeric(values, errors="coerce"),
        bins=CALIBRATION_BINS,
        labels=CALIBRATION_LABELS,
        right=False,
    )


def _numeric_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in ("week", "baseline_projection", "position_mean_pts", "actual_pts"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["projection_bucket"] = calibration_bucket(out["baseline_projection"])
    return out


def _score(group: pd.DataFrame, coefficient: float) -> tuple[int, float, float]:
    pred = group["position_mean_pts"] + coefficient * (
        group["baseline_projection"] - group["position_mean_pts"]
    )
    err = pred - group["actual_pts"]
    valid = pred.notna() & group["actual_pts"].notna()
    err = err.loc[valid]
    if err.empty:
        return 0, np.nan, np.nan
    return len(err), float(err.abs().mean()), float(np.sqrt(np.square(err).mean()))


def _select_coefficient(group: pd.DataFrame, min_n: int) -> dict | None:
    clean = group.dropna(subset=["position_mean_pts", "baseline_projection", "actual_pts"])
    if len(clean) < min_n:
        return None
    candidates = []
    for coefficient in COEFFICIENT_GRID:
        n, mae, rmse = _score(clean, coefficient)
        if n >= min_n:
            candidates.append({"coefficient": coefficient, "n": n, "mae": mae, "rmse": rmse})
    if not candidates:
        return None
    return min(candidates, key=lambda x: (x["mae"], x["rmse"], abs(x["coefficient"] - DEFAULT_SHRINKAGE)))


def fit_hierarchy(training: pd.DataFrame) -> dict:
    """Fit all coefficients using development rows only."""
    if training.empty:
        raise ValueError("Development set is empty; cannot fit shrinkage.")
    fitted = {"position_bucket": {}, "position": {}, "global": None}
    for (position, bucket), group in training.groupby(["position", "projection_bucket"], observed=True):
        fit = _select_coefficient(group, MIN_BUCKET_N)
        if fit is not None:
            fitted["position_bucket"][(position, str(bucket))] = fit
    for position, group in training.groupby("position", dropna=False):
        fit = _select_coefficient(group, MIN_GROUP_N)
        if fit is not None:
            fitted["position"][position] = fit
    fitted["global"] = _select_coefficient(training, MIN_GROUP_N)
    return fitted


def resolve_coefficient(row: pd.Series, fitted: dict) -> tuple[float, str, int]:
    fit = fitted["position_bucket"].get((row["position"], str(row["projection_bucket"])))
    source = "position_bucket"
    if fit is None:
        fit = fitted["position"].get(row["position"])
        source = "position"
    if fit is None:
        fit = fitted["global"]
        source = "global"
    if fit is None:
        return 1.0, "none", 0
    return float(fit["coefficient"]), source, int(fit["n"])


def metric_row(df: pd.DataFrame, prediction_col: str, label: str, position: str = "ALL") -> dict:
    pred = pd.to_numeric(df[prediction_col], errors="coerce")
    actual = pd.to_numeric(df["actual_pts"], errors="coerce")
    valid = pred.notna() & actual.notna()
    pred, actual = pred.loc[valid], actual.loc[valid]
    error = pred - actual
    return {
        "position": position,
        "model": label,
        "n": int(len(error)),
        "mae": float(error.abs().mean()) if len(error) else np.nan,
        "rmse": float(np.sqrt(np.square(error).mean())) if len(error) else np.nan,
        "bias": float(error.mean()) if len(error) else np.nan,
        "mean_projection": float(pred.mean()) if len(error) else np.nan,
        "mean_actual": float(actual.mean()) if len(error) else np.nan,
        "correlation": float(pred.corr(actual)) if len(error) > 1 else np.nan,
    }


def week_paired_deltas(scored: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for week, group in scored.groupby("week", sort=True):
        valid = group.dropna(subset=["baseline_projection", "holdout_shrinkage_projection", "actual_pts"])
        if valid.empty:
            continue
        base_err = (valid["baseline_projection"] - valid["actual_pts"]).abs()
        shrink_err = (valid["holdout_shrinkage_projection"] - valid["actual_pts"]).abs()
        rows.append({
            "week": int(week), "n": len(valid),
            "baseline_mae": float(base_err.mean()),
            "shrinkage_mae": float(shrink_err.mean()),
            "mae_delta_shrinkage_minus_baseline": float((shrink_err - base_err).mean()),
            "baseline_rmse": float(np.sqrt(np.square(valid["baseline_projection"] - valid["actual_pts"]).mean())),
            "shrinkage_rmse": float(np.sqrt(np.square(valid["holdout_shrinkage_projection"] - valid["actual_pts"]).mean())),
        })
    return pd.DataFrame(rows)


def bootstrap_week_delta(week_metrics: pd.DataFrame, reps: int, seed: int) -> dict:
    """Cluster bootstrap weeks; each sampled week carries all its player rows via its MAE delta."""
    deltas = week_metrics["mae_delta_shrinkage_minus_baseline"].to_numpy(dtype=float)
    weights = week_metrics["n"].to_numpy(dtype=float)
    valid = np.isfinite(deltas) & np.isfinite(weights) & (weights > 0)
    deltas, weights = deltas[valid], weights[valid]
    if len(deltas) < 2:
        return {"weeks": len(deltas), "bootstrap_reps": reps, "delta_mae": np.nan, "ci95_low": np.nan, "ci95_high": np.nan, "bootstrap_share_delta_below_zero": np.nan}
    rng = np.random.default_rng(seed)
    samples = rng.integers(0, len(deltas), size=(reps, len(deltas)))
    sampled_deltas = deltas[samples]
    sampled_weights = weights[samples]
    estimates = (sampled_deltas * sampled_weights).sum(axis=1) / sampled_weights.sum(axis=1)
    point = float(np.average(deltas, weights=weights))
    return {
        "weeks": len(deltas), "bootstrap_reps": reps, "delta_mae": point,
        "ci95_low": float(np.quantile(estimates, 0.025)),
        "ci95_high": float(np.quantile(estimates, 0.975)),
        "bootstrap_share_delta_below_zero": float(np.mean(estimates < 0.0)),
    }


def run_experiment(input_path: Path, holdout_start_week: int, bootstrap_reps: int, seed: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    df = pd.read_csv(input_path)
    required = {"week", "position", "baseline_projection", "position_mean_pts", "actual_pts"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError("Input CSV missing required fields: " + ", ".join(sorted(missing)))
    df = _numeric_frame(df)
    df = df.dropna(subset=["week", "position", "baseline_projection", "position_mean_pts", "actual_pts"]).copy()
    df["week"] = df["week"].astype(int)
    development = df.loc[df["week"] < holdout_start_week].copy()
    holdout = df.loc[df["week"] >= holdout_start_week].copy()
    if development.empty or holdout.empty:
        raise ValueError(
            f"Need both development weeks < {holdout_start_week} and holdout weeks >= {holdout_start_week}; "
            f"found development={sorted(df.loc[df.week < holdout_start_week, 'week'].unique().tolist())}, "
            f"holdout={sorted(df.loc[df.week >= holdout_start_week, 'week'].unique().tolist())}."
        )
    if development["week"].max() >= holdout["week"].min():
        raise AssertionError("Temporal split is invalid.")

    fitted = fit_hierarchy(development)
    holdout = holdout.copy()
    resolutions = holdout.apply(lambda row: resolve_coefficient(row, fitted), axis=1, result_type="expand")
    resolutions.columns = ["holdout_coefficient", "coefficient_source", "coefficient_training_n"]
    holdout[resolutions.columns] = resolutions
    holdout["holdout_shrinkage_projection"] = (
        holdout["position_mean_pts"] + holdout["holdout_coefficient"] *
        (holdout["baseline_projection"] - holdout["position_mean_pts"])
    )
    holdout["global_shrinkage_085_projection"] = (
        holdout["position_mean_pts"] + DEFAULT_SHRINKAGE *
        (holdout["baseline_projection"] - holdout["position_mean_pts"])
    )
    holdout["projection_bucket"] = calibration_bucket(holdout["baseline_projection"])

    metric_rows = []
    for position in ("ALL", *POSITIONS):
        group = holdout if position == "ALL" else holdout.loc[holdout["position"] == position]
        for col, label in (
            ("baseline_projection", "baseline"),
            ("global_shrinkage_085_projection", "fixed_global_shrinkage_0.85"),
            ("holdout_shrinkage_projection", "development_fit_position_bucket_shrinkage"),
        ):
            metric_rows.append(metric_row(group, col, label, position))
    metrics = pd.DataFrame(metric_rows)

    bucket_rows = []
    for (position, bucket), group in holdout.groupby(["position", "projection_bucket"], observed=True):
        for col, label in (("baseline_projection", "baseline"), ("holdout_shrinkage_projection", "development_fit_position_bucket_shrinkage")):
            row = metric_row(group, col, label, position)
            row["bucket"] = str(bucket)
            bucket_rows.append(row)
    bucket_metrics = pd.DataFrame(bucket_rows)

    weeks = week_paired_deltas(holdout)
    bootstrap = bootstrap_week_delta(weeks, bootstrap_reps, seed)
    bootstrap["holdout_start_week"] = int(holdout_start_week)
    bootstrap["development_weeks"] = ",".join(map(str, sorted(development.week.unique())))
    bootstrap["holdout_weeks"] = ",".join(map(str, sorted(holdout.week.unique())))
    bootstrap["n_holdout_rows"] = int(len(holdout))
    bootstrap["n_training_rows"] = int(len(development))
    return metrics, bucket_metrics, weeks, {"summary": bootstrap, "scored": holdout, "fitted": fitted}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path, help="Detailed CSV from backtest_projections.py --output")
    parser.add_argument("--holdout-start-week", type=int, default=13, help="First untouched evaluation week; earlier weeks fit coefficients")
    parser.add_argument("--bootstrap-reps", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20261013)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/shrinkage_holdout"))
    args = parser.parse_args()
    if args.bootstrap_reps < 1000:
        parser.error("Use at least 1,000 bootstrap replicates; default is 10,000.")

    metrics, buckets, weeks, extra = run_experiment(args.input_csv, args.holdout_start_week, args.bootstrap_reps, args.seed)
    outdir = args.output_dir
    outdir.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(outdir / "holdout_metrics.csv", index=False)
    buckets.to_csv(outdir / "holdout_metrics_by_baseline_bucket.csv", index=False)
    weeks.to_csv(outdir / "holdout_weekly_paired_deltas.csv", index=False)
    extra["scored"].to_csv(outdir / "holdout_scored_rows.csv", index=False)
    pd.DataFrame([extra["summary"]]).to_csv(outdir / "holdout_bootstrap_summary.csv", index=False)

    print("\nFIXED TEMPORAL HOLDOUT METRICS (buckets fixed from baseline projection)")
    print(metrics.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nPOSITION × ORIGINAL BASELINE BUCKET")
    print(buckets.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nPAIRED HOLDOUT WEEK RESULTS")
    print(weeks.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nWEEK-CLUSTER BOOTSTRAP: shrinkage MAE minus baseline MAE")
    print(pd.DataFrame([extra["summary"]]).to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"\nWrote five report files to: {outdir.resolve()}")


if __name__ == "__main__":
    main()
