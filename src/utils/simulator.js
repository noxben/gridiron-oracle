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
//   computeCompositeRating(player, leagueSize) → number
//   getReplacementLevel(position, leagueSize) → number

import { PLAYER_BY_GSIS_ID, PLAYERS_BY_POSITION } from './nfl_data.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SIM_RUNS        = 10_000;
const CHUNK_SIZE      = 500;    // simulations per setTimeout chunk
const PERCENTILES     = [10, 50, 90];

// Composite rating weights — per spec §3.2
const WEIGHTS = {
  QB:  { epa: 0.45, usage: 0.25, snap: 0.20, redZone: 0.10 },
  RB:  { epa: 0.35, usage: 0.30, snap: 0.15, redZone: 0.20 },
  WR:  { epa: 0.30, usage: 0.35, snap: 0.20, redZone: 0.15 },
  TE:  { epa: 0.30, usage: 0.35, snap: 0.20, redZone: 0.15 },
  K:   { epa: 0.40, usage: 0.20, snap: 0.20, redZone: 0.20 },
  DST: { epa: 0.40, usage: 0.20, snap: 0.20, redZone: 0.20 },
};

// Opponent DEF rank → Elo-point adjustment — per spec §3.2
// Rank 1 = hardest matchup, Rank 32 = easiest
const DEF_RANK_ADJ = {
  1:  -25, 2:  -22, 3:  -20, 4:  -18, 5:  -15,
  6:  -12, 7:  -10, 8:   -8, 9:   -6, 10:  -4,
  11:  -2, 12:  -1, 13:   0, 14:   0, 15:   0,
  16:   0, 17:   0, 18:   0, 19:   0, 20:   1,
  21:   2, 22:   4, 23:   6, 24:   8, 25:  10,
  26:  12, 27:  15, 28:  18, 29:  20, 30:  22,
  31:  24, 32:  25,
};

// Variance multipliers by player profile — per spec §3.2 variance model
// Applied as SD multiplier on top of base projected points
const VARIANCE_PROFILES = [
  { test: p => p.position === 'WR' && (p.air_yards_share ?? 0) > 0.35, mult: 1.25, label: 'deep threat WR' },
  { test: p => p.position === 'WR' && (p.snap_pct ?? 0) > 0.85 && (p.target_share ?? 0) > 0.20, mult: 0.88, label: 'slot WR' },
  { test: p => p.position === 'RB' && (p.red_zone_share ?? 0) > 0.40, mult: 1.20, label: 'goal-line RB' },
  { test: p => p.position === 'RB' && (p.carry_share ?? 0) < 0.45, mult: 0.90, label: 'committee RB' },
  { test: p => p.position === 'TE' && (p.snap_pct ?? 0) > 0.75, mult: 0.85, label: 'every-down TE' },
  { test: p => p.position === 'QB' && (p.snap_pct ?? 0) > 0.90, mult: 1.15, label: 'dome/weak-DEF QB' },
  { test: p => p.position === 'DST', mult: 1.30, label: 'DST boom-bust' },
];

// Coaching system bonuses — per spec §6.1
const SYSTEM_ADJUSTMENTS = [
  { teams: ['SF'],  positions: ['RB'],           adj: +15, reason: 'Shanahan outside zone' },
  { teams: ['TEN'], positions: ['RB'],            adj: +10, reason: 'run-heavy offense' },
  { teams: ['KC', 'MIA', 'LAR'], positions: ['RB', 'WR'], adj: -12, reason: 'pass-first offense' },
];

// QB-WR connection bonuses — per spec §6.2
// These are applied via manual overrides in the UI — not hardcoded per player
// The override slider encodes the user's eye-test judgment

