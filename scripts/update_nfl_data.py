#!/usr/bin/env python3
"""
update_nfl_data.py — Gridiron Oracle Data Pipeline
====================================================
Pulls nflfastR data via nfl_data_py, overlays the official NFL injury report,
and outputs src/utils/nfl_data.js (the single source of truth for the sim engine).

Run schedule:
  - Every Tuesday  (after MNF final stats are processed)
  - Every Thursday morning (before TNF kickoff, captures Thu injury report)

Usage:
  python scripts/update_nfl_data.py              # current week, auto-detected
  python scripts/update_nfl_data.py --week 14    # specific week
  python scripts/update_nfl_data.py --season 2024 --week 14
  python scripts/update_nfl_data.py --dry-run    # build data but don't write file

Requirements:
  pip install nfl_data_py pandas requests python-dotenv
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import nfl_data_py as nfl
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

DEFAULT_SEASON = 2024

# play_probability mappings — per spec §7.3
INJURY_STATUS_MAP = {
    "Out":          0.0,
    "IR":           0.0,
    "PUP":          0.0,
    "Suspended":    0.0,
    "Doubtful":     0.25,
    "Questionable": 0.55,
    "GTD":          0.55,   # Game-Time Decision — same as Q
    "Limited":      0.75,
    "Full":         1.0,
    "Active":       1.0,
    "Probable":     0.92,   # rare but appears in some feeds
}

# Positions we track
SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "K"}

# Composite rating weights — per spec §3.2
# Position-specific weight overrides applied in compute_composite_rating()
DEFAULT_WEIGHTS = {
    "epa":       0.35,
    "usage":     0.30,
    "snap":      0.20,
    "red_zone":  0.15,
}

WR_TE_WEIGHTS = {**DEFAULT_WEIGHTS, "usage": 0.35, "epa": 0.30}   # target share more valuable in PPR
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
# Step 1 — Pull nflfastR play-by-play and weekly stats
# ---------------------------------------------------------------------------

def fetch_nflfastr_data(season: int, week: int) -> pd.DataFrame:
    """
    Pull weekly player stats from nfl_data_py for the given season
    through the given week. Returns one row per player with aggregated
    season stats and last-3-week averages.
    """
    log.info(f"Fetching nflfastR weekly stats — season={season}, through week={week}")

    # Weekly stats — includes targets, carries, snaps, EPA, red zone, air yards
    weekly = nfl.import_weekly_data(
        years=[season],
        columns=[
            "player_id",        # GSIS ID
            "player_name",
            "position",
            "recent_team",
            "week",
            "season",
            # Passing
            "completions", "attempts", "passing_yards", "passing_tds",
            "interceptions", "sack_yards", "pacr",
            "dakota",           # composite QB rating (CPOE + EPA)
            # Rushing
            "carries", "rushing_yards", "rushing_tds",
            "rushing_first_downs",
            # Receiving
            "receptions", "targets", "receiving_yards", "receiving_tds",
            "target_share",     # % of team targets
            "air_yards_share",  # % of team air yards — WR ceiling signal
            "wopr",             # weighted opportunity rating (target + air yards)
            # EPA
            "racr",             # receiver air conversion ratio
            "fantasy_points",   # standard scoring
            "fantasy_points_ppr",
            # Snap counts
            # snap participation %
            # Red zone
        ],
    )

    # Limit to skill positions and weeks up to the requested week
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
            snap_pct=("wopr", "mean"),
            season_targets=("targets", "sum"),
            season_carries=("carries", "sum"),
            season_games=("week", "count"),
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
			snap_pct=("wopr", "mean"),            
			last3_targets=("targets", "mean"),
            last3_carries=("carries", "mean"),
        )
        .reset_index()
        .rename(columns={"player_id": "gsis_id"})
    )

    # EPA per play — pull from play-by-play for accuracy
    epa = fetch_epa_per_play(season, week)

    # Target share, carry share, air yards, red zone (current week context)
    usage = fetch_usage_stats(weekly, week)

    # Merge all
    df = season_avg.merge(last3, on="gsis_id", how="left")
    df = df.merge(epa, on="gsis_id", how="left")
    df = df.merge(usage, on="gsis_id", how="left")

    log.info(f"nflfastR pull complete — {len(df)} players")
    return df


def fetch_epa_per_play(season: int, week: int) -> pd.DataFrame:
    """
    Calculate EPA per play for each player from play-by-play data.
    Opponent-adjusted where possible.
    """
    log.info("Calculating EPA per play from play-by-play...")

    try:
        pbp = nfl.import_pbp_data(
            years=[season],
            columns=[
                "passer_player_id", "rusher_player_id", "receiver_player_id",
                "epa", "week", "posteam", "defteam",
                "pass_attempt", "rush_attempt", "complete_pass",
            ],
            downcast=True,
        )
        pbp = pbp[pbp["week"] <= week]

        records = []

        # QB EPA per dropback
        qb_pbp = pbp[pbp["pass_attempt"] == 1].dropna(subset=["passer_player_id"])
        qb_epa = (
            qb_pbp.groupby("passer_player_id")["epa"]
            .mean()
            .reset_index()
            .rename(columns={"passer_player_id": "gsis_id", "epa": "epa_per_play"})
        )
        records.append(qb_epa)

        # Rusher EPA per carry
        rush_pbp = pbp[pbp["rush_attempt"] == 1].dropna(subset=["rusher_player_id"])
        rush_epa = (
            rush_pbp.groupby("rusher_player_id")["epa"]
            .mean()
            .reset_index()
            .rename(columns={"rusher_player_id": "gsis_id", "epa": "epa_per_play"})
        )
        records.append(rush_epa)

        # Receiver EPA per target
        rec_pbp = pbp[pbp["pass_attempt"] == 1].dropna(subset=["receiver_player_id"])
        rec_epa = (
            rec_pbp.groupby("receiver_player_id")["epa"]
            .mean()
            .reset_index()
            .rename(columns={"receiver_player_id": "gsis_id", "epa": "epa_per_play"})
        )
        records.append(rec_epa)

        epa_df = pd.concat(records).drop_duplicates(subset="gsis_id", keep="first")
        return epa_df

    except Exception as e:
        log.warning(f"EPA calculation failed ({e}) — filling with 0.0")
        return pd.DataFrame(columns=["gsis_id", "epa_per_play"])


def fetch_usage_stats(weekly: pd.DataFrame, week: int) -> pd.DataFrame:
    current_week = weekly[weekly["week"] == week].copy()
    if current_week.empty:
        current_week = weekly[weekly["week"] == weekly["week"].max()].copy()

    usage = (
        current_week.groupby("player_id")
        .agg(
            target_share=("target_share", "mean"),
            air_yards_share=("air_yards_share", "mean"),
            snap_pct=("wopr", "mean"),
        )
        .reset_index()
        .rename(columns={"player_id": "gsis_id"})
    )

    usage["red_zone_share"] = 0.0
    return usage


# ---------------------------------------------------------------------------
# Step 2 — Opponent DEF rank by position
# ---------------------------------------------------------------------------

def fetch_opponent_def_ranks(season: int, week: int) -> dict:
    """
    Calculate opponent DEF rank by position for the upcoming week.
    Returns dict: { team_abbr: { position: rank_1_to_32 } }
    """
    log.info("Calculating opponent DEF ranks by position...")

    try:
        # Use schedule to get this week's matchups
        schedule = nfl.import_schedules(years=[season])
        this_week = schedule[schedule["week"] == week][["home_team", "away_team"]].dropna()

        # Pull points allowed by position from weekly data
        weekly = nfl.import_weekly_data(
            years=[season],
            columns=["player_id", "position", "recent_team", "week",
                     "fantasy_points_ppr", "opponent_team"],
        )
        past = weekly[weekly["week"] < week].copy()

        # Average PPR points allowed by each team vs each position
        allowed = (
            past.groupby(["opponent_team", "position"])["fantasy_points_ppr"]
            .mean()
            .reset_index()
            .rename(columns={"opponent_team": "team"})
        )

        def rank_position(pos: str) -> dict:
            pos_df = allowed[allowed["position"] == pos].copy()
            # Rank 1 = hardest matchup (fewest pts allowed), 32 = easiest
            pos_df["rank"] = pos_df["fantasy_points_ppr"].rank(ascending=True).astype(int)
            return dict(zip(pos_df["team"], pos_df["rank"]))

        positions = ["QB", "RB", "WR", "TE"]
        ranks = {pos: rank_position(pos) for pos in positions}

        # Invert: team → {position: opponent_rank_this_week}
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

def fetch_injury_report() -> dict:
    """
    Pull the official NFL injury report.
    Primary: nfl_data_py injuries endpoint.
    Returns dict: { gsis_id: { play_probability, injury_detail } }
    """
    log.info("Fetching NFL injury report...")

    try:
        injuries = nfl.import_injuries(years=[DEFAULT_SEASON])
        result = {}

        for _, row in injuries.iterrows():
            gsis_id = row.get("gsis_id") or row.get("player_id")
            if not gsis_id:
                continue

            status = str(row.get("report_status", "Active")).strip()
            primary = str(row.get("primary_injury", "")).strip()
            practice = str(row.get("practice_status", "")).strip()

            play_prob = INJURY_STATUS_MAP.get(status, 1.0)

            detail = status
            if primary and primary != "nan":
                detail = f"{primary} — {status}"

            result[gsis_id] = {
                "play_probability": play_prob,
                "injury_detail": detail,
                "practice_status": practice if practice != "nan" else "",
            }

        log.info(f"Injury report loaded — {len(result)} players flagged")
        return result

    except Exception as e:
        log.warning(f"Injury report fetch failed ({e}) — all players set to Active")
        return {}


# ---------------------------------------------------------------------------
# Step 4 — Composite rating (normalized 0–100)
# ---------------------------------------------------------------------------

def normalize_series(s: pd.Series) -> pd.Series:
    """Min-max normalize a series to 0–100. NaN → 50 (neutral)."""
    s = s.fillna(s.median())
    min_val, max_val = s.min(), s.max()
    if max_val == min_val:
        return pd.Series([50.0] * len(s), index=s.index)
    return ((s - min_val) / (max_val - min_val) * 100).round(2)


def compute_composite_ratings(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute composite rating per spec §3.2.
    Position-specific weights applied.
    """
    log.info("Computing composite ratings...")

    df = df.copy()

    # Normalize component columns
    df["epa_score"]      = normalize_series(df["epa_per_play"].fillna(0))
    df["snap_score"]     = normalize_series(df["snap_pct"].fillna(0))
    df["red_zone_score"] = normalize_series(df["red_zone_share"].fillna(0))

    # Usage score — position-specific
    df["usage_score"] = 0.0
    for pos, usage_col in [("WR", "target_share"), ("TE", "target_share"), ("RB", "carry_share")]:
        mask = df["position"] == pos
        if usage_col in df.columns:
            df.loc[mask, "usage_score"] = normalize_series(
                df.loc[mask, usage_col].fillna(0)
            )

    # QB: use epa_per_play heavily — no target/carry share
    qb_mask = df["position"] == "QB"
    df.loc[qb_mask, "usage_score"] = df.loc[qb_mask, "epa_score"]

    # Apply weights per position
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
        weekly = nfl.import_weekly_data(
            years=[season],
            columns=["player_id", "position", "recent_team", "week", "carries"],
        )
        curr = weekly[weekly["week"] == week][weekly["position"] == "RB"].copy()
        if curr.empty:
            curr = weekly[weekly["week"] == weekly["week"].max()][weekly["position"] == "RB"].copy()

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

