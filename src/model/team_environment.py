from __future__ import annotations

import pandas as pd


# ==============================================================================
# Team environment
# ==============================================================================

def build_team_environment(pbp: pd.DataFrame) -> pd.DataFrame:
    """
    Build season-to-date team offensive and defensive environment.

    EPA is expressed per play.
    Lower defensive EPA is better for the defense.
    """

    required = {
        "posteam",
        "defteam",
        "epa",
        "success",
        "pass_attempt",
        "rush_attempt",
    }

    missing = required - set(pbp.columns)
    if missing:
        raise ValueError(
            f"Team environment missing required columns: {sorted(missing)}"
        )

    df = pbp[
        pbp["posteam"].notna() &
        pbp["defteam"].notna()
    ].copy()

    for column in [
        "epa",
        "success",
        "pass_attempt",
        "rush_attempt",
    ]:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    # ------------------------------------------------------------------
    # Passing plays
    # ------------------------------------------------------------------

    pass_df = df[df["pass_attempt"] == 1].copy()

    # ------------------------------------------------------------------
    # Offense
    # ------------------------------------------------------------------

    offense = (
        df.groupby("posteam")
        .agg(
            team_off_epa=("epa", "mean"),
            team_off_success_rate=("success", "mean"),
            team_plays=("posteam", "size"),
        )
        .reset_index()
        .rename(columns={"posteam": "team"})
    )

    # ------------------------------------------------------------------
    # Passing offense
    # ------------------------------------------------------------------

    pass_offense = (
        pass_df.groupby("posteam")
        .agg(
            team_pass_epa=("epa", "mean"),
            team_pass_success_rate=("success", "mean"),
        )
        .reset_index()
        .rename(columns={"posteam": "team"})
    )

    # ------------------------------------------------------------------
    # Overall defense
    # ------------------------------------------------------------------

    defense = (
        df.groupby("defteam")
        .agg(
            team_def_epa=("epa", "mean"),
            team_def_success_rate=("success", "mean"),
        )
        .reset_index()
        .rename(columns={"defteam": "team"})
    )

    # ------------------------------------------------------------------
    # Pass defense
    # ------------------------------------------------------------------

    pass_defense = (
        pass_df.groupby("defteam")
        .agg(
            pass_def_epa=("epa", "mean"),
            pass_def_success_rate=("success", "mean"),
        )
        .reset_index()
        .rename(columns={"defteam": "team"})
    )

    # ------------------------------------------------------------------
    # Rush defense
    # ------------------------------------------------------------------

    rush_df = df[df["rush_attempt"] == 1].copy()

    rush_defense = (
        rush_df.groupby("defteam")
        .agg(
            rush_def_epa=("epa", "mean"),
            rush_def_success_rate=("success", "mean"),
        )
        .reset_index()
        .rename(columns={"defteam": "team"})
    )

    # ------------------------------------------------------------------
    # Combine
    # ------------------------------------------------------------------

    result = offense.merge(
        defense,
        on="team",
        how="outer",
    )

    result = result.merge(
        pass_defense,
        on="team",
        how="outer",
    )

    result = result.merge(
        pass_offense,
        on="team",
        how="outer",
    )

    result = result.merge(
        rush_defense,
        on="team",
        how="outer",
    )

    defaults = {
        "team_off_epa": 0.0,
        "team_off_success_rate": 0.5,
        "team_pass_epa": 0.0,
        "team_pass_success_rate": 0.5,
        "team_plays": 0,
        "team_def_epa": 0.0,
        "team_def_success_rate": 0.5,
        "pass_def_epa": 0.0,
        "pass_def_success_rate": 0.5,
        "rush_def_epa": 0.0,
        "rush_def_success_rate": 0.5,
    }

    for column, default in defaults.items():
        result[column] = (
            pd.to_numeric(
                result[column],
                errors="coerce",
            )
            .fillna(default)
        )

    return result


# ==============================================================================
# Opponent mapping
# ==============================================================================

def build_opponent_map(
    schedule: pd.DataFrame,
    week: int,
) -> pd.DataFrame:
    """
    Map every team to its upcoming opponent for the requested week.
    """

    required = {
        "week",
        "game_type",
        "away_team",
        "home_team",
    }

    missing = required - set(schedule.columns)
    if missing:
        raise ValueError(
            f"Schedule missing required columns: {sorted(missing)}"
        )

    games = schedule[
        (schedule["week"] == week) &
        (schedule["game_type"] == "REG") &
        schedule["away_team"].notna() &
        schedule["home_team"].notna()
    ][
        ["away_team", "home_team"]
    ].copy()

    away = games.rename(
        columns={
            "away_team": "team",
            "home_team": "opponent_team",
        }
    )

    home = games.rename(
        columns={
            "home_team": "team",
            "away_team": "opponent_team",
        }
    )

    return pd.concat(
        [away, home],
        ignore_index=True,
    )


# ==============================================================================
# Defensive normalization
# ==============================================================================

def _percentile_score(series: pd.Series) -> pd.Series:
    """
    Convert a league distribution into 0–100 percentile scores.

    Higher score = stronger defensive performance.
    """

    numeric = pd.to_numeric(
        series,
        errors="coerce",
    )

    if numeric.notna().sum() < 2:
        return pd.Series(
            50.0,
            index=series.index,
        )

    return (
        numeric.rank(
            method="average",
            pct=True,
        ) * 100
    )


