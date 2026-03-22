#!/usr/bin/env python3
"""
fetch_weather.py — Gridiron Oracle Weather Integration
=======================================================
v2.0 Step 4 — fetches game-day weather for this week's NFL games.

Uses open-meteo.com (free, no API key required).
Flags games with meaningful fantasy impact per spec v2.0 §8:
  - Wind > 20 mph  → WIND   (downgrade WR/TE/QB in outdoor games)
  - Rain / snow    → PRECIP (upgrade RB, downgrade WR/passing ceiling)
  - Temp < 25°F   → COLD   (mild reduction in passing efficiency)
  - Dome game     → DOME   (no adjustment — positive passing signal)

Output: src/utils/weather_data.js
Run schedule: Thu morning + Sun morning (before games)

Usage:
  python3 scripts/fetch_weather.py
  python3 scripts/fetch_weather.py --week 14
  python3 scripts/fetch_weather.py --dry-run
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT        = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "src" / "utils" / "weather_data.js"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 10  # seconds — fail fast per spec (non-blocking)

# ---------------------------------------------------------------------------
# NFL stadium coordinates + dome status
# Locked list — update for relocated teams each offseason
# ---------------------------------------------------------------------------

NFL_STADIUMS = {
    # Dome / retractable roof (always DOME flag — no weather adjustment)
    "ARI": {"name": "State Farm Stadium",          "lat": 33.5277,  "lon": -112.2626, "dome": True},
    "ATL": {"name": "Mercedes-Benz Stadium",        "lat": 33.7554,  "lon": -84.4008,  "dome": True},
    "BUF": {"name": "Highmark Stadium",             "lat": 42.7738,  "lon": -78.7870,  "dome": False},  # outdoor
    "CAR": {"name": "Bank of America Stadium",      "lat": 35.2258,  "lon": -80.8528,  "dome": False},
    "CHI": {"name": "Soldier Field",                "lat": 41.8623,  "lon": -87.6167,  "dome": False},
    "CIN": {"name": "Paycor Stadium",               "lat": 39.0955,  "lon": -84.5160,  "dome": False},
    "CLE": {"name": "Huntington Bank Field",        "lat": 41.5061,  "lon": -81.6995,  "dome": False},
    "DAL": {"name": "AT&T Stadium",                 "lat": 32.7473,  "lon": -97.0945,  "dome": True},
    "DEN": {"name": "Empower Field",                "lat": 39.7439,  "lon": -105.0201, "dome": False},
    "DET": {"name": "Ford Field",                   "lat": 42.3400,  "lon": -83.0456,  "dome": True},
    "GB":  {"name": "Lambeau Field",                "lat": 44.5013,  "lon": -88.0622,  "dome": False},
    "HOU": {"name": "NRG Stadium",                  "lat": 29.6847,  "lon": -95.4107,  "dome": True},
    "IND": {"name": "Lucas Oil Stadium",            "lat": 39.7601,  "lon": -86.1639,  "dome": True},
    "JAX": {"name": "EverBank Stadium",             "lat": 30.3239,  "lon": -81.6373,  "dome": False},
    "KC":  {"name": "GEHA Field at Arrowhead",      "lat": 39.0489,  "lon": -94.4839,  "dome": False},
    "LAC": {"name": "SoFi Stadium",                 "lat": 33.9535,  "lon": -118.3392, "dome": True},   # retractable
    "LAR": {"name": "SoFi Stadium",                 "lat": 33.9535,  "lon": -118.3392, "dome": True},
    "LV":  {"name": "Allegiant Stadium",            "lat": 36.0909,  "lon": -115.1833, "dome": True},
    "MIA": {"name": "Hard Rock Stadium",            "lat": 25.9580,  "lon": -80.2389,  "dome": False},  # open-air
    "MIN": {"name": "U.S. Bank Stadium",            "lat": 44.9736,  "lon": -93.2575,  "dome": True},
    "NE":  {"name": "Gillette Stadium",             "lat": 42.0909,  "lon": -71.2643,  "dome": False},
    "NO":  {"name": "Caesars Superdome",            "lat": 29.9511,  "lon": -90.0812,  "dome": True},
    "NYG": {"name": "MetLife Stadium",              "lat": 40.8135,  "lon": -74.0745,  "dome": False},
    "NYJ": {"name": "MetLife Stadium",              "lat": 40.8135,  "lon": -74.0745,  "dome": False},
    "PHI": {"name": "Lincoln Financial Field",      "lat": 39.9008,  "lon": -75.1675,  "dome": False},
    "PIT": {"name": "Acrisure Stadium",             "lat": 40.4468,  "lon": -80.0158,  "dome": False},
    "SEA": {"name": "Lumen Field",                  "lat": 47.5952,  "lon": -122.3316, "dome": False},
    "SF":  {"name": "Levi's Stadium",               "lat": 37.4032,  "lon": -121.9698, "dome": False},
    "TB":  {"name": "Raymond James Stadium",        "lat": 27.9759,  "lon": -82.5033,  "dome": False},
    "TEN": {"name": "Nissan Stadium",               "lat": 36.1665,  "lon": -86.7713,  "dome": False},
    "WSH": {"name": "Northwest Stadium",            "lat": 38.9078,  "lon": -76.8645,  "dome": False},
    "BAL": {"name": "M&T Bank Stadium",             "lat": 39.2780,  "lon": -76.6227,  "dome": False},
}

# Weather flag thresholds — per spec v2.0 §8
WIND_MPH_THRESHOLD  = 20.0
COLD_TEMP_THRESHOLD = 25.0   # °F

# WMO weather code groups → PRECIP flag
# https://open-meteo.com/en/docs — WMO Weather interpretation codes
PRECIP_CODES = set(range(51, 68))   # drizzle + rain
PRECIP_CODES |= set(range(71, 78))  # snow
PRECIP_CODES |= set(range(80, 87))  # rain showers
PRECIP_CODES |= set(range(95, 100)) # thunderstorm


# ---------------------------------------------------------------------------
# Weather fetch — one stadium at a time
# Non-blocking: errors return None, caller handles gracefully
# ---------------------------------------------------------------------------

def fetch_stadium_weather(team: str, stadium: dict) -> dict | None:
    """
    Fetch hourly forecast for a stadium on the next game day.
    Returns a weather dict or None on failure.

    open-meteo returns hourly data for 7 days.
    We take the worst conditions in the 1pm–8pm window (typical game times).
    """
    if stadium["dome"]:
        return {
            "team":        team,
            "stadium":     stadium["name"],
            "dome":        True,
            "flags":       ["DOME"],
            "wind_mph":    0,
            "temp_f":      72,
            "precip_code": 0,
            "description": "Dome — no weather impact",
        }

    params = {
        "latitude":        stadium["lat"],
        "longitude":       stadium["lon"],
        "hourly":          "temperature_2m,windspeed_10m,weathercode",
        "temperature_unit": "fahrenheit",
        "windspeed_unit":  "mph",
        "timezone":        "auto",
        "forecast_days":   7,
    }

    try:
        resp = requests.get(OPEN_METEO_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        log.warning(f"  Weather fetch failed for {team} ({stadium['name']}): {e}")
        return None

    hourly     = data.get("hourly", {})
    times      = hourly.get("time", [])
    temps      = hourly.get("temperature_2m", [])
    winds      = hourly.get("windspeed_10m", [])
    wcodes     = hourly.get("weathercode", [])

    # Extract game-window hours (13:00–20:00 local) across the next 7 days
    # Take the worst conditions in that window — conservative for fantasy
    game_window_temps  = []
    game_window_winds  = []
    game_window_wcodes = []

    for i, t in enumerate(times):
        try:
            hour = int(t[11:13])  # "2025-12-14T15:00" → 15
        except (IndexError, ValueError):
            continue
        if 13 <= hour <= 20:
            if i < len(temps):  game_window_temps.append(temps[i])
            if i < len(winds):  game_window_winds.append(winds[i])
            if i < len(wcodes): game_window_wcodes.append(wcodes[i])

    if not game_window_winds:
        log.warning(f"  No game-window data for {team} — skipping")
        return None

    # Worst-case values in game window
    max_wind  = max(game_window_winds)
    min_temp  = min(game_window_temps) if game_window_temps else 60.0
    worst_wcode = max(game_window_wcodes) if game_window_wcodes else 0

    # Build flags — per spec v2.0 §8
    flags = []
    if max_wind > WIND_MPH_THRESHOLD:
        flags.append("WIND")
    if worst_wcode in PRECIP_CODES:
        flags.append("PRECIP")
    if min_temp < COLD_TEMP_THRESHOLD:
        flags.append("COLD")
    if not flags:
        flags.append("CLEAR")

    description = build_description(flags, max_wind, min_temp, worst_wcode)

    return {
        "team":        team,
        "stadium":     stadium["name"],
        "dome":        False,
        "flags":       flags,
        "wind_mph":    round(max_wind, 1),
        "temp_f":      round(min_temp, 1),
        "precip_code": worst_wcode,
        "description": description,
    }


def build_description(flags: list, wind: float, temp: float, wcode: int) -> str:
    parts = []
    if "WIND" in flags:
        parts.append(f"wind {wind:.0f} mph")
    if "PRECIP" in flags:
        parts.append("rain/snow expected")
    if "COLD" in flags:
        parts.append(f"{temp:.0f}°F")
    if not parts:
        parts.append(f"{temp:.0f}°F, clear")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Impact advisory — per spec v2.0 §8 flag definitions
# Advisory text shown on player cards in the UI
# ---------------------------------------------------------------------------

FLAG_ADVISORY = {
    "WIND":   "Wind >20 mph — downgrade WR/TE passing game",
    "PRECIP": "Rain/snow — upgrade RB, downgrade WR ceiling",
    "COLD":   "Cold (<25°F) — mild passing efficiency reduction",
    "DOME":   "Dome — positive passing environment",
    "CLEAR":  None,  # no advisory needed
}


def build_advisory(flags: list) -> list[str]:
    return [FLAG_ADVISORY[f] for f in flags if FLAG_ADVISORY.get(f)]


# ---------------------------------------------------------------------------
# Main fetch loop
# ---------------------------------------------------------------------------

def fetch_all_weather() -> dict:
    log.info("Fetching weather for all NFL stadiums...")
    results   = {}
    success   = 0
    skipped   = 0  # domes — no API call needed
    failed    = 0

    for team, stadium in NFL_STADIUMS.items():
        if stadium["dome"]:
            skipped += 1
        weather = fetch_stadium_weather(team, stadium)
        if weather:
            weather["advisory"] = build_advisory(weather["flags"])
            results[team] = weather
            if not stadium["dome"]:
                success += 1
                flag_str = ", ".join(weather["flags"])
                log.info(f"  {team:<4} {stadium['name']:<35} [{flag_str}] "
                         f"wind={weather['wind_mph']}mph temp={weather['temp_f']}°F")
        else:
            failed += 1
            results[team] = {
                "team":      team,
                "stadium":   stadium["name"],
                "dome":      stadium["dome"],
                "flags":     ["UNKNOWN"],
                "advisory":  [],
                "error":     "fetch failed",
            }

    log.info(f"Weather complete: {success} outdoor fetched, {skipped} domes, {failed} failed")
    return results


# ---------------------------------------------------------------------------
# JS output — named export, matches espn_data.js / espn_league.js style
# ---------------------------------------------------------------------------

def write_js_file(weather: dict, path: Path, dry_run: bool = False):
    timestamp = datetime.now(timezone.utc).isoformat()
    flagged   = [t for t, w in weather.items() if any(f in w.get("flags", []) for f in ("WIND", "PRECIP", "COLD"))]

    js_content = f"""// weather_data.js — Gridiron Oracle game-day weather
