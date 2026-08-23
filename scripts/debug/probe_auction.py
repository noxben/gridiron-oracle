#!/usr/bin/env python3
"""
probe_auction.py — one-off diagnostic to discover what auction/draft data
is actually available via espn_api, before building the real pipeline.
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from espn_api.football import League

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

LEAGUE_ID = 839979
ESPN_S2 = os.getenv("ESPN_S2")
SWID = os.getenv("SWID")

load_dotenv(ROOT / ".env")

print("ROOT resolved to:", ROOT)
print("Looking for .env at:", ROOT / ".env")
print(".env exists:", (ROOT / ".env").exists())
print("ESPN_S2 loaded:", "yes" if os.getenv("ESPN_S2") else "NO — MISSING")
print("SWID loaded:", "yes" if os.getenv("SWID") else "NO — MISSING")

# --- Check current season settings (budget cap) ---
print("=== Current season (2026) settings ===")
league = League(league_id=LEAGUE_ID, year=2026, espn_s2=ESPN_S2, swid=SWID)
print("settings attrs:", [a for a in dir(league.settings) if not a.startswith('_')])
print()

# --- Check a past season's draft history ---
for season in [2025, 2024]:
    print(f"=== {season} draft ===")
    try:
        past_league = League(league_id=LEAGUE_ID, year=season, espn_s2=ESPN_S2, swid=SWID)
        draft = past_league.draft
        print(f"  {len(draft)} picks found")
        if draft:
            pick = draft[0]
            print("  pick attrs:", [a for a in dir(pick) if not a.startswith('_')])
            print("  sample pick:", vars(pick))
    except Exception as e:
        print(f"  FAILED: {e}")
    print()
    
# --- Check whether AAV / auction value data is exposed on player objects ---
print("=== Checking for AAV on player objects (2026) ===")
try:
    league_2026 = League(league_id=LEAGUE_ID, year=2026, espn_s2=ESPN_S2, swid=SWID)
    free_agents = league_2026.free_agents(size=5)
    if free_agents:
        p = free_agents[0]
        print("player attrs:", [a for a in dir(p) if not a.startswith('_')])
        print("sample player:", vars(p))
    else:
        print("  No free agents returned")
except Exception as e:
    print(f"  FAILED: {e}")    