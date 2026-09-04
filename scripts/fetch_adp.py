#!/usr/bin/env python3
"""
fetch_adp.py — Gridiron Oracle
Pulls current-season ADP (Average Draft Position) data from the
Fantasy Football Calculator public REST API (free, no key required).
Matches your league's scoring: 12-team, full PPR.

Output: src/utils/adp_data.js
"""

"""
fetch_adp_stg.py — Gridiron Oracle

Fetches ESPN player ADP data for the 2026 season.

Source:
    ESPN Fantasy API — kona_player_info

League:
    12-team
    Full PPR

Credentials:
    ESPN_S2 and SWID loaded from .env

Output:
    src/utils/adp_data.js
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "src" / "utils" / "adp_data.js"
ENV_PATH = ROOT / ".env"

SEASON = 2026
TEAMS = 12
LEAGUE_DEFAULT_ID = 3

ESPN_URL = (
    f"https://lm-api-reads.fantasy.espn.com/"
    f"apis/v3/games/ffl/seasons/{SEASON}/"
    f"segments/0/leaguedefaults/{LEAGUE_DEFAULT_ID}"
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize_name(name: str) -> str:
    """
    Normalize a player name for cross-source matching.

    Lowercase, strip punctuation and generational suffixes.
    """

    if not name:
        return ""

    n = name.lower()
    n = re.sub(r"[.'\-]", "", n)
    n = re.sub(r"\s+", " ", n).strip()

    parts = [
        p for p in n.split(" ")
        if p not in SUFFIXES
    ]

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def load_credentials():
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

def fetch_players(espn_s2, swid):
    """
    Fetch ESPN's full player pool using kona_player_info.

    The important structure returned by ESPN is:

        players[]
            player
                id
                fullName
                defaultPositionId
                proTeamId
                ownership
                    averageDraftPosition
                    percentOwned
                    percentStarted
                draftRanksByRankType
                    PPR
                        rank
    """

    log.info("=== Gridiron Oracle — ESPN ADP fetch ===")
    log.info(
        f"Fetching ESPN player ADP data "
        f"({TEAMS}-team, full PPR, {SEASON})..."
    )

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
        "Cookie": (
            f"espn_s2={espn_s2}; "
            f"SWID={swid}"
        ),
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "X-Fantasy-Filter": json.dumps(
            filter_payload
        ),
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
        )
    except requests.RequestException as e:
        log.error(f"ESPN request failed: {e}")
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
    except ValueError as e:
        log.error(
            f"ESPN returned invalid JSON: {e}"
        )
        log.error(
            f"Response: {response.text[:500]}"
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
# Position mapping
# ---------------------------------------------------------------------------

POSITION_MAP = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    5: "K",
    16: "DST",
}


# ---------------------------------------------------------------------------
# Player extraction
# ---------------------------------------------------------------------------

def extract_player(entry):
    """
    Extract the fields needed by Gridiron Oracle.

    ESPN's actual response structure:

        entry["player"]["ownership"]["averageDraftPosition"]

    PPR draft rank is:

        entry["player"]
            ["draftRanksByRankType"]
            ["PPR"]
            ["rank"]
    """

    player = entry.get("player", {})

    if not player:
        return None

    name = player.get("fullName")

    if not name:
        return None

    ownership = player.get("ownership") or {}

    adp = ownership.get(
        "averageDraftPosition"
    )

    if adp is None:
        return None

    try:
        adp = float(adp)
    except (TypeError, ValueError):
        return None

    if adp <= 0:
        return None

    position_id = player.get(
        "defaultPositionId"
    )

    draft_ranks = (
        player.get("draftRanksByRankType")
        or {}
    )

    ppr_rank_data = (
        draft_ranks.get("PPR")
        or {}
    )

    ppr_rank = ppr_rank_data.get("rank")

    return {
        "player_id": player.get("id"),
        "name": name,
        "normalized_name": normalize_name(name),

        "position": POSITION_MAP.get(
            position_id,
            "UNK",
        ),

        "position_id": position_id,
        "team_id": player.get("proTeamId"),

        "adp": adp,
        "ppr_rank": ppr_rank,

        "percent_owned": ownership.get(
            "percentOwned"
        ),

        "percent_started": ownership.get(
            "percentStarted"
        ),
    }


# ---------------------------------------------------------------------------
# Build ADP records
# ---------------------------------------------------------------------------

def build_records(players):

    records = []
    excluded = 0

    for entry in players:

        record = extract_player(entry)

        if record is None:
            excluded += 1
            continue

        records.append(record)

    log.info(
        f"  Excluded {excluded} players "
        f"with no meaningful ADP"
    )

    if not records:
        log.error(
            "No players with valid ADP found — "
            "file NOT written"
        )
        sys.exit(1)

    # Lower ADP = earlier expected draft position.
    records.sort(
        key=lambda r: r["adp"]
    )

    log.info(
        f"  {len(records)} players with valid ADP"
    )

    # Diagnostic output so we can immediately see whether ESPN's
    # ADP values look sane.
    log.info("  First 10 ESPN ADP records:")

    for i, record in enumerate(
        records[:10],
        start=1,
    ):
        log.info(
            f"    {i:2}. "
            f"{record['name']} | "
            f"{record['position']} | "
            f"ADP {record['adp']:.2f} | "
            f"PPR rank {record['ppr_rank']}"
        )

    return records


# ---------------------------------------------------------------------------
# Write JavaScript
# ---------------------------------------------------------------------------

def write_output(records):
    # Build name lookup, but remove ambiguous names rather than allowing
    # one player to silently overwrite another.
    name_groups = {}

    for r in records:
        key = r["normalized_name"]
        if key:
            name_groups.setdefault(key, []).append(r)

    by_name = {}
    ambiguous = {}

    for name, players in name_groups.items():
        if len(players) == 1:
            by_name[name] = players[0]
        else:
            ambiguous[name] = players

    if ambiguous:
        log.warning(
            f"  {len(ambiguous)} normalized-name collisions detected — "
            f"ambiguous names excluded from ADP_BY_NAME"
        )

        for name, players in ambiguous.items():
            for p in players:
                log.warning(
                    f"    {p['name']} | ESPN ID {p['player_id']} | "
                    f"ADP {p['adp']}"
                )

    js_content = f"""// adp_data.js — AUTO-GENERATED by scripts/fetch_adp_stg.py
