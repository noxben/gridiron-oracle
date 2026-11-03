// simulator.js — Gridiron Oracle Monte Carlo Simulation Engine
// =============================================================
// Ported from COOPERcast (March Madness simulator, March 2026).
// Same chunked setTimeout approach, same fat-tailed distribution,
// same injury probability roll, same override application.
//
// Runs 10,000 simulations in <8 seconds without freezing the browser
// by chunking work across animation frames (per spec §8.4).
//
// Primary exports:
//   runSimulation(myLineup, oppLineup, options) → SimResult
//   computeCompositeRating(player, allPlayers) → RatingResult
//   getReplacementLevel(position, leagueSize) → number
//
// v2.0 additions (spec §4.2):
//   buildOppLineupFromLeagueData(oppTeamId, leagueData, idMapping) → rosterArray
//   simulateMatchup(myRoster, oppTeamId, leagueData, idMapping, overrides, options) → Promise<SimResult>

import { PLAYER_BY_GSIS_ID, PLAYERS_BY_POSITION } from './nfl_data.js';

const SIM_RUNS = 10_000;
const CHUNK_SIZE = 500;

// -----------------------------------------------------------------------------
// Position-aware variance
// -----------------------------------------------------------------------------

const POSITION_CV = {
  QB: 0.30,
  RB: 0.38,
  WR: 0.44,
  TE: 0.40,
  K: 0.30,
  DST: 0.52,
};

const VARIANCE_PROFILES = [
  {
    test: p =>
      p.position === 'WR' &&
      (p.air_yards_share ?? 0) > 0.35,
    mult: 1.20,
    label: 'deep threat WR',
  },
  {
    test: p =>
      p.position === 'WR' &&
      (p.snap_pct ?? 0) > 0.80 &&
      (p.target_share ?? 0) > 0.25,
    mult: 0.88,
    label: 'high-volume WR',
  },
  {
    test: p =>
      p.position === 'RB' &&
      (p.red_zone_share ?? 0) > 0.40,
    mult: 1.15,
    label: 'goal-line RB',
  },
  {
    test: p =>
      p.position === 'RB' &&
      (p.carry_share ?? 0) < 0.45,
    mult: 1.10,
    label: 'committee RB',
  },
  {
    test: p =>
      p.position === 'TE' &&
      (p.snap_pct ?? 0) > 0.75 &&
      (p.target_share ?? 0) > 0.18,
    mult: 0.88,
    label: 'every-down TE',
  },
  {
    test: p => p.position === 'DST',
    mult: 1.25,
    label: 'DST boom-bust',
  },
];

// -----------------------------------------------------------------------------
// League starter counts
// -----------------------------------------------------------------------------

const STARTER_COUNTS = {
  8:  { QB: 8,  RB: 24, WR: 24, TE: 8,  FLEX: 8 },
  10: { QB: 10, RB: 30, WR: 30, TE: 10, FLEX: 10 },
  12: { QB: 12, RB: 36, WR: 36, TE: 12, FLEX: 12 },
  14: { QB: 14, RB: 42, WR: 42, TE: 14, FLEX: 14 },
};

// -----------------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------------

export function safeNum(v, fallback = 0) {
  return (typeof v === 'number' && Number.isFinite(v)) ? v : fallback;
}

function clamp(v, min, max) {
  return Math.max(min, Math.min(max, v));
}

function round2(v) {
  return Math.round(v * 100) / 100;
}

function scoreAdjustment(score, maxAdjustment) {
  const normalized = (safeNum(score, 50) - 50) / 50;
  return normalized * maxAdjustment;
}

// -----------------------------------------------------------------------------
// Python ratings are authoritative.
// Keep this function for API compatibility with existing consumers.
// It performs no duplicate JS rating calculation.
// -----------------------------------------------------------------------------

export function computeCompositeRating(player) {
  return {
    rating: clamp(safeNum(player.composite_rating, 50), 0, 100),
    epaScore: clamp(safeNum(player.epa_score, 50), 0, 100),
    usageScore: clamp(safeNum(player.usage_score, 50), 0, 100),
    snapScore: clamp(safeNum(player.snap_score, 50), 0, 100),
    redZoneScore: clamp(safeNum(player.red_zone_score, 50), 0, 100),
  };
}

// -----------------------------------------------------------------------------
// Projection engine
//
// Historical production is the anchor.
// Context modifies the anchor within deliberately bounded ranges.
//
// Composite rating is NOT used as a projection multiplier.
//
// Environment treatment:
//   QB / WR / TE -> passing environment
//   RB / K       -> overall offensive environment
//   DST          -> neutral
// -----------------------------------------------------------------------------

function getBaseProduction(player) {
  const seasonAvg = safeNum(player.season_avg_pts);
  const last3Avg = safeNum(player.last3_avg_pts, seasonAvg);

  if (seasonAvg === 0 && last3Avg === 0) return 0;

  return (0.40 * seasonAvg) + (0.60 * last3Avg);
}

function getEfficiencyAdjustment(player) {
  const epaAdj = scoreAdjustment(player.epa_score, 0.06);

  if (player.position !== 'QB') {
    return epaAdj;
  }

  const attempts = safeNum(player.cpoe_attempts, 0);

  if (attempts < 100) {
    return epaAdj;
  }

  const cpoeAdj = clamp(
    safeNum(player.cpoe, 0) / 100,
    -0.04,
    0.04
  );

  return clamp(
    epaAdj + cpoeAdj,
    -0.08,
    0.08
  );
}