def assemble_player_records(
    df: pd.DataFrame,
    injury_map: dict,
    def_ranks: dict,
) -> list[dict]:
    """
    Merge all data into the locked nfl_data.js schema per spec §3.1.
    """
    records = []

    for _, row in df.iterrows():
        gsis_id = str(row.get("gsis_id", "")).strip()
        if not gsis_id:
            continue

        position = str(row.get("position", "")).upper()
        team     = str(row.get("team", "")).upper()

        # Injury overlay
        inj = injury_map.get(gsis_id, {})
        play_probability = inj.get("play_probability", 1.0)
        injury_detail    = inj.get("injury_detail", "Active")

        # Opponent DEF rank — position-specific for this week's matchup
        team_ranks = def_ranks.get(team, {})
        opp_def_rank = team_ranks.get(position, 16)   # 16 = neutral if unknown

        record = {
            # --- Identity ---
            "gsis_id":          gsis_id,
            "name":             str(row.get("name", "Unknown")).strip(),
            "position":         position,
            "team":             team,

            # --- Efficiency ---
            "epa_per_play":     round(float(row.get("epa_per_play", 0.0) or 0.0), 4),

            # --- Usage ---
            "target_share":     round(float(row.get("target_share", 0.0) or 0.0), 3),
            "carry_share":      round(float(row.get("carry_share", 0.0) or 0.0), 3),
            "snap_pct":         round(float(row.get("snap_pct", 0.0) or 0.0), 3),
            "red_zone_share":   round(float(row.get("red_zone_share", 0.0) or 0.0), 3),
            "air_yards_share":  round(float(row.get("air_yards_share", 0.0) or 0.0), 3),

            # --- Matchup ---
            "opp_def_rank":     int(opp_def_rank),

            # --- Injury ---
            "play_probability": round(float(play_probability), 2),
            "injury_detail":    injury_detail,

            # --- Scoring history ---
            "season_avg_pts":   round(float(row.get("season_avg_pts", 0.0) or 0.0), 2),
            "last3_avg_pts":    round(float(row.get("last3_avg_pts", 0.0) or 0.0), 2),

            # --- Computed ---
            "composite_rating": round(float(row.get("composite_rating", 50.0) or 50.0), 2),
            "epa_score":        round(float(row.get("epa_score", 50.0) or 50.0), 2),
            "usage_score":      round(float(row.get("usage_score", 50.0) or 50.0), 2),
            "snap_score":       round(float(row.get("snap_score", 50.0) or 50.0), 2),
            "red_zone_score":   round(float(row.get("red_zone_score", 50.0) or 50.0), 2),

            # --- Metadata ---
            "conf":             "",   # NFL team conference — populated if needed
        }

        records.append(record)

    # Sort by composite_rating descending for readability
    records.sort(key=lambda r: r["composite_rating"], reverse=True)
    return records


