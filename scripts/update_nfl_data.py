#!/usr/bin/env python3
"""
update_nfl_data.py — Gridiron Oracle Data Pipeline
====================================================
Pulls player stats via nflreadpy, overlays the official NFL injury report,
and outputs src/utils/nfl_data.js (the single source of truth for the sim engine).

MIGRATION NOTE (2026-08): This pipeline previously used nfl_data_py, which was
archived by its maintainer in September 2025 and can no longer fetch data for
seasons after 2024 — its internal year-handling was never updated past that
point. Rewritten to use nflreadpy, the actively-maintained official nflverse
Python package. nflreadpy returns Polars DataFrames; every call below appends
.to_pandas() since the rest of this pipeline is written in pandas.

Run schedule:
  - Every Tuesday  (after MNF final stats are processed)
  - Every Thursday morning (before TNF kickoff, captures Thu injury report)

Usage:
  python scripts/update_nfl_data.py              # current week, auto-detected
  python scripts/update_nfl_data.py --week 14    # specific week
  python scripts/update_nfl_data.py --season 2025 --week 14
  python scripts/update_nfl_data.py --dry-run    # build data but don't write file

Requirements:
  pip install nflreadpy pandas pyarrow requests python-dotenv
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import nflreadpy as nfl
import pandas as pd
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "src" / "utils" / "nfl_data.js"
LOG_PATH = ROOT / "scripts" / "logs" / "update_nfl_data.log"

DEFAULT_SEASON = 2025
CURRENT_ROSTER_SEASON = 2026  # separate from DEFAULT_SEASON (stats season) —
                               # roster status should reflect the current
                               # actual season, not the historical stats year

INJURY_STATUS_MAP = {
    "Out":          0.0,
    "IR":           0.0,
    "PUP":          0.0,
    "Suspended":    0.0,
    "Doubtful":     0.25,
    "Questionable": 0.55,
    "GTD":          0.55,
    "Limited":      0.75,
    "Full":         1.0,
    "Active":       1.0,
    "Probable":     0.92,
}

SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "K"}

DEFAULT_WEIGHTS = {
    "epa":       0.35,
    "usage":     0.30,
    "snap":      0.20,
    "red_zone":  0.15,
}
WR_TE_WEIGHTS = {**DEFAULT_WEIGHTS, "usage": 0.35, "epa": 0.30}
RB_WEIGHTS    = {**DEFAULT_WEIGHTS, "usage": 0.30, "red_zone": 0.20, "snap": 0.15}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ESPN <-> GSIS crosswalk — now via nflreadpy's fantasy-focused ID table
# ---------------------------------------------------------------------------
def fetch_id_crosswalk() -> pd.DataFrame:
    """
    Pull the ESPN <-> GSIS <-> PFR ID crosswalk via nflreadpy.
    Replaces nfl_data_py's import_ids() (dead package).
    """
    log.info("Fetching ID crosswalk (espn_id / gsis_id / pfr_id)...")
    ids = nfl.load_ff_playerids().to_pandas()
    cols = ["gsis_id", "espn_id", "pfr_id", "name", "position", "team"]
    df = ids[cols].copy()
    df = df.dropna(subset=["gsis_id"])
    df["gsis_id"] = df["gsis_id"].astype(str).str.strip()
    return df

# ---------------------------------------------------------------------------
# Step 1 — Pull player weekly stats
# ---------------------------------------------------------------------------
def fetch_nflfastr_data(season: int, week: int) -> pd.DataFrame:
    """
    Pull weekly player stats via nflreadpy for the given season through
    the given week. Returns one row per player with aggregated season
    stats and last-3-week averages.
    """
    log.info(f"Fetching weekly player stats (nflreadpy) — season={season}, through week={week}")

    weekly = nfl.load_player_stats([season]).to_pandas()

    # nflreadpy column names confirmed 2026-08 — differ slightly from the
    # old nfl_data_py naming in a couple of spots (noted where relevant).
    weekly = weekly.rename(columns={"team": "recent_team"})

    weekly = weekly[
        (weekly["position"].isin(SKILL_POSITIONS)) &
        (weekly["week"] <= week)
    ].copy()

    if weekly.empty:
        log.error("No weekly data returned — check season/week parameters")
        sys.exit(1)

    # Season averages (all weeks)
    season_avg = (
        weekly.groupby("player_id")
        .agg(
            name=("player_name", "last"),
            position=("position", "last"),
            team=("recent_team", "last"),
            season_avg_pts=("fantasy_points_ppr", "mean"),
            season_targets=("targets", "sum"),
            season_carries=("carries", "sum"),
            season_games=("week", "count"),
            season_rush_yards=("rushing_yards", "sum"),
            season_rush_tds=("rushing_tds", "sum"),
            season_rec_tds=("receiving_tds", "sum"),
            season_pass_tds=("passing_tds", "sum"),
        )
        .reset_index()
        .rename(columns={"player_id": "gsis_id"})
    )

    # Last-3-week averages — recency weight
    last3 = (
        weekly[weekly["week"] >= max(1, week - 2)]
        .groupby("player_id")
        .agg(
            last3_avg_pts=("fantasy_points_ppr", "mean"),
            last3_targets=("targets", "mean"),
            last3_carries=("carries", "mean"),
        )
        .reset_index()
        .rename(columns={"player_id": "gsis_id"})
    )

    epa = fetch_epa_per_play(season, week)
    usage = fetch_usage_stats(weekly, week)
    snap_data = fetch_snap_counts(season, week)
    rz_data = fetch_red_zone_shares(season, week)

    df = season_avg.merge(last3, on="gsis_id", how="left")
    df = df.merge(epa, on="gsis_id", how="left")
    df = df.merge(usage, on="gsis_id", how="left")
    df = df.merge(snap_data, on="gsis_id", how="left")
    df = df.merge(rz_data, on="gsis_id", how="left")

    log.info(f"Weekly stats pull complete — {len(df)} players")
    return df


def fetch_epa_per_play(season: int, week: int) -> pd.DataFrame:
    """
    Calculate EPA per play for each player from play-by-play data.
    NOTE: nflreadpy's pbp uses passer_id / rusher_id / receiver_id —
    no "_player" in the middle, unlike the old nfl_data_py naming.
    """
    log.info("Calculating EPA per play from play-by-play (nflreadpy)...")
    try:
        pbp = nfl.load_pbp([season]).to_pandas()
        pbp = pbp[pbp["week"] <= week]

        records = []

        qb_pbp = pbp[pbp["pass_attempt"] == 1].dropna(subset=["passer_id"])
        qb_epa = (
            qb_pbp.groupby("passer_id")["epa"]
            .mean()
            .reset_index()
            .rename(columns={"passer_id": "gsis_id", "epa": "epa_per_play"})
        )
        records.append(qb_epa)

        rush_pbp = pbp[pbp["rush_attempt"] == 1].dropna(subset=["rusher_id"])
        rush_epa = (
            rush_pbp.groupby("rusher_id")["epa"]
            .mean()
            .reset_index()
            .rename(columns={"rusher_id": "gsis_id", "epa": "epa_per_play"})
        )
        records.append(rush_epa)

        rec_pbp = pbp[pbp["pass_attempt"] == 1].dropna(subset=["receiver_id"])
        rec_epa = (
            rec_pbp.groupby("receiver_id")["epa"]
            .mean()
            .reset_index()
            .rename(columns={"receiver_id": "gsis_id", "epa": "epa_per_play"})
        )
        records.append(rec_epa)

        epa_df = pd.concat(records).drop_duplicates(subset="gsis_id", keep="first")
        return epa_df
    except Exception as e:
        log.warning(f"EPA calculation failed ({e}) — filling with 0.0")
        return pd.DataFrame(columns=["gsis_id", "epa_per_play"])


def fetch_red_zone_shares(season: int, week: int) -> pd.DataFrame:
    """
    Compute red_zone_share: each player's share of their team's red-zone
    touches (rush attempts + targets inside the 20).
    """
    log.info("Calculating red zone share from play-by-play (nflreadpy)...")
    try:
        pbp = nfl.load_pbp([season]).to_pandas()
        pbp = pbp[(pbp["week"] <= week) & (pbp["yardline_100"] <= 20)]

        rush_touches = pbp[pbp["rush_attempt"] == 1].dropna(subset=["rusher_id"])
        rush_touches = rush_touches.rename(columns={"rusher_id": "gsis_id"})

        rec_touches = pbp[pbp["pass_attempt"] == 1].dropna(subset=["receiver_id"])
        rec_touches = rec_touches.rename(columns={"receiver_id": "gsis_id"})

        touches = pd.concat([
            rush_touches[["gsis_id", "posteam"]],
            rec_touches[["gsis_id", "posteam"]],
        ])

        player_rz = touches.groupby("gsis_id").size().rename("player_rz_touches")
        team_rz = touches.groupby("posteam").size().rename("team_rz_touches")
        player_team = touches.groupby("gsis_id")["posteam"].first()

        result = player_rz.to_frame().join(player_team)
        result = result.join(team_rz, on="posteam")
        result["red_zone_share"] = (
            result["player_rz_touches"] / result["team_rz_touches"].clip(lower=1)
        ).round(3)

        return result.reset_index()[["gsis_id", "red_zone_share"]]
    except Exception as e:
        log.warning(f"Red zone share calculation failed ({e}) — defaulting to 0.0")
        return pd.DataFrame(columns=["gsis_id", "red_zone_share"])


def fetch_snap_counts(season: int, week: int) -> pd.DataFrame:
    """
    Pull real offensive snap participation % via nflreadpy.
    Snap counts are keyed by pfr_player_id — requires the crosswalk.
    """
    log.info(f"Fetching real snap counts (nflreadpy) — season={season}, through week={week}")
    try:
        snaps = nfl.load_snap_counts([season]).to_pandas()
        snaps = snaps[snaps["week"] <= week].copy()

        crosswalk = fetch_id_crosswalk()
        crosswalk = crosswalk.dropna(subset=["pfr_id"]).drop_duplicates(subset="pfr_id")
        crosswalk_map = dict(zip(crosswalk["pfr_id"], crosswalk["gsis_id"]))

        pfr_col = "pfr_player_id" if "pfr_player_id" in snaps.columns else "pfr_id"
        snaps["gsis_id"] = snaps[pfr_col].map(crosswalk_map)
        snaps = snaps.dropna(subset=["gsis_id"])

        snap_pct = (
            snaps.groupby("gsis_id")["offense_pct"]
            .mean()
            .reset_index()
            .rename(columns={"offense_pct": "snap_pct"})
        )
        log.info(f"Snap counts matched — {len(snap_pct)} players")
        return snap_pct
    except Exception as e:
        log.warning(f"Snap count fetch failed ({e}) — snap_pct will be NaN")
        return pd.DataFrame(columns=["gsis_id", "snap_pct"])


def fetch_usage_stats(weekly: pd.DataFrame, week: int) -> pd.DataFrame:
    current_week = weekly[weekly["week"] == week].copy()
    if current_week.empty:
        current_week = weekly[weekly["week"] == weekly["week"].max()].copy()
    usage = (
        current_week.groupby("player_id")
        .agg(
            target_share=("target_share", "mean"),
            air_yards_share=("air_yards_share", "mean"),
        )
        .reset_index()
        .rename(columns={"player_id": "gsis_id"})
    )
    return usage

# ---------------------------------------------------------------------------
# Step 2 — Opponent DEF rank by position
# ---------------------------------------------------------------------------
def fetch_opponent_def_ranks(season: int, week: int) -> dict:
    log.info("Calculating opponent DEF ranks by position (nflreadpy)...")
    try:
        schedule = nfl.load_schedules([season]).to_pandas()
        this_week = schedule[schedule["week"] == week][["home_team", "away_team"]].dropna()

        weekly = nfl.load_player_stats([season]).to_pandas()
        weekly = weekly.rename(columns={"team": "recent_team"})
        past = weekly[weekly["week"] < week].copy()

        allowed = (
            past.groupby(["opponent_team", "position"])["fantasy_points_ppr"]
            .mean()
            .reset_index()
            .rename(columns={"opponent_team": "team"})
        )

        def rank_position(pos: str) -> dict:
            pos_df = allowed[allowed["position"] == pos].copy()
            pos_df["rank"] = pos_df["fantasy_points_ppr"].rank(ascending=True).astype(int)
            return dict(zip(pos_df["team"], pos_df["rank"]))

        positions = ["QB", "RB", "WR", "TE"]
        ranks = {pos: rank_position(pos) for pos in positions}

        matchup_ranks: dict = {}
        for _, row in this_week.iterrows():
            matchup_ranks[row["home_team"]] = {
                pos: ranks[pos].get(row["away_team"], 16) for pos in positions
            }
            matchup_ranks[row["away_team"]] = {
                pos: ranks[pos].get(row["home_team"], 16) for pos in positions
            }
        return matchup_ranks
    except Exception as e:
        log.warning(f"DEF rank calculation failed ({e}) — using neutral rank 16")
        return {}

# ---------------------------------------------------------------------------
# Step 3 — Injury overlay
# ---------------------------------------------------------------------------
def fetch_injury_report(season: int) -> dict:
    """
    Returns { gsis_id: { play_probability, injury_detail } }

    MIGRATION NOTE (2026-08): switched from nfl_data_py.import_injuries()
    (dead package, archived Sept 2025) to nflreadpy.load_injuries().
    """
    log.info("Fetching latest injury report (nflreadpy)...")
    try:
        injuries = nfl.load_injuries([season]).to_pandas()
        result = {}
        for _, row in injuries.iterrows():
            gsis_id = row.get("gsis_id") or row.get("player_id")
            if not gsis_id:
                continue
            status  = str(row.get("report_status") or "Active").strip()
            if status.lower() == "nan":
                status = "Active"
            primary = str(row.get("primary_injury") or "").strip()
            if primary.lower() == "nan":
                primary = ""
            play_prob = INJURY_STATUS_MAP.get(status, 1.0)
            detail = f"{primary} — {status}" if primary else status
            result[str(gsis_id)] = {
                "play_probability": play_prob,
                "injury_detail":    detail,
            }
        log.info(f"Injury report: {len(result)} players flagged")
        return result
    except Exception as e:
        log.error(f"Injury fetch failed: {e}")
        sys.exit(1)

def fetch_current_roster_status(season: int) -> dict:
    """
    Pull current roster status (ACT/RES/E14/RET/CUT) via nflreadpy.
    This is separate from fetch_injury_report — that's the weekly in-season
    practice-report system (unavailable pre-season, since load_injuries()
    only covers seasons nflreadpy considers "current," which lags behind
    the actual calendar year until Week 1 kicks off). Roster status is
    available year-round and reflects whether a player is actually on an
    active roster right now, which matters for pre-draft accuracy.

    RET (retired) and CUT players are excluded entirely at the call site
    (see assemble_player_records) — no draft-relevant purpose in keeping
    them visible if they can't actually be drafted.

    Returns { gsis_id: status_string }
    """
    log.info(f"Fetching current roster status (nflreadpy) — season={season}...")
    try:
        rosters = nfl.load_rosters([season]).to_pandas()
        rosters = rosters.dropna(subset=["gsis_id"])
        # Keep most recent entry per player if duplicates exist
        rosters = rosters.drop_duplicates(subset="gsis_id", keep="last")
        status_map = dict(zip(rosters["gsis_id"].astype(str), rosters["status"]))
        log.info(f"Roster status loaded — {len(status_map)} players")
        return status_map
    except Exception as e:
        log.warning(f"Roster status fetch failed ({e}) — all players treated as ACT")
        return {}

# ---------------------------------------------------------------------------
# Step 4 — Composite rating (normalized 0–100)
# ---------------------------------------------------------------------------
def normalize_series(s: pd.Series) -> pd.Series:
    """Min-max normalize a series to 0–100. NaN -> 50 (neutral)."""
    s = s.fillna(s.median())
    min_val, max_val = s.min(), s.max()
    if max_val == min_val:
        return pd.Series([50.0] * len(s), index=s.index)
    return ((s - min_val) / (max_val - min_val) * 100).round(2)


def compute_composite_ratings(df: pd.DataFrame) -> pd.DataFrame:
    log.info("Computing composite ratings...")
    df = df.copy()

    df["epa_score"]      = normalize_series(df["epa_per_play"].fillna(0))
    df["snap_score"]     = normalize_series(df["snap_pct"].fillna(0))
    df["red_zone_score"] = normalize_series(df["red_zone_share"].fillna(0))

    df["usage_score"] = 0.0
    for pos, usage_col in [("WR", "target_share"), ("TE", "target_share"), ("RB", "carry_share")]:
        mask = df["position"] == pos
        if usage_col in df.columns:
            df.loc[mask, "usage_score"] = normalize_series(
                df.loc[mask, usage_col].fillna(0)
            )

    qb_mask = df["position"] == "QB"
    df.loc[qb_mask, "usage_score"] = df.loc[qb_mask, "epa_score"]

    def weighted_rating(row):
        pos = row.get("position", "")
        if pos in ("WR", "TE"):
            w = WR_TE_WEIGHTS
        elif pos == "RB":
            w = RB_WEIGHTS
        else:
            w = DEFAULT_WEIGHTS
        return round(
            w["epa"]      * row["epa_score"] +
            w["usage"]    * row["usage_score"] +
            w["snap"]     * row["snap_score"] +
            w["red_zone"] * row["red_zone_score"],
            2,
        )

    df["composite_rating"] = df.apply(weighted_rating, axis=1)
    return df

# ---------------------------------------------------------------------------
# Step 5 — Carry share (RB-specific)
# ---------------------------------------------------------------------------
def add_carry_share(df: pd.DataFrame, season: int, week: int) -> pd.DataFrame:
    """Compute carry_share (% of team carries) for RBs."""
    try:
        weekly = nfl.load_player_stats([season]).to_pandas()
        weekly = weekly.rename(columns={"team": "recent_team"})
        weekly = weekly[["player_id", "position", "recent_team", "week", "carries"]]

        curr = weekly[(weekly["week"] == week) & (weekly["position"] == "RB")].copy()
        if curr.empty:
            curr = weekly[(weekly["week"] == weekly["week"].max()) & (weekly["position"] == "RB")].copy()

        team_carries = curr.groupby("recent_team")["carries"].sum().rename("team_carries")
        curr = curr.join(team_carries, on="recent_team")
        curr["carry_share"] = (curr["carries"] / curr["team_carries"].clip(lower=1)).round(3)

        carry_map = dict(zip(curr["player_id"], curr["carry_share"]))
        df["carry_share"] = df["gsis_id"].map(carry_map).fillna(0.0)
    except Exception as e:
        log.warning(f"Carry share calculation failed ({e})")
        df["carry_share"] = 0.0
    return df

# ---------------------------------------------------------------------------
# Step 6 — Assemble final player records
# ---------------------------------------------------------------------------
def assemble_player_records(df: pd.DataFrame, injury_map: dict, def_ranks: dict, roster_status_map: dict) -> list[dict]:
    records = []
    for _, row in df.iterrows():
        gsis_id = str(row.get("gsis_id", "")).strip()
        if not gsis_id:
            continue

        position = str(row.get("position", "")).upper()
        team     = str(row.get("team", "")).upper()

        inj = injury_map.get(gsis_id, {})
        play_probability = inj.get("play_probability", 1.0)
        injury_detail    = inj.get("injury_detail", "Active")

        team_ranks = def_ranks.get(team, {})
        opp_def_rank = team_ranks.get(position, 16)
        
        roster_status = roster_status_map.get(gsis_id, "ACT")
        if roster_status in ("RET", "CUT"):
            continue  # retired/cut players have no draft relevance

        record = {
            "gsis_id":          gsis_id,
            "name":             str(row.get("name", "Unknown")).strip(),
            "position":         position,
            "team":             team,
            "roster_status":    roster_status,   # ← ADD THIS LINE
            "epa_per_play":     round(float(row.get("epa_per_play", 0.0) or 0.0), 4),
            "target_share":     round(float(row.get("target_share", 0.0) or 0.0), 3),
            "carry_share":      round(float(row.get("carry_share", 0.0) or 0.0), 3),
            "snap_pct":         round(float(row.get("snap_pct", 0.0) or 0.0), 3),
            "red_zone_share":   round(float(row.get("red_zone_share", 0.0) or 0.0), 3),
            "air_yards_share":  round(float(row.get("air_yards_share", 0.0) or 0.0), 3),
            "opp_def_rank":     int(opp_def_rank),
            "play_probability": round(float(play_probability), 2),
            "injury_detail":    injury_detail,
            "season_avg_pts":   round(float(row.get("season_avg_pts", 0.0) or 0.0), 2),
            "last3_avg_pts":    round(float(row.get("last3_avg_pts", 0.0) or 0.0), 2),
            "season_rush_yards": round(float(row.get("season_rush_yards", 0.0) or 0.0), 1),
            "season_rush_tds":   int(row.get("season_rush_tds", 0) or 0),
            "season_rec_tds":    int(row.get("season_rec_tds", 0) or 0),
            "season_pass_tds":   int(row.get("season_pass_tds", 0) or 0),
            "composite_rating": round(float(row.get("composite_rating", 50.0) or 50.0), 2),
            "epa_score":        round(float(row.get("epa_score", 50.0) or 50.0), 2),
            "usage_score":      round(float(row.get("usage_score", 50.0) or 50.0), 2),
            "snap_score":       round(float(row.get("snap_score", 50.0) or 50.0), 2),
            "red_zone_score":   round(float(row.get("red_zone_score", 50.0) or 50.0), 2),
            "conf":             "",
        }
        records.append(record)

    records.sort(key=lambda r: r["composite_rating"], reverse=True)
    return records

# ---------------------------------------------------------------------------
# Step 7 — Write output file
# ---------------------------------------------------------------------------
def write_output(records: list[dict], week: int, season: int, dry_run: bool):
    timestamp = datetime.now(timezone.utc).isoformat()
    js_content = f"""// nfl_data.js — Gridiron Oracle player data
