// DraftBoard.jsx — Gridiron Oracle
// Pre-draft rankings: ADP + composite rating + VORP, sortable
// Fully standalone — no roster/matchup dependency, works pre-draft or offseason
import { useState, useMemo } from 'react';
import { ADP_LIST } from '../utils/adp_data.js';
import { PLAYERS_BY_POSITION } from '../utils/nfl_data.js';
import { computeCompositeRating, computeVORP, getReplacementLevel } from '../utils/simulator.js';
import { C, font, serif, POS_COLOR } from '../utils/theme.js';
import { AUCTION_HISTORY_BY_GSIS, AUCTION_HISTORY_META } from '../utils/auction_history.js';

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
// Bid Recommendation — two independent columns, not blended:
//   Mkt $  — your league's own real historical bid average...
//   Model $ — VORP-based fair-value share of the total league budget,
//             using a convex (exponentiated) curve rather than linear
//             proportional allocation. Real auction markets consistently
//             overpay for elite/low-variance production relative to a
//             strictly linear points-above-replacement split — this is
//             the well-documented "stars and scrubs" bidding pattern.
//             CURVE_EXPONENT > 1 stretches the gap between elite and
//             merely-good players; 1.0 would be pure linear (the old
//             behavior, which compressed the top end too flat).
//             Tunable — increase if the top of the board still feels
//             too flat vs. real market prices, decrease if the top
//             players are eating too much of the budget.
// ---------------------------------------------------------------------------
const TOTAL_LEAGUE_BUDGET =
  (AUCTION_HISTORY_META.league_budget?.acquisition_budget ?? 100) *
  (AUCTION_HISTORY_META.league_budget?.team_count ?? 12);

// Rough estimate of how many players will actually be bid on this draft —
// roster spots x teams. Used to cap the "draftable pool" for VORP-based
// dollar allocation, so bench-tier/waiver-tier players (near-zero VORP)
// don't dilute the per-VORP-point dollar rate.
const ESTIMATED_DRAFTABLE_SLOTS = 14 * (AUCTION_HISTORY_META.league_budget?.team_count ?? 12);
const CURVE_EXPONENT = 1.5;

function computeModelBidValue(vorp, allVorps) {
  if (vorp == null || vorp <= 0) return 1;
  const positiveVorps = allVorps
    .filter(v => v != null && v > 0)
    .sort((a, b) => b - a)
    .slice(0, ESTIMATED_DRAFTABLE_SLOTS);

  const curved = (v) => Math.pow(v, CURVE_EXPONENT);
  const totalCurved = positiveVorps.reduce((sum, v) => sum + curved(v), 0);
  if (totalCurved <= 0) return 1;

  const reservedFloor = positiveVorps.length * 1;
  const remainingBudget = Math.max(TOTAL_LEAGUE_BUDGET - reservedFloor, 0);
  const share = curved(vorp) / totalCurved;
  return Math.max(1, Math.round(1 + share * remainingBudget));
}

