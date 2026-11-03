"""
Gridiron Oracle — Player Rating Engine
======================================

Phase 1:
- Owns player rating calculations.
- Contains no NFL data-loading code.
- Uses position-relative percentile scoring.
- Missing metrics are neutral (50), not zero.
- Produces the authoritative component scores and composite rating.

The data pipeline imports this module and writes its results into
src/utils/nfl_data.js.

The JavaScript simulator consumes those stored values. It does NOT
recalculate them.
"""

from __future__ import annotations

import pandas as pd


# ---------------------------------------------------------------------------
# Position scope
# ---------------------------------------------------------------------------

RATING_POSITIONS = {"QB", "RB", "WR", "TE"}


# ---------------------------------------------------------------------------
# Composite weights
#
# These are intentionally the Phase 1 weights from the existing Python
# model. They are configuration, not permanent model truth.
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS = {
    "epa": 0.35,
    "usage": 0.30,
    "snap": 0.20,
    "red_zone": 0.15,
}

WR_TE_WEIGHTS = {
    "epa": 0.30,
    "usage": 0.35,
    "snap": 0.20,
    "red_zone": 0.15,
}

RB_WEIGHTS = {
    "epa": 0.35,
    "usage": 0.30,
    "snap": 0.15,
    "red_zone": 0.20,
}


# ---------------------------------------------------------------------------
# Percentile normalization
# ---------------------------------------------------------------------------

def _rank_score(series: pd.Series) -> pd.Series:
    """
    Convert a numeric series to a 0–100 percentile-style score.

    Higher raw value = higher score.

    Missing values receive 50 rather than being treated as zero.
    This is important because missing data is not the same thing as
    poor performance.
    """
    values = pd.to_numeric(series, errors="coerce")

    result = pd.Series(
        50.0,
        index=series.index,
        dtype="float64",
    )

    observed = values.notna()

    if observed.any():
        ranks = values.loc[observed].rank(
            pct=True,
            method="average",
        )

        result.loc[observed] = (
            ranks * 100.0
        ).clip(0, 100)

    return result.round(2)


def position_normalize(
    df: pd.DataFrame,
    column: str,
) -> pd.Series:
    """
    Normalize a metric independently within each position.

    QB EPA is compared with QB EPA.
    RB EPA is compared with RB EPA.
    WR EPA is compared with WR EPA.
    TE EPA is compared with TE EPA.

    Positions outside the current rating model receive neutral 50.
    """
    result = pd.Series(
        50.0,
        index=df.index,
        dtype="float64",
    )

    for position in RATING_POSITIONS:
        mask = df["position"].eq(position)

        if not mask.any():
            continue

        result.loc[mask] = _rank_score(
            df.loc[mask, column]
        )

    return result


# ---------------------------------------------------------------------------
# Position weights
# ---------------------------------------------------------------------------

def _weights_for_position(position: str) -> dict[str, float]:
    if position == "RB":
        return RB_WEIGHTS

    if position in {"WR", "TE"}:
        return WR_TE_WEIGHTS

    return DEFAULT_WEIGHTS


# ---------------------------------------------------------------------------
# Main rating function
# ---------------------------------------------------------------------------

def compute_player_ratings(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute the authoritative Phase 1 player ratings.

    Expected raw columns:

        position
        epa_per_play
        target_share
        carry_share
        snap_pct
        red_zone_share

    Returns a copy of the dataframe with:

        epa_score
        usage_score
        snap_score
        red_zone_score
        composite_rating
    """

    out = df.copy()

    # ---------------------------------------------------------------
    # Start everything at neutral.
    #
    # This is particularly important for K/DST, which currently do
    # not have meaningful values for these player-quality metrics.
    # ---------------------------------------------------------------

    out["epa_score"] = 50.0
    out["usage_score"] = 50.0
    out["snap_score"] = 50.0
    out["red_zone_score"] = 50.0

    # ---------------------------------------------------------------
    # EPA
    # ---------------------------------------------------------------

    relevant = out["position"].isin(RATING_POSITIONS)

    if "epa_per_play" in out.columns:
        out.loc[relevant, "epa_score"] = position_normalize(
            out.loc[relevant],
            "epa_per_play",
        )

    # ---------------------------------------------------------------
    # Snap participation
    # ---------------------------------------------------------------

    if "snap_pct" in out.columns:
        out.loc[relevant, "snap_score"] = position_normalize(
            out.loc[relevant],
            "snap_pct",
        )

    # ---------------------------------------------------------------
    # Red-zone involvement
    # ---------------------------------------------------------------

    if "red_zone_share" in out.columns:
        out.loc[relevant, "red_zone_score"] = position_normalize(
            out.loc[relevant],
            "red_zone_share",
        )

    # ---------------------------------------------------------------
    # Usage
    #
    # RB  -> carry share
    # WR/TE -> target share
    # QB -> temporary EPA proxy
    #
    # The QB usage model is explicitly temporary. A real QB opportunity
    # model belongs in the later QB-specific phase.
    # ---------------------------------------------------------------

    for position in RATING_POSITIONS:
        mask = out["position"].eq(position)

        if not mask.any():
            continue

        if position == "RB":
            if "carry_share" in out.columns:
                out.loc[mask, "usage_score"] = _rank_score(
                    out.loc[mask, "carry_share"]
                )

        elif position in {"WR", "TE"}:
            if "target_share" in out.columns:
                out.loc[mask, "usage_score"] = _rank_score(
                    out.loc[mask, "target_share"]
                )

        elif position == "QB":
            # Phase 1 compatibility proxy.
            out.loc[mask, "usage_score"] = out.loc[
                mask,
                "epa_score",
            ]

    # ---------------------------------------------------------------
    # Composite rating
    # ---------------------------------------------------------------

    def weighted_rating(row: pd.Series) -> float:
        weights = _weights_for_position(
            str(row.get("position", ""))
        )

        rating = (
            row["epa_score"] * weights["epa"]
            + row["usage_score"] * weights["usage"]
            + row["snap_score"] * weights["snap"]
            + row["red_zone_score"] * weights["red_zone"]
        )

        return round(
            max(0.0, min(100.0, rating)),
            2,
        )

    out["composite_rating"] = out.apply(
        weighted_rating,
        axis=1,
    )

    return out