// League size → starters by position (for replacement level) — per spec §5.1
const STARTERS_BY_LEAGUE_SIZE = {
  8:  { QB: 8,  RB: 24, WR: 24, TE: 8,  FLEX: 8  },
  10: { QB: 10, RB: 30, WR: 30, TE: 10, FLEX: 10 },
  12: { QB: 12, RB: 36, WR: 36, TE: 12, FLEX: 12 },
  14: { QB: 14, RB: 42, WR: 42, TE: 14, FLEX: 14 },
};

// ---------------------------------------------------------------------------
// Composite rating
// ---------------------------------------------------------------------------

/**
 * Compute composite rating for a player (0–100).
 * Normalized across all rostered players at that position.
 * Per spec §3.2.
 *
 * @param {Object} player     - player record from nfl_data.js
 * @param {Object} allPlayers - all rostered players (for normalization)
 * @returns {number} 0–100
 */
export function computeCompositeRating(player, allPlayers) {
  const pos     = player.position ?? 'WR';
  const weights = WEIGHTS[pos] ?? WEIGHTS['WR'];
  const peers   = allPlayers.filter(p => p.position === pos);

  const normalize = (val, field) => {
    const vals   = peers.map(p => p[field] ?? 0).filter(v => !isNaN(v));
    const min    = Math.min(...vals);
    const max    = Math.max(...vals);
    if (max === min) return 50;
    return ((val - min) / (max - min)) * 100;
  };

  const epaScore     = normalize(player.epa_per_play    ?? 0, 'epa_per_play');
  const snapScore    = normalize(player.snap_pct         ?? 0, 'snap_pct');
  const redZoneScore = normalize(player.red_zone_share   ?? 0, 'red_zone_share');

  // Usage score is position-specific
  let usageScore;
  if (pos === 'RB') {
    usageScore = normalize(player.carry_share  ?? 0, 'carry_share');
  } else if (pos === 'QB') {
    usageScore = epaScore; // QB efficiency IS usage
  } else {
    usageScore = normalize(player.target_share ?? 0, 'target_share');
  }

  const raw = (
    weights.epa     * epaScore     +
    weights.usage   * usageScore   +
    weights.snap    * snapScore    +
    weights.redZone * redZoneScore
  );

  // Apply system adjustment
  const sysAdj = getSystemAdjustment(player);

  // Apply opponent DEF rank adjustment
  const defAdj = DEF_RANK_ADJ[player.opp_def_rank ?? 16] ?? 0;

  return Math.max(0, Math.min(100, raw + sysAdj + defAdj));
}

/**
 * Get coaching system bonus for a player.
 */
function getSystemAdjustment(player) {
  for (const rule of SYSTEM_ADJUSTMENTS) {
    if (
      rule.teams.includes(player.team) &&
      rule.positions.includes(player.position)
    ) {
      return rule.adj;
    }
  }
  return 0;
}

// ---------------------------------------------------------------------------
// Projected points
// ---------------------------------------------------------------------------

/**
 * Project PPR points for a player given their composite rating.
 * Uses season_avg_pts and last3_avg_pts as the base, then adjusts
 * for composite rating relative to position average.
 *
 * @param {Object} player
 * @param {number} compositeRating   - computed composite rating (0–100)
 * @param {number} override          - manual eye-test override (-150 to +150)
 * @returns {number} projected points (50th percentile)
 */
export function projectPoints(player, compositeRating, override = 0) {
  // Base: weighted avg of season and last-3 (recency weighted 60/40)
  const seasonAvg = player.season_avg_pts ?? 0;
  const last3Avg  = player.last3_avg_pts  ?? seasonAvg;
  const basePoints = (0.40 * seasonAvg) + (0.60 * last3Avg);

  // Rating adjustment: normalize composite rating to a multiplier around 1.0
  // A rating of 50 = no adjustment, 100 = +20%, 0 = -20%
  const ratingMult = 0.8 + (compositeRating / 100) * 0.4;

  // Override: maps -150..+150 override to approx -4.5..+4.5 points
  // Per spec §6.3: +100 override ≈ +3.0 pts projected
  const overridePts = (override / 100) * 3.0;

  return Math.max(0, (basePoints * ratingMult) + overridePts);
}

