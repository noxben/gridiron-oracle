#!/usr/bin/env python3
"""
fetch_espn_roster.py — Gridiron Oracle ESPN Roster Fetcher
===========================================================
Uses the espn_api Python library to pull roster, matchup, and league info
for league 839979. Writes output to src/utils/espn_data.js.

React reads espn_data.js directly — no browser API calls, no CORS, no proxy.

Run this script:
  - Once before each week's lineup decisions
  - Any time you want to refresh injury status or matchup info

Usage:
  python3 scripts/fetch_espn_roster.py
  python3 scripts/fetch_espn_roster.py --week 14
  python3 scripts/fetch_espn_roster.py --dry-run

Credentials:
  Set ESPN_S2 and SWID in .env file (never commit to git):
    ESPN_S2=AEBxxxxx...
    SWID={XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}
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

LEAGUE_ID   = 839979
SEASON      = 2025

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ESPN injury status → play_probability (per spec §7.3)
INJURY_STATUS_MAP = {
    "ACTIVE":       1.0,
    "NORMAL":       1.0,
    "PROBABLE":     0.92,
    "QUESTIONABLE": 0.55,
    "DOUBTFUL":     0.25,
    "OUT":          0.0,
    "IR":           0.0,
    "SUSPENSION":   0.0,
    "GTD":          0.55,
}

# ESPN position slot ID → label
SLOT_MAP = {
    0:  "QB",
    2:  "RB",
    4:  "WR",
    6:  "TE",
    16: "DST",
    17: "K",
    20: "BENCH",
    21: "IR",
    23: "FLEX",
}


def load_credentials():
    """Load ESPN credentials from .env file."""
    load_dotenv(ENV_PATH)
    espn_s2 = os.getenv("ESPN_S2")
    swid    = os.getenv("SWID")

    if not espn_s2 or not swid:
        log.error("ESPN_S2 and SWID not found in .env file")
        log.error(f"Create {ENV_PATH} with:")
        log.error("  ESPN_S2=AEBxxxxx...")
        log.error("  SWID={XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}")
        sys.exit(1)

    return espn_s2.strip(), swid.strip()


def connect_league(espn_s2, swid):
    """Connect to ESPN league using espn_api library."""
    log.info(f"Connecting to ESPN league {LEAGUE_ID} (season {SEASON})...")
    try:
        league = League(
            league_id=LEAGUE_ID,
            year=SEASON,
            espn_s2=espn_s2,
            swid=swid,
        )
        league_name = getattr(league, 'name', None) or league.settings.name
        log.info(f"✓ Connected to '{league_name}' — {len(league.teams)} teams")
        return league
    except Exception as e:
        log.error(f"Failed to connect to ESPN: {e}")
        sys.exit(1)


def get_current_week(league):
    """Get the current scoring period from the league."""
    try:
        return league.current_week
    except Exception:
        return 1


def serialize_player(player, slot_id=None):
    """
    Serialize an espn_api Player object to a plain dict.
    Maps to the schema espn_data.js consumers expect.
    """
    if player is None:
        return None

    # Injury status
    injury_status = getattr(player, 'injuryStatus', 'ACTIVE') or 'ACTIVE'
    play_prob     = INJURY_STATUS_MAP.get(injury_status.upper(), 1.0)

    # Stats
    stats         = getattr(player, 'stats', {}) or {}
    avg_pts       = getattr(player, 'avg_points', 0.0) or 0.0
    total_pts     = getattr(player, 'total_points', 0.0) or 0.0
    proj_pts      = getattr(player, 'projected_avg_points', 0.0) or 0.0

    # Position
    position      = getattr(player, 'position', 'UNK') or 'UNK'
    pro_team      = getattr(player, 'proTeam', 'UNK') or 'UNK'
    espn_id       = str(getattr(player, 'playerId', '') or '')

    return {
        "espn_id":         espn_id,
        "name":            getattr(player, 'name', 'Unknown') or 'Unknown',
        "position":        position.upper(),
        "team":            pro_team.upper(),
        "lineup_slot":     SLOT_MAP.get(slot_id, 'BENCH') if slot_id is not None else 'BENCH',
        "on_bench":        slot_id == 20,
        "on_ir":           slot_id == 21,
        "injury_status":   injury_status.upper(),
        "play_probability": play_prob,
        "injury_detail":   injury_status if injury_status.upper() != 'ACTIVE' else '',
        "avg_points":      round(float(avg_pts), 2),
        "total_points":    round(float(total_pts), 2),
        "projected_points": round(float(proj_pts), 2),
    }


def fetch_my_team(league):
    """
    Find the logged-in user's team.
    espn_api exposes league.teams — we pick the one whose owners
    match the SWID, or fall back to prompting.
    """
    teams = league.teams

    # espn_api sets team.owners as a list of SWIDs
    swid = os.getenv("SWID", "").strip().strip("{}")

    my_team = None
    for team in teams:
        owners = getattr(team, 'owners', []) or []
        for owner in owners:
            if swid.lower() in str(owner).lower().replace("{", "").replace("}", ""):
                my_team = team
                break
        if my_team:
            break

    if not my_team:
        log.warning("Could not auto-detect your team from SWID — listing all teams:")
        for i, team in enumerate(teams):
            log.info(f"  [{i}] {team.team_name} (ID: {team.team_id})")
        # Default to first team for now — will be configurable in UI
        my_team = teams[0]
        log.info(f"Defaulting to team 0: {my_team.team_name}")

    log.info(f"✓ My team: {my_team.team_name} (ID: {my_team.team_id})")
    return my_team


def fetch_roster(team, week):
    """Fetch and serialize the full roster for a team."""
    log.info(f"Fetching roster for week {week}...")

    roster = team.roster
    players = []

    for player in roster:
        slot_id    = getattr(player, 'slot_position', None)
        # slot_position is a string like 'QB', 'RB', 'BE', 'IR'
        # map back to slot ID for our SLOT_MAP
        slot_str   = str(slot_id).upper() if slot_id else 'BENCH'
        slot_id_num = {
            'QB': 0, 'RB': 2, 'WR': 4, 'TE': 6,
            'D/ST': 16, 'DST': 16, 'K': 17,
            'BE': 20, 'BENCH': 20, 'IR': 21, 'FLEX': 23,
        }.get(slot_str, 20)

        serialized = serialize_player(player, slot_id=slot_id_num)
        if serialized:
            players.append(serialized)

    log.info(f"✓ Roster: {len(players)} players")
    return players


def fetch_matchup(league, team, week):
    """Fetch this week's matchup info."""
    log.info(f"Fetching matchup for week {week}...")
    try:
        box_scores = league.box_scores(week=week)
        my_team_id = team.team_id

        for matchup in box_scores:
            home_id = getattr(matchup.home_team, 'team_id', None)
            away_id = getattr(matchup.away_team, 'team_id', None)

            if home_id == my_team_id or away_id == my_team_id:
                is_home  = home_id == my_team_id
                opp_team = matchup.away_team if is_home else matchup.home_team

                return {
                    "week":             week,
                    "my_team_id":       my_team_id,
                    "opp_team_id":      getattr(opp_team, 'team_id', None),
                    "opp_team_name":    getattr(opp_team, 'team_name', 'Unknown'),
                    "my_projected":     round(float(matchup.home_projected if is_home else matchup.away_projected or 0), 2),
                    "opp_projected":    round(float(matchup.away_projected if is_home else matchup.home_projected or 0), 2),
                    "my_actual":        round(float(matchup.home_score if is_home else matchup.away_score or 0), 2),
                    "opp_actual":       round(float(matchup.away_score if is_home else matchup.home_score or 0), 2),
                }

        log.warning(f"No matchup found for team {my_team_id} in week {week}")
        return None

    except Exception as e:
        log.warning(f"Matchup fetch failed: {e}")
        return None


