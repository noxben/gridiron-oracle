"""
Gridiron Oracle — Rating Validation
====================================

Validates generated player ratings and reports:
- data integrity
- rating ranges
- position distributions
- component health
- suspiciously constant components
- top players by position

Run after:
    python3 scripts/update_nfl_data.py
"""

from __future__ import annotations

import math
import re
import statistics
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "src" / "utils" / "nfl_data.js"

POSITIONS = ["QB", "RB", "WR", "TE"]

COMPONENTS = [
    "epa_score",
    "usage_score",
    "snap_score",
    "red_zone_score",
    "composite_rating",
]


def load_players() -> list[dict]:
    """Load players from the generated NFL_PLAYERS JS export."""
    text = DATA_FILE.read_text(encoding="utf-8")

    match = re.search(
        r"export const NFL_PLAYERS = (\[.*?\]);",
        text,
        re.DOTALL,
    )

    if not match:
        raise RuntimeError(
            f"Could not find NFL_PLAYERS export in {DATA_FILE}"
        )

    import json

    raw = match.group(1)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "NFL_PLAYERS is not valid JSON. "
            "The generated JS format may need a parser adjustment."
        ) from exc

def load_metadata() -> dict:
    text = DATA_FILE.read_text(encoding="utf-8")

    match = re.search(
        r"export const NFL_DATA_META\s*=\s*(\{.*?\});",
        text,
        re.DOTALL,
    )

    if not match:
        raise RuntimeError("Could not find NFL_DATA_META")

    raw = match.group(1)

    result = {}

    for key in ("season", "week", "matchup_week", "player_count"):
        value = re.search(rf"\b{key}\s*:\s*(\d+)", raw)
        if value:
            result[key] = int(value.group(1))

    bye_match = re.search(
        r"\bbye_teams\s*:\s*(\[[^\]]*\])",
        raw,
    )

    if bye_match:
        import json
        result["bye_teams"] = json.loads(bye_match.group(1))

    return result

def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")

    values = sorted(values)

    index = (len(values) - 1) * p
    lower = math.floor(index)
    upper = math.ceil(index)

    if lower == upper:
        return values[lower]

    fraction = index - lower

    return (
        values[lower]
        + (values[upper] - values[lower]) * fraction
    )


def fmt(value: float) -> str:
    if math.isnan(value):
        return "N/A"

    return f"{value:.2f}"


