#!/usr/bin/env python3
"""
fetch_auction_history.py — Gridiron Oracle League Auction History
==================================================================

Fetches historical ESPN auction drafts and generates:

    src/utils/auction_history.js

Canonical ID architecture:

    ESPN draft playerId
        -> ESPN_TO_GSIS (src/utils/id_mapping.js)
        -> PLAYER_INDEX (src/utils/id_mapping.js)
        -> position / player metadata

IMPORTANT:
    Do NOT use nfl_data_py here.
    Do NOT rebuild the ESPN -> GSIS crosswalk.
    id_mapping.js is the project's canonical generated ID artifact.

Usage:
    python scripts/fetch_auction_history.py
    python scripts/fetch_auction_history.py --seasons 2021 2022 2023 2024 2025

Requirements:
    pip install espn-api python-dotenv
"""

import argparse
import json
import logging
import math
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from espn_api.football import League


# ---------------------------------------------------------------------------
# Paths / configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent

ENV_PATH = ROOT / ".env"
ID_MAPPING_PATH = ROOT / "src" / "utils" / "id_mapping.js"
OUTPUT_PATH = ROOT / "src" / "utils" / "auction_history.js"

LEAGUE_ID = 839979
DEFAULT_SEASONS = [2021, 2022, 2023, 2024, 2025]

# Current league configuration used only for metadata.
LEAGUE_SIZE = 12
BUDGET_PER_TEAM = 200


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def load_credentials():
    """Load and validate ESPN credentials using the project's .env."""

    load_dotenv(ENV_PATH)

    espn_s2 = os.getenv("ESPN_S2")
    swid = os.getenv("SWID")

    if not espn_s2 or not swid:
        log.error(f"ESPN_S2 and SWID not found in {ENV_PATH}")
        sys.exit(1)

    return espn_s2.strip(), swid.strip()


# ---------------------------------------------------------------------------
# id_mapping.js loader
# ---------------------------------------------------------------------------

def extract_exported_object(text: str, export_name: str) -> dict:
    """
    Extract a JSON-compatible exported object from id_mapping.js.

    The generated file contains:

        export const ESPN_TO_GSIS = {...};
        export const PLAYER_INDEX = {...};

    Because the generated objects are JSON-compatible, we locate the
    assignment and use json.JSONDecoder to parse the object safely.
    """

    marker = f"export const {export_name} ="

    start = text.find(marker)

    if start == -1:
        raise ValueError(
            f"Could not find '{marker}' in {ID_MAPPING_PATH}"
        )

    object_start = text.find("{", start)

    if object_start == -1:
        raise ValueError(
            f"Could not find opening '{{' for {export_name}"
        )

    decoder = json.JSONDecoder()

    try:
        obj, _ = decoder.raw_decode(text[object_start:])
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Could not parse {export_name} from {ID_MAPPING_PATH}: {exc}"
        ) from exc

    if not isinstance(obj, dict):
        raise ValueError(
            f"{export_name} did not resolve to an object"
        )

    return obj


def load_id_mapping():
    """
    Load the project's canonical ESPN -> GSIS mapping and player metadata.

    Returns:
        espn_to_gsis: { ESPN ID string -> GSIS ID }
        player_index: { GSIS ID -> metadata }
    """

    if not ID_MAPPING_PATH.exists():
        log.error(f"ID mapping not found: {ID_MAPPING_PATH}")
        log.error("Run scripts/build_id_mapping.py first.")
        sys.exit(1)

    log.info(f"Loading canonical ID mapping from {ID_MAPPING_PATH}...")

    text = ID_MAPPING_PATH.read_text(encoding="utf-8")

    espn_to_gsis = extract_exported_object(text, "ESPN_TO_GSIS")
    player_index = extract_exported_object(text, "PLAYER_INDEX")

    log.info(
        f"  {len(espn_to_gsis)} ESPN -> GSIS mappings available"
    )
    log.info(
        f"  {len(player_index)} GSIS player metadata records available"
    )

    return espn_to_gsis, player_index


