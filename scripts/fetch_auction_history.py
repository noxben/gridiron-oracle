#!/usr/bin/env python3
"""
fetch_auction_history.py — Gridiron Oracle Auction History Fetcher
=====================================================================
Pulls historical auction draft results for league 839979 across past
seasons, and joins them to GSIS IDs so the draft board can build bid
recommendations from real league-specific spending patterns.

Keeper picks are flagged separately (keeper_status=True) since keeper
bid amounts are set by league keeper rules, not real auction market
value — they should not be blended into "market average bid" without
that context.

Output: src/utils/auction_history.js

Usage:
  python3 scripts/fetch_auction_history.py
  python3 scripts/fetch_auction_history.py --seasons 2020 2021 2022 2023 2024 2025
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import nfl_data_py as nfl
from dotenv import load_dotenv
from espn_api.football import League

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "src" / "utils" / "auction_history.js"
ENV_PATH = ROOT / ".env"

LEAGUE_ID = 839979
DEFAULT_SEASONS = [2022, 2023, 2024, 2025]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def build_espn_to_gsis_crosswalk() -> dict:
    """
    Same crosswalk approach as build_id_mapping.py — needed here to join
    ESPN's numeric playerId (from draft picks) to our GSIS-keyed player data.
    """
    log.info("Building ESPN ID -> GSIS ID crosswalk...")
    ids = nfl.import_ids()
    espn_col = "espn_id" if "espn_id" in ids.columns else "fantasy_data_id"
    gsis_col = "gsis_id" if "gsis_id" in ids.columns else "gsis_it_id"
    df = ids[[espn_col, gsis_col]].dropna()
    df[espn_col] = df[espn_col].astype(str).str.strip().str.split(".").str[0]
    df[gsis_col] = df[gsis_col].astype(str).str.strip()
    crosswalk = dict(zip(df[espn_col], df[gsis_col]))
    log.info(f"  {len(crosswalk)} ESPN->GSIS mappings available")
    return crosswalk


def fetch_league_budget(espn_s2: str, swid: str, current_season: int) -> dict:
    """Pull acquisition_budget and related settings from current season."""
    log.info(f"Fetching league settings (season={current_season})...")
    try:
        league = League(league_id=LEAGUE_ID, year=current_season, espn_s2=espn_s2, swid=swid)
        s = league.settings
        return {
            "acquisition_budget": getattr(s, "acquisition_budget", None),
            "team_count": getattr(s, "team_count", None),
            "keeper_count": getattr(s, "keeper_count", None),
        }
    except Exception as e:
        log.warning(f"Could not fetch current season settings: {e}")
        return {}


def fetch_season_draft(espn_s2: str, swid: str, season: int) -> list:
    """Pull one season's full auction draft results."""
    log.info(f"Fetching {season} draft...")
    try:
        league = League(league_id=LEAGUE_ID, year=season, espn_s2=espn_s2, swid=swid)
        picks = league.draft
        log.info(f"  {len(picks)} picks found")
        return picks
    except Exception as e:
        log.warning(f"  Failed to fetch {season} draft: {e}")
        return []


def assemble_history(all_picks: dict, crosswalk: dict) -> dict:
    """
    Merge picks across seasons into a per-GSIS-ID history.
    all_picks: { season: [pick, ...] }
    Returns: { gsis_id: { name, seasons: [...], avg_bid_non_keeper, most_recent_bid } }
    """
    by_gsis = {}
    unmatched = 0
    total = 0

    for season, picks in all_picks.items():
        for pick in picks:
            total += 1
            espn_id = str(getattr(pick, "playerId", ""))
            gsis_id = crosswalk.get(espn_id)
            if not gsis_id:
                unmatched += 1
                continue

            entry = by_gsis.setdefault(gsis_id, {
                "name": getattr(pick, "playerName", "Unknown"),
                "seasons": [],
            })
            entry["seasons"].append({
                "season": season,
                "bid_amount": getattr(pick, "bid_amount", None),
                "round_num": getattr(pick, "round_num", None),
                "keeper_status": bool(getattr(pick, "keeper_status", False)),
            })

    # Compute summary stats per player
    for gsis_id, entry in by_gsis.items():
        non_keeper_bids = [
            s["bid_amount"] for s in entry["seasons"]
            if not s["keeper_status"] and s["bid_amount"] is not None
        ]
        entry["avg_bid_non_keeper"] = (
            round(sum(non_keeper_bids) / len(non_keeper_bids), 1)
            if non_keeper_bids else None
        )
        seasons_sorted = sorted(entry["seasons"], key=lambda s: s["season"], reverse=True)
        entry["most_recent_bid"] = seasons_sorted[0]["bid_amount"] if seasons_sorted else None
        entry["most_recent_season"] = seasons_sorted[0]["season"] if seasons_sorted else None

    log.info(f"Matched {total - unmatched}/{total} picks to GSIS IDs ({unmatched} unmatched — likely retired/practice-squad players)")
    return by_gsis


def write_output(history: dict, budget_info: dict, seasons: list):
    timestamp = datetime.now(timezone.utc).isoformat()
    js_content = f"""// auction_history.js — Gridiron Oracle league auction history
// AUTO-GENERATED by scripts/fetch_auction_history.py — DO NOT EDIT MANUALLY
// Seasons covered: {seasons} | Generated: {timestamp}
//
// Keeper picks are flagged (keeper_status: true) — their bid_amount reflects
// league keeper rules, not real auction market value. avg_bid_non_keeper
// excludes keeper picks; use it as the "real market price" signal.

export const AUCTION_HISTORY_META = {{
  seasons: {json.dumps(seasons)},
  generated_at: "{timestamp}",
  league_budget: {json.dumps(budget_info)},
  player_count: {len(history)},
}};

// Keyed by GSIS ID
export const AUCTION_HISTORY_BY_GSIS = {json.dumps(history, indent=2)};
"""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(js_content, encoding="utf-8")
    log.info(f"Wrote {len(history)} players' auction history to {OUTPUT_PATH}")


def main():
    parser = argparse.ArgumentParser(description="Fetch league auction draft history")
    parser.add_argument("--seasons", type=int, nargs="+", default=DEFAULT_SEASONS)
    args = parser.parse_args()

    load_dotenv(ENV_PATH)
    espn_s2 = os.getenv("ESPN_S2")
    swid = os.getenv("SWID")
    if not espn_s2 or not swid:
        log.error("ESPN_S2/SWID not found in .env")
        sys.exit(1)

    log.info(f"=== Fetching auction history — seasons={args.seasons} ===")

    crosswalk = build_espn_to_gsis_crosswalk()
    budget_info = fetch_league_budget(espn_s2, swid, current_season=2026)

    all_picks = {}
    for season in args.seasons:
        all_picks[season] = fetch_season_draft(espn_s2, swid, season)

    history = assemble_history(all_picks, crosswalk)
    write_output(history, budget_info, args.seasons)

    log.info("=== Auction history fetch complete ===")


if __name__ == "__main__":
    main()