#!/usr/bin/env python3
"""FFL Oracle: frozen WR-alpha walk-forward evaluation (no alpha selection).

Compares a pre-frozen WR attenuation (default 0.50) with global shrinkage 0.85.
For each target week W, the shrinkage hierarchy is fitted only on rows with week < W.
QB/RB/TE remain at global 0.85; WR uses baseline + alpha*(full fitted shrinkage - baseline).

This is a prospective/rolling evaluation tool, not a license to reselect alpha on
its evaluation weeks. Do not interpret previously examined 2025 holdout weeks as a
fresh confirmatory test. Production settings are never modified.

Required input columns: week, position, baseline_projection, position_mean_pts,
actual_pts. Run beside backtest_shrinkage_wr_only_vs_global085.py and its helpers.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import numpy as np
import pandas as pd
import backtest_shrinkage_wr_only_vs_global085 as wr_base

GLOBAL_ALPHA = 0.85


def metrics(df: pd.DataFrame, col: str, model: str, position: str, alpha: float) -> dict:
    g = df if position == "ALL" else df.loc[df.position.eq(position)]
    x = pd.to_numeric(g[col], errors="coerce")
    y = pd.to_numeric(g.actual_pts, errors="coerce")
    valid = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
    x, y = x.loc[valid].to_numpy(float), y.loc[valid].to_numpy(float)
    err = x - y
    corr = float(np.corrcoef(x, y)[0, 1]) if len(x) > 1 and np.std(x) > 0 and np.std(y) > 0 else np.nan
    return {"position": position, "model": model, "wr_alpha": alpha, "n": len(err),
            "mae": float(np.mean(np.abs(err))) if len(err) else np.nan,
            "rmse": float(np.sqrt(np.mean(err**2))) if len(err) else np.nan,
            "bias": float(np.mean(err)) if len(err) else np.nan,
            "mean_projection": float(np.mean(x)) if len(err) else np.nan,
            "mean_actual": float(np.mean(y)) if len(err) else np.nan,
            "correlation": corr}


def weekly_deltas(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for week, g in df.groupby("week", sort=True):
        for pos in ("ALL", *wr_base.base.POSITIONS):
            s = g if pos == "ALL" else g.loc[g.position.eq(pos)]
            valid = s.dropna(subset=["fixed_wr_projection", "global_085_projection", "actual_pts"])
            if valid.empty:
                continue
            ec = valid.fixed_wr_projection.to_numpy(float) - valid.actual_pts.to_numpy(float)
            eg = valid.global_085_projection.to_numpy(float) - valid.actual_pts.to_numpy(float)
            rows.append({"week": int(week), "position": pos, "n": len(valid),
                         "fixed_mae": float(np.mean(np.abs(ec))), "global085_mae": float(np.mean(np.abs(eg))),
                         "mae_delta_fixed_minus_global": float(np.mean(np.abs(ec) - np.abs(eg))),
                         "fixed_rmse": float(np.sqrt(np.mean(ec**2))), "global085_rmse": float(np.sqrt(np.mean(eg**2))),
                         "fixed_bias": float(np.mean(ec)), "global085_bias": float(np.mean(eg))})
    return pd.DataFrame(rows)


def cluster_bootstrap(weekly: pd.DataFrame, reps: int, seed: int) -> pd.DataFrame:
    rows = []
    for j, pos in enumerate(("ALL", *wr_base.base.POSITIONS)):
        s = weekly.loc[weekly.position.eq(pos)].copy()
        d = s.mae_delta_fixed_minus_global.to_numpy(float)
        w = s.n.to_numpy(float)
        good = np.isfinite(d) & np.isfinite(w) & (w > 0)
        d, w = d[good], w[good]
        if len(d) < 2:
            rows.append({"position": pos, "weeks": len(d), "bootstrap_reps": reps,
                         "delta_mae": np.nan, "ci95_low": np.nan, "ci95_high": np.nan,
                         "share_delta_below_zero": np.nan})
            continue
        rng = np.random.default_rng(seed + j)
        idx = rng.integers(0, len(d), size=(reps, len(d)))
        sampled_d, sampled_w = d[idx], w[idx]
        estimates = (sampled_d * sampled_w).sum(axis=1) / sampled_w.sum(axis=1)
        rows.append({"position": pos, "weeks": len(d), "bootstrap_reps": reps,
                     "delta_mae": float(np.average(d, weights=w)),
                     "ci95_low": float(np.quantile(estimates, .025)),
                     "ci95_high": float(np.quantile(estimates, .975)),
                     "share_delta_below_zero": float(np.mean(estimates < 0))})
    return pd.DataFrame(rows)


def run(input_csv: Path, alpha: float, first_eval_week: int, last_eval_week: int | None,
        min_train_weeks: int, reps: int, seed: int):
    df = wr_base.load_frame(input_csv)
    eval_last = int(df.week.max()) if last_eval_week is None else last_eval_week
    eval_weeks = [int(w) for w in sorted(df.week.unique()) if first_eval_week <= w <= eval_last]
    if not eval_weeks:
        raise ValueError("No evaluation weeks in requested range.")
    scored_parts = []
    for week in eval_weeks:
        train = df.loc[df.week < week].copy()
        target = df.loc[df.week.eq(week)].copy()
        if train.week.nunique() < min_train_weeks:
            continue
        if train.empty or target.empty or train.week.max() >= target.week.min():
            raise AssertionError(f"Invalid temporal split at target week {week}.")
        fitted = wr_base.base.fit_hierarchy(train)
        scored = wr_base.attenuation_base.apply_fitted(target, fitted)
        scored["global_085_projection"] = scored.position_mean_pts + GLOBAL_ALPHA * (
            scored.baseline_projection - scored.position_mean_pts)
        scored["fixed_wr_projection"] = scored.global_085_projection
        wr = scored.position.eq("WR")
        scored.loc[wr, "fixed_wr_projection"] = scored.loc[wr, "baseline_projection"] + alpha * (
            scored.loc[wr, "full_shrinkage_projection"] - scored.loc[wr, "baseline_projection"])
        scored["train_max_week"] = int(train.week.max())
        scored["eval_week"] = week
        scored_parts.append(scored)
    if not scored_parts:
        raise ValueError("No weeks scored; lower --min-train-weeks or widen the evaluation range.")
    scored_all = pd.concat(scored_parts, ignore_index=True)
    if scored_all.week.duplicated().any():
        # Rows repeat by player, so check unique eval-week coverage rather than row uniqueness.
        pass
    coverage = scored_all.groupby("week").agg(n=("actual_pts", "size"), train_max_week=("train_max_week", "max")).reset_index()
    if not (coverage.train_max_week < coverage.week).all():
        raise AssertionError("Leakage check failed: training contains target or future week.")
    model_defs = [("baseline_projection", "baseline"), ("global_085_projection", "global_0.85"),
                  ("full_shrinkage_projection", "full_position_bucket_shrinkage"),
                  ("fixed_wr_projection", f"fixed_WR_{alpha:.2f}")]
    metric_rows = [metrics(scored_all, col, label, pos, alpha)
                   for pos in ("ALL", *wr_base.base.POSITIONS) for col, label in model_defs]
    metric_df = pd.DataFrame(metric_rows)
    weekly = weekly_deltas(scored_all)
    boots = cluster_bootstrap(weekly, reps, seed)
    return scored_all, coverage, metric_df, weekly, boots


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input_csv", type=Path)
    p.add_argument("--alpha", type=float, default=0.50)
    p.add_argument("--first-eval-week", type=int, required=True,
                   help="First week scored out-of-sample; each week fits using prior weeks only.")
    p.add_argument("--last-eval-week", type=int, default=None)
    p.add_argument("--min-train-weeks", type=int, default=3)
    p.add_argument("--bootstrap-reps", type=int, default=10000)
    p.add_argument("--seed", type=int, default=20261020)
    p.add_argument("--output-dir", type=Path, default=Path("outputs/shrinkage_wr_frozen_alpha"))
    a = p.parse_args()
    if not 0 <= a.alpha <= 1:
        p.error("--alpha must be in [0, 1].")
    if a.min_train_weeks < 1 or a.bootstrap_reps < 1000:
        p.error("Require --min-train-weeks >= 1 and --bootstrap-reps >= 1000.")
    if a.last_eval_week is not None and a.last_eval_week < a.first_eval_week:
        p.error("--last-eval-week must be >= --first-eval-week.")
    scored, coverage, metric_df, weekly, boots = run(
        a.input_csv, a.alpha, a.first_eval_week, a.last_eval_week, a.min_train_weeks, a.bootstrap_reps, a.seed)
    out = a.output_dir
    out.mkdir(parents=True, exist_ok=True)
    coverage.to_csv(out / "week_coverage_and_leakage_check.csv", index=False)
    metric_df.to_csv(out / "metrics_overall_and_position.csv", index=False)
    weekly.to_csv(out / "weekly_paired_metrics.csv", index=False)
    boots.to_csv(out / "week_cluster_bootstrap_by_position.csv", index=False)
    scored.to_csv(out / "scored_rows.csv", index=False)
    print("\nFROZEN WR ALPHA WALK-FORWARD EVALUATION")
    print(f"WR alpha={a.alpha:.2f}; QB/RB/TE remain at global 0.85; scored weeks={coverage.week.min()}-{coverage.week.max()}; rows={len(scored)}")
    print("\nOVERALL + POSITION METRICS")
    print(metric_df.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nWEEK-CLUSTER BOOTSTRAP: fixed WR minus global 0.85 MAE")
    print(boots.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"\nLeakage check passed for {len(coverage)} scored weeks. Reports: {out.resolve()}")

if __name__ == "__main__":
    main()
