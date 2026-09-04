#!/usr/bin/env python3
"""
injury_overlay.py — Gridiron Oracle Injury Overlay
====================================================
Overlays current ESPN injury/roster status on top of an existing
nfl_data.js file WITHOUT re-pulling all nflfastR/nflreadpy stats.

MIGRATION NOTE (2026-08): Previously used nflreadpy.load_injuries(),
which only covers seasons nflreadpy considers "current" — it was stuck
on 2025 and returned nothing usable for the 2026 season. Rewritten to
pull directly from ESPN's own Fantasy API (the same endpoint used by
fetch_adp_stg.py and fetch_espn_aav.py), which reflects real, current
2026 player status.

Use this for rapid injury status updates without needing to refresh
the full statistical pipeline.

Usage:
  python scripts/injury_overlay.py              # update existing nfl_data.js
  python scripts/injury_overlay.py --dry-run    # preview changes only
  python scripts/injury_overlay.py --report     # print injury report summary

Schedule:
  - Any time during the season, especially Thursday/Sunday mornings
  - Any time after significant injury news breaks
"""
import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

ROOT        = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "src" / "utils" / "nfl_data.js"
ID_MAPPING_PATH = ROOT / "src" / "utils" / "id_mapping.js"
ENV_PATH    = ROOT / ".env"

CURRENT_SEASON = 2026

# ESPN's injuryStatus vocabulary is different from the official NFL
# practice-report vocabulary (GTD/Limited/Full don't apply here) —
# kept separate from any nflreadpy-based injury map used elsewhere.
# Verify against real ESPN output before trusting blindly; run:
#   python3 -c "... print distinct injuryStatus values ..."
# if new/unmapped statuses start showing up as unexpectedly Active.
ESPN_INJURY_STATUS_MAP = {
    "ACTIVE":       1.00,
    "NORMAL":       1.00,
    "PROBABLE":     0.92,
    "QUESTIONABLE": 0.55,
    "DOUBTFUL":     0.25,
    "GTD":          0.55,
    "OUT":          0.00,
    "IR":           0.00,
    "SUSPENSION":   0.00,
    "PUP":          0.00,
    "INACTIVE":     0.00,
}


def load_current_players(path: Path) -> list[dict]:
    content = path.read_text(encoding="utf-8")
    match = re.search(r"export const NFL_PLAYERS\s*=\s*(\[.*?\]);", content, re.DOTALL)
    if not match:
        log.error("Could not parse NFL_PLAYERS from nfl_data.js")
        sys.exit(1)
    return json.loads(match.group(1))


