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


# =============================================================================
# Backtest configuration
# =============================================================================

# Candidate production-history weights tested walk-forward.
# 0.25 recent / 0.75 season is the current validated candidate.
WALK_FORWARD_PRODUCTION_RECENT_WEIGHTS = (
    0.10,
    0.15,
    0.20,
    0.25,
    0.30,
    0.60,
)

PRODUCTION_SELECTED_RECENT_WEIGHT = 0.25

FROZEN_HOLDOUT_START_WEEK = 15

CONTEXT_SCALE_VALUES = (
    0.00,
    0.25,
    0.50,
    0.75,
    1.00,
    1.25,
    1.50,
)

CONTEXT_ADJUSTMENT_COLUMNS = (
    "efficiency_adjustment",
    "opportunity_adjustment",
    "matchup_adjustment",
    "environment_adjustment",
)

FROZEN_HOLDOUT_CONTEXT_RECENT_WEIGHT = PRODUCTION_SELECTED_RECENT_WEIGHT
FROZEN_HOLDOUT_CONTEXT_SCALES = (
    0.00,
    0.25,
    0.50,
    0.75,
    1.00,
)

RATING_INTERACTION_RECENT_WEIGHT = PRODUCTION_SELECTED_RECENT_WEIGHT
RATING_INTERACTION_SCALES = (
    0.00,
    0.50,
    0.625,
    0.75,
    0.875,
    1.00,
)

# Research-only sensitivity grid for how much prior OOS history is required
# before selecting a nonzero position-specific rating scale.
POSITION_RATING_MIN_HISTORY_WEEKS = (0, 3, 5, 8)

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
CALIBRATION_MIN_GROUP_N = 25
CALIBRATION_DEFAULT_SHRINKAGE = 0.85

POSITION_BUCKET_SHRINKAGE_GRID = (
    0.70,
    0.75,
    0.80,
    0.85,
    0.90,
    0.95,
    1.00,
)


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








# =============================================================================
# Walk-forward context-adjustment scaling
# =============================================================================
#
# Diagnostic only. Does NOT modify production projections.
#
# Tests whether each existing context adjustment is correctly sized.
#
# For each target week:
#
#   projection =
#       base_projection
#       * (
#           1
#           + scaled_component
#           + all other production components
#         )
#
# Candidate scales are evaluated against the target week without using
# target-week outcomes to choose the scale.
#
# Therefore each scale receives a genuine walk-forward OOS evaluation.
# =============================================================================


def build_walk_forward_context_scaling(
    results: pd.DataFrame,
    component: str,
    scale: float,
) -> pd.DataFrame:
    """
    Leakage-safe walk-forward evaluation of one context adjustment scale.

    The selected component is multiplied by `scale`.

    All other production components remain unchanged.

    No target-week actuals are used in constructing the projection.
    """

    required = {
        "week",
        "position",
        "base_projection",
        "efficiency_adjustment",
        "opportunity_adjustment",
        "matchup_adjustment",
        "environment_adjustment",
        "actual_pts",
    }

    missing = required - set(results.columns)

    if missing:
        raise ValueError(
            "Missing required context-scaling fields: "
            + ", ".join(sorted(missing))
        )

    if component not in CONTEXT_ADJUSTMENT_COLUMNS:
        raise ValueError(
            f"Unsupported context component: {component}"
        )

    df = results.copy()

    numeric_columns = [
        "week",
        "base_projection",
        *CONTEXT_ADJUSTMENT_COLUMNS,
        "actual_pts",
    ]

    for column in numeric_columns:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = df.dropna(
        subset=[
            "week",
            "base_projection",
            *CONTEXT_ADJUSTMENT_COLUMNS,
            "actual_pts",
            "position",
        ]
    ).copy()

    output = []

    for target_week in sorted(
        df["week"].unique()
    ):

        target = df.loc[
            df["week"] == target_week
        ].copy()

        if target.empty:
            continue

        adjustments = (
            target[
                list(CONTEXT_ADJUSTMENT_COLUMNS)
            ]
            .copy()
        )

        adjustments[component] *= scale

        total_adjustment = adjustments.sum(
            axis=1
        )

        target["projection"] = np.maximum(
            0.0,
            target["base_projection"]
            * (1.0 + total_adjustment),
        )

        target["component"] = component
        target["scale"] = scale

        output.append(
            target[
                [
                    "week",
                    "position",
                    "actual_pts",
                    "base_projection",
                    "projection",
                    "component",
                    "scale",
                ]
            ]
        )

    if not output:
        return pd.DataFrame(
            columns=[
                "week",
                "position",
                "actual_pts",
                "base_projection",
                "projection",
                "component",
                "scale",
            ]
        )

    return pd.concat(
        output,
        ignore_index=True,
    )


def build_walk_forward_context_scaling_grid(
    results: pd.DataFrame,
) -> pd.DataFrame:
    """Evaluate every configured context component/scale once."""
    frames = []

    for component in CONTEXT_ADJUSTMENT_COLUMNS:
        for scale in CONTEXT_SCALE_VALUES:
            evaluated = build_walk_forward_context_scaling(
                results,
                component,
                scale,
            )
            if not evaluated.empty:
                frames.append(evaluated)

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)


def build_frozen_holdout_context_scaling(
    results: pd.DataFrame,
    component: str,
    scale: float,
    holdout_start_week: int,
) -> pd.DataFrame:
    """
    Evaluate one context-adjustment scale on the frozen holdout.

    The adjustment scale is fixed before evaluating the holdout.
    """

    required = {
        "week",
        "position",
        "base_projection",
        "efficiency_adjustment",
        "opportunity_adjustment",
        "matchup_adjustment",
        "environment_adjustment",
        "actual_pts",
    }

    missing = required - set(results.columns)

    if missing:
        raise ValueError(
            "Missing required frozen-holdout context fields: "
            + ", ".join(sorted(missing))
        )

    if component not in CONTEXT_ADJUSTMENT_COLUMNS:
        raise ValueError(
            f"Unsupported context component: {component}"
        )

    df = results.copy()

    numeric_columns = [
        "week",
        "base_projection",
        *CONTEXT_ADJUSTMENT_COLUMNS,
        "actual_pts",
    ]

    for column in numeric_columns:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = df.dropna(
        subset=[
            "week",
            "base_projection",
            *CONTEXT_ADJUSTMENT_COLUMNS,
            "actual_pts",
            "position",
        ]
    ).copy()

    holdout = df.loc[
        df["week"] >= holdout_start_week
    ].copy()

    if holdout.empty:
        return pd.DataFrame(
            columns=[
                "week",
                "position",
                "actual_pts",
                "base_projection",
                "projection",
                "component",
                "scale",
            ]
        )

    adjustments = (
        holdout[
            list(CONTEXT_ADJUSTMENT_COLUMNS)
        ]
        .copy()
    )

    adjustments[component] *= scale

    total_adjustment = adjustments.sum(
        axis=1
    )

    holdout["projection"] = np.maximum(
        0.0,
        holdout["base_projection"]
        * (1.0 + total_adjustment),
    )

    holdout["component"] = component
    holdout["scale"] = scale

    return holdout[
        [
            "week",
            "position",
            "actual_pts",
            "base_projection",
            "projection",
            "component",
            "scale",
        ]
    ]