// ---------------------------------------------------------------------------
// Blended $ — averages Model $ (pure VORP, recent-performance-based) with
// Mkt $ (your league's real multi-year bid history) when both exist. This
// addresses cases like a player having a disrupted/down recent season that
// a stats-only model penalizes, while real bid history still remembers
// their track record across multiple prior seasons — closer to how an
// actual drafter weighs "recent stats" against "known talent/history."
// Falls back to Model $ alone when there's no bid history to blend with
// (rookies, players new to this league) — no synthetic substitute invented.
// ---------------------------------------------------------------------------
function computeBlendedBid(modelBid, mktBid) {
  if (modelBid == null) return null;
  if (mktBid == null) return modelBid;
  return Math.round((modelBid + mktBid) / 2);
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
      let best = null;
      let bestLen = 0;
      for (const p of candidates) {
        const last = lastNameFromInitialFormat(p.name);
        if (last && compact.endsWith(last) && last.length > bestLen) {
          best = p;
          bestLen = last.length;
        }
      }
      return best;
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
        return { ...base, hasModelData: false, compositeRating: null, vorp: null, seasonAvgPts: null, opportunityScore: null, tdRegression: null, mktBid: null, rosterStatus: null, teamChanged: false };
      }

      const { rating: compositeRating } = computeCompositeRating(playerData, allNflPlayers);
      const seasonAvgPts = playerData.season_avg_pts ?? 0;
      const replacement = getReplacement(playerData.position);
      const vorp = seasonAvgPts - replacement;
      const rosterStatus = playerData.roster_status ?? 'ACT';

      const positionPeers = PLAYERS_BY_POSITION[playerData.position] ?? [];
      const opportunityScore = computeOpportunityScore(playerData, positionPeers);
      const tdRegression = computeTDRegression(playerData, positionPeers);

      // Mkt $ — real league bid history, keyed by GSIS ID. No fallback —
      // missing history is shown honestly as "—", not guessed.
      const history = AUCTION_HISTORY_BY_GSIS[playerData.gsis_id];
      const mktBid = history?.avg_bid_non_keeper ?? null;

      const teamChanged = hasTeamChanged(adpEntry, playerData);

      return { ...base, hasModelData: true, compositeRating, vorp, seasonAvgPts, opportunityScore, tdRegression, mktBid, rosterStatus, teamChanged };
    });

    // --- Pass 2: tiers + value-vs-ADP + model/blended bid, needs the full pool first ---
    const tierMap = computeTiers(basePool);
    const valueFlagMap = computeValueFlags(basePool);
    const allVorps = basePool.map(p => p.vorp).filter(v => v != null);

    return basePool.map(p => {
      const modelBid = p.hasModelData ? computeModelBidValue(p.vorp, allVorps) : null;
      const blendedBid = p.hasModelData ? computeBlendedBid(modelBid, p.mktBid) : null;
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
      fontSize: '9px', fontWeight: '700', letterSpacing: '0.10em',
      background: color + '18', color, marginRight: '8px',
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
        fontSize: '9px',
        fontWeight: '700',
        letterSpacing: '0.06em',
        background: color + '18',
        color,
        marginRight: '8px',
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
              marginLeft: '8px', fontSize: '8px', color: C.amber,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title="No historical model data — likely a rookie or unmatched name">
              no model data
            </span>
          )}
          {player.tdRegression?.flag === 'risk' && (
            <span style={{
              marginLeft: '8px', fontSize: '8px', color: C.red,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title={`${player.tdRegression.actualTds} TDs vs ${player.tdRegression.expectedTds} expected from opportunity — touchdown-dependent, regression risk`}>
              TD risk
            </span>
          )}
          {player.tdRegression?.flag === 'value' && (
            <span style={{
              marginLeft: '8px', fontSize: '8px', color: C.green,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title={`${player.tdRegression.actualTds} TDs vs ${player.tdRegression.expectedTds} expected from opportunity — underperformed role, buy-low candidate`}>
              TD value
            </span>
          )}
          {player.valueFlag?.flag === 'value' && (
            <span style={{
              marginLeft: '8px', fontSize: '8px', color: C.green,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title={`ADP rank #${player.valueFlag.adpRank}, model rank #${player.valueFlag.vorpRank} — market may be undervaluing this player`}>
              value
            </span>
          )}
          {player.valueFlag?.flag === 'reach' && (
            <span style={{
              marginLeft: '8px', fontSize: '8px', color: C.amber,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title={`ADP rank #${player.valueFlag.adpRank}, model rank #${player.valueFlag.vorpRank} — market may be overvaluing this player`}>
              reach
            </span>
          )}
          {player.rosterStatus && player.rosterStatus !== 'ACT' && (
            <span style={{
              marginLeft: '8px', fontSize: '8px', color: C.amber,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title={`Roster status: ${player.rosterStatus} — not on active roster`}>
              {player.rosterStatus}
            </span>
          )}
          {player.teamChanged && (
            <span style={{
              marginLeft: '8px', fontSize: '8px', color: C.textMid,
              letterSpacing: '0.08em', textTransform: 'uppercase',
            }} title="Team changed since the season this player's stats reflect — model score may not account for new situation">
              ↻ new team
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
        <div style={{ fontSize: '9px', color: C.textDim }}>
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
      <td style={{ padding: '10px 0 10px 12px', width: '56px', textAlign: 'right' }} title="Value over replacement player">
        <span style={{
          fontSize: '12px',
          color: player.vorp == null ? C.textDim : player.vorp >= 0 ? C.green : C.red,
        }}>
          {player.vorp != null ? `${player.vorp >= 0 ? '+' : ''}${player.vorp.toFixed(1)}` : '—'}
        </span>
      </td>
      <td style={{ padding: '10px 0 10px 12px', width: '48px', textAlign: 'right' }} title="Your league's real historical bid average (2022-2025, excludes keeper picks)">
        <span style={{ fontSize: '12px', color: player.mktBid != null ? C.text : C.textDim }}>
          {player.mktBid != null ? `$${player.mktBid}` : '—'}
        </span>
      </td>
      <td style={{ padding: '10px 0 10px 12px', width: '48px', textAlign: 'right' }} title="Model-derived fair value from VORP share of total league budget">
        <span style={{ fontSize: '12px', color: player.modelBid != null ? C.accent : C.textDim }}>
          {player.modelBid != null ? `$${player.modelBid}` : '—'}
        </span>
      </td>
      <td style={{ padding: '10px 0 10px 12px', width: '48px', textAlign: 'right' }} title="Averages Model $ with your league's real bid history — accounts for track record beyond just last season">
        <span style={{ fontSize: '12px', fontWeight: '700', color: player.blendedBid != null ? C.text : C.textDim }}>
          {player.blendedBid != null ? `$${player.blendedBid}` : '—'}
        </span>
      </td>
      <td style={{ padding: '10px 0 10px 12px', width: '32px', textAlign: 'center' }}>
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
    const withData = list.filter(p => p.hasModelData || sortBy === 'adp');
    const withoutData = sortBy === 'adp' ? [] : list.filter(p => !p.hasModelData);

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

      <div style={{ maxWidth: '900px', margin: '0 auto', padding: '32px 40px' }}>
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
                  fontSize: '9px',
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
  fontSize: '9px',
  letterSpacing: '0.16em',
  textTransform: 'uppercase',
  color: C.textDim,
  textAlign: 'left',
  padding: '8px 0',
  fontWeight: '400',
  fontFamily: '"DM Mono", monospace',
};