// AUTO-GENERATED by scripts/fetch_weather.py — DO NOT EDIT MANUALLY
// Fetched: {timestamp}
// Flagged games: {", ".join(flagged) if flagged else "none"}

export const WEATHER_DATA = {json.dumps(weather, indent=2)};

/**
 * Get weather flags for a player's NFL team.
 * Returns array of flag strings: ["WIND"], ["PRECIP"], ["COLD"], ["DOME"], ["CLEAR"]
 * Returns ["UNKNOWN"] if weather data unavailable — treat as non-blocking.
 *
 * @param {{string}} nflTeam - NFL team abbreviation (e.g. "KC", "BUF")
 * @returns {{string[]}}
 */
export function getWeatherFlags(nflTeam) {{
  return WEATHER_DATA[nflTeam]?.flags ?? ["UNKNOWN"];
}}

/**
 * Get advisory text for a player's matchup weather.
 * Returns array of advisory strings for display on player cards.
 *
 * @param {{string}} nflTeam
 * @returns {{string[]}}
 */
export function getWeatherAdvisory(nflTeam) {{
  return WEATHER_DATA[nflTeam]?.advisory ?? [];
}}

/**
 * True if a team's game has any meaningful weather flag (not DOME/CLEAR/UNKNOWN).
 * Used to show/hide the weather indicator on player cards.
 *
 * @param {{string}} nflTeam
 * @returns {{boolean}}
 */
