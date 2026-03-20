#!/usr/bin/env python3
"""
build_id_mapping.py — Gridiron Oracle ID Mapping Builder
=========================================================
Builds src/utils/id_mapping.js — a lookup table mapping ESPN player IDs
to GSIS IDs (nflfastR primary keys).

This is the bridge between ESPN's roster import (Step 3) and nflfastR
data (Step 1). It must be run ONCE before Step 3, and re-run at the
start of each new season to pick up new players.

Per spec §4.2:
  - Never join ESPN data to nflfastR data by name string
  - Always join by ID
  - Flag unmatched players explicitly — handle with manual override fallback

Usage:
  python scripts/build_id_mapping.py              # build for current season
  python scripts/build_id_mapping.py --season 2024
  python scripts/build_id_mapping.py --dry-run    # preview, don't write file
  python scripts/build_id_mapping.py --report     # print coverage stats

Requirements:
  pip install nfl_data_py pandas
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import nfl_data_py as nfl
import pandas as pd

ROOT        = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "src" / "utils" / "id_mapping.js"

DEFAULT_SEASON = 2024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "K"}


def fetch_id_table(season: int) -> pd.DataFrame:
    """
    Pull the ESPN ID → GSIS ID mapping table from nfl_data_py.
    import_ids() returns a DataFrame with espn_id, gsis_id, name, position, team.
    Covers ~98% of rostered players per spec §4.2.
    """
    log.info(f"Fetching ID mapping table from nfl_data_py (season={season})...")

    try:
        ids = nfl.import_ids()

        # Columns we need — nfl_data_py column names vary slightly by version
        # Try both known variants
        espn_col = "espn_id" if "espn_id" in ids.columns else "fantasy_data_id"
        gsis_col = "gsis_id" if "gsis_id" in ids.columns else "gsis_it_id"

        if gsis_col not in ids.columns:
            log.error(f"GSIS ID column not found. Available: {list(ids.columns)}")
            sys.exit(1)

        df = ids[[espn_col, gsis_col, "name", "position", "team"]].copy()
        df.columns = ["espn_id", "gsis_id", "name", "position", "team"]

        # Drop rows missing either key
        before = len(df)
        df = df.dropna(subset=["espn_id", "gsis_id"])
        df = df[df["espn_id"] != ""]
        df = df[df["gsis_id"] != ""]
        after = len(df)

        log.info(f"Raw table: {before} rows → {after} rows after dropping null IDs")

        # Normalize types
        df["espn_id"] = df["espn_id"].astype(str).str.strip().str.split(".").str[0]
        df["gsis_id"] = df["gsis_id"].astype(str).str.strip()
        df["position"] = df["position"].fillna("").str.upper()
        df["team"]     = df["team"].fillna("").str.upper()
        df["name"]     = df["name"].fillna("").str.strip()

        # Filter to skill positions only — we don't need kickers on IR from 2015
        df = df[df["position"].isin(SKILL_POSITIONS) | (df["position"] == "")]

        return df

    except Exception as e:
        log.error(f"Failed to fetch ID table: {e}")
        sys.exit(1)


def build_lookup(df: pd.DataFrame) -> tuple[dict, dict]:
    """
    Build two lookup dicts from the DataFrame:
      espn_to_gsis: { espn_id_string → gsis_id }
      gsis_to_espn: { gsis_id → espn_id_string }
    """
    espn_to_gsis = {}
    gsis_to_espn = {}
    duplicates   = []

    for _, row in df.iterrows():
        espn = str(row["espn_id"])
        gsis = str(row["gsis_id"])

        if espn in espn_to_gsis and espn_to_gsis[espn] != gsis:
            duplicates.append(f"ESPN {espn} maps to multiple GSIS IDs: {espn_to_gsis[espn]} and {gsis}")

        espn_to_gsis[espn] = gsis
        gsis_to_espn[gsis] = espn

    if duplicates:
        log.warning(f"{len(duplicates)} duplicate ESPN ID mappings (keeping last):")
        for d in duplicates[:10]:
            log.warning(f"  ⚠ {d}")

    return espn_to_gsis, gsis_to_espn


def build_player_index(df: pd.DataFrame) -> dict:
    """
    Build a player metadata index keyed by GSIS ID.
    Used by the app to show name/position/team for unmatched players.
    { gsis_id: { name, position, team, espn_id } }
    """
    index = {}
    for _, row in df.iterrows():
        gsis = str(row["gsis_id"])
        index[gsis] = {
            "name":     row["name"],
            "position": row["position"],
            "team":     row["team"],
            "espn_id":  str(row["espn_id"]),
        }
    return index


def coverage_report(df: pd.DataFrame):
    """Print coverage stats by position."""
    print(f"\n{'='*55}")
    print(f"  ID Mapping Coverage Report")
    print(f"{'='*55}")
    print(f"  Total mapped players:  {len(df)}")
    print()
    for pos in ["QB", "RB", "WR", "TE", "K"]:
        count = len(df[df["position"] == pos])
        print(f"  {pos:<6} {count:>4} players mapped")
    unmapped = len(df[~df["position"].isin(SKILL_POSITIONS)])
    print(f"  Other  {unmapped:>4} players mapped")
    print(f"{'='*55}\n")


def write_output(
    espn_to_gsis: dict,
    gsis_to_espn: dict,
    player_index: dict,
    season: int,
    dry_run: bool,
):
    timestamp = datetime.now(timezone.utc).isoformat()

    js_content = f"""// id_mapping.js — Gridiron Oracle ESPN → GSIS ID lookup