# ---------------------------------------------------------------------------
# ESPN connection
# ---------------------------------------------------------------------------

def connect_league(espn_s2, swid, season):
    """Connect to ESPN for a specific historical season."""

    log.info(
        f"Fetching league settings/draft (season={season})..."
    )

    try:
        league = League(
            league_id=LEAGUE_ID,
            year=season,
            espn_s2=espn_s2,
            swid=swid,
        )

        league_name = (
            getattr(league, "name", None)
            or getattr(getattr(league, "settings", None), "name", None)
            or f"League {LEAGUE_ID}"
        )

        log.info(
            f"  Connected to '{league_name}' — "
            f"{len(league.teams)} teams"
        )

        return league

    except Exception as exc:
        log.error(
            f"Failed to connect to league {LEAGUE_ID} "
            f"(season={season}): {exc}"
        )
        return None


# ---------------------------------------------------------------------------
# Draft extraction
# ---------------------------------------------------------------------------

def get_attr(obj, *names, default=None):
    """Return the first available non-None attribute."""

    for name in names:
        value = getattr(obj, name, None)

        if value is not None:
            return value

    return default


def fetch_season_draft(league, season, espn_to_gsis, player_index):
    """
    Extract auction picks for one season.

    Position is resolved through:

        pick.playerId
          -> ESPN_TO_GSIS
          -> PLAYER_INDEX[gsis_id]["position"]
    """

    picks = getattr(league, "draft", None)

    if not picks:
        log.warning(f"  No draft picks found for {season}")
        return [], 0

    log.info(f"  {len(picks)} picks found")

    records = []
    unmatched = 0

    for auction_rank, pick in enumerate(picks, start=1):

        espn_id = get_attr(pick, "playerId")

        if espn_id is None:
            log.warning(
                f"  Pick #{auction_rank} has no playerId; skipping"
            )
            unmatched += 1
            continue

        espn_id = str(espn_id).strip()

        gsis_id = espn_to_gsis.get(espn_id)

        if not gsis_id:
            unmatched += 1
            continue

        meta = player_index.get(gsis_id, {})

        position = meta.get("position") or ""
        player_name = (
            meta.get("name")
            or get_attr(pick, "playerName", default="")
            or ""
        )

        team = meta.get("team") or ""

        bid_amount = get_attr(
            pick,
            "bid_amount",
            "bidAmount",
            default=None,
        )

        if bid_amount is None:
            log.warning(
                f"  {player_name} ({espn_id}) has no bid amount; skipping"
            )
            unmatched += 1
            continue

        keeper_status = bool(
            get_attr(
                pick,
                "keeper_status",
                "keeperStatus",
                default=False,
            )
        )

        round_num = get_attr(
            pick,
            "round_num",
            "roundNum",
            default=None,
        )

        round_pick = get_attr(
            pick,
            "round_pick",
            "roundPick",
            default=None,
        )

        records.append({
            "season": season,
            "auction_rank": auction_rank,
            "espn_id": espn_id,
            "gsis_id": gsis_id,
            "player_name": player_name,
            "position": position,
            "team": team,
            "bid_amount": int(bid_amount),
            "keeper_status": keeper_status,
            "round_num": round_num,
            "round_pick": round_pick,
        })

    return records, unmatched


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def safe_mean(values):
    return round(statistics.mean(values), 2) if values else None


def safe_median(values):
    return round(statistics.median(values), 2) if values else None


def safe_stdev(values):
    if len(values) < 2:
        return 0.0

    return round(statistics.stdev(values), 2)


def calculate_weighted_bid(records):
    """
    Recency-weighted historical bid.

    Most recent season receives the highest weight.

    For five seasons:
        2021 = 1
        2022 = 2
        2023 = 3
        2024 = 4
        2025 = 5

    Missing seasons simply receive no weight.
    """

    if not records:
        return None

    by_season = defaultdict(list)

    for record in records:
        if not record["keeper_status"]:
            by_season[record["season"]].append(
                record["bid_amount"]
            )

    if not by_season:
        return None

    max_season = max(by_season)

    numerator = 0.0
    denominator = 0.0

    for season, bids in by_season.items():
        weight = season - min(by_season) + 1

        numerator += safe_mean(bids) * weight
        denominator += weight

    if denominator == 0:
        return None

    return round(numerator / denominator, 2)