def load_espn_to_gsis_crosswalk() -> dict:
    """
    Reuse the existing ESPN ID -> GSIS ID crosswalk from id_mapping.js
    (built by build_id_mapping.py). Same approach as fetch_espn_aav.py —
    avoids re-deriving a crosswalk and avoids name-matching entirely.
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


def fetch_injury_report(season: int) -> dict:
    """
    Fetch current-season player injury/health status from ESPN Fantasy.
    Returns:
        {
            gsis_id: {
                "play_probability": float,
                "injury_detail": str
            }
        }
    """
    log.info(f"Fetching current {season} injury data from ESPN Fantasy API...")

    load_dotenv(ENV_PATH)
    espn_s2 = os.getenv("ESPN_S2")
    swid = os.getenv("SWID")
    if not espn_s2 or not swid:
        log.error(f"ESPN_S2 and SWID not found in {ENV_PATH}")
        sys.exit(1)

    url = (
        f"https://lm-api-reads.fantasy.espn.com/"
        f"apis/v3/games/ffl/seasons/{season}/"
        f"segments/0/leaguedefaults/3"
    )
    filter_payload = {
        "players": {
            "limit": 3000,
            "sortPercOwned": {"sortPriority": 4, "sortAsc": False},
        }
    }
    headers = {
        "Cookie": f"espn_s2={espn_s2.strip()}; SWID={swid.strip()}",
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "X-Fantasy-Filter": json.dumps(filter_payload),
    }
    try:
        response = requests.get(
            url,
            params={"view": "kona_player_info"},
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        log.error(f"ESPN injury fetch failed: {e}")
        sys.exit(1)
    except ValueError as e:
        log.error(f"ESPN returned invalid JSON: {e}")
        sys.exit(1)

    players = data.get("players", [])
    if not players:
        log.error("ESPN injury fetch returned zero players")
        sys.exit(1)
    log.info(f"ESPN player pool: {len(players)} players")

    espn_to_gsis = load_espn_to_gsis_crosswalk()

    result = {}
    flagged = 0
    unmatched = 0

    for entry in players:
        player = entry.get("player", entry)
        espn_id = player.get("id") or entry.get("id")
        if not espn_id:
            continue
        espn_id = str(espn_id)

        gsis_id = espn_to_gsis.get(espn_id)
        if not gsis_id:
            unmatched += 1
            continue

        status = str(player.get("injuryStatus") or "ACTIVE").strip().upper()
        play_prob = ESPN_INJURY_STATUS_MAP.get(status, 1.0)
        injury_detail = "Active" if play_prob == 1.0 else status.replace("_", " ").title()

        result[gsis_id] = {
            "play_probability": play_prob,
            "injury_detail": injury_detail,
        }
        if play_prob < 1.0:
            flagged += 1

    log.info(
        f"ESPN injury data: {len(result)} players mapped | "
        f"{flagged} currently flagged | "
        f"{unmatched} ESPN players without GSIS mapping"
    )
    return result


def apply_overlay(players: list[dict], injury_map: dict) -> tuple[list[dict], list[str]]:
    """
    Apply injury map to existing player records.
    Returns (updated_players, change_log).
    """
    changes = []
    updated = []
    for p in players:
        gsis = p["gsis_id"]
        inj  = injury_map.get(gsis)
        if inj:
            old_prob   = p.get("play_probability", 1.0)
            old_detail = p.get("injury_detail", "Active")
            new_prob   = inj["play_probability"]
            new_detail = inj["injury_detail"]
            if old_prob != new_prob or old_detail != new_detail:
                changes.append(
                    f"{p['name']} ({p['position']}, {p['team']}): "
                    f"play_prob {old_prob} → {new_prob} | {old_detail} → {new_detail}"
                )
                p = {**p, "play_probability": new_prob, "injury_detail": new_detail}
        else:
            if p.get("play_probability", 1.0) < 1.0:
                changes.append(
                    f"{p['name']} ({p['position']}, {p['team']}): "
                    f"cleared from injury report → Active (play_prob 1.0)"
                )
                p = {**p, "play_probability": 1.0, "injury_detail": "Active"}
        updated.append(p)
    return updated, changes


def patch_js_file(path: Path, players: list[dict], dry_run: bool, changes: list[str]):
    """Patch only the NFL_PLAYERS array in the existing JS file."""
    if not changes:
        log.info("No injury changes — nfl_data.js is already up to date")
        return

    log.info(f"{len(changes)} injury update(s):")
    for c in changes:
        log.info(f"  → {c}")

    if dry_run:
        log.info("DRY RUN — no file written")
        return

    content = path.read_text(encoding="utf-8")
    new_array = json.dumps(players, indent=2)
    patched = re.sub(
        r"(export const NFL_PLAYERS\s*=\s*)(\[.*?\]);",
        rf"\g<1>{new_array};",
        content,
        flags=re.DOTALL,
    )
    ts = datetime.now(timezone.utc).isoformat()
    patched = re.sub(
        r"// AUTO-GENERATED.*?\n",
        f"// AUTO-GENERATED by scripts/update_nfl_data.py — DO NOT EDIT MANUALLY\n"
        f"// Injury overlay applied (ESPN): {ts}\n",
        patched,
        count=1,
    )
    patched = re.sub(
        r"(player_count:\s*)\d+",
        f"\\g<1>{len(players)}",
        patched,
    )
    path.write_text(patched, encoding="utf-8")
    log.info(f"✓ Patched {path} with {len(changes)} injury update(s)")


def print_report(players: list[dict]):
    """Print a summary of all currently injured players."""
    flagged = [p for p in players if p.get("play_probability", 1.0) < 1.0]
    flagged.sort(key=lambda p: p["play_probability"])
    print(f"\n{'='*60}")
    print(f"  Injury Report — {len(flagged)} players flagged")
    print(f"{'='*60}")
    for p in flagged:
        prob = p["play_probability"]
        status_icon = "🔴" if prob == 0.0 else "🟡" if prob <= 0.55 else "🟠"
        print(
            f"  {status_icon} {p['name']:<22} {p['position']:<4} {p['team']:<4} "
            f"play_prob={prob:.2f}  {p['injury_detail']}"
        )
    print()


def main():
    parser = argparse.ArgumentParser(description="Apply ESPN injury overlay to nfl_data.js")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report",  action="store_true", help="Print injury summary and exit")
    parser.add_argument("--file",    type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()

    if not args.file.exists():
        log.error(f"nfl_data.js not found at {args.file} — run update_nfl_data.py first")
        sys.exit(1)

    players    = load_current_players(args.file)
    injury_map = fetch_injury_report(CURRENT_SEASON)
    updated, changes = apply_overlay(players, injury_map)

    if args.report:
        print_report(updated)
        return

    patch_js_file(args.file, updated, args.dry_run, changes)


if __name__ == "__main__":
    main()