function getOpportunityAdjustment(player) {
  const usage = safeNum(player.usage_score, 50);
  const snap = safeNum(player.snap_score, 50);
  const redZone = safeNum(player.red_zone_score, 50);

  let adjustment;

  switch (player.position) {
    case 'RB':
      adjustment =
        (0.45 * ((usage - 50) / 50)) +
        (0.25 * ((snap - 50) / 50)) +
        (0.30 * ((redZone - 50) / 50));
      break;

    case 'WR':
    case 'TE':
      adjustment =
        (0.50 * ((usage - 50) / 50)) +
        (0.30 * ((snap - 50) / 50)) +
        (0.20 * ((redZone - 50) / 50));
      break;

    case 'QB':
      adjustment =
        (0.55 * ((usage - 50) / 50)) +
        (0.45 * ((snap - 50) / 50));
      break;

    default:
      adjustment = 0;
  }

  return clamp(adjustment, -0.08, 0.08);
}

function getMatchupAdjustment(player) {
  if (player.position === 'K' || player.position === 'DST') {
    return 0;
  }

  return clamp(
    scoreAdjustment(player.matchup_score, 0.08),
    -0.08,
    0.08
  );
}

function getPassingEnvironmentAdjustment(player) {
  const passEpa = safeNum(player.team_pass_epa, 0);
  const passSuccessRate = safeNum(
    player.team_pass_success_rate,
    0.50
  );

  const epaSignal = clamp(
    passEpa / 0.15,
    -1,
    1
  );

  const successSignal = clamp(
    (passSuccessRate - 0.50) / 0.10,
    -1,
    1
  );

  const combinedSignal =
    (0.60 * epaSignal) +
    (0.40 * successSignal);

  return clamp(
    combinedSignal * 0.05,
    -0.05,
    0.05
  );
}

function getOffensiveEnvironmentAdjustment(player) {
  return clamp(
    scoreAdjustment(player.team_environment_score, 0.05),
    -0.05,
    0.05
  );
}

function getEnvironmentAdjustment(player) {
  switch (player.position) {
    case 'QB':
    case 'WR':
    case 'TE':
      return getPassingEnvironmentAdjustment(player);

    case 'RB':
    case 'K':
      return getOffensiveEnvironmentAdjustment(player);

    case 'DST':
      return 0;

    default:
      return getOffensiveEnvironmentAdjustment(player);
  }
}

export function projectPoints(
  player,
  override = 0
) {
  const base = getBaseProduction(player);

  if (base <= 0) {
    return Math.max(0, (override / 100) * 3.0);
  }

  const efficiencyAdj = getEfficiencyAdjustment(player);
  const opportunityAdj = getOpportunityAdjustment(player);
  const matchupAdj = getMatchupAdjustment(player);
  const environmentAdj = getEnvironmentAdjustment(player);
  const overridePts = (override / 100) * 3.0;

  const multiplier =
    1 +
    efficiencyAdj +
    opportunityAdj +
    matchupAdj +
    environmentAdj;

  return Math.max(
    0,
    (base * multiplier) + overridePts
  );
}

/**
 * Diagnostic projection breakdown.
 *
 * This intentionally uses the exact same calculations as projectPoints().
 * It does not create a second projection model.
 *
 * Useful for validating the model with real players and seeing exactly
 * why a projection moved.
 */
export function explainProjection(
  player,
  override = 0
) {
  const base = getBaseProduction(player);

  const efficiencyAdj = getEfficiencyAdjustment(player);
  const opportunityAdj = getOpportunityAdjustment(player);
  const matchupAdj = getMatchupAdjustment(player);
  const environmentAdj = getEnvironmentAdjustment(player);

  const overridePts = (override / 100) * 3.0;

  const multiplier =
    1 +
    efficiencyAdj +
    opportunityAdj +
    matchupAdj +
    environmentAdj;

  const projectedPoints = base <= 0
    ? Math.max(0, overridePts)
    : Math.max(
        0,
        (base * multiplier) + overridePts
      );

  const explanation = {
    name: player.name,
    position: player.position,
    team: player.team,

    baseProduction: Number(base.toFixed(3)),

    adjustments: {
      efficiency: Number(efficiencyAdj.toFixed(4)),
      opportunity: Number(opportunityAdj.toFixed(4)),
      matchup: Number(matchupAdj.toFixed(4)),
      environment: Number(environmentAdj.toFixed(4)),
    },

    multiplier: Number(multiplier.toFixed(4)),
    overridePoints: Number(overridePts.toFixed(3)),
    projectedPoints: Number(projectedPoints.toFixed(3)),
  };

  if (player.position === 'QB') {
    explanation.qbEfficiency = {
      epaScore: safeNum(player.epa_score, 50),
      cpoe: safeNum(player.cpoe, 0),
      cpoeAttempts: safeNum(player.cpoe_attempts, 0),
      cpoeAdjustment:
        safeNum(player.cpoe_attempts, 0) >= 100
          ? clamp(
              safeNum(player.cpoe, 0) / 100,
              -0.04,
              0.04
            )
          : 0,
    };
  }

  if (
    player.position === 'QB' ||
    player.position === 'WR' ||
    player.position === 'TE'
  ) {
    explanation.passingEnvironment = {
      teamPassEpa: safeNum(player.team_pass_epa, 0),
      teamPassSuccessRate: safeNum(
        player.team_pass_success_rate,
        0.50
      ),
      adjustment: Number(
        getPassingEnvironmentAdjustment(player).toFixed(4)
      ),
    };
  } else if (
    player.position === 'RB' ||
    player.position === 'K'
  ) {
    explanation.offensiveEnvironment = {
      teamEnvironmentScore: safeNum(
        player.team_environment_score,
        50
      ),
      adjustment: Number(
        getOffensiveEnvironmentAdjustment(player).toFixed(4)
      ),
    };
  }

  return explanation;
}