// AUTO-GENERATED by scripts/update_nfl_data.py — DO NOT EDIT MANUALLY
// Season: {season} | Week: {week} | Generated: {timestamp}
// Players: {len(records)}
// Data source: nflreadpy (migrated from archived nfl_data_py, 2026-08)
//
// Schema locked per spec §3.1. Primary key: gsis_id (never name string).
// Run validate_data.py before every deploy.
export const NFL_DATA_META = {{
  season: {season},
  week: {week},
  generated_at: "{timestamp}",
  player_count: {len(records)},
}};
export const NFL_PLAYERS = {json.dumps(records, indent=2)};
export const PLAYER_BY_GSIS_ID = Object.fromEntries(
  NFL_PLAYERS.map(p => [p.gsis_id, p])
);
export const PLAYERS_BY_POSITION = NFL_PLAYERS.reduce((acc, p) => {{
  if (!acc[p.position]) acc[p.position] = [];
  acc[p.position].push(p);
  return acc;
}}, {{}});
"""
    if dry_run:
        log.info(f"DRY RUN — would write {len(records)} players to {OUTPUT_PATH}")
        log.info(f"Sample record:\n{json.dumps(records[0] if records else {{}}, indent=2)}")
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(js_content, encoding="utf-8")
    log.info(f"✓ Wrote {len(records)} players to {OUTPUT_PATH}")

# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------
def validate_records(records: list[dict]) -> bool:
    errors = []
    seen_ids = set()
    for r in records:
        gsis = r.get("gsis_id", "")
        if not gsis:
            errors.append(f"Missing gsis_id: {r.get('name')}")
        if gsis in seen_ids:
            errors.append(f"Duplicate gsis_id: {gsis} ({r.get('name')})")
        seen_ids.add(gsis)
        if not (0.0 <= r.get("play_probability", -1) <= 1.0):
            errors.append(f"{gsis}: play_probability out of range: {r.get('play_probability')}")
        if not (1 <= r.get("opp_def_rank", 0) <= 32):
            errors.append(f"{gsis}: opp_def_rank out of range: {r.get('opp_def_rank')}")

    if errors:
        log.error(f"Validation FAILED — {len(errors)} error(s):")
        for e in errors[:20]:
            log.error(f"  ✗ {e}")
        return False

    log.info(f"✓ Validation passed — {len(records)} records, {len(seen_ids)} unique GSIS IDs")
    return True

# ---------------------------------------------------------------------------
# Week detection
# ---------------------------------------------------------------------------
def detect_current_week(season: int) -> int:
    try:
        schedule = nfl.load_schedules([season]).to_pandas()
        today = datetime.now(timezone.utc).date()
        schedule["game_date"] = pd.to_datetime(schedule["gameday"]).dt.date
        past_games = schedule[schedule["game_date"] <= today]
        if past_games.empty:
            return 1
        return int(past_games["week"].max())
    except Exception as e:
        log.warning(f"Week detection failed ({e}) — defaulting to week 1")
        return 1

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Gridiron Oracle — NFL data pipeline")
    parser.add_argument("--season", type=int, default=DEFAULT_SEASON)
    parser.add_argument("--week",   type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    season = args.season
    week   = args.week or detect_current_week(season)

    log.info(f"=== Gridiron Oracle data pipeline — season={season}, week={week} ===")

    df = fetch_nflfastr_data(season, week)
    df = add_carry_share(df, season, week)
    def_ranks = fetch_opponent_def_ranks(season, week)
    injury_map = fetch_injury_report(season)
    roster_status_map = fetch_current_roster_status(CURRENT_ROSTER_SEASON)
    df = compute_composite_ratings(df)
    records = assemble_player_records(df, injury_map, def_ranks, roster_status_map)

    if not validate_records(records):
        log.error("Aborting — fix validation errors before deploying")
        sys.exit(1)

    write_output(records, week, season, dry_run=args.dry_run)
    log.info("=== Pipeline complete ===")


if __name__ == "__main__":
    main()