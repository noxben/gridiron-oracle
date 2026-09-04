// DraftBoard.jsx — Gridiron Oracle
// Pre-draft rankings: ADP + composite rating + VORP, sortable
// Fully standalone — no roster/matchup dependency, works pre-draft or offseason
import { useState, useMemo } from 'react';
import { ADP_LIST } from '../utils/adp_data.js';
import { PLAYERS_BY_POSITION } from '../utils/nfl_data.js';
import { computeCompositeRating, computeVORP, getReplacementLevel } from '../utils/simulator.js';
import { C, font, serif, POS_COLOR } from '../utils/theme.js';
import { AUCTION_HISTORY_BY_GSIS, AUCTION_HISTORY_META } from '../utils/auction_history.js';
import { ESPN_AAV_BY_GSIS } from '../utils/espn_aav.js';
import { ROUND_BASELINES, CURRENT_DRAFT_CLASS } from '../utils/draft_picks.js';
import { QB_CONTEXT_BY_GSIS } from '../utils/qb_context.js';

// ---------------------------------------------------------------------------
// Matching — nfl_data.js stores names as "X.Worthy" (initial + last name),
// so full-name comparison against ADP's "Xavier Worthy" is impossible.
// Match on last name + position only. Team is deliberately excluded —
// FFC's ADP team field has been observed to be unreliable/inconsistent
// with actual rosters (e.g. showing traded/rumored teams), which caused
// false negatives when team was part of the join key.
// ---------------------------------------------------------------------------
function lastNameFromInitialFormat(name) {
  const parts = name.split('.');
  const last = parts.length > 1 ? parts.slice(1).join('.') : parts[0];
  return last.toLowerCase().replace(/[^a-z]/g, '');
}

