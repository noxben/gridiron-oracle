#!/usr/bin/env python3
"""FFL Oracle: expanding-window, multi-origin evaluation of WR-only attenuation.

At each origin, choose WR attenuation from rolling-origin folds strictly before
that origin. Freeze the choice for the next N weeks. QB/RB/TE always use global
shrinkage 0.85. Test windows are sequential and non-overlapping; after each
window, its outcomes may enter training for later origins, as in real deployment.

Outputs per-origin selections/metrics, week-level OOS comparisons, combined
OOS metrics, and week-cluster bootstrap comparisons. This script never changes
production settings.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import numpy as np
import pandas as pd
import backtest_shrinkage_attenuation as attenuation_base
import backtest_shrinkage_holdout as base

GLOBAL_ALPHA = 0.85
DEFAULT_ATTENUATIONS = tuple(round(float(x), 2) for x in np.arange(0.0, 1.0001, 0.05))
REQUIRED = {"week", "position", "baseline_projection", "position_mean_pts", "actual_pts"}


def load_frame(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = REQUIRED - set(df.columns)
    if missing:
        raise ValueError("Input CSV missing required fields: " + ", ".join(sorted(missing)))
    df = base._numeric_frame(df).dropna(subset=list(REQUIRED)).copy()
    df["week"] = df.week.astype(int)
    df["position"] = df.position.astype(str).str.upper()
    unexpected = sorted(set(df.position.unique()) - set(base.POSITIONS))
    if unexpected:
        raise ValueError(f"Unexpected positions: {unexpected}; expected {list(base.POSITIONS)}")
    if df.empty:
        raise ValueError("No valid rows remain after required-field cleaning.")
    if (df.week < 1).any():
        raise ValueError("Week numbers must be positive integers.")
    return df


def wr_cv(development: pd.DataFrame, attenuations: tuple[float, ...], min_prior_weeks: int):
    rows = []
    for validation_week in sorted(development.week.unique().tolist()):
        prior = development.loc[development.week < validation_week]
        if prior.week.nunique() < min_prior_weeks:
            continue
        validation = development.loc[development.week == validation_week]
        fitted = base.fit_hierarchy(prior)
        scored = attenuation_base.apply_fitted(validation, fitted)
        wr = scored.loc[scored.position.eq("WR")]
        if wr.empty:
            continue
        for alpha in attenuations:
            pred = wr.baseline_projection + alpha * (wr.full_shrinkage_projection - wr.baseline_projection)
            metrics = attenuation_base.score_predictions(pred, wr.actual_pts)
            rows.append({"validation_week": int(validation_week), "position": "WR", "attenuation": float(alpha), **metrics})
    cv = pd.DataFrame(rows)
    if cv.empty:
        raise ValueError("No rolling-origin development folds. Need more weeks or a lower --min-prior-weeks.")
    summaries = []
    for alpha, group in cv.groupby("attenuation", sort=True):
        valid = group.loc[group.n > 0]
        n = int(valid.n.sum())
        summaries.append({
            "attenuation": float(alpha), "n": n,
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
        rule = "lowest MAE among candidates satisfying pooled and fold bias constraints"
    else:
        ordered = out.sort_values(["max_fold_abs_bias", "abs_weighted_bias", "weighted_mae", "weighted_rmse", "attenuation"])
        rule = "no feasible candidate; minimum worst-fold absolute bias fallback"
    chosen = float(ordered.iloc[0].attenuation)
    out["selection_rule"] = rule
    out["selected"] = np.isclose(out.attenuation, chosen)
    return out.sort_values(["selected", "bias_feasible", "weighted_mae"], ascending=[False, False, True]).reset_index(drop=True), chosen


def score_predictions(pred: pd.Series, actual: pd.Series) -> dict:
    valid = pred.notna() & actual.notna()
    err = pred.loc[valid] - actual.loc[valid]
    if err.empty:
        return {"n": 0, "mae": np.nan, "rmse": np.nan, "bias": np.nan}
    return {"n": int(len(err)), "mae": float(err.abs().mean()),
            "rmse": float(np.sqrt(np.square(err).mean())), "bias": float(err.mean())}


def metrics_for(scored: pd.DataFrame, origin: int, holdout_end: int, alpha: float) -> list[dict]:
    model_cols = [
        ("baseline_projection", "baseline"),
        ("global_085_projection", "global_0.85"),
        ("full_shrinkage_projection", "full_position_bucket_shrinkage"),
        ("hybrid_projection", f"global_0.85_plus_WR_{alpha:.2f}"),
    ]
    out = []
    for position in ("ALL", *base.POSITIONS):
        group = scored if position == "ALL" else scored.loc[scored.position.eq(position)]
        for col, model in model_cols:
            out.append({"origin_week": origin, "holdout_end_week": holdout_end,
                        "wr_alpha": alpha, **base.metric_row(group, col, model, position)})
    return out


def week_comparisons(scored: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for week, g in scored.groupby("week", sort=True):
        valid = g.dropna(subset=["actual_pts", "hybrid_projection", "global_085_projection", "baseline_projection"])
        if valid.empty:
            continue
        row = {"week": int(week), "origin_week": int(valid.origin_week.iloc[0]), "wr_alpha": float(valid.wr_alpha.iloc[0]), "n": int(len(valid))}
        for label, col in (("hybrid", "hybrid_projection"), ("global085", "global_085_projection"), ("baseline", "baseline_projection")):
            err = valid[col] - valid.actual_pts
            row[f"{label}_mae"] = float(err.abs().mean())
            row[f"{label}_rmse"] = float(np.sqrt(np.square(err).mean()))
            row[f"{label}_bias"] = float(err.mean())
        row["hybrid_minus_global085_mae"] = row["hybrid_mae"] - row["global085_mae"]
        row["hybrid_minus_baseline_mae"] = row["hybrid_mae"] - row["baseline_mae"]
        rows.append(row)
    return pd.DataFrame(rows)


def cluster_bootstrap(weekly: pd.DataFrame, delta_col: str, reps: int, seed: int, comparison: str) -> dict:
    valid = weekly.dropna(subset=[delta_col, "n"])
    d = valid[delta_col].to_numpy(float); w = valid.n.to_numpy(float)
    if len(d) < 2:
        return {"comparison": comparison, "weeks": len(d), "bootstrap_reps": reps, "delta_mae": np.nan,
                "ci95_low": np.nan, "ci95_high": np.nan, "share_delta_below_zero": np.nan}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), size=(reps, len(d)))
    estimates = (d[idx] * w[idx]).sum(axis=1) / w[idx].sum(axis=1)
    return {"comparison": comparison, "weeks": int(len(d)), "bootstrap_reps": int(reps),
            "delta_mae": float(np.average(d, weights=w)), "ci95_low": float(np.quantile(estimates, .025)),
            "ci95_high": float(np.quantile(estimates, .975)),
            "share_delta_below_zero": float(np.mean(estimates < 0))}


def run(input_csv: Path, first_origin: int, window_weeks: int, step_weeks: int,
        min_prior_weeks: int, max_abs_bias: float, max_fold_abs_bias: float,
        attenuations: tuple[float, ...], bootstrap_reps: int, seed: int):
    df = load_frame(input_csv)
    available = sorted(df.week.unique().tolist())
    max_week = max(available)
    origins = list(range(first_origin, max_week - window_weeks + 2, step_weeks))
    if not origins:
        raise ValueError(f"No holdout origins fit the data: first_origin={first_origin}, window={window_weeks}, max_week={max_week}.")
    all_cv, all_selection, all_metrics, all_scored = [], [], [], []
    for origin in origins:
        end = origin + window_weeks - 1
        dev = df.loc[df.week < origin].copy()
        holdout = df.loc[df.week.between(origin, end)].copy()
        if dev.week.nunique() < min_prior_weeks + 1:
            raise ValueError(f"Origin {origin} has only {dev.week.nunique()} development weeks; increase --first-origin or lower --min-prior-weeks.")
        if holdout.empty or holdout.week.nunique() != window_weeks:
            raise ValueError(f"Origin {origin} expected {window_weeks} consecutive holdout weeks ({origin}-{end}); found {sorted(holdout.week.unique().tolist())}.")
        cv, summary = wr_cv(dev, attenuations, min_prior_weeks)
        selection, alpha = select_wr(summary, cv, max_abs_bias, max_fold_abs_bias)
        cv.insert(0, "origin_week", origin)
        selection.insert(0, "origin_week", origin)
        all_cv.append(cv); all_selection.append(selection)
        fitted = base.fit_hierarchy(dev)
        scored = attenuation_base.apply_fitted(holdout, fitted)
        scored["origin_week"] = origin
        scored["holdout_end_week"] = end
        scored["wr_alpha"] = alpha
        scored["global_085_projection"] = scored.position_mean_pts + GLOBAL_ALPHA * (scored.baseline_projection - scored.position_mean_pts)
        scored["hybrid_projection"] = scored["global_085_projection"]
        wr = scored.position.eq("WR")
        scored.loc[wr, "hybrid_projection"] = scored.loc[wr, "baseline_projection"] + alpha * (
            scored.loc[wr, "full_shrinkage_projection"] - scored.loc[wr, "baseline_projection"])
        scored["full_shrinkage_projection"] = scored["full_shrinkage_projection"]
        all_metrics.extend(metrics_for(scored, origin, end, alpha))
        all_scored.append(scored)
    cv_all = pd.concat(all_cv, ignore_index=True)
    selection_all = pd.concat(all_selection, ignore_index=True)
    metrics_all = pd.DataFrame(all_metrics)
    scored_all = pd.concat(all_scored, ignore_index=True)
    weekly = week_comparisons(scored_all)
    boot = pd.DataFrame([
        cluster_bootstrap(weekly, "hybrid_minus_global085_mae", bootstrap_reps, seed, "hybrid_vs_global_0.85"),
        cluster_bootstrap(weekly, "hybrid_minus_baseline_mae", bootstrap_reps, seed + 1, "hybrid_vs_baseline"),
    ])
    # Combined OOS metrics across every non-overlapping test window.
    combined_rows = []
    for position in ("ALL", *base.POSITIONS):
        group = scored_all if position == "ALL" else scored_all.loc[scored_all.position.eq(position)]
        for col, label in (("baseline_projection", "baseline"), ("global_085_projection", "global_0.85"),
                           ("full_shrinkage_projection", "full_position_bucket_shrinkage"), ("hybrid_projection", "rolling_selected_WR_hybrid")):
            combined_rows.append({"position": position, "model": label, **base.metric_row(group, col, label, position),
                                  "origins": ",".join(map(str, origins)), "oos_weeks": ",".join(map(str, sorted(scored_all.week.unique())))})
    combined = pd.DataFrame(combined_rows)
    return origins, cv_all, selection_all, metrics_all, weekly, scored_all, boot, combined


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input_csv", type=Path)
    p.add_argument("--first-origin", type=int, default=11, help="First OOS week; default 11 gives 5-10 development weeks on the supplied 2025 window.")
    p.add_argument("--window-weeks", type=int, default=2)
    p.add_argument("--step-weeks", type=int, default=2, help="Use equal to window-weeks for non-overlapping OOS windows.")
    p.add_argument("--min-prior-weeks", type=int, default=3)
    p.add_argument("--max-abs-bias", type=float, default=0.25)
    p.add_argument("--max-fold-abs-bias", type=float, default=0.50)
    p.add_argument("--attenuations", type=float, nargs="+", default=list(DEFAULT_ATTENUATIONS))
    p.add_argument("--bootstrap-reps", type=int, default=10000)
    p.add_argument("--seed", type=int, default=20261020)
    p.add_argument("--output-dir", type=Path, default=Path("outputs/shrinkage_wr_rolling_holdouts"))
    a = p.parse_args()
    if a.window_weeks < 1 or a.step_weeks < a.window_weeks:
        p.error("Require window-weeks >= 1 and step-weeks >= window-weeks to prevent overlapping OOS rows.")
    if a.min_prior_weeks < 1 or a.first_origin < 2:
        p.error("first-origin must be >= 2 and min-prior-weeks must be >= 1.")
    if a.bootstrap_reps < 1000:
        p.error("Use at least 1,000 bootstrap replicates.")
    if a.max_abs_bias < 0 or a.max_fold_abs_bias < 0:
        p.error("Bias limits must be non-negative.")
    if any(x < 0 or x > 1 for x in a.attenuations) or len(set(a.attenuations)) != len(a.attenuations):
        p.error("Attenuations must be unique values between 0 and 1.")
    results = run(a.input_csv, a.first_origin, a.window_weeks, a.step_weeks, a.min_prior_weeks,
                  a.max_abs_bias, a.max_fold_abs_bias, tuple(sorted(a.attenuations)), a.bootstrap_reps, a.seed)
    origins, cv, selection, metrics, weekly, scored, boot, combined = results
    out = a.output_dir; out.mkdir(parents=True, exist_ok=True)
    cv.to_csv(out / "rolling_development_wr_folds.csv", index=False)
    selection.to_csv(out / "rolling_development_wr_selection.csv", index=False)
    metrics.to_csv(out / "per_origin_holdout_metrics.csv", index=False)
    weekly.to_csv(out / "oos_weekly_comparisons.csv", index=False)
    scored.to_csv(out / "oos_scored_rows.csv", index=False)
    boot.to_csv(out / "oos_week_cluster_bootstrap.csv", index=False)
    combined.to_csv(out / "combined_oos_metrics.csv", index=False)
    print("ROLLING-ORIGIN WR SELECTION (development data only)")
    print(selection.loc[selection.selected, ["origin_week", "attenuation", "n", "weighted_mae", "weighted_rmse", "weighted_bias", "validation_weeks", "max_fold_abs_bias", "bias_feasible", "selection_rule"]].to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nPER-ORIGIN HOLDOUT METRICS")
    print(metrics.loc[metrics.position.eq("ALL")].to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nCOMBINED NON-OVERLAPPING OOS METRICS")
    print(combined.loc[combined.position.eq("ALL")].to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nWEEK-CLUSTER BOOTSTRAP (combined OOS weeks)")
    print(boot.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"\nOrigins: {origins}; combined OOS weeks: {sorted(scored.week.unique().tolist())}")
    print(f"Wrote seven reports to: {out.resolve()}")

if __name__ == "__main__":
    main()
