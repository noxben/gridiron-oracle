from __future__ import annotations

import pandas as pd


# ==============================================================================
# Team environment
# ==============================================================================

def build_team_environment(pbp: pd.DataFrame) -> pd.DataFrame:
    """Build team offensive and defensive environment from play-by-play."""

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

    numeric = ["epa", "success", "pass_attempt", "rush_attempt"]
    for column in numeric:
        df[column] = pd.to_numeric(df[column], errors="coerce")

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

    defense = (
        df.groupby("defteam")
        .agg(
            team_def_epa=("epa", "mean"),
            team_def_success_rate=("success", "mean"),
        )
        .reset_index()
        .rename(columns={"defteam": "team"})
    )

    pass_defense = (
        df[df["pass_attempt"] == 1]
        .groupby("defteam")
        .agg(
            pass_def_epa=("epa", "mean"),
            pass_def_success_rate=("success", "mean"),
        )
        .reset_index()
        .rename(columns={"defteam": "team"})
    )

    rush_defense = (
        df[df["rush_attempt"] == 1]
        .groupby("defteam")
        .agg(
            rush_def_epa=("epa", "mean"),
            rush_def_success_rate=("success", "mean"),
        )
        .reset_index()
        .rename(columns={"defteam": "team"})
    )

    result = offense.merge(defense, on="team", how="outer")
    result = result.merge(pass_defense, on="team", how="outer")
    result = result.merge(rush_defense, on="team", how="outer")

    defaults = {
        "team_off_epa": 0.0,
        "team_off_success_rate": 0.5,
        "team_plays": 0,
        "team_def_epa": 0.0,
        "team_def_success_rate": 0.5,
        "pass_def_epa": 0.0,
        "pass_def_success_rate": 0.5,
        "rush_def_epa": 0.0,
        "rush_def_success_rate": 0.5,
    }

    for column, default in defaults.items():
        result[column] = pd.to_numeric(
            result[column], errors="coerce"
        ).fillna(default)

    result["team"] = (
        result["team"].astype(str).str.upper().str.strip()
    )

    return result


# ==============================================================================
# Schedule / opponent mapping
# ==============================================================================

def build_opponent_map(
    schedule: pd.DataFrame,
    week: int,
) -> pd.DataFrame:
    """Map each team to its opponent for the requested regular-season week."""

    required = {"week", "game_type", "away_team", "home_team"}
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
    ][["away_team", "home_team"]].copy()

    away = games.rename(
        columns={"away_team": "team", "home_team": "opponent_team"}
    )
    home = games.rename(
        columns={"home_team": "team", "away_team": "opponent_team"}
    )

    result = pd.concat([away, home], ignore_index=True)

    for column in ["team", "opponent_team"]:
        result[column] = (
            result[column].astype(str).str.upper().str.strip()
        )

    return result.drop_duplicates("team")


# ==============================================================================
# League-relative scores
# ==============================================================================

def _percentile_score(series: pd.Series) -> pd.Series:
    """Return 0-100 percentile scores. Missing/insufficient data => 50."""
    numeric = pd.to_numeric(series, errors="coerce")

    if numeric.notna().sum() < 2:
        return pd.Series(50.0, index=series.index)

    return (numeric.rank(method="average", pct=True) * 100).fillna(50.0)


def _defense_score(
    epa: pd.Series,
    success_rate: pd.Series,
) -> pd.Series:
    """Higher = better defense, using lower EPA/success allowed."""
    epa_score = _percentile_score(-pd.to_numeric(epa, errors="coerce"))
    success_score = _percentile_score(
        -pd.to_numeric(success_rate, errors="coerce")
    )
    return (0.60 * epa_score + 0.40 * success_score).round(2)


def add_defensive_scores(
    team_environment: pd.DataFrame,
) -> pd.DataFrame:
    """Add overall, pass, rush defensive and offensive environment scores."""

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
# Player context
# ==============================================================================

def attach_team_environment(
    players: pd.DataFrame,
    team_environment: pd.DataFrame,
    opponent_map: pd.DataFrame,
) -> pd.DataFrame:
    """Attach current team, opponent and position-aware matchup context."""

    required_player = {"team", "position"}
    missing = required_player - set(players.columns)
    if missing:
        raise ValueError(
            f"Players missing required columns: {sorted(missing)}"
        )

    env = add_defensive_scores(team_environment)
    out = players.copy()

    out["team"] = out["team"].astype(str).str.upper().str.strip()

    out = out.merge(env, on="team", how="left")
    out = out.merge(opponent_map, on="team", how="left")

    opponent = env[
        [
            "team",
            "team_def_epa",
            "team_def_success_rate",
            "pass_def_epa",
            "pass_def_success_rate",
            "rush_def_epa",
            "rush_def_success_rate",
            "defense_score",
            "pass_defense_score",
            "rush_defense_score",
        ]
    ].rename(
        columns={
            "team": "opponent_team",
            "team_def_epa": "opp_def_epa",
            "team_def_success_rate": "opp_def_success_rate",
            "pass_def_epa": "opp_pass_def_epa",
            "pass_def_success_rate": "opp_pass_def_success_rate",
            "rush_def_epa": "opp_rush_def_epa",
            "rush_def_success_rate": "opp_rush_def_success_rate",
            "defense_score": "opp_defense_score",
            "pass_defense_score": "opp_pass_defense_score",
            "rush_defense_score": "opp_rush_defense_score",
        }
    )

    out = out.merge(opponent, on="opponent_team", how="left")

    out["matchup_score"] = 50.0

    pass_mask = out["position"].isin(["QB", "WR", "TE"])
    rush_mask = out["position"].eq("RB")

    out.loc[pass_mask, "matchup_score"] = (
        out.loc[pass_mask, "opp_pass_defense_score"].fillna(50.0)
    )
    out.loc[rush_mask, "matchup_score"] = (
        out.loc[rush_mask, "opp_rush_defense_score"].fillna(50.0)
    )

    out["team_environment_score"] = out["offense_score"].fillna(50.0)

    # Internal context fields. Neutral values are deliberately explicit.
    neutral_scores = [
        "defense_score",
        "pass_defense_score",
        "rush_defense_score",
        "offense_score",
        "opp_defense_score",
        "opp_pass_defense_score",
        "opp_rush_defense_score",
        "matchup_score",
        "team_environment_score",
    ]
    neutral_rates = [
        "team_off_success_rate",
        "team_def_success_rate",
        "pass_def_success_rate",
        "rush_def_success_rate",
        "opp_def_success_rate",
        "opp_pass_def_success_rate",
        "opp_rush_def_success_rate",
    ]
    neutral_epa = [
        "team_off_epa",
        "team_def_epa",
        "pass_def_epa",
        "rush_def_epa",
        "opp_def_epa",
        "opp_pass_def_epa",
        "opp_rush_def_epa",
    ]

    for column in neutral_scores:
        if column in out.columns:
            out[column] = pd.to_numeric(
                out[column], errors="coerce"
            ).fillna(50.0)

    for column in neutral_rates:
        if column in out.columns:
            out[column] = pd.to_numeric(
                out[column], errors="coerce"
            ).fillna(0.5)

    for column in neutral_epa:
        if column in out.columns:
            out[column] = pd.to_numeric(
                out[column], errors="coerce"
            ).fillna(0.0)

    if "team_plays" in out.columns:
        out["team_plays"] = pd.to_numeric(
            out["team_plays"], errors="coerce"
        ).fillna(0).astype(int)

    return out
