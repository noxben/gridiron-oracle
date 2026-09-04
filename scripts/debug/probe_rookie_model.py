#!/usr/bin/env python3
"""
probe_rookie_model.py — check sample sizes for a draft-slot-based
rookie fantasy projection model before building it for real.
"""
import nflreadpy as nfl
import pandas as pd

picks = nfl.load_draft_picks().to_pandas()
picks = picks[picks["season"] >= 2015]  # last decade, recent enough to reflect modern usage patterns
picks = picks[picks["position"].isin(["QB", "RB", "WR", "TE"])]

all_rookie_pts = []
for yr in range(2015, 2025):
    yr_picks = picks[picks["season"] == yr][["gsis_id", "round", "pick", "position"]]
    stats = nfl.load_player_stats([yr]).to_pandas()
    rookie_pts = stats.groupby("player_id")["fantasy_points_ppr"].sum().reset_index()
    rookie_pts.columns = ["gsis_id", "rookie_season_pts"]
    merged = yr_picks.merge(rookie_pts, on="gsis_id", how="left")
    merged["rookie_season_pts"] = merged["rookie_season_pts"].fillna(0.0)
    merged["draft_season"] = yr
    all_rookie_pts.append(merged)

full = pd.concat(all_rookie_pts)
print(f"Total rookie-season records (2015-2024, QB/RB/WR/TE): {len(full)}")
print()

# Bucket by round, show sample size + average rookie fantasy output per position
for pos in ["QB", "RB", "WR", "TE"]:
    print(f"=== {pos} ===")
    pos_df = full[full["position"] == pos]
    summary = pos_df.groupby("round")["rookie_season_pts"].agg(["count", "mean", "median"]).round(1)
    print(summary)
    print()