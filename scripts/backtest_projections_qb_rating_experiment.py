#!/usr/bin/env python3
"""
Gridiron Oracle — Historical Projection Backtest

Walk-forward validation of the projection architecture.

For each target week, only information available before that week is used
for projection features. Target-week fantasy points are the outcome.

Compares:
  LEGACY
    40% season average + 60% last-3 average
    × composite-rating multiplier

  CURRENT
    same production baseline
    + efficiency / CPOE
    + opportunity
    + matchup
    + environment

Usage:
  python3 scripts/backtest_projections.py
  python3 scripts/backtest_projections.py --season 2025 --start-week 5 --end-week 18
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import nflreadpy as nfl
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.model.ratings import compute_player_ratings
from src.model.team_environment import (
    build_team_environment,
    build_opponent_map,
    attach_team_environment,
)
from scripts.update_nfl_data import (
    fetch_red_zone_shares,
    fetch_snap_counts,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

POSITIONS = {"QB", "RB", "WR", "TE"}

EFFICIENCY_MAX = 0.06
CPOE_MAX = 0.04
OPPORTUNITY_MAX = 0.08
MATCHUP_MAX = 0.08
ENVIRONMENT_MAX = 0.05


def num(value, default=0.0):
    try:
        value = float(value)
        return value if np.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def clamp(value, low, high):
    return max(low, min(high, value))


def score_adjustment(score, max_adjustment):
    return ((num(score, 50.0) - 50.0) / 50.0) * max_adjustment


def _resolve_team_col(df):
    for col in ("recent_team", "team", "posteam", "fantasy_team"):
        if col in df.columns:
            return col
    return None


def _resolve_points_col(df):
    for col in (
        "fantasy_points_ppr",
        "fantasy_points",
        "fantasy_points_half_ppr",
    ):
        if col in df.columns:
            return col
    return None


# =============================================================================
# Historical production
# =============================================================================

def build_production_features(weekly, cutoff_week):
    hist = weekly[weekly["week"] < cutoff_week].copy()

    if hist.empty:
        return pd.DataFrame()

    points_col = _resolve_points_col(hist)
    if points_col is None:
        raise ValueError("Weekly data does not contain fantasy points.")

    hist = hist[hist["player_id"].notna()].copy()
    hist["gsis_id"] = hist["player_id"].astype(str).str.strip()
    hist[points_col] = pd.to_numeric(
        hist[points_col],
        errors="coerce",
    ).fillna(0.0)

    hist = hist.sort_values(["gsis_id", "week"])

    grouped = hist.groupby("gsis_id", group_keys=False)

    hist["last3_avg_pts"] = grouped[points_col].transform(
        lambda s: s.rolling(3, min_periods=1).mean()
    )

    team_col = _resolve_team_col(hist)

    result = (
        hist.groupby("gsis_id")
        .agg(
            season_avg_pts=(points_col, "mean"),
            last3_avg_pts=("last3_avg_pts", "last"),
            games_played=(points_col, "count"),
        )
        .reset_index()
    )

    if team_col:
        latest_team = (
            hist.sort_values("week")
            .groupby("gsis_id")[team_col]
            .last()
            .rename("historical_team")
        )
        result = result.merge(
            latest_team,
            on="gsis_id",
            how="left",
        )

    return result


# =============================================================================
# Historical usage
# =============================================================================

def build_usage_features(weekly, cutoff_week):
    hist = weekly[weekly["week"] < cutoff_week].copy()

    if hist.empty:
        return pd.DataFrame()

    hist = hist[hist["player_id"].notna()].copy()
    hist["gsis_id"] = hist["player_id"].astype(str).str.strip()

    team_col = _resolve_team_col(hist)
    if team_col is None:
        raise ValueError("Weekly data has no team field.")

    hist[team_col] = hist[team_col].fillna("").astype(str).str.strip()

    numeric = {
        "targets": ("targets", "receiving_targets"),
        "carries": ("carries", "rushing_attempts"),
        "air_yards": ("air_yards",),
    }

    for output, candidates in numeric.items():
        source = next(
            (c for c in candidates if c in hist.columns),
            None,
        )
        if source is None:
            hist[output] = 0.0
        else:
            hist[output] = pd.to_numeric(
                hist[source],
                errors="coerce",
            ).fillna(0.0)

    pw = (
        hist.groupby(
            ["gsis_id", "position", team_col, "week"],
            as_index=False,
        )
        .agg(
            targets=("targets", "sum"),
            carries=("carries", "sum"),
            air_yards=("air_yards", "sum"),
        )
    )

    team = (
        hist.groupby(
            [team_col, "week"],
            as_index=False,
        )
        .agg(
            team_targets=("targets", "sum"),
            team_carries=("carries", "sum"),
            team_air_yards=("air_yards", "sum"),
        )
    )

    pw = pw.merge(
        team,
        on=[team_col, "week"],
        how="left",
    )

    pw["target_share_week"] = np.where(
        pw["team_targets"] > 0,
        pw["targets"] / pw["team_targets"],
        np.nan,
    )

    pw["carry_share_week"] = np.where(
        pw["team_carries"] > 0,
        pw["carries"] / pw["team_carries"],
        np.nan,
    )

    pw["air_yards_share_week"] = np.where(
        pw["team_air_yards"].abs() > 0,
        pw["air_yards"] / pw["team_air_yards"],
        np.nan,
    )

    return (
        pw.groupby(
            ["gsis_id", "position"],
            as_index=False,
        )
        .agg(
            target_share=("target_share_week", "mean"),
            carry_share=("carry_share_week", "mean"),
            air_yards_share=("air_yards_share_week", "mean"),
        )
    )


# =============================================================================
# Historical snap / red-zone opportunity
# =============================================================================

def build_snap_features(season, cutoff_week):
    if cutoff_week <= 1:
        return pd.DataFrame(
            columns=["gsis_id", "snap_pct"]
        )

    frames = []

    for week in range(1, cutoff_week):
        try:
            frame = fetch_snap_counts(
                season,
                week,
            )

            if frame is None or frame.empty:
                continue

            frame = frame.copy()

            id_col = next(
                (
                    c for c in (
                        "gsis_id",
                        "player_id",
                    )
                    if c in frame.columns
                ),
                None,
            )

            snap_col = next(
                (
                    c for c in (
                        "snap_pct",
                        "offense_snap_pct",
                        "snap_percentage",
                    )
                    if c in frame.columns
                ),
                None,
            )

            if not id_col or not snap_col:
                continue

            frame["gsis_id"] = (
                frame[id_col]
                .astype(str)
                .str.strip()
            )

            frame["snap_pct"] = pd.to_numeric(
                frame[snap_col],
                errors="coerce",
            )

            frames.append(
                frame[
                    ["gsis_id", "snap_pct"]
                ]
            )

        except Exception as exc:
            log.debug(
                "Snap data unavailable for Week %s: %s",
                week,
                exc,
            )

    if not frames:
        return pd.DataFrame(
            columns=["gsis_id", "snap_pct"]
        )

    data = pd.concat(
        frames,
        ignore_index=True,
    )

    return (
        data.groupby("gsis_id", as_index=False)
        .agg(
            snap_pct=("snap_pct", "mean")
        )
    )


def build_red_zone_features(season, cutoff_week):
    if cutoff_week <= 1:
        return pd.DataFrame(
            columns=[
                "gsis_id",
                "red_zone_share",
            ]
        )

    frames = []

    for week in range(1, cutoff_week):
        try:
            frame = fetch_red_zone_shares(
                season,
                week,
            )

            if frame is None or frame.empty:
                continue

            frame = frame.copy()

            id_col = next(
                (
                    c for c in (
                        "gsis_id",
                        "player_id",
                    )
                    if c in frame.columns
                ),
                None,
            )

            rz_col = next(
                (
                    c for c in (
                        "red_zone_share",
                        "rz_share",
                    )
                    if c in frame.columns
                ),
                None,
            )

            if not id_col or not rz_col:
                continue

            frame["gsis_id"] = (
                frame[id_col]
                .astype(str)
                .str.strip()
            )

            frame["red_zone_share"] = pd.to_numeric(
                frame[rz_col],
                errors="coerce",
            )

            frames.append(
                frame[
                    ["gsis_id", "red_zone_share"]
                ]
            )

        except Exception as exc:
            log.debug(
                "Red-zone data unavailable for Week %s: %s",
                week,
                exc,
            )

    if not frames:
        return pd.DataFrame(
            columns=[
                "gsis_id",
                "red_zone_share",
            ]
        )

    data = pd.concat(
        frames,
        ignore_index=True,
    )

    return (
        data.groupby("gsis_id", as_index=False)
        .agg(
            red_zone_share=("red_zone_share", "mean")
        )
    )


# =============================================================================
# EPA / CPOE
# =============================================================================

def build_epa_cpoe_features(pbp, cutoff_week):
    hist = pbp[pbp["week"] < cutoff_week].copy()

    if hist.empty:
        return pd.DataFrame()

    records = []

    if {
        "pass_attempt",
        "passer_id",
        "epa",
    }.issubset(hist.columns):
        passes = hist[
            (hist["pass_attempt"] == 1)
            & hist["passer_id"].notna()
            & hist["epa"].notna()
        ]

        if not passes.empty:
            records.append(
                passes.groupby("passer_id")["epa"]
                .mean()
                .rename("qb_epa")
                .to_frame()
                .rename_axis("gsis_id")
                .reset_index()
            )

    if {
        "rush_attempt",
        "rusher_id",
        "epa",
    }.issubset(hist.columns):
        rushes = hist[
            (hist["rush_attempt"] == 1)
            & hist["rusher_id"].notna()
            & hist["epa"].notna()
        ]

        if not rushes.empty:
            records.append(
                rushes.groupby("rusher_id")["epa"]
                .mean()
                .rename("rush_epa")
                .to_frame()
                .rename_axis("gsis_id")
                .reset_index()
            )

    if {
        "pass_attempt",
        "receiver_id",
        "epa",
    }.issubset(hist.columns):
        recs = hist[
            (hist["pass_attempt"] == 1)
            & hist["receiver_id"].notna()
            & hist["epa"].notna()
        ]

        if not recs.empty:
            records.append(
                recs.groupby("receiver_id")["epa"]
                .mean()
                .rename("rec_epa")
                .to_frame()
                .rename_axis("gsis_id")
                .reset_index()
            )

    if not records:
        return pd.DataFrame(
            columns=[
                "gsis_id",
                "epa_per_play",
                "cpoe",
                "cpoe_attempts",
            ]
        )

    epa = pd.concat(
        [
            x.set_index("gsis_id")
            for x in records
        ],
        axis=1,
    )

    epa["epa_per_play"] = epa.mean(
        axis=1,
        skipna=True,
    )

    epa = epa[
        ["epa_per_play"]
    ].reset_index()

    if {
        "pass_attempt",
        "passer_id",
        "cpoe",
    }.issubset(hist.columns):

        passes = hist[
            (hist["pass_attempt"] == 1)
            & hist["passer_id"].notna()
            & hist["cpoe"].notna()
        ]

        if not passes.empty:
            cpoe = (
                passes.groupby("passer_id")
                .agg(
                    cpoe=("cpoe", "mean"),
                    cpoe_attempts=("cpoe", "count"),
                )
                .reset_index()
                .rename(
                    columns={
                        "passer_id": "gsis_id"
                    }
                )
            )

            cpoe["gsis_id"] = (
                cpoe["gsis_id"]
                .astype(str)
                .str.strip()
            )

            epa = epa.merge(
                cpoe,
                on="gsis_id",
                how="left",
            )

    if "cpoe" not in epa.columns:
        epa["cpoe"] = 0.0

    if "cpoe_attempts" not in epa.columns:
        epa["cpoe_attempts"] = 0

    return epa


# =============================================================================
# Historical team / matchup context
# =============================================================================

def build_context(
    pbp,
    schedule,
    cutoff_week,
):
    """
    Build historical team environment and target-week matchup context.

    Team metrics use only PBP before cutoff_week.
    Target-week schedule is used only to identify opponents.
    """

    hist_pbp = pbp[
        pbp["week"] < cutoff_week
    ].copy()

    if hist_pbp.empty:
        return pd.DataFrame()

    required = {
        "posteam",
        "defteam",
        "epa",
        "success",
        "pass_attempt",
        "rush_attempt",
    }

    missing = required - set(hist_pbp.columns)

    if missing:
        raise ValueError(
            "Historical PBP missing team-environment fields: "
            + ", ".join(sorted(missing))
        )

    for col in [
        "epa",
        "success",
        "pass_attempt",
        "rush_attempt",
    ]:
        hist_pbp[col] = pd.to_numeric(
            hist_pbp[col],
            errors="coerce",
        )

    hist_pbp["pass_attempt"] = (
        hist_pbp["pass_attempt"]
        .fillna(0)
    )

    hist_pbp["rush_attempt"] = (
        hist_pbp["rush_attempt"]
        .fillna(0)
    )

    hist_pbp = hist_pbp[
        hist_pbp["posteam"].notna()
        & hist_pbp["defteam"].notna()
        & hist_pbp["epa"].notna()
    ].copy()

    for col in ["posteam", "defteam"]:
        hist_pbp[col] = (
            hist_pbp[col]
            .astype(str)
            .str.strip()
        )

    # -------------------------------------------------------------------------
    # Overall offense
    # -------------------------------------------------------------------------

    offense = (
        hist_pbp
        .groupby("posteam")
        .agg(
            team_off_epa=("epa", "mean"),
            team_off_success_rate=("success", "mean"),
            team_plays=("epa", "count"),
        )
        .reset_index()
        .rename(columns={"posteam": "team"})
    )

    # -------------------------------------------------------------------------
    # Overall defense
    # -------------------------------------------------------------------------

    defense = (
        hist_pbp
        .groupby("defteam")
        .agg(
            team_def_epa=("epa", "mean"),
            team_def_success_rate=("success", "mean"),
        )
        .reset_index()
        .rename(columns={"defteam": "team"})
    )

    # -------------------------------------------------------------------------
    # Passing
    # -------------------------------------------------------------------------

    passing = hist_pbp[
        hist_pbp["pass_attempt"] == 1
    ].copy()

    pass_offense = (
        passing
        .groupby("posteam")
        .agg(
            team_pass_epa=("epa", "mean"),
            team_pass_success_rate=("success", "mean"),
        )
        .reset_index()
        .rename(columns={"posteam": "team"})
    )

    pass_defense = (
        passing
        .groupby("defteam")
        .agg(
            pass_def_epa=("epa", "mean"),
            pass_def_success_rate=("success", "mean"),
        )
        .reset_index()
        .rename(columns={"defteam": "team"})
    )

    # -------------------------------------------------------------------------
    # Rushing defense
    # -------------------------------------------------------------------------

    rushing = hist_pbp[
        hist_pbp["rush_attempt"] == 1
    ].copy()

    rush_defense = (
        rushing
        .groupby("defteam")
        .agg(
            rush_def_epa=("epa", "mean"),
            rush_def_success_rate=("success", "mean"),
        )
        .reset_index()
        .rename(columns={"defteam": "team"})
    )

    # -------------------------------------------------------------------------
    # Combine team metrics
    # -------------------------------------------------------------------------

    teams = pd.DataFrame(
        {
            "team": sorted(
                set(hist_pbp["posteam"])
                | set(hist_pbp["defteam"])
            )
        }
    )

    for frame in (
        offense,
        defense,
        pass_offense,
        pass_defense,
        rush_defense,
    ):
        teams = teams.merge(
            frame,
            on="team",
            how="left",
        )

    defaults = {
        "team_off_epa": 0.0,
        "team_off_success_rate": 0.50,
        "team_plays": 0,
        "team_def_epa": 0.0,
        "team_def_success_rate": 0.50,
        "team_pass_epa": 0.0,
        "team_pass_success_rate": 0.50,
        "pass_def_epa": 0.0,
        "pass_def_success_rate": 0.50,
        "rush_def_epa": 0.0,
        "rush_def_success_rate": 0.50,
    }

    for col, default in defaults.items():
        teams[col] = pd.to_numeric(
            teams[col],
            errors="coerce",
        ).fillna(default)

    # -------------------------------------------------------------------------
    # Continuous 0-100 team scores
    # -------------------------------------------------------------------------

    def percentile_score(series, invert=False):
        values = pd.to_numeric(
            series,
            errors="coerce",
        )

        rank = (
            values.rank(
                pct=True,
                method="average",
            )
            * 100.0
        )

        if invert:
            rank = 100.0 - rank

        return rank.clip(
            0,
            100,
        ).fillna(50.0)

    teams["defense_score"] = (
        0.50 * percentile_score(
            teams["team_def_epa"],
            invert=True,
        )
        + 0.50 * percentile_score(
            teams["team_def_success_rate"],
            invert=True,
        )
    )

    teams["pass_defense_score"] = (
        0.50 * percentile_score(
            teams["pass_def_epa"],
            invert=True,
        )
        + 0.50 * percentile_score(
            teams["pass_def_success_rate"],
            invert=True,
        )
    )

    teams["rush_defense_score"] = (
        0.50 * percentile_score(
            teams["rush_def_epa"],
            invert=True,
        )
        + 0.50 * percentile_score(
            teams["rush_def_success_rate"],
            invert=True,
        )
    )

    teams["offense_score"] = (
        0.50 * percentile_score(
            teams["team_off_epa"],
        )
        + 0.50 * percentile_score(
            teams["team_off_success_rate"],
        )
    )

    # -------------------------------------------------------------------------
    # Target-week opponents
    #
    # Build directly from schedule rather than build_opponent_map(), avoiding
    # duplicate/Series values from the helper.
    # -------------------------------------------------------------------------

    target_schedule = schedule[
        schedule["week"].eq(cutoff_week)
    ].copy()

    if target_schedule.empty:
        return pd.DataFrame()

    required_schedule = {
        "home_team",
        "away_team",
    }

    missing_schedule = (
        required_schedule
        - set(target_schedule.columns)
    )

    if missing_schedule:
        raise ValueError(
            "Schedule missing opponent fields: "
            + ", ".join(sorted(missing_schedule))
        )

    opponent_map = {}

    for _, game in target_schedule.iterrows():
        home = str(
            game["home_team"]
        ).strip()

        away = str(
            game["away_team"]
        ).strip()

        if not home or not away:
            continue

        opponent_map[home] = away
        opponent_map[away] = home

    # -------------------------------------------------------------------------
    # Attach opponent context
    # -------------------------------------------------------------------------

    team_lookup = (
        teams
        .set_index("team")
    )

    rows = []

    for team, opponent in opponent_map.items():

        if team not in team_lookup.index:
            continue

        if opponent not in team_lookup.index:
            continue

        own = team_lookup.loc[team]
        opp = team_lookup.loc[opponent]

        rows.append(
            {
                "team": team,

                "team_off_epa": own["team_off_epa"],
                "team_off_success_rate": own[
                    "team_off_success_rate"
                ],
                "team_plays": own["team_plays"],

                "team_pass_epa": own[
                    "team_pass_epa"
                ],
                "team_pass_success_rate": own[
                    "team_pass_success_rate"
                ],

                "team_def_epa": own[
                    "team_def_epa"
                ],
                "team_def_success_rate": own[
                    "team_def_success_rate"
                ],

                "pass_def_epa": own[
                    "pass_def_epa"
                ],
                "pass_def_success_rate": own[
                    "pass_def_success_rate"
                ],

                "rush_def_epa": own[
                    "rush_def_epa"
                ],
                "rush_def_success_rate": own[
                    "rush_def_success_rate"
                ],

                "defense_score": own[
                    "defense_score"
                ],
                "pass_defense_score": own[
                    "pass_defense_score"
                ],
                "rush_defense_score": own[
                    "rush_defense_score"
                ],
                "offense_score": own[
                    "offense_score"
                ],

                "opponent_team": opponent,

                "opp_def_epa": opp[
                    "team_def_epa"
                ],
                "opp_def_success_rate": opp[
                    "team_def_success_rate"
                ],

                "opp_pass_def_epa": opp[
                    "pass_def_epa"
                ],
                "opp_pass_def_success_rate": opp[
                    "pass_def_success_rate"
                ],

                "opp_rush_def_epa": opp[
                    "rush_def_epa"
                ],
                "opp_rush_def_success_rate": opp[
                    "rush_def_success_rate"
                ],

                "opp_defense_score": opp[
                    "defense_score"
                ],
                "opp_pass_defense_score": opp[
                    "pass_defense_score"
                ],
                "opp_rush_defense_score": opp[
                    "rush_defense_score"
                ],
            }
        )

    return pd.DataFrame(rows)



# =============================================================================
# Ratings
# =============================================================================

def add_ratings(df):
    return compute_player_ratings(df)


# =============================================================================
# Projection models
# =============================================================================

def get_base_projection(row):
    season = num(
        row.get("season_avg_pts")
    )
    last3 = num(
        row.get("last3_avg_pts"),
        season,
    )

    return (
        0.40 * season
        + 0.60 * last3
    )


def old_project_row(row):
    base = get_base_projection(row)

    if base <= 0:
        return 0.0

    rating = num(
        row.get("composite_rating"),
        50.0,
    )

    multiplier = (
        0.8
        + (rating / 100.0) * 0.4
    )

    return max(
        0.0,
        base * multiplier,
    )


def new_projection_components(row):
    base = get_base_projection(row)

    if base <= 0:
        return {
            "base_projection": 0.0,
            "efficiency_adjustment": 0.0,
            "cpoe_adjustment": 0.0,
            "opportunity_adjustment": 0.0,
            "matchup_adjustment": 0.0,
            "environment_adjustment": 0.0,
            "new_projection": 0.0,
        }

    position = row.get("position")

    # Efficiency
    epa_adjustment = score_adjustment(
        row.get("epa_score"),
        EFFICIENCY_MAX,
    )

    cpoe_adjustment = 0.0

    if position == "QB":
        attempts = num(
            row.get("cpoe_attempts")
        )

        if attempts >= 100:
            cpoe_adjustment = clamp(
                num(row.get("cpoe")) / 100.0,
                -CPOE_MAX,
                CPOE_MAX,
            )

    efficiency = clamp(
        epa_adjustment + cpoe_adjustment,
        -0.08,
        0.08,
    )

    # Opportunity
    usage = num(
        row.get("usage_score"),
        50.0,
    )
    snap = num(
        row.get("snap_score"),
        50.0,
    )
    rz = num(
        row.get("red_zone_score"),
        50.0,
    )

    if position == "RB":
        opportunity = (
            0.45 * ((usage - 50.0) / 50.0)
            + 0.25 * ((snap - 50.0) / 50.0)
            + 0.30 * ((rz - 50.0) / 50.0)
        )
    elif position in {"WR", "TE"}:
        opportunity = (
            0.50 * ((usage - 50.0) / 50.0)
            + 0.30 * ((snap - 50.0) / 50.0)
            + 0.20 * ((rz - 50.0) / 50.0)
        )
    elif position == "QB":
        opportunity = (
            0.55 * ((usage - 50.0) / 50.0)
            + 0.45 * ((snap - 50.0) / 50.0)
        )
    else:
        opportunity = 0.0

    opportunity = clamp(
        opportunity,
        -OPPORTUNITY_MAX,
        OPPORTUNITY_MAX,
    )

    # Matchup
    if position in {"K", "DST"}:
        matchup = 0.0
    else:
        matchup = clamp(
            score_adjustment(
                row.get("matchup_score"),
                MATCHUP_MAX,
            ),
            -MATCHUP_MAX,
            MATCHUP_MAX,
        )

    # Environment
    if position in {"QB", "WR", "TE"}:
        pass_epa = num(
            row.get("team_pass_epa")
        )
        pass_success = num(
            row.get("team_pass_success_rate"),
            0.50,
        )

        epa_signal = clamp(
            pass_epa / 0.15,
            -1.0,
            1.0,
        )

        success_signal = clamp(
            (pass_success - 0.50) / 0.10,
            -1.0,
            1.0,
        )

        environment = clamp(
            (
                0.60 * epa_signal
                + 0.40 * success_signal
            ) * 0.05,
            -ENVIRONMENT_MAX,
            ENVIRONMENT_MAX,
        )

    elif position in {"RB", "K"}:
        environment = clamp(
            score_adjustment(
                row.get("team_environment_score"),
                ENVIRONMENT_MAX,
            ),
            -ENVIRONMENT_MAX,
            ENVIRONMENT_MAX,
        )
    else:
        environment = 0.0

    multiplier = (
        1.0
        + efficiency
        + opportunity
        + matchup
        + environment
    )

    projection = max(
        0.0,
        base * multiplier,
    )

    return {
        "base_projection": base,
        "efficiency_adjustment": efficiency,
        "cpoe_adjustment": cpoe_adjustment,
        "opportunity_adjustment": opportunity,
        "matchup_adjustment": matchup,
        "environment_adjustment": environment,
        "new_projection": projection,
    }


# =============================================================================
# Target week
# =============================================================================

def evaluate_week(
    weekly,
    pbp,
    schedule,
    season,
    week,
):
    log.info(
        "Backtesting Week %s...",
        week,
    )

    features = build_production_features(
        weekly,
        week,
    )

    usage = build_usage_features(
        weekly,
        week,
    )

    epa = build_epa_cpoe_features(
        pbp,
        week,
    )

    snap = build_snap_features(
        season,
        week,
    )

    red_zone = build_red_zone_features(
        season,
        week,
    )

    context = build_context(
        pbp,
        schedule,
        week,
    )

    target = weekly[
        weekly["week"] == week
    ].copy()

    if target.empty:
        raise ValueError(
            f"No weekly data for Week {week}"
        )

    team_col = _resolve_team_col(target)
    points_col = _resolve_points_col(target)

    if team_col is None:
        raise ValueError(
            f"No team field. Columns: {', '.join(target.columns)}"
        )

    if points_col is None:
        raise ValueError(
            f"No fantasy-points field. Columns: {', '.join(target.columns)}"
        )

    target["gsis_id"] = (
        target["player_id"]
        .astype(str)
        .str.strip()
    )

    target["team"] = (
        target[team_col]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    target["actual_pts"] = pd.to_numeric(
        target[points_col],
        errors="coerce",
    )

    target = target[
        target["gsis_id"].ne("")
        & target["position"].isin(POSITIONS)
        & target["team"].ne("")
    ].copy()

    target = (
        target
        .sort_values("gsis_id")
        .drop_duplicates(
            "gsis_id",
            keep="last",
        )
    )

    df = target[
        [
            "gsis_id",
            "position",
            "team",
            "actual_pts",
        ]
    ].copy()

    # -------------------------------------------------------------------------
    # Historical team
    #
    # Last team recorded before the target week. This is the team whose
    # environment should affect the historical projection.
    # -------------------------------------------------------------------------

    historical = weekly[
        weekly["week"] < week
    ].copy()

    historical["gsis_id"] = (
        historical["player_id"]
        .astype(str)
        .str.strip()
    )

    historical["historical_team"] = (
        historical[team_col]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    historical = historical[
        historical["gsis_id"].ne("")
        & historical["historical_team"].ne("")
    ].copy()

    historical = (
        historical
        .sort_values(
            [
                "gsis_id",
                "week",
            ]
        )
        .drop_duplicates(
            "gsis_id",
            keep="last",
        )
    )

    historical_team = historical[
        [
            "gsis_id",
            "historical_team",
        ]
    ]

    df = df.merge(
        historical_team,
        on="gsis_id",
        how="left",
    )

    df["historical_team"] = (
        df["historical_team"]
        .fillna(df["team"])
        .astype(str)
        .str.strip()
    )

    # -------------------------------------------------------------------------
    # Leakage-safe position baseline
    #
    # Mean actual fantasy points by position using only weeks before the
    # target week. Target-week results are never included.
    # -------------------------------------------------------------------------

    historical_points = pd.to_numeric(
        historical[points_col],
        errors="coerce",
    )

    historical_position_means = (
        pd.DataFrame(
            {
                "position": historical["position"],
                "historical_pts": historical_points,
            }
        )
        .loc[
            lambda x: x["position"].isin(POSITIONS)
        ]
        .dropna(
            subset=[
                "position",
                "historical_pts",
            ]
        )
        .groupby("position")["historical_pts"]
        .mean()
        .rename("position_mean_pts")
        .reset_index()
    )

    df = df.merge(
        historical_position_means,
        on="position",
        how="left",
    )

    # -------------------------------------------------------------------------
    # Production
    # -------------------------------------------------------------------------

    df = df.merge(
        features,
        on="gsis_id",
        how="inner",
        suffixes=("", "_prod"),
    )

    # Keep the explicitly derived historical team.
    if "historical_team_prod" in df.columns:
        df["historical_team"] = (
            df["historical_team"]
            .replace("", np.nan)
            .fillna(df["historical_team_prod"])
        )
        df.drop(
            columns=["historical_team_prod"],
            inplace=True,
        )

    # -------------------------------------------------------------------------
    # Usage
    # -------------------------------------------------------------------------

    df = df.merge(
        usage,
        on=[
            "gsis_id",
            "position",
        ],
        how="left",
    )

    # -------------------------------------------------------------------------
    # EPA / CPOE
    # -------------------------------------------------------------------------

    df = df.merge(
        epa,
        on="gsis_id",
        how="left",
    )

    # -------------------------------------------------------------------------
    # Snap
    # -------------------------------------------------------------------------

    df = df.merge(
        snap,
        on="gsis_id",
        how="left",
    )

    # -------------------------------------------------------------------------
    # Red zone
    # -------------------------------------------------------------------------

    df = df.merge(
        red_zone,
        on="gsis_id",
        how="left",
    )

    # -------------------------------------------------------------------------
    # Context
    # -------------------------------------------------------------------------

    if context.empty:
        raise ValueError(
            f"Week {week}: build_context() returned no rows."
        )

    context = context.copy()

    context["team"] = (
        context["team"]
        .astype(str)
        .str.strip()
    )

    context = context.rename(
        columns={
            "team": "historical_team",
        }
    )

    df = df.merge(
        context,
        on="historical_team",
        how="left",
    )

    # -------------------------------------------------------------------------
    # Defaults
    # -------------------------------------------------------------------------

    defaults = {
        "snap_pct": 0.50,
        "red_zone_share": 0.0,
        "target_share": 0.0,
        "carry_share": 0.0,
        "air_yards_share": 0.0,
        "epa_per_play": 0.0,
        "cpoe": 0.0,
        "cpoe_attempts": 0,

        "matchup_score": 50.0,
        "team_environment_score": 50.0,

        "team_off_epa": 0.0,
        "team_off_success_rate": 0.50,
        "team_plays": 0,

        "team_pass_epa": 0.0,
        "team_pass_success_rate": 0.50,

        "team_def_epa": 0.0,
        "team_def_success_rate": 0.50,

        "opponent_team": None,

        "opp_def_epa": 0.0,
        "opp_def_success_rate": 0.50,

        "opp_pass_def_epa": 0.0,
        "opp_pass_def_success_rate": 0.50,

        "opp_rush_def_epa": 0.0,
        "opp_rush_def_success_rate": 0.50,

        "opp_defense_score": 50.0,
        "opp_pass_defense_score": 50.0,
        "opp_rush_defense_score": 50.0,
    }

    for col, default in defaults.items():

        if col not in df.columns:
            df[col] = default

        df[col] = df[col].fillna(default)

    # -------------------------------------------------------------------------
    # Position-aware matchup
    # -------------------------------------------------------------------------

    df["matchup_score"] = 50.0

    pass_positions = {
        "QB",
        "WR",
        "TE",
    }

    df.loc[
        df["position"].isin(pass_positions),
        "matchup_score",
    ] = df.loc[
        df["position"].isin(pass_positions),
        "opp_pass_defense_score",
    ]

    df.loc[
        df["position"].eq("RB"),
        "matchup_score",
    ] = df.loc[
        df["position"].eq("RB"),
        "opp_rush_defense_score",
    ]

    # -------------------------------------------------------------------------
    # Offensive environment
    # -------------------------------------------------------------------------

    if "offense_score" in df.columns:
        df["team_environment_score"] = (
            pd.to_numeric(
                df["offense_score"],
                errors="coerce",
            )
            .fillna(50.0)
        )

    # -------------------------------------------------------------------------
    # Context diagnostics
    # -------------------------------------------------------------------------

    total = len(df)

    context_attached = (
        pd.to_numeric(
            df["team_plays"],
            errors="coerce",
        )
        > 0
    )

    opponents_attached = (
        df["opponent_team"].notna()
        & df["opponent_team"].astype(str).ne("")
    )

    if total:
        log.info(
            "Week %s context attachment: "
            "%s/%s players have team context, "
            "%s/%s have opponents",
            week,
            int(context_attached.sum()),
            total,
            int(opponents_attached.sum()),
            total,
        )

    if total and (
        context_attached.sum() == 0
        or opponents_attached.sum() == 0
    ):
        raise ValueError(
            f"Week {week}: historical context failed to attach. "
            f"team_context={int(context_attached.sum())}/{total}, "
            f"opponents={int(opponents_attached.sum())}/{total}"
        )

    # -------------------------------------------------------------------------
    # Numeric normalization
    # -------------------------------------------------------------------------

    numeric_cols = [
        "snap_pct",
        "red_zone_share",
        "target_share",
        "carry_share",
        "air_yards_share",
        "epa_per_play",
        "cpoe",
        "cpoe_attempts",
    ]

    for col in numeric_cols:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce",
        )

    df["cpoe"] = df["cpoe"].fillna(0.0)

    df["cpoe_attempts"] = (
        df["cpoe_attempts"]
        .fillna(0)
        .astype(int)
    )

    df["position_mean_pts"] = pd.to_numeric(
        df["position_mean_pts"],
        errors="coerce",
    )

    # -------------------------------------------------------------------------
    # Ratings
    # -------------------------------------------------------------------------

    df = add_ratings(df)

    # -------------------------------------------------------------------------
    # Projections
    # -------------------------------------------------------------------------

    df["baseline_projection"] = df.apply(
        get_base_projection,
        axis=1,
    )

    df["old_projection"] = df.apply(
        old_project_row,
        axis=1,
    )

    components = df.apply(
        new_projection_components,
        axis=1,
    )

    component_df = pd.DataFrame(
        components.tolist(),
        index=df.index,
    )

    df = pd.concat(
        [
            df,
            component_df,
        ],
        axis=1,
    )

    for col in [
        "actual_pts",
        "baseline_projection",
        "old_projection",
        "new_projection",
    ]:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce",
        )

    df = df.dropna(
        subset=[
            "actual_pts",
            "old_projection",
            "new_projection",
        ]
    )

    log.info(
        "Week %s: evaluated %s players",
        week,
        len(df),
    )

    return df

# =============================================================================
# Diagnostics
# =============================================================================

SHRINKAGE_VALUES = [
    0.50,
    0.60,
    0.70,
    0.75,
    0.80,
    0.85,
    0.90,
    0.95,
    1.00,
]

CALIBRATION_BINS = [
    -np.inf,
    5,
    10,
    15,
    20,
    np.inf,
]

CALIBRATION_LABELS = [
    "<5",
    "5-10",
    "10-15",
    "15-20",
    "20+",
]

CALIBRATION_MIN_BUCKET_N = 25
CALIBRATION_SHRINKAGE_K = 50.0


def metric_row(group, projection):
    """
    Calculate standard projection metrics.

    Canonical backtest outcome column:
        actual_pts
    """

    prediction = pd.to_numeric(
        group[projection],
        errors="coerce",
    )

    actual = pd.to_numeric(
        group["actual_pts"],
        errors="coerce",
    )

    mask = (
        prediction.notna()
        & actual.notna()
    )

    prediction = prediction.loc[mask]
    actual = actual.loc[mask]

    if prediction.empty:
        return {
            "projection": projection,
            "n": 0,
            "mae": np.nan,
            "rmse": np.nan,
            "bias": np.nan,
            "mean_projection": np.nan,
            "mean_actual": np.nan,
            "correlation": np.nan,
        }

    error = prediction - actual

    return {
        "projection": projection,
        "n": len(prediction),
        "mae": error.abs().mean(),
        "rmse": np.sqrt(
            (error ** 2).mean()
        ),
        "bias": error.mean(),
        "mean_projection": prediction.mean(),
        "mean_actual": actual.mean(),
        "correlation": (
            prediction.corr(actual)
            if len(prediction) > 1
            else np.nan
        ),
    }


def summarize(results):
    """
    Summarize legacy and current production models overall
    and by position.
    """

    rows = []

    for projection in (
        "old_projection",
        "new_projection",
    ):
        rows.append(
            {
                "position": "ALL",
                **metric_row(
                    results,
                    projection,
                ),
            }
        )

        for position, group in results.groupby(
            "position",
            dropna=False,
        ):
            rows.append(
                {
                    "position": position,
                    **metric_row(
                        group,
                        projection,
                    ),
                }
            )

    return pd.DataFrame(rows)


def calibration(results):
    """
    Calibration of the current production model by projected-points bucket.
    """

    projection = "new_projection"

    if projection not in results.columns:
        raise ValueError(
            f"Missing calibration projection: {projection}"
        )

    df = results.copy()

    df[projection] = pd.to_numeric(
        df[projection],
        errors="coerce",
    )

    df["actual_pts"] = pd.to_numeric(
        df["actual_pts"],
        errors="coerce",
    )

    df = df.dropna(
        subset=[
            projection,
            "actual_pts",
        ]
    ).copy()

    df["bucket"] = pd.cut(
        df[projection],
        bins=CALIBRATION_BINS,
        labels=CALIBRATION_LABELS,
        right=False,
    )

    rows = []

    for bucket in CALIBRATION_LABELS:
        group = df[
            df["bucket"] == bucket
        ]

        if group.empty:
            continue

        rows.append(
            {
                "bucket": bucket,
                **metric_row(
                    group,
                    projection,
                ),
            }
        )

    return pd.DataFrame(rows)


# =============================================================================
# QB-only composite-rating scale experiment
# =============================================================================

QB_RATING_SCALES = (0.00, 0.25, 0.50, 0.75, 1.00)

def build_qb_rating_scale_predictions(results):
    """
    Diagnostic-only QB rating sweep using already walk-forward-evaluated rows.

    Scale 0.00 is the unadjusted baseline. Scale 1.00 reproduces the existing
    old-model rating adjustment magnitude, but applies it to QBs only.
    Non-QBs remain exactly at baseline for every candidate.
    """
    required = {"position", "baseline_projection", "composite_rating", "actual_pts"}
    missing = required - set(results.columns)
    if missing:
        raise ValueError(
            "Missing QB rating experiment fields: " + ", ".join(sorted(missing))
        )

    out = results.copy()
    baseline = pd.to_numeric(out["baseline_projection"], errors="coerce")
    rating = pd.to_numeric(out["composite_rating"], errors="coerce").fillna(50.0)
    qb_mask = out["position"].eq("QB")
    centered_rating = ((rating - 50.0) / 50.0) * 0.40

    for scale in QB_RATING_SCALES:
        projection = baseline.copy()
        projection.loc[qb_mask] = (
            baseline.loc[qb_mask] * (1.0 + scale * centered_rating.loc[qb_mask])
        )
        out[f"qb_rating_scale_{scale:.2f}"] = projection.clip(lower=0.0)

    return out

def summarize_qb_rating_scale_predictions(results):
    """Return aggregate and position-specific metrics for each QB rating scale."""
    rows = []
    for scale in QB_RATING_SCALES:
        projection = f"qb_rating_scale_{scale:.2f}"
        for position, group in [("ALL", results), *list(results.groupby("position", dropna=False))]:
            metrics = metric_row(group, projection)
            rows.append({"position": position, "rating_scale": scale, **metrics})
    return pd.DataFrame(rows)


def adjustment_summary(results):
    """
    Summarize the magnitude and direction of each current-model adjustment.
    """

    adjustment_columns = [
        "efficiency_adjustment",
        "cpoe_adjustment",
        "opportunity_adjustment",
        "matchup_adjustment",
        "environment_adjustment",
    ]

    rows = []

    for column in adjustment_columns:
        if column not in results.columns:
            continue

        values = pd.to_numeric(
            results[column],
            errors="coerce",
        ).dropna()

        if values.empty:
            continue

        rows.append(
            {
                "adjustment": column,
                "mean": values.mean(),
                "median": values.median(),
                "mean_abs": values.abs().mean(),
                "min": values.min(),
                "max": values.max(),
            }
        )

    return pd.DataFrame(rows)


# =============================================================================
# Ablation diagnostics
# =============================================================================

def build_ablation_predictions(df):
    """
    Build isolated context-adjustment variants from the existing
    walk-forward results.

    These are diagnostic models only. They do not modify production
    projection code.
    """

    out = df.copy()

    base = pd.to_numeric(
        out["base_projection"],
        errors="coerce",
    ).fillna(0.0)

    efficiency = pd.to_numeric(
        out["efficiency_adjustment"],
        errors="coerce",
    ).fillna(0.0)

    opportunity = pd.to_numeric(
        out["opportunity_adjustment"],
        errors="coerce",
    ).fillna(0.0)

    matchup = pd.to_numeric(
        out["matchup_adjustment"],
        errors="coerce",
    ).fillna(0.0)

    environment = pd.to_numeric(
        out["environment_adjustment"],
        errors="coerce",
    ).fillna(0.0)

    out["ablation_baseline"] = base

    out["ablation_efficiency"] = (
        base
        * (1.0 + efficiency)
    )

    out["ablation_opportunity"] = (
        base
        * (1.0 + opportunity)
    )

    out["ablation_matchup"] = (
        base
        * (1.0 + matchup)
    )

    out["ablation_environment"] = (
        base
        * (1.0 + environment)
    )

    out["ablation_efficiency_opportunity"] = (
        base
        * (
            1.0
            + efficiency
            + opportunity
        )
    )

    out["ablation_context"] = (
        base
        * (
            1.0
            + efficiency
            + opportunity
            + matchup
            + environment
        )
    )

    return out


ABLATION_PROJECTIONS = [
    (
        "baseline",
        "ablation_baseline",
    ),
    (
        "+ efficiency",
        "ablation_efficiency",
    ),
    (
        "+ opportunity",
        "ablation_opportunity",
    ),
    (
        "+ matchup",
        "ablation_matchup",
    ),
    (
        "+ environment",
        "ablation_environment",
    ),
    (
        "efficiency + opportunity",
        "ablation_efficiency_opportunity",
    ),
    (
        "all context",
        "ablation_context",
    ),
]


def summarize_ablation(df):
    rows = []

    for label, projection_column in ABLATION_PROJECTIONS:
        rows.append(
            {
                "model": label,
                **metric_row(
                    df,
                    projection_column,
                ),
            }
        )

    return pd.DataFrame(rows)


def summarize_ablation_by_position(df):
    rows = []

    for position, group in df.groupby(
        "position",
        dropna=False,
    ):
        for label, projection_column in ABLATION_PROJECTIONS:
            rows.append(
                {
                    "position": position,
                    "model": label,
                    **metric_row(
                        group,
                        projection_column,
                    ),
                }
            )

    return pd.DataFrame(rows)


def summarize_calibration_by_position(df):
    """
    Calibration of the full context-adjusted ablation model by
    position and projected-points bucket.
    """

    working = df.copy()

    working["ablation_context"] = pd.to_numeric(
        working["ablation_context"],
        errors="coerce",
    )

    working["calibration_bucket"] = pd.cut(
        working["ablation_context"],
        bins=CALIBRATION_BINS,
        labels=CALIBRATION_LABELS,
        right=False,
    )

    rows = []

    for (
        position,
        bucket,
    ), subset in working.groupby(
        ["position", "calibration_bucket"],
        observed=False,
    ):
        if subset.empty:
            continue

        rows.append(
            {
                "position": position,
                "bucket": str(bucket),
                **metric_row(
                    subset,
                    "ablation_context",
                ),
            }
        )

    return pd.DataFrame(rows)


# =============================================================================
# Position-specific baseline shrinkage
# =============================================================================

POSITION_SHRINKAGE_VALUES = (
    0.70,
    0.75,
    0.80,
    0.85,
    0.90,
    0.95,
    1.00,
)


def build_shrinkage_predictions(results):
    """
    Apply position-specific shrinkage to the walk-forward baseline.

    position_mean_pts is calculated inside evaluate_week() using only
    historical weeks before the target week.
    """

    required = {
        "baseline_projection",
        "actual_pts",
        "position",
        "position_mean_pts",
    }

    missing = required - set(results.columns)

    if missing:
        raise ValueError(
            "Missing required shrinkage fields: "
            + ", ".join(sorted(missing))
        )

    df = results.copy()

    for column in (
        "baseline_projection",
        "position_mean_pts",
        "actual_pts",
    ):
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = df.dropna(
        subset=[
            "baseline_projection",
            "position_mean_pts",
            "actual_pts",
        ]
    ).copy()

    for shrinkage in POSITION_SHRINKAGE_VALUES:
        projection_name = (
            f"position_shrink_{shrinkage:.2f}"
        )

        df[projection_name] = (
            df["position_mean_pts"]
            + shrinkage
            * (
                df["baseline_projection"]
                - df["position_mean_pts"]
            )
        )

    return df


def summarize_shrinkage(
    shrinkage_df,
):
    rows = []

    for shrinkage in POSITION_SHRINKAGE_VALUES:
        projection = (
            f"position_shrink_{shrinkage:.2f}"
        )

        rows.append(
            {
                "shrinkage": shrinkage,
                **metric_row(
                    shrinkage_df,
                    projection,
                ),
            }
        )

    return pd.DataFrame(rows)


def summarize_shrinkage_by_position(
    shrinkage_df,
):
    rows = []

    for position in (
        "QB",
        "RB",
        "TE",
        "WR",
    ):
        group = shrinkage_df[
            shrinkage_df["position"] == position
        ]

        for shrinkage in POSITION_SHRINKAGE_VALUES:
            projection = (
                f"position_shrink_{shrinkage:.2f}"
            )

            rows.append(
                {
                    "position": position,
                    "shrinkage": shrinkage,
                    **metric_row(
                        group,
                        projection,
                    ),
                }
            )

    return pd.DataFrame(rows)


def summarize_shrinkage_calibration(
    shrinkage_df,
    shrinkage,
):
    projection = (
        f"position_shrink_{shrinkage:.2f}"
    )

    rows = []

    for position in (
        "QB",
        "RB",
        "TE",
        "WR",
    ):
        group = shrinkage_df[
            shrinkage_df["position"] == position
        ].copy()

        if group.empty:
            continue

        group["calibration_bucket"] = pd.cut(
            group[projection],
            bins=CALIBRATION_BINS,
            labels=CALIBRATION_LABELS,
            right=False,
        )

        for bucket in CALIBRATION_LABELS:
            bucket_group = group[
                group["calibration_bucket"] == bucket
            ]

            if bucket_group.empty:
                continue

            rows.append(
                {
                    "position": position,
                    "bucket": bucket,
                    **metric_row(
                        bucket_group,
                        projection,
                    ),
                }
            )

    return pd.DataFrame(rows)


# =============================================================================
# Walk-forward position/bucket shrinkage calibration
# =============================================================================
#
# Diagnostic only. Does NOT modify production projections.
#
# For each target week W:
#
#   training = all observations from weeks < W
#
# The calibrated projection is:
#
#   position_mean_pts
#       + shrinkage
#       * (baseline_projection - position_mean_pts)
#
# Coefficient hierarchy:
#
#   1. position + baseline projection bucket
#   2. position
#   3. global
#   4. baseline / no calibration
#
# Coefficients are selected using only observations available before the
# target week. Projection buckets are based on the raw baseline projection,
# never the calibrated projection.
# =============================================================================

POSITION_BUCKET_SHRINKAGE_GRID = (
    0.70,
    0.75,
    0.80,
    0.85,
    0.90,
    0.95,
    1.00,
)

CALIBRATION_MIN_BUCKET_N = 25
CALIBRATION_MIN_GROUP_N = 25
CALIBRATION_DEFAULT_SHRINKAGE = 0.85


def _calibration_bucket(values):
    """
    Assign the standard projection buckets.

    Buckets are always based on baseline_projection so the calibration
    process cannot recursively depend on its own output.
    """

    return pd.cut(
        pd.to_numeric(
            values,
            errors="coerce",
        ),
        bins=CALIBRATION_BINS,
        labels=CALIBRATION_LABELS,
        right=False,
    )


def _shrinkage_prediction(
    group,
    coefficient,
):
    """
    Apply position-mean shrinkage.
    """

    position_mean = pd.to_numeric(
        group["position_mean_pts"],
        errors="coerce",
    )

    baseline = pd.to_numeric(
        group["baseline_projection"],
        errors="coerce",
    )

    return (
        position_mean
        + coefficient
        * (baseline - position_mean)
    )


def _score_shrinkage_candidate(
    group,
    coefficient,
):
    """
    Score one shrinkage coefficient against historical observations.

    MAE is the primary selection metric.
    RMSE is used as the deterministic secondary metric.
    """

    prediction = _shrinkage_prediction(
        group,
        coefficient,
    )

    actual = pd.to_numeric(
        group["actual_pts"],
        errors="coerce",
    )

    mask = (
        prediction.notna()
        & actual.notna()
    )

    if not mask.any():
        return {
            "n": 0,
            "mae": np.nan,
            "rmse": np.nan,
        }

    error = (
        prediction.loc[mask]
        - actual.loc[mask]
    )

    return {
        "n": int(mask.sum()),
        "mae": float(error.abs().mean()),
        "rmse": float(
            np.sqrt(
                (error ** 2).mean()
            )
        ),
    }


def _select_shrinkage_coefficient(
    training,
    min_n,
):
    """
    Select one shrinkage coefficient from historical training data.

    The target week is never present in training.

    Selection:
        1. lowest MAE
        2. lowest RMSE
        3. closest to 0.85

    The final tie-break simply prevents arbitrary movement between equally
    performing grid values.
    """

    required = {
        "position_mean_pts",
        "baseline_projection",
        "actual_pts",
    }

    missing = required - set(training.columns)

    if missing:
        raise ValueError(
            "Missing shrinkage training fields: "
            + ", ".join(sorted(missing))
        )

    working = training[
        [
            "position_mean_pts",
            "baseline_projection",
            "actual_pts",
        ]
    ].copy()

    for column in (
        "position_mean_pts",
        "baseline_projection",
        "actual_pts",
    ):
        working[column] = pd.to_numeric(
            working[column],
            errors="coerce",
        )

    working = working.dropna().copy()

    if len(working) < min_n:
        return None

    candidates = []

    for coefficient in POSITION_BUCKET_SHRINKAGE_GRID:
        metrics = _score_shrinkage_candidate(
            working,
            coefficient,
        )

        if metrics["n"] < min_n:
            continue

        candidates.append(
            {
                "coefficient": coefficient,
                "n": metrics["n"],
                "mae": metrics["mae"],
                "rmse": metrics["rmse"],
            }
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda row: (
            row["mae"],
            row["rmse"],
            abs(
                row["coefficient"]
                - CALIBRATION_DEFAULT_SHRINKAGE
            ),
        )
    )

    return candidates[0]


def _fit_position_bucket_shrinkage(
    training,
):
    """
    Fit the historical shrinkage hierarchy.

    Returns:
        {
            "position_bucket": {
                (position, bucket): fit
            },
            "position": {
                position: fit
            },
            "global": fit_or_none,
        }

    Every fit is based exclusively on the supplied historical training
    dataframe.
    """

    if training.empty:
        return {
            "position_bucket": {},
            "position": {},
            "global": None,
        }

    working = training.copy()

    working["projection_bucket"] = _calibration_bucket(
        working["baseline_projection"]
    )

    position_bucket_fits = {}
    position_fits = {}

    # ------------------------------------------------------------------
    # Position + projection bucket
    # ------------------------------------------------------------------

    for (
        position,
        bucket,
    ), group in working.groupby(
        [
            "position",
            "projection_bucket",
        ],
        observed=True,
    ):
        fit = _select_shrinkage_coefficient(
            group,
            CALIBRATION_MIN_BUCKET_N,
        )

        if fit is not None:
            position_bucket_fits[
                (
                    position,
                    str(bucket),
                )
            ] = fit

    # ------------------------------------------------------------------
    # Position
    # ------------------------------------------------------------------

    for position, group in working.groupby(
        "position",
        dropna=False,
    ):
        fit = _select_shrinkage_coefficient(
            group,
            CALIBRATION_MIN_GROUP_N,
        )

        if fit is not None:
            position_fits[position] = fit

    # ------------------------------------------------------------------
    # Global
    # ------------------------------------------------------------------

    global_fit = _select_shrinkage_coefficient(
        working,
        CALIBRATION_MIN_GROUP_N,
    )

    return {
        "position_bucket": position_bucket_fits,
        "position": position_fits,
        "global": global_fit,
    }


def _resolve_position_bucket_shrinkage(
    row,
    fitted,
):
    """
    Resolve the appropriate historical coefficient.

    Hierarchy:
        position + bucket
        position
        global
        none
    """

    position = row["position"]
    bucket = str(
        row["projection_bucket"]
    )

    position_bucket_fit = fitted[
        "position_bucket"
    ].get(
        (
            position,
            bucket,
        )
    )

    if position_bucket_fit is not None:
        return {
            "coefficient": (
                position_bucket_fit[
                    "coefficient"
                ]
            ),
            "source": "position_bucket",
            "training_n": (
                position_bucket_fit["n"]
            ),
        }

    position_fit = fitted[
        "position"
    ].get(position)

    if position_fit is not None:
        return {
            "coefficient": (
                position_fit[
                    "coefficient"
                ]
            ),
            "source": "position",
            "training_n": position_fit["n"],
        }

    global_fit = fitted["global"]

    if global_fit is not None:
        return {
            "coefficient": (
                global_fit[
                    "coefficient"
                ]
            ),
            "source": "global",
            "training_n": global_fit["n"],
        }

    return {
        "coefficient": 1.0,
        "source": "none",
        "training_n": 0,
    }


def build_walk_forward_position_bucket_shrinkage(
    results,
):
    """
    Apply position/bucket shrinkage strictly walk-forward.

    For target week W:

        training = rows where week < W

    No target-week actuals are used when selecting the coefficient.
    """

    required = {
        "week",
        "position",
        "baseline_projection",
        "position_mean_pts",
        "actual_pts",
    }

    missing = required - set(results.columns)

    if missing:
        raise ValueError(
            "Missing required position/bucket shrinkage fields: "
            + ", ".join(sorted(missing))
        )

    df = results.copy()

    for column in (
        "week",
        "baseline_projection",
        "position_mean_pts",
        "actual_pts",
    ):
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = df.sort_values(
        [
            "week",
            "gsis_id",
        ]
        if "gsis_id" in df.columns
        else ["week"]
    ).reset_index(drop=True)

    # Bucket is deliberately based on the raw baseline.
    df["projection_bucket"] = _calibration_bucket(
        df["baseline_projection"]
    )

    df[
        "position_bucket_shrinkage_projection"
    ] = df["baseline_projection"]

    df[
        "position_bucket_shrinkage_coefficient"
    ] = np.nan

    df[
        "position_bucket_shrinkage_source"
    ] = "none"

    df[
        "position_bucket_shrinkage_training_n"
    ] = 0

    weeks = sorted(
        df["week"].dropna().unique()
    )

    for target_week in weeks:

        target_mask = df["week"].eq(
            target_week
        )

        if not target_mask.any():
            continue

        # --------------------------------------------------------------
        # Leakage boundary:
        # target week is NEVER included here.
        # --------------------------------------------------------------

        training = df[
            df["week"] < target_week
        ].copy()

        if training.empty:
            continue

        fitted = _fit_position_bucket_shrinkage(
            training
        )

        target_indices = df.index[
            target_mask
        ]

        for index in target_indices:

            row = df.loc[index]

            resolution = (
                _resolve_position_bucket_shrinkage(
                    row,
                    fitted,
                )
            )

            coefficient = resolution[
                "coefficient"
            ]

            df.loc[
                index,
                "position_bucket_shrinkage_coefficient",
            ] = coefficient

            df.loc[
                index,
                "position_bucket_shrinkage_source",
            ] = resolution["source"]

            df.loc[
                index,
                "position_bucket_shrinkage_training_n",
            ] = resolution[
                "training_n"
            ]

            if resolution["source"] == "none":
                continue

            position_mean = row[
                "position_mean_pts"
            ]

            baseline = row[
                "baseline_projection"
            ]

            if (
                pd.isna(position_mean)
                or pd.isna(baseline)
            ):
                continue

            df.loc[
                index,
                "position_bucket_shrinkage_projection",
            ] = (
                position_mean
                + coefficient
                * (
                    baseline
                    - position_mean
                )
            )

    return df


def summarize_position_bucket_shrinkage(
    df,
):
    """
    Overall baseline vs walk-forward shrinkage.
    """

    rows = []

    for label, projection in (
        (
            "baseline",
            "baseline_projection",
        ),
        (
            "position_bucket_shrinkage",
            "position_bucket_shrinkage_projection",
        ),
    ):
        rows.append(
            {
                "model": label,
                **metric_row(
                    df,
                    projection,
                ),
            }
        )

    return pd.DataFrame(rows)


def summarize_position_bucket_shrinkage_by_position(
    df,
):
    """
    Baseline vs calibrated performance by position.
    """

    rows = []

    for position in (
        "QB",
        "RB",
        "TE",
        "WR",
    ):
        group = df[
            df["position"] == position
        ]

        for label, projection in (
            (
                "baseline",
                "baseline_projection",
            ),
            (
                "position_bucket_shrinkage",
                "position_bucket_shrinkage_projection",
            ),
        ):
            rows.append(
                {
                    "position": position,
                    "model": label,
                    **metric_row(
                        group,
                        projection,
                    ),
                }
            )

    return pd.DataFrame(rows)


def summarize_position_bucket_shrinkage_buckets(
    df,
):
    """
    Evaluate calibration using the ORIGINAL baseline projection bucket.

    This is intentional. Bucketing on the calibrated projection would hide
    the compression effect we are trying to measure.
    """

    rows = []

    for (
        position,
        bucket,
    ), group in df.groupby(
        [
            "position",
            "projection_bucket",
        ],
        observed=True,
    ):
        if group.empty:
            continue

        baseline = metric_row(
            group,
            "baseline_projection",
        )

        calibrated = metric_row(
            group,
            "position_bucket_shrinkage_projection",
        )

        coefficients = pd.to_numeric(
            group[
                "position_bucket_shrinkage_coefficient"
            ],
            errors="coerce",
        ).dropna()

        rows.append(
            {
                "position": position,
                "bucket": str(bucket),
                "n": len(group),
                "mean_coefficient": (
                    coefficients.mean()
                    if not coefficients.empty
                    else np.nan
                ),
                "baseline_mae": baseline["mae"],
                "calibrated_mae": calibrated["mae"],
                "baseline_rmse": baseline["rmse"],
                "calibrated_rmse": calibrated["rmse"],
                "calibrated_bias": calibrated["bias"],
            }
        )

    return pd.DataFrame(rows)


def summarize_position_bucket_shrinkage_sources(
    df,
):
    """
    Show how often each calibration fallback level was used.
    """

    if (
        "position_bucket_shrinkage_source"
        not in df.columns
    ):
        return pd.DataFrame()

    rows = []

    for source, group in df.groupby(
        "position_bucket_shrinkage_source",
        dropna=False,
    ):
        coefficients = pd.to_numeric(
            group[
                "position_bucket_shrinkage_coefficient"
            ],
            errors="coerce",
        ).dropna()

        training_n = pd.to_numeric(
            group[
                "position_bucket_shrinkage_training_n"
            ],
            errors="coerce",
        )

        rows.append(
            {
                "source": source,
                "n": len(group),
                "share": (
                    len(group) / len(df)
                    if len(df)
                    else np.nan
                ),
                "mean_coefficient": (
                    coefficients.mean()
                    if not coefficients.empty
                    else np.nan
                ),
                "mean_training_n": (
                    training_n.mean()
                    if not training_n.empty
                    else np.nan
                ),
            }
        )

    return pd.DataFrame(rows)


def summarize_position_bucket_shrinkage_coefficients(
    df,
):
    """
    Show the coefficients selected over the backtest.
    """

    working = df[
        df[
            "position_bucket_shrinkage_source"
        ]
        == "position_bucket"
    ].copy()

    if working.empty:
        return pd.DataFrame()

    rows = []

    for (
        position,
        bucket,
    ), group in working.groupby(
        [
            "position",
            "projection_bucket",
        ],
        observed=True,
    ):
        coefficient = pd.to_numeric(
            group[
                "position_bucket_shrinkage_coefficient"
            ],
            errors="coerce",
        ).dropna()

        training_n = pd.to_numeric(
            group[
                "position_bucket_shrinkage_training_n"
            ],
            errors="coerce",
        ).dropna()

        if coefficient.empty:
            continue

        rows.append(
            {
                "position": position,
                "bucket": str(bucket),
                "n": len(coefficient),
                "mean_coefficient": (
                    coefficient.mean()
                ),
                "median_coefficient": (
                    coefficient.median()
                ),
                "min_coefficient": (
                    coefficient.min()
                ),
                "max_coefficient": (
                    coefficient.max()
                ),
                "mean_training_n": (
                    training_n.mean()
                    if not training_n.empty
                    else np.nan
                ),
            }
        )

    return pd.DataFrame(rows)


# =============================================================================
# Walk-forward production-history weighting × position/bucket shrinkage
# =============================================================================

WALK_FORWARD_PRODUCTION_RECENT_WEIGHTS = (
    0.10,
    0.15,
    0.20,
    0.25,
    0.30,
    0.60,  # current production baseline
)


def _build_candidate_production_projection(
    df: pd.DataFrame,
    recent_weight: float,
) -> pd.Series:
    """
    Build the candidate production-history projection.

    This is intentionally diagnostic-only. It does not modify the production
    projection used by evaluate_week().
    """

    season = pd.to_numeric(
        df["season_avg_pts"],
        errors="coerce",
    )

    recent = pd.to_numeric(
        df["last3_avg_pts"],
        errors="coerce",
    )

    return (
        (1.0 - recent_weight) * season
        + recent_weight * recent
    )


def _fit_walk_forward_production_shrinkage(
    training: pd.DataFrame,
) -> dict:
    """
    Fit the existing walk-forward position/bucket shrinkage hierarchy
    against a candidate production-history projection.

    The existing calibration machinery is reused unchanged by temporarily
    treating the candidate projection as baseline_projection.

    Hierarchy remains:
        position + bucket
        position
        global
    """

    fitted = _fit_position_bucket_shrinkage(
        training
    )

    return fitted


def _resolve_walk_forward_production_shrinkage(
    row: pd.Series,
    fitted: dict,
) -> tuple[float, str, float]:
    """
    Resolve the existing position/bucket hierarchy for one target row.

    The caller supplies a row whose baseline_projection has been replaced by
    the candidate production-history projection.
    """

    prediction, source, coefficient = (
        _resolve_position_bucket_shrinkage(
            row,
            fitted,
        )
    )

    return (
        prediction,
        source,
        coefficient,
    )


def build_walk_forward_production_shrinkage(
    results: pd.DataFrame,
    recent_weight: float,
) -> pd.DataFrame:
    """
    Leakage-safe walk-forward test for one production-history weight.

    For each target week:
      1. training contains only earlier weeks;
      2. candidate production projection is built from season + last3;
      3. calibration buckets are based on that candidate projection;
      4. existing position/bucket shrinkage selection is fit on training only;
      5. target-week predictions use the fitted hierarchy.

    The candidate projection is intentionally passed through the existing
    baseline_projection field on temporary copies so the production model and
    known-good evaluate_week() pipeline remain untouched.
    """

    required = {
        "week",
        "position",
        "season_avg_pts",
        "last3_avg_pts",
        "actual_pts",
        "baseline_projection",
    }

    missing = required - set(results.columns)

    if missing:
        raise ValueError(
            "Missing required walk-forward production/shrinkage fields: "
            + ", ".join(sorted(missing))
        )

    df = results.copy()

    numeric_columns = (
        "week",
        "season_avg_pts",
        "last3_avg_pts",
        "actual_pts",
    )

    for column in numeric_columns:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = df.dropna(
        subset=[
            "week",
            "season_avg_pts",
            "last3_avg_pts",
            "actual_pts",
            "position",
        ]
    ).copy()

    output = []

    for target_week in sorted(df["week"].unique()):
        target = df.loc[
            df["week"] == target_week
        ].copy()

        training = df.loc[
            df["week"] < target_week
        ].copy()

        target["candidate_projection"] = (
            _build_candidate_production_projection(
                target,
                recent_weight,
            )
        )

        target = target.dropna(
            subset=["candidate_projection"]
        ).copy()

        if training.empty:
            target["projection"] = (
                target["candidate_projection"]
            )
            target["shrinkage_source"] = "none"
            target["shrinkage_coefficient"] = np.nan

        else:
            training["candidate_projection"] = (
                _build_candidate_production_projection(
                    training,
                    recent_weight,
                )
            )

            training = training.dropna(
                subset=["candidate_projection"]
            ).copy()

            if training.empty:
                target["projection"] = (
                    target["candidate_projection"]
                )
                target["shrinkage_source"] = "none"
                target["shrinkage_coefficient"] = np.nan

            else:
                # The existing calibration helpers operate on
                # baseline_projection. For this diagnostic only, substitute
                # the candidate projection into temporary copies.
                training_calibration = training.copy()
                training_calibration[
                    "baseline_projection"
                ] = training_calibration[
                    "candidate_projection"
                ]

                target_calibration = target.copy()
                target_calibration[
                    "baseline_projection"
                ] = target_calibration[
                    "candidate_projection"
                ]

                fitted = (
                    _fit_walk_forward_production_shrinkage(
                        training_calibration
                    )
                )

                resolved = []

                for _, row in target_calibration.iterrows():
                    prediction, source, coefficient = (
                        _resolve_walk_forward_production_shrinkage(
                            row,
                            fitted,
                        )
                    )

                    resolved.append(
                        (
                            prediction,
                            source,
                            coefficient,
                        )
                    )

                target[
                    "projection"
                ] = [
                    item[0]
                    for item in resolved
                ]

                target[
                    "shrinkage_source"
                ] = [
                    item[1]
                    for item in resolved
                ]

                target[
                    "shrinkage_coefficient"
                ] = [
                    item[2]
                    for item in resolved
                ]

        target[
            "recent_weight"
        ] = recent_weight

        target[
            "season_weight"
        ] = 1.0 - recent_weight

        output.append(
            target[
                [
                    "week",
                    "position",
                    "actual_pts",
                    "candidate_projection",
                    "projection",
                    "shrinkage_source",
                    "shrinkage_coefficient",
                    "season_weight",
                    "recent_weight",
                ]
            ]
        )

    if not output:
        return pd.DataFrame(
            columns=[
                "week",
                "position",
                "actual_pts",
                "candidate_projection",
                "projection",
                "shrinkage_source",
                "shrinkage_coefficient",
                "season_weight",
                "recent_weight",
            ]
        )

    return pd.concat(
        output,
        ignore_index=True,
    )


def summarize_walk_forward_production_shrinkage(
    results: pd.DataFrame,
) -> pd.DataFrame:
    """
    Compare candidate production-history weights using the full
    leakage-safe walk-forward calibration pipeline.
    """

    rows = []

    for recent_weight in (
        WALK_FORWARD_PRODUCTION_RECENT_WEIGHTS
    ):
        evaluated = (
            build_walk_forward_production_shrinkage(
                results,
                recent_weight,
            )
        )

        if evaluated.empty:
            continue

        metrics = metric_row(
            evaluated,
            "projection",
        )

        metrics["season_weight"] = round(
            1.0 - recent_weight,
            2,
        )
        metrics["recent_weight"] = recent_weight

        rows.append(metrics)

    if not rows:
        return pd.DataFrame()

    summary = pd.DataFrame(rows)

    return summary[
        [
            "season_weight",
            "recent_weight",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values(
        [
            "mae",
            "rmse",
            "recent_weight",
        ]
    )


def summarize_walk_forward_production_shrinkage_by_position(
    results: pd.DataFrame,
) -> pd.DataFrame:
    """
    Same leakage-safe walk-forward test summarized by position.
    """

    rows = []

    for recent_weight in (
        WALK_FORWARD_PRODUCTION_RECENT_WEIGHTS
    ):
        evaluated = (
            build_walk_forward_production_shrinkage(
                results,
                recent_weight,
            )
        )

        if evaluated.empty:
            continue

        for position, group in evaluated.groupby(
            "position",
            dropna=False,
        ):
            metrics = metric_row(
                group,
                "projection",
            )

            metrics["position"] = position
            metrics["season_weight"] = round(
                1.0 - recent_weight,
                2,
            )
            metrics["recent_weight"] = recent_weight

            rows.append(metrics)

    if not rows:
        return pd.DataFrame()

    summary = pd.DataFrame(rows)

    return summary[
        [
            "position",
            "season_weight",
            "recent_weight",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values(
        [
            "position",
            "mae",
            "rmse",
            "recent_weight",
        ]
    )


# =============================================================================
# Production-history weighting experiment
# =============================================================================

PRODUCTION_WEIGHT_GRID = tuple(
    round(weight, 2)
    for weight in np.arange(0.00, 1.01, 0.05)
)


def build_weighted_production_projection(
    df: pd.DataFrame,
    recent_weight: float,
) -> pd.Series:
    season = pd.to_numeric(
        df["season_avg_pts"],
        errors="coerce",
    )

    recent = pd.to_numeric(
        df["last3_avg_pts"],
        errors="coerce",
    )

    return (
        (1.0 - recent_weight) * season
        + recent_weight * recent
    )


def summarize_production_weighting(
    results: pd.DataFrame,
) -> pd.DataFrame:
    rows = []

    for recent_weight in PRODUCTION_WEIGHT_GRID:
        evaluation = results.copy()

        evaluation["_production_weight_projection"] = (
            build_weighted_production_projection(
                evaluation,
                recent_weight,
            )
        )

        metrics = metric_row(
            evaluation,
            "_production_weight_projection",
        )

        metrics["season_weight"] = round(
            1.0 - recent_weight,
            2,
        )

        metrics["recent_weight"] = recent_weight

        rows.append(metrics)

    summary = pd.DataFrame(rows)

    return summary[
        [
            "season_weight",
            "recent_weight",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values("recent_weight")


def summarize_production_weighting_by_position(
    results: pd.DataFrame,
) -> pd.DataFrame:
    rows = []

    for position, group in results.groupby(
        "position",
        dropna=False,
    ):
        for recent_weight in PRODUCTION_WEIGHT_GRID:
            evaluation = group.copy()

            evaluation["_production_weight_projection"] = (
                build_weighted_production_projection(
                    evaluation,
                    recent_weight,
                )
            )

            metrics = metric_row(
                evaluation,
                "_production_weight_projection",
            )

            metrics["position"] = position
            metrics["season_weight"] = round(
                1.0 - recent_weight,
                2,
            )
            metrics["recent_weight"] = recent_weight

            rows.append(metrics)

    summary = pd.DataFrame(rows)

    return summary[
        [
            "position",
            "season_weight",
            "recent_weight",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values(
        ["position", "recent_weight"],
    )

# =============================================================================
# Walk-forward production-history weighting × position/bucket shrinkage
# =============================================================================

WALK_FORWARD_PRODUCTION_RECENT_WEIGHTS = (
    0.10,
    0.15,
    0.20,
    0.25,
    0.30,
    0.60,  # current production baseline
)


def _build_candidate_production_projection(
    df: pd.DataFrame,
    recent_weight: float,
) -> pd.Series:
    """
    Build the candidate production-history projection.

    Diagnostic only. Does not modify the production model.
    """

    season = pd.to_numeric(
        df["season_avg_pts"],
        errors="coerce",
    )

    recent = pd.to_numeric(
        df["last3_avg_pts"],
        errors="coerce",
    )

    return (
        (1.0 - recent_weight) * season
        + recent_weight * recent
    )


def build_walk_forward_production_shrinkage(
    results: pd.DataFrame,
    recent_weight: float,
) -> pd.DataFrame:
    """
    Leakage-safe walk-forward test for one production-history weight.

    For each target week:

      1. Training contains only earlier weeks.
      2. Candidate production projection is season + last3.
      3. Projection buckets are rebuilt from the candidate projection.
      4. Existing position/bucket shrinkage is fitted on training only.
      5. Target-week rows are resolved through the existing hierarchy.

    Existing production code is not modified.
    """

    required = {
        "week",
        "position",
        "season_avg_pts",
        "last3_avg_pts",
        "actual_pts",
        "baseline_projection",
    }

    missing = required - set(results.columns)

    if missing:
        raise ValueError(
            "Missing required walk-forward production/shrinkage fields: "
            + ", ".join(sorted(missing))
        )

    df = results.copy()

    for column in (
        "week",
        "season_avg_pts",
        "last3_avg_pts",
        "actual_pts",
    ):
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = df.dropna(
        subset=[
            "week",
            "season_avg_pts",
            "last3_avg_pts",
            "actual_pts",
            "position",
        ]
    ).copy()

    output = []

    for target_week in sorted(df["week"].unique()):

        target = df.loc[
            df["week"] == target_week
        ].copy()

        training = df.loc[
            df["week"] < target_week
        ].copy()

        # -------------------------------------------------------------------
        # Build candidate production projection
        # -------------------------------------------------------------------

        target["candidate_projection"] = (
            _build_candidate_production_projection(
                target,
                recent_weight,
            )
        )

        target = target.dropna(
            subset=["candidate_projection"]
        ).copy()

        # -------------------------------------------------------------------
        # Week 5 has no historical training data
        # -------------------------------------------------------------------

        if training.empty:

            target["projection"] = (
                target["candidate_projection"]
            )

            target["shrinkage_source"] = "none"

            target["shrinkage_coefficient"] = np.nan

        else:

            # ---------------------------------------------------------------
            # Build candidate projection on training history
            # ---------------------------------------------------------------

            training["candidate_projection"] = (
                _build_candidate_production_projection(
                    training,
                    recent_weight,
                )
            )

            training = training.dropna(
                subset=["candidate_projection"]
            ).copy()

            if training.empty:

                target["projection"] = (
                    target["candidate_projection"]
                )

                target["shrinkage_source"] = "none"

                target["shrinkage_coefficient"] = np.nan

            else:

                # -----------------------------------------------------------
                # IMPORTANT:
                #
                # Existing shrinkage machinery expects:
                #     baseline_projection
                #     projection_bucket
                #
                # Both must represent the candidate projection here.
                # -----------------------------------------------------------

                training_calibration = training.copy()

                training_calibration[
                    "baseline_projection"
                ] = training_calibration[
                    "candidate_projection"
                ]

                training_calibration["projection_bucket"] = _calibration_bucket(
                    training_calibration["candidate_projection"]
                )

                target_calibration = target.copy()

                target_calibration[
                    "baseline_projection"
                ] = target_calibration[
                    "candidate_projection"
                ]

                target_calibration["projection_bucket"] = _calibration_bucket(
                    target_calibration["candidate_projection"]
                )

                # -----------------------------------------------------------
                # Fit using prior weeks only
                # -----------------------------------------------------------

                fitted = _fit_position_bucket_shrinkage(
                    training_calibration
                )

                resolved = []

                for _, row in target_calibration.iterrows():

                    prediction, source, coefficient = (
                        _resolve_position_bucket_shrinkage(
                            row,
                            fitted,
                        )
                    )

                    resolved.append(
                        (
                            prediction,
                            source,
                            coefficient,
                        )
                    )

                target["projection"] = [
                    item[0]
                    for item in resolved
                ]

                target["shrinkage_source"] = [
                    item[1]
                    for item in resolved
                ]

                target["shrinkage_coefficient"] = [
                    item[2]
                    for item in resolved
                ]

        target["recent_weight"] = recent_weight

        target["season_weight"] = (
            1.0 - recent_weight
        )

        output.append(
            target[
                [
                    "week",
                    "position",
                    "actual_pts",
                    "candidate_projection",
                    "projection",
                    "shrinkage_source",
                    "shrinkage_coefficient",
                    "season_weight",
                    "recent_weight",
                ]
            ]
        )

    if not output:
        return pd.DataFrame(
            columns=[
                "week",
                "position",
                "actual_pts",
                "candidate_projection",
                "projection",
                "shrinkage_source",
                "shrinkage_coefficient",
                "season_weight",
                "recent_weight",
            ]
        )

    return pd.concat(
        output,
        ignore_index=True,
    )


# =============================================================================
# Production-history weighting × fixed position shrinkage
# =============================================================================

PRODUCTION_SHRINKAGE_WEIGHT_GRID = tuple(
    round(weight, 2)
    for weight in np.arange(0.00, 0.41, 0.05)
)

PRODUCTION_SHRINKAGE_VALUES = (
    0.75,
    0.80,
    0.85,
    0.90,
    0.95,
    1.00,
)


def summarize_production_weighting_shrinkage(
    results: pd.DataFrame,
) -> pd.DataFrame:
    """
    Diagnostic interaction test.

    Candidate projection:
        weighted production baseline
        + fixed position-mean shrinkage

    No production model code is modified.
    """

    required = {
        "season_avg_pts",
        "last3_avg_pts",
        "position_mean_pts",
        "actual_pts",
        "position",
    }

    missing = required - set(results.columns)

    if missing:
        raise ValueError(
            "Missing required production/shrinkage fields: "
            + ", ".join(sorted(missing))
        )

    df = results.copy()

    for column in (
        "season_avg_pts",
        "last3_avg_pts",
        "position_mean_pts",
        "actual_pts",
    ):
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = df.dropna(
        subset=[
            "season_avg_pts",
            "last3_avg_pts",
            "position_mean_pts",
            "actual_pts",
        ]
    ).copy()

    rows = []

    for recent_weight in PRODUCTION_SHRINKAGE_WEIGHT_GRID:

        weighted = (
            (1.0 - recent_weight)
            * df["season_avg_pts"]
            + recent_weight
            * df["last3_avg_pts"]
        )

        for shrinkage in PRODUCTION_SHRINKAGE_VALUES:

            projection = (
                df["position_mean_pts"]
                + shrinkage
                * (
                    weighted
                    - df["position_mean_pts"]
                )
            )

            evaluation = pd.DataFrame(
                {
                    "actual_pts": df["actual_pts"],
                    "_projection": projection,
                }
            )

            metrics = metric_row(
                evaluation,
                "_projection",
            )

            metrics["season_weight"] = round(
                1.0 - recent_weight,
                2,
            )

            metrics["recent_weight"] = recent_weight
            metrics["shrinkage"] = shrinkage

            rows.append(metrics)

    summary = pd.DataFrame(rows)

    return summary[
        [
            "season_weight",
            "recent_weight",
            "shrinkage",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values(
        [
            "mae",
            "rmse",
            "recent_weight",
            "shrinkage",
        ]
    )


def summarize_production_weighting_shrinkage_by_position(
    results: pd.DataFrame,
) -> pd.DataFrame:
    """
    Same interaction test by position.
    """

    rows = []

    for position, group in results.groupby(
        "position",
        dropna=False,
    ):

        summary = summarize_production_weighting_shrinkage(
            group
        )

        summary["position"] = position

        rows.append(summary)

    if not rows:
        return pd.DataFrame()

    summary = pd.concat(
        rows,
        ignore_index=True,
    )

    return summary[
        [
            "position",
            "season_weight",
            "recent_weight",
            "shrinkage",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values(
        [
            "position",
            "mae",
            "rmse",
        ]
    )

# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--season",
        type=int,
        default=2025,
    )

    parser.add_argument(
        "--start-week",
        type=int,
        default=5,
    )

    parser.add_argument(
        "--end-week",
        type=int,
        default=18,
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=None,
    )

    args = parser.parse_args()

    if args.start_week <= 1:
        raise ValueError(
            "Backtest must begin after Week 1."
        )

    log.info(
        "Loading weekly data — season=%s",
        args.season,
    )

    weekly = nfl.load_player_stats(
        [args.season]
    ).to_pandas()

    log.info(
        "Loading play-by-play — season=%s",
        args.season,
    )

    pbp = nfl.load_pbp(
        args.season
    ).to_pandas()

    schedule = nfl.load_schedules(
        [args.season]
    ).to_pandas()

    schedule = schedule[
        schedule["game_type"].eq("REG")
    ].copy()

    results = []

    for week in range(
        args.start_week,
        args.end_week + 1,
    ):
        try:
            result = evaluate_week(
                weekly,
                pbp,
                schedule,
                args.season,
                week,
            )

            if not result.empty:
                result["week"] = week
                results.append(result)

        except Exception as exc:
            log.exception(
                "Week %s failed: %s",
                week,
                exc,
            )

    if not results:
        raise RuntimeError(
            "Backtest produced no results."
        )

    results = pd.concat(
        results,
        ignore_index=True,
    )
    
    print("\nBACKTEST RESULT COVERAGE")
    print(
        results.groupby("week")
        .size()
        .to_string()
    )
    
    print(
        "\nTOTAL BACKTEST ROWS:",
        len(results),
    )
    
    print(
        "\nWEEKS:",
        sorted(results["week"].dropna().unique().tolist()),
    )

    # -----------------------------------------------------------------------
    # Production-history weighting
    # -----------------------------------------------------------------------

    production_weighting = (
        summarize_production_weighting(
            results
        )
    )

    production_weighting_position = (
        summarize_production_weighting_by_position(
            results
        )
    )

    print(
        "\nPRODUCTION HISTORY WEIGHTING"
    )

    print(
        production_weighting.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print(
        "\nPRODUCTION HISTORY WEIGHTING "
        "BY POSITION"
    )

    print(
        production_weighting_position.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    # -----------------------------------------------------------------------
    # Production-history weighting × fixed position shrinkage
    # -----------------------------------------------------------------------

    production_shrinkage = (
        summarize_production_weighting_shrinkage(
            results
        )
    )

    production_shrinkage_position = (
        summarize_production_weighting_shrinkage_by_position(
            results
        )
    )

    print(
        "\nPRODUCTION HISTORY WEIGHTING "
        "× POSITION SHRINKAGE"
    )

    print(
        production_shrinkage.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print(
        "\nPRODUCTION HISTORY WEIGHTING "
        "× POSITION SHRINKAGE "
        "BY POSITION"
    )

    print(
        production_shrinkage_position.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    # -----------------------------------------------------------------------
    # Walk-forward production-history weighting × position/bucket shrinkage
    # -----------------------------------------------------------------------

    walk_forward_production_shrinkage = (
        summarize_walk_forward_production_shrinkage(
            results
        )
    )

    walk_forward_production_shrinkage_position = (
        summarize_walk_forward_production_shrinkage_by_position(
            results
        )
    )

    print(
        "\nWALK-FORWARD PRODUCTION HISTORY "
        "WEIGHTING × POSITION/BUCKET SHRINKAGE"
    )

    print(
        walk_forward_production_shrinkage.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print(
        "\nWALK-FORWARD PRODUCTION HISTORY "
        "WEIGHTING × POSITION/BUCKET SHRINKAGE "
        "BY POSITION"
    )

    print(
        walk_forward_production_shrinkage_position.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )


    # -----------------------------------------------------------------------
    # QB-only composite-rating scale experiment (diagnostic only)
    # -----------------------------------------------------------------------

    qb_rating_df = build_qb_rating_scale_predictions(results)
    qb_rating_summary = summarize_qb_rating_scale_predictions(qb_rating_df)
    print("\nQB-ONLY COMPOSITE-RATING SCALE SWEEP")
    print(
        qb_rating_summary.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    # -----------------------------------------------------------------------
    # Diagnostics
    # -----------------------------------------------------------------------

    summary = summarize(results)
    calibration_table = calibration(results)
    adjustments = adjustment_summary(results)

    ablation_df = build_ablation_predictions(
        results
    )

    ablation_summary = summarize_ablation(
        ablation_df
    )

    ablation_position = (
        summarize_ablation_by_position(
            ablation_df
        )
    )

    ablation_calibration = (
        summarize_calibration_by_position(
            ablation_df
        )
    )

    # -----------------------------------------------------------------------
    # Fixed position-specific baseline shrinkage
    # -----------------------------------------------------------------------

    shrinkage_df = build_shrinkage_predictions(
        results
    )

    shrinkage_summary = summarize_shrinkage(
        shrinkage_df
    )

    shrinkage_position = (
        summarize_shrinkage_by_position(
            shrinkage_df
        )
    )

    # -----------------------------------------------------------------------
    # Walk-forward position/bucket shrinkage
    # -----------------------------------------------------------------------

    position_bucket_shrinkage_df = (
        build_walk_forward_position_bucket_shrinkage(
            results
        )
    )

    position_bucket_summary = (
        summarize_position_bucket_shrinkage(
            position_bucket_shrinkage_df
        )
    )

    position_bucket_position_summary = (
        summarize_position_bucket_shrinkage_by_position(
            position_bucket_shrinkage_df
        )
    )

    position_bucket_calibration = (
        summarize_position_bucket_shrinkage_buckets(
            position_bucket_shrinkage_df
        )
    )

    position_bucket_sources = (
        summarize_position_bucket_shrinkage_sources(
            position_bucket_shrinkage_df
        )
    )

    position_bucket_coefficients = (
        summarize_position_bucket_shrinkage_coefficients(
            position_bucket_shrinkage_df
        )
    )

    # -----------------------------------------------------------------------
    # Output
    # -----------------------------------------------------------------------

    print("\nMODEL PERFORMANCE")
    print(
        summary.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nCALIBRATION")
    print(
        calibration_table.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nCURRENT-MODEL ADJUSTMENTS")
    print(
        adjustments.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nMODEL ABLATION")
    print(
        ablation_summary.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nABLATION BY POSITION")
    print(
        ablation_position.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print(
        "\nFULL MODEL CALIBRATION "
        "BY POSITION / BUCKET"
    )

    print(
        ablation_calibration.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nBASELINE SHRINKAGE")
    print(
        shrinkage_summary.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nBASELINE SHRINKAGE BY POSITION")
    print(
        shrinkage_position.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    for shrinkage in (
        0.70,
        0.80,
        0.90,
    ):
        print(
            f"\nSHRINKAGE CALIBRATION: "
            f"{shrinkage:.2f}"
        )

        table = summarize_shrinkage_calibration(
            shrinkage_df,
            shrinkage,
        )

        print(
            table.to_string(
                index=False,
                float_format=lambda x: f"{x:.3f}",
            )
        )

    # -----------------------------------------------------------------------
    # Walk-forward position/bucket shrinkage diagnostics
    # -----------------------------------------------------------------------

    print(
        "\nWALK-FORWARD "
        "POSITION/BUCKET SHRINKAGE"
    )

    print(
        position_bucket_summary.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print(
        "\nWALK-FORWARD "
        "POSITION/BUCKET SHRINKAGE "
        "BY POSITION"
    )

    print(
        position_bucket_position_summary.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print(
        "\nPOSITION / BASELINE BUCKET"
    )

    print(
        position_bucket_calibration.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print(
        "\nCALIBRATION SOURCES"
    )

    print(
        position_bucket_sources.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print(
        "\nSELECTED COEFFICIENTS"
    )

    print(
        position_bucket_coefficients.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    if args.output:
        args.output.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        # Preserve the original detailed walk-forward
        # results plus the new position/bucket shrinkage
        # diagnostic columns.
        position_bucket_shrinkage_df.to_csv(
            args.output,
            index=False,
        )

        log.info(
            "Detailed results written to %s",
            args.output,
        )


if __name__ == "__main__":
    main()
    