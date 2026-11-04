#!/usr/bin/env python3
"""FFL Oracle: test whether per-origin WR attenuation selection beats a frozen WR alpha.

Uses identical expanding-window, non-overlapping holdouts for:
  1) global shrinkage 0.85 for all positions;
  2) global 0.85 for QB/RB/TE plus a frozen WR alpha (default 0.50);
  3) global 0.85 for QB/RB/TE plus WR alpha selected from development-only CV at each origin.

The fixed alpha is specified before evaluation; it is not tuned on the combined OOS
weeks. Full position/bucket shrinkage is retained as a diagnostic comparator. Production
settings are never changed.

Run beside backtest_shrinkage_wr_only_vs_global085.py and its two helper modules.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import numpy as np
import pandas as pd
import backtest_shrinkage_wr_only_vs_global085 as wr_base

GLOBAL_ALPHA = 0.85


def metrics_for(df: pd.DataFrame, projection: str, label: str, origin: int, end_week: int, alpha: float | None) -> dict:
    err = df[projection].to_numpy(dtype=float) - df.actual_pts.to_numpy(dtype=float)
    valid = np.isfinite(err)
    err = err[valid]
    actual = df.actual_pts.to_numpy(dtype=float)[valid]
    pred = df[projection].to_numpy(dtype=float)[valid]
    corr = float(np.corrcoef(pred, actual)[0, 1]) if len(err) > 1 and np.std(pred) > 0 and np.std(actual) > 0 else np.nan
    return {
        "origin_week": origin, "holdout_end_week": end_week, "wr_alpha": alpha,
        "position": "ALL", "model": label, "n": int(len(err)),
        "mae": float(np.mean(np.abs(err))), "rmse": float(np.sqrt(np.mean(err ** 2))),
        "bias": float(np.mean(err)), "mean_projection": float(np.mean(pred)),
        "mean_actual": float(np.mean(actual)), "correlation": corr,
    }


def make_predictions(scored: pd.DataFrame, alpha: float) -> pd.DataFrame:
    out = scored.copy()
    out["global_085_projection"] = out.position_mean_pts + GLOBAL_ALPHA * (out.baseline_projection - out.position_mean_pts)
    out["fixed_wr_projection"] = out["global_085_projection"]
    mask = out.position.eq("WR")
    out.loc[mask, "fixed_wr_projection"] = out.loc[mask, "baseline_projection"] + alpha * (
        out.loc[mask, "full_shrinkage_projection"] - out.loc[mask, "baseline_projection"])
    return out


def paired_weekly(df: pd.DataFrame, candidate_col: str, comparator_col: str, origin: int, end_week: int) -> pd.DataFrame:
    rows = []
    for week, g in df.groupby("week", sort=True):
        valid = g.dropna(subset=[candidate_col, comparator_col, "actual_pts"])
        if valid.empty:
            continue
        ec = valid[candidate_col].to_numpy(float) - valid.actual_pts.to_numpy(float)
        eb = valid[comparator_col].to_numpy(float) - valid.actual_pts.to_numpy(float)
        rows.append({
            "origin_week": origin, "holdout_end_week": end_week, "week": int(week), "n": len(valid),
            "candidate_mae": float(np.mean(np.abs(ec))), "comparator_mae": float(np.mean(np.abs(eb))),
            "mae_delta": float(np.mean(np.abs(ec) - np.abs(eb))),
            "candidate_rmse": float(np.sqrt(np.mean(ec ** 2))), "comparator_rmse": float(np.sqrt(np.mean(eb ** 2))),
            "candidate_bias": float(np.mean(ec)), "comparator_bias": float(np.mean(eb)),
        })
    return pd.DataFrame(rows)


def bootstrap(weekly: pd.DataFrame, reps: int, seed: int) -> dict:
    d = weekly.mae_delta.to_numpy(float)
    w = weekly.n.to_numpy(float)
    good = np.isfinite(d) & np.isfinite(w) & (w > 0)
    d, w = d[good], w[good]
    if len(d) < 2:
        raise ValueError("Need at least two out-of-sample weeks for week-cluster bootstrap.")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), size=(reps, len(d)))
    sampled_d, sampled_w = d[idx], w[idx]
    estimates = (sampled_d * sampled_w).sum(axis=1) / sampled_w.sum(axis=1)
    return {
        "weeks": int(len(d)), "bootstrap_reps": int(reps),
        "delta_mae": float(np.average(d, weights=w)),
        "ci95_low": float(np.quantile(estimates, .025)), "ci95_high": float(np.quantile(estimates, .975)),
        "share_delta_below_zero": float(np.mean(estimates < 0)),
    }


def run(input_csv: Path, first_origin: int, window_weeks: int, step_weeks: int,
        min_prior_weeks: int, max_abs_bias: float, max_fold_abs_bias: float,
        fixed_wr_alpha: float, attenuations: tuple[float, ...], bootstrap_reps: int, seed: int):
    df = wr_base.load_frame(input_csv)
    weeks = sorted(int(w) for w in df.week.unique())
    origins = []
    origin = first_origin
    while True:
        holdout_weeks = [w for w in weeks if origin <= w < origin + window_weeks]
        if len(holdout_weeks) != window_weeks:
            break
        if not any(w < origin for w in weeks):
            raise ValueError(f"Origin {origin} has no prior development weeks.")
        origins.append((origin, origin + window_weeks - 1, holdout_weeks))
        origin += step_weeks
    if not origins:
        raise ValueError("No complete holdout windows. Check --first-origin, --window-weeks, and available weeks.")
    all_metrics, all_weekly, all_scored, all_selection = [], [], [], []
    for origin, end_week, holdout_weeks in origins:
        dev = df.loc[df.week < origin].copy()
        hold = df.loc[df.week.isin(holdout_weeks)].copy()
        if dev.week.nunique() < min_prior_weeks:
            raise ValueError(f"Origin {origin}: only {dev.week.nunique()} prior weeks; need {min_prior_weeks}.")
        cv, summary = wr_base.wr_cv(dev, attenuations, min_prior_weeks)
        selection, selected_alpha = wr_base.select_wr(summary, cv, max_abs_bias, max_fold_abs_bias)
        selection["origin_week"] = origin
        all_selection.append(selection)
        fitted = wr_base.base.fit_hierarchy(dev)
        scored = wr_base.attenuation_base.apply_fitted(hold, fitted)
        scored = make_predictions(scored, fixed_wr_alpha)
        scored["selected_wr_projection"] = scored.global_085_projection
        wr_mask = scored.position.eq("WR")
        scored.loc[wr_mask, "selected_wr_projection"] = scored.loc[wr_mask, "baseline_projection"] + selected_alpha * (
            scored.loc[wr_mask, "full_shrinkage_projection"] - scored.loc[wr_mask, "baseline_projection"])
        scored["origin_week"] = origin
        scored["holdout_end_week"] = end_week
        scored["selected_wr_alpha"] = selected_alpha
        all_scored.append(scored)
        model_defs = [
            ("baseline_projection", "baseline", np.nan),
            ("global_085_projection", "global_0.85", np.nan),
            ("full_shrinkage_projection", "full_position_bucket_shrinkage", np.nan),
            ("fixed_wr_projection", f"global_0.85_plus_fixed_WR_{fixed_wr_alpha:.2f}", fixed_wr_alpha),
            ("selected_wr_projection", f"global_0.85_plus_selected_WR_{selected_alpha:.2f}", selected_alpha),
        ]
        for col, label, alpha in model_defs:
            all_metrics.append(metrics_for(scored, col, label, origin, end_week, alpha))
        for cand_col, label in (("fixed_wr_projection", "fixed_wr_vs_global"), ("selected_wr_projection", "selected_wr_vs_global"),
                                ("selected_wr_projection", "selected_vs_fixed_wr")):
            comp_col = "global_085_projection" if label != "selected_vs_fixed_wr" else "fixed_wr_projection"
            w = paired_weekly(scored, cand_col, comp_col, origin, end_week)
            w["comparison"] = label
            all_weekly.append(w)
    metrics = pd.DataFrame(all_metrics)
    weekly = pd.concat(all_weekly, ignore_index=True)
    scored_all = pd.concat(all_scored, ignore_index=True)
    # The holdout windows are required to be non-overlapping for valid combined OOS aggregation.
    if len(set(scored_all.week)) != scored_all.week.nunique():
        raise AssertionError("Holdout weeks overlap across origins; combined OOS metrics would double-count rows.")
    combined_rows = []
    for col, label, alpha_col in [
        ("baseline_projection", "baseline", None), ("global_085_projection", "global_0.85", None),
        ("full_shrinkage_projection", "full_position_bucket_shrinkage", None),
        ("fixed_wr_projection", f"fixed_WR_{fixed_wr_alpha:.2f}", None),
        ("selected_wr_projection", "rolling_selected_WR", "selected_wr_alpha"),
    ]:
        row = metrics_for(scored_all, col, label, first_origin, int(scored_all.week.max()), fixed_wr_alpha if label.startswith("fixed_WR") else None)
        row["origin_week"] = np.nan
        row["holdout_end_week"] = int(scored_all.week.max())
        row["origins"] = ",".join(map(str, sorted(scored_all.origin_week.unique())))
        row["oos_weeks"] = ",".join(map(str, sorted(scored_all.week.unique())))
        combined_rows.append(row)
    combined = pd.DataFrame(combined_rows)
    boots = []
    for i, comparison in enumerate(("fixed_wr_vs_global", "selected_wr_vs_global", "selected_vs_fixed_wr")):
        sub = weekly.loc[weekly.comparison == comparison]
        # Paired week clusters across the combined OOS evaluation.
        b = bootstrap(sub, bootstrap_reps, seed + i)
        b["comparison"] = comparison
        b["origins"] = ",".join(map(str, sorted(scored_all.origin_week.unique())))
        b["oos_weeks"] = ",".join(map(str, sorted(scored_all.week.unique())))
        b["fixed_wr_alpha"] = fixed_wr_alpha
        boots.append(b)
    return pd.concat(all_selection, ignore_index=True), metrics, combined, weekly, scored_all, pd.DataFrame(boots)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input_csv", type=Path)
    p.add_argument("--first-origin", type=int, default=11)
    p.add_argument("--window-weeks", type=int, default=2)
    p.add_argument("--step-weeks", type=int, default=2)
    p.add_argument("--min-prior-weeks", type=int, default=3)
    p.add_argument("--max-abs-bias", type=float, default=.25)
    p.add_argument("--max-fold-abs-bias", type=float, default=.50)
    p.add_argument("--fixed-wr-alpha", type=float, default=.50)
    p.add_argument("--attenuations", type=float, nargs="+", default=list(wr_base.DEFAULT_ATTENUATIONS))
    p.add_argument("--bootstrap-reps", type=int, default=10000)
    p.add_argument("--seed", type=int, default=20261020)
    p.add_argument("--output-dir", type=Path, default=Path("outputs/shrinkage_wr_alpha_stability"))
    a = p.parse_args()
    if a.window_weeks < 1 or a.step_weeks < a.window_weeks:
        p.error("Require window-weeks >= 1 and step-weeks >= window-weeks to avoid overlapping holdouts.")
    if a.min_prior_weeks < 1 or a.bootstrap_reps < 1000:
        p.error("Require min-prior-weeks >= 1 and bootstrap-reps >= 1000.")
    if not 0 <= a.fixed_wr_alpha <= 1 or any(x < 0 or x > 1 for x in a.attenuations):
        p.error("All attenuation values must be within [0, 1].")
    if a.max_abs_bias < 0 or a.max_fold_abs_bias < 0:
        p.error("Bias constraints must be non-negative.")
    if len(set(a.attenuations)) != len(a.attenuations):
        p.error("Attenuation candidates must be unique.")
    selection, metrics, combined, weekly, scored, boots = run(
        a.input_csv, a.first_origin, a.window_weeks, a.step_weeks, a.min_prior_weeks,
        a.max_abs_bias, a.max_fold_abs_bias, a.fixed_wr_alpha, tuple(sorted(a.attenuations)),
        a.bootstrap_reps, a.seed)
    out = a.output_dir
    out.mkdir(parents=True, exist_ok=True)
    selection.to_csv(out / "development_wr_selection_by_origin.csv", index=False)
    metrics.to_csv(out / "holdout_metrics_by_origin.csv", index=False)
    combined.to_csv(out / "combined_oos_metrics.csv", index=False)
    weekly.to_csv(out / "holdout_weekly_comparisons.csv", index=False)
    scored.to_csv(out / "combined_oos_scored_rows.csv", index=False)
    boots.to_csv(out / "week_cluster_bootstrap.csv", index=False)
    print("\nWR ATTENUATION SELECTED USING DEVELOPMENT DATA ONLY")
    print(selection.loc[selection.selected, ["origin_week", "attenuation", "weighted_mae", "weighted_bias", "max_fold_abs_bias", "bias_feasible", "selection_rule"]].to_string(index=False))
    print(f"\nFIXED WR ALPHA: {a.fixed_wr_alpha:.2f}; QB/RB/TE remain at global 0.85")
    print("\nPER-ORIGIN HOLDOUT METRICS")
    print(metrics.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nCOMBINED NON-OVERLAPPING OOS METRICS")
    print(combined.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nWEEK-CLUSTER BOOTSTRAP")
    print(boots.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"\nWrote six reports to: {out.resolve()}")

if __name__ == "__main__":
    main()