def main() -> int:
    print("=" * 72)
    print("GRIDIRON ORACLE — RATING VALIDATION")
    print("=" * 72)

    players = load_players()

    meta = load_metadata()

    if meta.get("matchup_week") != meta.get("week", 0) + 1:
        raise RuntimeError(
            "Invalid forecast metadata: matchup_week must equal week + 1"
        )

    nfl_teams = {
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
        "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
        "LA", "LAC", "LV", "MIA", "MIN", "NE", "NO", "NYG",
        "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
    }

    bye_teams = meta.get("bye_teams", [])

    if len(bye_teams) != len(set(bye_teams)):
        raise RuntimeError("Duplicate teams in bye_teams metadata")

    if not set(bye_teams).issubset(nfl_teams):
        raise RuntimeError("Unknown team in bye_teams metadata")

    for player in players:
        team = str(player.get("team", "")).upper()
        opponent = player.get("opponent_team")

        if team in bye_teams and opponent is not None:
            raise RuntimeError(
                f"{player.get('name')}: bye team {team} has an opponent"
            )

        if team not in bye_teams and opponent is None:
            raise RuntimeError(
                f"{player.get('name')}: scheduled team {team} has no opponent"
            )

        if opponent is not None and opponent not in nfl_teams:
            raise RuntimeError(
                f"{player.get('name')}: invalid opponent {opponent}"
            )

        if opponent == team:
            raise RuntimeError(
                f"{player.get('name')}: team cannot play itself"
            )

    errors: list[str] = []
    warnings: list[str] = []

    # ------------------------------------------------------------------
    # Data integrity
    # ------------------------------------------------------------------

    gsis_ids = [
        str(p.get("gsis_id", "")).strip()
        for p in players
    ]

    missing_ids = [
        i for i, value in enumerate(gsis_ids)
        if not value
    ]

    duplicates = {
        value
        for value in gsis_ids
        if value and gsis_ids.count(value) > 1
    }

    if missing_ids:
        errors.append(
            f"{len(missing_ids)} players missing gsis_id"
        )

    if duplicates:
        errors.append(
            f"{len(duplicates)} duplicate gsis_id values"
        )

    print("\nDATA INTEGRITY")
    print(f"  Players:                 {len(players)}")
    print(f"  Unique GSIS IDs:         {len(set(gsis_ids))}")

    # ------------------------------------------------------------------
    # Rating validation
    # ------------------------------------------------------------------

    for player in players:
        name = player.get("name", "Unknown")

        for component in COMPONENTS:
            value = player.get(component)

            if value is None:
                errors.append(
                    f"{name}: missing {component}"
                )
                continue

            try:
                numeric = float(value)
            except (TypeError, ValueError):
                errors.append(
                    f"{name}: {component} is not numeric"
                )
                continue

            if not math.isfinite(numeric):
                errors.append(
                    f"{name}: {component} is NaN/Inf"
                )
                continue

            if not 0 <= numeric <= 100:
                errors.append(
                    f"{name}: {component}={numeric} outside 0–100"
                )

        probability = player.get("play_probability")

        try:
            probability = float(probability)
            if not 0 <= probability <= 1:
                errors.append(
                    f"{name}: play_probability={probability}"
                )
        except (TypeError, ValueError):
            errors.append(
                f"{name}: invalid play_probability"
            )

        opp_rank = player.get("opp_def_rank")

        try:
            opp_rank = int(opp_rank)
            if not 1 <= opp_rank <= 32:
                errors.append(
                    f"{name}: opp_def_rank={opp_rank}"
                )
        except (TypeError, ValueError):
            errors.append(
                f"{name}: invalid opp_def_rank"
            )

    print(f"  Rating failures:         {sum('score' in e or 'composite_rating' in e for e in errors)}")
    print(f"  Other validation errors: {len(errors)}")

    # ------------------------------------------------------------------
    # Position distribution
    # ------------------------------------------------------------------

    print("\nPOSITION DISTRIBUTION")

    position_players: dict[str, list[dict]] = {}

    for position in POSITIONS:
        position_players[position] = [
            p for p in players
            if str(p.get("position", "")).upper() == position
        ]

        print(
            f"  {position}: "
            f"{len(position_players[position])}"
        )

    # ------------------------------------------------------------------
    # Rating distribution
    # ------------------------------------------------------------------

    print("\nRATING DISTRIBUTION")

    for position in POSITIONS:
        ratings = [
            float(p["composite_rating"])
            for p in position_players[position]
            if isinstance(p.get("composite_rating"), (int, float))
        ]

        if not ratings:
            warnings.append(
                f"{position}: no valid composite ratings"
            )
            continue

        print(
            f"  {position}: "
            f"mean={statistics.mean(ratings):.2f} | "
            f"P10={percentile(ratings, .10):.2f} | "
            f"P25={percentile(ratings, .25):.2f} | "
            f"P50={percentile(ratings, .50):.2f} | "
            f"P75={percentile(ratings, .75):.2f} | "
            f"P90={percentile(ratings, .90):.2f} | "
            f"min={min(ratings):.2f} | "
            f"max={max(ratings):.2f}"
        )

        high_90 = sum(r >= 90 for r in ratings)
        high_80 = sum(r >= 80 for r in ratings)
        low_20 = sum(r < 20 for r in ratings)

        print(
            f"       >=90: {high_90} | "
            f">=80: {high_80} | "
            f"<20: {low_20}"
        )

    # ------------------------------------------------------------------
    # Component health
    # ------------------------------------------------------------------

    print("\nCOMPONENT HEALTH")

    for position in POSITIONS:
        position_rows = position_players[position]

        for component in COMPONENTS[:-1]:
            values = [
                float(p[component])
                for p in position_rows
                if isinstance(p.get(component), (int, float))
                and math.isfinite(float(p[component]))
            ]

            unique_values = len(set(values))

            if not values:
                warnings.append(
                    f"{position} {component}: no values"
                )
                status = "WARNING"
            elif unique_values <= 2:
                warnings.append(
                    f"{position} {component}: only "
                    f"{unique_values} unique values"
                )
                status = "WARNING"
            else:
                status = "OK"

            print(
                f"  {position} {component:<16} "
                f"{status} "
                f"(unique={unique_values})"
            )

    # ------------------------------------------------------------------
    # Top players
    # ------------------------------------------------------------------

    print("\nTOP 10 BY POSITION")

    for position in POSITIONS:
        ranked = sorted(
            position_players[position],
            key=lambda p: float(p.get("composite_rating", 0)),
            reverse=True,
        )[:10]

        print(f"\n  === {position} ===")

        for player in ranked:
            print(
                f"  {str(player.get('name', 'Unknown')):<24} "
                f"{float(player.get('composite_rating', 0)):6.2f} "
                f"EPA {float(player.get('epa_score', 0)):6.2f} "
                f"USE {float(player.get('usage_score', 0)):6.2f} "
                f"SNP {float(player.get('snap_score', 0)):6.2f} "
                f"RZ {float(player.get('red_zone_score', 0)):6.2f}"
            )

    # ------------------------------------------------------------------
    # Result
    # ------------------------------------------------------------------

    print("\n" + "=" * 72)

    if warnings:
        print("WARNINGS")
        for warning in warnings:
            print(f"  ! {warning}")

    if errors:
        print("\nERRORS")
        for error in errors[:50]:
            print(f"  ! {error}")

        if len(errors) > 50:
            print(
                f"  ... and {len(errors) - 50} more"
            )

        print("\nRESULT: FAIL")
        print("=" * 72)
        return 1

    print(
        f"\nRESULT: {'PASS WITH WARNINGS' if warnings else 'PASS'}"
    )
    print("=" * 72)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())