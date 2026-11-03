#!/usr/bin/env python3
"""
update_nfl_data.py — Gridiron Oracle Data Pipeline

Historical player production: DEFAULT_SEASON
Current roster/team/schedule context: CURRENT_ROSTER_SEASON

Pipeline:
  player stats -> player metrics -> current roster context
  -> team environment/matchup -> injury/status -> ratings -> output
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import nflreadpy as nfl
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "src" / "utils" / "nfl_data.js"
LOG_PATH = ROOT / "scripts" / "logs" / "update_nfl_data.log"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.model.ratings import compute_player_ratings
from src.model.team_environment import (
    build_team_environment,
    build_opponent_map,
    attach_team_environment,
)

DEFAULT_SEASON = 2025
CURRENT_ROSTER_SEASON = 2026

NFL_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
    "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
    "LA", "LAC", "LV", "MIA", "MIN", "NE", "NO", "NYG",
    "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
}

SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "K"}

INJURY_STATUS_MAP = {
    "Out": 0.0,
    "IR": 0.0,
    "PUP": 0.0,
    "Suspended": 0.0,
    "Doubtful": 0.25,
    "Questionable": 0.55,
    "GTD": 0.55,
    "Limited": 0.75,
    "Full": 1.0,
    "Active": 1.0,
    "Probable": 0.92,
}

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ==============================================================================
# Shared data loads
# ==============================================================================

def fetch_id_crosswalk() -> pd.DataFrame:
    log.info("Fetching ID crosswalk (espn_id / gsis_id / pfr_id)...")
    ids = nfl.load_ff_playerids().to_pandas()
    cols = ["gsis_id", "espn_id", "pfr_id", "name", "position", "team"]
    df = ids[cols].copy()
    df = df.dropna(subset=["gsis_id"])
    df["gsis_id"] = df["gsis_id"].astype(str).str.strip()
    return df


def fetch_current_schedule_context(
    matchup_week: int,
) -> tuple[pd.DataFrame, int, list[str]]:
    schedule = nfl.load_schedules(
        [CURRENT_ROSTER_SEASON]
    ).to_pandas()

    required = {"game_type", "week", "away_team", "home_team"}
    missing = required - set(schedule.columns)

    if missing:
        raise ValueError(
            f"Schedule missing required columns: {sorted(missing)}"
        )

    schedule = schedule[
        schedule["game_type"].eq("REG")
    ].copy()

    schedule["week"] = pd.to_numeric(
        schedule["week"],
        errors="coerce",
    )

    for column in ("away_team", "home_team"):
        schedule[column] = (
            schedule[column]
            .astype("string")
            .str.upper()
            .str.strip()
        )

    target_games = schedule[
        schedule["week"].eq(matchup_week)
    ].copy()

    if target_games.empty:
        raise RuntimeError(
            f"No regular-season schedule found for "
            f"{CURRENT_ROSTER_SEASON} week {matchup_week}"
        )

    if target_games[["away_team", "home_team"]].isna().any().any():
        raise RuntimeError(
            f"Incomplete team assignments in week {matchup_week}"
        )

    teams = pd.concat(
        [
            target_games["away_team"],
            target_games["home_team"],
        ],
        ignore_index=True,
    )

    unknown_teams = set(teams) - NFL_TEAMS

    if unknown_teams:
        raise RuntimeError(
            f"Unknown NFL teams in week {matchup_week}: "
            f"{sorted(unknown_teams)}"
        )

    if len(teams) != len(set(teams)):
        duplicates = teams[teams.duplicated()].unique().tolist()
        raise RuntimeError(
            f"Teams assigned to multiple games in week "
            f"{matchup_week}: {duplicates}"
        )

    playing_teams = set(teams)
    bye_teams = sorted(NFL_TEAMS - playing_teams)

    if playing_teams & set(bye_teams):
        raise RuntimeError(
            "A team cannot both play and have a bye"
        )

    if playing_teams | set(bye_teams) != NFL_TEAMS:
        raise RuntimeError(
            "Schedule does not account for all 32 NFL teams"
        )

    if len(playing_teams) != len(target_games) * 2:
        raise RuntimeError(
            "Invalid schedule game/team count"
        )

    log.info(
        f"Schedule context — season={CURRENT_ROSTER_SEASON}, "
        f"matchup_week={matchup_week}, "
        f"games={len(target_games)}, "
        f"teams_playing={len(playing_teams)}, "
        f"bye_teams={bye_teams}"
    )

    return schedule, matchup_week, bye_teams


def fetch_current_roster_teams(season: int) -> dict:
    log.info(
        f"Fetching current roster teams (nflreadpy) — season={season}..."
    )

    try:
        rosters = nfl.load_rosters([season]).to_pandas()

        required = {"gsis_id", "team"}
        missing = required - set(rosters.columns)
        if missing:
            raise ValueError(
                f"Roster data missing required columns: {sorted(missing)}"
            )

        roster = rosters[
            rosters["gsis_id"].notna() &
            rosters["team"].notna()
        ].copy()

        roster["gsis_id"] = (
            roster["gsis_id"].astype(str).str.strip()
        )
        roster["team"] = (
            roster["team"].astype(str).str.upper().str.strip()
        )

        roster = roster.drop_duplicates("gsis_id", keep="last")

        team_map = dict(zip(roster["gsis_id"], roster["team"]))

        log.info(
            f"Current roster teams loaded — {len(team_map)} players"
        )
        return team_map

    except Exception as e:
        log.warning(
            f"Current roster team fetch failed ({e}) — "
            "historical teams will be retained"
        )
        return {}


def fetch_current_roster_status(season: int) -> dict:
    log.info(
        f"Fetching current roster status (nflreadpy) — season={season}..."
    )

    try:
        rosters = nfl.load_rosters([season]).to_pandas()

        required = {"gsis_id", "status"}
        missing = required - set(rosters.columns)
        if missing:
            raise ValueError(
                f"Roster data missing required columns: {sorted(missing)}"
            )

        roster = rosters[rosters["gsis_id"].notna()].copy()
        roster["gsis_id"] = (
            roster["gsis_id"].astype(str).str.strip()
        )
        roster["status"] = (
            roster["status"].fillna("").astype(str).str.upper().str.strip()
        )
        roster = roster.drop_duplicates("gsis_id", keep="last")

        status_map = dict(zip(roster["gsis_id"], roster["status"]))

        log.info(f"Roster status loaded — {len(status_map)} players")
        return status_map

    except Exception as e:
        log.warning(
            f"Current roster status fetch failed ({e}) — "
            "defaulting to empty status map"
        )
        return {}


def fetch_injury_report(season: int) -> dict:
    log.info("Fetching latest injury report (nflreadpy)...")

    try:
        injuries = nfl.load_injuries([season]).to_pandas()
        result = {}

        for _, row in injuries.iterrows():
            gsis_id = row.get("gsis_id") or row.get("player_id")
            if not gsis_id:
                continue

            status = str(
                row.get("report_status") or "Active"
            ).strip()
            if status.lower() == "nan":
                status = "Active"

            primary = str(
                row.get("primary_injury") or ""
            ).strip()
            if primary.lower() == "nan":
                primary = ""

            play_prob = INJURY_STATUS_MAP.get(status, 1.0)
            detail = f"{primary} — {status}" if primary else status

            result[str(gsis_id)] = {
                "play_probability": play_prob,
                "injury_detail": detail,
            }

        log.info(f"Injury report: {len(result)} players flagged")
        return result

    except Exception as e:
        log.error(f"Injury fetch failed: {e}")
        sys.exit(1)


# ==============================================================================
# Player stats and metrics
# ==============================================================================

def fetch_epa_per_play_from_pbp(
    pbp: pd.DataFrame,
) -> pd.DataFrame:
    """Aggregate player EPA across all applicable player roles."""
    records = []

    qb_pbp = pbp[
        (pbp["pass_attempt"] == 1) &
        pbp["passer_id"].notna()
    ]
    records.append(
        qb_pbp.groupby("passer_id")["epa"]
        .mean()
        .reset_index()
        .rename(
            columns={
                "passer_id": "gsis_id",
                "epa": "epa_per_play",
            }
        )
    )

    rush_pbp = pbp[
        (pbp["rush_attempt"] == 1) &
        pbp["rusher_id"].notna()
    ]
    records.append(
        rush_pbp.groupby("rusher_id")["epa"]
        .mean()
        .reset_index()
        .rename(
            columns={
                "rusher_id": "gsis_id",
                "epa": "epa_per_play",
            }
        )
    )

    rec_pbp = pbp[
        (pbp["pass_attempt"] == 1) &
        pbp["receiver_id"].notna()
    ]
    records.append(
        rec_pbp.groupby("receiver_id")["epa"]
        .mean()
        .reset_index()
        .rename(
            columns={
                "receiver_id": "gsis_id",
                "epa": "epa_per_play",
            }
        )
    )

    if not records:
        return pd.DataFrame(columns=["gsis_id", "epa_per_play"])

    return (
        pd.concat(records, ignore_index=True)
        .groupby("gsis_id", as_index=False)["epa_per_play"]
        .mean()
    )


def fetch_cpoe_from_pbp(pbp: pd.DataFrame) -> pd.DataFrame:
    """Calculate season-to-date QB CPOE from play-by-play."""
    try:
        required = {"passer_id", "pass_attempt", "cpoe"}
        missing = required - set(pbp.columns)
        if missing:
            log.warning(
                f"CPOE unavailable — missing columns: {sorted(missing)}"
            )
            return pd.DataFrame(
                columns=["gsis_id", "cpoe", "cpoe_attempts"]
            )

        passes = pbp[
            (pbp["pass_attempt"] == 1) &
            pbp["passer_id"].notna() &
            pbp["cpoe"].notna()
        ].copy()

        if passes.empty:
            return pd.DataFrame(
                columns=["gsis_id", "cpoe", "cpoe_attempts"]
            )

        result = (
            passes.groupby("passer_id")
            .agg(
                cpoe=("cpoe", "mean"),
                cpoe_attempts=("cpoe", "count"),
            )
            .reset_index()
            .rename(columns={"passer_id": "gsis_id"})
        )

        result["gsis_id"] = (
            result["gsis_id"].astype(str).str.strip()
        )
        result["cpoe"] = (
            pd.to_numeric(result["cpoe"], errors="coerce")
            .fillna(0.0)
            .round(3)
        )
        result["cpoe_attempts"] = (
            pd.to_numeric(
                result["cpoe_attempts"], errors="coerce"
            )
            .fillna(0)
            .astype(int)
        )

        log.info(
            f"CPOE calculated — {len(result)} QBs"
        )
        return result

    except Exception as e:
        log.warning(
            f"CPOE calculation failed ({e}) — "
            "defaulting to 0.0"
        )
        return pd.DataFrame(
            columns=["gsis_id", "cpoe", "cpoe_attempts"]
        )


def fetch_epa_per_play(season: int, week: int) -> pd.DataFrame:
    """Compatibility wrapper for callers outside the main pipeline."""
    log.info("Calculating EPA per play from play-by-play (nflreadpy)...")
    try:
        pbp = nfl.load_pbp([season]).to_pandas()
        pbp = pbp[pbp["week"] <= week].copy()
        return fetch_epa_per_play_from_pbp(pbp)
    except Exception as e:
        log.warning(f"EPA calculation failed ({e}) — filling with 0.0")
        return pd.DataFrame(columns=["gsis_id", "epa_per_play"])


def fetch_red_zone_shares(
    season: int,
    week: int,
    pbp: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Compute each player's share of team red-zone touches."""
    log.info("Calculating red zone share from play-by-play (nflreadpy)...")

    try:
        if pbp is None:
            pbp = nfl.load_pbp([season]).to_pandas()

        pbp = pbp[
            (pbp["week"] <= week) &
            (pd.to_numeric(pbp["yardline_100"], errors="coerce") <= 20)
        ].copy()

        rush = (
            pbp[pbp["rush_attempt"] == 1]
            .dropna(subset=["rusher_id"])
            .rename(columns={"rusher_id": "gsis_id"})
        )
        rec = (
            pbp[pbp["pass_attempt"] == 1]
            .dropna(subset=["receiver_id"])
            .rename(columns={"receiver_id": "gsis_id"})
        )

        touches = pd.concat(
            [
                rush[["gsis_id", "posteam"]],
                rec[["gsis_id", "posteam"]],
            ],
            ignore_index=True,
        )

        if touches.empty:
            return pd.DataFrame(
                columns=["gsis_id", "red_zone_share"]
            )

        player_rz = touches.groupby("gsis_id").size().rename(
            "player_rz_touches"
        )
        team_rz = touches.groupby("posteam").size().rename(
            "team_rz_touches"
        )
        player_team = touches.groupby("gsis_id")["posteam"].first()

        result = player_rz.to_frame().join(player_team)
        result = result.join(team_rz, on="posteam")

        result["red_zone_share"] = (
            result["player_rz_touches"] /
            result["team_rz_touches"].clip(lower=1)
        )

        result["red_zone_share"] = (
            pd.to_numeric(
                result["red_zone_share"], errors="coerce"
            )
            .fillna(0.0)
            .clip(0.0, 1.0)
            .round(3)
        )

        return result.reset_index()[
            ["gsis_id", "red_zone_share"]
        ]

    except Exception as e:
        log.warning(
            f"Red zone share calculation failed ({e}) — "
            "defaulting to 0.0"
        )
        return pd.DataFrame(
            columns=["gsis_id", "red_zone_share"]
        )