function fullNameCompact(name) {
  if (!name) return '';
  const SUFFIXES = new Set(['jr', 'sr', 'ii', 'iii', 'iv', 'v']);
  const cleaned = name.toLowerCase().replace(/[.']/g, '');
  const parts = cleaned.split(/[\s-]+/).filter(Boolean).filter(p => !SUFFIXES.has(p));
  return parts.join('').replace(/[^a-z]/g, '');
}

const LEAGUE_SIZE = 12;

// ---------------------------------------------------------------------------
// Rookie Baseline — for players with zero nfl_data.js history (true rookies,
// not yet in an NFL season), project a rough season_avg_pts equivalent using
// historical rookie-season output at the same position and draft round.
// This is a real, data-grounded baseline, not a guess — but it's still a
// rough starting point, not a player-specific projection. Two players
// picked in the same slot can vary widely in actual outcome.
// ---------------------------------------------------------------------------
function getRookieBaseline(playerGsisId, position) {
  const draftInfo = CURRENT_DRAFT_CLASS[playerGsisId];
  if (!draftInfo) return null;
  const posBaselines = ROUND_BASELINES[position];
  if (!posBaselines) return null;
  const roundData = posBaselines[draftInfo.round];
  if (!roundData) return null;
  return {
    projectedSeasonPts: roundData.median,
    round: draftInfo.round,
    pick: draftInfo.pick,
    sampleSize: roundData.count,
  };
}

// ---------------------------------------------------------------------------
// Opportunity Score — pure volume/role signal, deliberately excludes
// epa_per_play and red_zone_share (volatile, regression-prone stats).
// ---------------------------------------------------------------------------
function safeNum(v) {
  return (typeof v === 'number' && !Number.isNaN(v)) ? v : 0;
}

function normalizeInGroup(value, peerValues) {
  const vals = peerValues.map(safeNum);
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  if (max === min) return 50;
  return ((safeNum(value) - min) / (max - min)) * 100;
}

function computeOpportunityScore(player, positionPeers) {
  const pos = player.position;

  if (pos === 'QB') {
    const rushVolume = safeNum(player.season_rush_yards) + safeNum(player.season_rush_tds) * 20;
    const peerRushVolume = positionPeers.map(
      p => safeNum(p.season_rush_yards) + safeNum(p.season_rush_tds) * 20
    );
    const rushScore = normalizeInGroup(rushVolume, peerRushVolume);
    const snapScore = normalizeInGroup(player.snap_pct, positionPeers.map(p => p.snap_pct));
    return Math.round(0.65 * rushScore + 0.35 * snapScore);
  }

  if (pos === 'RB') {
    const carryScore = normalizeInGroup(player.carry_share, positionPeers.map(p => p.carry_share));
    const snapScore = normalizeInGroup(player.snap_pct, positionPeers.map(p => p.snap_pct));
    const targetScore = normalizeInGroup(player.target_share, positionPeers.map(p => p.target_share));
    return Math.round(0.55 * carryScore + 0.30 * snapScore + 0.15 * targetScore);
  }

  // WR / TE
  const targetScore = normalizeInGroup(player.target_share, positionPeers.map(p => p.target_share));
  const snapScore = normalizeInGroup(player.snap_pct, positionPeers.map(p => p.snap_pct));
  const rushVolume = safeNum(player.season_rush_yards) + safeNum(player.season_rush_tds) * 20;
  const peerRushVolume = positionPeers.map(
    p => safeNum(p.season_rush_yards) + safeNum(p.season_rush_tds) * 20
  );
  const rushBonus = normalizeInGroup(rushVolume, peerRushVolume);
  return Math.round(0.55 * targetScore + 0.30 * snapScore + 0.15 * rushBonus);
}

// ---------------------------------------------------------------------------
// TD Regression Flag — compares actual TDs against what a player's red-zone
// opportunity share would predict, using the league-wide TD-per-red-zone-share
// rate at that position as the baseline. RB/WR/TE only — QB passing TDs are
// driven more by skill/scheme than red-zone touch luck.
// ---------------------------------------------------------------------------
function computeTDRegression(player, positionPeers) {
  const pos = player.position;
  if (!['RB', 'WR', 'TE'].includes(pos)) {
    return { actualTds: null, expectedTds: null, diff: null, flag: null };
  }

  const actualTds = safeNum(player.season_rush_tds) + safeNum(player.season_rec_tds);
  const rzShare = safeNum(player.red_zone_share);

  const peersWithRZ = positionPeers.filter(p => safeNum(p.red_zone_share) > 0);
  const totalTds = peersWithRZ.reduce(
    (sum, p) => sum + safeNum(p.season_rush_tds) + safeNum(p.season_rec_tds), 0
  );
  const totalRz = peersWithRZ.reduce((sum, p) => sum + safeNum(p.red_zone_share), 0);
  const leagueRate = totalRz > 0 ? totalTds / totalRz : 0;

  const expectedTds = rzShare * leagueRate;
  const diff = actualTds - expectedTds;

  let flag = null;
  if (rzShare > 0.02) {
    if (diff >= 2 && actualTds >= expectedTds * 1.4) flag = 'risk';
    else if (diff <= -2 && expectedTds >= 2) flag = 'value';
  }

  return {
    actualTds,
    expectedTds: Math.round(expectedTds * 10) / 10,
    diff: Math.round(diff * 10) / 10,
    flag,
  };
}

// ---------------------------------------------------------------------------
// Bid Recommendation — Fair Auction Value
// ---------------------------------------------------------------------------
//
// Auction value is estimated from football signals and calibrated against
// actual ESPN auction prices.
//
// Football signals:
//   - VORP
//   - Composite Rating
//   - Opportunity Score
//
// Each signal is converted to a positional percentile (0–1), so QB/RB/WR/TE
// values can be compared on the same relative scale.
//
// The model does NOT copy ESPN's price directly. Instead:
//
//   1. Build a positional ranking from the football signals.
//   2. Use real ESPN auction prices to establish the dollar scale.
//   3. Place each player on that empirical price curve.
//
// Model $ = football-derived fair value
// Mkt $   = observed ESPN auction average
// Blend $ = weighted combination of Model $ and Market $
//
// Market data is deliberately retained in the final blend because it captures
// things the football model cannot fully see, including historical market
// behavior, player reputation, and recurring auction tendencies.
// ---------------------------------------------------------------------------


// ---------------------------------------------------------------------------
// Percentile helpers
// ---------------------------------------------------------------------------

function percentile(values, p) {
  if (!values.length) return null;

  const sorted = [...values].sort((a, b) => a - b);

  if (sorted.length === 1) {
    return sorted[0];
  }

  const index = (sorted.length - 1) * p;
  const lower = Math.floor(index);
  const upper = Math.ceil(index);

  if (lower === upper) {
    return sorted[lower];
  }

  const weight = index - lower;

  return sorted[lower] +
    (sorted[upper] - sorted[lower]) * weight;
}


// ---------------------------------------------------------------------------
// Positional percentile
// ---------------------------------------------------------------------------
//
// Converts a raw football signal into a 0–1 percentile within the player's
// position.
//
// Example:
//   A WR with VORP in the 90th percentile gets 0.90.
//   A QB with VORP in the 90th percentile also gets 0.90.
//
// This allows relative positional strength to drive auction pricing without
// assuming that raw VORP/Rating/Opportunity scales are identical across
// positions.
// ---------------------------------------------------------------------------

function positionalPercentile(value, values) {
  if (value == null || !values.length) {
    return 0.5;
  }

  const sorted = [...values].sort((a, b) => a - b);

  if (sorted.length === 1) {
    return 0.5;
  }

  let below = 0;

  for (const v of sorted) {
    if (v < value) {
      below++;
    } else {
      break;
    }
  }

  return below / (sorted.length - 1);
}


// ---------------------------------------------------------------------------
// Build positional auction-price curve
// ---------------------------------------------------------------------------
//
// Only players with:
//   - real football model data
//   - VORP
//   - real ESPN market price
//
// are included.
//
// The football signals determine where a player sits on the positional
// strength curve. Actual ESPN prices provide the dollar scale.
//
// Football score:
//   70% VORP
//   20% Composite Rating
//   10% Opportunity
//
// This keeps VORP as the primary valuation signal while allowing rating and
// opportunity to refine the result.
// ---------------------------------------------------------------------------

function buildPositionPriceCurve(pool, position) {
  const samples = pool.filter(p =>
    p.position === position &&
    p.hasModelData &&
    p.vorp != null &&
    p.mktBid != null &&
    p.mktBid > 0
  );

  if (samples.length < 12) return null;

  const vorps = samples.map(p => p.vorp);
  const ratings = samples.map(p => p.compositeRating);
  const opportunities = samples.map(p => p.opportunityScore);

  const scored = samples.map(p => {
    const vorpPct = positionalPercentile(p.vorp, vorps);
    const ratingPct = positionalPercentile(p.compositeRating, ratings);
    const opportunityPct = positionalPercentile(
      p.opportunityScore,
      opportunities
    );

    const footballScore =
      0.70 * vorpPct +
      0.20 * ratingPct +
      0.10 * opportunityPct;

    return {
      ...p,
      footballScore,
    };
  });

  scored.sort((a, b) => a.footballScore - b.footballScore);

  return {
    samples: scored,
    marketPrices: scored.map(p => p.mktBid),
  };
}


// ---------------------------------------------------------------------------
// Predict Model $
// ---------------------------------------------------------------------------
//
// Converts the player's football signals into positional percentiles, creates
// the player's football score, then maps that score onto the observed ESPN
// auction-price distribution for that position.
//
// This is an empirical price curve, not a regression equation. That is
// intentional: with relatively small positional samples, directly mapping
// football strength to the observed market distribution is more stable and
// easier to interpret than fitting a heavily parameterized price model.
// ---------------------------------------------------------------------------

function predictModelPrice(player, curve) {
  if (!curve || player.vorp == null) return null;

  const vorpPct = positionalPercentile(
    player.vorp,
    curve.samples.map(p => p.vorp)
  );

  const ratingPct = positionalPercentile(
    player.compositeRating ?? 50,
    curve.samples.map(p => p.compositeRating)
  );

  const opportunityPct = positionalPercentile(
    player.opportunityScore ?? 50,
    curve.samples.map(p => p.opportunityScore)
  );

  const footballScore =
    0.70 * vorpPct +
    0.20 * ratingPct +
    0.10 * opportunityPct;

  const scores = curve.samples.map(p => p.footballScore);
  const prices = curve.samples.map(p => p.mktBid);

  const scorePct = positionalPercentile(
    footballScore,
    scores
  );

  return Math.round(percentile(prices, scorePct));
}


// ---------------------------------------------------------------------------
// Model / Market blend
// ---------------------------------------------------------------------------
//
// Model $ = football-derived estimate.
// Mkt $   = actual ESPN auction price.
//
// The current blend intentionally gives the market substantial weight:
//
//   20% Model
//   80% Market
//
// This means Blend $ is primarily an estimate of expected auction behavior,
// with the football model providing a correction rather than attempting to
// completely replace the market.
//
// If there is no market price, use the model alone.
// ---------------------------------------------------------------------------

function computeBlendedBid(modelBid, mktBid) {
  if (modelBid == null) return null;
  if (mktBid == null) return modelBid;

  return Math.round(
    0.20 * modelBid +
    0.80 * mktBid
  );
}
// ---------------------------------------------------------------------------
// Team Change Flag — compares a player's team in nfl_data.js (their team
// during the season the stats were pulled) against their current team in
// ADP_LIST (live mock draft data). A mismatch means the player's model
// score reflects a situation that no longer applies — trades, signings,
// etc. This doesn't correct the pricing, but makes the reason for any
// model-vs-market disagreement visible at a glance.
// ---------------------------------------------------------------------------
const TEAM_ALIASES_FOR_COMPARISON = {
  LA: 'LAR', WSH: 'WAS', JAC: 'JAX',
};

function normalizeTeamForComparison(team) {
  const t = (team ?? '').toUpperCase();
  return TEAM_ALIASES_FOR_COMPARISON[t] ?? t;
}

function hasTeamChanged(adpEntry, playerData) {
  if (!playerData?.team || !adpEntry?.team) return false;
  return normalizeTeamForComparison(playerData.team) !== normalizeTeamForComparison(adpEntry.team);
}

// ---------------------------------------------------------------------------
// Positional Tiers — groups players within each position into tiers based
// on VORP "cliffs": gaps between consecutive players that are meaningfully
// larger than the surrounding spread. Tier 1 = top tier at that position.
// Only players with real model data (hasModelData) are tiered; players
// without model data get tier = null.
// ---------------------------------------------------------------------------
function computeTiers(poolWithVorp) {
  const tierMap = new Map(); // key -> tier number

  const positions = ['QB', 'RB', 'WR', 'TE', 'K', 'DST'];
  for (const pos of positions) {
    const posPlayers = poolWithVorp
      .filter(p => p.position === pos && p.hasModelData && p.vorp != null)
      .sort((a, b) => b.vorp - a.vorp);

    if (posPlayers.length === 0) continue;

    const vorps = posPlayers.map(p => p.vorp);
    const range = Math.max(vorps[0] - vorps[vorps.length - 1], 1);
    // Cliff threshold: a gap counts as a new tier if it's at least this big.
    // Floor of 1.0 point prevents near-zero ranges from producing a new
    // tier on every tiny gap.
    const threshold = Math.max(1.0, range * 0.12);

    let tier = 1;
    tierMap.set(posPlayers[0].key, tier);
    for (let i = 1; i < posPlayers.length; i++) {
      const gap = posPlayers[i - 1].vorp - posPlayers[i].vorp;
      if (gap > threshold) tier++;
      tierMap.set(posPlayers[i].key, tier);
    }
  }

  return tierMap;
}

// ---------------------------------------------------------------------------
// Value-vs-ADP Flag — compares each player's overall ADP rank against their
// overall VORP rank. A player whose model rank is meaningfully better than
// their market ADP rank is flagged as a "value" (market undervaluing them
// relative to opportunity/production); the reverse is flagged as a "reach".
// Thresholds are picks-based since ADP itself is pick-based.
// ---------------------------------------------------------------------------
function computeValueFlags(poolWithVorp) {
  const flagMap = new Map(); // key -> { adpRank, vorpRank, diff, flag }

  const byAdp = [...poolWithVorp].sort((a, b) => (a.adp ?? 999) - (b.adp ?? 999));
  byAdp.forEach((p, i) => { p._adpRank = i + 1; });

  const withVorp = poolWithVorp.filter(p => p.hasModelData && p.vorp != null);
  const byVorp = [...withVorp].sort((a, b) => b.vorp - a.vorp);
  byVorp.forEach((p, i) => { p._vorpRank = i + 1; });

  for (const p of poolWithVorp) {
    if (!p.hasModelData || p._vorpRank == null) {
      flagMap.set(p.key, { adpRank: p._adpRank, vorpRank: null, diff: null, flag: null });
      continue;
    }
    const diff = p._adpRank - p._vorpRank; // positive = model likes them better than market
    let flag = null;
    if (diff >= 15) flag = 'value';
    else if (diff <= -15) flag = 'reach';
    flagMap.set(p.key, { adpRank: p._adpRank, vorpRank: p._vorpRank, diff, flag });
  }

  return flagMap;
}

// ---------------------------------------------------------------------------
// Sort descriptions — shown as tooltips on buttons + a standing line above table
// ---------------------------------------------------------------------------
const SORT_DESCRIPTIONS = {
  adp: 'Market consensus draft position, from live mock drafts.',
  composite: 'Overall model rating — blends efficiency, usage, snaps, and red zone role. Includes stats that can swing year-to-year.',
  opportunity: 'Pure role/volume signal — target share, carry share, snaps, rushing volume. Excludes volatile efficiency stats.',
  vorp: 'Value over replacement — points above a freely available player at the same position.',
};

// ---------------------------------------------------------------------------
// Data pool
// ---------------------------------------------------------------------------
function useDraftPool() {
  return useMemo(() => {
    const allNflPlayers = Object.values(PLAYERS_BY_POSITION).flat();
    const replacementCache = {};
    const getReplacement = (pos) => {
      if (!(pos in replacementCache)) {
        replacementCache[pos] = getReplacementLevel(pos, LEAGUE_SIZE);
      }
      return replacementCache[pos];
    };
    const findMatch = (adpEntry) => {
      const compact = fullNameCompact(adpEntry.name);
      const candidates = PLAYERS_BY_POSITION[adpEntry.position] ?? [];

      const matches = candidates.filter(p => {
        const last = lastNameFromInitialFormat(p.name);
        return last && compact.endsWith(last);
      });

      if (matches.length === 0) return null;
      if (matches.length === 1) return matches[0];

      const teamMatch = matches.find(
        p => normalizeTeamForComparison(p.team) === normalizeTeamForComparison(adpEntry.team)
      );
      if (teamMatch) return teamMatch;

      console.warn(`[DraftBoard] Ambiguous match for "${adpEntry.name}" (${adpEntry.position}) — ${matches.length} candidates, no team match. Picking first.`);
      return matches.sort((a, b) => lastNameFromInitialFormat(b.name).length - lastNameFromInitialFormat(a.name).length)[0];
    };

    // --- Pass 1: base data, ratings, opportunity, TD regression, market bid ---
    const basePool = ADP_LIST.map(adpEntry => {
      const playerData = findMatch(adpEntry);
      const base = {
        name: adpEntry.name,
        position: adpEntry.position,
        team: adpEntry.team,
        bye: adpEntry.bye,
        adp: adpEntry.adp,
        adpFormatted: adpEntry.adp_formatted,
        adpHigh: adpEntry.adp_high,
        adpLow: adpEntry.adp_low,
        timesDrafted: adpEntry.times_drafted,
        key: adpEntry.player_id ?? adpEntry.normalized_name ?? adpEntry.name,
      };

        if (!playerData) {
          const rookieGsis = Object.keys(CURRENT_DRAFT_CLASS).find(gsis => {
            const info = CURRENT_DRAFT_CLASS[gsis];
            return info.position === adpEntry.position &&
                   fullNameCompact(info.name) === fullNameCompact(adpEntry.name);
          });
         const rookieBaseline = rookieGsis ? getRookieBaseline(rookieGsis, adpEntry.position) : null;

        if (rookieBaseline) {
          const replacement = getReplacement(adpEntry.position);
          const vorp = rookieBaseline.projectedSeasonPts - replacement;
          return {
            ...base,
            hasModelData: false,
            isRookieBaseline: true,
            compositeRating: null,
            opportunityScore: null,
            tdRegression: null,
            mktBid: null,
            rosterStatus: null,
            injuryDetail: null,        // ADD
            playProbability: null,     // ADD
            teamChanged: false,
            vorp,
            seasonAvgPts: rookieBaseline.projectedSeasonPts,
            rookieDraftInfo: rookieBaseline,
          };
        }

        return { ...base, hasModelData: false, isRookieBaseline: false, compositeRating: null, vorp: null, seasonAvgPts: null, opportunityScore: null, tdRegression: null, injuryDetail: null, playProbability: null, mktBid: null, rosterStatus: null, teamChanged: false };
      }

      const { rating: compositeRating } = computeCompositeRating(playerData, allNflPlayers);
      const seasonAvgPts = playerData.season_avg_pts ?? 0;
      const replacement = getReplacement(playerData.position);
      const vorp = seasonAvgPts - replacement;
      const rosterStatus = playerData.roster_status ?? 'ACT';
      const injuryDetail = playerData.injury_detail ?? 'Active';
      const playProbability = playerData.play_probability ?? 1.0;
      const positionPeers = PLAYERS_BY_POSITION[playerData.position] ?? [];
      const opportunityScore = computeOpportunityScore(playerData, positionPeers);
      const tdRegression = computeTDRegression(playerData, positionPeers);
      const espnAav = ESPN_AAV_BY_GSIS[playerData.gsis_id];
      const mktBid = espnAav?.aav ?? null;
      const teamChanged = hasTeamChanged(adpEntry, playerData);
      const qbContext = QB_CONTEXT_BY_GSIS[playerData.gsis_id] ?? null;
      return { ...base, hasModelData: true, isRookieBaseline: false, compositeRating, vorp, seasonAvgPts, opportunityScore, tdRegression, rosterStatus, injuryDetail, mktBid, playProbability, teamChanged, qbContext };
    });

	// --- Pass 2: tiers + value-vs-ADP + auction pricing ---
	const tierMap = computeTiers(basePool);
	const valueFlagMap = computeValueFlags(basePool);
	
	// Build one empirical price curve per position.
	// The football model determines the player's position on the curve.
	// Actual ESPN market prices determine the dollar scale.
	const positions = ['QB', 'RB', 'WR', 'TE', 'K', 'DST'];
	const priceModels = {};
	
	for (const pos of positions) {
	  const curve = buildPositionPriceCurve(basePool, pos);
	  priceModels[pos] = curve;
	
	  if (!curve) {
		console.log(
		  `[DraftBoard] Price curve for ${pos}: insufficient data`
		);
		continue;
	  }
	
	  const marketPrices = curve.marketPrices;
	
	  console.log(
		`[DraftBoard] Price curve for ${pos}: ` +
		`n=${curve.samples.length}, ` +
		`median=$${percentile(marketPrices, 0.50)?.toFixed(1)}, ` +
		`P90=$${percentile(marketPrices, 0.90)?.toFixed(1)}`
	  );
	}
	
	return basePool.map(p => {
	  if (!p.hasModelData && !p.isRookieBaseline) {
		return {
		  ...p,
		  tier: tierMap.get(p.key) ?? null,
		  valueFlag: valueFlagMap.get(p.key) ?? null,
		  modelBid: null,
		  blendedBid: null,
		};
	  }
	
	  const model = priceModels[p.position];
	
	  // Rookies have no compositeRating/opportunityScore.
	  // Use neutral values so their VORP-based baseline can still be priced.
	  const playerForPricing = p.isRookieBaseline
		? {
			...p,
			compositeRating: p.compositeRating ?? 50,
			opportunityScore: p.opportunityScore ?? 50,
		  }
		: p;
	
	  const modelBid = predictModelPrice(playerForPricing, model);
	  const blendedBid = computeBlendedBid(modelBid, p.mktBid);
	
	  return {
		...p,
		tier: tierMap.get(p.key) ?? null,
		valueFlag: valueFlagMap.get(p.key) ?? null,
		modelBid,
		blendedBid,
	  };
	});
  }, []);
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------
const TIER_COLORS = [C.accent, '#90d060', C.amber, C.textMid, C.textDim];

function PosTag({ pos }) {
  const color = POS_COLOR[pos] ?? C.textMid;
  return (
    <span style={{
      display: 'inline-block', padding: '1px 5px', borderRadius: '3px',
      fontSize: '10px', fontWeight: '700', letterSpacing: '0.10em',
      background: color + '18', color, marginRight: '10px',
      minWidth: '28px', textAlign: 'center',
    }}>{pos}</span>
  );
}

function TierTag({ tier }) {
  if (tier == null) return null;
  const color = TIER_COLORS[Math.min(tier - 1, TIER_COLORS.length - 1)];
  return (
    <span
      title={`Tier ${tier} at this position`}
      style={{
        display: 'inline-block',
        padding: '1px 5px',
        borderRadius: '3px',
        fontSize: '10px',
        fontWeight: '700',
        letterSpacing: '0.06em',
        background: color + '18',
        color,
        marginRight: '10px',
      }}
    >T{tier}</span>
  );
}

function SortToggle({ sortKey, current, onSelect, label, description }) {
  const active = sortKey === current;
  return (
    <button
      onClick={() => onSelect(sortKey)}
      title={description}
      style={{
        background: active ? C.accent + '18' : 'transparent',
        color: active ? C.accent : C.textDim,
        border: `1px solid ${active ? C.accent : C.border}`,
        borderRadius: '4px',
        padding: '5px 12px',
        fontSize: '10px',
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        cursor: 'pointer',
        fontFamily: '"DM Mono", monospace',
      }}
    >{label}</button>
  );
}

function DraftRow({ player, rank, drafted, onToggleDrafted }) {
  return (
    <tr style={{
      borderBottom: `1px solid ${C.border}`,
      opacity: drafted ? 0.4 : 1,
      transition: 'opacity 0.15s',
    }}>
      <td style={{ padding: '10px 0', width: '24px' }}>
        <span style={{ fontSize: '10px', color: C.textDim }}>{rank}</span>
      </td>
      <td style={{ padding: '10px 8px 10px 0' }}>
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <PosTag pos={player.position} />
          <TierTag tier={player.tier} />
          <span style={{
            fontSize: '13px',
            color: C.text,
            textDecoration: drafted ? 'line-through' : 'none',
          }}>{player.name}</span>
          {!player.hasModelData && (
            <span style={{
              marginLeft: '10px', fontSize: '10px', color: C.amber,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title="No historical model data — likely a rookie or unmatched name">
              no model data
            </span>
          )}
          {player.isRookieBaseline && (
            <span style={{
              marginLeft: '10px', fontSize: '10px', color: C.accentDim,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title={`Round ${player.rookieDraftInfo.round} historical baseline (n=${player.rookieDraftInfo.sampleSize}) — not player-specific, no NFL stats exist yet`}>
              rookie est.
            </span>
          )}
          {player.tdRegression?.flag === 'risk' && (
            <span style={{
              marginLeft: '10px', fontSize: '10px', color: C.red,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title={`${player.tdRegression.actualTds} TDs vs ${player.tdRegression.expectedTds} expected from opportunity — touchdown-dependent, regression risk`}>
              TD risk
            </span>
          )}
          {player.tdRegression?.flag === 'value' && (
            <span style={{
              marginLeft: '10px', fontSize: '10px', color: C.green,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title={`${player.tdRegression.actualTds} TDs vs ${player.tdRegression.expectedTds} expected from opportunity — underperformed role, buy-low candidate`}>
              TD value
            </span>
          )}
          {player.valueFlag?.flag === 'value' && (
            <span style={{
              marginLeft: '10px', fontSize: '10px', color: C.green,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title={`ADP rank #${player.valueFlag.adpRank}, model rank #${player.valueFlag.vorpRank} — market may be undervaluing this player`}>
              value
            </span>
          )}
          {player.valueFlag?.flag === 'reach' && (
            <span style={{
              marginLeft: '10px', fontSize: '10px', color: C.amber,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title={`ADP rank #${player.valueFlag.adpRank}, model rank #${player.valueFlag.vorpRank} — market may be overvaluing this player`}>
              reach
            </span>
          )}
          {player.rosterStatus && player.rosterStatus !== 'ACT' && (
            <span style={{
              marginLeft: '10px', fontSize: '10px', color: C.amber,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title={`Roster status: ${player.rosterStatus} — not on active roster`}>
              {player.rosterStatus}
            </span>
          )}
          {player.injuryDetail && player.injuryDetail !== 'Active' && (
              <span style={{
                marginLeft: '8px', fontSize: '8px', color: C.red,
                letterSpacing: '0.08em', textTransform: 'uppercase',
              }} title={`${player.injuryDetail} — ${Math.round((player.playProbability ?? 1) * 100)}% play probability`}>
                🩹 {player.injuryDetail}
              </span>
            )}
          {player.teamChanged && (
            <span style={{
              marginLeft: '10px', fontSize: '10px', color: C.textMid,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title="Team changed since the season this player's stats reflect — model score may not account for new situation">
              ↻ new team
            </span>
          )}
          {player.qbContext?.qb_changed && (
            <span style={{
              marginLeft: '8px', fontSize: '8px', color: C.textMid,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title={`2025 QB: ${player.qbContext.prior_qb_name} → 2026: ${player.qbContext.current_qb_name}`}>
              🔄 new QB
            </span>
          )}
        </div>
        <div style={{ fontSize: '10px', color: C.textDim, marginTop: '2px', paddingLeft: '36px' }}>
          {player.team} · bye {player.bye ?? '—'}
        </div>
      </td>
      <td style={{ padding: '10px 0', width: '64px', textAlign: 'right' }}>
        <span style={{ fontSize: '13px', fontFamily: serif, color: C.text }}>
          {player.adpFormatted ?? player.adp?.toFixed(1) ?? '—'}
        </span>
        <div style={{ fontSize: '10px', color: C.textDim }}>
          {player.adpHigh != null && player.adpLow != null
            ? `${player.adpHigh}–${player.adpLow}`
            : ''}
        </div>
      </td>
      <td style={{ padding: '10px 0', width: '56px', textAlign: 'right' }} title="Blends efficiency, usage, snaps, red zone role">
        <span style={{ fontSize: '13px', color: player.hasModelData ? C.text : C.textDim }}>
          {player.compositeRating != null ? player.compositeRating.toFixed(0) : '—'}
        </span>
      </td>
      <td style={{ padding: '10px 0', width: '56px', textAlign: 'right' }} title="Pure role/volume signal">
        <span style={{ fontSize: '13px', color: player.hasModelData ? C.accent : C.textDim }}>
          {player.opportunityScore != null ? player.opportunityScore : '—'}
        </span>
      </td>
      <td style={{ padding: '10px 0 10px 12px', width: '56px', textAlign: 'center' }} title="Value over replacement player">
        <span style={{
          fontSize: '12px',
          color: player.vorp == null ? C.textDim : player.vorp >= 0 ? C.green : C.red,
        }}>
          {player.vorp != null ? `${player.vorp >= 0 ? '+' : ''}${player.vorp.toFixed(1)}` : '—'}
        </span>
      </td>
      <td style={{ padding: '10px 0 10px 12px', width: '48px', textAlign: 'center' }} title="Your league's real historical bid average (2022-2025, excludes keeper picks)">
        <span style={{ fontSize: '12px', color: player.mktBid != null ? C.text : C.textDim }}>
          {player.mktBid != null ? `$${player.mktBid}` : '—'}
        </span>
      </td>
      <td style={{ padding: '10px 0 10px 12px', width: '64px', textAlign: 'center' }} title="Model-derived fair value from VORP share of total league budget">
        <span style={{ fontSize: '12px', color: player.modelBid != null ? C.accent : C.textDim }}>
          {player.modelBid != null ? `$${player.modelBid}` : '—'}
        </span>
      </td>
      <td style={{ padding: '10px 0 10px 12px', width: '64px', textAlign: 'center' }} title="Averages Model $ with your league's real bid history — accounts for track record beyond just last season">
        <span style={{ fontSize: '12px', fontWeight: '700', color: player.blendedBid != null ? C.text : C.textDim }}>
          {player.blendedBid != null ? `$${player.blendedBid}` : '—'}
        </span>
      </td>
      <td style={{ padding: '14px 0 10px 12px', width: '32px', textAlign: 'center' }}>
        <input
          type="checkbox"
          checked={drafted}
          onChange={onToggleDrafted}
          style={{ cursor: 'pointer' }}
        />
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------
export default function DraftBoard() {
  const pool = useDraftPool();
  const [sortBy, setSortBy] = useState('adp'); // 'adp' | 'vorp' | 'composite' | 'opportunity'
  const [drafted, setDrafted] = useState(() => new Set());
  const [posFilter, setPosFilter] = useState('ALL');

  const toggleDrafted = (key) => {
    setDrafted(prev => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  };

  const sorted = useMemo(() => {
    let list = posFilter === 'ALL' ? pool : pool.filter(p => p.position === posFilter);
    const withData = list.filter(p => p.hasModelData || p.isRookieBaseline || sortBy === 'adp');
    const withoutData = sortBy === 'adp' ? [] : list.filter(p => !p.hasModelData && !p.isRookieBaseline);

    const sortFn = {
      adp: (a, b) => (a.adp ?? 999) - (b.adp ?? 999),
      composite: (a, b) => (b.compositeRating ?? -1) - (a.compositeRating ?? -1),
      opportunity: (a, b) => (b.opportunityScore ?? -1) - (a.opportunityScore ?? -1),
      vorp: (a, b) => (b.vorp ?? -999) - (a.vorp ?? -999),
    }[sortBy];

    return [...withData.sort(sortFn), ...withoutData.sort((a, b) => (a.adp ?? 999) - (b.adp ?? 999))];
  }, [pool, sortBy, posFilter]);

  const positions = ['ALL', 'QB', 'RB', 'WR', 'TE', 'K', 'DST'];

  return (
    <div style={{ minHeight: '100vh', background: C.bg, color: C.text, fontFamily: font }}>
      <header style={{
        borderBottom: `1px solid ${C.border}`,
        padding: '20px 40px',
        display: 'flex',
        alignItems: 'center',
        gap: '20px',
      }}>
        <span style={{ color: C.border }}>|</span>
        <span style={{ fontSize: '11px', letterSpacing: '0.18em', textTransform: 'uppercase', color: C.accent }}>
          Gridiron Oracle
        </span>
        <span style={{ fontSize: '11px', color: C.textDim, letterSpacing: '0.10em' }}>
          Draft Board
        </span>
        <span style={{ marginLeft: 'auto', fontSize: '10px', color: C.textDim }}>
          {drafted.size} drafted · {pool.length} players
        </span>
      </header>

      <div style={{ maxWidth: '1200px', margin: '0 auto', padding: '32px 40px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '16px', flexWrap: 'wrap', gap: '12px' }}>
          <div style={{ display: 'flex', gap: '6px' }}>
            <SortToggle sortKey="adp" current={sortBy} onSelect={setSortBy} label="ADP" description={SORT_DESCRIPTIONS.adp} />
            <SortToggle sortKey="composite" current={sortBy} onSelect={setSortBy} label="Composite" description={SORT_DESCRIPTIONS.composite} />
            <SortToggle sortKey="opportunity" current={sortBy} onSelect={setSortBy} label="Opportunity" description={SORT_DESCRIPTIONS.opportunity} />
            <SortToggle sortKey="vorp" current={sortBy} onSelect={setSortBy} label="VORP" description={SORT_DESCRIPTIONS.vorp} />
          </div>
          <div style={{ display: 'flex', gap: '4px' }}>
            {positions.map(pos => (
              <button
                key={pos}
                onClick={() => setPosFilter(pos)}
                style={{
                  background: posFilter === pos ? C.surface : 'transparent',
                  color: posFilter === pos ? C.text : C.textDim,
                  border: `1px solid ${C.border}`,
                  borderRadius: '4px',
                  padding: '5px 10px',
                  fontSize: '10px',
                  letterSpacing: '0.08em',
                  cursor: 'pointer',
                  fontFamily: '"DM Mono", monospace',
                }}
              >{pos}</button>
            ))}
          </div>
        </div>

        <div style={{ fontSize: '10px', color: C.textDim, marginBottom: '16px', fontStyle: 'italic' }}>
          {SORT_DESCRIPTIONS[sortBy]}
        </div>

        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ borderBottom: `1px solid ${C.borderMid}` }}>
              <th style={thStyle}>#</th>
              <th style={thStyle}>Player</th>
              <th style={{ ...thStyle, textAlign: 'right' }}>ADP</th>
              <th style={{ ...thStyle, textAlign: 'right' }} title="Blends efficiency, usage, snaps, red zone role">Rating</th>
              <th style={{ ...thStyle, textAlign: 'right' }} title="Pure role/volume signal">Opp</th>
              <th style={{ ...thStyle, textAlign: 'right' }} title="Value over replacement player">VORP</th>
              <th style={{ ...thStyle, textAlign: 'right' }} title="Your league's real historical bid average">Mkt $</th>
              <th style={{ ...thStyle, textAlign: 'right' }} title="Model-derived fair value from VORP">Model $</th>
              <th style={{ ...thStyle, textAlign: 'right' }} title="Blends Model $ with real bid history">Blend $</th>
              <th style={{ ...thStyle, textAlign: 'center' }}>Drafted</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((player, i) => (
              <DraftRow
                key={player.key}
                player={player}
                rank={i + 1}
                drafted={drafted.has(player.key)}
                onToggleDrafted={() => toggleDrafted(player.key)}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const thStyle = {
  fontSize: '10px',
  letterSpacing: '0.16em',
  textTransform: 'uppercase',
  color: C.textDim,
  textAlign: 'left',
  padding: '8px 0',
  fontWeight: '400',
  fontFamily: '"DM Mono", monospace',
};