def _defense_score(
    epa: pd.Series,
    success_rate: pd.Series,
) -> pd.Series:
    """
    Defensive strength score.

    Lower EPA and lower offensive success allowed are better.
    """

    epa_score = _percentile_score(-epa)
    success_score = _percentile_score(-success_rate)

    return (
        0.60 * epa_score +
        0.40 * success_score
    ).round(2)


def add_defensive_scores(
    team_environment: pd.DataFrame,
) -> pd.DataFrame:
    """
    Add continuous league-relative defensive strength scores.
    """

    out = team_environment.copy()

    out["defense_score"] = _defense_score(
        out["team_def_epa"],
        out["team_def_success_rate"],
    )

    out["pass_defense_score"] = _defense_score(
        out["pass_def_epa"],
        out["pass_def_success_rate"],
    )

    out["rush_defense_score"] = _defense_score(
        out["rush_def_epa"],
        out["rush_def_success_rate"],
    )

    out["offense_score"] = (
        0.60 * _percentile_score(out["team_off_epa"]) +
        0.40 * _percentile_score(out["team_off_success_rate"])
    ).round(2)

    return out


# ==============================================================================
# Attach context to players
# ==============================================================================

def attach_team_environment(
    players: pd.DataFrame,
    team_environment: pd.DataFrame,
    opponent_map: pd.DataFrame,
) -> pd.DataFrame:
    """
    Attach team environment, opponent environment, and
    position-aware matchup context to players.
    """

    env = add_defensive_scores(team_environment)

    out = players.copy()

    # Own-team environment
    out = out.merge(
        env,
        on="team",
        how="left",
    )

    # Upcoming opponent
    out = out.merge(
        opponent_map,
        on="team",
        how="left",
    )

    # Opponent defensive environment
    opponent_defense = env[
        [
            "team",
            "defense_score",
            "pass_defense_score",
            "rush_defense_score",
            "team_def_epa",
            "team_def_success_rate",
            "pass_def_epa",
            "pass_def_success_rate",
            "rush_def_epa",
            "rush_def_success_rate",
        ]
    ].rename(
        columns={
            "team": "opponent_team",
            "defense_score": "opp_defense_score",
            "pass_defense_score": "opp_pass_defense_score",
            "rush_defense_score": "opp_rush_defense_score",
            "team_def_epa": "opp_def_epa",
            "team_def_success_rate": "opp_def_success_rate",
            "pass_def_epa": "opp_pass_def_epa",
            "pass_def_success_rate": "opp_pass_def_success_rate",
            "rush_def_epa": "opp_rush_def_epa",
            "rush_def_success_rate": "opp_rush_def_success_rate",
        }
    )

    out = out.merge(
        opponent_defense,
        on="opponent_team",
        how="left",
    )

    # ------------------------------------------------------------------
    # Position-aware matchup
    #
    # 50 = neutral
    # >50 = favorable
    # <50 = difficult
    #
    # QB/WR/TE -> pass defense
    # RB        -> rush defense
    # K/DST     -> neutral for now
    # ------------------------------------------------------------------

    out["matchup_score"] = 50.0

    pass_mask = out["position"].isin(["QB", "WR", "TE"])
    rush_mask = out["position"].eq("RB")

    out.loc[pass_mask, "matchup_score"] = (
        out.loc[pass_mask, "opp_pass_defense_score"]
    )

    out.loc[rush_mask, "matchup_score"] = (
        out.loc[rush_mask, "opp_rush_defense_score"]
    )

    # Better team environment should also modestly help.
    out["team_environment_score"] = (
        out["offense_score"]
        .fillna(50.0)
    )

        # ------------------------------------------------------------------
    # Validate and preserve matchup context
    # ------------------------------------------------------------------

    opponent_columns = [
        "opp_defense_score",
        "opp_pass_defense_score",
        "opp_rush_defense_score",
        "opp_def_epa",
        "opp_def_success_rate",
        "opp_pass_def_epa",
        "opp_pass_def_success_rate",
        "opp_rush_def_epa",
        "opp_rush_def_success_rate",
    ]

    playing = out["opponent_team"].notna()
    missing_opponent = (
        playing & out[opponent_columns].isna().any(axis=1)
    )

    if missing_opponent.any():
        examples = out.loc[
            missing_opponent,
            ["gsis_id", "team", "opponent_team"],
        ].head(10).to_dict("records")

        raise RuntimeError(
            "Missing opponent defensive context for scheduled players: "
            f"{examples}"
        )

    # Missing opponent data is legitimate only for teams without a
    # scheduled matchup. Keep those fields null in the output.
    bye_players = ~playing

    out.loc[bye_players, "matchup_score"] = 50.0

    # Own-team environment must exist for every player.
    own_environment_columns = [
        "defense_score",
        "pass_defense_score",
        "rush_defense_score",
        "offense_score",
    ]

    missing_own = out[own_environment_columns].isna().any(axis=1)

    if missing_own.any():
        examples = out.loc[
            missing_own,
            ["gsis_id", "team"],
        ].head(10).to_dict("records")

        raise RuntimeError(
            f"Missing own-team environment: {examples}"
        )

    # Normalize own-team fields and matchup scores only.
    own_columns = [
        "defense_score",
        "pass_defense_score",
        "rush_defense_score",
        "offense_score",
        "matchup_score",
        "team_environment_score",
    ]

    for column in own_columns:
        out[column] = pd.to_numeric(
            out[column],
            errors="coerce",
        )

    if out[own_columns].isna().any().any():
        raise RuntimeError("Invalid own-team or matchup score values")

    # Bye-week opponent statistics intentionally remain NaN.
    return out