// -----------------------------------------------------------------------------
// Variance
//
// Fantasy outcomes are not normally distributed. Touchdowns, explosive plays,
// game script, and role volatility create asymmetric distributions.
//
// The projection remains the median/central expectation. Variance controls
// how widely outcomes spread around that expectation.
//
// Position CV provides the base volatility.
// Role adjustments refine it.
// VARIANCE_PROFILES handles explicit player archetypes.
// -----------------------------------------------------------------------------

export function getVarianceMultiplier(player) {
  for (const profile of VARIANCE_PROFILES) {
    if (profile.test(player)) {
      return profile.mult;
    }
  }

  return 1.0;
}

function getVarianceCoefficient(player, varianceMult = 1.0) {
  let cv = POSITION_CV[player.position] ?? 0.40;

  const targetShare = safeNum(player.target_share, 0);
  const carryShare = safeNum(player.carry_share, 0);
  const airYardsShare = safeNum(player.air_yards_share, 0);
  const redZoneShare = safeNum(player.red_zone_share, 0);
  const snapPct = safeNum(player.snap_pct, 0);
  const matchup = Math.abs(
    safeNum(player.matchup_score, 50) - 50
  );

  // ---------------------------------------------------------------------------
  // Role stability
  //
  // High-volume roles produce more repeatable opportunity and therefore
  // somewhat narrower distributions.
  // ---------------------------------------------------------------------------

  if (targetShare > 0.25) {
    cv *= 0.94;
  }

  if (carryShare > 0.60) {
    cv *= 0.94;
  }

  if (snapPct >= 0.85) {
    cv *= 0.96;
  }

  // ---------------------------------------------------------------------------
  // Explosiveness / touchdown dependence
  //
  // These increase the width of the distribution rather than changing the
  // projection itself.
  // ---------------------------------------------------------------------------

  if (airYardsShare > 0.35) {
    cv *= 1.08;
  }

  if (redZoneShare > 0.40) {
    cv *= 1.05;
  }

  // ---------------------------------------------------------------------------
  // Low-opportunity roles are inherently less stable.
  //
  // Particularly relevant to WR/TE players whose fantasy value depends on
  // limited targets.
  // ---------------------------------------------------------------------------

  if (
    (player.position === 'WR' || player.position === 'TE') &&
    targetShare < 0.12
  ) {
    cv *= 1.06;
  }

  // Committee / uncertain RB usage.
  if (
    player.position === 'RB' &&
    carryShare < 0.45
  ) {
    cv *= 1.05;
  }

  // ---------------------------------------------------------------------------
  // Matchup uncertainty
  //
  // Extreme matchup environments can widen the range of plausible outcomes.
  // Keep this deliberately small because matchup is already represented in
  // the projection itself.
  // ---------------------------------------------------------------------------

  cv *= 1 + ((matchup / 50) * 0.04);

  // Apply explicit archetype profile.
  cv *= varianceMult;

  // Final safety bounds.
  //
  // Lower bound prevents overly deterministic projections.
  // Upper bound prevents a single player from generating absurd Monte Carlo
  // tails.
  return clamp(cv, 0.24, 0.58);
}

function randomNormal() {
  let u = 0;
  let v = 0;

  while (u === 0) {
    u = Math.random();
  }

  while (v === 0) {
    v = Math.random();
  }

  return Math.sqrt(
    -2 * Math.log(u)
  ) * Math.cos(
    2 * Math.PI * v
  );
}

/**
 * Generate a lognormal multiplier with median = 1.0.
 *
 * This gives us:
 *   - non-negative fantasy scores
 *   - a longer upside tail
 *   - a controlled lower tail
 *   - preservation of the input projection as the distribution median
 */
function randomLognormalMultiplier(cv) {
  const sigma = Math.sqrt(
    Math.log(1 + (cv * cv))
  );

  const z = randomNormal();

  return Math.exp(
    (-0.5 * sigma * sigma) +
    (sigma * z)
  );
}

/**
 * Add a small explosive-outcome tail.
 *
 * This represents outcomes such as:
 *   - long touchdowns
 *   - multi-touchdown games
 *   - unusually high efficiency
 *   - defensive/ST scoring spikes
 *
 * The probability remains low so this does not turn every player into a
 * lottery ticket.
 */
function applyExplosiveTail(multiplier, player) {
  let tailProbability = 0.05;

  if (player?.position === 'DST') {
    tailProbability = 0.08;
  }

  if (
    player?.position === 'WR' &&
    safeNum(player.air_yards_share, 0) > 0.35
  ) {
    tailProbability = 0.07;
  }

  if (Math.random() >= tailProbability) {
    return multiplier;
  }

  const tail = Math.abs(randomNormal());

  return multiplier * (
    1 + (tail * 0.18)
  );
}

