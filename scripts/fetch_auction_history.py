#!/usr/bin/env python3
"""
fetch_auction_history.py — Gridiron Oracle

Fetch historical ESPN auction drafts and build the auction-history artifact.

ID resolution order:
    1. Canonical ESPN_TO_GSIS / PLAYER_INDEX from src/utils/id_mapping.js
    2. ESPN current player metadata (kona_player_info)
    3. Unresolved record retained in AUCTION_HISTORY_UNMAPPED

The auction draft itself is authoritative for:
    - season
    - auction rank
    - ESPN player ID
    - player name
    - bid amount
    - keeper status
    - round / pick

Historical market values exclude keeper picks.
"""

import argparse
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, stdev

import requests
from dotenv import load_dotenv
from espn_api.football import League


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"

ID_MAPPING_PATH = ROOT / "src" / "utils" / "id_mapping.js"
OUTPUT_PATH = ROOT / "src" / "utils" / "auction_history.js"

LEAGUE_ID = 839979
DEFAULT_SEASONS = [2021, 2022, 2023, 2024, 2025]

LEAGUE_SIZE = 12
BUDGET_PER_TEAM = 200

ESPN_PLAYER_URL = (
    "https://lm-api-reads.fantasy.espn.com/"
    "apis/v3/games/ffl/seasons/2026/segments/0/leaguedefaults/3"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ESPN fantasy position IDs
ESPN_POSITION_MAP = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    5: "K",
    16: "DST",
}


# ESPN NFL team IDs.
# Used only to provide useful team metadata for fallback records.
ESPN_TEAM_MAP = {
    0: "FA",
    1: "ATL",
    2: "BUF",
    3: "CHI",
    4: "CIN",
    5: "CLE",
    6: "DAL",
    7: "DEN",
    8: "DET",
    9: "GB",
    10: "TEN",
    11: "IND",
    12: "KC",
    13: "LV",
    14: "LAR",
    15: "MIA",
    16: "MIN",
    17: "NE",
    18: "NO",
    19: "NYG",
    20: "NYJ",
    21: "PHI",
    22: "ARI",
    23: "PIT",
    24: "LAC",
    25: "SF",
    26: "SEA",
    27: "TB",
    28: "WAS",
    29: "CAR",
    30: "JAX",
    33: "BAL",
    34: "HOU",
}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def load_credentials():
    load_dotenv(ENV_PATH)

    espn_s2 = os.getenv("ESPN_S2")
    swid = os.getenv("SWID")

    if not espn_s2 or not swid:
        log.error(f"ESPN_S2 and SWID not found in {ENV_PATH}")
        raise SystemExit(1)

    return espn_s2.strip(), swid.strip()


def extract_object(text, name):
    """
    Extract a JSON object assigned to:

        export const NAME = {...};

    from a generated JS file.
    """
    marker = f"export const {name} ="
    start = text.find(marker)

    if start == -1:
        raise ValueError(f"Could not find {name} in {ID_MAPPING_PATH}")

    object_start = text.find("{", start)

    if object_start == -1:
        raise ValueError(f"Could not find object start for {name}")

    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text[object_start:])

    return obj


def load_canonical_mapping():
    log.info(f"Loading canonical ID mapping from {ID_MAPPING_PATH}...")

    text = ID_MAPPING_PATH.read_text(encoding="utf-8")

    espn_to_gsis = extract_object(text, "ESPN_TO_GSIS")
    player_index = extract_object(text, "PLAYER_INDEX")

    log.info(
        f"  {len(espn_to_gsis)} ESPN -> GSIS mappings available"
    )
    log.info(
        f"  {len(player_index)} GSIS player metadata records available"
    )

    return espn_to_gsis, player_index


def connect_league(espn_s2, swid, season):
    log.info(
        f"Fetching league settings/draft (season={season})..."
    )

    league = League(
        league_id=LEAGUE_ID,
        year=season,
        espn_s2=espn_s2,
        swid=swid,
    )

    league_name = (
        getattr(league, "name", None)
        or getattr(getattr(league, "settings", None), "name", None)
        or ""
    )

    log.info(
        f"  Connected to '{league_name}' — "
        f"{len(getattr(league, 'teams', []))} teams"
    )

    return league


