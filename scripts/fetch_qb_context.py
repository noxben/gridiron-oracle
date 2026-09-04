#!/usr/bin/env python3
"""
fetch_qb_context.py — Gridiron Oracle QB Change Detector
=========================================================
For every WR/TE/RB currently present in nfl_data.js:

1. Determine who threw that player the most targets in 2025.
2. Determine the player's CURRENT 2026 team from nfl_data.js.
3. Determine that team's current QB1 from the latest 2026 depth chart.
4. Flag the player if their 2025 primary QB differs from their
   current team's presumed 2026 starting QB.

This is deliberately separate from:
- team-change detection
- injury detection
- ESPN player/injury data

Output:
    src/utils/qb_context.js

Usage:
    python3 scripts/fetch_qb_context.py
"""

import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import nflreadpy as nfl
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
NFL_DATA_PATH = ROOT / "src" / "utils" / "nfl_data.js"
OUTPUT_PATH = ROOT / "src" / "utils" / "qb_context.js"

PRIOR_SEASON = 2025
CURRENT_SEASON = 2026

RECEIVER_POSITIONS = {"WR", "TE", "RB"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def load_current_players() -> list[dict]:
    """
    Load the current player universe from nfl_data.js.

    nfl_data.js is the authoritative source for:
      - which players Gridiron Oracle cares about
      - their current team
      - their position
      - their GSIS ID
    """
    log.info(f"Loading current player universe from {NFL_DATA_PATH}...")

    text = NFL_DATA_PATH.read_text(encoding="utf-8")

    # nfl_data.js contains:
    #
    # export const NFL_PLAYERS = [...];
    #
    # Extract the JSON array.
    marker = "export const NFL_PLAYERS = "
    start = text.find(marker)

    if start == -1:
        raise RuntimeError(
            f"Could not find '{marker}' in {NFL_DATA_PATH}"
        )

    start += len(marker)

    # Find the terminating semicolon.
    end = text.find("];", start)

    if end == -1:
        raise RuntimeError(
            f"Could not find end of NFL_PLAYERS array in {NFL_DATA_PATH}"
        )

    raw = text[start:end + 1]

    # nfl_data.js may contain bare NaN values, which Python's JSON parser
    # accepts by default.
    players = json.loads(raw)

    receivers = [
        p for p in players
        if p.get("position") in RECEIVER_POSITIONS
        and p.get("gsis_id")
        and p.get("team")
    ]

    log.info(
        f"Loaded {len(players)} total players; "
        f"{len(receivers)} current WR/TE/RB pass-catchers"
    )

    return receivers


def fetch_2025_primary_qb_by_receiver() -> dict:
    """
    Determine each receiver's primary QB in 2025.

    Returns:
        {
            receiver_gsis_id: {
                "qb_gsis_id": passer_gsis_id,
                "target_count": int
            }
        }

    Primary QB = passer responsible for the most targets to the receiver
    during the 2025 regular season.
    """
    log.info(
        f"Determining each receiver's primary {PRIOR_SEASON} QB "
        f"from play-by-play..."
    )

    pbp = nfl.load_pbp([PRIOR_SEASON]).to_pandas()

    required = {"pass_attempt", "receiver_id", "passer_id"}

    missing = required - set(pbp.columns)
    if missing:
        raise RuntimeError(
            f"PBP data is missing required columns: {sorted(missing)}"
        )

    targets = pbp[
        (pbp["pass_attempt"] == 1)
        & pbp["receiver_id"].notna()
        & pbp["passer_id"].notna()
    ].copy()

    counts = (
        targets
        .groupby(["receiver_id", "passer_id"])
        .size()
        .reset_index(name="target_count")
    )

    # Sort explicitly so ties resolve deterministically.
    counts = counts.sort_values(
        ["receiver_id", "target_count", "passer_id"],
        ascending=[True, False, True],
    )

    primary = (
        counts
        .drop_duplicates(subset="receiver_id", keep="first")
        .set_index("receiver_id")
        .apply(
            lambda row: {
                "qb_gsis_id": row["passer_id"],
                "target_count": int(row["target_count"]),
            },
            axis=1,
        )
        .to_dict()
    )

    log.info(
        f"Mapped primary {PRIOR_SEASON} QB for "
        f"{len(primary)} receivers"
    )

    return primary


def fetch_current_starters() -> dict:
    """
    Get the latest QB1 for every team from the 2026 depth charts.

    Returns:
        {
            team_abbr: qb_gsis_id
        }
    """
    log.info(
        f"Fetching current ({CURRENT_SEASON}) depth charts..."
    )

    dc = nfl.load_depth_charts([CURRENT_SEASON]).to_pandas()

    required = {"team", "pos_abb", "pos_rank", "gsis_id", "dt"}
    missing = required - set(dc.columns)

    if missing:
        raise RuntimeError(
            f"Depth-chart data is missing required columns: "
            f"{sorted(missing)}"
        )

    qbs = dc[
        (dc["pos_abb"] == "QB")
        & (dc["pos_rank"] == 1)
        & dc["gsis_id"].notna()
        & dc["team"].notna()
    ].copy()

    if qbs.empty:
        raise RuntimeError(
            "No QB1 entries found in current depth-chart data."
        )

    # Latest snapshot for each team.
    qbs = (
        qbs
        .sort_values("dt")
        .drop_duplicates(subset="team", keep="last")
    )

    starters = dict(zip(qbs["team"], qbs["gsis_id"]))

    log.info(
        f"Current QB1 mapped for {len(starters)} teams"
    )

    return starters


def build_name_lookup() -> dict:
    """
    GSIS ID -> display name.
    """
    log.info("Building GSIS player-name lookup...")

    ids = nfl.load_ff_playerids().to_pandas()

    if "gsis_id" not in ids.columns or "name" not in ids.columns:
        raise RuntimeError(
            "Player ID data does not contain gsis_id/name columns."
        )

    ids = ids.dropna(subset=["gsis_id"])

    return dict(
        zip(
            ids["gsis_id"].astype(str),
            ids["name"],
        )
    )


def build_context(
    current_players: list[dict],
    primary_qb_map: dict,
    current_starters: dict,
    name_lookup: dict,
) -> dict:
    """
    Compare each CURRENT Gridiron Oracle pass-catcher's 2025 primary QB
    against the QB1 of their CURRENT 2026 team.

    Important:
        The player's team comes from nfl_data.js, NOT from 2025 stats.

    This means an offseason team change is handled correctly:

        2025 QB relationship
                +
        2026 current team
                ↓
        2026 current team's QB1

    Only players with an actual QB change are included.
    """

    context = {}

    matched = 0
    no_history = 0
    no_current_qb = 0
    unchanged = 0

    for player in current_players:
        receiver_gsis = str(player["gsis_id"])
        current_team = player["team"]

        historical = primary_qb_map.get(receiver_gsis)

        if not historical:
            no_history += 1
            continue

        current_qb_gsis = current_starters.get(current_team)

        if not current_qb_gsis:
            no_current_qb += 1
            continue

        prior_qb_gsis = str(historical["qb_gsis_id"])
        current_qb_gsis = str(current_qb_gsis)

        matched += 1

        if current_qb_gsis == prior_qb_gsis:
            unchanged += 1
            continue

        context[receiver_gsis] = {
            "player_name": player.get("name", "Unknown"),
            "position": player.get("position", ""),
            "current_team": current_team,

            "prior_qb_name": name_lookup.get(
                prior_qb_gsis,
                "Unknown",
            ),
            "prior_qb_gsis_id": prior_qb_gsis,
            "prior_qb_target_count": historical["target_count"],

            "current_qb_name": name_lookup.get(
                current_qb_gsis,
                "Unknown",
            ),
            "current_qb_gsis_id": current_qb_gsis,

            "qb_changed": True,
        }

    log.info(f"Current pass-catchers matched to 2025 QB history: {matched}")
    log.info(f"Current pass-catchers with no 2025 QB history: {no_history}")
    log.info(f"Current teams with no QB1 found: {no_current_qb}")
    log.info(f"Players whose QB did not change: {unchanged}")
    log.info(f"QB-change flags generated: {len(context)}")

    return context


def write_output(context: dict):
    """
    Write the generated JS module.
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    js_content = f"""// qb_context.js — Gridiron Oracle QB change detector
// AUTO-GENERATED by scripts/fetch_qb_context.py — DO NOT EDIT MANUALLY
//
// Prior season: {PRIOR_SEASON}
// Current season: {CURRENT_SEASON}
// Generated: {timestamp}
//
// The player universe and CURRENT team come from nfl_data.js.
// Historical QB relationships come from nflreadpy PBP.
// Current QB1 comes from nflreadpy depth charts.
//
// Only includes current Gridiron Oracle WR/TE/RB players whose
// current team's presumed QB1 differs from the QB who threw them
// the most targets during {PRIOR_SEASON}.

export const QB_CONTEXT_BY_GSIS = {json.dumps(
        context,
        indent=2,
    )};

export const QB_CONTEXT_META = {{
  prior_season: {PRIOR_SEASON},
  current_season: {CURRENT_SEASON},
  generated_at: "{timestamp}",
  flagged_count: {len(context)},
}};
"""

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(js_content, encoding="utf-8")

    log.info(
        f"Wrote {len(context)} QB-change flags to {OUTPUT_PATH}"
    )


def main():
    log.info("=== QB context fetch starting ===")

    current_players = load_current_players()
    primary_qb_map = fetch_2025_primary_qb_by_receiver()
    current_starters = fetch_current_starters()
    name_lookup = build_name_lookup()

    context = build_context(
        current_players,
        primary_qb_map,
        current_starters,
        name_lookup,
    )

    write_output(context)

    log.info("=== QB context fetch complete ===")


if __name__ == "__main__":
    main()