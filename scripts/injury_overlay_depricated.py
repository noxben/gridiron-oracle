#!/usr/bin/env python3
"""
fetch_injuries.py — Gridiron Oracle ESPN Injury Data
=====================================================

Pulls current-season player injury/availability data from the ESPN
Fantasy Football API.

Uses the same authenticated ESPN endpoint and .env credentials as
the rest of the Gridiron Oracle ESPN data pipeline.

Output:
    src/utils/injury_data.js

Primary identity:
    ESPN player ID

The generated data is intentionally separate from nfl_data.js.
Injury information is current-state data and should be refreshed
independently of the statistical player dataset.

Usage:
    python scripts/fetch_injuries.py
    python scripts/fetch_injuries.py --dry-run
    python scripts/fetch_injuries.py --report

Recommended refresh:
    - Daily during preseason
    - Thursday / Friday during the regular season
    - Sunday morning before games
    - Immediately following significant injury news

Required .env:
    ESPN_S2=...
    SWID=...
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

OUTPUT_PATH = ROOT / "src" / "utils" / "injury_data.js"

SEASON = 2026

ESPN_URL = (
    f"https://lm-api-reads.fantasy.espn.com/"
    f"apis/v3/games/ffl/seasons/{SEASON}/"
    f"segments/0/leaguedefaults/3"
)

# ESPN uses these status values in player.injuryStatus.
#
# These are intentionally availability probabilities rather than
# fantasy-value multipliers. The ranking engine can decide how strongly
# to apply the probability later.
INJURY_STATUS_MAP = {
    "ACTIVE": 1.00,
    "NORMAL": 1.00,
    "PROBABLE": 0.92,
    "QUESTIONABLE": 0.55,
    "GTD": 0.55,
    "DOUBTFUL": 0.25,
    "OUT": 0.00,
    "IR": 0.00,
    "PUP": 0.00,
    "SUSPENSION": 0.00,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ESPN authentication
# ---------------------------------------------------------------------------

def load_credentials():
    """Load ESPN authentication credentials from .env."""

    load_dotenv(ENV_PATH)

    espn_s2 = os.getenv("ESPN_S2")
    swid = os.getenv("SWID")

    if not espn_s2 or not swid:
        log.error(
            f"ESPN_S2 and SWID not found in {ENV_PATH}"
        )
        sys.exit(1)

    return espn_s2.strip(), swid.strip()


# ---------------------------------------------------------------------------
# ESPN API
# ---------------------------------------------------------------------------

def fetch_espn_players(espn_s2: str, swid: str) -> list[dict]:
    """
    Fetch current ESPN player information.

    The important structure is:

        response["players"][]
            ["id"]
            ["player"]
                ["fullName"]
                ["firstName"]
                ["lastName"]
                ["defaultPositionId"]
                ["proTeamId"]
                ["injured"]
                ["injuryStatus"]
                ["injuryDetails"]
                ["injuryDate"]
                ["lastNewsDate"]

    """

    log.info(
        f"Fetching ESPN player injury data "
        f"({SEASON})..."
    )

    headers = {
        "Cookie": (
            f"espn_s2={espn_s2}; "
            f"SWID={swid}"
        ),
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }

    params = {
        "view": "kona_player_info",
    }

    try:
        response = requests.get(
            ESPN_URL,
            params=params,
            headers=headers,
            timeout=30,
            allow_redirects=False,
        )

    except requests.RequestException as exc:
        log.error(f"ESPN request failed: {exc}")
        sys.exit(1)

    if response.status_code != 200:
        log.error(
            f"ESPN returned HTTP {response.status_code}"
        )
        log.error(
            f"Response: {response.text[:500]}"
        )
        sys.exit(1)

    try:
        data = response.json()
    except ValueError as exc:
        log.error(f"ESPN returned invalid JSON: {exc}")
        log.error(
            f"Response starts with: {response.text[:500]}"
        )
        sys.exit(1)

    players = data.get("players", [])

    if not players:
        log.error(
            "ESPN returned zero player records — "
            "file NOT written"
        )
        sys.exit(1)

    log.info(
        f"  {len(players)} player records returned"
    )

    return players


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ESPN_POSITION_MAP = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    5: "K",
    16: "D/ST",
}


def normalize_status(status):
    """Normalize ESPN injury status."""

    if not status:
        return "ACTIVE"

    status = str(status).strip().upper()

    if status in INJURY_STATUS_MAP:
        return status

    log.warning(
        f"Unknown ESPN injury status: {status!r} "
        f"— defaulting to ACTIVE"
    )

    return "ACTIVE"


def extract_injury_detail(player):
    """
    Extract ESPN's injuryDetails value safely.

    ESPN has used slightly different shapes for injuryDetails over time,
    so this intentionally handles strings, dictionaries and missing data.
    """

    details = player.get("injuryDetails")

    if not details:
        return ""

    if isinstance(details, str):
        return details.strip()

    if isinstance(details, dict):

        # Common ESPN fields.
        parts = []

        for key in (
            "type",
            "description",
            "detail",
            "location",
            "side",
        ):
            value = details.get(key)

            if value:
                parts.append(str(value).strip())

        if parts:
            return " — ".join(parts)

        # Preserve unexpected structures rather than silently throwing
        # useful information away.
        return json.dumps(
            details,
            separators=(",", ":"),
        )

    return str(details)


def timestamp_to_iso(value):
    """Convert ESPN millisecond timestamp to ISO-8601 UTC."""

    if not value:
        return None

    try:
        timestamp = float(value) / 1000.0
        return datetime.fromtimestamp(
            timestamp,
            tz=timezone.utc,
        ).isoformat()

    except (TypeError, ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# Build injury records
# ---------------------------------------------------------------------------

def build_injury_records(players):
    """
    Convert ESPN player records into a compact normalized injury dataset.

    Every player with meaningful ESPN player information is retained.

    This is important because a player becoming healthy again needs to
    be represented as ACTIVE rather than disappearing from the dataset.
    """

    records = []
    excluded = 0

    for wrapper in players:

        player = wrapper.get("player") or {}

        espn_id = (
            player.get("id")
            or wrapper.get("id")
        )

        if not espn_id:
            excluded += 1
            continue

        name = (
            player.get("fullName")
            or " ".join(
                x for x in (
                    player.get("firstName"),
                    player.get("lastName"),
                )
                if x
            )
        ).strip()

        if not name:
            excluded += 1
            continue

        position_id = player.get("defaultPositionId")

        position = ESPN_POSITION_MAP.get(
            position_id,
            "UNK",
        )

        status = normalize_status(
            player.get("injuryStatus")
        )

        injured = bool(
            player.get("injured")
        )

        injury_detail = extract_injury_detail(
            player
        )

        injury_date = timestamp_to_iso(
            player.get("injuryDate")
        )

        last_news_date = timestamp_to_iso(
            player.get("lastNewsDate")
        )

        play_probability = INJURY_STATUS_MAP.get(
            status,
            1.0,
        )

        # ESPN occasionally has stale/odd combinations where injured=false
        # but an old injuryStatus remains. ACTIVE should take precedence
        # when ESPN explicitly says the player is not injured.
        if not injured and status not in {
            "OUT",
            "IR",
            "PUP",
            "SUSPENSION",
        }:
            status = "ACTIVE"
            play_probability = 1.0

        record = {
            "espn_id": int(espn_id),
            "name": name,
            "position": position,
            "position_id": position_id,
            "pro_team_id": player.get("proTeamId"),
            "injured": injured,
            "injury_status": status,
            "play_probability": play_probability,
            "injury_detail": injury_detail or None,
            "injury_date": injury_date,
            "last_news_date": last_news_date,
        }

        records.append(record)

    log.info(
        f"  {len(records)} normalized injury records"
    )

    if excluded:
        log.warning(
            f"  Excluded {excluded} records "
            f"with no usable ESPN player ID/name"
        )

    return records


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def print_summary(records):
    """Print current ESPN injury summary."""

    flagged = [
        r for r in records
        if r["play_probability"] < 1.0
    ]

    flagged.sort(
        key=lambda r: (
            r["play_probability"],
            r["name"],
        )
    )

    print()
    print("=" * 78)
    print(
        f"  ESPN Injury Report — "
        f"{len(flagged)} players with reduced availability"
    )
    print("=" * 78)

    if not flagged:
        print("  No players currently flagged by ESPN.")
        print()
        return

    for r in flagged:

        prob = r["play_probability"]

        if prob == 0.0:
            icon = "🔴"
        elif prob <= 0.55:
            icon = "🟡"
        else:
            icon = "🟠"

        detail = r["injury_detail"] or r["injury_status"]

        print(
            f"  {icon} "
            f"{r['name']:<25} "
            f"{r['position']:<4} "
            f"status={r['injury_status']:<13} "
            f"play_prob={prob:.2f}  "
            f"{detail}"
        )

    print()


def print_status_counts(records):
    """Print counts by ESPN injury status."""

    counts = {}

    for r in records:
        status = r["injury_status"]
        counts[status] = counts.get(status, 0) + 1

    print()
    print("ESPN injury status counts:")

    for status in sorted(counts):
        print(
            f"  {status:<15} {counts[status]}"
        )

    print()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_output(records, dry_run=False):

    fetched_at = datetime.now(
        timezone.utc
    ).isoformat()

    # Exact ESPN ID lookup.
    by_espn_id = {
        str(r["espn_id"]): r
        for r in records
    }

    # Name lookup is deliberately only populated for unique names.
    # ESPN IDs should be preferred whenever available.
    name_groups = {}

    for r in records:
        key = r["name"].lower().strip()

        if key:
            name_groups.setdefault(
                key,
                [],
            ).append(r)

    by_name = {}
    collisions = {}

    for name, players in name_groups.items():

        if len(players) == 1:
            by_name[name] = players[0]

        else:
            collisions[name] = players

    if collisions:
        log.warning(
            f"  {len(collisions)} duplicate player names "
            f"excluded from INJURY_BY_NAME"
        )

        for name, players in collisions.items():
            for player in players:
                log.warning(
                    f"    {player['name']} | "
                    f"ESPN ID {player['espn_id']}"
                )

    js_content = f"""// injury_data.js — AUTO-GENERATED by scripts/fetch_injuries.py