# ---------------------------------------------------------------------------
# ESPN fallback metadata
# ---------------------------------------------------------------------------

def load_espn_player_metadata(espn_s2, swid):
    """
    Load ESPN's current player universe once.

    This is a fallback only. Canonical id_mapping.js remains primary.
    """

    log.info("Loading ESPN player metadata fallback...")

    filter_payload = {
        "players": {
            "limit": 3000,
            "sortPercOwned": {
                "sortPriority": 4,
                "sortAsc": False,
            },
        }
    }

    headers = {
        "Cookie": f"espn_s2={espn_s2}; SWID={swid}",
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "X-Fantasy-Filter": json.dumps(filter_payload),
    }

    response = requests.get(
        ESPN_PLAYER_URL,
        params={"view": "kona_player_info"},
        headers=headers,
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()
    players = data.get("players", [])

    metadata = {}

    for item in players:
        player = item.get("player")

        if not player:
            continue

        espn_id = str(player.get("id", "")).strip()

        if not espn_id:
            continue

        position_id = player.get("defaultPositionId")
        position = ESPN_POSITION_MAP.get(position_id)

        pro_team_id = player.get("proTeamId")
        team = ESPN_TEAM_MAP.get(pro_team_id)

        metadata[espn_id] = {
            "player_name": player.get("fullName"),
            "position": position,
            "team": team,
            "espn_position_id": position_id,
            "pro_team_id": pro_team_id,
        }

    log.info(
        f"  ESPN fallback metadata: {len(metadata)} players"
    )

    return metadata


# ---------------------------------------------------------------------------
# Player resolution
# ---------------------------------------------------------------------------

def resolve_player(
    espn_id,
    player_name,
    espn_to_gsis,
    player_index,
    espn_metadata,
):
    """
    Resolve an ESPN draft player.

    Returns:
        gsis_id
        player_name
        position
        team
        source
    """

    espn_id = str(espn_id).strip()

    # ---------------------------------------------------------------
    # 1. Canonical mapping
    # ---------------------------------------------------------------

    gsis_id = espn_to_gsis.get(espn_id)

    if gsis_id:
        meta = player_index.get(gsis_id, {})

        return {
            "gsis_id": gsis_id,
            "player_name": meta.get("name") or player_name,
            "position": meta.get("position") or None,
            "team": meta.get("team") or None,
            "source": "canonical",
        }

    # ---------------------------------------------------------------
    # 2. ESPN fallback
    # ---------------------------------------------------------------

    fallback = espn_metadata.get(espn_id)

    if fallback:
        return {
            "gsis_id": None,
            "player_name": fallback.get("player_name") or player_name,
            "position": fallback.get("position"),
            "team": fallback.get("team"),
            "source": "espn_fallback",
        }

    # ---------------------------------------------------------------
    # 3. Historical unresolved
    # ---------------------------------------------------------------

    return {
        "gsis_id": None,
        "player_name": player_name,
        "position": None,
        "team": None,
        "source": "unresolved",
    }


# ---------------------------------------------------------------------------
# Draft extraction
# ---------------------------------------------------------------------------

def fetch_season_draft(
    league,
    season,
    espn_to_gsis,
    player_index,
    espn_metadata,
):
    picks = list(getattr(league, "draft", []) or [])

    log.info(f"  {len(picks)} picks found")

    records = []
    unmatched = []

    for auction_rank, pick in enumerate(picks, start=1):
        espn_id = str(
            getattr(pick, "playerId", "")
        ).strip()

        player_name = (
            getattr(pick, "playerName", None)
            or ""
        ).strip()

        bid_amount = getattr(
            pick,
            "bid_amount",
            None,
        )

        keeper_status = bool(
            getattr(
                pick,
                "keeper_status",
                False,
            )
        )

        round_num = getattr(
            pick,
            "round_num",
            None,
        )

        round_pick = getattr(
            pick,
            "round_pick",
            None,
        )

        if bid_amount is not None:
            try:
                bid_amount = int(bid_amount)
            except (TypeError, ValueError):
                raise ValueError(
                    f"Invalid bid amount for {player_name}: "
                    f"{bid_amount}"
                )

        if bid_amount is not None and bid_amount < 0:
            raise ValueError(
                f"Negative bid for {player_name}: {bid_amount}"
            )

        resolved = resolve_player(
            espn_id=espn_id,
            player_name=player_name,
            espn_to_gsis=espn_to_gsis,
            player_index=player_index,
            espn_metadata=espn_metadata,
        )

        record = {
            "season": season,
            "auction_rank": auction_rank,
            "espn_id": espn_id,
            "gsis_id": resolved["gsis_id"],
            "player_name": resolved["player_name"],
            "position": resolved["position"],
            "team": resolved["team"],
            "bid_amount": bid_amount,
            "keeper_status": keeper_status,
            "round_num": round_num,
            "round_pick": round_pick,
            "resolution_source": resolved["source"],
        }

        records.append(record)

        if resolved["source"] == "unresolved":
            unmatched.append(record)

    canonical_count = sum(
        r["resolution_source"] == "canonical"
        for r in records
    )

    fallback_count = sum(
        r["resolution_source"] == "espn_fallback"
        for r in records
    )

    unresolved_count = sum(
        r["resolution_source"] == "unresolved"
        for r in records
    )

    log.info(
        f"  Resolved: {canonical_count} canonical, "
        f"{fallback_count} ESPN fallback, "
        f"{unresolved_count} unresolved"
    )

    return records, unmatched


# ---------------------------------------------------------------------------
# Historical market calculations
# ---------------------------------------------------------------------------

def calculate_historical_market(season_bids):
    """
    season_bids contains only non-keeper bids.
    """

    bids = [
        r["bid_amount"]
        for r in season_bids
        if r.get("bid_amount") is not None
    ]

    if not bids:
        return {
            "avg_bid": None,
            "median_bid": None,
            "weighted_bid": None,
            "min_bid": None,
            "max_bid": None,
            "std_dev": None,
            "seasons_count": 0,
        }

    # Recency weights:
    # oldest → 1
    # newest → 5
    #
    # This is deliberately modest. Historical prices remain useful
    # without allowing one recent auction to completely dominate.

    by_season = sorted(
        season_bids,
        key=lambda r: r["season"],
    )

    weighted_numerator = 0
    weighted_denominator = 0

    season_values = defaultdict(list)

    for record in by_season:
        season = record["season"]
        bid = record["bid_amount"]

        if bid is None:
            continue

        season_values[season].append(bid)

    seasons = sorted(season_values)

    for index, season in enumerate(seasons, start=1):
        season_avg = mean(season_values[season])

        weighted_numerator += season_avg * index
        weighted_denominator += index

    weighted_bid = (
        weighted_numerator / weighted_denominator
        if weighted_denominator
        else None
    )

    return {
        "avg_bid": round(mean(bids), 2),
        "median_bid": round(median(bids), 2),
        "weighted_bid": round(weighted_bid, 2)
        if weighted_bid is not None
        else None,
        "min_bid": min(bids),
        "max_bid": max(bids),
        "std_dev": round(stdev(bids), 2)
        if len(bids) >= 2
        else 0,
        "seasons_count": len(season_values),
    }


def build_player_history(records):
    """
    Build the GSIS-keyed player history.

    Only records with a GSIS ID can belong here.
    Keeper picks remain visible in season_bids but are excluded
    from historical_market calculations.
    """

    by_gsis = defaultdict(list)

    for record in records:
        gsis_id = record.get("gsis_id")

        if not gsis_id:
            continue

        by_gsis[gsis_id].append(record)

    output = {}

    for gsis_id, player_records in by_gsis.items():
        player_records.sort(
            key=lambda r: (
                r["season"],
                r["auction_rank"],
            )
        )

        first = player_records[0]

        season_bids = [
            {
                "season": r["season"],
                "auction_rank": r["auction_rank"],
                "bid_amount": r["bid_amount"],
            }
            for r in player_records
        ]

        non_keeper = [
            r
            for r in player_records
            if not r["keeper_status"]
            and r["bid_amount"] is not None
        ]

        market = calculate_historical_market(
            non_keeper
        )

        output[gsis_id] = {
            "player_name": first["player_name"],
            "position": first["position"],
            "seasons": sorted(
                {
                    r["season"]
                    for r in player_records
                }
            ),
            "season_bids": season_bids,
            "historical_market": market,
            "weighted_bid_trend": market["weighted_bid"],
            "avg_bid_non_keeper": market["avg_bid"],
            "most_recent_bid": (
                non_keeper[-1]["bid_amount"]
                if non_keeper
                else None
            ),
            "most_recent_season": (
                non_keeper[-1]["season"]
                if non_keeper
                else None
            ),
        }

    return output


def build_unmapped_history(records):
    """
    Preserve records without GSIS IDs.

    These include D/ST, some kickers, and potentially historical
    players absent from the canonical crosswalk/current ESPN universe.
    """

    output = []

    for record in records:
        if record.get("gsis_id"):
            continue

        output.append({
            "season": record["season"],
            "auction_rank": record["auction_rank"],
            "espn_id": record["espn_id"],
            "player_name": record["player_name"],
            "position": record["position"],
            "team": record["team"],
            "bid_amount": record["bid_amount"],
            "keeper_status": record["keeper_status"],
            "round_num": record["round_num"],
            "round_pick": record["round_pick"],
            "resolution_source": record["resolution_source"],
        })

    return output


def build_season_history(records):
    output = defaultdict(list)

    for record in records:
        output[record["season"]].append(record)

    for season in output:
        output[season].sort(
            key=lambda r: r["auction_rank"]
        )

    return dict(sorted(output.items()))


def build_season_summaries(records):
    by_season = defaultdict(list)

    for record in records:
        by_season[record["season"]].append(record)

    summaries = {}

    for season, season_records in sorted(by_season.items()):
        bids = [
            r["bid_amount"]
            for r in season_records
            if r["bid_amount"] is not None
        ]

        non_keeper_bids = [
            r["bid_amount"]
            for r in season_records
            if not r["keeper_status"]
            and r["bid_amount"] is not None
        ]

        summaries[str(season)] = {
            "picks": len(season_records),
            "total_spend": sum(bids),
            "non_keeper_spend": sum(non_keeper_bids),
            "avg_bid": round(mean(bids), 2)
            if bids
            else None,
            "median_bid": round(median(bids), 2)
            if bids
            else None,
            "max_bid": max(bids)
            if bids
            else None,
        }

    return summaries


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(records, expected_seasons):
    expected_counts = {
        2021: 156,
        2022: 130,
        2023: 156,
        2024: 156,
        2025: 156,
    }

    expected_spend = {
        2021: 2326,
        2022: 1935,
        2023: 2359,
        2024: 2362,
        2025: 2328,
    }

    by_season = defaultdict(list)

    for record in records:
        by_season[record["season"]].append(record)

    errors = []

    for season in expected_seasons:
        actual_count = len(by_season[season])

        if season in expected_counts:
            if actual_count != expected_counts[season]:
                errors.append(
                    f"{season}: expected "
                    f"{expected_counts[season]} picks, "
                    f"got {actual_count}"
                )

        total_spend = sum(
            r["bid_amount"]
            for r in by_season[season]
            if r["bid_amount"] is not None
        )

        if season in expected_spend:
            if total_spend != expected_spend[season]:
                errors.append(
                    f"{season}: expected total spend "
                    f"{expected_spend[season]}, "
                    f"got {total_spend}"
                )

    if errors:
        for error in errors:
            log.error(f"VALIDATION ERROR: {error}")

        raise RuntimeError(
            "Auction history validation failed."
        )

    log.info("Validation: historical pick counts and spend match CSV reference.")


# ---------------------------------------------------------------------------
# JS output
# ---------------------------------------------------------------------------

def js_dump(obj):
    return json.dumps(
        obj,
        indent=2,
        ensure_ascii=False,
    )


def write_output(
    all_records,
    player_history,
    season_history,
    season_summaries,
    unmapped,
    seasons,
):
    generated_at = datetime.now(
        timezone.utc
    ).isoformat()

    canonical_count = sum(
        r["resolution_source"] == "canonical"
        for r in all_records
    )

    fallback_count = sum(
        r["resolution_source"] == "espn_fallback"
        for r in all_records
    )

    unresolved_count = sum(
        r["resolution_source"] == "unresolved"
        for r in all_records
    )

    header = f"""// auction_history.js — Gridiron Oracle historical auction data
// AUTO-GENERATED by scripts/fetch_auction_history.py — DO NOT EDIT MANUALLY
// League: {LEAGUE_ID} | Seasons: {", ".join(map(str, seasons))}
// Generated: {generated_at}
// Total auction records: {len(all_records)}
// Canonical GSIS matches: {canonical_count}
// ESPN fallback matches: {fallback_count}
// Unresolved records: {unresolved_count}
//
// ID architecture:
// ESPN draft playerId
//   -> ESPN_TO_GSIS / PLAYER_INDEX when available
//   -> ESPN kona_player_info fallback when available
//   -> retained in AUCTION_HISTORY_UNMAPPED when unresolved
//
// Historical market values exclude keeper picks.
// """

    meta = {
        "league_id": LEAGUE_ID,
        "league_size": LEAGUE_SIZE,
        "budget_per_team": BUDGET_PER_TEAM,
        "seasons": seasons,
        "generated_at": generated_at,
        "total_fetched": len(all_records),
        "canonical_matches": canonical_count,
        "espn_fallback_matches": fallback_count,
        "total_unmatched": unresolved_count,
        "matched_records": len(all_records) - unresolved_count,
    }

    content = (
        header
        + "\n"
        + "export const AUCTION_HISTORY_META = "
        + js_dump(meta)
        + ";\n\n"
        + "export const AUCTION_HISTORY_BY_GSIS = "
        + js_dump(player_history)
        + ";\n\n"
        + "export const AUCTION_HISTORY_BY_SEASON = "
        + js_dump(season_history)
        + ";\n\n"
        + "export const AUCTION_SEASON_SUMMARIES = "
        + js_dump(season_summaries)
        + ";\n\n"
        + "export const AUCTION_HISTORY_UNMAPPED = "
        + js_dump(unmapped)
        + ";\n"
    )

    OUTPUT_PATH.write_text(
        content,
        encoding="utf-8",
    )

    log.info(
        f"✓ Wrote auction history to {OUTPUT_PATH}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--seasons",
        nargs="+",
        type=int,
        default=DEFAULT_SEASONS,
        help="Seasons to fetch",
    )

    args = parser.parse_args()

    seasons = args.seasons

    log.info(
        f"=== Fetching auction history — "
        f"seasons={seasons} ==="
    )

    espn_s2, swid = load_credentials()

    espn_to_gsis, player_index = (
        load_canonical_mapping()
    )

    # Load ESPN fallback metadata exactly once.
    espn_metadata = load_espn_player_metadata(
        espn_s2,
        swid,
    )

    all_records = []

    for season in seasons:
        league = connect_league(
            espn_s2,
            swid,
            season,
        )

        records, _ = fetch_season_draft(
            league=league,
            season=season,
            espn_to_gsis=espn_to_gsis,
            player_index=player_index,
            espn_metadata=espn_metadata,
        )

        all_records.extend(records)

    # ---------------------------------------------------------------
    # Validate the raw ESPN draft retrieval before generating output.
    # ---------------------------------------------------------------

    log.info(
        f"Validation: {len(all_records)} total "
        f"auction records fetched"
    )

    validate(
        all_records,
        seasons,
    )

    # ---------------------------------------------------------------
    # Build artifacts
    # ---------------------------------------------------------------

    player_history = build_player_history(
        all_records
    )

    season_history = build_season_history(
        all_records
    )

    season_summaries = build_season_summaries(
        all_records
    )

    unmapped = build_unmapped_history(
        all_records
    )

    write_output(
        all_records=all_records,
        player_history=player_history,
        season_history=season_history,
        season_summaries=season_summaries,
        unmapped=unmapped,
        seasons=seasons,
    )

    log.info(
        f"=== Auction history complete: "
        f"{len(all_records)} records ==="
    )


if __name__ == "__main__":
    main()