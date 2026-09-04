#!/usr/bin/env python3
"""
fetch_espn_aav.py — Gridiron Oracle
Fetches ESPN's own real Average Auction Value (AAV) data — the actual
platform-wide market price, not a calculation. Since this is what your
league drafts on (ESPN), this is the most direct "Mkt $" signal
available: no recency-weighting math, no small-sample-size own-league
history — just what ESPN's whole platform is currently paying.
Source:
    ESPN Fantasy API — kona_player_info (same endpoint as fetch_adp_stg.py)
Output:
    src/utils/espn_aav.js — keyed by GSIS ID (via id_mapping.js crosswalk),
    ready to slot directly into DraftBoard.jsx's Mkt $ lookup.
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

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "src" / "utils" / "espn_aav.js"
ENV_PATH = ROOT / ".env"
ID_MAPPING_PATH = ROOT / "src" / "utils" / "id_mapping.js"

SEASON = 2026
LEAGUE_DEFAULT_ID = 3

ESPN_URL = (
    f"https://lm-api-reads.fantasy.espn.com/"
    f"apis/v3/games/ffl/seasons/{SEASON}/"
    f"segments/0/leaguedefaults/{LEAGUE_DEFAULT_ID}"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

POSITION_MAP = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}


def load_credentials():
    load_dotenv(ENV_PATH)
    espn_s2 = os.getenv("ESPN_S2")
    swid = os.getenv("SWID")
    if not espn_s2 or not swid:
        log.error(f"ESPN_S2 and SWID not found in {ENV_PATH}")
        sys.exit(1)
    return espn_s2.strip(), swid.strip()


def load_espn_to_gsis_crosswalk() -> dict:
    """
    Reuse the existing ESPN ID -> GSIS ID crosswalk from id_mapping.js
    (built by build_id_mapping.py). Avoids re-deriving a crosswalk and
    avoids any name-matching collision risk entirely, since this is a
    direct ID -> ID lookup already proven correct today.
    """
    if not ID_MAPPING_PATH.exists():
        log.error(f"{ID_MAPPING_PATH} not found — run build_id_mapping.py first")
        sys.exit(1)
    content = ID_MAPPING_PATH.read_text(encoding="utf-8")
    match = re.search(r"export const ESPN_TO_GSIS\s*=\s*(\{.*?\});", content, re.DOTALL)
    if not match:
        log.error("Could not parse ESPN_TO_GSIS from id_mapping.js")
        sys.exit(1)
    crosswalk = json.loads(match.group(1))
    log.info(f"Loaded {len(crosswalk)} ESPN->GSIS mappings from id_mapping.js")
    return crosswalk


def fetch_players(espn_s2: str, swid: str) -> list:
    log.info("=== Gridiron Oracle — ESPN AAV fetch ===")
    log.info(f"Fetching ESPN auction value data ({SEASON})...")

    filter_payload = {
        "players": {
            "limit": 3000,
            "sortPercOwned": {"sortPriority": 4, "sortAsc": False},
        }
    }
    headers = {
        "Cookie": f"espn_s2={espn_s2}; SWID={swid}",
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "X-Fantasy-Filter": json.dumps(filter_payload),
    }
    params = {"view": "kona_player_info"}

    try:
        response = requests.get(ESPN_URL, params=params, headers=headers, timeout=30)
    except requests.RequestException as e:
        log.error(f"ESPN request failed: {e}")
        sys.exit(1)

    if response.status_code != 200:
        log.error(f"ESPN returned HTTP {response.status_code}")
        log.error(f"Response: {response.text[:500]}")
        sys.exit(1)

    try:
        data = response.json()
    except ValueError as e:
        log.error(f"ESPN returned invalid JSON: {e}")
        sys.exit(1)

    players = data.get("players", [])
    if not players:
        log.error("ESPN returned zero player records — file NOT written")
        sys.exit(1)

    log.info(f"  {len(players)} player records returned")
    return players


def extract_aav(entry: dict) -> dict | None:
    """
    Pull real ESPN auction value data.
    entry["draftAuctionValue"]  — outer-level, keeper-aware auction value
    entry["player"]["ownership"]["auctionValueAverage"] — live platform AAV
    """
    player = entry.get("player", {})
    if not player:
        return None

    espn_id = player.get("id")
    name = player.get("fullName")
    if not espn_id or not name:
        return None

    ownership = player.get("ownership") or {}
    aav = ownership.get("auctionValueAverage")

    try:
        aav = float(aav) if aav is not None else None
    except (TypeError, ValueError):
        aav = None

    if aav is None or aav <= 0:
        return None

    position_id = player.get("defaultPositionId")

    return {
        "espn_id": espn_id,
        "name": name,
        "position": POSITION_MAP.get(position_id, "UNK"),
        "aav": round(aav, 2),
        "aav_change": ownership.get("auctionValueAverageChange"),
        "draft_auction_value": entry.get("draftAuctionValue"),
    }


def build_records(players: list) -> list:
    records = []
    excluded = 0
    for entry in players:
        record = extract_aav(entry)
        if record is None:
            excluded += 1
            continue
        records.append(record)

    log.info(f"  Excluded {excluded} players with no meaningful AAV")

    if not records:
        log.error("No players with valid AAV found — file NOT written")
        sys.exit(1)

    records.sort(key=lambda r: r["aav"], reverse=True)
    log.info(f"  {len(records)} players with valid AAV")

    log.info("  Top 10 by AAV:")
    for i, r in enumerate(records[:10], start=1):
        log.info(f"    {i:2}. {r['name']:25} {r['position']:4} AAV ${r['aav']:.2f}")

    return records


def write_output(records: list, crosswalk: dict):
    by_gsis = {}
    unmatched = 0

    for r in records:
        gsis_id = crosswalk.get(str(r["espn_id"]))
        if not gsis_id:
            unmatched += 1
            continue
        by_gsis[gsis_id] = {
            "name": r["name"],
            "position": r["position"],
            "aav": r["aav"],
            "aav_change": r["aav_change"],
        }

    log.info(f"  Matched {len(by_gsis)}/{len(records)} to GSIS IDs ({unmatched} unmatched)")

    timestamp = datetime.now(timezone.utc).isoformat()
    js_content = f"""// espn_aav.js — AUTO-GENERATED by scripts/fetch_espn_aav.py
// Source: ESPN Fantasy Football API (kona_player_info)
// Real platform-wide Average Auction Value — not a calculation.
// Fetched: {timestamp}
// DO NOT EDIT BY HAND

// Keyed by GSIS ID for direct use in DraftBoard.jsx
export const ESPN_AAV_BY_GSIS = {json.dumps(by_gsis, indent=2)};

export const ESPN_AAV_META = {{
  source: "ESPN Fantasy Football API",
  season: {SEASON},
  fetched_at: "{timestamp}",
  player_count: {len(by_gsis)},
}};
"""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(js_content)
    log.info(f"Written: {len(by_gsis)} players → {OUTPUT_PATH.relative_to(Path.cwd())}")


def main():
    espn_s2, swid = load_credentials()
    crosswalk = load_espn_to_gsis_crosswalk()
    players = fetch_players(espn_s2, swid)
    records = build_records(players)
    write_output(records, crosswalk)
    log.info("=== ESPN AAV fetch complete ===")


if __name__ == "__main__":
    main()