def build_player_history(all_records):
    """
    Aggregate historical market data by GSIS ID.

    Historical statistics exclude keeper picks because keeper prices
    are not representative of open auction market value.
    """

    grouped = defaultdict(list)

    for record in all_records:
        if not record["keeper_status"]:
            grouped[record["gsis_id"]].append(record)

    history = {}

    for gsis_id, records in grouped.items():

        bids = [r["bid_amount"] for r in records]

        seasons = sorted(
            set(r["season"] for r in records)
        )

        first = records[0]

        history[gsis_id] = {
            "player_name": first["player_name"],
            "position": first["position"],
            "seasons": seasons,
            "season_bids": [
                {
                    "season": r["season"],
                    "auction_rank": r["auction_rank"],
                    "bid_amount": r["bid_amount"],
                }
                for r in sorted(
                    records,
                    key=lambda x: x["season"]
                )
            ],
            "historical_market": {
                "avg_bid": safe_mean(bids),
                "median_bid": safe_median(bids),
                "weighted_bid": calculate_weighted_bid(records),
                "min_bid": min(bids),
                "max_bid": max(bids),
                "std_dev": safe_stdev(bids),
                "seasons_count": len(seasons),
            },

            # Backward-compatible fields.
            "weighted_bid_trend": calculate_weighted_bid(records),
            "avg_bid_non_keeper": safe_mean(bids),
            "most_recent_bid": records[-1]["bid_amount"],
            "most_recent_season": records[-1]["season"],
        }

    return history


# ---------------------------------------------------------------------------
# Season summaries
# ---------------------------------------------------------------------------