def fetch_league_info(league, week):
    """Serialize league-level metadata."""
    settings = league.settings
    return {
        "league_id":        LEAGUE_ID,
        "season":           SEASON,
        "league_name":      getattr(league, 'name', None) or settings.name,
        "team_count":       len(league.teams),
        "current_week":     week,
        "scoring_format":   getattr(settings, 'scoring_format', 'PPR'),
        "teams": [
            {
                "team_id":   t.team_id,
                "team_name": t.team_name,
                "wins":      getattr(t, 'wins', 0),
                "losses":    getattr(t, 'losses', 0),
            }
            for t in league.teams
        ],
    }


def write_output(league_info, my_team, roster, matchup, dry_run):
    """Write all data to src/utils/espn_data.js."""
    timestamp = datetime.now(timezone.utc).isoformat()

    data = {
        "league":    league_info,
        "my_team": {
            "team_id":   my_team.team_id,
            "team_name": my_team.team_name,
            "wins":      getattr(my_team, 'wins', 0),
            "losses":    getattr(my_team, 'losses', 0),
        },
        "roster":    roster,
        "matchup":   matchup,
        "fetched_at": timestamp,
    }

    js_content = f"""// espn_data.js — Gridiron Oracle ESPN roster data
// AUTO-GENERATED by scripts/fetch_espn_roster.py — DO NOT EDIT MANUALLY
// League: {LEAGUE_ID} | Season: {SEASON} | Fetched: {timestamp}
// Team: {my_team.team_name} | Players: {len(roster)}
//
// Re-run scripts/fetch_espn_roster.py to refresh.

export const ESPN_DATA = {json.dumps(data, indent=2)};

export const MY_ROSTER  = ESPN_DATA.roster;
export const MY_TEAM    = ESPN_DATA.my_team;
export const MATCHUP    = ESPN_DATA.matchup;
export const LEAGUE     = ESPN_DATA.league;
export const FETCHED_AT = ESPN_DATA.fetched_at;
"""

    if dry_run:
        log.info(f"DRY RUN — would write {len(roster)} players to {OUTPUT_PATH}")
        log.info(f"Sample player: {json.dumps(roster[0] if roster else {{}}, indent=2)}")
        if matchup:
            log.info(f"Matchup: vs {matchup['opp_team_name']} | "
                     f"My proj: {matchup['my_projected']} | "
                     f"Opp proj: {matchup['opp_projected']}")
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(js_content, encoding="utf-8")
    log.info(f"✓ Wrote espn_data.js — {len(roster)} players, week {league_info['current_week']}")


def main():
    parser = argparse.ArgumentParser(description="Fetch ESPN roster data for Gridiron Oracle")
    parser.add_argument("--week",    type=int, default=None, help="NFL week (auto-detected if omitted)")
    parser.add_argument("--dry-run", action="store_true",    help="Preview without writing file")
    args = parser.parse_args()

    log.info("=== Gridiron Oracle — ESPN roster fetch ===")

    espn_s2, swid = load_credentials()
    league        = connect_league(espn_s2, swid)
    week          = args.week or get_current_week(league)

    log.info(f"Week: {week}")

    my_team     = fetch_my_team(league)
    roster      = fetch_roster(my_team, week)
    matchup     = fetch_matchup(league, my_team, week)
    league_info = fetch_league_info(league, week)

    write_output(league_info, my_team, roster, matchup, dry_run=args.dry_run)

    log.info("=== ESPN fetch complete ===")
    log.info(f"Next: run scripts/update_nfl_data.py to refresh nflfastR stats")


if __name__ == "__main__":
    main()