export function samplePlayerScore(
  projectedPts,
  varianceMult,
  playProb,
  replacementPts,
  player = null
) {
  const probability = clamp(
    safeNum(playProb, 1.0),
    0,
    1
  );

  const projection = Math.max(
    0,
    safeNum(projectedPts, 0)
  );

  const replacement = Math.max(
    0,
    safeNum(replacementPts, 0)
  );

  /*
   * Player does not take the field.
   *
   * Use replacement production rather than zero. This keeps injury/
   * availability uncertainty inside the simulation instead of artificially
   * crushing lineup scores.
   *
   * Replacement itself gets no player-specific variance because it represents
   * the expected output of the replacement-level pool, not this player's
   * individual outcome distribution.
   */
  if (Math.random() > probability) {
    return replacement;
  }

  if (projection <= 0) {
    return 0;
  }

  const cv = player
    ? getVarianceCoefficient(
        player,
        safeNum(varianceMult, 1.0)
      )
    : clamp(
        0.40 * safeNum(varianceMult, 1.0),
        0.24,
        0.58
      );

  let multiplier =
    randomLognormalMultiplier(cv);

  multiplier =
    applyExplosiveTail(
      multiplier,
      player
    );

  return Math.max(
    0,
    projection * multiplier
  );
}

// -----------------------------------------------------------------------------
// Replacement level / VORP
//
// STARTER_COUNTS already contains league-wide starter demand.
// For a 12-team league:
//   QB  = 12
//   RB  = 36
//   WR  = 36
//   TE  = 12
//   FLEX = 12
//
// FLEX demand is distributed across RB/WR/TE according to the available
// projected player pool.
// -----------------------------------------------------------------------------

function getPositionStarterCount(position, leagueSize = 12) {
  return (
    STARTER_COUNTS[leagueSize]?.[position] ??
    STARTER_COUNTS[12]?.[position] ??
    0
  );
}

function getProjectedPlayersAtPosition(position) {
  const players =
    PLAYERS_BY_POSITION[position] ?? [];

  return players
    .map(player => ({
      player,
      projectedPts: projectPoints(
        player,
        0
      ),
    }))
    .filter(item => item.projectedPts > 0)
    .sort(
      (a, b) =>
        b.projectedPts - a.projectedPts
    );
}

function getFlexDemandByPosition(
  leagueSize = 12
) {
  const positions = ['RB', 'WR', 'TE'];
  const viable = {};

  for (const position of positions) {
    viable[position] =
      getProjectedPlayersAtPosition(position)
        .filter(item => item.projectedPts > 5)
        .length;
  }

  const totalViable =
    Object.values(viable)
      .reduce(
        (sum, count) => sum + count,
        0
      );

  if (totalViable <= 0) {
    return {
      RB: 0,
      WR: 0,
      TE: 0,
    };
  }

  const totalFlexSlots =
    STARTER_COUNTS[leagueSize]?.FLEX ??
    STARTER_COUNTS[12]?.FLEX ??
    leagueSize;

  return {
    RB:
      totalFlexSlots *
      (viable.RB / totalViable),

    WR:
      totalFlexSlots *
      (viable.WR / totalViable),

    TE:
      totalFlexSlots *
      (viable.TE / totalViable),
  };
}

export function getReplacementLevel(
  position,
  leagueSize = 12
) {
  const players =
    getProjectedPlayersAtPosition(position);

  if (!players.length) {
    return 0;
  }

  let requiredPlayers =
    getPositionStarterCount(
      position,
      leagueSize
    );

  if (
    position === 'RB' ||
    position === 'WR' ||
    position === 'TE'
  ) {
    requiredPlayers +=
      getFlexDemandByPosition(
        leagueSize
      )[position] ?? 0;
  }

  const replacementIndex =
    Math.floor(requiredPlayers);

  const replacement =
    players[replacementIndex];

  if (replacement) {
    return replacement.projectedPts;
  }

  return players[
    players.length - 1
  ]?.projectedPts ?? 0;
}

export function computeVORP(
  projectedPts,
  position,
  leagueSize = 12
) {
  return (
    safeNum(projectedPts, 0) -
    getReplacementLevel(
      position,
      leagueSize
    )
  );
}
// -----------------------------------------------------------------------------
// Opponent lineup
// -----------------------------------------------------------------------------

// -----------------------------------------------------------------------------
// Opponent lineup
// -----------------------------------------------------------------------------