def fetch_snap_counts(
    season: int,
    week: int,
    crosswalk: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Pull offensive snap participation through the requested week."""
    log.info(
        f"Fetching real snap counts (nflreadpy) — "
        f"season={season}, through week={week}"
    )

    try:
        snaps = nfl.load_snap_counts([season]).to_pandas()
        snaps = snaps[snaps["week"] <= week].copy()

        if crosswalk is None:
            crosswalk = fetch_id_crosswalk()

        crosswalk = (
            crosswalk.dropna(subset=["pfr_id"])
            .drop_duplicates(subset="pfr_id")
        )
        crosswalk_map = dict(
            zip(crosswalk["pfr_id"], crosswalk["gsis_id"])
        )

        pfr_col = (
            "pfr_player_id"
            if "pfr_player_id" in snaps.columns
            else "pfr_id"
        )
        snaps["gsis_id"] = snaps[pfr_col].map(crosswalk_map)
        snaps = snaps.dropna(subset=["gsis_id"])

        snap_pct = (
            snaps.groupby("gsis_id")["offense_pct"]
            .mean()
            .reset_index()
            .rename(columns={"offense_pct": "snap_pct"})
        )

        snap_pct["snap_pct"] = (
            pd.to_numeric(
                snap_pct["snap_pct"], errors="coerce"
            )
            .fillna(0.0)
            .clip(0.0, 1.0)
            .round(3)
        )

        log.info(f"Snap counts matched — {len(snap_pct)} players")
        return snap_pct

    except Exception as e:
        log.warning(
            f"Snap count fetch failed ({e}) — snap_pct will be NaN"
        )
        return pd.DataFrame(columns=["gsis_id", "snap_pct"])


def fetch_usage_stats(
    weekly: pd.DataFrame,
    week: int,
) -> pd.DataFrame:
    """Season-long average target share and air-yards share."""
    return (
        weekly.groupby("player_id")
        .agg(
            target_share=("target_share", "mean"),
            air_yards_share=("air_yards_share", "mean"),
        )
        .reset_index()
        .rename(columns={"player_id": "gsis_id"})
    )


def fetch_nflfastr_data(
    season: int,
    week: int,
    matchup_week: int,
    current_team_map: dict,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Build historical player metrics plus current matchup context.

    Historical player/PBP data use `season` + `week`.
    Current matchup context comes from fetch_current_schedule_context().
    Player stats and PBP are each loaded once.
    """

    log.info(
        f"Fetching weekly player stats (nflreadpy) — "
        f"season={season}, through week={week}"
    )

    weekly = nfl.load_player_stats([season]).to_pandas()
    weekly = weekly.rename(columns={"team": "recent_team"})

    weekly = weekly[
        (weekly["position"].isin(SKILL_POSITIONS)) &
        (weekly["week"] <= week)
    ].copy()

    if weekly.empty:
        log.error("No weekly data returned — check season/week parameters")
        sys.exit(1)

    season_avg = (
        weekly.groupby("player_id")
        .agg(
            name=("player_name", "last"),
            position=("position", "last"),
            team=("recent_team", "last"),
            season_avg_pts=("fantasy_points_ppr", "mean"),
            season_targets=("targets", "sum"),
            season_carries=("carries", "sum"),
            season_games=("week", "count"),
            season_rush_yards=("rushing_yards", "sum"),
            season_rush_tds=("rushing_tds", "sum"),
            season_rec_tds=("receiving_tds", "sum"),
            season_pass_tds=("passing_tds", "sum"),
        )
        .reset_index()
        .rename(columns={"player_id": "gsis_id"})
    )

    season_avg["historical_team"] = (
        season_avg["team"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    last3 = (
        weekly[weekly["week"] >= max(1, week - 2)]
        .groupby("player_id")
        .agg(
            last3_avg_pts=("fantasy_points_ppr", "mean"),
            last3_targets=("targets", "mean"),
            last3_carries=("carries", "mean"),
        )
        .reset_index()
        .rename(columns={"player_id": "gsis_id"})
    )

    log.info("Loading play-by-play once for player/team context...")
    pbp = nfl.load_pbp([season]).to_pandas()
    pbp = pbp[pbp["week"] <= week].copy()

    epa = fetch_epa_per_play_from_pbp(pbp)
    cpoe = fetch_cpoe_from_pbp(pbp)
    usage = fetch_usage_stats(weekly, week)

    crosswalk = fetch_id_crosswalk()
    snap_data = fetch_snap_counts(season, week, crosswalk)
    rz_data = fetch_red_zone_shares(season, week, pbp)

    df = season_avg.merge(
        last3,
        on="gsis_id",
        how="left",
    )

    df["last3_avg_pts"] = (
        df["last3_avg_pts"]
        .fillna(df["season_avg_pts"])
    )

    df = df.merge(epa, on="gsis_id", how="left")
    df = df.merge(cpoe, on="gsis_id", how="left")
    df = df.merge(usage, on="gsis_id", how="left")
    df = df.merge(snap_data, on="gsis_id", how="left")
    df = df.merge(rz_data, on="gsis_id", how="left")

    df["cpoe"] = (
        pd.to_numeric(df["cpoe"], errors="coerce")
        .fillna(0.0)
        .round(3)
    )

    df["cpoe_attempts"] = (
        pd.to_numeric(df["cpoe_attempts"], errors="coerce")
        .fillna(0)
        .astype(int)
    )

    df["snap_pct"] = (
        pd.to_numeric(df["snap_pct"], errors="coerce")
        .fillna(0.0)
        .clip(0.0, 1.0)
        .round(3)
    )

    # Current roster team identity
    if current_team_map:
        df["current_team"] = df["gsis_id"].map(current_team_map)

        matched = df["current_team"].notna().sum()

        log.info(
            f"Current team assignments matched — "
            f"{matched}/{len(df)} players"
        )

        df["team"] = (
            df["current_team"]
            .fillna(df["historical_team"])
            .astype(str)
            .str.upper()
            .str.strip()
        )
    else:
        df["team"] = df["historical_team"]

    # Carry share uses the player-stat dataframe already loaded above.
    df = add_carry_share(
        df,
        season,
        week,
        weekly=weekly,
    )

    log.info("Building team environment and opponent mapping...")

    team_environment = build_team_environment(pbp)

    schedule, _, bye_teams = fetch_current_schedule_context(
        matchup_week
    )
    opponent_map = build_opponent_map(
        schedule,
        matchup_week,
    )

    if opponent_map.empty:
        raise RuntimeError(
            f"Empty opponent map for matchup week {matchup_week}"
        )

    if "team" not in opponent_map.columns:
        raise RuntimeError(
            f"Opponent map missing 'team' column for week {matchup_week}"
        )

    if len(opponent_map) != len(opponent_map["team"].unique()):
        raise RuntimeError(
            f"Duplicate opponent mappings for week {matchup_week}"
        )

    scheduled_teams = NFL_TEAMS - set(bye_teams)

    if set(opponent_map["team"]) != scheduled_teams:
        raise RuntimeError(
            f"Opponent map does not match scheduled teams for week "
            f"{matchup_week}"
        )

    df = attach_team_environment(
        df,
        team_environment,
        opponent_map,
    )

    # ------------------------------------------------------------------
    # Restore legacy positional opponent defensive ranks.
    # Rank 1 = fewest PPR points allowed (toughest).
    # Rank 32 = most PPR points allowed (easiest).
    # ------------------------------------------------------------------
    rank_source = weekly[
        weekly["week"] < matchup_week
    ].copy()

    required_rank_columns = {
        "opponent_team",
        "position",
        "fantasy_points_ppr",
    }
    missing_rank_columns = (
        required_rank_columns - set(rank_source.columns)
    )

    if missing_rank_columns:
        raise RuntimeError(
            "Cannot calculate opp_def_rank; weekly data missing "
            f"columns: {sorted(missing_rank_columns)}"
        )

    rank_source["opponent_team"] = (
        rank_source["opponent_team"]
        .fillna("")
        .astype(str)
        .str.upper()
        .str.strip()
    )

    rank_source["position"] = (
        rank_source["position"]
        .fillna("")
        .astype(str)
        .str.upper()
        .str.strip()
    )

    rank_source["fantasy_points_ppr"] = pd.to_numeric(
        rank_source["fantasy_points_ppr"],
        errors="coerce",
    )

    rank_source = rank_source[
        rank_source["opponent_team"].isin(NFL_TEAMS) &
        rank_source["position"].isin(
            {"QB", "RB", "WR", "TE"}
        ) &
        rank_source["fantasy_points_ppr"].notna()
    ]

    defensive_points = (
        rank_source
        .groupby(
            ["opponent_team", "position"]
        )["fantasy_points_ppr"]
        .mean()
        .reset_index()
    )

    defensive_points["rank"] = 16

    for position in ("QB", "RB", "WR", "TE"):
        mask = defensive_points["position"] == position

        defensive_points.loc[mask, "rank"] = (
            defensive_points.loc[mask, "fantasy_points_ppr"]
            .rank(ascending=True)
            .astype(int)
        )

    rank_lookup = {
        (row.opponent_team, row.position): int(row.rank)
        for row in defensive_points.itertuples(index=False)
    }

    rank_lookup = {
        (row.opponent_team, row.position): int(row.rank)
        for row in defensive_points.itertuples(index=False)
    }

    rank_keys = list(zip(
        df["opponent_team"].fillna("").astype(str).str.upper().str.strip(),
        df["position"].fillna("").astype(str).str.upper().str.strip(),
    ))

    missing_rank_mask = pd.Series(
        [key not in rank_lookup for key in rank_keys],
        index=df.index,
    )

    df["opp_def_rank"] = [
        rank_lookup.get(key, 16)
        for key in rank_keys
    ]

    log.info(
        "Opponent defensive ranks attached — "
        f"{len(rank_lookup)} team/position combinations; "
        f"{missing_rank_mask.sum()} actual fallbacks"
    )

    log.info(
        "Fallbacks by position:\n%s",
        df.loc[missing_rank_mask, "position"]
          .value_counts(dropna=False)
          .to_dict()
    )

    log.info(
        "Fallbacks by opponent:\n%s",
        df.loc[missing_rank_mask, "opponent_team"]
          .value_counts(dropna=False)
          .to_dict()
    )

    if missing_rank_mask.any():
        fallback_columns = [
            col for col in [
                "player_id",
                "player_name",
                "position",
                "team",
                "opponent_team",
            ]
            if col in df.columns
        ]

        log.warning(
            "Players receiving fallback opp_def_rank:\n%s",
            df.loc[
                missing_rank_mask,
                fallback_columns,
            ].to_string(index=False)
        )

    log.info(
        f"Team environment attached — "
        f"{len(team_environment)} teams; "
        f"opponent mapping — {len(opponent_map)} teams"
    )

    log.info(f"Weekly stats pull complete — {len(df)} players")
    return df, bye_teams

def add_carry_share(
    df: pd.DataFrame,
    season: int,
    week: int,
    weekly: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Compute season-to-date carry share.

    Kept as a compatibility function. The main pipeline already has the
    player stats available, so it can pass them here without another fetch.
    """
    try:
        if weekly is None:
            weekly = nfl.load_player_stats([season]).to_pandas()
            weekly = weekly.rename(columns={"team": "recent_team"})

        required = {
            "player_id",
            "position",
            "recent_team",
            "week",
            "carries",
        }
        missing = required - set(weekly.columns)
        if missing:
            raise ValueError(
                f"Carry-share data missing columns: {sorted(missing)}"
            )

        rb = weekly[
            (pd.to_numeric(weekly["week"], errors="coerce") <= week) &
            (weekly["position"] == "RB")
        ].copy()

        rb["carries"] = pd.to_numeric(
            rb["carries"], errors="coerce"
        ).fillna(0)

        if rb.empty:
            df["carry_share"] = 0.0
            return df

        player_carries = (
            rb.groupby(["player_id", "recent_team"])["carries"]
            .sum()
            .rename("player_carries")
        )
        team_carries = (
            rb.groupby("recent_team")["carries"]
            .sum()
            .rename("team_carries")
        )

        carry_df = player_carries.reset_index().merge(
            team_carries.reset_index(),
            on="recent_team",
            how="left",
        )

        carry_df["carry_share"] = (
            carry_df["player_carries"] /
            carry_df["team_carries"].clip(lower=1)
        ).round(3)

        carry_map = dict(
            zip(carry_df["player_id"], carry_df["carry_share"])
        )

        df["carry_share"] = (
            df["gsis_id"].map(carry_map).fillna(0.0)
        )

    except Exception as e:
        log.warning(f"Carry share calculation failed ({e})")
        df["carry_share"] = 0.0

    return df


# ==============================================================================
# Ratings / assembly
# ==============================================================================

def compute_composite_ratings(df: pd.DataFrame) -> pd.DataFrame:
    log.info("Computing player ratings via src/model/ratings.py...")
    return compute_player_ratings(df)


def assemble_player_records(
    df: pd.DataFrame,
    injury_map: dict,
    roster_status_map: dict,
) -> list[dict]:

    records = []

    def num(value, default=0.0):
        if pd.isna(value):
            return default

        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def optional_num(value, digits=4):
        if pd.isna(value):
            return None

        try:
            return round(float(value), digits)
        except (TypeError, ValueError):
            return None

    for _, row in df.iterrows():
        gsis_id = str(row.get("gsis_id", "")).strip()
        if not gsis_id:
            continue

        position = str(row.get("position", "")).upper().strip()
        team = str(row.get("team", "")).upper().strip()
        historical_team = str(
            row.get("historical_team", team)
        ).upper().strip()

        roster_status = str(
            roster_status_map.get(gsis_id, "ACT")
        ).upper().strip()

        if roster_status in {"RET", "CUT"}:
            continue

        inj = injury_map.get(gsis_id, {})

        play_probability = num(
            inj.get("play_probability", 1.0),
            1.0,
        )

        record = {
            "gsis_id": gsis_id,
            "name": str(row.get("name", "Unknown")).strip(),
            "position": position,
            "team": team,
            "historical_team": historical_team,
            "roster_status": roster_status,

            "epa_per_play": round(
                num(row.get("epa_per_play")), 4
            ),
            "cpoe": round(
                num(row.get("cpoe")), 3
            ),
            "cpoe_attempts": int(
                num(row.get("cpoe_attempts"))
            ),
            "target_share": round(
                num(row.get("target_share")), 3
            ),
            "carry_share": round(
                num(row.get("carry_share")), 3
            ),
            "snap_pct": round(
                num(row.get("snap_pct")), 3
            ),
            "red_zone_share": round(
                num(row.get("red_zone_share")), 3
            ),
            "air_yards_share": round(
                num(row.get("air_yards_share")), 3
            ),

            "opponent_team": (
                str(row.get("opponent_team")).upper().strip()
                if pd.notna(row.get("opponent_team"))
                else None
            ),

            "opp_def_rank": int(
                num(row.get("opp_def_rank"), 16)
            ),

            "opp_def_epa": optional_num(
                row.get("opp_def_epa"),
                4,
            ),
            "opp_def_success_rate": optional_num(
                row.get("opp_def_success_rate"),
                3,
            ),
            "opp_pass_def_epa": optional_num(
                row.get("opp_pass_def_epa"),
                4,
            ),
            "opp_pass_def_success_rate": optional_num(
                row.get("opp_pass_def_success_rate"),
                3,
            ),
            "opp_rush_def_epa": optional_num(
                row.get("opp_rush_def_epa"),
                4,
            ),
            "opp_rush_def_success_rate": optional_num(
                row.get("opp_rush_def_success_rate"),
                3,
            ),
            "opp_defense_score": round(
                num(row.get("opp_defense_score"), 50.0), 2
            ),
            "opp_pass_defense_score": round(
                num(row.get("opp_pass_defense_score"), 50.0), 2
            ),
            "opp_rush_defense_score": round(
                num(row.get("opp_rush_defense_score"), 50.0), 2
            ),
            "matchup_score": round(
                num(row.get("matchup_score"), 50.0), 2
            ),
            "team_environment_score": round(
                num(row.get("team_environment_score"), 50.0), 2
            ),

            "team_off_epa": round(
                num(row.get("team_off_epa")), 4
            ),
            "team_off_success_rate": round(
                num(row.get("team_off_success_rate"), 0.5), 3
            ),
            "team_pass_epa": round(
                num(row.get("team_pass_epa")), 4
            ),
            "team_pass_success_rate": round(
                num(row.get("team_pass_success_rate"), 0.5), 3
            ),
            "team_plays": int(
                num(row.get("team_plays"))
            ),
            "team_def_epa": round(
                num(row.get("team_def_epa")), 4
            ),
            "team_def_success_rate": round(
                num(row.get("team_def_success_rate"), 0.5), 3
            ),

            "play_probability": round(
                max(0.0, min(1.0, play_probability)),
                2,
            ),
            "injury_detail": inj.get(
                "injury_detail",
                "Active",
            ),

            "season_avg_pts": round(
                num(row.get("season_avg_pts")), 2
            ),
            "last3_avg_pts": round(
                num(row.get("last3_avg_pts")), 2
            ),
            "season_rush_yards": round(
                num(row.get("season_rush_yards")), 1
            ),
            "season_rush_tds": int(
                num(row.get("season_rush_tds"))
            ),
            "season_rec_tds": int(
                num(row.get("season_rec_tds"))
            ),
            "season_pass_tds": int(
                num(row.get("season_pass_tds"))
            ),

            "composite_rating": round(
                num(row.get("composite_rating"), 50.0), 2
            ),
            "epa_score": round(
                num(row.get("epa_score"), 50.0), 2
            ),
            "usage_score": round(
                num(row.get("usage_score"), 50.0), 2
            ),
            "snap_score": round(
                num(row.get("snap_score"), 50.0), 2
            ),
            "red_zone_score": round(
                num(row.get("red_zone_score"), 50.0), 2
            ),

            "conf": "",
        }

        records.append(record)

    records.sort(
        key=lambda r: r["composite_rating"],
        reverse=True,
    )

    return records


def validate_records(records: list[dict]) -> bool:
    errors = []
    seen_ids = set()

    score_fields = [
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

    for r in records:
        gsis = r.get("gsis_id", "")

        if not gsis:
            errors.append(
                f"Missing gsis_id: {r.get('name')}"
            )

        if gsis in seen_ids:
            errors.append(
                f"Duplicate gsis_id: {gsis} ({r.get('name')})"
            )

        seen_ids.add(gsis)

        probability = r.get("play_probability", -1)

        if not (0.0 <= probability <= 1.0):
            errors.append(
                f"{gsis}: play_probability out of range: "
                f"{probability}"
            )

        for field in score_fields:
            value = r.get(field)

            if value is None or not (0.0 <= value <= 100.0):
                errors.append(
                    f"{gsis}: {field} out of range: {value}"
                )

        opp_def_rank = r.get("opp_def_rank")

        if (
            isinstance(opp_def_rank, bool)
            or not isinstance(opp_def_rank, int)
            or not (1 <= opp_def_rank <= 32)
        ):
            errors.append(
                f"{gsis}: opp_def_rank invalid: {opp_def_rank}"
            )

        cpoe = r.get("cpoe")
        cpoe_attempts = r.get("cpoe_attempts")

        if cpoe is None or not (-100.0 <= cpoe <= 100.0):
            errors.append(
                f"{gsis}: cpoe invalid: {cpoe}"
            )

        if cpoe_attempts is None or cpoe_attempts < 0:
            errors.append(
                f"{gsis}: cpoe_attempts invalid: "
                f"{cpoe_attempts}"
            )

        pass_success = r.get("team_pass_success_rate")

        if pass_success is None or not (0.0 <= pass_success <= 1.0):
            errors.append(
                f"{gsis}: team_pass_success_rate invalid: "
                f"{pass_success}"
            )

    if errors:
        log.error(
            f"Validation FAILED — {len(errors)} error(s):"
        )

        for error in errors[:20]:
            log.error(f"  ✗ {error}")

        return False

    log.info(
        f"✓ Validation passed — "
        f"{len(records)} records, "
        f"{len(seen_ids)} unique GSIS IDs"
    )

    return True

# ==============================================================================
# Output / week detection
# ==============================================================================

def write_output(
    records: list[dict],
    week: int,
    season: int,
    matchup_week: int,
    bye_teams: list[str],
    dry_run: bool,
):
    timestamp = datetime.now(timezone.utc).isoformat()

    js_content = f"""// nfl_data.js — Gridiron Oracle player data
// AUTO-GENERATED by scripts/update_nfl_data.py — DO NOT EDIT MANUALLY
// Season: {season} | Week: {week} | Generated: {timestamp}
// Players: {len(records)}
// Data source: nflreadpy
export const NFL_DATA_META = {{
  season: {season},
  week: {week},
  matchup_season: {CURRENT_ROSTER_SEASON},
  matchup_week: {matchup_week},
  bye_teams: {json.dumps(sorted(bye_teams))},
  generated_at: "{timestamp}",
  player_count: {len(records)},
}};
export const NFL_PLAYERS = {json.dumps(records, indent=2)};
export const PLAYER_BY_GSIS_ID = Object.fromEntries(
  NFL_PLAYERS.map(p => [p.gsis_id, p])
);
export const PLAYERS_BY_POSITION = NFL_PLAYERS.reduce((acc, p) => {{
  if (!acc[p.position]) acc[p.position] = [];
  acc[p.position].push(p);
  return acc;
}}, {{}});
"""

    if dry_run:
        log.info(
            f"DRY RUN — would write {len(records)} players to {OUTPUT_PATH}"
        )
        log.info(
            f"Sample record:\n"
            f"{json.dumps(records[0] if records else {{}}, indent=2)}"
        )
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(js_content, encoding="utf-8")
    log.info(f"✓ Wrote {len(records)} players to {OUTPUT_PATH}")


def detect_current_week(season: int) -> int:
    schedule = nfl.load_schedules([season]).to_pandas()

    required = {"game_type", "week", "away_score", "home_score"}
    missing = required - set(schedule.columns)
    if missing:
        raise ValueError(
            f"Schedule missing required columns: {sorted(missing)}"
        )

    schedule = schedule[
        schedule["game_type"].eq("REG")
    ].copy()

    if schedule.empty:
        raise ValueError(f"No regular-season schedule found for {season}")

    schedule["week"] = pd.to_numeric(
        schedule["week"], errors="coerce"
    )

    complete = (
        schedule["away_score"].notna()
        & schedule["home_score"].notna()
    )

    weeks = sorted(schedule["week"].dropna().astype(int).unique())
    complete_weeks = sorted(
        schedule.loc[complete, "week"].dropna().astype(int).unique()
    )

    if not complete_weeks:
        raise RuntimeError(
            f"No completed regular-season games found for {season}"
        )

    latest = max(complete_weeks)

    for week_number in range(1, latest + 1):
        week_games = schedule[schedule["week"].eq(week_number)]

        if week_games.empty or not (
            week_games["away_score"].notna()
            & week_games["home_score"].notna()
        ).all():
            raise RuntimeError(
                f"Cannot advance feature cutoff: week {week_number} "
                "is incomplete or missing"
            )

    if latest not in weeks:
        raise RuntimeError("Detected completed week is not in schedule")

    log.info(f"Latest fully completed regular-season week: {latest}")
    return latest


# ==============================================================================
# Main
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Gridiron Oracle — NFL data pipeline"
    )

    parser.add_argument(
        "--season",
        type=int,
        default=DEFAULT_SEASON,
    )

    parser.add_argument(
        "--week",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
    )

    args = parser.parse_args()

    season = args.season

    if not 2020 <= season <= 2035:
        parser.error("--season must be between 2020 and 2035")

    if args.week is not None and not 1 <= args.week <= 18:
        parser.error("--week must be between 1 and 18")

    week = (
        args.week
        if args.week is not None
        else detect_current_week(season)
    )
    log.info(
        f"=== Gridiron Oracle data pipeline — "
        f"season={season}, week={week} ==="
    )

    current_team_map = fetch_current_roster_teams(
        CURRENT_ROSTER_SEASON
    )

    roster_status_map = fetch_current_roster_status(
        CURRENT_ROSTER_SEASON
    )

    matchup_week = week + 1

    if matchup_week > 18:
        log.error(
            f"Source week {week} has no valid following regular-season week"
        )
        sys.exit(1)

    log.info(
        f"Forecast target: season={CURRENT_ROSTER_SEASON}, "
        f"week={matchup_week}"
    )

    df, bye_teams = fetch_nflfastr_data(
        season,
        week,
        matchup_week,
        current_team_map,
    )

    injury_map = fetch_injury_report(CURRENT_ROSTER_SEASON)

    df = compute_composite_ratings(df)

    records = assemble_player_records(
        df,
        injury_map,
        roster_status_map,
    )

    if not validate_records(records):
        log.error(
            "Aborting — fix validation errors before deploying"
        )
        sys.exit(1)

    write_output(
        records,
        week,
        season,
        matchup_week,
        bye_teams,
        dry_run=args.dry_run,
    )
    log.info("=== Pipeline complete ===")


if __name__ == "__main__":
    main()