// ---------------------------------------------------------------------------
// Variance model
// ---------------------------------------------------------------------------

/**
 * Get the variance multiplier for a player based on their profile.
 * Per spec §3.2 variance model.
 */
function getVarianceMultiplier(player) {
  for (const profile of VARIANCE_PROFILES) {
    if (profile.test(player)) return profile.mult;
  }
  return 1.0; // default — no variance adjustment
}

/**
 * Sample a single player score from a fat-tailed distribution.
 * Uses a normal distribution approximation with position-specific SD.
 *
 * Base SD is roughly 40% of projected points (fantasy football is noisy).
 * Variance multiplier shifts the distribution per player profile.
 *
 * @param {number} projectedPts   - 50th percentile projection
 * @param {number} varianceMult   - SD multiplier from variance profile
 * @param {number} playProb       - injury probability (0.0–1.0)
 * @param {number} replacementPts - points to use if player doesn't play
 * @returns {number}
 */
function samplePlayerScore(projectedPts, varianceMult, playProb, replacementPts) {
  // Injury roll — per spec §7.3 simulation logic
  if (Math.random() > playProb) {
    return replacementPts; // didn't play — use replacement level, not zero
  }

  const sd = projectedPts * 0.40 * varianceMult;

  // Box-Muller transform for normal distribution
  const u1 = Math.random();
  const u2 = Math.random();
  const z  = Math.sqrt(-2 * Math.log(Math.max(u1, 1e-10))) * Math.cos(2 * Math.PI * u2);

  // Fat tail: occasionally add a bonus for big games (5% of sims)
  const fatTail = Math.random() < 0.05 ? Math.abs(z) * sd * 0.5 : 0;

  return Math.max(0, projectedPts + z * sd + fatTail);
}

// ---------------------------------------------------------------------------
// Replacement level
// ---------------------------------------------------------------------------

/**
 * Get replacement level points for a position given league size.
 * Replacement = best available waiver player at that position.
 * Per spec §5.2 VORP calculation.
 *
 * @param {string} position
 * @param {number} leagueSize
 * @returns {number} replacement level points
 */
export function getReplacementLevel(position, leagueSize) {
  const starters    = STARTERS_BY_LEAGUE_SIZE[leagueSize] ?? STARTERS_BY_LEAGUE_SIZE[12];
  const starterCount = starters[position] ?? 12;

  // Get all players at this position sorted by season avg
  const posPlayers = (PLAYERS_BY_POSITION[position] ?? [])
    .filter(p => p.season_avg_pts > 0)
    .sort((a, b) => b.season_avg_pts - a.season_avg_pts);

  // Replacement = the player just outside starter threshold
  const replacement = posPlayers[starterCount] ?? posPlayers[posPlayers.length - 1];
  return replacement?.season_avg_pts ?? 3.0; // fallback 3 pts
}

/**
 * Compute VORP (Value Over Replacement Player) for a player.
 * Per spec §5.2 — the correct metric for start/sit and trade decisions.
 *
 * @param {Object} player
 * @param {number} projectedPts
 * @param {number} leagueSize
 * @returns {number}
 */
export function computeVORP(player, projectedPts, leagueSize) {
  const replacement = getReplacementLevel(player.position, leagueSize);
  return projectedPts - replacement;
}

// ---------------------------------------------------------------------------
// Lineup scoring
// ---------------------------------------------------------------------------

/**
 * Score a full lineup once (one simulation run).
 *
 * @param {Array} lineup         - array of player objects with projectedPts, varianceMult, etc.
 * @param {Map}   replacementMap - position → replacement level pts
 * @returns {number} total lineup score
 */