// Source: ESPN Fantasy Football API
// League match: {TEAMS}-team, full PPR
// Fetched: {datetime.now(timezone.utc).isoformat()}
// DO NOT EDIT BY HAND

export const ADP_LIST = {json.dumps(records, indent=2)};

// Lookup by normalized name.
//
// IMPORTANT:
// Only unambiguous player names are included here.
// Players sharing the same normalized name are intentionally excluded
// to prevent an incorrect name-based join.
//
// Use ESPN player ID when an exact identity match is available.
export const ADP_BY_NAME = {json.dumps(by_name, indent=2)};

export const ADP_META = {{
  source: "ESPN Fantasy Football API",
  scoring: "ppr",
  teams: {TEAMS},
  season: {SEASON},
  fetched_at: "{datetime.now(timezone.utc).isoformat()}",
  player_count: {len(records)},
  ambiguous_name_count: {len(ambiguous)},
}};
"""

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(js_content)

    log.info(
        f"Written: {len(records)} players → "
        f"{OUTPUT_PATH.relative_to(Path.cwd())}"
    )

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():

    espn_s2, swid = load_credentials()

    players = fetch_players(
        espn_s2,
        swid,
    )

    records = build_records(players)

    write_output(records)

    log.info(
        "=== ESPN ADP fetch complete ==="
    )


if __name__ == "__main__":
    main()