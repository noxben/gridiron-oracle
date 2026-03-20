#!/usr/bin/env python3
"""
injury_overlay.py — Gridiron Oracle Injury Overlay
====================================================
Overlays the latest official NFL injury report on top of an existing
nfl_data.js file WITHOUT re-pulling all nflfastR data.

Use this for rapid Thursday / Sunday morning injury updates
when you don't need to refresh the full statistical pipeline.

Usage:
  python scripts/injury_overlay.py              # update existing nfl_data.js
  python scripts/injury_overlay.py --dry-run    # preview changes only
  python scripts/injury_overlay.py --report     # print injury report summary

Schedule:
  - Thursday morning (official report drops ~4pm ET Wednesday)
  - Sunday morning  (final injury designations ~11am ET)
  - Any time after a significant injury news breaks
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import nfl_data_py as nfl
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

ROOT        = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "src" / "utils" / "nfl_data.js"

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

DEFAULT_SEASON = 2024


def load_current_players(path: Path) -> list[dict]:
    content = path.read_text(encoding="utf-8")
    match = re.search(r"export const NFL_PLAYERS\s*=\s*(\[.*?\]);", content, re.DOTALL)
    if not match:
        log.error("Could not parse NFL_PLAYERS from nfl_data.js")
        sys.exit(1)
    return json.loads(match.group(1))


def fetch_latest_injuries() -> dict:
    """Returns { gsis_id: { play_probability, injury_detail } }"""
    log.info("Fetching latest injury report from nfl_data_py...")
    try:
        injuries = nfl.import_injuries(years=[DEFAULT_SEASON])
        result = {}
        for _, row in injuries.iterrows():
            gsis_id = row.get("gsis_id") or row.get("player_id")
            if not gsis_id:
                continue
            status  = str(row.get("report_status", "Active")).strip()
            primary = str(row.get("primary_injury", "")).strip()
            play_prob = INJURY_STATUS_MAP.get(status, 1.0)
            detail = f"{primary} — {status}" if primary and primary != "nan" else status
            result[str(gsis_id)] = {
                "play_probability": play_prob,
                "injury_detail":    detail,
            }
        log.info(f"Injury report: {len(result)} players flagged")
        return result
    except Exception as e:
        log.error(f"Injury fetch failed: {e}")
        sys.exit(1)


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
            # Not in injury report → set Active if they were previously flagged
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

    # Update timestamp comment
    ts = datetime.now(timezone.utc).isoformat()
    patched = re.sub(
        r"// AUTO-GENERATED.*?\n",
        f"// AUTO-GENERATED by scripts/update_nfl_data.py — DO NOT EDIT MANUALLY\n"
        f"// Injury overlay applied: {ts}\n",
        patched,
        count=1,
    )

    # Update player_count in META
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
    parser = argparse.ArgumentParser(description="Apply NFL injury overlay to nfl_data.js")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report",  action="store_true", help="Print injury summary and exit")
    parser.add_argument("--file",    type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()

    if not args.file.exists():
        log.error(f"nfl_data.js not found at {args.file} — run update_nfl_data.py first")
        sys.exit(1)

    players     = load_current_players(args.file)
    injury_map  = fetch_latest_injuries()
    updated, changes = apply_overlay(players, injury_map)

    if args.report:
        print_report(updated)
        return

    patch_js_file(args.file, updated, args.dry_run, changes)


if __name__ == "__main__":
    main()