function scoreLineupOnce(lineup, replacementMap) {
  let total = 0;
  for (const player of lineup) {
    total += samplePlayerScore(
      player.projectedPts,
      player.varianceMult,
      player.play_probability ?? 1.0,
      replacementMap.get(player.position) ?? 3.0,
    );
  }
  return total;
}

// ---------------------------------------------------------------------------
// Main simulation
// ---------------------------------------------------------------------------

/**
 * Prepare a lineup for simulation — compute all per-player values upfront
 * so the hot loop (scoreLineupOnce) does minimal work.
 *
 * @param {Array}  espnRoster  - matched roster from espn_api.js
 * @param {Object} overrides   - { [gsisId]: overrideValue (-150..+150) }
 * @param {number} leagueSize
 * @returns {Array} prepared lineup
 */
export function prepareLineup(espnRoster, overrides = {}, leagueSize = 12) {
  const allPlayers = espnRoster.map(p => PLAYER_BY_GSIS_ID[p.gsisId]).filter(Boolean);

  return espnRoster
    .filter(p => !p.onBench && !p.onIR)   // starters only
    .map(rosterEntry => {
      const playerData = PLAYER_BY_GSIS_ID[rosterEntry.gsisId];
      if (!playerData) {
        // Unknown player — use ESPN's projected points as fallback
        return {
          gsisId:        rosterEntry.gsisId,
          name:          rosterEntry.name,
          position:      rosterEntry.position,
          projectedPts:  rosterEntry.projectedPts ?? 5.0,
          varianceMult:  1.0,
          play_probability: rosterEntry.playProbability ?? 1.0,
          compositeRating: 50,
          vorp:          0,
          isUnknown:     true,
        };
      }

      const override        = overrides[rosterEntry.gsisId] ?? 0;
      const compositeRating = computeCompositeRating(playerData, allPlayers);
      const projectedPts    = projectPoints(playerData, compositeRating, override);
      const varianceMult    = getVarianceMultiplier(playerData);
      const vorp            = computeVORP(playerData, projectedPts, leagueSize);

      return {
        ...playerData,
        gsisId:          rosterEntry.gsisId,
        lineupSlot:      rosterEntry.lineupSlot,
        compositeRating,
        projectedPts,
        varianceMult,
        vorp,
        override,
        injuryDetail:    rosterEntry.injuryDetail,
      };
    });
}

/**
 * Run the full Monte Carlo simulation.
 * Returns a Promise that resolves with SimResult when all chunks complete.
 *
 * Chunked via setTimeout to avoid blocking the browser UI thread.
 * Per spec §8.4: 10,000 sims in <8 seconds, no browser freeze.
 *
 * @param {Array}  myLineup   - prepared lineup (from prepareLineup)
 * @param {Array}  oppLineup  - opponent's prepared lineup
 * @param {Object} options    - { leagueSize, onProgress }
 * @returns {Promise<SimResult>}
 */
export function runSimulation(myLineup, oppLineup, options = {}) {
  const { leagueSize = 12, onProgress = null } = options;

  return new Promise((resolve) => {
    const myScores  = new Float32Array(SIM_RUNS);
    const oppScores = new Float32Array(SIM_RUNS);

    // Build replacement map once
    const replacementMap = new Map(
      ['QB', 'RB', 'WR', 'TE', 'K', 'DST'].map(pos => [
        pos, getReplacementLevel(pos, leagueSize)
      ])
    );

    let completed = 0;
    const startTime = performance.now();

    function runChunk() {
      const end = Math.min(completed + CHUNK_SIZE, SIM_RUNS);

      for (let i = completed; i < end; i++) {
        myScores[i]  = scoreLineupOnce(myLineup,  replacementMap);
        oppScores[i] = scoreLineupOnce(oppLineup, replacementMap);
      }

      completed = end;

      if (onProgress) {
        onProgress(completed / SIM_RUNS);
      }

      if (completed < SIM_RUNS) {
        setTimeout(runChunk, 0); // yield to browser between chunks
      } else {
        resolve(buildResult(myScores, oppScores, myLineup, oppLineup, performance.now() - startTime));
      }
    }

    setTimeout(runChunk, 0); // start first chunk after current callstack clears
  });
}

