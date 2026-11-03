#!/usr/bin/env python3
"""
validate_data.py — Gridiron Oracle Data Validator
==================================================
Validates generated data files against the current locked schemas.

Usage:
  python scripts/validate_data.py
  python scripts/validate_data.py --file path/to/nfl_data.js
  python scripts/validate_data.py --league
  python scripts/validate_data.py --all
  python scripts/validate_data.py --strict
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

ROOT = Path(__file__).resolve().parent.parent
NFL_DATA_PATH = ROOT / "src" / "utils" / "nfl_data.js"
LEAGUE_DATA_PATH = ROOT / "src" / "utils" / "espn_league.js"


# ==============================================================================
# nfl_data.js schema
# ==============================================================================

REQUIRED_FIELDS = [
    "gsis_id",
    "name",
    "position",
    "team",
    "roster_status",
    "epa_per_play",
    "target_share",
    "carry_share",
    "snap_pct",
    "red_zone_share",
    "air_yards_share",
    "team_off_epa",
    "team_off_success_rate",
    "team_plays",
    "team_def_epa",
    "team_def_success_rate",
    "opp_def_epa",
    "opp_def_success_rate",
    "play_probability",
    "injury_detail",
    "season_avg_pts",
    "last3_avg_pts",
    "season_rush_yards",
    "season_rush_tds",
    "season_rec_tds",
    "season_pass_tds",
    "composite_rating",
    "epa_score",
    "usage_score",
    "snap_score",
    "red_zone_score",
    "conf",
    "opp_pass_def_epa",
    "opp_pass_def_success_rate",
    "opp_rush_def_epa",
    "opp_rush_def_success_rate",
    "opp_defense_score",
    "opp_pass_defense_score",
    "opp_rush_defense_score",
    "matchup_score",
    "team_environment_score",
    "historical_team",
]

VALID_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DST"}

RATING_FIELDS = [
    "composite_rating",
    "epa_score",
    "usage_score",
    "snap_score",
    "red_zone_score",
    "opp_defense_score",
    "opp_pass_defense_score",
    "opp_rush_defense_score",
    "matchup_score",
    "team_environment_score",
]

SHARE_FIELDS = [
    "target_share",
    "carry_share",
    "snap_pct",
    "red_zone_share",
]

TEAM_SUCCESS_FIELDS = [
    "team_off_success_rate",
    "team_def_success_rate",
    "opp_def_success_rate",
    "opp_pass_def_success_rate",
    "opp_rush_def_success_rate",
]

NON_NEGATIVE_FIELDS = [
    "season_rush_tds",
    "season_rec_tds",
    "season_pass_tds",
    "team_plays",
]

# ==============================================================================
# espn_league.js schema
# ==============================================================================

LEAGUE_REQUIRED_KEYS = [
    "league_id",
    "season",
    "current_week",
    "fetched_at",
    "teams",
    "rosters",
    "matchups",
    "schedule",
    "waiver_pool",
    "faab_budgets",
    "standings",
    "transactions",
]

ROSTER_PLAYER_REQUIRED = [
    "espn_id",
    "name",
    "position",
    "team",
    "lineup_slot",
    "on_bench",
    "on_ir",
    "injury_status",
    "play_probability",
    "avg_points",
    "projected_points",
]

WAIVER_PLAYER_REQUIRED = [
    "espn_id",
    "name",
    "position",
    "avg_points",
    "projected_points",
    "percent_owned",
]

VALID_LINEUP_SLOTS = {
    "QB", "RB", "WR", "TE", "FLEX", "K", "DST", "BENCH", "IR"
}

EXPECTED_TEAM_COUNT = 12


# ==============================================================================
# nfl_data.js extraction
# ==============================================================================

def extract_nfl_meta_from_js(path: Path) -> dict:
    """Extract NFL_DATA_META from generated JS."""
    content = path.read_text(encoding="utf-8")

    match = re.search(
        r"export const NFL_DATA_META\s*=\s*(\{.*?\});",
        content,
        re.DOTALL,
    )

    if not match:
        log.error("Could not find NFL_DATA_META in file")
        sys.exit(1)

    meta = match.group(1)

    result = {}

    for key in ("season", "week", "player_count"):
        match = re.search(
            rf"\b{key}\s*:\s*(\d+)",
            meta,
        )
        if match:
            result[key] = int(match.group(1))

    match = re.search(
        r'\bgenerated_at\s*:\s*"([^"]+)"',
        meta,
    )
    if match:
        result["generated_at"] = match.group(1)

    return result


def extract_players_from_js(path: Path) -> list[dict]:
    """Extract NFL_PLAYERS array from generated JS."""
    content = path.read_text(encoding="utf-8")

    match = re.search(
        r"export const NFL_PLAYERS\s*=\s*(\[.*?\]);",
        content,
        re.DOTALL,
    )

    if not match:
        log.error("Could not find NFL_PLAYERS array in file")
        sys.exit(1)

    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as e:
        log.error(f"Could not parse NFL_PLAYERS JSON: {e}")
        sys.exit(1)


# ==============================================================================
# nfl_data.js validation
# ==============================================================================

def validate(
    players: list[dict],
    meta: dict,
    strict: bool = False,
) -> tuple[list[str], list[str]]:
    """
    Validate current nfl_data.js player records.

    Python is the authoritative rating engine, so this validator checks
    the generated rating fields and their ranges, not the rating formula.
    """
    errors = []
    warnings = []
    seen_ids = set()

    if not players:
        errors.append("NFL_PLAYERS array is empty")
        return errors, warnings

    # --------------------------------------------------------------------------
    # Metadata
    # --------------------------------------------------------------------------

    if "player_count" not in meta:
        errors.append("NFL_DATA_META missing player_count")
    elif meta["player_count"] != len(players):
        errors.append(
            f"NFL_DATA_META.player_count={meta['player_count']} "
            f"but NFL_PLAYERS contains {len(players)} records"
        )

    if "season" not in meta:
        errors.append("NFL_DATA_META missing season")

    if "week" not in meta:
        errors.append("NFL_DATA_META missing week")

    if "generated_at" not in meta or not meta["generated_at"]:
        errors.append("NFL_DATA_META missing generated_at")

    if len(players) < 500:
        warnings.append(
            f"Only {len(players)} players — expected roughly 600+ "
            "for the current skill-position pipeline"
        )

    # --------------------------------------------------------------------------
    # Player records
    # --------------------------------------------------------------------------

    for i, r in enumerate(players):
        ctx = (
            f"[{i}] "
            f"{r.get('name', '?')} "
            f"({r.get('gsis_id', 'NO_ID')})"
        )

        # Required fields
        for field in REQUIRED_FIELDS:
            if field not in r:
                errors.append(
                    f"{ctx}: missing required field '{field}'"
                )
            elif r[field] is None and field != "opponent_team":
                errors.append(
                    f"{ctx}: field '{field}' is null"
                )

        # Opponent may be null for bye-week players.
        opponent = r.get("opponent_team")
        valid_nfl_teams = {
            "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN",
            "CLE", "DAL", "DEN", "DET", "GB", "HOU", "IND",
            "JAX", "KC", "LAC", "LA", "LV", "MIA", "MIN",
            "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA",
            "SF", "TB", "TEN", "WAS",
        }
        if opponent is not None and opponent not in valid_nfl_teams:
            errors.append(
                f"{ctx}: invalid opponent_team '{opponent}'"
            )

        # GSIS ID
        gsis = r.get("gsis_id", "")

        if not gsis or not isinstance(gsis, str):
            errors.append(
                f"{ctx}: gsis_id is empty or not a string"
            )
        elif gsis in seen_ids:
            errors.append(
                f"{ctx}: duplicate gsis_id '{gsis}'"
            )

        seen_ids.add(gsis)

        # Position
        pos = r.get("position", "")

        if pos not in VALID_POSITIONS:
            errors.append(
                f"{ctx}: invalid position '{pos}'"
            )

        # Team
        team = r.get("team")

        if not isinstance(team, str) or not team:
            errors.append(
                f"{ctx}: team is empty or not a string"
            )

        # ----------------------------------------------------------------------
        # Numeric validation
        # ----------------------------------------------------------------------

        for field in RATING_FIELDS:
            value = r.get(field)

            try:
                value = float(value)
            except (TypeError, ValueError):
                errors.append(
                    f"{ctx}: {field}='{value}' is not numeric"
                )
                continue

            if not 0.0 <= value <= 100.0:
                errors.append(
                    f"{ctx}: {field}={value} out of range [0, 100]"
                )

        # Shares / percentages are stored as decimals
        for field in SHARE_FIELDS:
            value = r.get(field)

            try:
                value = float(value)
            except (TypeError, ValueError):
                errors.append(
                    f"{ctx}: {field}='{value}' is not numeric"
                )
                continue

            if not 0.0 <= value <= 1.0:
                errors.append(
                    f"{ctx}: {field}={value} out of range [0, 1]"
                )

        # Play probability
        play_prob = r.get("play_probability")

        try:
            play_prob = float(play_prob)
        except (TypeError, ValueError):
            errors.append(
                f"{ctx}: play_probability='{play_prob}' is not numeric"
            )
            play_prob = None

        if play_prob is not None and not 0.0 <= play_prob <= 1.0:
            errors.append(
                f"{ctx}: play_probability={play_prob} "
                "out of range [0, 1]"
            )

        # Team success rates
        for field in TEAM_SUCCESS_FIELDS:
            value = r.get(field)

            try:
                value = float(value)
            except (TypeError, ValueError):
                errors.append(
                    f"{ctx}: {field}='{value}' is not numeric"
                )
                continue

            if not 0.0 <= value <= 1.0:
                errors.append(
                    f"{ctx}: {field}={value} out of range [0, 1]"
                )

        # Team play count / season counting stats
        for field in NON_NEGATIVE_FIELDS:
            value = r.get(field)

            try:
                value = float(value)
            except (TypeError, ValueError):
                errors.append(
                    f"{ctx}: {field}='{value}' is not numeric"
                )
                continue

            if value < 0:
                errors.append(
                    f"{ctx}: {field}={value} cannot be negative"
                )

        # EPA values may legitimately be negative
        for field in (
            "epa_per_play",
            "team_off_epa",
            "team_def_epa",
            "opp_def_epa",
        ):
            value = r.get(field)

            try:
                float(value)
            except (TypeError, ValueError):
                errors.append(
                    f"{ctx}: {field}='{value}' is not numeric"
                )

        # ----------------------------------------------------------------------
        # Data-quality warnings
        # ----------------------------------------------------------------------

        if pos in {"QB", "RB", "WR", "TE"}:
            season_pts = float(r.get("season_avg_pts", 0) or 0)
            epa = float(r.get("epa_per_play", 0) or 0)
            snap = float(r.get("snap_pct", 0) or 0)

            if epa == 0.0 and season_pts > 5:
                warnings.append(
                    f"{ctx}: epa_per_play=0.0 with "
                    f"{season_pts} avg fantasy points"
                )

            if snap == 0.0 and season_pts > 5:
                warnings.append(
                    f"{ctx}: snap_pct=0.0 with "
                    f"{season_pts} avg fantasy points"
                )

        # Historical production should remain available even if unavailable
        # this week, because replacement/value logic can still use it.
        if (
            play_prob == 0.0
            and float(r.get("season_avg_pts", 0) or 0) == 0.0
        ):
            warnings.append(
                f"{ctx}: play_probability=0 with "
                "season_avg_pts=0.0"
            )

        # ----------------------------------------------------------------------
        # Context sanity
        # ----------------------------------------------------------------------

        opponent = r.get("opponent_team")

        if opponent is not None and not isinstance(opponent, str):
            errors.append(
                f"{ctx}: opponent_team must be string or null"
            )

    # --------------------------------------------------------------------------
    # Global ordering check
    # --------------------------------------------------------------------------

    ratings = [
        r.get("composite_rating")
        for r in players
        if isinstance(r.get("composite_rating"), (int, float))
    ]

    if ratings:
        if ratings != sorted(ratings, reverse=True):
            warnings.append(
                "NFL_PLAYERS is not sorted by composite_rating descending"
            )

    return errors, warnings


# ==============================================================================
# espn_league.js validator
# ==============================================================================

def extract_league_from_js(path: Path) -> dict:
    """Extract ESPN_LEAGUE_DATA object from espn_league.js."""
    content = path.read_text(encoding="utf-8")

    match = re.search(
        r"export const ESPN_LEAGUE_DATA\s*=\s*(\{.*?\});\s*\n",
        content,
        re.DOTALL,
    )

    if not match:
        log.error("Could not find ESPN_LEAGUE_DATA object in file")
        log.error("Run scripts/fetch_espn_league.py first")
        sys.exit(1)

    return json.loads(match.group(1))


def validate_league(
    data: dict,
    strict: bool = False,
) -> tuple[list[str], list[str]]:
    """Validate espn_league.js against the league schema."""
    errors = []
    warnings = []

    for key in LEAGUE_REQUIRED_KEYS:
        if key not in data:
            errors.append(
                f"Missing required top-level key: '{key}'"
            )

    if errors:
        return errors, warnings

    if data["league_id"] != 839979:
        errors.append(
            f"league_id={data['league_id']} — expected 839979"
        )

    if (
        not isinstance(data["current_week"], int)
        or data["current_week"] < 0
    ):
        errors.append(
            f"current_week={data['current_week']} — "
            "expected non-negative int"
        )

    if not data["fetched_at"]:
        errors.append("fetched_at is empty")

    # Teams
    teams = data["teams"]

    if len(teams) != EXPECTED_TEAM_COUNT:
        errors.append(
            f"Expected {EXPECTED_TEAM_COUNT} teams, got {len(teams)}"
        )

    team_ids = set()

    for i, team in enumerate(teams):
        ctx = f"teams[{i}] '{team.get('team_name', '?')}'"

        for field in (
            "team_id",
            "team_name",
            "wins",
            "losses",
            "points_for",
            "points_against",
        ):
            if field not in team:
                errors.append(
                    f"{ctx}: missing field '{field}'"
                )

        tid = team.get("team_id")

        if tid in team_ids:
            errors.append(
                f"{ctx}: duplicate team_id {tid}"
            )

        team_ids.add(tid)

    # Rosters
    rosters = data["rosters"]

    if not isinstance(rosters, dict):
        errors.append(
            "rosters must be a dict keyed by team_id string"
        )
    else:
        roster_team_ids = set(rosters.keys())
        expected_ids = {str(tid) for tid in team_ids}

        missing_rosters = expected_ids - roster_team_ids

        if missing_rosters:
            errors.append(
                f"Missing rosters for team_ids: "
                f"{sorted(missing_rosters)}"
            )

        total_slots = 0

        for team_id_str, roster in rosters.items():
            ctx_team = f"rosters[{team_id_str}]"

            if len(roster) < 10:
                errors.append(
                    f"{ctx_team}: only {len(roster)} slots "
                    "(expected ≥10)"
                )

            seen_espn_ids = set()

            for j, player in enumerate(roster):
                ctx = (
                    f"{ctx_team}[{j}] "
                    f"'{player.get('name', '?')}'"
                )

                for field in ROSTER_PLAYER_REQUIRED:
                    if field not in player:
                        errors.append(
                            f"{ctx}: missing field '{field}'"
                        )

                espn_id = player.get("espn_id", "")

                if not espn_id or not isinstance(espn_id, str):
                    errors.append(
                        f"{ctx}: espn_id is empty or not a string"
                    )
                elif espn_id in seen_espn_ids:
                    errors.append(
                        f"{ctx}: duplicate espn_id "
                        f"'{espn_id}' within same roster"
                    )

                seen_espn_ids.add(espn_id)

                slot = player.get("lineup_slot", "")

                if slot not in VALID_LINEUP_SLOTS:
                    warnings.append(
                        f"{ctx}: unexpected lineup_slot '{slot}'"
                    )

                play_prob = player.get("play_probability", -1)

                try:
                    if not 0.0 <= float(play_prob) <= 1.0:
                        errors.append(
                            f"{ctx}: play_probability={play_prob} "
                            "out of range [0.0, 1.0]"
                        )
                except (TypeError, ValueError):
                    errors.append(
                        f"{ctx}: play_probability='{play_prob}' "
                        "is not a number"
                    )

                pos = player.get("position", "")

                if pos not in VALID_POSITIONS:
                    warnings.append(
                        f"{ctx}: unexpected position '{pos}'"
                    )

            total_slots += len(roster)

        if total_slots < EXPECTED_TEAM_COUNT * 10:
            warnings.append(
                f"Only {total_slots} total roster slots — "
                f"expected ≥{EXPECTED_TEAM_COUNT * 10}"
            )

    # Matchups
    matchups = data["matchups"]

    if not matchups:
        warnings.append(
            "matchups list is empty "
            "(offseason or week not set yet)"
        )
    else:
        expected_matchups = EXPECTED_TEAM_COUNT // 2

        if len(matchups) != expected_matchups:
            warnings.append(
                f"Expected {expected_matchups} matchups, "
                f"got {len(matchups)}"
            )

        matchup_team_ids = set()

        for i, matchup in enumerate(matchups):
            ctx = f"matchups[{i}]"

            for field in (
                "week",
                "home_team_id",
                "away_team_id",
            ):
                if field not in matchup:
                    errors.append(
                        f"{ctx}: missing field '{field}'"
                    )

            home_id = matchup.get("home_team_id")
            away_id = matchup.get("away_team_id")

            if home_id in matchup_team_ids:
                errors.append(
                    f"{ctx}: team {home_id} appears "
                    "in multiple matchups this week"
                )

            if away_id in matchup_team_ids:
                errors.append(
                    f"{ctx}: team {away_id} appears "
                    "in multiple matchups this week"
                )

            matchup_team_ids.update([home_id, away_id])

    # Schedule
    schedule = data["schedule"]

    if not schedule:
        warnings.append("schedule list is empty")
    else:
        weeks_seen = {
            entry.get("week")
            for entry in schedule
        }

        if len(weeks_seen) < 1:
            warnings.append(
                "schedule contains entries but no week values"
            )

    # Waiver pool
    waiver_pool = data["waiver_pool"]

    if len(waiver_pool) < 50:
        warnings.append(
            f"Only {len(waiver_pool)} waiver pool players — "
            "expected 50+ (offseason?)"
        )

    seen_waiver_ids = set()

    for j, player in enumerate(waiver_pool):
        ctx = (
            f"waiver_pool[{j}] "
            f"'{player.get('name', '?')}'"
        )

        for field in WAIVER_PLAYER_REQUIRED:
            if field not in player:
                errors.append(
                    f"{ctx}: missing field '{field}'"
                )

        espn_id = player.get("espn_id", "")

        if espn_id and espn_id in seen_waiver_ids:
            errors.append(
                f"{ctx}: duplicate espn_id "
                f"'{espn_id}' in waiver pool"
            )

        seen_waiver_ids.add(espn_id)

    if isinstance(rosters, dict):
        rostered_ids = {
            p["espn_id"]
            for roster in rosters.values()
            for p in roster
            if p.get("espn_id")
        }

        crossover = rostered_ids & seen_waiver_ids

        if crossover:
            errors.append(
                f"{len(crossover)} player(s) appear in both "
                "a roster and waiver pool: "
                f"{list(crossover)[:3]}"
                f"{'...' if len(crossover) > 3 else ''}"
            )

    # FAAB
    faab = data["faab_budgets"]

    if not faab:
        warnings.append("faab_budgets is empty")
    else:
        if len(faab) != EXPECTED_TEAM_COUNT:
            warnings.append(
                f"faab_budgets has {len(faab)} entries, "
                f"expected {EXPECTED_TEAM_COUNT}"
            )

        for team_id_str, budget in faab.items():
            if budget < 0:
                errors.append(
                    f"faab_budgets[{team_id_str}]={budget} "
                    "— cannot be negative"
                )

    # Standings
    standings = data["standings"]

    if len(standings) != EXPECTED_TEAM_COUNT:
        errors.append(
            f"standings has {len(standings)} entries, "
            f"expected {EXPECTED_TEAM_COUNT}"
        )
    else:
        seeds = [s.get("seed") for s in standings]

        if sorted(seeds) != list(
            range(1, EXPECTED_TEAM_COUNT + 1)
        ):
            errors.append(
                f"standings seeds are not 1–{EXPECTED_TEAM_COUNT}: "
                f"{seeds}"
            )

        standing_team_ids = {
            s.get("team_id")
            for s in standings
        }

        if standing_team_ids != team_ids:
            missing = team_ids - standing_team_ids
            extra = standing_team_ids - team_ids

            if missing:
                errors.append(
                    f"standings missing team_ids: {missing}"
                )

            if extra:
                errors.append(
                    f"standings has unknown team_ids: {extra}"
                )

    return errors, warnings


# ==============================================================================
# runners
# ==============================================================================

def run_nfl_validation(
    file_path: Path,
    strict: bool,
) -> bool:
    """Validate nfl_data.js."""
    if not file_path.exists():
        log.error(f"File not found: {file_path}")
        log.error("Run scripts/update_nfl_data.py first")
        return False

    log.info(f"--- Validating nfl_data.js: {file_path}")

    meta = extract_nfl_meta_from_js(file_path)
    players = extract_players_from_js(file_path)

    log.info(
        f"Loaded {len(players)} player records — "
        f"season={meta.get('season')}, "
        f"week={meta.get('week')}"
    )

    errors, warnings = validate(
        players,
        meta,
        strict=strict,
    )

    if warnings:
        log.warning(f"{len(warnings)} warning(s):")
        for warning in warnings:
            log.warning(f"  ⚠ {warning}")

    if errors:
        log.error(
            f"VALIDATION FAILED — {len(errors)} error(s):"
        )

        for error in errors:
            log.error(f"  ✗ {error}")

        return False

    if strict and warnings:
        log.warning(
            f"Strict mode: {len(warnings)} warning(s) retained; "
            "warnings do not fail validation."
        )

    log.info(
        f"✓ nfl_data.js passed — "
        f"{len(players)} players, "
        f"0 errors, "
        f"{len(warnings)} warnings"
    )

    return True


def run_league_validation(
    file_path: Path,
    strict: bool,
) -> bool:
    """Validate espn_league.js."""
    if not file_path.exists():
        log.error(f"File not found: {file_path}")
        log.error("Run scripts/fetch_espn_league.py first")
        return False

    log.info(f"--- Validating espn_league.js: {file_path}")

    data = extract_league_from_js(file_path)

    log.info(
        f"Loaded league data — "
        f"week {data.get('current_week')}, "
        f"{len(data.get('teams', []))} teams, "
        f"{sum(len(v) for v in data.get('rosters', {}).values())} "
        f"roster slots, "
        f"{len(data.get('waiver_pool', []))} waiver players"
    )

    errors, warnings = validate_league(
        data,
        strict=strict,
    )

    if warnings:
        log.warning(f"{len(warnings)} warning(s):")
        for warning in warnings:
            log.warning(f"  ⚠ {warning}")

    if errors:
        log.error(
            f"VALIDATION FAILED — {len(errors)} error(s):"
        )

        for error in errors:
            log.error(f"  ✗ {error}")

        return False

    if strict and warnings:
        log.warning(
            f"Strict mode: {len(warnings)} warning(s) retained; "
            "warnings do not fail validation."
        )

    log.info(
        f"✓ espn_league.js passed — "
        f"0 errors, {len(warnings)} warnings"
    )

    return True


# ==============================================================================
# main
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Validate Gridiron Oracle data files"
    )

    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Override file path",
    )

    parser.add_argument(
        "--league",
        action="store_true",
        help="Validate espn_league.js",
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="Validate both data files",
    )

    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors",
    )

    args = parser.parse_args()

    passed = True

    if args.all:
        nfl_path = args.file or NFL_DATA_PATH
        league_path = LEAGUE_DATA_PATH

        passed = (
            run_nfl_validation(
                nfl_path,
                args.strict,
            )
            and passed
        )

        passed = (
            run_league_validation(
                league_path,
                args.strict,
            )
            and passed
        )

    elif args.league:
        league_path = args.file or LEAGUE_DATA_PATH

        passed = run_league_validation(
            league_path,
            args.strict,
        )

    else:
        nfl_path = args.file or NFL_DATA_PATH

        passed = run_nfl_validation(
            nfl_path,
            args.strict,
        )

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()