# ---------------------------------------------------------------------------
# Step 7 — Write output file
# ---------------------------------------------------------------------------

def write_output(records: list[dict], week: int, season: int, dry_run: bool):
    """
    Write records to src/utils/nfl_data.js in the locked schema format.
    File is consumed directly by simulator.js (no bundler transform needed).
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    js_content = f"""// nfl_data.js — Gridiron Oracle player data
// AUTO-GENERATED by scripts/update_nfl_data.py — DO NOT EDIT MANUALLY
// Season: {season} | Week: {week} | Generated: {timestamp}
// Players: {len(records)}
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

// Lookup by GSIS ID — O(1) access for simulator.js
export const PLAYER_BY_GSIS_ID = Object.fromEntries(
  NFL_PLAYERS.map(p => [p.gsis_id, p])
);

// Lookup by position — used for scarcity model
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
# Validation gate — must pass before file is written
# ---------------------------------------------------------------------------

def validate_records(records: list[dict]) -> bool:
    """
    Pre-write validation. Mirrors validate_data.py checks.
    Returns True if all pass; logs errors and returns False otherwise.
    """
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
        for e in errors[:20]:   # cap output for large error sets
            log.error(f"  ✗ {e}")
        return False

    log.info(f"✓ Validation passed — {len(records)} records, {len(seen_ids)} unique GSIS IDs")
    return True