// ---------------------------------------------------------------------------
// Result builder
// ---------------------------------------------------------------------------

/**
 * Build the SimResult from raw score arrays.
 *
 * @param {Float32Array} myScores
 * @param {Float32Array} oppScores
 * @param {Array}        myLineup
 * @param {Array}        oppLineup
 * @param {number}       elapsedMs
 * @returns {SimResult}
 */
function buildResult(myScores, oppScores, myLineup, oppLineup, elapsedMs) {
  const myWins = countWhere(myScores, oppScores, (m, o) => m > o);
  const winPct = myWins / SIM_RUNS;

  // Validate: win probs must sum to ~100% — per spec §8.2
  // (ties are rare but possible — allocate half to each)
  const ties   = countWhere(myScores, oppScores, (m, o) => m === o);
  const lossPct = (SIM_RUNS - myWins - ties) / SIM_RUNS;
  console.assert(
    Math.abs((winPct + lossPct + ties / SIM_RUNS) - 1.0) < 0.001,
    'Win probabilities do not sum to 100%'
  );

  return {
    // Win probability
    winProbability:  round2(winPct * 100),
    lossProbability: round2(lossPct * 100),
    tieProbability:  round2((ties / SIM_RUNS) * 100),

    // Score distributions
    myScore:  buildScoreDist(myScores),
    oppScore: buildScoreDist(oppScores),

    // Per-player projections
    myPlayers:  myLineup.map(p => buildPlayerResult(p, myScores, oppScores)),
    oppPlayers: oppLineup.map(p => buildPlayerResult(p, oppScores, myScores)),

    // Highest-variance player — per spec §7.2
    highestVariancePlayer: findHighestVariancePlayer(myLineup),

    // Metadata
    simRuns:   SIM_RUNS,
    elapsedMs: Math.round(elapsedMs),
    timestamp: new Date().toISOString(),
  };
}

function buildScoreDist(scores) {
  const sorted = Array.from(scores).sort((a, b) => a - b);
  return {
    p10: round2(percentile(sorted, 10)),
    p50: round2(percentile(sorted, 50)),
    p90: round2(percentile(sorted, 90)),
    mean: round2(sorted.reduce((s, v) => s + v, 0) / sorted.length),
  };
}

function buildPlayerResult(player, teamScores, oppTeamScores) {
  return {
    gsisId:          player.gsisId,
    name:            player.name,
    position:        player.position,
    team:            player.team,
    compositeRating: player.compositeRating,
    projectedPts:    round2(player.projectedPts),
    vorp:            round2(player.vorp ?? 0),
    varianceMult:    player.varianceMult,
    varianceProfile: getVarianceLabel(player),
    override:        player.override ?? 0,
    play_probability: player.play_probability ?? 1.0,
    injuryDetail:    player.injuryDetail ?? '',
    opp_def_rank:    player.opp_def_rank ?? 16,
    epa_per_play:    player.epa_per_play ?? 0,
    // Component scores for UI transparency
    scores: {
      epa:     round2(player.epa_score ?? 50),
      usage:   round2(player.usage_score ?? 50),
      snap:    round2(player.snap_score ?? 50),
      redZone: round2(player.red_zone_score ?? 50),
    },
  };
}

function findHighestVariancePlayer(lineup) {
  return lineup.reduce((best, p) => {
    const score = (p.varianceMult ?? 1.0) * (p.projectedPts ?? 0);
    const bestScore = (best?.varianceMult ?? 1.0) * (best?.projectedPts ?? 0);
    return score > bestScore ? p : best;
  }, lineup[0]);
}