// AUTO-GENERATED by scripts/build_id_mapping.py — DO NOT EDIT MANUALLY
// Season: {season} | Generated: {timestamp}
// Coverage: {len(espn_to_gsis)} ESPN IDs mapped to GSIS IDs
//
// Per spec §4.2: NEVER join ESPN data to nflfastR data by name string.
// Always use these ID lookups. Unmatched players → UNMATCHED_PLAYERS fallback.

// ESPN player ID → GSIS ID (primary lookup used by espn_api.js)
export const ESPN_TO_GSIS = {json.dumps(espn_to_gsis, indent=2)};

// GSIS ID → ESPN player ID (reverse lookup)
export const GSIS_TO_ESPN = {json.dumps(gsis_to_espn, indent=2)};

// Player metadata indexed by GSIS ID (name, position, team, espn_id)
// Used to display info for players not yet in nfl_data.js (new callups, etc.)
export const PLAYER_INDEX = {json.dumps(player_index, indent=2)};

/**
 * Look up GSIS ID from ESPN player ID.
 * Returns null if not found — caller must handle the unmatched case.
 *
 * @param {{string|number}} espnId
 * @returns {{string|null}} gsis_id or null
 */
export function espnToGsis(espnId) {{
  return ESPN_TO_GSIS[String(espnId)] ?? null;
}}

/**
 * Look up ESPN ID from GSIS ID.
 * @param {{string}} gsisId
 * @returns {{string|null}}
 */
export function gsisToEspn(gsisId) {{
  return GSIS_TO_ESPN[gsisId] ?? null;
}}

/**
 * Get player metadata by GSIS ID.
 * @param {{string}} gsisId
 * @returns {{{{name, position, team, espn_id}}|null}}
 */
export function getPlayerMeta(gsisId) {{
  return PLAYER_INDEX[gsisId] ?? null;
}}

/**
 * Batch convert an array of ESPN IDs to GSIS IDs.
 * Returns {{ matched: [{{espnId, gsisId}}], unmatched: [espnId] }}
 *
 * Unmatched players are new callups or edge cases (~2% per spec §4.2).
 * The caller (RosterSetup.js) must surface these for manual assignment.
 *
 * @param {{Array<string|number>}} espnIds
 * @returns {{{{ matched: Array, unmatched: Array }}}}
 */
export function batchEspnToGsis(espnIds) {{
  const matched   = [];
  const unmatched = [];

  for (const espnId of espnIds) {{
    const gsisId = espnToGsis(espnId);
    if (gsisId) {{
      matched.push({{ espnId: String(espnId), gsisId }});
    }} else {{
      unmatched.push(String(espnId));
    }}
  }}

  return {{ matched, unmatched }};
}}

export const ID_MAPPING_META = {{
  season: {season},
  generated_at: "{timestamp}",
  total_mapped: {len(espn_to_gsis)},
}};
"""

    if dry_run:
        log.info(f"DRY RUN — would write {len(espn_to_gsis)} mappings to {OUTPUT_PATH}")
        log.info(f"Sample espn→gsis: { {k: v for k, v in list(espn_to_gsis.items())[:3]} }")
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(js_content, encoding="utf-8")
    log.info(f"✓ Wrote {len(espn_to_gsis)} ID mappings to {OUTPUT_PATH}")


def main():
    parser = argparse.ArgumentParser(description="Build ESPN → GSIS ID mapping")
    parser.add_argument("--season",  type=int, default=DEFAULT_SEASON)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report",  action="store_true", help="Print coverage stats only")
    args = parser.parse_args()

    log.info(f"=== Building ID mapping table (season={args.season}) ===")

    df = fetch_id_table(args.season)

    if args.report:
        coverage_report(df)
        return

    espn_to_gsis, gsis_to_espn = build_lookup(df)
    player_index                = build_player_index(df)

    coverage_report(df)

    write_output(espn_to_gsis, gsis_to_espn, player_index, args.season, args.dry_run)

    log.info("=== ID mapping complete ===")
    log.info(f"Step 2 gate: {len(espn_to_gsis)} ESPN IDs mapped — run Step 3 (espn_api.js) next")


if __name__ == "__main__":
    main()