def build_season_summaries(all_records):
    """Build summary statistics for each fetched season."""

    grouped = defaultdict(list)

    for record in all_records:
        grouped[record["season"]].append(record)

    summaries = {}

    for season, records in sorted(grouped.items()):

        open_market = [
            r for r in records
            if not r["keeper_status"]
        ]

        bids = [r["bid_amount"] for r in open_market]

        position_summary = {}

        by_position = defaultdict(list)

        for record in open_market:
            if record["position"]:
                by_position[record["position"]].append(
                    record["bid_amount"]
                )

        for position, position_bids in sorted(
            by_position.items()
        ):
            position_summary[position] = {
                "count": len(position_bids),
                "avg_bid": safe_mean(position_bids),
                "median_bid": safe_median(position_bids),
                "max_bid": max(position_bids),
            }

        summaries[str(season)] = {
            "pick_count": len(records),
            "open_market_count": len(open_market),
            "keeper_count": len(records) - len(open_market),
            "total_spend": sum(bids),
            "avg_bid": safe_mean(bids),
            "median_bid": safe_median(bids),
            "max_bid": max(bids) if bids else None,
            "position_summary": position_summary,
        }

    return summaries


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_output(
    all_records,
    player_history,
    season_summaries,
    seasons,
    total_fetched,
    total_unmatched,
):
    """Write the generated auction history JavaScript module."""

    timestamp = datetime.now(timezone.utc).isoformat()

    # Keep individual auction records separate from the player-level
    # aggregation. This makes the source data auditable.
    records_by_season = defaultdict(list)

    for record in all_records:
        records_by_season[record["season"]].append(record)

    output = {
        "meta": {
            "league_id": LEAGUE_ID,
            "league_size": LEAGUE_SIZE,
            "budget_per_team": BUDGET_PER_TEAM,
            "seasons": seasons,
            "generated_at": timestamp,
            "total_fetched": total_fetched,
            "total_unmatched": total_unmatched,
            "matched_records": len(all_records),
        },
        "records_by_season": dict(
            sorted(records_by_season.items())
        ),
        "players": player_history,
        "season_summaries": season_summaries,
    }

    js_content = (
        "// auction_history.js — Gridiron Oracle historical auction data\n"
        "// AUTO-GENERATED by scripts/fetch_auction_history.py — DO NOT EDIT MANUALLY\n"
        f"// League: {LEAGUE_ID} | Seasons: {', '.join(map(str, seasons))}\n"
        f"// Generated: {timestamp}\n"
        f"// Matched auction records: {len(all_records)} / {total_fetched}\n"
        f"// Unmatched auction records: {total_unmatched}\n"
        "//\n"
        "// ID architecture:\n"
        "// ESPN draft playerId -> ESPN_TO_GSIS -> GSIS ID\n"
        "// Historical player metadata is resolved from the canonical ID mapping.\n"
        "//\n"
        "// Historical market values exclude keeper picks.\n\n"
        f"export const AUCTION_HISTORY_META = "
        f"{json.dumps(output['meta'], indent=2)};\n\n"
        f"export const AUCTION_HISTORY_BY_GSIS = "
        f"{json.dumps(player_history, indent=2)};\n\n"
        f"export const AUCTION_HISTORY_BY_SEASON = "
        f"{json.dumps(output['records_by_season'], indent=2)};\n\n"
        f"export const AUCTION_SEASON_SUMMARIES = "
        f"{json.dumps(season_summaries, indent=2)};\n"
    )

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT_PATH.write_text(
        js_content,
        encoding="utf-8",
    )

    log.info(
        f"✓ Wrote auction history to {OUTPUT_PATH}"
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_records(all_records, seasons, total_fetched):
    """Run sanity checks before writing generated output."""

    if not all_records:
        log.error("No matched auction records. Refusing to write output.")
        sys.exit(1)

    seasons_found = sorted(
        set(r["season"] for r in all_records)
    )

    if seasons_found != sorted(seasons):
        log.warning(
            f"Expected seasons {sorted(seasons)}, "
            f"found {seasons_found}"
        )

    bad_bids = [
        r for r in all_records
        if r["bid_amount"] < 0
    ]

    if bad_bids:
        log.error(
            f"Found {len(bad_bids)} records with negative bids."
        )
        sys.exit(1)

    bad_positions = [
        r for r in all_records
        if not r["position"]
    ]

    if bad_positions:
        log.warning(
            f"{len(bad_positions)} matched records have no position."
        )

    log.info(
        f"Validation: {len(all_records)} matched records "
        f"from {total_fetched} fetched picks"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():

    parser = argparse.ArgumentParser(
        description="Fetch ESPN league auction history"
    )

    parser.add_argument(
        "--seasons",
        nargs="+",
        type=int,
        default=DEFAULT_SEASONS,
        help="Seasons to fetch",
    )

    args = parser.parse_args()

    seasons = sorted(set(args.seasons))

    log.info(
        f"=== Fetching auction history — seasons={seasons} ==="
    )

    espn_s2, swid = load_credentials()

    espn_to_gsis, player_index = load_id_mapping()

    all_records = []
    total_fetched = 0
    total_unmatched = 0

    for season in seasons:

        league = connect_league(
            espn_s2,
            swid,
            season,
        )

        if league is None:
            continue

        records, unmatched = fetch_season_draft(
            league,
            season,
            espn_to_gsis,
            player_index,
        )

        total_fetched += len(getattr(league, "draft", []) or [])
        total_unmatched += unmatched

        all_records.extend(records)

        log.info(
            f"  Matched {len(records)} picks; "
            f"{unmatched} unmatched"
        )

    validate_records(
        all_records,
        seasons,
        total_fetched,
    )

    player_history = build_player_history(
        all_records
    )

    season_summaries = build_season_summaries(
        all_records
    )

    write_output(
        all_records,
        player_history,
        season_summaries,
        seasons,
        total_fetched,
        total_unmatched,
    )

    log.info(
        f"=== Auction history complete: "
        f"{len(all_records)}/{total_fetched} matched ==="
    )


if __name__ == "__main__":
    main()