// Source: ESPN Fantasy Football API
// Season: {SEASON}
// Fetched: {fetched_at}
// DO NOT EDIT BY HAND

// Complete current-season ESPN injury/availability records.
// ESPN player ID is the preferred identity key.
export const INJURY_LIST = {json.dumps(records, indent=2)};

// Exact lookup by ESPN player ID.
export const INJURY_BY_ESPN_ID = {json.dumps(by_espn_id, indent=2)};

// Fallback lookup by lowercase full name.
// Ambiguous names are intentionally excluded.
export const INJURY_BY_NAME = {json.dumps(by_name, indent=2)};

export const INJURY_META = {{
  source: "ESPN Fantasy Football API",
  season: {SEASON},
  fetched_at: "{fetched_at}",
  player_count: {len(records)},
  injured_count: {sum(1 for r in records if r["injured"])},
  reduced_availability_count: {
        sum(1 for r in records if r["play_probability"] < 1.0)
    },
  ambiguous_name_count: {len(collisions)},
}};
"""

    if dry_run:
        log.info(
            "DRY RUN — injury_data.js was NOT written"
        )
        return

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT_PATH.write_text(
        js_content,
        encoding="utf-8",
    )

    log.info(
        f"Written: {len(records)} players → "
        f"{OUTPUT_PATH.relative_to(ROOT)}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Fetch current ESPN injury/availability data "
            "for Gridiron Oracle"
        )
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and analyze data without writing the JS file",
    )

    parser.add_argument(
        "--report",
        action="store_true",
        help="Print injury report summary",
    )

    parser.add_argument(
        "--counts",
        action="store_true",
        help="Print injury status counts",
    )

    args = parser.parse_args()

    log.info(
        "=== Gridiron Oracle — ESPN Injury fetch ==="
    )

    espn_s2, swid = load_credentials()

    players = fetch_espn_players(
        espn_s2,
        swid,
    )

    records = build_injury_records(
        players
    )

    if not records:
        log.error(
            "No usable injury records found — "
            "file NOT written"
        )
        sys.exit(1)

    if args.report:
        print_summary(records)

    if args.counts:
        print_status_counts(records)

    if args.report or args.counts:
        return

    write_output(
        records,
        dry_run=args.dry_run,
    )

    log.info(
        "=== ESPN Injury fetch complete ==="
    )


if __name__ == "__main__":
    main()