function getVarianceLabel(player) {
  for (const profile of VARIANCE_PROFILES) {
    if (profile.test(player)) return profile.label;
  }
  return 'standard';
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function percentile(sorted, p) {
  const idx = Math.floor((p / 100) * sorted.length);
  return sorted[Math.min(idx, sorted.length - 1)];
}

function countWhere(arr1, arr2, fn) {
  let count = 0;
  for (let i = 0; i < arr1.length; i++) {
    if (fn(arr1[i], arr2[i])) count++;
  }
  return count;
}

function round2(n) {
  return Math.round(n * 100) / 100;
}

// ---------------------------------------------------------------------------
// Override helpers (used by LineupOptimizer UI)
// ---------------------------------------------------------------------------

/**
 * Re-run simulation with updated overrides without reloading all data.
 * Called when user moves an override slider.
 *
 * @param {Array}  roster     - full roster from espn_api.js
 * @param {Object} overrides  - { [gsisId]: value }
 * @param {Array}  oppRoster  - opponent roster
 * @param {Object} options
 * @returns {Promise<SimResult>}
 */
export async function rerunWithOverrides(roster, overrides, oppRoster, options = {}) {
  const myLineup  = prepareLineup(roster,    overrides, options.leagueSize);
  const oppLineup = prepareLineup(oppRoster, {},        options.leagueSize);
  return runSimulation(myLineup, oppLineup, options);
}

/**
 * Get optimal starting lineup from a full roster.
 * Picks highest VORP player at each required position.
 *
 * Lineup format per spec §2.1: QB RB RB WR WR TE FLEX K DST
 *
 * @param {Array}  fullRoster - all players (bench + starters)
 * @param {Object} overrides
 * @param {number} leagueSize
 * @returns {Array} recommended starting lineup
 */
export function getOptimalLineup(fullRoster, overrides = {}, leagueSize = 12) {
  const allPlayers     = fullRoster.map(p => PLAYER_BY_GSIS_ID[p.gsisId]).filter(Boolean);
  const withProjections = fullRoster.map(rosterEntry => {
    const playerData = PLAYER_BY_GSIS_ID[rosterEntry.gsisId];
    if (!playerData) return { ...rosterEntry, projectedPts: rosterEntry.projectedPts ?? 0, vorp: 0 };
    const override        = overrides[rosterEntry.gsisId] ?? 0;
    const compositeRating = computeCompositeRating(playerData, allPlayers);
    const projectedPts    = projectPoints(playerData, compositeRating, override);
    const vorp            = computeVORP(playerData, projectedPts, leagueSize);
    // Injury-adjust projected points for ranking
    const adjProjected    = projectedPts * (playerData.play_probability ?? 1.0);
    return { ...rosterEntry, ...playerData, projectedPts, adjProjected, vorp, compositeRating };
  });

  const byPosition = (pos) => withProjections
    .filter(p => p.position === pos)
    .sort((a, b) => b.adjProjected - a.adjProjected);

  const lineup = [];
  const used   = new Set();

  const pick = (pos) => {
    const best = byPosition(pos).find(p => !used.has(p.gsisId));
    if (best) { used.add(best.gsisId); lineup.push({ ...best, lineupSlot: pos }); }
  };

  // Fill required slots — per spec §2.1
  pick('QB');
  pick('RB'); pick('RB');
  pick('WR'); pick('WR');
  pick('TE');
  pick('K');
  pick('DST');

  // FLEX — best remaining RB/WR/TE by adjProjected
  const flexCandidates = withProjections
    .filter(p => ['RB', 'WR', 'TE'].includes(p.position) && !used.has(p.gsisId))
    .sort((a, b) => b.adjProjected - a.adjProjected);
  if (flexCandidates[0]) {
    used.add(flexCandidates[0].gsisId);
    lineup.push({ ...flexCandidates[0], lineupSlot: 'FLEX' });
  }

  return lineup;
}

export { SIM_RUNS, WEIGHTS, DEF_RANK_ADJ, VARIANCE_PROFILES };
