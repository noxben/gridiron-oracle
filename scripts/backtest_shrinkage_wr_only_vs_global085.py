#!/usr/bin/env python3
"""FFL Oracle: test WR-only bucket-shrinkage attenuation against global 0.85.

Model under test:
  * QB/RB/TE: fixed global coefficient 0.85.
  * WR: baseline + alpha_WR * (development-fitted position/bucket shrinkage - baseline).

alpha_WR is selected only from rolling-origin folds inside the development period.
A candidate must satisfy both pooled absolute bias and worst-fold absolute bias
limits; if none qualifies, the same deterministic fallback rule used by the
fold-stable position experiment is applied. The final holdout is untouched until
selection is frozen.

Required input columns: week, position, baseline_projection, position_mean_pts,
actual_pts. All comparisons are paired on the same valid holdout rows.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import numpy as np
import pandas as pd
import backtest_shrinkage_attenuation as attenuation_base
import backtest_shrinkage_holdout as base

DEFAULT_ATTENUATIONS = tuple(round(float(x), 2) for x in np.arange(0.0, 1.0001, 0.05))
GLOBAL_ALPHA = 0.85


def load_frame(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"week", "position", "baseline_projection", "position_mean_pts", "actual_pts"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError("Input CSV missing required fields: " + ", ".join(sorted(missing)))
    df = base._numeric_frame(df).dropna(subset=list(required)).copy()
    df["week"] = df["week"].astype(int)
    df["position"] = df["position"].astype(str).str.upper()
    unexpected = sorted(set(df.position.unique()) - set(base.POSITIONS))
    if unexpected:
        raise ValueError(f"Unexpected positions: {unexpected}; expected {list(base.POSITIONS)}")
    return df


def wr_cv(development: pd.DataFrame, attenuations: tuple[float, ...], min_prior_weeks: int):
    rows = []
    weeks = sorted(development.week.unique().tolist())
    for validation_week in weeks:
        prior = development.loc[development.week < validation_week]
        if prior.week.nunique() < min_prior_weeks:
            continue
        validation = development.loc[development.week == validation_week]
        fitted = base.fit_hierarchy(prior)
        scored = attenuation_base.apply_fitted(validation, fitted)
        wr = scored.loc[scored.position == "WR"]
        if wr.empty:
            continue
        for alpha in attenuations:
            pred = wr.baseline_projection + alpha * (wr.full_shrinkage_projection - wr.baseline_projection)
            m = attenuation_base.score_predictions(pred, wr.actual_pts)
            rows.append({"validation_week": int(validation_week), "position": "WR",
                         "attenuation": float(alpha), **m})
    cv = pd.DataFrame(rows)
    if cv.empty:
        raise ValueError("No WR rolling-origin folds; lower --min-prior-weeks or provide more development weeks.")
    summaries = []
    for alpha, group in cv.groupby("attenuation", sort=True):
        valid = group.loc[group.n > 0]
        n = int(valid.n.sum())
        summaries.append({
            "position": "WR", "attenuation": float(alpha), "n": n,
            "weighted_mae": float(np.average(valid.mae, weights=valid.n)) if n else np.nan,
            "weighted_rmse": float(np.sqrt(np.average(np.square(valid.rmse), weights=valid.n))) if n else np.nan,
            "weighted_bias": float(np.average(valid.bias, weights=valid.n)) if n else np.nan,
            "validation_weeks": int(valid.validation_week.nunique()),
        })
    return cv, pd.DataFrame(summaries)


def select_wr(summary: pd.DataFrame, cv: pd.DataFrame, max_abs_bias: float, max_fold_abs_bias: float):
    out = summary.copy()
    out["abs_weighted_bias"] = out.weighted_bias.abs()
    fold = cv.groupby("attenuation", as_index=False).agg(
        max_fold_abs_bias=("bias", lambda x: float(x.abs().max())),
        mean_fold_abs_bias=("bias", lambda x: float(x.abs().mean())),
        fold_bias_sd=("bias", "std"),
    )
    out = out.merge(fold, on="attenuation", how="left")
    out["bias_feasible"] = ((out.abs_weighted_bias <= max_abs_bias + 1e-12) &
                             (out.max_fold_abs_bias <= max_fold_abs_bias + 1e-12))
    feasible = out.loc[out.bias_feasible]
    if not feasible.empty:
        ordered = feasible.sort_values(["weighted_mae", "weighted_rmse", "attenuation"])
        rule = "lowest MAE among WR candidates satisfying pooled and fold bias constraints"
    else:
        ordered = out.sort_values(["max_fold_abs_bias", "abs_weighted_bias", "weighted_mae", "weighted_rmse", "attenuation"])
        rule = "no feasible WR candidate; minimum worst-fold absolute bias fallback"
    chosen = float(ordered.iloc[0].attenuation)
    out["selection_rule"] = rule
    out["selected"] = np.isclose(out.attenuation, chosen)
    out = out.sort_values(["selected", "bias_feasible", "weighted_mae"], ascending=[False, False, True]).reset_index(drop=True)
    return out, chosen


def metric_delta_by_week(scored: pd.DataFrame, candidate_col: str, comparator_col: str, prefix: str) -> pd.DataFrame:
    rows = []
    for week, g in scored.groupby("week", sort=True):
        valid = g.dropna(subset=[candidate_col, comparator_col, "actual_pts"])
        if valid.empty:
            continue
        ec = valid[candidate_col] - valid.actual_pts
        eb = valid[comparator_col] - valid.actual_pts
        rows.append({
            "week": int(week), "n": int(len(valid)),
            f"{prefix}_candidate_mae": float(ec.abs().mean()),
            f"{prefix}_comparator_mae": float(eb.abs().mean()),
            f"{prefix}_mae_delta": float((ec.abs() - eb.abs()).mean()),
            f"{prefix}_candidate_rmse": float(np.sqrt(np.square(ec).mean())),
            f"{prefix}_comparator_rmse": float(np.sqrt(np.square(eb).mean())),
            f"{prefix}_candidate_bias": float(ec.mean()),
            f"{prefix}_comparator_bias": float(eb.mean()),
        })
    return pd.DataFrame(rows)


def bootstrap_delta(weekly: pd.DataFrame, delta_col: str, reps: int, seed: int) -> dict:
    deltas = weekly[delta_col].to_numpy(dtype=float)
    weights = weekly.n.to_numpy(dtype=float)
    valid = np.isfinite(deltas) & np.isfinite(weights) & (weights > 0)
    deltas, weights = deltas[valid], weights[valid]
    if len(deltas) < 2:
        return {"weeks": len(deltas), "bootstrap_reps": reps, "delta_mae": np.nan,
                "ci95_low": np.nan, "ci95_high": np.nan, "bootstrap_share_delta_below_zero": np.nan}
    rng = np.random.default_rng(seed)
    ix = rng.integers(0, len(deltas), size=(reps, len(deltas)))
    d, w = deltas[ix], weights[ix]
    estimates = (d * w).sum(axis=1) / w.sum(axis=1)
    return {"weeks": int(len(deltas)), "bootstrap_reps": int(reps),
            "delta_mae": float(np.average(deltas, weights=weights)),
            "ci95_low": float(np.quantile(estimates, .025)),
            "ci95_high": float(np.quantile(estimates, .975)),
            "bootstrap_share_delta_below_zero": float(np.mean(estimates < 0))}


def run(input_csv: Path, holdout_start_week: int, bootstrap_reps: int, seed: int,
        attenuations: tuple[float, ...], min_prior_weeks: int,
        max_abs_bias: float, max_fold_abs_bias: float):
    df = load_frame(input_csv)
    development = df.loc[df.week < holdout_start_week].copy()
    holdout = df.loc[df.week >= holdout_start_week].copy()
    if development.empty or holdout.empty:
        raise ValueError("Need development and holdout rows on opposite sides of --holdout-start-week.")
    if development.week.max() >= holdout.week.min():
        raise AssertionError("Temporal split is invalid.")

    cv, cv_summary = wr_cv(development, attenuations, min_prior_weeks)
    selection, wr_alpha = select_wr(cv_summary, cv, max_abs_bias, max_fold_abs_bias)

    fitted = base.fit_hierarchy(development)
    scored = attenuation_base.apply_fitted(holdout, fitted)
    scored["wr_alpha"] = wr_alpha
    scored["global_085_projection"] = scored.position_mean_pts + GLOBAL_ALPHA * (
        scored.baseline_projection - scored.position_mean_pts)
    scored["full_position_bucket_projection"] = scored.full_shrinkage_projection
    scored["wr_only_candidate_projection"] = scored.global_085_projection
    wr_mask = scored.position.eq("WR")
    scored.loc[wr_mask, "wr_only_candidate_projection"] = (
        scored.loc[wr_mask, "baseline_projection"] + wr_alpha *
        (scored.loc[wr_mask, "full_shrinkage_projection"] - scored.loc[wr_mask, "baseline_projection"]))

    model_cols = [
        ("baseline_projection", "baseline"),
        ("global_085_projection", "fixed_global_shrinkage_0.85"),
        ("full_position_bucket_projection", "full_position_bucket_shrinkage"),
        ("wr_only_candidate_projection", f"global_0.85_plus_WR_attenuation_{wr_alpha:.2f}"),
    ]
    metrics = pd.DataFrame([
        {**base.metric_row(g, col, label, pos), **({"wr_alpha": wr_alpha} if label.startswith("global_0.85_plus") and pos == "ALL" else {})}
        for pos in ("ALL", *base.POSITIONS)
        for g in [scored if pos == "ALL" else scored.loc[scored.position == pos]]
        for col, label in model_cols
    ])

    weekly_global = metric_delta_by_week(scored, "wr_only_candidate_projection", "global_085_projection", "vs_global085")
    weekly_base = metric_delta_by_week(scored, "wr_only_candidate_projection", "baseline_projection", "vs_baseline")
    weekly = weekly_global.merge(weekly_base, on=["week", "n"], how="outer")
    boot_rows = []
    for i, (label, delta_col) in enumerate((("vs_global_085", "vs_global085_mae_delta"),
                                             ("vs_baseline", "vs_baseline_mae_delta"))):
        summary = bootstrap_delta(weekly, delta_col, bootstrap_reps, seed + i)
        summary.update({"comparison": label, "wr_alpha": wr_alpha,
                        "holdout_start_week": int(holdout_start_week),
                        "development_weeks": ",".join(map(str, sorted(development.week.unique()))),
                        "holdout_weeks": ",".join(map(str, sorted(holdout.week.unique()))),
                        "n_holdout_rows": int(len(holdout)), "n_development_rows": int(len(development)),
                        "max_abs_development_bias": max_abs_bias,
                        "max_fold_abs_development_bias": max_fold_abs_bias})
        boot_rows.append(summary)
    bootstrap = pd.DataFrame(boot_rows)

    return cv, selection, metrics, weekly, scored, bootstrap


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("--holdout-start-week", type=int, default=13)
    parser.add_argument("--bootstrap-reps", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20261014)
    parser.add_argument("--min-prior-weeks", type=int, default=3)
    parser.add_argument("--max-abs-bias", type=float, default=0.25)
    parser.add_argument("--max-fold-abs-bias", type=float, default=0.50)
    parser.add_argument("--attenuations", type=float, nargs="+", default=list(DEFAULT_ATTENUATIONS))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/shrinkage_wr_only_vs_global085"))
    args = parser.parse_args()
    if args.bootstrap_reps < 1000:
        parser.error("Use at least 1,000 bootstrap replicates.")
    if args.max_abs_bias < 0 or args.max_fold_abs_bias < 0:
        parser.error("Bias limits must be non-negative.")
    if any(a < 0 or a > 1 for a in args.attenuations):
        parser.error("Attenuations must be between 0 and 1.")
    if len(set(args.attenuations)) != len(args.attenuations):
        parser.error("Attenuations must be unique.")

    cv, selection, metrics, weekly, scored, bootstrap = run(
        args.input_csv, args.holdout_start_week, args.bootstrap_reps, args.seed,
        tuple(sorted(args.attenuations)), args.min_prior_weeks,
        args.max_abs_bias, args.max_fold_abs_bias)
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    cv.to_csv(out / "development_wr_rolling_origin_folds.csv", index=False)
    selection.to_csv(out / "development_wr_attenuation_selection.csv", index=False)
    metrics.to_csv(out / "holdout_model_metrics.csv", index=False)
    weekly.to_csv(out / "holdout_weekly_comparisons.csv", index=False)
    scored.to_csv(out / "holdout_scored_rows.csv", index=False)
    bootstrap.to_csv(out / "holdout_week_cluster_bootstrap.csv", index=False)

    print("\nDEVELOPMENT-ONLY WR ATTENUATION SELECTION")
    print(selection.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"\nFrozen WR attenuation: {selection.loc[selection.selected, 'attenuation'].iloc[0]:.2f}; QB/RB/TE fixed at global coefficient {GLOBAL_ALPHA:.2f}")
    print("\nFIXED HOLDOUT METRICS")
    print(metrics.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nHOLDOUT WEEKLY COMPARISONS")
    print(weekly.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nWEEK-CLUSTER BOOTSTRAP: candidate MAE minus comparator MAE")
    print(bootstrap.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"\nWrote six reports to: {out.resolve()}")

if __name__ == "__main__":
    main()