export function buildOppLineupFromLeagueData(
  oppTeamId,
  leagueData,
  idMapping
) {
  const roster =
    leagueData?.rosters?.[String(oppTeamId)];

  if (!roster || roster.length === 0) {
    console.warn(
      `[simulator] No roster found for opponent team_id=${oppTeamId}`
    );
    return [];
  }

  const mapped = roster.map(p => ({
    gsisId: idMapping?.[p.espn_id] ?? null,
    espnId: p.espn_id,
    name: p.name,
    position: p.position,
    lineupSlot: p.lineup_slot,
    onBench: p.on_bench,
    onIR: p.on_ir,
    playProbability: p.play_probability ?? 1.0,
    projectedPts:
      p.projected_points ??
      p.avg_points ??
      5.0,
    injuryDetail: p.injury_detail ?? '',
  }));

  const hasLineupSet =
    mapped.some(
      p => !p.onBench && !p.onIR
    );

  if (hasLineupSet) {
    return mapped;
  }

  console.info(
    `[simulator] Opponent team ${oppTeamId} has no lineup set — building optimal`
  );

  const byPos = {};

  for (const player of mapped) {
    if (!byPos[player.position]) {
      byPos[player.position] = [];
    }
    byPos[player.position].push(player);
  }

  for (const position of Object.keys(byPos)) {
    byPos[position].sort(
      (a, b) =>
        safeNum(b.projectedPts) -
        safeNum(a.projectedPts)
    );
  }

  const used = new Set();
  const starters = [];

  const pick = (position, slot = position) => {
    const candidates =
      byPos[position] ?? [];

    const player =
      candidates.find(
        p => !used.has(p.espnId)
      );

    if (!player) return;

    used.add(player.espnId);

    starters.push({
      ...player,
      lineupSlot: slot,
      onBench: false,
      onIR: false,
    });
  };

  pick('QB');
  pick('RB');
  pick('RB');
  pick('WR');
  pick('WR');
  pick('TE');
  pick('K');
  pick('DST');

  const flex =
    ['RB', 'WR', 'TE']
      .flatMap(
        position =>
          byPos[position] ?? []
      )
      .filter(
        p => !used.has(p.espnId)
      )
      .sort(
        (a, b) =>
          safeNum(b.projectedPts) -
          safeNum(a.projectedPts)
      )[0];

  if (flex) {
    used.add(flex.espnId);

    starters.push({
      ...flex,
      lineupSlot: 'FLEX',
      onBench: false,
      onIR: false,
    });
  }

  const bench =
    mapped
      .filter(
        p => !used.has(p.espnId)
      )
      .map(p => ({
        ...p,
        lineupSlot: 'BENCH',
        onBench: true,
      }));

  return [
    ...starters,
    ...bench,
  ];
}

// -----------------------------------------------------------------------------
// Prepare lineup
// -----------------------------------------------------------------------------

export function prepareLineup(
  espnRoster,
  overrides = {},
  leagueSize = 12
) {
  const starters = espnRoster.filter(
    p => !p.onBench && !p.onIR
  );

  return starters.map(rosterPlayer => {
    const playerData =
      PLAYER_BY_GSIS_ID[rosterPlayer.gsisId];

    // Preserve existing fallback behavior for players missing from the
    // generated data file.
    if (!playerData) {
      const projectedPts =
        safeNum(rosterPlayer.projectedPts);

      return {
        ...rosterPlayer,

        gsisId: rosterPlayer.gsisId,
        lineupSlot: rosterPlayer.lineupSlot,

        projectedPts,

        compositeRating: 50,
        epa_score: 50,
        usage_score: 50,
        snap_score: 50,
        red_zone_score: 50,

        varianceMult: 1,
        varianceProfile: 'unknown',
        vorp: 0,

        override:
          overrides[rosterPlayer.gsisId] ?? 0,

        play_probability: safeNum(
          rosterPlayer.playProbability,
          1.0
        ),

        injuryDetail:
          rosterPlayer.injuryDetail,

        isUnknown: true,
      };
    }

    const override =
      overrides[playerData.gsisId] ??
      overrides[rosterPlayer.gsisId] ??
      0;

    // Python-generated ratings are authoritative.
    const compositeRating =
      safeNum(
        playerData.composite_rating,
        50
      );

    const projectedPts =
      projectPoints(
        playerData,
        compositeRating,
        override
      );

    const varianceMult =
      getVarianceMultiplier(playerData);

    const vorp =
      computeVORP(
        projectedPts,
        playerData.position,
        leagueSize
      );

    return {
      ...playerData,
      ...rosterPlayer,

      gsisId: playerData.gsisId,
      lineupSlot: rosterPlayer.lineupSlot,

      compositeRating,

      epa_score:
        safeNum(playerData.epa_score, 50),

      usage_score:
        safeNum(playerData.usage_score, 50),

      snap_score:
        safeNum(playerData.snap_score, 50),

      red_zone_score:
        safeNum(playerData.red_zone_score, 50),

      projectedPts,

      varianceMult,

      varianceProfile:
        getVarianceLabel(playerData),

      vorp,

      override,

      play_probability: safeNum(
        playerData.play_probability,
        safeNum(
          rosterPlayer.playProbability,
          1.0
        )
      ),

      injuryDetail:
        rosterPlayer.injuryDetail ??
        playerData.injury_detail,
    };
  });
}

// -----------------------------------------------------------------------------
// Simulation correlation
//
// Players in the same NFL game share some game-environment variance.
// Offensive players on both sides tend to move with overall scoring pace,
// while DST moves in the opposite direction.
// -----------------------------------------------------------------------------

function buildGameShocks(myLineup, oppLineup) {
  const shocks = new Map();

  const allPlayers = [
    ...myLineup,
    ...oppLineup,
  ];

  const teams = new Set(
    allPlayers
      .map(player => player.team ?? player.current_team)
      .filter(Boolean)
  );

  const games = new Map();

  for (const player of allPlayers) {
    const team =
      player.team ??
      player.current_team;

    const opponent =
      player.opponent_team;

    if (!team || !opponent) {
      continue;
    }

    const key = [team, opponent]
      .sort()
      .join(':');

    if (!games.has(key)) {
      games.set(key, {
        teams: new Set(),
      });
    }

    games.get(key).teams.add(team);
    games.get(key).teams.add(opponent);
  }

  for (const team of teams) {
    shocks.set(
      team,
      {
        offensive: 0,
        defensive: 0,
      }
    );
  }

  for (const game of games.values()) {
    const z =
      clamp(
        randomNormal() * 0.07,
        -0.15,
        0.15
      );

    for (const team of game.teams) {
      if (shocks.has(team)) {
        shocks.get(team).offensive = z;
      }
    }
  }

  return shocks;
}

