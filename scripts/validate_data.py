#!/usr/bin/env python3
"""
validate_data.py — Gridiron Oracle Data Validator
==================================================
Validates data files against locked schemas. MUST pass with zero errors
before any deploy.

Validates:
  nfl_data.js      — NFL player stats pipeline (spec §7.4)
  espn_league.js   — Full league data (spec v2.0 §4.1)  ← NEW in v2.0

Usage:
  python scripts/validate_data.py                           # validate nfl_data.js (default)
  python scripts/validate_data.py --file path/to/nfl_data.js
  python scripts/validate_data.py --league                  # validate espn_league.js
  python scripts/validate_data.py --league --file path/to/espn_league.js
  python scripts/validate_data.py --all                     # validate both files
  python scripts/validate_data.py --strict                  # fail on warnings too
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

ROOT             = Path(__file__).resolve().parent.parent
NFL_DATA_PATH    = ROOT / "src" / "utils" / "nfl_data.js"
LEAGUE_DATA_PATH = ROOT / "src" / "utils" / "espn_league.js"

# ── nfl_data.js schema ────────────────────────────────────────────────────────
# Required fields per spec §3.1 — unchanged from v1.0
REQUIRED_FIELDS = [
    "gsis_id", "name", "position", "team",
    "epa_per_play", "target_share", "carry_share", "snap_pct",
    "red_zone_share", "air_yards_share", "opp_def_rank",
    "play_probability", "injury_detail",
    "season_avg_pts", "last3_avg_pts",
]

VALID_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DST"}

# ── espn_league.js schema ─────────────────────────────────────────────────────
# Required top-level keys per spec v2.0 §4.1
LEAGUE_REQUIRED_KEYS = [
    "league_id", "season", "current_week", "fetched_at",
    "teams", "rosters", "matchups", "schedule",
    "waiver_pool", "faab_budgets", "standings", "transactions",
]

# Required fields on every rostered player
ROSTER_PLAYER_REQUIRED = [
    "espn_id", "name", "position", "team",
    "lineup_slot", "on_bench", "on_ir",
    "injury_status", "play_probability",
    "avg_points", "projected_points",
]

# Required fields on every waiver pool player
WAIVER_PLAYER_REQUIRED = [
    "espn_id", "name", "position",
    "avg_points", "projected_points",
    "percent_owned",
]

VALID_LINEUP_SLOTS  = {"QB", "RB", "WR", "TE", "FLEX", "K", "DST", "BENCH", "IR"}
EXPECTED_TEAM_COUNT = 12


# ==============================================================================
# nfl_data.js — existing validator, unchanged
# ==============================================================================

def extract_players_from_js(path: Path) -> list[dict]:
    """Extract the NFL_PLAYERS array from the generated JS file."""
    content = path.read_text(encoding="utf-8")
    match = re.search(r"export const NFL_PLAYERS\s*=\s*(\[.*?\]);", content, re.DOTALL)
    if not match:
        log.error("Could not find NFL_PLAYERS array in file")
        sys.exit(1)
    return json.loads(match.group(1))


def validate(players: list[dict], strict: bool = False) -> tuple[list[str], list[str]]:
    """
    Validate nfl_data.js player records.
    Returns (errors, warnings).
    """
    errors   = []
    warnings = []
    seen_ids = set()

    if not players:
        errors.append("NFL_PLAYERS array is empty")
        return errors, warnings

    if len(players) < 100:
        warnings.append(f"Only {len(players)} players — expected 200+ for a full season")

    for i, r in enumerate(players):
        ctx = f"[{i}] {r.get('name', '?')} ({r.get('gsis_id', 'NO_ID')})"

        for field in REQUIRED_FIELDS:
            if field not in r:
                errors.append(f"{ctx}: missing required field '{field}'")
            elif r[field] is None:
                errors.append(f"{ctx}: field '{field}' is null")

        gsis = r.get("gsis_id", "")
        if not gsis or not isinstance(gsis, str):
            errors.append(f"{ctx}: gsis_id is empty or not a string")
        elif gsis in seen_ids:
            errors.append(f"{ctx}: duplicate gsis_id '{gsis}'")
        seen_ids.add(gsis)

        pos = r.get("position", "")
        if pos not in VALID_POSITIONS:
            warnings.append(f"{ctx}: unexpected position '{pos}'")

        play_prob = r.get("play_probability", -1)
        if not (0.0 <= float(play_prob) <= 1.0):
            errors.append(f"{ctx}: play_probability={play_prob} out of range [0.0, 1.0]")

        opp_rank = r.get("opp_def_rank", 0)
        if not (1 <= int(opp_rank) <= 32):
            errors.append(f"{ctx}: opp_def_rank={opp_rank} out of range [1, 32]")

        snap_pct = r.get("snap_pct", -1)
        if not (0.0 <= float(snap_pct) <= 1.0):
            warnings.append(f"{ctx}: snap_pct={snap_pct} out of expected range [0.0, 1.0]")

        if pos in ("QB", "RB", "WR", "TE"):
            if r.get("epa_per_play") == 0.0 and r.get("season_avg_pts", 0) > 5:
                warnings.append(
                    f"{ctx}: epa_per_play=0.0 for player with {r.get('season_avg_pts')} avg pts — "
                    "may be missing EPA data"
                )
            if r.get("snap_pct", 0) == 0.0 and r.get("season_avg_pts", 0) > 5:
                warnings.append(f"{ctx}: snap_pct=0.0 for player with {r.get('season_avg_pts')} avg pts")

        if play_prob == 0.0 and r.get("season_avg_pts", 0) == 0.0:
            warnings.append(
                f"{ctx}: IR/Out player has season_avg_pts=0.0 — "
                "should store historical avg so replacement logic works"
            )

        comp = r.get("composite_rating", -1)
        if not (0.0 <= float(comp) <= 100.0):
            errors.append(f"{ctx}: composite_rating={comp} out of range [0, 100]")

    return errors, warnings


# ==============================================================================
# espn_league.js — new validator for v2.0
# ==============================================================================

def extract_league_from_js(path: Path) -> dict:
    """Extract the ESPN_LEAGUE_DATA object from espn_league.js."""
    content = path.read_text(encoding="utf-8")
    match = re.search(r"export const ESPN_LEAGUE_DATA\s*=\s*(\{.*?\});\s*\n", content, re.DOTALL)
    if not match:
        log.error("Could not find ESPN_LEAGUE_DATA object in file")
        log.error("Run scripts/fetch_espn_league.py first")
        sys.exit(1)
    return json.loads(match.group(1))


def validate_league(data: dict, strict: bool = False) -> tuple[list[str], list[str]]:
    """
    Validate espn_league.js against the locked schema (spec v2.0 §4.1).
    Returns (errors, warnings).
    """
    errors   = []
    warnings = []

    # ── Top-level structure ───────────────────────────────────────────────────
    for key in LEAGUE_REQUIRED_KEYS:
        if key not in data:
            errors.append(f"Missing required top-level key: '{key}'")

    if errors:
        # Structural failure — remaining checks will crash, stop here
        return errors, warnings

    # ── League metadata ───────────────────────────────────────────────────────
    if data["league_id"] != 839979:
        errors.append(f"league_id={data['league_id']} — expected 839979")

    if not isinstance(data["current_week"], int) or data["current_week"] < 0:
        errors.append(f"current_week={data['current_week']} — expected non-negative int")

    if not data["fetched_at"]:
        errors.append("fetched_at is empty")

    # ── Teams ─────────────────────────────────────────────────────────────────
    teams = data["teams"]
    if len(teams) != EXPECTED_TEAM_COUNT:
        errors.append(f"Expected {EXPECTED_TEAM_COUNT} teams, got {len(teams)}")

    team_ids = set()
    for i, team in enumerate(teams):
        ctx = f"teams[{i}] '{team.get('team_name', '?')}'"
        for field in ("team_id", "team_name", "wins", "losses", "points_for", "points_against"):
            if field not in team:
                errors.append(f"{ctx}: missing field '{field}'")
        tid = team.get("team_id")
        if tid in team_ids:
            errors.append(f"{ctx}: duplicate team_id {tid}")
        team_ids.add(tid)

    # ── Rosters ───────────────────────────────────────────────────────────────
    rosters = data["rosters"]
    if not isinstance(rosters, dict):
        errors.append("rosters must be a dict keyed by team_id string")
    else:
        roster_team_ids = set(rosters.keys())
        expected_ids    = {str(tid) for tid in team_ids}
        missing_rosters = expected_ids - roster_team_ids
        if missing_rosters:
            errors.append(f"Missing rosters for team_ids: {sorted(missing_rosters)}")

        total_slots = 0
        for team_id_str, roster in rosters.items():
            ctx_team = f"rosters[{team_id_str}]"
            if len(roster) < 10:
                errors.append(f"{ctx_team}: only {len(roster)} slots (expected ≥10)")

            seen_espn_ids = set()
            for j, player in enumerate(roster):
                ctx = f"{ctx_team}[{j}] '{player.get('name', '?')}'"

                for field in ROSTER_PLAYER_REQUIRED:
                    if field not in player:
                        errors.append(f"{ctx}: missing field '{field}'")

                espn_id = player.get("espn_id", "")
                if not espn_id or not isinstance(espn_id, str):
                    errors.append(f"{ctx}: espn_id is empty or not a string")
                elif espn_id in seen_espn_ids:
                    errors.append(f"{ctx}: duplicate espn_id '{espn_id}' within same roster")
                seen_espn_ids.add(espn_id)

                slot = player.get("lineup_slot", "")
                if slot not in VALID_LINEUP_SLOTS:
                    warnings.append(f"{ctx}: unexpected lineup_slot '{slot}'")

                play_prob = player.get("play_probability", -1)
                try:
                    if not (0.0 <= float(play_prob) <= 1.0):
                        errors.append(f"{ctx}: play_probability={play_prob} out of range [0.0, 1.0]")
                except (TypeError, ValueError):
                    errors.append(f"{ctx}: play_probability='{play_prob}' is not a number")

                pos = player.get("position", "")
                if pos not in VALID_POSITIONS:
                    warnings.append(f"{ctx}: unexpected position '{pos}'")

            total_slots += len(roster)

        if total_slots < EXPECTED_TEAM_COUNT * 10:
            warnings.append(f"Only {total_slots} total roster slots — expected ≥{EXPECTED_TEAM_COUNT * 10}")

    # ── Matchups ──────────────────────────────────────────────────────────────
    matchups = data["matchups"]
    if not matchups:
        # Offseason — warn only, not an error
        warnings.append("matchups list is empty (offseason or week not set yet)")
    else:
        expected_matchups = EXPECTED_TEAM_COUNT // 2
        if len(matchups) != expected_matchups:
            warnings.append(f"Expected {expected_matchups} matchups, got {len(matchups)}")

        matchup_team_ids = set()
        for i, m in enumerate(matchups):
            ctx = f"matchups[{i}]"
            for field in ("week", "home_team_id", "away_team_id"):
                if field not in m:
                    errors.append(f"{ctx}: missing field '{field}'")
            home_id = m.get("home_team_id")
            away_id = m.get("away_team_id")
            if home_id in matchup_team_ids:
                errors.append(f"{ctx}: team {home_id} appears in multiple matchups this week")
            if away_id in matchup_team_ids:
                errors.append(f"{ctx}: team {away_id} appears in multiple matchups this week")
            matchup_team_ids.update([home_id, away_id])

    # ── Schedule ──────────────────────────────────────────────────────────────
    schedule = data["schedule"]
    if not schedule:
        warnings.append("schedule list is empty")
    else:
        weeks_seen = {e.get("week") for e in schedule}
        if len(weeks_seen) < 1:
            warnings.append("schedule contains entries but no week values")

    # ── Waiver pool ───────────────────────────────────────────────────────────
    waiver_pool = data["waiver_pool"]
    if len(waiver_pool) < 50:
        warnings.append(f"Only {len(waiver_pool)} waiver pool players — expected 50+ (offseason?)")

    seen_waiver_ids = set()
    for j, player in enumerate(waiver_pool):
        ctx = f"waiver_pool[{j}] '{player.get('name', '?')}'"
        for field in WAIVER_PLAYER_REQUIRED:
            if field not in player:
                errors.append(f"{ctx}: missing field '{field}'")
        espn_id = player.get("espn_id", "")
        if espn_id and espn_id in seen_waiver_ids:
            errors.append(f"{ctx}: duplicate espn_id '{espn_id}' in waiver pool")
        seen_waiver_ids.add(espn_id)

    # Sanity: no rostered player should appear in waiver pool
    if isinstance(rosters, dict):
        rostered_ids = {p["espn_id"] for r in rosters.values() for p in r if p.get("espn_id")}
        crossover = rostered_ids & seen_waiver_ids
        if crossover:
            errors.append(f"{len(crossover)} player(s) appear in both a roster and waiver pool: "
                          f"{list(crossover)[:3]}{'...' if len(crossover) > 3 else ''}")

    # ── FAAB budgets ──────────────────────────────────────────────────────────
    faab = data["faab_budgets"]
    if not faab:
        warnings.append("faab_budgets is empty")
    else:
        if len(faab) != EXPECTED_TEAM_COUNT:
            warnings.append(f"faab_budgets has {len(faab)} entries, expected {EXPECTED_TEAM_COUNT}")
        for team_id_str, budget in faab.items():
            if budget < 0:
                errors.append(f"faab_budgets[{team_id_str}]={budget} — cannot be negative")

    # ── Standings ─────────────────────────────────────────────────────────────
    standings = data["standings"]
    if len(standings) != EXPECTED_TEAM_COUNT:
        errors.append(f"standings has {len(standings)} entries, expected {EXPECTED_TEAM_COUNT}")
    else:
        seeds = [s.get("seed") for s in standings]
        if sorted(seeds) != list(range(1, EXPECTED_TEAM_COUNT + 1)):
            errors.append(f"standings seeds are not 1–{EXPECTED_TEAM_COUNT}: {seeds}")

        standing_team_ids = {s.get("team_id") for s in standings}
        if standing_team_ids != team_ids:
            missing = team_ids - standing_team_ids
            extra   = standing_team_ids - team_ids
            if missing:
                errors.append(f"standings missing team_ids: {missing}")
            if extra:
                errors.append(f"standings has unknown team_ids: {extra}")

    return errors, warnings


# ==============================================================================
# main
# ==============================================================================

def run_nfl_validation(file_path: Path, strict: bool) -> bool:
    """Validate nfl_data.js. Returns True if passed."""
    if not file_path.exists():
        log.error(f"File not found: {file_path}")
        log.error("Run scripts/update_nfl_data.py first")
        return False

    log.info(f"--- Validating nfl_data.js: {file_path}")
    players = extract_players_from_js(file_path)
    log.info(f"Loaded {len(players)} player records")

    errors, warnings = validate(players, strict=strict)

    if warnings:
        log.warning(f"{len(warnings)} warning(s):")
        for w in warnings:
            log.warning(f"  ⚠ {w}")
    if errors:
        log.error(f"VALIDATION FAILED — {len(errors)} error(s):")
        for e in errors:
            log.error(f"  ✗ {e}")
        return False
    if strict and warnings:
        log.error("Strict mode: warnings treated as errors")
        return False

    log.info(f"✓ nfl_data.js passed — {len(players)} players, 0 errors, {len(warnings)} warnings")
    return True


def run_league_validation(file_path: Path, strict: bool) -> bool:
    """Validate espn_league.js. Returns True if passed."""
    if not file_path.exists():
        log.error(f"File not found: {file_path}")
        log.error("Run scripts/fetch_espn_league.py first")
        return False

    log.info(f"--- Validating espn_league.js: {file_path}")
    data = extract_league_from_js(file_path)
    log.info(f"Loaded league data — week {data.get('current_week')}, "
             f"{len(data.get('teams', []))} teams, "
             f"{sum(len(v) for v in data.get('rosters', {}).values())} roster slots, "
             f"{len(data.get('waiver_pool', []))} waiver players")

    errors, warnings = validate_league(data, strict=strict)

    if warnings:
        log.warning(f"{len(warnings)} warning(s):")
        for w in warnings:
            log.warning(f"  ⚠ {w}")
    if errors:
        log.error(f"VALIDATION FAILED — {len(errors)} error(s):")
        for e in errors:
            log.error(f"  ✗ {e}")
        return False
    if strict and warnings:
        log.error("Strict mode: warnings treated as errors")
        return False

    log.info(f"✓ espn_league.js passed — 0 errors, {len(warnings)} warnings")
    return True


def main():
    parser = argparse.ArgumentParser(description="Validate Gridiron Oracle data files")
    parser.add_argument("--file",   type=Path, default=None,
                        help="Override file path (used with --league or default nfl mode)")
    parser.add_argument("--league", action="store_true",
                        help="Validate espn_league.js instead of nfl_data.js")
    parser.add_argument("--all",    action="store_true",
                        help="Validate both nfl_data.js and espn_league.js")
    parser.add_argument("--strict", action="store_true",
                        help="Treat warnings as errors")
    args = parser.parse_args()

    passed = True

    if args.all:
        nfl_path    = args.file or NFL_DATA_PATH
        league_path = LEAGUE_DATA_PATH
        passed = run_nfl_validation(nfl_path, args.strict) and passed
        passed = run_league_validation(league_path, args.strict) and passed

    elif args.league:
        league_path = args.file or LEAGUE_DATA_PATH
        passed = run_league_validation(league_path, args.strict)

    else:
        nfl_path = args.file or NFL_DATA_PATH
        passed = run_nfl_validation(nfl_path, args.strict)

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
