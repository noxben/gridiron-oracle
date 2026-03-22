#!/usr/bin/env python3
"""
fetch_espn_league.py — Gridiron Oracle Full League Fetcher
===========================================================
v2.0 Step 1 — fetches ALL league-wide data for league 839979 (#siscocks).

What this fetches:
  - All 12 rosters with lineup slots, projections, injury status
  - This week's matchups (with projected + actual scores)
  - Full season schedule (all weeks)
  - Waiver pool (unrostered players, all positions)
  - FAAB budgets per team
  - Standings (sorted by wins, then points_for)
  - Recent transactions (last ~50 actions)

Output: src/utils/espn_league.js
Schema: locked per spec v2.0 §4.1

Run schedule (cron): Tue 06:00, Thu 06:00, Sun 06:00 (before games)
Credentials: Commissioner's ESPN_S2 + SWID from .env (same as fetch_espn_roster.py)

Usage:
  python3 scripts/fetch_espn_league.py
  python3 scripts/fetch_espn_roster.py --week 14   # override scoring period
  python3 scripts/fetch_espn_league.py --dry-run   # print summary, no file write
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from espn_api.football import League

ROOT        = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "src" / "utils" / "espn_league.js"
ENV_PATH    = ROOT / ".env"

LEAGUE_ID = 839979
SEASON    = 2025   # ⚠ Update to 2026 before August 2026 — also rebuild id_mapping

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# Mirrors fetch_espn_roster.py — keep in sync
INJURY_STATUS_MAP = {
    "ACTIVE":       1.0,
    "NORMAL":       1.0,
    "PROBABLE":     0.92,
    "QUESTIONABLE": 0.55,
    "DOUBTFUL":     0.25,
    "GTD":          0.55,
    "OUT":          0.0,
    "IR":           0.0,
    "SUSPENSION":   0.0,
}

# Mirrors ESPN_SLOT_MAP in espn_api.js
ESPN_SLOT_TO_LABEL = {
    'QB': 'QB', 'RB': 'RB', 'WR': 'WR', 'TE': 'TE',
    'K':  'K',  'D/ST': 'DST', 'DST': 'DST', 'FLEX': 'FLEX',
    'BE': 'BENCH', 'IR': 'IR',
}

SKILL_POSITIONS = ['QB', 'RB', 'WR', 'TE']


# ---------------------------------------------------------------------------
# Credentials — same pattern as fetch_espn_roster.py
# ---------------------------------------------------------------------------

def load_credentials():
    load_dotenv(ENV_PATH)
    espn_s2 = os.getenv("ESPN_S2")
    swid    = os.getenv("SWID")
    if not espn_s2 or not swid:
        log.error(f"ESPN_S2 and SWID not found in {ENV_PATH}")
        sys.exit(1)
    return espn_s2.strip(), swid.strip()


def connect_league(espn_s2, swid):
    log.info(f"Connecting to ESPN league {LEAGUE_ID} (season {SEASON})...")
    try:
        league      = League(league_id=LEAGUE_ID, year=SEASON, espn_s2=espn_s2, swid=swid)
        league_name = getattr(league, 'name', None) or league.settings.name
        log.info(f"Connected to '{league_name}' — {len(league.teams)} teams")
        return league
    except Exception as e:
        log.error(f"Failed to connect: {e}")
        sys.exit(1)


def get_current_week(league):
    try:
        return league.current_week
    except Exception:
        return 1


# ---------------------------------------------------------------------------
# Player serialization — field names match espn_data.js exactly
# ---------------------------------------------------------------------------

def serialize_player(player, slot_str='BE', team_id=None):
    """
    Convert espn_api Player object → plain dict.
    Field names match fetch_espn_roster.py serialize_player() — keep in sync.
    Extra fields for league context: on_team_id.
    """
    if player is None:
        return None

    injury_status = (getattr(player, 'injuryStatus', None) or 'ACTIVE').upper()
    play_prob     = INJURY_STATUS_MAP.get(injury_status, 1.0)
    slot_label    = ESPN_SLOT_TO_LABEL.get(slot_str.upper(), 'BENCH')

    # Normalize position — strip D/ST → DST, matches espn_api.js ESPN_SLOT_MAP
    raw_pos  = (getattr(player, 'position', 'UNK') or 'UNK').upper()
    position = raw_pos.replace('D/ST', 'DST')

    return {
        # IDs — espn_id is STRING per existing codebase convention
        "espn_id":          str(getattr(player, 'playerId', '') or ''),
        "on_team_id":       team_id,                    # None for waiver pool players

        # Identity
        "name":             getattr(player, 'name', 'Unknown') or 'Unknown',
        "position":         position,
        "team":             (getattr(player, 'proTeam', 'UNK') or 'UNK').upper(),

        # Lineup slot (rostered players only — waiver pool has no slot)
        "lineup_slot":      slot_label,
        "on_bench":         slot_label in ('BENCH', 'IR'),
        "on_ir":            slot_label == 'IR',

        # Injury
        "injury_status":    injury_status,
        "play_probability": play_prob,
        "injury_detail":    injury_status if injury_status != 'ACTIVE' else '',

        # Scoring — field names match fetch_espn_roster.py
        "avg_points":       round(float(getattr(player, 'avg_points',          0) or 0), 2),
        "total_points":     round(float(getattr(player, 'total_points',         0) or 0), 2),
        "projected_points": round(float(getattr(player, 'projected_avg_points', 0) or 0), 2),

        # Availability (waiver pool context)
        "percent_owned":    round(float(getattr(player, 'percent_owned',   0) or 0), 1),
        "percent_started":  round(float(getattr(player, 'percent_started', 0) or 0), 1),
    }


# ---------------------------------------------------------------------------
# Roster fetch — all 12 teams
# ---------------------------------------------------------------------------

def fetch_all_rosters(league):
    log.info("Fetching all 12 rosters...")
    teams_list    = []
    rosters_dict  = {}
    total_players = 0

    for team in league.teams:
        team_id = team.team_id

        teams_list.append({
            "team_id":        team_id,
            "team_name":      team.team_name,
            "owner":          getattr(team, 'owner', ''),
            "wins":           getattr(team, 'wins',   0),
            "losses":         getattr(team, 'losses', 0),
            "ties":           getattr(team, 'ties',   0),
            "points_for":     round(float(getattr(team, 'points_for',     0) or 0), 2),
            "points_against": round(float(getattr(team, 'points_against', 0) or 0), 2),
            "playoff_seed":   None,  # backfilled by build_standings()
        })

        roster_players = []
        for player in team.roster:
            slot_str = str(getattr(player, 'lineupSlot', 'BE') or 'BE')
            p = serialize_player(player, slot_str=slot_str, team_id=team_id)
            if p:
                roster_players.append(p)
                total_players += 1

        rosters_dict[str(team_id)] = roster_players

    log.info(f"  {len(league.teams)} teams, {total_players} total roster slots")
    return teams_list, rosters_dict


# ---------------------------------------------------------------------------
# Matchups — current week
# ---------------------------------------------------------------------------

def fetch_matchups(league, current_week):
    log.info(f"Fetching matchups for week {current_week}...")
    matchups_list = []
    try:
        box_scores = league.box_scores(week=current_week)
        for box in box_scores:
            home = box.home_team
            away = box.away_team
            matchups_list.append({
                "week":           current_week,
                "home_team_id":   home.team_id if home else None,
                "away_team_id":   away.team_id if away else None,
                "home_projected": round(float(box.home_projected or 0), 2),
                "away_projected": round(float(box.away_projected or 0), 2),
                "home_score":     round(float(box.home_score    or 0), 2),
                "away_score":     round(float(box.away_score    or 0), 2),
            })
        log.info(f"  {len(matchups_list)} matchups")
    except Exception as e:
        log.warning(f"  Matchups fetch failed (non-blocking): {e}")
    return matchups_list


# ---------------------------------------------------------------------------
# Full season schedule — all weeks
# ---------------------------------------------------------------------------

def fetch_schedule(league, current_week):
    log.info("Fetching full season schedule...")
    schedule_list = []
    last_week     = 1
    try:
        for week_num in range(1, 19):
            try:
                week_boxes = league.box_scores(week=week_num)
                for box in week_boxes:
                    home = box.home_team
                    away = box.away_team
                    schedule_list.append({
                        "week":         week_num,
                        "home_team_id": home.team_id if home else None,
                        "away_team_id": away.team_id if away else None,
                        "home_score":   round(float(box.home_score or 0), 2),
                        "away_score":   round(float(box.away_score or 0), 2),
                        "completed":    week_num < current_week,
                    })
                last_week = week_num
            except Exception:
                break  # future weeks don't exist yet — stop cleanly
        log.info(f"  {len(schedule_list)} schedule entries across {last_week} weeks")
    except Exception as e:
        log.warning(f"  Schedule fetch failed (non-blocking): {e}")
    return schedule_list


# ---------------------------------------------------------------------------
# Waiver pool — all unrostered players
# ---------------------------------------------------------------------------

def fetch_waiver_pool(league):
    log.info("Fetching waiver pool...")
    waiver_list = []
    try:
        # Skill positions — 50 deep each
        for pos in SKILL_POSITIONS:
            try:
                for player in league.free_agents(size=50, position=pos):
                    p = serialize_player(player, slot_str='BE', team_id=None)
                    if p:
                        p['position'] = pos  # free_agents() sometimes blanks position
                        waiver_list.append(p)
            except Exception as e:
                log.warning(f"  free_agents({pos}) failed: {e}")

        # K and DST — 20 deep each
        for pos in ['K', 'D/ST']:
            try:
                for player in league.free_agents(size=20, position=pos):
                    p = serialize_player(player, slot_str='BE', team_id=None)
                    if p:
                        p['position'] = pos.replace('D/ST', 'DST')
                        waiver_list.append(p)
            except Exception as e:
                log.warning(f"  free_agents({pos}) failed: {e}")

        # Deduplicate by espn_id
        seen, deduped = set(), []
        for p in waiver_list:
            eid = p.get('espn_id')
            if eid and eid not in seen:
                seen.add(eid)
                deduped.append(p)
        waiver_list = deduped

        log.info(f"  {len(waiver_list)} waiver pool players")
    except Exception as e:
        log.warning(f"  Waiver pool fetch failed (non-blocking): {e}")
    return waiver_list


# ---------------------------------------------------------------------------
# FAAB budgets
# ---------------------------------------------------------------------------

def fetch_faab_budgets(league):
    log.info("Fetching FAAB budgets...")
    faab_dict = {}
    try:
        for team in league.teams:
            faab_dict[str(team.team_id)] = round(float(getattr(team, 'faab', 0) or 0), 2)
        log.info(f"  {len(faab_dict)} teams")
    except Exception as e:
        log.warning(f"  FAAB fetch failed (non-blocking): {e}")
    return faab_dict


# ---------------------------------------------------------------------------
# Standings — sorted by wins desc, points_for desc
# ---------------------------------------------------------------------------

def build_standings(league, teams_list):
    log.info("Building standings...")
    standings_list = []
    try:
        sorted_teams = sorted(
            league.teams,
            key=lambda t: (-getattr(t, 'wins', 0), -float(getattr(t, 'points_for', 0) or 0))
        )
        for seed, team in enumerate(sorted_teams, start=1):
            standings_list.append({
                "seed":           seed,
                "team_id":        team.team_id,
                "team_name":      team.team_name,
                "wins":           getattr(team, 'wins',   0),
                "losses":         getattr(team, 'losses', 0),
                "ties":           getattr(team, 'ties',   0),
                "points_for":     round(float(getattr(team, 'points_for',     0) or 0), 2),
                "points_against": round(float(getattr(team, 'points_against', 0) or 0), 2),
            })
            # Backfill playoff_seed into teams_list entry
            for t in teams_list:
                if t['team_id'] == team.team_id:
                    t['playoff_seed'] = seed
        log.info(f"  {len(standings_list)} teams seeded")
    except Exception as e:
        log.warning(f"  Standings failed (non-blocking): {e}")
    return standings_list


# ---------------------------------------------------------------------------
# Recent transactions
# ---------------------------------------------------------------------------

def fetch_transactions(league):
    log.info("Fetching recent transactions...")
    transactions_list = []
    try:
        activity = league.recent_activity(size=50)
        for action in activity:
            for a in action.actions:
                team_obj   = a[0]
                action_str = a[1]
                player_obj = a[2]
                transactions_list.append({
                    "date":     (action.date.isoformat()
                                 if hasattr(action.date, 'isoformat')
                                 else str(action.date)),
                    "team_id":  getattr(team_obj,   'team_id',  None),
                    "type":     action_str,                              # 'ADD', 'DROP', 'TRADED'
                    "player":   getattr(player_obj, 'name',     'Unknown'),
                    "espn_id":  str(getattr(player_obj, 'playerId', '') or ''),
                })
        log.info(f"  {len(transactions_list)} transactions")
    except Exception as e:
        log.warning(f"  Transactions fetch failed (non-blocking): {e}")
    return transactions_list


# ---------------------------------------------------------------------------
# Pre-flight validation — fast structural checks before writing
# Full gate is validate_data.py (Step 2)
# ---------------------------------------------------------------------------

def quick_validate(data: dict) -> list[str]:
    errors = []

    team_count = len(data['teams'])
    if team_count != 12:
        errors.append(f"Expected 12 teams, got {team_count}")

    if not data['rosters']:
        errors.append("rosters dict is empty")
    else:
        for team_id, roster in data['rosters'].items():
            if len(roster) < 10:
                errors.append(f"Team {team_id}: only {len(roster)} roster slots (expected ≥10)")
            missing_ids = [p['name'] for p in roster if not p.get('espn_id')]
            if missing_ids:
                errors.append(f"Team {team_id}: {len(missing_ids)} players missing espn_id: {missing_ids[:3]}")

    if not data['matchups']:
        errors.append("matchups list is empty — ESPN may not have set week yet (offseason?)")

    if not data['standings']:
        errors.append("standings list is empty")

    return errors


# ---------------------------------------------------------------------------
# JS file output — named exports, matches espn_data.js style
# validate_data.py (Step 2) will regex-parse ESPN_LEAGUE_DATA
# ---------------------------------------------------------------------------

def write_js_file(data: dict, path: Path, dry_run: bool = False):
    timestamp   = data['fetched_at']
    week        = data['current_week']
    team_count  = len(data['teams'])
    roster_slots = sum(len(v) for v in data['rosters'].values())

    js_content = f"""// espn_league.js — Gridiron Oracle full league data