function getTeamCorrelationWeight(player) {
  switch (player.position) {
    case 'QB':
      return 0.75;

    case 'WR':
    case 'TE':
      return 0.65;

    case 'RB':
      return 0.45;

    case 'K':
      return 0.35;

    case 'DST':
      return 0.00;

    default:
      return 0.40;
  }
}

function applyTeamGameShock(
  score,
  player,
  gameShocks
) {
  if (!player || score <= 0) {
    return score;
  }

  const team =
    player.team ??
    player.current_team;

  if (!team || !gameShocks.has(team)) {
    return score;
  }

  const shock =
    gameShocks.get(team);

  if (player.position === 'DST') {
    return Math.max(
      0,
      score * (
        1 -
        shock.offensive * 0.50
      )
    );
  }

  const weight =
    getTeamCorrelationWeight(player);

  return Math.max(
    0,
    score * (
      1 +
      shock.offensive * weight
    )
  );
}

// -----------------------------------------------------------------------------
// Single simulation
// -----------------------------------------------------------------------------

export function scoreLineupOnce(
  lineup,
  replacementMap,
  gameShocks = null
) {
  let score = 0;

  const shocks =
    gameShocks ??
    buildGameShocks(lineup, []);

  for (const player of lineup) {
    const replacementPts =
      replacementMap[player.position] ?? 0;

    let playerScore = samplePlayerScore(
      player.projectedPts,
      player.varianceMult,
      player.play_probability,
      replacementPts,
      player
    );

    playerScore =
      applyTeamGameShock(
        playerScore,
        player,
        shocks
      );

    score += playerScore;
  }

  return score;
}

// -----------------------------------------------------------------------------
// Simulation validation
// -----------------------------------------------------------------------------

export function validateSimulationResult(result) {
  const checks = {
    hasRuns:
      Number.isInteger(result?.simRuns) &&
      result.simRuns === SIM_RUNS,

    finiteScores:
      Number.isFinite(
        result?.myScoreDistribution?.p50
      ) &&
      Number.isFinite(
        result?.oppScoreDistribution?.p50
      ),

    orderedMyDistribution:
      result?.myScoreDistribution?.p10 <=
      result?.myScoreDistribution?.p50 &&
      result?.myScoreDistribution?.p50 <=
      result?.myScoreDistribution?.p90,

    orderedOppDistribution:
      result?.oppScoreDistribution?.p10 <=
      result?.oppScoreDistribution?.p50 &&
      result?.oppScoreDistribution?.p50 <=
      result?.oppScoreDistribution?.p90,

    positiveVariance:
      (
        result?.myScoreDistribution?.p90 -
        result?.myScoreDistribution?.p10
      ) > 0 &&
      (
        result?.oppScoreDistribution?.p90 -
        result?.oppScoreDistribution?.p10
      ) > 0,

    probabilitiesFinite:
      Number.isFinite(result?.winProbability) &&
      Number.isFinite(result?.lossProbability) &&
      Number.isFinite(result?.tieProbability),

    probabilitiesValid:
      Math.abs(
        (result?.winProbability ?? 0) +
        (result?.lossProbability ?? 0) +
        (result?.tieProbability ?? 0) -
        1
      ) < 0.001,
  };

  const failed = Object.entries(checks)
    .filter(([, passed]) => !passed)
    .map(([name]) => name);

  if (failed.length) {
    console.error(
      '[simulator] Validation failed:',
      failed
    );

    return false;
  }

  console.info(
    `[simulator] Validation passed — ${SIM_RUNS.toLocaleString()} simulations`
  );

  return true;
}

// -----------------------------------------------------------------------------
// Matchup simulation
// -----------------------------------------------------------------------------

export function simulateMatchup(
  myRoster,
  oppTeamId,
  leagueData,
  idMapping,
  overrides = {},
  options = {}
) {
  const leagueSize =
    options.leagueSize ?? 12;

  const oppRoster =
    buildOppLineupFromLeagueData(
      oppTeamId,
      leagueData,
      idMapping
    );

  const myLineup =
    prepareLineup(
      myRoster,
      overrides,
      leagueSize
    );

  const oppLineup =
    prepareLineup(
      oppRoster,
      {},
      leagueSize
    );

  if (
    !myLineup.length ||
    !oppLineup.length
  ) {
    console.warn(
      '[simulator] Empty lineup:',
      {
        my: myLineup.length,
        opponent: oppLineup.length,
      }
    );
  }

  return runSimulation(
    myLineup,
    oppLineup,
    leagueSize
  );
}

// -----------------------------------------------------------------------------
// Monte Carlo simulation
// -----------------------------------------------------------------------------