def summarize_walk_forward_context_scaling(
    results: pd.DataFrame,
    evaluated: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Summarize context-adjustment scaling across all walk-forward weeks."""
    if evaluated is None:
        evaluated = build_walk_forward_context_scaling_grid(results)

    if evaluated.empty:
        return pd.DataFrame()

    rows = []

    for (component, scale), group in evaluated.groupby(
        ["component", "scale"],
        dropna=False,
    ):
        metrics = metric_row(group, "projection")
        metrics["component"] = component
        metrics["scale"] = scale
        rows.append(metrics)

    return pd.DataFrame(rows)[
        [
            "component",
            "scale",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values(
        ["component", "mae", "rmse", "scale"]
    )


def summarize_walk_forward_context_scaling_by_position(
    results: pd.DataFrame,
    evaluated: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Summarize context-adjustment scaling by position."""
    if evaluated is None:
        evaluated = build_walk_forward_context_scaling_grid(results)

    if evaluated.empty:
        return pd.DataFrame()

    rows = []

    for (component, position, scale), group in evaluated.groupby(
        ["component", "position", "scale"],
        dropna=False,
    ):
        metrics = metric_row(group, "projection")
        metrics["component"] = component
        metrics["position"] = position
        metrics["scale"] = scale
        rows.append(metrics)

    return pd.DataFrame(rows)[
        [
            "component",
            "position",
            "scale",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values(
        ["component", "position", "mae", "rmse", "scale"]
    )


def build_frozen_holdout_context_scaling_grid(
    results: pd.DataFrame,
    holdout_start_week: int,
) -> pd.DataFrame:
    """Evaluate every configured context component/scale on the frozen holdout."""
    frames = []

    for component in CONTEXT_ADJUSTMENT_COLUMNS:
        for scale in FROZEN_HOLDOUT_CONTEXT_SCALES:
            evaluated = build_frozen_holdout_context_scaling(
                results,
                component,
                scale,
                holdout_start_week,
            )
            if not evaluated.empty:
                frames.append(evaluated)

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)


def summarize_frozen_holdout_context_scaling(
    results: pd.DataFrame,
    holdout_start_week: int,
    evaluated: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Summarize context-adjustment scaling on the frozen holdout."""
    if evaluated is None:
        evaluated = build_frozen_holdout_context_scaling_grid(
            results,
            holdout_start_week,
        )

    if evaluated.empty:
        return pd.DataFrame()

    rows = []

    for (component, scale), group in evaluated.groupby(
        ["component", "scale"],
        dropna=False,
    ):
        metrics = metric_row(group, "projection")
        metrics["component"] = component
        metrics["scale"] = scale
        rows.append(metrics)

    return pd.DataFrame(rows)[
        [
            "component",
            "scale",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values(
        ["component", "mae", "rmse", "scale"]
    )


# =============================================================================
# Position-specific baseline shrinkage
# =============================================================================



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


def _build_candidate_production_projection(
    df: pd.DataFrame,
    recent_weight: float,
) -> pd.Series:
    """
    Build the diagnostic candidate production-history projection.

    Does not modify the production model.
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

    For target week W:

        training = all rows where week < W

    The candidate production projection is:

        (1 - recent_weight) * season_avg_pts
        + recent_weight * last3_avg_pts

    The candidate is temporarily substituted into baseline_projection
    and passed through the existing position/bucket shrinkage machinery.

    The existing calibration machinery creates projection_bucket itself.
    No production code or calibration helper is modified.
    """

    required = {
        "week",
        "position",
        "season_avg_pts",
        "last3_avg_pts",
        "position_mean_pts",
        "actual_pts",
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
        "position_mean_pts",
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
            "position_mean_pts",
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
        # Candidate production projection for target week
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

        if target.empty:
            continue

        # -------------------------------------------------------------------
        # No historical training data
        # -------------------------------------------------------------------

        if training.empty:

            target["projection"] = (
                target["candidate_projection"]
            )

            target["shrinkage_source"] = "none"

            target["shrinkage_coefficient"] = np.nan

        else:

            # ---------------------------------------------------------------
            # Candidate production projection for historical training rows
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
                # Existing shrinkage machinery expects baseline_projection.
                #
                # Temporarily substitute the candidate projection.
                #
                # IMPORTANT:
                # _fit_position_bucket_shrinkage() itself creates
                # projection_bucket by calling _calibration_bucket()
                # vectorized across the full Series.
                #
                # Therefore:
                #   - no .apply()
                #   - no manual projection_bucket
                #   - no changes to calibration helpers
                # -----------------------------------------------------------

                training_calibration = training.copy()

                training_calibration[
                    "baseline_projection"
                ] = training_calibration[
                    "candidate_projection"
                ]

                fitted = _fit_position_bucket_shrinkage(
                    training_calibration
                )

                # -----------------------------------------------------------
                # Resolve each target row through the fitted hierarchy.
                #
                # _resolve_position_bucket_shrinkage() returns:
                #
                #     {
                #         "coefficient": ...,
                #         "source": ...,
                #         "training_n": ...,
                #     }
                #
                # Therefore calculate the final prediction here.
                # -----------------------------------------------------------

                predictions = []
                sources = []
                coefficients = []

                for _, row in target.iterrows():

                    candidate = float(
                        row["candidate_projection"]
                    )

                    position_mean = float(
                        row["position_mean_pts"]
                    )

                    resolution_row = row.copy()

                    resolution_row[
                        "baseline_projection"
                    ] = candidate

                    # Resolve the bucket for this single target value.
                    #
                    # _calibration_bucket() is vectorized, so give it a
                    # one-element Series rather than using .apply().
                    resolution_row[
                        "projection_bucket"
                    ] = _calibration_bucket(
                        pd.Series([candidate])
                    ).iloc[0]

                    resolution = (
                        _resolve_position_bucket_shrinkage(
                            resolution_row,
                            fitted,
                        )
                    )

                    coefficient = float(
                        resolution["coefficient"]
                    )

                    prediction = (
                        position_mean
                        + coefficient
                        * (
                            candidate
                            - position_mean
                        )
                    )

                    predictions.append(
                        prediction
                    )

                    sources.append(
                        resolution["source"]
                    )

                    coefficients.append(
                        coefficient
                    )

                target["projection"] = predictions

                target["shrinkage_source"] = sources

                target["shrinkage_coefficient"] = (
                    coefficients
                )

        target["recent_weight"] = recent_weight

        target["season_weight"] = (
            1.0 - recent_weight
        )

        output.append(
            target[
                [
                    "week",
                    "position",
                    "position_mean_pts",
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
                    "position_mean_pts",
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


def build_walk_forward_production_shrinkage_grid(
    results: pd.DataFrame,
) -> pd.DataFrame:
    """Evaluate all configured production-history weights once."""
    frames = []

    for recent_weight in WALK_FORWARD_PRODUCTION_RECENT_WEIGHTS:
        evaluated = build_walk_forward_production_shrinkage(
            results,
            recent_weight,
        )
        if not evaluated.empty:
            frames.append(evaluated)

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)


def summarize_walk_forward_production_shrinkage(
    results: pd.DataFrame,
    evaluated: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Summarize leakage-safe production-history weighting."""
    if evaluated is None:
        evaluated = build_walk_forward_production_shrinkage_grid(results)

    if evaluated.empty:
        return pd.DataFrame()

    rows = []

    for recent_weight, group in evaluated.groupby(
        "recent_weight",
        dropna=False,
    ):
        metrics = metric_row(group, "projection")
        metrics["season_weight"] = round(1.0 - recent_weight, 2)
        metrics["recent_weight"] = recent_weight
        rows.append(metrics)

    return pd.DataFrame(rows)[
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
        ["mae", "rmse", "recent_weight"]
    )


def summarize_walk_forward_production_shrinkage_by_position(
    results: pd.DataFrame,
    evaluated: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Summarize leakage-safe production-history weighting by position."""
    if evaluated is None:
        evaluated = build_walk_forward_production_shrinkage_grid(results)

    if evaluated.empty:
        return pd.DataFrame()

    rows = []

    for (position, recent_weight), group in evaluated.groupby(
        ["position", "recent_weight"],
        dropna=False,
    ):
        metrics = metric_row(group, "projection")
        metrics["position"] = position
        metrics["season_weight"] = round(1.0 - recent_weight, 2)
        metrics["recent_weight"] = recent_weight
        rows.append(metrics)

    return pd.DataFrame(rows)[
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
        ["position", "mae", "rmse", "recent_weight"]
    )


# =============================================================================
# Production-history weighting × fixed position shrinkage
# =============================================================================





# =============================================================================
# Summarize Walk Forward Shrinkage Damping
# =============================================================================




# =============================================================================
# Walk-forward shrinkage diagnostics
# =============================================================================

def summarize_walk_forward_shrinkage_sources(
    results: pd.DataFrame,
    recent_weights=(0.20, 0.25),
) -> pd.DataFrame:
    """
    Inspect which walk-forward shrinkage hierarchy/source is being selected
    and what coefficients are being applied.

    Diagnostic only. No production behavior changes.
    """

    rows = []

    for recent_weight in recent_weights:
        evaluated = build_walk_forward_production_shrinkage(
            results,
            recent_weight,
        )

        if evaluated.empty:
            continue

        evaluated = evaluated.copy()

        evaluated["residual"] = (
            evaluated["projection"]
            - evaluated["actual_pts"]
        )

        for source, group in evaluated.groupby(
            "shrinkage_source",
            dropna=False,
        ):
            coefficient = pd.to_numeric(
                group["shrinkage_coefficient"],
                errors="coerce",
            )

            rows.append(
                {
                    "season_weight": round(
                        1.0 - recent_weight,
                        2,
                    ),
                    "recent_weight": recent_weight,
                    "shrinkage_source": source,
                    "n": len(group),
                    "share": len(group) / len(evaluated),
                    "mean_coefficient": coefficient.mean(),
                    "median_coefficient": coefficient.median(),
                    "min_coefficient": coefficient.min(),
                    "max_coefficient": coefficient.max(),
                    "mean_candidate": group[
                        "candidate_projection"
                    ].mean(),
                    "mean_projection": group[
                        "projection"
                    ].mean(),
                    "mean_actual": group[
                        "actual_pts"
                    ].mean(),
                    "bias": group[
                        "projection"
                    ].sub(
                        group["actual_pts"]
                    ).mean(),
                    "mae": group[
                        "projection"
                    ].sub(
                        group["actual_pts"]
                    ).abs().mean(),
                    "rmse": np.sqrt(
                        group[
                            "projection"
                        ].sub(
                            group["actual_pts"]
                        ).pow(2).mean()
                    ),
                }
            )

    if not rows:
        return pd.DataFrame()

    return (
        pd.DataFrame(rows)
        .sort_values(
            [
                "recent_weight",
                "share",
            ],
            ascending=[True, False],
        )
        .reset_index(drop=True)
    )


def summarize_walk_forward_shrinkage_by_bucket(
    results: pd.DataFrame,
    recent_weights=(0.20, 0.25),
) -> pd.DataFrame:
    """
    Inspect walk-forward prediction behavior by position × projection bucket.

    The bucket is reconstructed from the candidate production projection,
    exactly as used by the walk-forward calibration process.
    """

    rows = []

    for recent_weight in recent_weights:
        evaluated = build_walk_forward_production_shrinkage(
            results,
            recent_weight,
        )

        if evaluated.empty:
            continue

        evaluated = evaluated.copy()

        evaluated["projection_bucket"] = _calibration_bucket(
            evaluated["candidate_projection"]
        )

        evaluated["error"] = (
            evaluated["projection"]
            - evaluated["actual_pts"]
        )

        for (
            position,
            bucket,
        ), group in evaluated.groupby(
            [
                "position",
                "projection_bucket",
            ],
            observed=True,
            dropna=False,
        ):
            coefficient = pd.to_numeric(
                group["shrinkage_coefficient"],
                errors="coerce",
            )

            source_counts = (
                group["shrinkage_source"]
                .value_counts()
            )

            dominant_source = (
                source_counts.index[0]
                if not source_counts.empty
                else "none"
            )

            rows.append(
                {
                    "season_weight": round(
                        1.0 - recent_weight,
                        2,
                    ),
                    "recent_weight": recent_weight,
                    "position": position,
                    "projection_bucket": str(bucket),
                    "n": len(group),
                    "dominant_source": dominant_source,
                    "dominant_source_share": (
                        source_counts.iloc[0] / len(group)
                        if not source_counts.empty
                        else np.nan
                    ),
                    "mean_coefficient": coefficient.mean(),
                    "median_coefficient": coefficient.median(),
                    "min_coefficient": coefficient.min(),
                    "max_coefficient": coefficient.max(),
                    "mean_candidate": group[
                        "candidate_projection"
                    ].mean(),
                    "mean_projection": group[
                        "projection"
                    ].mean(),
                    "mean_actual": group[
                        "actual_pts"
                    ].mean(),
                    "bias": group[
                        "projection"
                    ].sub(
                        group["actual_pts"]
                    ).mean(),
                    "mae": group[
                        "projection"
                    ].sub(
                        group["actual_pts"]
                    ).abs().mean(),
                    "rmse": np.sqrt(
                        group[
                            "projection"
                        ].sub(
                            group["actual_pts"]
                        ).pow(2).mean()
                    ),
                }
            )

    if not rows:
        return pd.DataFrame()

    return (
        pd.DataFrame(rows)
        .sort_values(
            [
                "recent_weight",
                "position",
                "projection_bucket",
            ]
        )
        .reset_index(drop=True)
    )


def summarize_walk_forward_shrinkage_oos_delta(
    results: pd.DataFrame,
    recent_weights=(0.20, 0.25),
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Out-of-sample diagnostic for walk-forward shrinkage.

    For each target week, build the candidate projection and apply shrinkage
    fitted only on earlier weeks. Compare the target-week candidate directly
    against the walk-forward calibrated projection.

    Negative MAE/RMSE deltas mean shrinkage improved the metric.
    """

    rows = []

    for recent_weight in recent_weights:
        evaluated = build_walk_forward_production_shrinkage(
            results,
            recent_weight,
        )

        if evaluated.empty:
            continue

        evaluated = evaluated.copy()

        evaluated["projection_bucket"] = _calibration_bucket(
            evaluated["candidate_projection"]
        )

        candidate_error = (
            evaluated["candidate_projection"]
            - evaluated["actual_pts"]
        )

        calibrated_error = (
            evaluated["projection"]
            - evaluated["actual_pts"]
        )

        evaluated["candidate_error"] = candidate_error
        evaluated["calibrated_error"] = calibrated_error

        for (
            week,
            position,
            bucket,
            source,
        ), group in evaluated.groupby(
            [
                "week",
                "position",
                "projection_bucket",
                "shrinkage_source",
            ],
            observed=True,
            dropna=False,
        ):
            candidate_error = group["candidate_error"]
            calibrated_error = group["calibrated_error"]

            rows.append(
                {
                    "season_weight": round(
                        1.0 - recent_weight,
                        2,
                    ),
                    "recent_weight": recent_weight,
                    "week": week,
                    "position": position,
                    "projection_bucket": str(bucket),
                    "shrinkage_source": source,
                    "n": len(group),

                    "candidate_mae": candidate_error.abs().mean(),
                    "calibrated_mae": calibrated_error.abs().mean(),
                    "mae_delta": (
                        calibrated_error.abs().mean()
                        - candidate_error.abs().mean()
                    ),

                    "candidate_rmse": np.sqrt(
                        candidate_error.pow(2).mean()
                    ),
                    "calibrated_rmse": np.sqrt(
                        calibrated_error.pow(2).mean()
                    ),
                    "rmse_delta": (
                        np.sqrt(
                            calibrated_error.pow(2).mean()
                        )
                        - np.sqrt(
                            candidate_error.pow(2).mean()
                        )
                    ),

                    "candidate_bias": candidate_error.mean(),
                    "calibrated_bias": calibrated_error.mean(),
                    "bias_delta": (
                        calibrated_error.mean()
                        - candidate_error.mean()
                    ),
                }
            )

    if not rows:
        empty = pd.DataFrame()
        return empty, empty, empty

    detail = pd.DataFrame(rows)

    overall_rows = []

    for (
        recent_weight,
        week,
    ), group in detail.groupby(
        ["recent_weight", "week"],
        dropna=False,
    ):
        n = group["n"].sum()

        overall_rows.append(
            {
                "season_weight": round(
                    1.0 - recent_weight,
                    2,
                ),
                "recent_weight": recent_weight,
                "week": week,
                "n": n,
                "mae_delta": np.average(
                    group["mae_delta"],
                    weights=group["n"],
                ),
                "rmse_delta": np.average(
                    group["rmse_delta"],
                    weights=group["n"],
                ),
                "bias_delta": np.average(
                    group["bias_delta"],
                    weights=group["n"],
                ),
            }
        )

    weekly = (
        pd.DataFrame(overall_rows)
        .sort_values(
            ["recent_weight", "week"]
        )
        .reset_index(drop=True)
    )

    summary_rows = []

    for recent_weight, group in weekly.groupby(
        "recent_weight",
        dropna=False,
    ):
        summary_rows.append(
            {
                "season_weight": round(
                    1.0 - recent_weight,
                    2,
                ),
                "recent_weight": recent_weight,
                "weeks": len(group),
                "mean_weekly_mae_delta": group[
                    "mae_delta"
                ].mean(),
                "median_weekly_mae_delta": group[
                    "mae_delta"
                ].median(),
                "weeks_mae_improved": (
                    group["mae_delta"] < 0
                ).sum(),
                "share_weeks_mae_improved": (
                    group["mae_delta"] < 0
                ).mean(),
                "mean_weekly_rmse_delta": group[
                    "rmse_delta"
                ].mean(),
                "median_weekly_rmse_delta": group[
                    "rmse_delta"
                ].median(),
                "weeks_rmse_improved": (
                    group["rmse_delta"] < 0
                ).sum(),
                "share_weeks_rmse_improved": (
                    group["rmse_delta"] < 0
                ).mean(),
                "mean_weekly_bias_delta": group[
                    "bias_delta"
                ].mean(),
            }
        )

    summary = (
        pd.DataFrame(summary_rows)
        .sort_values("recent_weight")
        .reset_index(drop=True)
    )

    return detail, weekly, summary




















def build_frozen_holdout_production_shrinkage(
    results: pd.DataFrame,
    recent_weight: float,
    holdout_start_week: int,
) -> pd.DataFrame:
    """
    Leakage-safe frozen holdout test for one production-history weight.

    Training:
        weeks < holdout_start_week

    Frozen holdout:
        weeks >= holdout_start_week

    The position/bucket shrinkage model is fitted once on the training
    period and then applied unchanged to every holdout row.
    """

    required = {
        "week",
        "position",
        "season_avg_pts",
        "last3_avg_pts",
        "position_mean_pts",
        "actual_pts",
    }

    missing = required - set(results.columns)

    if missing:
        raise ValueError(
            "Missing required frozen holdout fields: "
            + ", ".join(sorted(missing))
        )

    df = results.copy()

    for column in (
        "week",
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
            "week",
            "position",
            "season_avg_pts",
            "last3_avg_pts",
            "position_mean_pts",
            "actual_pts",
        ]
    ).copy()

    training = df.loc[
        df["week"] < holdout_start_week
    ].copy()

    holdout = df.loc[
        df["week"] >= holdout_start_week
    ].copy()

    if holdout.empty:
        return pd.DataFrame(
            columns=[
                "week",
                "position",
                "position_mean_pts",
                "actual_pts",
                "candidate_projection",
                "projection",
                "shrinkage_source",
                "shrinkage_coefficient",
                "season_weight",
                "recent_weight",
            ]
        )

    # -----------------------------------------------------------------------
    # Candidate production projection
    # -----------------------------------------------------------------------

    training["candidate_projection"] = (
        _build_candidate_production_projection(
            training,
            recent_weight,
        )
    )

    holdout["candidate_projection"] = (
        _build_candidate_production_projection(
            holdout,
            recent_weight,
        )
    )

    training = training.dropna(
        subset=["candidate_projection"]
    ).copy()

    holdout = holdout.dropna(
        subset=["candidate_projection"]
    ).copy()

    if holdout.empty:
        return pd.DataFrame(
            columns=[
                "week",
                "position",
                "position_mean_pts",
                "actual_pts",
                "candidate_projection",
                "projection",
                "shrinkage_source",
                "shrinkage_coefficient",
                "season_weight",
                "recent_weight",
            ]
        )

    # -----------------------------------------------------------------------
    # No historical training data
    # -----------------------------------------------------------------------

    if training.empty:

        holdout["projection"] = (
            holdout["candidate_projection"]
        )

        holdout["shrinkage_source"] = "none"

        holdout["shrinkage_coefficient"] = np.nan

    else:

        # -------------------------------------------------------------------
        # Fit shrinkage ONCE on the frozen training period.
        # -------------------------------------------------------------------

        training_calibration = training.copy()

        training_calibration[
            "baseline_projection"
        ] = training_calibration[
            "candidate_projection"
        ]

        fitted = _fit_position_bucket_shrinkage(
            training_calibration
        )

        # -------------------------------------------------------------------
        # Apply the frozen hierarchy to every holdout row.
        # -------------------------------------------------------------------

        predictions = []
        sources = []
        coefficients = []

        for _, row in holdout.iterrows():

            candidate = float(
                row["candidate_projection"]
            )

            position_mean = float(
                row["position_mean_pts"]
            )

            resolution_row = row.copy()

            resolution_row[
                "baseline_projection"
            ] = candidate

            resolution_row[
                "projection_bucket"
            ] = _calibration_bucket(
                pd.Series([candidate])
            ).iloc[0]

            resolution = (
                _resolve_position_bucket_shrinkage(
                    resolution_row,
                    fitted,
                )
            )

            coefficient = float(
                resolution["coefficient"]
            )

            prediction = (
                position_mean
                + coefficient
                * (
                    candidate
                    - position_mean
                )
            )

            predictions.append(
                prediction
            )

            sources.append(
                resolution["source"]
            )

            coefficients.append(
                coefficient
            )

        holdout["projection"] = predictions

        holdout["shrinkage_source"] = sources

        holdout["shrinkage_coefficient"] = (
            coefficients
        )

    holdout["season_weight"] = (
        1.0 - recent_weight
    )

    holdout["recent_weight"] = (
        recent_weight
    )

    return holdout[
        [
            "week",
            "position",
            "position_mean_pts",
            "actual_pts",
            "candidate_projection",
            "projection",
            "shrinkage_source",
            "shrinkage_coefficient",
            "season_weight",
            "recent_weight",
        ]
    ].copy()


def build_frozen_holdout_production_shrinkage_grid(
    results: pd.DataFrame,
    holdout_start_week: int,
) -> pd.DataFrame:
    """Evaluate all configured production-history weights on one frozen holdout."""
    frames = []

    for recent_weight in WALK_FORWARD_PRODUCTION_RECENT_WEIGHTS:
        evaluated = build_frozen_holdout_production_shrinkage(
            results,
            recent_weight,
            holdout_start_week,
        )
        if not evaluated.empty:
            frames.append(evaluated)

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)


def summarize_frozen_holdout_production_shrinkage(
    results: pd.DataFrame,
    holdout_start_week: int,
    evaluated: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Summarize production-history weighting on a frozen holdout."""
    if evaluated is None:
        evaluated = build_frozen_holdout_production_shrinkage_grid(
            results,
            holdout_start_week,
        )

    if evaluated.empty:
        return pd.DataFrame()

    rows = []

    for recent_weight, group in evaluated.groupby(
        "recent_weight",
        dropna=False,
    ):
        metrics = metric_row(group, "projection")
        metrics["season_weight"] = round(1.0 - recent_weight, 2)
        metrics["recent_weight"] = recent_weight
        rows.append(metrics)

    return pd.DataFrame(rows)[
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
        ["mae", "rmse", "recent_weight"]
    )


def summarize_frozen_holdout_production_shrinkage_by_position(
    results: pd.DataFrame,
    holdout_start_week: int,
    evaluated: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Summarize the frozen production-history test by position."""
    if evaluated is None:
        evaluated = build_frozen_holdout_production_shrinkage_grid(
            results,
            holdout_start_week,
        )

    if evaluated.empty:
        return pd.DataFrame()

    rows = []

    for (position, recent_weight), group in evaluated.groupby(
        ["position", "recent_weight"],
        dropna=False,
    ):
        metrics = metric_row(group, "projection")
        metrics["position"] = position
        metrics["season_weight"] = round(1.0 - recent_weight, 2)
        metrics["recent_weight"] = recent_weight
        rows.append(metrics)

    return pd.DataFrame(rows)[
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
        ["position", "mae", "rmse", "recent_weight"]
    )

# =============================================================================
# Frozen holdout: production weighting × position/bucket shrinkage × context
# =============================================================================


def build_frozen_holdout_production_shrinkage_context(
    results: pd.DataFrame,
    holdout_start_week: int,
    recent_weight: float = FROZEN_HOLDOUT_CONTEXT_RECENT_WEIGHT,
) -> pd.DataFrame:
    """
    Frozen holdout interaction test.

    Pipeline:

        weighted production projection
        -> scaled context adjustment
        -> frozen position/bucket shrinkage

    Training:
        week < holdout_start_week

    Holdout:
        week >= holdout_start_week

    The shrinkage hierarchy is fitted once on training data and then
    applied unchanged to the holdout.
    """

    required = {
        "week",
        "position",
        "season_avg_pts",
        "last3_avg_pts",
        "position_mean_pts",
        "actual_pts",
        "efficiency_adjustment",
        "opportunity_adjustment",
        "matchup_adjustment",
        "environment_adjustment",
    }

    missing = required - set(results.columns)

    if missing:
        raise ValueError(
            "Missing required frozen context fields: "
            + ", ".join(sorted(missing))
        )

    df = results.copy()

    numeric_columns = [
        "week",
        "season_avg_pts",
        "last3_avg_pts",
        "position_mean_pts",
        "actual_pts",
        "efficiency_adjustment",
        "opportunity_adjustment",
        "matchup_adjustment",
        "environment_adjustment",
    ]

    for column in numeric_columns:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = df.dropna(
        subset=[
            "week",
            "position",
            "season_avg_pts",
            "last3_avg_pts",
            "position_mean_pts",
            "actual_pts",
        ]
    ).copy()

    training = df.loc[
        df["week"] < holdout_start_week
    ].copy()

    holdout = df.loc[
        df["week"] >= holdout_start_week
    ].copy()

    if training.empty or holdout.empty:
        return pd.DataFrame()

    components = (
        "efficiency_adjustment",
        "opportunity_adjustment",
        "matchup_adjustment",
        "environment_adjustment",
    )

    output = []

    for component in (
        *components,
        "all_context",
    ):

        if component == "all_context":
            adjustment = (
                training["efficiency_adjustment"]
                + training["opportunity_adjustment"]
                + training["matchup_adjustment"]
                + training["environment_adjustment"]
            )
        else:
            adjustment = training[component]

        training_base = _build_candidate_production_projection(
            training,
            recent_weight,
        )

        training["candidate_projection"] = (
            training_base
            * (
                1.0
                + adjustment.fillna(0.0)
            )
        )

        # ---------------------------------------------------------------
        # Fit frozen shrinkage on context-adjusted candidate.
        # ---------------------------------------------------------------

        training_calibration = training.copy()

        training_calibration[
            "baseline_projection"
        ] = training_calibration[
            "candidate_projection"
        ]

        fitted = _fit_position_bucket_shrinkage(
            training_calibration
        )

        # ---------------------------------------------------------------
        # Apply frozen model to holdout for each scale.
        # ---------------------------------------------------------------

        for scale in FROZEN_HOLDOUT_CONTEXT_SCALES:

            target = holdout.copy()

            candidate = _build_candidate_production_projection(
                target,
                recent_weight,
            )

            if component == "all_context":
                target_adjustment = (
                    target["efficiency_adjustment"]
                    + target["opportunity_adjustment"]
                    + target["matchup_adjustment"]
                    + target["environment_adjustment"]
                )
            else:
                target_adjustment = target[component]

            target["candidate_projection"] = (
                candidate
                * (
                    1.0
                    + scale
                    * target_adjustment.fillna(0.0)
                )
            )

            target_calibration = target.copy()

            target_calibration[
                "baseline_projection"
            ] = target_calibration[
                "candidate_projection"
            ]

            target_calibration[
                "projection_bucket"
            ] = _calibration_bucket(
                target_calibration[
                    "candidate_projection"
                ]
            )

            predictions = []

            for _, row in target_calibration.iterrows():

                resolution = (
                    _resolve_position_bucket_shrinkage(
                        row,
                        fitted,
                    )
                )

                coefficient = float(
                    resolution["coefficient"]
                )

                prediction = (
                    float(row["position_mean_pts"])
                    + coefficient
                    * (
                        float(row["candidate_projection"])
                        - float(row["position_mean_pts"])
                    )
                )

                predictions.append(prediction)

            target["projection"] = predictions

            target["component"] = component
            target["scale"] = scale
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
                        "component",
                        "scale",
                        "season_weight",
                        "recent_weight",
                    ]
                ]
            )

    return pd.concat(
        output,
        ignore_index=True,
    )

