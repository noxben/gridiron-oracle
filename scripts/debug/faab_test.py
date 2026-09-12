
#!/usr/bin/env python3
"""
test_faab.py — Inspect ESPN FAAB / acquisition data

Uses the exact same ESPN connection configuration as fetch_espn_roster.py.
"""

import os
import sys
from pathlib import Path
from pprint import pprint

from dotenv import load_dotenv
from espn_api.football import League


ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = ROOT / ".env"

LEAGUE_ID = 839979
SEASON = 2026


def load_credentials():
    load_dotenv(ENV_PATH)

    espn_s2 = os.getenv("ESPN_S2")
    swid = os.getenv("SWID")

    if not espn_s2 or not swid:
        print(f"[ERROR] ESPN_S2 and SWID not found in {ENV_PATH}")
        sys.exit(1)

    return espn_s2.strip(), swid.strip()


def connect_league(espn_s2, swid):
    print(f"Connecting to ESPN league {LEAGUE_ID} (season {SEASON})...")

    try:
        league = League(
            league_id=LEAGUE_ID,
            year=SEASON,
            espn_s2=espn_s2,
            swid=swid,
        )

        league_name = getattr(league, "name", None) or league.settings.name

        print(
            f"Connected to '{league_name}' "
            f"— {len(league.teams)} teams"
        )

        return league

    except Exception as e:
        print(f"[ERROR] Failed to connect: {e}")
        sys.exit(1)


def main():
    espn_s2, swid = load_credentials()
    league = connect_league(espn_s2, swid)

    # ------------------------------------------------------------
    # LEAGUE SETTINGS
    # ------------------------------------------------------------

    print()
    print("=" * 80)
    print("LEAGUE SETTINGS")
    print("=" * 80)

    settings = league.settings

    for name in dir(settings):
        if name.startswith("_"):
            continue

        if any(
            term in name.lower()
            for term in (
                "acquisition",
                "budget",
                "waiver",
                "faab",
                "bid",
            )
        ):
            try:
                print(f"{name}: {getattr(settings, name)!r}")
            except Exception as e:
                print(f"{name}: <ERROR {e}>")


    # ------------------------------------------------------------
    # TEAMS
    # ------------------------------------------------------------

    print()
    print("=" * 80)
    print("TEAM FAAB / ACQUISITION FIELDS")
    print("=" * 80)

    for team in league.teams:
        print()
        print(f"{team.team_name}  (ID {team.team_id})")

        found = False

        for name in dir(team):
            if name.startswith("_"):
                continue

            if any(
                term in name.lower()
                for term in (
                    "acquisition",
                    "budget",
                    "faab",
                    "waiver",
                    "bid",
                    "charge",
                )
            ):
                try:
                    value = getattr(team, name)
                    print(f"  {name}: {value!r}")
                    found = True
                except Exception as e:
                    print(f"  {name}: <ERROR {e}>")

        if not found:
            print("  <No acquisition/budget-related fields found>")


    # ------------------------------------------------------------
    # RAW TEAM OBJECT
    # ------------------------------------------------------------

    print()
    print("=" * 80)
    print("RAW TEAM OBJECT — TEAM 7")
    print("=" * 80)

    team7 = next(
        (t for t in league.teams if t.team_id == 7),
        None,
    )

    if team7:
        pprint(team7.__dict__, sort_dicts=True)
    else:
        print("Team 7 not found.")


    # ------------------------------------------------------------
    # TRANSACTIONS
    # ------------------------------------------------------------

    print()
    print("=" * 80)
    print("TRANSACTIONS")
    print("=" * 80)

    try:
        transactions = league.transactions()

        print(f"Transactions returned: {len(transactions)}")

        for tx in transactions[:50]:
            print()
            print("Transaction:")
            print(f"  type: {getattr(tx, 'type', None)!r}")
            print(f"  status: {getattr(tx, 'status', None)!r}")
            print(f"  date: {getattr(tx, 'date', None)!r}")
            print(
                f"  team: "
                f"{tx.team.team_name if getattr(tx, 'team', None) else None}"
            )
            print(
                f"  team_id: "
                f"{tx.team.team_id if getattr(tx, 'team', None) else None}"
            )

            # Print every public transaction attribute that might
            # contain bidding information.
            for name in dir(tx):
                if name.startswith("_"):
                    continue

                if any(
                    term in name.lower()
                    for term in (
                        "bid",
                        "amount",
                        "acquisition",
                        "budget",
                        "waiver",
                        "price",
                    )
                ):
                    try:
                        print(f"  {name}: {getattr(tx, name)!r}")
                    except Exception:
                        pass

            print("  items:")

            for item in getattr(tx, "items", []):
                print(
                    f"    type={getattr(item, 'type', None)!r} "
                    f"player={getattr(item, 'player', None)!r} "
                    f"playerId={getattr(item, 'playerId', None)!r}"
                )

    except Exception as e:
        print(f"Could not retrieve transactions: {e}")


    print()
    print("=" * 80)
    print("DONE")
    print("=" * 80)


if __name__ == "__main__":
    main()