export function runSimulation(
  myLineup,
  oppLineup,
  options = 12
) {
  const leagueSize =
    typeof options === 'number'
      ? options
      : options?.leagueSize ?? 12;

  const replacementMap = {};

  const starterConfig =
    STARTER_COUNTS[leagueSize] ??
    STARTER_COUNTS[12];

  for (const position of Object.keys(starterConfig)) {
    if (position === 'FLEX') {
      continue;
    }

    replacementMap[position] =
      getReplacementLevel(
        position,
        leagueSize
      );
  }

  const myScores =
    new Float32Array(SIM_RUNS);

  const oppScores =
    new Float32Array(SIM_RUNS);

  const startTime =
    performance.now();

  return new Promise(resolve => {
    let index = 0;

    const processChunk = () => {
      const end =
        Math.min(
          index + CHUNK_SIZE,
          SIM_RUNS
        );

      for (; index < end; index++) {
        const gameShocks =
          buildGameShocks(
            myLineup,
            oppLineup
          );

        myScores[index] =
          scoreLineupOnce(
            myLineup,
            replacementMap,
            gameShocks
          );

        oppScores[index] =
          scoreLineupOnce(
            oppLineup,
            replacementMap,
            gameShocks
          );
      }

      if (index < SIM_RUNS) {
        setTimeout(processChunk, 0);
        return;
      }

      const result =
        buildResult(
          myScores,
          oppScores,
          myLineup,
          oppLineup,
          performance.now() - startTime
        );

      validateSimulationResult(result);

      resolve(result);
    };

    processChunk();
  });
}

// -----------------------------------------------------------------------------
// Results
// -----------------------------------------------------------------------------

function findHighestVariancePlayer(lineup) {
  return lineup.reduce(
    (best, player) => {
      const score =
        (player.varianceMult ?? 1.0) *
        (player.projectedPts ?? 0);

      const bestScore =
        (best?.varianceMult ?? 1.0) *
        (best?.projectedPts ?? 0);

      return score > bestScore
        ? player
        : best;
    },
    lineup[0]
  );
}

function getVarianceLabel(player) {
  for (const profile of VARIANCE_PROFILES) {
    if (profile.test(player)) {
      return profile.label;
    }
  }

  return 'standard';
}

function percentile(sorted, p) {
  if (!sorted.length) return 0;

  const idx = Math.floor(
    (p / 100) * sorted.length
  );

  return sorted[
    Math.min(idx, sorted.length - 1)
  ];
}

function buildScoreDist(values) {
  const sorted = Array.from(values).sort((a, b) => a - b);

  return {
    p10: round2(percentile(sorted, 0.10)),
    p50: round2(percentile(sorted, 0.50)),
    p90: round2(percentile(sorted, 0.90)),
    mean: round2(
      sorted.reduce((sum, v) => sum + v, 0) / sorted.length
    ),
  };
}

function buildResult(
  myScores,
  oppScores,
  myPlayers,
  oppPlayers,
  elapsedMs
) {
  let myWins = 0;
  let oppWins = 0;
  let ties = 0;

  for (let i = 0; i < myScores.length; i++) {
    if (myScores[i] > oppScores[i]) {
      myWins++;
    } else if (myScores[i] < oppScores[i]) {
      oppWins++;
    } else {
      ties++;
    }
  }

  const winProbability = myWins / SIM_RUNS;
  const lossProbability = oppWins / SIM_RUNS;
  const tieProbability = ties / SIM_RUNS;

  return {
    winProbability,
    lossProbability,
    tieProbability,

    winPct: round2(winProbability * 100),
    lossPct: round2(lossProbability * 100),

    ties,

    myScoreDistribution: buildScoreDist(myScores),
    oppScoreDistribution: buildScoreDist(oppScores),

    myPlayers: myPlayers.map(buildPlayerResult),
    oppPlayers: oppPlayers.map(buildPlayerResult),

    highestVariancePlayer:
      findHighestVariancePlayer([
        ...myPlayers,
        ...oppPlayers,
      ]),

    simRuns: SIM_RUNS,
    elapsedMs: round2(elapsedMs),
    timestamp: new Date().toISOString(),
    usedRealOppRoster: true,
  };
}

