#!/usr/bin/env python3
"""
probe_rookie_data.py — one-time diagnostic for building a rookie
prediction model from draft capital + combine data via nflreadpy.
"""
import nflreadpy as nfl

print("=" * 60)
print("1. DRAFT PICKS — structure and 2025 class sample")
print("=" * 60)
picks = nfl.load_draft_picks().to_pandas()
print("columns:", picks.columns.tolist())
print()
print("season range:", picks["season"].min(), "-", picks["season"].max())
print()
recent = picks[picks["season"] == 2025]
print(f"2025 draft class: {len(recent)} picks")
print(recent[["season", "round", "pick", "position", "team", "pfr_player_name" if "pfr_player_name" in picks.columns else picks.columns[0]]].head(10) if len(recent) else "no 2025 rows found")

print()
print("=" * 60)
print("2. COMBINE — structure and sample")
print("=" * 60)
combine = nfl.load_combine().to_pandas()
print("columns:", combine.columns.tolist())
print()
print("season range:", combine["season"].min(), "-", combine["season"].max())
print(combine.head(5))

print()
print("=" * 60)
print("3. Can draft_picks be joined to gsis_id directly?")
print("=" * 60)
gsis_candidates = [c for c in picks.columns if "gsis" in c.lower() or "pfr" in c.lower() or "espn" in c.lower()]
print("ID-like columns in draft_picks:", gsis_candidates)

print()
print("=" * 60)
print("4. Historical validation — 2024 rookie class, do we have both")
print("   draft position AND their rookie-year fantasy output?")
print("=" * 60)
picks_2024 = picks[picks["season"] == 2024]
stats_2024 = nfl.load_player_stats([2024]).to_pandas()
if gsis_candidates:
    join_col = gsis_candidates[0]
    merged = picks_2024.merge(
        stats_2024.groupby("player_id")["fantasy_points_ppr"].sum().reset_index(),
        left_on=join_col, right_on="player_id", how="inner"
    )
    print(f"Matched {len(merged)} of {len(picks_2024)} 2024 draft picks to rookie-season stats")
    print(merged[[join_col, "round", "pick", "position", "fantasy_points_ppr"]].sort_values("pick").head(15))
else:
    print("No direct ID column found — would need pfr_id crosswalk via load_ff_playerids()")
    
    