// AUTO-GENERATED by scripts/fetch_espn_league.py — DO NOT EDIT MANUALLY
// League: {LEAGUE_ID} | Season: {SEASON} | Fetched: {timestamp}
// Week: {week} | Teams: {team_count} | Roster slots: {roster_slots} | Waiver pool: {len(data['waiver_pool'])}

export const ESPN_LEAGUE_DATA = {json.dumps(data, indent=2)};

export const ALL_TEAMS      = ESPN_LEAGUE_DATA.teams;
export const ALL_ROSTERS    = ESPN_LEAGUE_DATA.rosters;      // keyed by team_id string
export const ALL_MATCHUPS   = ESPN_LEAGUE_DATA.matchups;
export const FULL_SCHEDULE  = ESPN_LEAGUE_DATA.schedule;
export const WAIVER_POOL    = ESPN_LEAGUE_DATA.waiver_pool;
export const FAAB_BUDGETS   = ESPN_LEAGUE_DATA.faab_budgets; // keyed by team_id string
export const STANDINGS      = ESPN_LEAGUE_DATA.standings;
export const TRANSACTIONS   = ESPN_LEAGUE_DATA.transactions;
export const LEAGUE_WEEK    = ESPN_LEAGUE_DATA.current_week;
export const LEAGUE_FETCHED_AT = ESPN_LEAGUE_DATA.fetched_at;
"""

    if dry_run:
        log.info("DRY RUN — would write:")
        log.info(f"  teams:        {team_count}")
        log.info(f"  roster slots: {roster_slots}")
        log.info(f"  matchups:     {len(data['matchups'])}")
        log.info(f"  schedule:     {len(data['schedule'])}")
        log.info(f"  waiver pool:  {len(data['waiver_pool'])}")
        log.info(f"  standings:    {len(data['standings'])}")
        log.info(f"  transactions: {len(data['transactions'])}")
        log.info(f"  file size:    ~{len(js_content) // 1024} KB")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(js_content, encoding='utf-8')
    size_kb = round(path.stat().st_size / 1024, 1)
    log.info(f"Written: {path}  ({size_kb} KB)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch full ESPN league data — Gridiron Oracle v2.0")
    parser.add_argument('--week',    type=int,          default=None, help='Override scoring period week')
    parser.add_argument('--dry-run', action='store_true',             help='Fetch data but do not write file')
    parser.add_argument('--output',  type=str,          default=None, help='Override output path')
    args = parser.parse_args()

    output_path = Path(args.output) if args.output else OUTPUT_PATH

    log.info("=== Gridiron Oracle — full league fetch ===")

    espn_s2, swid = load_credentials()
    league        = connect_league(espn_s2, swid)

    # Offseason detection — mirrors fetch_espn_roster.py logic
    nfl_week  = getattr(league, 'nfl_week', None)
    week      = args.week or get_current_week(league)
    offseason = (nfl_week is not None and nfl_week > 22) or week == 0
    if offseason:
        log.info("Offseason detected — schedule/matchup data will be limited")

    teams_list, rosters_dict = fetch_all_rosters(league)
    matchups_list            = fetch_matchups(league, week)
    schedule_list            = fetch_schedule(league, week)
    waiver_list              = fetch_waiver_pool(league)
    faab_dict                = fetch_faab_budgets(league)
    standings_list           = build_standings(league, teams_list)  # also backfills playoff_seed
    transactions_list        = fetch_transactions(league)

    data = {
        "league_id":    LEAGUE_ID,
        "season":       SEASON,
        "current_week": week,
        "offseason":    offseason,
        "fetched_at":   datetime.now(timezone.utc).isoformat(),
        "teams":        teams_list,
        "rosters":      rosters_dict,
        "matchups":     matchups_list,
        "schedule":     schedule_list,
        "waiver_pool":  waiver_list,
        "faab_budgets": faab_dict,
        "standings":    standings_list,
        "transactions": transactions_list,
    }

    # Pre-flight check
    errors = quick_validate(data)
    if errors:
        log.error(f"Pre-flight validation FAILED ({len(errors)} error(s)):")
        for err in errors:
            log.error(f"  ✗ {err}")
        if not args.dry_run:
            log.error("File NOT written. Fix errors and re-run.")
            sys.exit(1)
    else:
        log.info("Pre-flight validation passed ✓")

    write_js_file(data, output_path, dry_run=args.dry_run)
    log.info("Next: python3 scripts/validate_data.py --file src/utils/espn_league.js")
    log.info("=== League fetch complete ===")


if __name__ == '__main__':
    main()
