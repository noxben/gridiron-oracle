#!/usr/bin/env python3
"""
fetch_espn_roster.py — Gridiron Oracle ESPN Roster Fetcher
===========================================================
Handles both in-season (real lineup slots) and offseason (projected lineup).

Usage:
  python3 scripts/fetch_espn_roster.py
  python3 scripts/fetch_espn_roster.py --week 14
  python3 scripts/fetch_espn_roster.py --dry-run
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
OUTPUT_PATH = ROOT / "src" / "utils" / "espn_data.js"
ENV_PATH    = ROOT / ".env"

LEAGUE_ID = 839979
SEASON    = 2025

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

INJURY_STATUS_MAP = {
    "ACTIVE": 1.0, "NORMAL": 1.0, "PROBABLE": 0.92,
    "QUESTIONABLE": 0.55, "DOUBTFUL": 0.25, "GTD": 0.55,
    "OUT": 0.0, "IR": 0.0, "SUSPENSION": 0.0,
}

ESPN_SLOT_TO_LABEL = {
    'QB': 'QB', 'RB': 'RB', 'WR': 'WR', 'TE': 'TE',
    'K': 'K', 'D/ST': 'DST', 'DST': 'DST', 'FLEX': 'FLEX',
    'BE': 'BENCH', 'IR': 'IR',
}


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


def serialize_player(player, slot_str='BE'):
    if player is None:
        return None
    injury_status = (getattr(player, 'injuryStatus', None) or 'ACTIVE').upper()
    play_prob     = INJURY_STATUS_MAP.get(injury_status, 1.0)
    slot_label    = ESPN_SLOT_TO_LABEL.get(slot_str.upper(), 'BENCH')
    return {
        "espn_id":          str(getattr(player, 'playerId', '') or ''),
        "name":             getattr(player, 'name', 'Unknown') or 'Unknown',
        "position": (getattr(player, 'position', 'UNK') or 'UNK').upper().replace('D/ST', 'DST'),
        "team":             (getattr(player, 'proTeam', 'UNK') or 'UNK').upper(),
        "lineup_slot":      slot_label,
        "on_bench":         slot_label in ('BENCH', 'IR'),
        "on_ir":            slot_label == 'IR',
        "injury_status":    injury_status,
        "play_probability": play_prob,
        "injury_detail":    injury_status if injury_status != 'ACTIVE' else '',
        "avg_points":       round(float(getattr(player, 'avg_points', 0) or 0), 2),
        "total_points":     round(float(getattr(player, 'total_points', 0) or 0), 2),
        "projected_points": round(float(getattr(player, 'projected_avg_points', 0) or 0), 2),
    }


def build_optimal_lineup(players):
    """
    Build best starting lineup from roster when ESPN has no lineup slots set.
    Used in offseason and any all-bench scenario.
    Mirrors getOptimalLineup() in simulator.js — keep in sync.
    """
    by_pos = {}
    for p in players:
        by_pos.setdefault(p['position'], []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: x['projected_points'], reverse=True)

    used     = set()
    starters = []

    def pick(pos, slot=None):
        for p in by_pos.get(pos, []):
            if p['espn_id'] not in used:
                used.add(p['espn_id'])
                p = {**p, 'lineup_slot': slot or pos, 'on_bench': False, 'on_ir': False}
                starters.append(p)
                return True
        return False

    pick('QB')
    pick('RB'); pick('RB')
    pick('WR'); pick('WR')
    pick('TE')
    pick('K')
    pick('DST')

    # FLEX — best remaining RB/WR/TE
    flex = sorted(
        [p for pos in ('RB', 'WR', 'TE') for p in by_pos.get(pos, []) if p['espn_id'] not in used],
        key=lambda x: x['projected_points'], reverse=True
    )
    if flex:
        p = {**flex[0], 'lineup_slot': 'FLEX', 'on_bench': False, 'on_ir': False}
        used.add(p['espn_id'])
        starters.append(p)

    bench = [{**p, 'lineup_slot': 'BENCH', 'on_bench': True} for p in players if p['espn_id'] not in used]
    return starters + bench


def fetch_my_team(league):
    swid    = os.getenv("SWID", "").strip().strip("{}")
    my_team = None
    for team in league.teams:
        owners = getattr(team, 'owners', []) or []
        for owner in owners:
            if swid.lower() in str(owner).lower().replace("{", "").replace("}", ""):
                my_team = team
                break
        if my_team:
            break
    if not my_team:
        log.warning("Could not auto-detect team from SWID — using first team")
        my_team = league.teams[0]
    log.info(f"My team: {my_team.team_name} (ID: {my_team.team_id})")
    return my_team


def fetch_roster(team, offseason=False):
    log.info(f"Fetching roster...")
    players = []
    for player in team.roster:
        slot_str = str(getattr(player, 'lineupSlot', 'BE') or 'BE')
        serialized = serialize_player(player, slot_str=slot_str)
        if serialized:
            players.append(serialized)

    # If all-bench (offseason or lineup not set) — build optimal
    active = [p for p in players if not p['on_bench'] and not p['on_ir']]
    if not active or offseason:
        log.info("No lineup set — building optimal lineup from projected points")
        players = build_optimal_lineup(players)

    starters = [p for p in players if not p['on_bench'] and not p['on_ir']]
    bench    = [p for p in players if p['on_bench']]
    log.info(f"Roster: {len(starters)} starters, {len(bench)} bench")
    return players


def fetch_matchup(league, team, week):
    log.info(f"Fetching matchup for week {week}...")
    try:
        box_scores = league.box_scores(week=week)
        my_id      = team.team_id
        for m in box_scores:
            home_id = getattr(m.home_team, 'team_id', None)
            away_id = getattr(m.away_team, 'team_id', None)
            if home_id == my_id or away_id == my_id:
                is_home  = home_id == my_id
                opp      = m.away_team if is_home else m.home_team
                return {
                    "week":          week,
                    "my_team_id":    my_id,
                    "opp_team_id":   getattr(opp, 'team_id', None),
                    "opp_team_name": getattr(opp, 'team_name', 'Unknown'),
                    "my_projected":  round(float(m.home_projected if is_home else m.away_projected or 0), 2),
                    "opp_projected": round(float(m.away_projected if is_home else m.home_projected or 0), 2),
                    "my_actual":     round(float(m.home_score if is_home else m.away_score or 0), 2),
                    "opp_actual":    round(float(m.away_score if is_home else m.home_score or 0), 2),
                }
        log.warning(f"No matchup found for team {my_id} in week {week}")
        return None
    except Exception as e:
        log.warning(f"Matchup fetch failed: {e}")
        return None


def fetch_league_info(league, week):
    settings = league.settings
    return {
        "league_id":      LEAGUE_ID,
        "season":         SEASON,
        "league_name":    getattr(league, 'name', None) or settings.name,
        "team_count":     len(league.teams),
        "current_week":   week,
        "scoring_format": getattr(settings, 'scoring_format', 'PPR'),
        "teams": [
            {"team_id": t.team_id, "team_name": t.team_name,
             "wins": getattr(t, 'wins', 0), "losses": getattr(t, 'losses', 0)}
            for t in league.teams
        ],
    }


def write_output(league_info, my_team, roster, matchup, dry_run):
    timestamp = datetime.now(timezone.utc).isoformat()
    starters  = [p for p in roster if not p['on_bench'] and not p['on_ir']]
    offseason = len(starters) == 0 or all(p.get('projected_points', 0) == 0 for p in starters)

    data = {
        "league":     league_info,
        "my_team":    {"team_id": my_team.team_id, "team_name": my_team.team_name,
                       "wins": getattr(my_team, 'wins', 0), "losses": getattr(my_team, 'losses', 0)},
        "roster":     roster,
        "matchup":    matchup,
        "fetched_at": timestamp,
        "offseason":  offseason,
    }

    js_content = f"""// espn_data.js — Gridiron Oracle ESPN roster data