function buildPlayerResult(player) {
  return {
    gsisId: player.gsisId,
    lineupSlot: player.lineupSlot,
    name: player.full_name ?? player.name,
    position: player.position,
    team: player.team ?? player.current_team,

    // -------------------------------------------------------------------------
    // Valuation
    // -------------------------------------------------------------------------

    compositeRating: safeNum(
      player.compositeRating,
      player.composite_rating ?? 50
    ),

    projectedPts: round2(
      safeNum(player.projectedPts)
    ),

    vorp: round2(
      safeNum(player.vorp)
    ),

    // -------------------------------------------------------------------------
    // Availability / variance
    // -------------------------------------------------------------------------

    varianceMult: round2(
      safeNum(player.varianceMult, 1)
    ),

    varianceProfile:
      player.varianceProfile ??
      getVarianceLabel(player),

    play_probability: safeNum(
      player.play_probability,
      1
    ),

    injuryDetail:
      player.injuryDetail ??
      player.injury_detail,

    override: safeNum(
      player.override
    ),

    // -------------------------------------------------------------------------
    // Player efficiency / opportunity
    // -------------------------------------------------------------------------

    epa_per_play: safeNum(
      player.epa_per_play
    ),

    cpoe: safeNum(
      player.cpoe
    ),

    cpoe_attempts: safeNum(
      player.cpoe_attempts
    ),

    target_share: safeNum(
      player.target_share
    ),

    carry_share: safeNum(
      player.carry_share
    ),

    snap_pct: safeNum(
      player.snap_pct
    ),

    red_zone_share: safeNum(
      player.red_zone_share
    ),

    air_yards_share: safeNum(
      player.air_yards_share
    ),

    // -------------------------------------------------------------------------
    // Matchup
    // -------------------------------------------------------------------------

    matchup_score: safeNum(
      player.matchup_score,
      50
    ),

    opponent_team:
      player.opponent_team,

    opp_def_epa: safeNum(
      player.opp_def_epa
    ),

    opp_def_success_rate: safeNum(
      player.opp_def_success_rate,
      0.5
    ),

    opp_pass_def_epa: safeNum(
      player.opp_pass_def_epa
    ),

    opp_pass_def_success_rate: safeNum(
      player.opp_pass_def_success_rate,
      0.5
    ),

    opp_rush_def_epa: safeNum(
      player.opp_rush_def_epa
    ),

    opp_rush_def_success_rate: safeNum(
      player.opp_rush_def_success_rate,
      0.5
    ),

    // -------------------------------------------------------------------------
    // Team environment
    // -------------------------------------------------------------------------

    team_environment_score: safeNum(
      player.team_environment_score,
      50
    ),

    team_off_epa: safeNum(
      player.team_off_epa
    ),

    team_off_success_rate: safeNum(
      player.team_off_success_rate,
      0.5
    ),

    team_pass_epa: safeNum(
      player.team_pass_epa
    ),

    team_pass_success_rate: safeNum(
      player.team_pass_success_rate,
      0.5
    ),

    team_plays: safeNum(
      player.team_plays
    ),

    team_def_epa: safeNum(
      player.team_def_epa
    ),

    team_def_success_rate: safeNum(
      player.team_def_success_rate,
      0.5
    ),

    // -------------------------------------------------------------------------
    // Authoritative Python rating breakdown
    // -------------------------------------------------------------------------

    scores: {
      epa: safeNum(
        player.epa_score,
        50
      ),

      usage: safeNum(
        player.usage_score,
        50
      ),

      snap: safeNum(
        player.snap_score,
        50
      ),

      redZone: safeNum(
        player.red_zone_score,
        50
      ),
    },
  };
}

// -----------------------------------------------------------------------------
// Utility
// -----------------------------------------------------------------------------

export function countWhere(array, predicate) {
  return array.filter(predicate).length;
}

export function rerunWithOverrides(
  myRoster,
  oppTeamId,
  leagueData,
  idMapping,
  overrides = {},
  options = {}
) {
  return simulateMatchup(
    myRoster,
    oppTeamId,
    leagueData,
    idMapping,
    overrides,
    options
  );
}

export function getOptimalLineup(
  fullRoster,
  overrides = {},
  leagueSize = 12
) {
  const players = fullRoster
    .map(p => PLAYER_BY_GSIS_ID[p.gsisId])
    .filter(Boolean)
    .map(player => {
      const rating = computeCompositeRating(
        player,
        fullRoster
      );

      const override =
        overrides[player.gsisId] ?? 0;

      const projectedPts = projectPoints(
        player,
        override
      );

      const playProbability =
        safeNum(player.play_probability, 1.0);

      const varianceMult =
        getVarianceMultiplier(player);

      const vorp =
        computeVORP(
          projectedPts,
          player.position,
          leagueSize
        );

      /*
       * Availability-adjusted projection is used for lineup selection.
       *
       * VORP remains based on the football projection itself.
       * This prevents injury probability from contaminating the underlying
       * player valuation while still making unavailable/uncertain players
       * less attractive for lineup construction.
       */
      const adjProjected =
        projectedPts * playProbability;

      return {
        ...player,

        compositeRating: rating.rating,
        epa_score: rating.epaScore,
        usage_score: rating.usageScore,
        snap_score: rating.snapScore,
        red_zone_score: rating.redZoneScore,

        projectedPts,
        playProbability,
        adjProjected,

        varianceMult,
        varianceProfile:
          getVarianceLabel(player),

        vorp,
        override,
      };
    })
    .filter(
      player =>
        player.playProbability > 0 &&
        player.projectedPts > 0
    );

  const selected = [];
  const used = new Set();

  const takeBest = (
    position,
    count,
    lineupSlot = position
  ) => {
    const candidates = players
      .filter(
        player =>
          player.position === position &&
          !used.has(player.gsisId)
      )
      .sort(
        (a, b) =>
          b.adjProjected - a.adjProjected
      );

    for (const player of candidates.slice(0, count)) {
      selected.push({
        ...player,
        lineupSlot,
      });

      used.add(player.gsisId);
    }
  };

  // Required starting positions.
  takeBest('QB', 1);
  takeBest('RB', 2);
  takeBest('WR', 2);
  takeBest('TE', 1);
  takeBest('K', 1);
  takeBest('DST', 1);

  // ---------------------------------------------------------------------------
  // FLEX
  //
  // FLEX is selected only after the mandatory positional slots are filled.
  // RB / WR / TE then compete directly on availability-adjusted projection.
  // ---------------------------------------------------------------------------

  const flexCandidates = players
    .filter(
      player =>
        ['RB', 'WR', 'TE'].includes(player.position) &&
        !used.has(player.gsisId)
    )
    .sort(
      (a, b) =>
        b.adjProjected - a.adjProjected
    );

  const flex = flexCandidates[0];

  if (flex) {
    selected.push({
      ...flex,
      lineupSlot: 'FLEX',
    });

    used.add(flex.gsisId);
  }

  return selected;
}

// Keep exported for existing imports.
export {
  SIM_RUNS,
  VARIANCE_PROFILES,
};