# ---------------------------------------------------------------------------
# Week detection
# ---------------------------------------------------------------------------

def detect_current_week(season: int) -> int:
    """
    Detect current NFL week from schedule data.
    Falls back to week 1 if detection fails.
    """
    try:
        schedule = nfl.import_schedules(years=[season])
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
    parser.add_argument("--week",   type=int, default=None,
                        help="NFL week number (auto-detected if omitted)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build data but don't write output file")
    args = parser.parse_args()

    season = args.season
    week   = args.week or detect_current_week(season)

    log.info(f"=== Gridiron Oracle data pipeline — season={season}, week={week} ===")

    # 1. Pull nflfastR stats
    df = fetch_nflfastr_data(season, week)

    # 2. Add carry share for RBs
    df = add_carry_share(df, season, week)

    # 3. Opponent DEF ranks for this week's matchups
    def_ranks = fetch_opponent_def_ranks(season, week)

    # 4. Injury overlay
    injury_map = fetch_injury_report()

    # 5. Compute composite ratings
    df = compute_composite_ratings(df)

    # 6. Assemble final records
    records = assemble_player_records(df, injury_map, def_ranks)

    # 7. Validate before writing
    if not validate_records(records):
        log.error("Aborting — fix validation errors before deploying")
        sys.exit(1)

    # 8. Write output
    write_output(records, week, season, dry_run=args.dry_run)

    log.info("=== Pipeline complete ===")


if __name__ == "__main__":
    main()
