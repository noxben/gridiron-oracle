#!/usr/bin/env python3
"""
build_id_mapping.py — Gridiron Oracle ID Mapping Builder
=========================================================
Builds src/utils/id_mapping.js — a lookup table mapping ESPN player IDs
to GSIS IDs (nflfastR primary keys).

This is the bridge between ESPN's roster import (Step 3) and player stats
data (Step 1). It must be run ONCE before Step 3, and re-run at the start
of each new season to pick up new players.

MIGRATION NOTE (2026-08): Previously used nfl_data_py.import_ids(), which
is dead — nfl_data_py was archived by its maintainer in September 2025.
Rewritten to use nflreadpy's load_ff_playerids(), the actively-maintained
official nflverse equivalent. This table is actually a better fit for our
purposes than the old one — it's explicitly a fantasy-focused ID crosswalk
(includes sleeper_id, yahoo_id, etc. alongside espn_id/gsis_id/pfr_id).

Per spec §4.2:
  - Never join ESPN data to nflfastR data by name string
  - Always join by ID
  - Flag unmatched players explicitly — handle with manual override fallback

Usage:
  python scripts/build_id_mapping.py              # build for current season
  python scripts/build_id_mapping.py --season 2026
  python scripts/build_id_mapping.py --dry-run    # preview, don't write file
  python scripts/build_id_mapping.py --report     # print coverage stats

Requirements:
  pip install nflreadpy pandas pyarrow
"""
import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import nflreadpy as nfl
import pandas as pd

ROOT        = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "src" / "utils" / "id_mapping.js"

DEFAULT_SEASON = 2026

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "K"}


def fetch_id_table(season: int) -> pd.DataFrame:
    """
    Pull the ESPN ID -> GSIS ID mapping table via nflreadpy.
    load_ff_playerids() returns a DataFrame with espn_id, gsis_id, name,
    position, team, plus other platform IDs we don't currently use.

    NOTE (carried over from the pre-migration version): this table is not
    filtered by season internally — like the old nfl_data_py equivalent,
    it's a maintained crosswalk of known players rather than a per-season
    snapshot. The `season` param here only labels the output metadata.
    Whether current-year rookies appear depends on whether nflverse's
    crosswalk has been updated to include them yet — verify with
    --report before trusting coverage close to a draft.
    """
    log.info(f"Fetching ID mapping table via nflreadpy (season={season})...")
    try:
        ids = nfl.load_ff_playerids().to_pandas()

        if "gsis_id" not in ids.columns or "espn_id" not in ids.columns:
            log.error(f"Expected columns not found. Available: {list(ids.columns)}")
            sys.exit(1)

        df = ids[["espn_id", "gsis_id", "name", "position", "team"]].copy()

        before = len(df)
        df = df.dropna(subset=["espn_id", "gsis_id"])
        df = df[df["espn_id"] != ""]
        df = df[df["gsis_id"] != ""]
        after = len(df)
        log.info(f"Raw table: {before} rows -> {after} rows after dropping null IDs")

        df["espn_id"] = df["espn_id"].astype(str).str.strip().str.split(".").str[0]
        df["gsis_id"] = df["gsis_id"].astype(str).str.strip()
        df["position"] = df["position"].fillna("").str.upper()
        df["team"]     = df["team"].fillna("").str.upper()
        df["name"]     = df["name"].fillna("").str.strip()

        df = df[df["position"].isin(SKILL_POSITIONS) | (df["position"] == "")]

        return df
    except Exception as e:
        log.error(f"Failed to fetch ID table: {e}")
        sys.exit(1)


def build_lookup(df: pd.DataFrame) -> tuple[dict, dict]:
    """
    Build two lookup dicts from the DataFrame:
      espn_to_gsis: { espn_id_string -> gsis_id }
      gsis_to_espn: { gsis_id -> espn_id_string }
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

    js_content = f"""// id_mapping.js — Gridiron Oracle ESPN -> GSIS ID lookup
// AUTO-GENERATED by scripts/build_id_mapping.py — DO NOT EDIT MANUALLY
// Season: {season} | Generated: {timestamp}
// Coverage: {len(espn_to_gsis)} ESPN IDs mapped to GSIS IDs
// Data source: nflreadpy (migrated from archived nfl_data_py, 2026-08)
//
// Per spec §4.2: NEVER join ESPN data to nflfastR data by name string.
// Always use these ID lookups. Unmatched players -> UNMATCHED_PLAYERS fallback.

export const ESPN_TO_GSIS = {json.dumps(espn_to_gsis, indent=2)};

export const GSIS_TO_ESPN = {json.dumps(gsis_to_espn, indent=2)};

export const PLAYER_INDEX = {json.dumps(player_index, indent=2)};

export function espnToGsis(espnId) {{
  return ESPN_TO_GSIS[String(espnId)] ?? null;
}}

export function gsisToEspn(gsisId) {{
  return GSIS_TO_ESPN[gsisId] ?? null;
}}

export function getPlayerMeta(gsisId) {{
  return PLAYER_INDEX[gsisId] ?? null;
}}

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
        sample = {k: v for k, v in list(espn_to_gsis.items())[:3]}
        log.info(f"Sample espn->gsis: {sample}")
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(js_content, encoding="utf-8")
    log.info(f"✓ Wrote {len(espn_to_gsis)} ID mappings to {OUTPUT_PATH}")


def main():
    parser = argparse.ArgumentParser(description="Build ESPN -> GSIS ID mapping")
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