// AUTO-GENERATED by scripts/fetch_espn_roster.py — DO NOT EDIT MANUALLY
// League: {LEAGUE_ID} | Season: {SEASON} | Fetched: {timestamp}
// Team: {my_team.team_name} | Starters: {len(starters)} | Total: {len(roster)}

export const ESPN_DATA = {json.dumps(data, indent=2)};

export const MY_ROSTER  = ESPN_DATA.roster;
export const MY_TEAM    = ESPN_DATA.my_team;
export const MATCHUP    = ESPN_DATA.matchup;
export const LEAGUE     = ESPN_DATA.league;
export const FETCHED_AT = ESPN_DATA.fetched_at;
export const OFFSEASON  = ESPN_DATA.offseason;
"""

    if dry_run:
        log.info(f"DRY RUN — {len(starters)} starters, {len(roster)} total")
        for p in starters:
            log.info(f"  {p['lineup_slot']:<6} {p['name']} ({p['position']}, {p['team']}) — {p['projected_points']} pts")
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(js_content, encoding="utf-8")
    log.info(f"Written: {len(starters)} starters, {len(roster)} total players")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--week",    type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log.info("=== Gridiron Oracle — ESPN roster fetch ===")
    espn_s2, swid = load_credentials()
    league        = connect_league(espn_s2, swid)
    week          = args.week or get_current_week(league)

    # Offseason: nfl_week > 22 or current_week == 0
    nfl_week  = getattr(league, 'nfl_week', week)
    offseason = nfl_week > 22 or week == 0
    if offseason:
        log.info("Offseason detected — building optimal lineup from projected points")

    my_team     = fetch_my_team(league)
    roster      = fetch_roster(my_team, offseason=offseason)
    matchup     = fetch_matchup(league, my_team, week)
    league_info = fetch_league_info(league, week)

    write_output(league_info, my_team, roster, matchup, dry_run=args.dry_run)
    log.info("=== ESPN fetch complete ===")


if __name__ == "__main__":
    main()