def summarize_frozen_holdout_production_shrinkage_context(
    results: pd.DataFrame,
    holdout_start_week: int,
    evaluated: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Summarize frozen production × shrinkage × context scaling."""
    if evaluated is None:
        evaluated = build_frozen_holdout_production_shrinkage_context(
            results,
            holdout_start_week,
        )

    if evaluated.empty:
        return pd.DataFrame()

    rows = []

    for (component, scale), group in evaluated.groupby(
        ["component", "scale"],
        dropna=False,
    ):
        metrics = metric_row(group, "projection")
        metrics["component"] = component
        metrics["scale"] = scale
        rows.append(metrics)

    return pd.DataFrame(rows)[
        [
            "component",
            "scale",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values(
        ["component", "mae", "rmse", "scale"]
    )


def summarize_frozen_holdout_production_shrinkage_context_by_position(
    results: pd.DataFrame,
    holdout_start_week: int,
    evaluated: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Summarize frozen production × shrinkage × context scaling by position."""
    if evaluated is None:
        evaluated = build_frozen_holdout_production_shrinkage_context(
            results,
            holdout_start_week,
        )

    if evaluated.empty:
        return pd.DataFrame()

    rows = []

    for (component, position, scale), group in evaluated.groupby(
        ["component", "position", "scale"],
        dropna=False,
    ):
        metrics = metric_row(group, "projection")
        metrics["component"] = component
        metrics["position"] = position
        metrics["scale"] = scale
        rows.append(metrics)

    return pd.DataFrame(rows)[
        [
            "component",
            "position",
            "scale",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values(
        ["component", "position", "mae", "rmse", "scale"]
    )






# =============================================================================
# Walk-forward / frozen holdout: production × shrinkage × composite rating
# =============================================================================


def summarize_rating_population(results: pd.DataFrame) -> pd.DataFrame:
    """
    Audit the population available to the composite-rating experiment.

    Rating comparisons must use only rows with an observed composite_rating.
    This keeps the no-rating control and every rating scale on the identical
    observation set instead of silently treating missing ratings as neutral.
    """

    required = {"position", "composite_rating"}
    missing = required - set(results.columns)

    if missing:
        raise ValueError(
            "Missing rating population fields: "
            + ", ".join(sorted(missing))
        )

    df = results[["position", "composite_rating"]].copy()
    df["composite_rating"] = pd.to_numeric(
        df["composite_rating"],
        errors="coerce",
    )

    rows = []

    groups = [("ALL", df)]
    groups.extend(
        (position, group)
        for position, group in df.groupby("position", dropna=False)
    )

    for position, group in groups:
        total = len(group)
        valid = int(group["composite_rating"].notna().sum())
        missing_count = total - valid

        rows.append(
            {
                "position": position,
                "total_n": total,
                "rating_n": valid,
                "missing_n": missing_count,
                "missing_pct": (
                    missing_count / total * 100.0
                    if total else np.nan
                ),
            }
        )

    return pd.DataFrame(rows)


def summarize_frozen_holdout_rating_weekly_stability(
    evaluated: pd.DataFrame,
) -> pd.DataFrame:
    """
    Compare each frozen-holdout rating scale with the scale=0 control by week.

    The control is deliberately the same matched rating population and the
    same fitted shrinkage architecture.
    """

    if evaluated.empty:
        return pd.DataFrame()

    required = {"week", "rating_scale", "projection", "actual_pts"}
    missing = required - set(evaluated.columns)

    if missing:
        raise ValueError(
            "Missing frozen rating stability fields: "
            + ", ".join(sorted(missing))
        )

    control = evaluated[
        evaluated["rating_scale"].eq(0.0)
    ][["week", "actual_pts", "projection"]].copy()

    control_metrics = (
        control.groupby("week", as_index=False)
        .apply(
            lambda g: pd.Series({
                "control_mae": (
                    g["projection"] - g["actual_pts"]
                ).abs().mean()
            }),
            include_groups=False,
        )
        .reset_index(drop=True)
    )

    rows = []

    for scale, group in evaluated.groupby("rating_scale"):
        if scale == 0.0:
            continue

        weekly = (
            group.groupby("week", as_index=False)
            .apply(
                lambda g: pd.Series({
                    "rating_mae": (
                        g["projection"] - g["actual_pts"]
                    ).abs().mean()
                }),
                include_groups=False,
            )
            .reset_index(drop=True)
        )

        merged = weekly.merge(
            control_metrics,
            on="week",
            how="inner",
        )

        if merged.empty:
            continue

        delta = merged["rating_mae"] - merged["control_mae"]

        rows.append(
            {
                "rating_scale": scale,
                "weeks": len(delta),
                "mean_weekly_mae_delta": delta.mean(),
                "median_weekly_mae_delta": delta.median(),
                "weeks_improved": int((delta < 0).sum()),
                "weeks_worse": int((delta > 0).sum()),
                "pct_weeks_improved": (delta < 0).mean() * 100.0,
                "pct_weeks_worse": (delta > 0).mean() * 100.0,
            }
        )

    return pd.DataFrame(rows).sort_values(
        ["mean_weekly_mae_delta", "rating_scale"]
    )



def _select_position_rating_scales_from_walk_forward(
    evaluated: pd.DataFrame,
    target_week: float | int | None = None,
    min_history_weeks: int = 0,
) -> dict:
    """
    Select one rating scale per position using prior walk-forward OOS weeks.

    If target_week is supplied, only weeks strictly before target_week are
    eligible. Ties resolve to the smallest scale. With no prior evidence,
    scale=0 is the conservative fallback.
    """
    if evaluated.empty:
        return {}

    required = {
        "week",
        "position",
        "rating_scale",
        "projection",
        "actual_pts",
    }
    missing = required - set(evaluated.columns)
    if missing:
        raise ValueError(
            "Missing position-rating selection fields: "
            + ", ".join(sorted(missing))
        )

    if min_history_weeks < 0:
        raise ValueError("min_history_weeks must be >= 0")

    history = evaluated.copy()
    if target_week is not None:
        history = history[history["week"] < target_week].copy()

    if history.empty:
        return {}

    if min_history_weeks:
        weeks_by_position = history.groupby("position")["week"].nunique()
        eligible_positions = weeks_by_position[
            weeks_by_position >= min_history_weeks
        ].index
        history = history[history["position"].isin(eligible_positions)].copy()

    if history.empty:
        return {}

    history["abs_error"] = (
        history["projection"] - history["actual_pts"]
    ).abs()

    scores = (
        history.groupby(
            ["position", "rating_scale"],
            dropna=False,
        )["abs_error"]
        .mean()
        .reset_index(name="mae")
        .sort_values(
            ["position", "mae", "rating_scale"]
        )
    )

    selected = (
        scores.groupby("position", as_index=False)
        .first()
    )

    return dict(
        zip(
            selected["position"],
            selected["rating_scale"],
        )
    )


def build_walk_forward_position_specific_rating(
    evaluated: pd.DataFrame,
    min_history_weeks: int = 0,
) -> pd.DataFrame:
    """
    Build a leakage-safe position-specific rating-scale selection.

    For each target week, each position's scale is selected only from prior
    walk-forward OOS observations. The selected rows are then compared as a
    single composite projection.
    """
    if evaluated.empty:
        return pd.DataFrame()

    required = {
        "week",
        "position",
        "rating_scale",
        "projection",
        "actual_pts",
    }
    missing = required - set(evaluated.columns)
    if missing:
        raise ValueError(
            "Missing position-rating evaluation fields: "
            + ", ".join(sorted(missing))
        )

    rows = []

    for week in sorted(evaluated["week"].dropna().unique()):
        prior = evaluated[evaluated["week"] < week]
        scales = _select_position_rating_scales_from_walk_forward(
            evaluated,
            target_week=week,
            min_history_weeks=min_history_weeks,
        )

        target = evaluated[evaluated["week"].eq(week)].copy()
        if not scales:
            scales = {
                position: 0.0
                for position in target["position"].dropna().unique()
            }

        # Count distinct prior weeks available for each position. This is an
        # audit field only; it does not change the scale-selection rule.
        history_weeks = (
            prior.groupby("position")["week"].nunique().to_dict()
            if not prior.empty else {}
        )
        target["selection_history_weeks"] = (
            target["position"].map(history_weeks).fillna(0).astype(int)
        )
        target["selected_rating_scale"] = target["position"].map(
            scales
        ).fillna(0.0)

        target = target[
            target["rating_scale"].eq(
                target["selected_rating_scale"]
            )
        ].copy()

        if not target.empty:
            rows.append(target)

    if not rows:
        return pd.DataFrame()

    return pd.concat(rows, ignore_index=True)


def build_frozen_holdout_position_specific_rating(
    walk_forward_evaluated: pd.DataFrame,
    holdout_evaluated: pd.DataFrame,
    holdout_start_week: int,
    min_history_weeks: int = 0,
) -> pd.DataFrame:
    """
    Apply position-specific rating scales selected entirely before holdout.

    Scale selection uses walk-forward OOS rows from weeks before the frozen
    holdout. Holdout rows are never used to choose the scale.
    """
    if walk_forward_evaluated.empty or holdout_evaluated.empty:
        return pd.DataFrame()

    required = {
        "week",
        "position",
        "rating_scale",
        "projection",
        "actual_pts",
    }

    for name, frame in (
        ("walk-forward", walk_forward_evaluated),
        ("holdout", holdout_evaluated),
    ):
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(
                f"Missing {name} position-rating fields: "
                + ", ".join(sorted(missing))
            )

    pre_holdout = walk_forward_evaluated[
        walk_forward_evaluated["week"] < holdout_start_week
    ]
    scales = _select_position_rating_scales_from_walk_forward(
        walk_forward_evaluated,
        target_week=holdout_start_week,
        min_history_weeks=min_history_weeks,
    )
    history_weeks = (
        pre_holdout.groupby("position")["week"].nunique().to_dict()
        if not pre_holdout.empty else {}
    )

    target = holdout_evaluated.copy()
    target["selection_history_weeks"] = (
        target["position"].map(history_weeks).fillna(0).astype(int)
    )
    target["selected_rating_scale"] = target["position"].map(
        scales
    ).fillna(0.0)

    return target[
        target["rating_scale"].eq(
            target["selected_rating_scale"]
        )
    ].copy()


def summarize_position_specific_rating(
    evaluated: pd.DataFrame,
) -> pd.DataFrame:
    """Summarize the selected position-specific rating projection."""
    if evaluated.empty:
        return pd.DataFrame()

    rows = []
    for position, group in evaluated.groupby(
        "position",
        dropna=False,
    ):
        metrics = metric_row(group, "projection")
        metrics["position"] = position
        scales_used = sorted(
            pd.to_numeric(
                group["selected_rating_scale"], errors="coerce"
            ).dropna().unique().tolist()
        )
        metrics["rating_scale"] = (
            scales_used[0] if len(scales_used) == 1 else np.nan
        )
        metrics["rating_scales_used"] = ", ".join(
            f"{scale:.3f}" for scale in scales_used
        )
        rows.append(metrics)

    overall = metric_row(evaluated, "projection")
    overall["position"] = "ALL"
    overall["rating_scale"] = np.nan
    overall["rating_scales_used"] = "position-specific"
    rows.append(overall)

    return pd.DataFrame(rows)[
        [
            "position",
            "rating_scale",
            "rating_scales_used",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values("position")

def summarize_position_rating_scale_frequency(
    evaluated: pd.DataFrame,
) -> pd.DataFrame:
    """Report how often each position-specific scale was selected."""
    if evaluated.empty:
        return pd.DataFrame()

    required = {
        "week", "position", "selected_rating_scale",
        "selection_history_weeks",
    }
    missing = required - set(evaluated.columns)
    if missing:
        raise ValueError(
            "Missing position-rating selection audit fields: "
            + ", ".join(sorted(missing))
        )

    selections = evaluated[
        ["week", "position", "selected_rating_scale", "selection_history_weeks"]
    ].drop_duplicates()
    rows = []
    for position, group in selections.groupby("position", dropna=False):
        total_weeks = int(group["week"].nunique())
        for scale, scale_group in group.groupby("selected_rating_scale", dropna=False):
            rows.append({
                "position": position,
                "selected_rating_scale": float(scale),
                "weeks_selected": int(scale_group["week"].nunique()),
                "total_position_weeks": total_weeks,
                "pct_position_weeks": (
                    100.0 * scale_group["week"].nunique() / total_weeks
                    if total_weeks else np.nan
                ),
                "min_prior_weeks": int(scale_group["selection_history_weeks"].min()),
                "median_prior_weeks": float(scale_group["selection_history_weeks"].median()),
                "max_prior_weeks": int(scale_group["selection_history_weeks"].max()),
            })

    return pd.DataFrame(rows).sort_values(
        ["position", "selected_rating_scale"]
    ).reset_index(drop=True)


def summarize_position_rating_selection_stability(
    selected: pd.DataFrame,
    candidate_grid: pd.DataFrame,
    fixed_scales: tuple[float, ...] = (0.0, 0.75),
) -> pd.DataFrame:
    """Compare adaptive selections with fixed scales on weekly matched groups."""
    if selected.empty or candidate_grid.empty:
        return pd.DataFrame()

    required = {"week", "position", "rating_scale", "projection", "actual_pts"}
    for label, frame in (("selected", selected), ("candidate grid", candidate_grid)):
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(
                f"Missing {label} stability fields: " + ", ".join(sorted(missing))
            )

    selected_rows = selected.copy()
    selected_rows["abs_error"] = (
        selected_rows["projection"] - selected_rows["actual_pts"]
    ).abs()
    selected_weekly = selected_rows.groupby(
        ["week", "position"], as_index=False
    )["abs_error"].mean().rename(columns={"abs_error": "selected_mae"})

    results = []
    for scale in fixed_scales:
        baseline = candidate_grid[
            candidate_grid["rating_scale"].eq(scale)
        ].copy()
        if baseline.empty:
            continue
        baseline["abs_error"] = (
            baseline["projection"] - baseline["actual_pts"]
        ).abs()
        baseline_weekly = baseline.groupby(
            ["week", "position"], as_index=False
        )["abs_error"].mean().rename(columns={"abs_error": "fixed_mae"})

        paired = selected_weekly.merge(
            baseline_weekly, on=["week", "position"], how="inner", validate="one_to_one"
        )
        paired["delta_mae"] = paired["selected_mae"] - paired["fixed_mae"]
        for position, group in paired.groupby("position", dropna=False):
            results.append({
                "comparison": f"selected_vs_{scale:.3f}",
                "position": position,
                "position_weeks": int(len(group)),
                "mean_weekly_delta_mae": float(group["delta_mae"].mean()),
                "median_weekly_delta_mae": float(group["delta_mae"].median()),
                "weeks_improved": int((group["delta_mae"] < -1e-12).sum()),
                "weeks_tied": int((group["delta_mae"].abs() <= 1e-12).sum()),
                "weeks_worse": int((group["delta_mae"] > 1e-12).sum()),
            })

        # Position-week averages are not population-weighted. Compute true
        # overall weekly MAE directly from row-level observations instead.
        sel_overall = selected_rows.groupby("week")["abs_error"].mean().rename("selected_mae")
        base_overall = baseline.groupby("week")["abs_error"].mean().rename("fixed_mae")
        overall = pd.concat([sel_overall, base_overall], axis=1).dropna()
        delta = overall["selected_mae"] - overall["fixed_mae"]
        results.append({
            "comparison": f"selected_vs_{scale:.3f}",
            "position": "ALL",
            "position_weeks": int(len(delta)),
            "mean_weekly_delta_mae": float(delta.mean()) if len(delta) else np.nan,
            "median_weekly_delta_mae": float(delta.median()) if len(delta) else np.nan,
            "weeks_improved": int((delta < -1e-12).sum()),
            "weeks_tied": int((delta.abs() <= 1e-12).sum()),
            "weeks_worse": int((delta > 1e-12).sum()),
        })

    return pd.DataFrame(results)[[
        "comparison", "position", "position_weeks", "mean_weekly_delta_mae",
        "median_weekly_delta_mae", "weeks_improved", "weeks_tied", "weeks_worse",
    ]].sort_values(["comparison", "position"]).reset_index(drop=True)



def summarize_rating_week_block_bootstrap(
    candidate: pd.DataFrame,
    control: pd.DataFrame,
    candidate_label: str,
    control_label: str,
    n_bootstrap: int = 10000,
    seed: int = 20261009,
) -> pd.DataFrame:
    """Estimate uncertainty in paired MAE differences by resampling whole weeks.

    Negative delta means the candidate beats the control. Resampling whole
    weeks preserves within-week dependence better than treating every player
    row as an independent observation.
    """
    if n_bootstrap < 1000:
        raise ValueError("n_bootstrap must be >= 1000")
    required = {"week", "position", "projection", "actual_pts"}
    for label, frame in (("candidate", candidate), ("control", control)):
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(
                f"Missing {label} bootstrap fields: " + ", ".join(sorted(missing))
            )
    if candidate.empty or control.empty:
        return pd.DataFrame()

    def weekly_mae(frame: pd.DataFrame, position=None) -> pd.Series:
        subset = frame if position is None else frame[frame["position"].eq(position)]
        if subset.empty:
            return pd.Series(dtype=float)
        errors = (subset["projection"] - subset["actual_pts"]).abs()
        return errors.groupby(subset["week"]).mean().sort_index()

    positions = sorted(
        set(candidate["position"].dropna().unique())
        & set(control["position"].dropna().unique())
    )
    groups = [("ALL", None)] + [(str(pos), pos) for pos in positions]
    rng = np.random.default_rng(seed)
    rows = []

    for position_label, position_value in groups:
        cand_weekly = weekly_mae(candidate, position_value)
        ctrl_weekly = weekly_mae(control, position_value)
        paired = pd.concat(
            [cand_weekly.rename("candidate_mae"), ctrl_weekly.rename("control_mae")],
            axis=1,
            join="inner",
        ).dropna()
        if paired.empty:
            continue
        delta = (paired["candidate_mae"] - paired["control_mae"]).to_numpy(dtype=float)
        sample_n = len(delta)
        boot_means = np.empty(n_bootstrap, dtype=float)
        for start in range(0, n_bootstrap, 1000):
            batch_n = min(1000, n_bootstrap - start)
            indices = rng.integers(0, sample_n, size=(batch_n, sample_n))
            boot_means[start:start + batch_n] = delta[indices].mean(axis=1)
        rows.append({
            "candidate": candidate_label,
            "control": control_label,
            "position": position_label,
            "weeks": int(sample_n),
            "mean_weekly_delta_mae": float(delta.mean()),
            "ci_2_5": float(np.quantile(boot_means, 0.025)),
            "ci_97_5": float(np.quantile(boot_means, 0.975)),
            "bootstrap_pct_candidate_better": float((boot_means < 0).mean() * 100.0),
            "weeks_improved": int((delta < -1e-12).sum()),
            "weeks_tied": int((np.abs(delta) <= 1e-12).sum()),
            "weeks_worse": int((delta > 1e-12).sum()),
        })
    return pd.DataFrame(rows)


def build_rating_week_block_bootstrap_summary(
    walk_forward_grid: pd.DataFrame,
    holdout_grid: pd.DataFrame,
    walk_forward_selected: pd.DataFrame,
    holdout_selected: pd.DataFrame,
    scales: tuple[float, ...] = RATING_INTERACTION_SCALES,
    n_bootstrap: int = 10000,
) -> pd.DataFrame:
    """Compare fixed and adaptive rating choices with paired week-block CIs."""
    rows = []
    for period, grid, selected in (
        ("walk_forward", walk_forward_grid, walk_forward_selected),
        ("frozen_holdout", holdout_grid, holdout_selected),
    ):
        if grid.empty:
            continue
        control = grid[np.isclose(grid["rating_scale"].astype(float), 0.0)].copy()
        if control.empty:
            continue
        for scale in scales:
            candidate = grid[
                np.isclose(grid["rating_scale"].astype(float), float(scale))
            ].copy()
            if candidate.empty or np.isclose(float(scale), 0.0):
                continue
            summary = summarize_rating_week_block_bootstrap(
                candidate, control, f"fixed_{scale:.3f}", "fixed_0.000",
                n_bootstrap=n_bootstrap,
                seed=20261009 + int(round(scale * 1000)) + (0 if period == "walk_forward" else 100000),
            )
            if not summary.empty:
                summary.insert(0, "period", period)
                rows.append(summary)

        if not selected.empty:
            for scale in (0.0, 0.75):
                fixed = grid[
                    np.isclose(grid["rating_scale"].astype(float), scale)
                ].copy()
                if fixed.empty:
                    continue
                summary = summarize_rating_week_block_bootstrap(
                    selected, fixed, "position_specific", f"fixed_{scale:.3f}",
                    n_bootstrap=n_bootstrap,
                    seed=30301009 + int(round(scale * 1000)) + (0 if period == "walk_forward" else 100000),
                )
                if not summary.empty:
                    summary.insert(0, "period", period)
                    rows.append(summary)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True).sort_values(
        ["period", "candidate", "control", "position"]
    ).reset_index(drop=True)


def summarize_position_rating_history_sensitivity(
    walk_forward_grid: pd.DataFrame,
    holdout_grid: pd.DataFrame,
    holdout_start_week: int,
    min_history_values: tuple[int, ...] = POSITION_RATING_MIN_HISTORY_WEEKS,
) -> pd.DataFrame:
    """Compare adaptive rating selection across minimum-history thresholds."""
    rows = []
    for min_history in min_history_values:
        wf_selected = build_walk_forward_position_specific_rating(
            walk_forward_grid, min_history_weeks=min_history
        )
        ho_selected = build_frozen_holdout_position_specific_rating(
            walk_forward_grid, holdout_grid, holdout_start_week,
            min_history_weeks=min_history,
        )
        for period, selected, candidates in (
            ("walk_forward", wf_selected, walk_forward_grid),
            ("frozen_holdout", ho_selected, holdout_grid),
        ):
            if selected.empty:
                continue
            for position, group in list(selected.groupby("position")) + [("ALL", selected)]:
                metrics = metric_row(group, "projection")
                rows.append({
                    "period": period,
                    "min_history_weeks": int(min_history),
                    "position": position,
                    "n": int(metrics["n"]),
                    "mae": float(metrics["mae"]),
                    "rmse": float(metrics["rmse"]),
                    "bias": float(metrics["bias"]),
                    "selected_scales": ",".join(
                        f"{x:.3f}" for x in sorted(
                            pd.to_numeric(group["selected_rating_scale"], errors="coerce")
                            .dropna().unique().tolist()
                        )
                    ),
                })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(
        ["period", "position", "min_history_weeks"]
    ).reset_index(drop=True)


def _build_rating_adjusted_projection(
    df: pd.DataFrame,
    recent_weight: float,
    rating_scale: float,
) -> pd.Series:
    """
    Build the validated production projection with an optional
    composite-rating adjustment.

    scale=1.0 reproduces the existing legacy rating multiplier:

        0.8 + 0.004 * composite_rating

    scale=0 disables the rating signal entirely.
    """

    base = _build_candidate_production_projection(
        df,
        recent_weight,
    )

    rating = pd.to_numeric(
        df["composite_rating"],
        errors="coerce",
    ).fillna(50.0)

    rating_adjustment = (
        rating - 50.0
    ) * 0.004 * rating_scale

    return (
        base
        * (
            1.0
            + rating_adjustment
        )
    )


def _apply_fitted_shrinkage(
    target: pd.DataFrame,
    fitted: dict,
) -> pd.DataFrame:
    """
    Apply a fitted position/bucket shrinkage model to candidate projections.

    `_resolve_position_bucket_shrinkage()` returns a resolution dictionary.
    Keep that contract in one place so diagnostic pipelines cannot drift.
    """
    target = target.copy()

    target["projection_bucket"] = _calibration_bucket(
        target["candidate_projection"]
    )
    target["baseline_projection"] = target["candidate_projection"]

    predictions = []
    sources = []
    coefficients = []

    for _, row in target.iterrows():
        candidate = float(row["candidate_projection"])
        position_mean = float(row["position_mean_pts"])

        resolution = _resolve_position_bucket_shrinkage(
            row,
            fitted,
        )

        coefficient = float(resolution["coefficient"])
        prediction = (
            position_mean
            + coefficient * (candidate - position_mean)
        )

        predictions.append(prediction)
        sources.append(resolution["source"])
        coefficients.append(coefficient)

    target["projection"] = predictions
    target["shrinkage_source"] = sources
    target["shrinkage_coefficient"] = coefficients

    return target


def build_walk_forward_production_shrinkage_rating(
    results: pd.DataFrame,
    recent_weight: float = RATING_INTERACTION_RECENT_WEIGHT,
) -> pd.DataFrame:
    """
    Leakage-safe walk-forward test of:

        production weighting
        -> composite rating adjustment
        -> position/bucket shrinkage

    Every rating scale gets its own training projection and its own
    shrinkage fit. Nothing from the target week enters calibration.
    """

    required = {
        "week",
        "position",
        "season_avg_pts",
        "last3_avg_pts",
        "actual_pts",
        "position_mean_pts",
        "composite_rating",
    }

    missing = required - set(results.columns)

    if missing:
        raise ValueError(
            "Missing required production/shrinkage/rating fields: "
            + ", ".join(sorted(missing))
        )

    df = results.copy()

    for column in (
        "week",
        "season_avg_pts",
        "last3_avg_pts",
        "actual_pts",
        "position_mean_pts",
        "composite_rating",
    ):
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = df.dropna(
        subset=[
            "week",
            "position",
            "season_avg_pts",
            "last3_avg_pts",
            "actual_pts",
            "position_mean_pts",
            "composite_rating",
        ]
    ).copy()

    output = []

    for week in sorted(df["week"].unique()):

        training = df[
            df["week"] < week
        ].copy()

        target = df[
            df["week"] == week
        ].copy()

        if training.empty or target.empty:
            continue

        for scale in RATING_INTERACTION_SCALES:

            training["candidate_projection"] = (
                _build_rating_adjusted_projection(
                    training,
                    recent_weight,
                    scale,
                )
            )

            training_calibration = training.copy()

            training_calibration[
                "baseline_projection"
            ] = training_calibration[
                "candidate_projection"
            ]

            training_calibration[
                "projection_bucket"
            ] = _calibration_bucket(
                training_calibration[
                    "candidate_projection"
                ]
            )

            fitted = _fit_position_bucket_shrinkage(
                training_calibration
            )

            target_scale = target.copy()

            target_scale[
                "candidate_projection"
            ] = _build_rating_adjusted_projection(
                target_scale,
                recent_weight,
                scale,
            )

            target_scale = _apply_fitted_shrinkage(
                target_scale,
                fitted,
            )

            target_scale["week"] = week
            target_scale["rating_scale"] = scale
            target_scale["recent_weight"] = recent_weight
            target_scale["season_weight"] = (
                1.0 - recent_weight
            )

            output.append(
                target_scale[
                    [
                        "week",
                        "position",
                        "actual_pts",
                        "candidate_projection",
                        "projection",
                        "rating_scale",
                        "season_weight",
                        "recent_weight",
                        "shrinkage_source",
                        "shrinkage_coefficient",
                    ]
                ]
            )

    if not output:
        return pd.DataFrame()

    return pd.concat(
        output,
        ignore_index=True,
    )


def build_walk_forward_production_shrinkage_rating_grid(
    results: pd.DataFrame,
) -> pd.DataFrame:
    """Evaluate every configured composite-rating scale once."""
    return build_walk_forward_production_shrinkage_rating(
        results,
        recent_weight=RATING_INTERACTION_RECENT_WEIGHT,
    )


def summarize_walk_forward_production_shrinkage_rating(
    results: pd.DataFrame,
    evaluated: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Summarize the walk-forward production × shrinkage × rating test."""
    if evaluated is None:
        evaluated = build_walk_forward_production_shrinkage_rating_grid(
            results
        )

    if evaluated.empty:
        return pd.DataFrame()

    rows = []

    for scale, group in evaluated.groupby(
        "rating_scale",
        dropna=False,
    ):
        metrics = metric_row(group, "projection")
        metrics["rating_scale"] = scale
        rows.append(metrics)

    return pd.DataFrame(rows)[
        [
            "rating_scale",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values(
        ["mae", "rmse", "rating_scale"]
    )


def summarize_walk_forward_production_shrinkage_rating_by_position(
    results: pd.DataFrame,
    evaluated: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Summarize the walk-forward rating test by position."""
    if evaluated is None:
        evaluated = build_walk_forward_production_shrinkage_rating_grid(
            results
        )

    if evaluated.empty:
        return pd.DataFrame()

    rows = []

    for (position, scale), group in evaluated.groupby(
        ["position", "rating_scale"],
        dropna=False,
    ):
        metrics = metric_row(group, "projection")
        metrics["position"] = position
        metrics["rating_scale"] = scale
        rows.append(metrics)

    return pd.DataFrame(rows)[
        [
            "position",
            "rating_scale",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values(
        ["position", "mae", "rmse", "rating_scale"]
    )


def build_frozen_holdout_production_shrinkage_rating(
    results: pd.DataFrame,
    holdout_start_week: int,
    recent_weight: float = RATING_INTERACTION_RECENT_WEIGHT,
) -> pd.DataFrame:
    """
    Frozen holdout validation of the production × shrinkage × rating stack.

    Training:
        week < holdout_start_week

    Holdout:
        week >= holdout_start_week

    Each rating scale receives an independent training-time shrinkage fit.
    """

    required = {
        "week",
        "position",
        "season_avg_pts",
        "last3_avg_pts",
        "actual_pts",
        "position_mean_pts",
        "composite_rating",
    }

    missing = required - set(results.columns)

    if missing:
        raise ValueError(
            "Missing required frozen rating fields: "
            + ", ".join(sorted(missing))
        )

    df = results.copy()

    for column in (
        "week",
        "season_avg_pts",
        "last3_avg_pts",
        "actual_pts",
        "position_mean_pts",
        "composite_rating",
    ):
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = df.dropna(
        subset=[
            "week",
            "position",
            "season_avg_pts",
            "last3_avg_pts",
            "actual_pts",
            "position_mean_pts",
            "composite_rating",
        ]
    ).copy()

    training = df[
        df["week"] < holdout_start_week
    ].copy()

    holdout = df[
        df["week"] >= holdout_start_week
    ].copy()

    if training.empty or holdout.empty:
        return pd.DataFrame()

    output = []

    for scale in RATING_INTERACTION_SCALES:

        training["candidate_projection"] = (
            _build_rating_adjusted_projection(
                training,
                recent_weight,
                scale,
            )
        )

        training_calibration = training.copy()

        training_calibration[
            "baseline_projection"
        ] = training_calibration[
            "candidate_projection"
        ]

        training_calibration[
            "projection_bucket"
        ] = _calibration_bucket(
            training_calibration[
                "candidate_projection"
            ]
        )

        fitted = _fit_position_bucket_shrinkage(
            training_calibration
        )

        target = holdout.copy()

        target["candidate_projection"] = (
            _build_rating_adjusted_projection(
                target,
                recent_weight,
                scale,
            )
        )

        target = _apply_fitted_shrinkage(
            target,
            fitted,
        )

        target["rating_scale"] = scale
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
                    "rating_scale",
                    "season_weight",
                    "recent_weight",
                    "shrinkage_source",
                    "shrinkage_coefficient",
                ]
            ]
        )

    return pd.concat(
        output,
        ignore_index=True,
    )


def build_frozen_holdout_production_shrinkage_rating_grid(
    results: pd.DataFrame,
    holdout_start_week: int,
) -> pd.DataFrame:
    """Evaluate every configured composite-rating scale on one frozen holdout."""
    return build_frozen_holdout_production_shrinkage_rating(
        results,
        holdout_start_week,
        recent_weight=RATING_INTERACTION_RECENT_WEIGHT,
    )


def summarize_frozen_holdout_production_shrinkage_rating(
    results: pd.DataFrame,
    holdout_start_week: int,
    evaluated: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Summarize the frozen production × shrinkage × rating test."""
    if evaluated is None:
        evaluated = build_frozen_holdout_production_shrinkage_rating_grid(
            results,
            holdout_start_week,
        )

    if evaluated.empty:
        return pd.DataFrame()

    rows = []

    for scale, group in evaluated.groupby(
        "rating_scale",
        dropna=False,
    ):
        metrics = metric_row(group, "projection")
        metrics["rating_scale"] = scale
        rows.append(metrics)

    return pd.DataFrame(rows)[
        [
            "rating_scale",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values(
        ["mae", "rmse", "rating_scale"]
    )


def summarize_frozen_holdout_production_shrinkage_rating_by_position(
    results: pd.DataFrame,
    holdout_start_week: int,
    evaluated: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Summarize the frozen rating test by position."""
    if evaluated is None:
        evaluated = build_frozen_holdout_production_shrinkage_rating_grid(
            results,
            holdout_start_week,
        )

    if evaluated.empty:
        return pd.DataFrame()

    rows = []

    for (position, scale), group in evaluated.groupby(
        ["position", "rating_scale"],
        dropna=False,
    ):
        metrics = metric_row(group, "projection")
        metrics["position"] = position
        metrics["rating_scale"] = scale
        rows.append(metrics)

    return pd.DataFrame(rows)[
        [
            "position",
            "rating_scale",
            "n",
            "mae",
            "rmse",
            "bias",
            "mean_projection",
            "mean_actual",
            "correlation",
        ]
    ].sort_values(
        ["position", "mae", "rmse", "rating_scale"]
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
        raise ValueError("Backtest must begin after Week 1.")

    log.info(
        "Loading weekly data — season=%s",
        args.season,
    )
    weekly = nfl.load_player_stats([args.season]).to_pandas()

    log.info(
        "Loading play-by-play — season=%s",
        args.season,
    )
    pbp = nfl.load_pbp(args.season).to_pandas()

    schedule = nfl.load_schedules([args.season]).to_pandas()
    schedule = schedule[
        schedule["game_type"].eq("REG")
    ].copy()

    results = []

    for week in range(args.start_week, args.end_week + 1):
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
        raise RuntimeError("Backtest produced no results.")

    results = pd.concat(results, ignore_index=True)

    print("\nBACKTEST RESULT COVERAGE")
    print(results.groupby("week").size().to_string())
    print("\nTOTAL BACKTEST ROWS:", len(results))
    print(
        "\nWEEKS:",
        sorted(results["week"].dropna().unique().tolist()),
    )

    # -----------------------------------------------------------------------
    # Core model
    # -----------------------------------------------------------------------

    print("\nMODEL PERFORMANCE")
    print(
        summarize(results).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nCALIBRATION")
    print(
        calibration(results).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nCURRENT-MODEL ADJUSTMENTS")
    print(
        adjustment_summary(results).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    # -----------------------------------------------------------------------
    # Production-history weighting
    # -----------------------------------------------------------------------

    production_weighting = summarize_production_weighting(results)
    production_weighting_position = (
        summarize_production_weighting_by_position(results)
    )

    print("\nPRODUCTION HISTORY WEIGHTING")
    print(
        production_weighting.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nPRODUCTION HISTORY WEIGHTING BY POSITION")
    print(
        production_weighting_position.to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    # -----------------------------------------------------------------------
    # Walk-forward production × position/bucket shrinkage
    # -----------------------------------------------------------------------

    walk_forward_production = (
        build_walk_forward_production_shrinkage_grid(results)
    )

    print(
        "\nWALK-FORWARD PRODUCTION HISTORY "
        "× POSITION/BUCKET SHRINKAGE"
    )
    print(
        summarize_walk_forward_production_shrinkage(
            results,
            evaluated=walk_forward_production,
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print(
        "\nWALK-FORWARD PRODUCTION HISTORY "
        "× POSITION/BUCKET SHRINKAGE BY POSITION"
    )
    print(
        summarize_walk_forward_production_shrinkage_by_position(
            results,
            evaluated=walk_forward_production,
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    # -----------------------------------------------------------------------
    # Position/bucket calibration diagnostics
    # -----------------------------------------------------------------------

    position_bucket = build_walk_forward_position_bucket_shrinkage(results)

    print("\nWALK-FORWARD POSITION/BUCKET SHRINKAGE")
    print(
        summarize_position_bucket_shrinkage(position_bucket).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nWALK-FORWARD POSITION/BUCKET SHRINKAGE BY POSITION")
    print(
        summarize_position_bucket_shrinkage_by_position(
            position_bucket
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nPOSITION / BASELINE BUCKET")
    print(
        summarize_position_bucket_shrinkage_buckets(
            position_bucket
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nCALIBRATION SOURCES")
    print(
        summarize_position_bucket_shrinkage_sources(
            position_bucket
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nSELECTED COEFFICIENTS")
    print(
        summarize_position_bucket_shrinkage_coefficients(
            position_bucket
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    # -----------------------------------------------------------------------
    # Walk-forward OOS shrinkage deltas
    # -----------------------------------------------------------------------

    (
        oos_delta,
        oos_weekly,
        oos_summary,
    ) = summarize_walk_forward_shrinkage_oos_delta(
        results,
        recent_weights=(0.20, 0.25),
    )

    print("\nWALK-FORWARD OOS SHRINKAGE SUMMARY")
    if oos_summary.empty:
        print("No walk-forward OOS delta diagnostics available.")
    else:
        print(
            oos_summary[
                [
                    "season_weight",
                    "recent_weight",
                    "weeks",
                    "mean_weekly_mae_delta",
                    "median_weekly_mae_delta",
                    "share_weeks_mae_improved",
                    "mean_weekly_rmse_delta",
                    "median_weekly_rmse_delta",
                    "share_weeks_rmse_improved",
                ]
            ].to_string(
                index=False,
                float_format=lambda x: f"{x:.3f}",
            )
        )

    print("\nWALK-FORWARD OOS WEEKLY MAE DELTA")
    if oos_weekly.empty:
        print("No weekly diagnostics available.")
    else:
        print(
            oos_weekly[
                [
                    "season_weight",
                    "recent_weight",
                    "week",
                    "n",
                    "mae_delta",
                    "rmse_delta",
                    "bias_delta",
                ]
            ].to_string(
                index=False,
                float_format=lambda x: f"{x:.3f}",
            )
        )

    if not oos_delta.empty:
        print("\nWALK-FORWARD OOS DELTA BY POSITION")
        position_delta = (
            oos_delta
            .groupby(
                ["season_weight", "recent_weight", "position"],
                dropna=False,
            )
            .apply(
                lambda group: pd.Series(
                    {
                        "n": group["n"].sum(),
                        "mae_delta": np.average(
                            group["mae_delta"],
                            weights=group["n"],
                        ),
                        "rmse_delta": np.average(
                            group["rmse_delta"],
                            weights=group["n"],
                        ),
                        "bias_delta": np.average(
                            group["bias_delta"],
                            weights=group["n"],
                        ),
                    }
                )
            )
            .reset_index()
        )
        print(
            position_delta.to_string(
                index=False,
                float_format=lambda x: f"{x:.3f}",
            )
        )

    # -----------------------------------------------------------------------
    # Frozen holdout: production × shrinkage
    # -----------------------------------------------------------------------

    frozen_production = (
        build_frozen_holdout_production_shrinkage_grid(
            results,
            FROZEN_HOLDOUT_START_WEEK,
        )
    )

    print(
        "\nFROZEN HOLDOUT: PRODUCTION HISTORY "
        "× POSITION/BUCKET SHRINKAGE"
    )
    print(
        summarize_frozen_holdout_production_shrinkage(
            results,
            FROZEN_HOLDOUT_START_WEEK,
            evaluated=frozen_production,
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nFROZEN HOLDOUT BY POSITION")
    print(
        summarize_frozen_holdout_production_shrinkage_by_position(
            results,
            FROZEN_HOLDOUT_START_WEEK,
            evaluated=frozen_production,
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    # -----------------------------------------------------------------------
    # Context scaling
    # -----------------------------------------------------------------------

    context_walk_forward = build_walk_forward_context_scaling_grid(results)

    print("\nWALK-FORWARD CONTEXT-ADJUSTMENT SCALING")
    print(
        summarize_walk_forward_context_scaling(
            results,
            evaluated=context_walk_forward,
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nWALK-FORWARD CONTEXT SCALING BY POSITION")
    print(
        summarize_walk_forward_context_scaling_by_position(
            results,
            evaluated=context_walk_forward,
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    context_holdout = build_frozen_holdout_context_scaling_grid(
        results,
        FROZEN_HOLDOUT_START_WEEK,
    )

    print("\nFROZEN HOLDOUT: CONTEXT-ADJUSTMENT SCALING")
    print(
        summarize_frozen_holdout_context_scaling(
            results,
            FROZEN_HOLDOUT_START_WEEK,
            evaluated=context_holdout,
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    # -----------------------------------------------------------------------
    # Frozen holdout: production × shrinkage × context
    # -----------------------------------------------------------------------

    frozen_context = build_frozen_holdout_production_shrinkage_context(
        results,
        FROZEN_HOLDOUT_START_WEEK,
    )

    print(
        "\nFROZEN HOLDOUT: "
        "PRODUCTION × SHRINKAGE × CONTEXT SCALING"
    )
    print(
        summarize_frozen_holdout_production_shrinkage_context(
            results,
            FROZEN_HOLDOUT_START_WEEK,
            evaluated=frozen_context,
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print(
        "\nFROZEN HOLDOUT: "
        "PRODUCTION × SHRINKAGE × CONTEXT SCALING BY POSITION"
    )
    print(
        summarize_frozen_holdout_production_shrinkage_context_by_position(
            results,
            FROZEN_HOLDOUT_START_WEEK,
            evaluated=frozen_context,
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    # -----------------------------------------------------------------------
    # Composite-rating population integrity
    # -----------------------------------------------------------------------

    print("\nCOMPOSITE RATING POPULATION INTEGRITY")
    print(
        summarize_rating_population(results).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    # -----------------------------------------------------------------------
    # Production × shrinkage × composite rating
    # -----------------------------------------------------------------------

    rating_walk_forward = (
        build_walk_forward_production_shrinkage_rating_grid(results)
    )

    print(
        "\nWALK-FORWARD: "
        "PRODUCTION × SHRINKAGE × COMPOSITE RATING"
    )
    print(
        summarize_walk_forward_production_shrinkage_rating(
            results,
            evaluated=rating_walk_forward,
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print(
        "\nWALK-FORWARD: "
        "PRODUCTION × SHRINKAGE × COMPOSITE RATING BY POSITION"
    )
    print(
        summarize_walk_forward_production_shrinkage_rating_by_position(
            results,
            evaluated=rating_walk_forward,
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    rating_holdout = (
        build_frozen_holdout_production_shrinkage_rating_grid(
            results,
            FROZEN_HOLDOUT_START_WEEK,
        )
    )

    print(
        "\nFROZEN HOLDOUT: "
        "PRODUCTION × SHRINKAGE × COMPOSITE RATING"
    )
    print(
        summarize_frozen_holdout_production_shrinkage_rating(
            results,
            FROZEN_HOLDOUT_START_WEEK,
            evaluated=rating_holdout,
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print(
        "\nFROZEN HOLDOUT: "
        "PRODUCTION × SHRINKAGE × COMPOSITE RATING BY POSITION"
    )
    print(
        summarize_frozen_holdout_production_shrinkage_rating_by_position(
            results,
            FROZEN_HOLDOUT_START_WEEK,
            evaluated=rating_holdout,
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print(
        "\nFROZEN HOLDOUT: COMPOSITE RATING WEEKLY STABILITY "
        "VS SCALE 0.000"
    )
    print(
        summarize_frozen_holdout_rating_weekly_stability(
            rating_holdout
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )
    # -----------------------------------------------------------------------
    # Position-specific composite-rating scale
    # -----------------------------------------------------------------------

    rating_position_walk_forward = (
        build_walk_forward_position_specific_rating(
            rating_walk_forward
        )
    )

    print(
        "\nWALK-FORWARD: POSITION-SPECIFIC COMPOSITE RATING SCALE"
    )
    print(
        summarize_position_specific_rating(
            rating_position_walk_forward
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nWALK-FORWARD: POSITION-SPECIFIC SCALE SELECTION FREQUENCY")
    print(
        summarize_position_rating_scale_frequency(
            rating_position_walk_forward
        ).to_string(index=False, float_format=lambda x: f"{x:.3f}")
    )

    print("\nWALK-FORWARD: POSITION-SPECIFIC WEEKLY STABILITY VS FIXED SCALES")
    print(
        summarize_position_rating_selection_stability(
            rating_position_walk_forward,
            rating_walk_forward,
        ).to_string(index=False, float_format=lambda x: f"{x:.3f}")
    )

    rating_position_holdout = (
        build_frozen_holdout_position_specific_rating(
            rating_walk_forward,
            rating_holdout,
            FROZEN_HOLDOUT_START_WEEK,
        )
    )

    print(
        "\nFROZEN HOLDOUT: POSITION-SPECIFIC COMPOSITE RATING SCALE"
    )
    print(
        summarize_position_specific_rating(
            rating_position_holdout
        ).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print(
        "\nFROZEN HOLDOUT: POSITION-SPECIFIC SELECTED RATING SCALES"
    )
    print(
        rating_position_holdout[
            ["position", "selected_rating_scale"]
        ].drop_duplicates()
        .sort_values("position")
        .to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    print("\nFROZEN HOLDOUT: POSITION-SPECIFIC WEEKLY STABILITY VS FIXED SCALES")
    print(
        summarize_position_rating_selection_stability(
            rating_position_holdout,
            rating_holdout,
        ).to_string(index=False, float_format=lambda x: f"{x:.3f}")
    )

    print("\nPOSITION-SPECIFIC RATING: MINIMUM-HISTORY SENSITIVITY")
    print(
        summarize_position_rating_history_sensitivity(
            rating_walk_forward,
            rating_holdout,
            FROZEN_HOLDOUT_START_WEEK,
        ).to_string(index=False, float_format=lambda x: f"{x:.3f}")
    )

    print("\nCOMPOSITE RATING: PAIRED WEEK-BLOCK BOOTSTRAP UNCERTAINTY")
    print(
        build_rating_week_block_bootstrap_summary(
            rating_walk_forward,
            rating_holdout,
            rating_position_walk_forward,
            rating_position_holdout,
            n_bootstrap=10000,
        ).to_string(index=False, float_format=lambda x: f"{x:.3f}")
    )


    # -----------------------------------------------------------------------
    # Output
    # -----------------------------------------------------------------------

    if args.output:
        args.output.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        position_bucket.to_csv(
            args.output,
            index=False,
        )
        log.info(
            "Detailed position/bucket results written to %s",
            args.output,
        )


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