export function hasWeatherImpact(nflTeam) {{
  const flags = getWeatherFlags(nflTeam);
  return flags.some(f => ["WIND", "PRECIP", "COLD"].includes(f));
}}

export const WEATHER_FETCHED_AT = "{timestamp}";
"""

    if dry_run:
        log.info(f"DRY RUN — would write ~{len(js_content) // 1024} KB to {path}")
        log.info(f"  Total teams: {len(weather)}")
        log.info(f"  Flagged (WIND/PRECIP/COLD): {flagged if flagged else 'none'}")
        for team, w in weather.items():
            flags = w.get("flags", [])
            if any(f in flags for f in ("WIND", "PRECIP", "COLD")):
                log.info(f"  ⚠ {team}: {flags} — {w.get('description', '')}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(js_content, encoding="utf-8")
    size_kb = round(path.stat().st_size / 1024, 1)
    log.info(f"Written: {path}  ({size_kb} KB)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch NFL game-day weather — Gridiron Oracle v2.0")
    parser.add_argument("--dry-run", action="store_true", help="Fetch but do not write file")
    parser.add_argument("--output",  type=str, default=None, help="Override output path")
    parser.add_argument("--week",    type=int, default=None, help="(Reserved — week not used, data is forward-looking)")
    args = parser.parse_args()

    output_path = Path(args.output) if args.output else OUTPUT_PATH

    log.info("=== Gridiron Oracle — weather fetch ===")
    log.info(f"Output: {output_path}")

    weather = fetch_all_weather()
    write_js_file(weather, output_path, dry_run=args.dry_run)

    log.info("=== Weather fetch complete ===")


if __name__ == "__main__":
    main()
