#!/usr/bin/env python3
"""
validate_data.py — Gridiron Oracle Data Validator
==================================================
Validates nfl_data.js against the locked schema (spec §7.4).
MUST pass with zero errors before any deploy.

Usage:
  python scripts/validate_data.py                         # validate default output path
  python scripts/validate_data.py --file path/to/nfl_data.js
  python scripts/validate_data.py --strict                # fail on warnings too
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

ROOT        = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "src" / "utils" / "nfl_data.js"

# Required fields per spec §3.1
REQUIRED_FIELDS = [
    "gsis_id", "name", "position", "team",
    "epa_per_play", "target_share", "carry_share", "snap_pct",
    "red_zone_share", "air_yards_share", "opp_def_rank",
    "play_probability", "injury_detail",
    "season_avg_pts", "last3_avg_pts",
]

VALID_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DST"}


def extract_players_from_js(path: Path) -> list[dict]:
    """Extract the NFL_PLAYERS array from the generated JS file."""
    content = path.read_text(encoding="utf-8")
    # Extract the JSON array between NFL_PLAYERS = [...];
    match = re.search(r"export const NFL_PLAYERS\s*=\s*(\[.*?\]);", content, re.DOTALL)
    if not match:
        log.error("Could not find NFL_PLAYERS array in file")
        sys.exit(1)
    return json.loads(match.group(1))


def validate(players: list[dict], strict: bool = False) -> tuple[list[str], list[str]]:
    """
    Run all validation checks.
    Returns (errors, warnings).
    """
    errors   = []
    warnings = []
    seen_ids = set()

    # File-level checks
    if not players:
        errors.append("NFL_PLAYERS array is empty")
        return errors, warnings

    if len(players) < 100:
        warnings.append(f"Only {len(players)} players — expected 200+ for a full season")

    for i, r in enumerate(players):
        ctx = f"[{i}] {r.get('name', '?')} ({r.get('gsis_id', 'NO_ID')})"

        # ── Required fields ──────────────────────────────────────────────
        for field in REQUIRED_FIELDS:
            if field not in r:
                errors.append(f"{ctx}: missing required field '{field}'")
            elif r[field] is None:
                errors.append(f"{ctx}: field '{field}' is null")

        # ── GSIS ID ───────────────────────────────────────────────────────
        gsis = r.get("gsis_id", "")
        if not gsis or not isinstance(gsis, str):
            errors.append(f"{ctx}: gsis_id is empty or not a string")
        elif gsis in seen_ids:
            errors.append(f"{ctx}: duplicate gsis_id '{gsis}'")
        seen_ids.add(gsis)

        # ── Position ──────────────────────────────────────────────────────
        pos = r.get("position", "")
        if pos not in VALID_POSITIONS:
            warnings.append(f"{ctx}: unexpected position '{pos}'")

        # ── Numeric range checks ──────────────────────────────────────────
        play_prob = r.get("play_probability", -1)
        if not (0.0 <= float(play_prob) <= 1.0):
            errors.append(f"{ctx}: play_probability={play_prob} out of range [0.0, 1.0]")

        opp_rank = r.get("opp_def_rank", 0)
        if not (1 <= int(opp_rank) <= 32):
            errors.append(f"{ctx}: opp_def_rank={opp_rank} out of range [1, 32]")

        snap_pct = r.get("snap_pct", -1)
        if not (0.0 <= float(snap_pct) <= 1.0):
            warnings.append(f"{ctx}: snap_pct={snap_pct} out of expected range [0.0, 1.0]")

        # ── Starters must have EPA and snap data ─────────────────────────
        if pos in ("QB", "RB", "WR", "TE"):
            if r.get("epa_per_play") == 0.0 and r.get("season_avg_pts", 0) > 5:
                warnings.append(
                    f"{ctx}: epa_per_play=0.0 for player with {r.get('season_avg_pts')} avg pts — "
                    "may be missing EPA data"
                )
            if r.get("snap_pct", 0) == 0.0 and r.get("season_avg_pts", 0) > 5:
                warnings.append(
                    f"{ctx}: snap_pct=0.0 for player with {r.get('season_avg_pts')} avg pts"
                )

        # ── Inactive players should have replacement-level pts, not zero ─
        if play_prob == 0.0 and r.get("season_avg_pts", 0) == 0.0:
            warnings.append(
                f"{ctx}: IR/Out player has season_avg_pts=0.0 — "
                "should store historical avg so replacement logic works"
            )

        # ── Composite rating in expected range ───────────────────────────
        comp = r.get("composite_rating", -1)
        if not (0.0 <= float(comp) <= 100.0):
            errors.append(f"{ctx}: composite_rating={comp} out of range [0, 100]")

    return errors, warnings


def main():
    parser = argparse.ArgumentParser(description="Validate nfl_data.js")
    parser.add_argument("--file",   type=Path, default=OUTPUT_PATH)
    parser.add_argument("--strict", action="store_true",
                        help="Treat warnings as errors")
    args = parser.parse_args()

    if not args.file.exists():
        log.error(f"File not found: {args.file}")
        log.error("Run scripts/update_nfl_data.py first")
        sys.exit(1)

    log.info(f"Validating {args.file}")
    players = extract_players_from_js(args.file)
    log.info(f"Loaded {len(players)} player records")

    errors, warnings = validate(players, strict=args.strict)

    if warnings:
        log.warning(f"{len(warnings)} warning(s):")
        for w in warnings:
            log.warning(f"  ⚠ {w}")

    if errors:
        log.error(f"VALIDATION FAILED — {len(errors)} error(s):")
        for e in errors:
            log.error(f"  ✗ {e}")
        sys.exit(1)

    if args.strict and warnings:
        log.error("Strict mode: warnings treated as errors")
        sys.exit(1)

    log.info(f"✓ Validation passed — {len(players)} players, 0 errors, {len(warnings)} warnings")
    sys.exit(0)


if __name__ == "__main__":
    main()
