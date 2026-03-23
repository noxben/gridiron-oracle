// WaiverWire.jsx — Gridiron Oracle
// VORP-ranked available players + add/drop recommendations + FAAB bid estimator
// v2.0 Step 7 per spec §4.3 / §6

import { useState, useMemo } from 'react';
import { MY_ROSTER, MY_TEAM, LEAGUE } from '../utils/espn_data.js';
import {
  WAIVER_POOL,
  FAAB_BUDGETS,
  LEAGUE_FETCHED_AT,
  ESPN_LEAGUE_DATA,
} from '../utils/espn_league.js';
import { PLAYER_BY_GSIS_ID, PLAYERS_BY_POSITION } from '../utils/nfl_data.js';
import { ESPN_TO_GSIS } from '../utils/id_mapping.js';
import { hasWeatherImpact, getWeatherAdvisory } from '../utils/weather_data.js';

// ---------------------------------------------------------------------------
// Design tokens
// ---------------------------------------------------------------------------

const C = {
  bg:        '#1a1d23',
  surface:   '#22262e',
  border:    '#333a45',
  borderMid: '#3d4652',
  text:      '#f0ede6',
  textMid:   '#a8b0bc',
  textDim:   '#6a7585',
  accent:    '#c8ff00',
  accentDim: '#5a7000',
  red:       '#ff6b6b',
  amber:     '#ffb84d',
  green:     '#5ddd8a',
};

const font  = '"DM Mono", "Fira Mono", "Consolas", monospace';
const serif = '"DM Serif Display", "Georgia", serif';

const POS_COLOR = {
  QB: '#5a9ff0', RB: '#50c878', WR: '#c090f0',
  TE: '#f0b840', K: '#808080', DST: '#e06060',
};

const POSITIONS = ['ALL', 'QB', 'RB', 'WR', 'TE', 'K', 'DST'];

// ---------------------------------------------------------------------------
// VORP calculation — per spec §6.2
// Replacement level = projected pts of best waiver player at that position
// For a 12-team league: QB13, RB37, WR37, TE13 are replacement level
// ---------------------------------------------------------------------------

const STARTERS_BY_POS = { QB: 12, RB: 36, WR: 36, TE: 12, K: 12, DST: 12 };

function getReplacementLevel(position, allPlayers) {
  const posPlayers = allPlayers
    .filter(p => p.position === position && (p.projected_points ?? p.avg_points ?? 0) > 0)
    .sort((a, b) => (b.projected_points ?? b.avg_points ?? 0) - (a.projected_points ?? a.avg_points ?? 0));

  const starterCount = STARTERS_BY_POS[position] ?? 12;
  const replacement  = posPlayers[starterCount] ?? posPlayers[posPlayers.length - 1];
  return replacement ? (replacement.projected_points ?? replacement.avg_points ?? 3.0) : 3.0;
}

function computeVORP(player, replacementLevel) {
  const pts = player.projected_points ?? player.avg_points ?? 0;
  return pts - replacementLevel;
}

// ---------------------------------------------------------------------------
// FAAB bid estimator — per spec §6.3
// bid = (player_vorp / max_vorp_at_position) × remaining_budget × scarcity_factor
// ---------------------------------------------------------------------------

function estimateFAAB(vorp, maxVorpAtPos, remainingBudget, scarcityFactor) {
  if (maxVorpAtPos <= 0 || remainingBudget <= 0) return { low: 0, mid: 0, high: 0 };
  const base = (Math.max(0, vorp) / maxVorpAtPos) * remainingBudget * scarcityFactor;
  return {
    low:  Math.round(base * 0.7),
    mid:  Math.round(base),
    high: Math.round(base * 1.35),
  };
}

function getScarcityFactor(position, waiverPool) {
  const available = waiverPool.filter(p => p.position === position).length;
  if (available < 3) return 1.4;
  if (available < 8) return 1.15;
  return 1.0;
}

// ---------------------------------------------------------------------------
// Find best drop candidate — per spec §6.4
// Lowest injury-adjusted VORP bench player at same position
// ---------------------------------------------------------------------------

function findDropCandidate(position, myRoster, replacementMap) {
  const FLEX_POSITIONS = ['RB', 'WR', 'TE'];
  const eligible = myRoster.filter(p => {
    if (!p.on_bench && !p.on_ir) return false; // only bench/IR
    const posMatch = p.position === position || (FLEX_POSITIONS.includes(position) && FLEX_POSITIONS.includes(p.position));
    return posMatch;
  });

  if (eligible.length === 0) return null;

  return eligible
    .map(p => {
      const pts         = p.projected_points ?? p.avg_points ?? 0;
      const adjPts      = pts * (p.play_probability ?? 1.0);
      const replLevel   = replacementMap[p.position] ?? 3.0;
      const vorp        = adjPts - replLevel;
      return { ...p, vorp };
    })
    .sort((a, b) => a.vorp - b.vorp)[0]; // lowest VORP = best drop
}

// ---------------------------------------------------------------------------
// Enrich waiver player with nfl_data.js stats via ESPN→GSIS mapping
// ---------------------------------------------------------------------------

function enrichWithNflData(waiverPlayer) {
  const gsisId    = ESPN_TO_GSIS[String(waiverPlayer.espn_id)];
  const nflRecord = gsisId ? PLAYER_BY_GSIS_ID?.[gsisId] : null;
  if (!nflRecord) return waiverPlayer;
  return {
    ...waiverPlayer,
    gsisId,
    epa_per_play:   nflRecord.epa_per_play,
    target_share:   nflRecord.target_share,
    carry_share:    nflRecord.carry_share,
    snap_pct:       nflRecord.snap_pct,
    opp_def_rank:   nflRecord.opp_def_rank,
    // Use nflfastR averages if ESPN projected is missing
    projected_points: waiverPlayer.projected_points || nflRecord.season_avg_pts || waiverPlayer.avg_points,
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fetchAge(fetchedAt) {
  if (!fetchedAt) return null;
  const mins = Math.round((Date.now() - new Date(fetchedAt).getTime()) / 60000);
  if (mins < 60)   return `${mins}m ago`;
  if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
  return `${Math.round(mins / 1440)}d ago`;
}

function defRankColor(rank) {
  if (!rank) return C.textDim;
  if (rank <= 5)  return C.red;
  if (rank <= 12) return C.amber;
  if (rank >= 28) return C.green;
  if (rank >= 22) return '#90d060';
  return C.textMid;
}

function defRankLabel(rank) {
  if (!rank) return '—';
  if (rank <= 5)  return `#${rank} tough`;
  if (rank >= 28) return `#${rank} easy`;
  return `#${rank}`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function PosTag({ pos }) {
  const color = POS_COLOR[pos] ?? C.textMid;
  return (
    <span style={{
      display:       'inline-block',
      padding:       '1px 5px',
      borderRadius:  '3px',
      fontSize:      '9px',
      fontWeight:    '700',
      letterSpacing: '0.10em',
      background:    color + '18',
      color,
      marginRight:   '8px',
      minWidth:      '28px',
      textAlign:     'center',
    }}>{pos}</span>
  );
}

function InjuryDot({ prob }) {
  if (prob >= 1.0) return null;
  const color = prob === 0 ? C.red : prob <= 0.55 ? C.amber : '#d0c030';
  return (
    <span style={{
      display:       'inline-block',
      width:         '6px', height: '6px',
      borderRadius:  '50%',
      background:    color,
      marginLeft:    '6px',
      verticalAlign: 'middle',
      flexShrink:    0,
    }} title={`Play probability: ${Math.round(prob * 100)}%`} />
  );
}

function StatPill({ label, value, color }) {
  if (value == null || isNaN(value)) return null;
  return (
    <span style={{
      fontSize:      '9px',
      color:         color ?? C.textDim,
      background:    (color ?? C.textDim) + '15',
      padding:       '1px 5px',
      borderRadius:  '3px',
      marginRight:   '5px',
    }}>
      {label} {typeof value === 'number' ? value.toFixed(2) : value}
    </span>
  );
}

function FAABRange({ bid }) {
  if (!bid || bid.high === 0) return <span style={{ fontSize: '10px', color: C.textDim }}>$0</span>;
  return (
    <span style={{ fontSize: '10px', color: C.amber }}>
      ${bid.low}–${bid.mid}–${bid.high}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Waiver player row
// ---------------------------------------------------------------------------

function WaiverRow({ player, rank, dropCandidate, faabBid, myRoster }) {
  const [expanded, setExpanded] = useState(false);

  const pts          = player.projected_points ?? player.avg_points ?? 0;
  const vorp         = player.vorp ?? 0;
  const vorpColor    = vorp >= 3 ? C.green : vorp >= 0 ? C.textMid : C.red;
  const ptsColor     = pts > 15 ? C.accent : pts > 8 ? C.text : C.textMid;
  const weatherFlag  = hasWeatherImpact(player.team ?? '');
  const weatherText  = weatherFlag ? getWeatherAdvisory(player.team ?? '').join(' · ') : null;

  return (
    <>
      <tr
        onClick={() => setExpanded(e => !e)}
        style={{
          borderBottom: `1px solid ${C.border}`,
          cursor:       'pointer',
          background:   expanded ? C.surface : 'transparent',
          transition:   'background 0.15s',
        }}
      >
        {/* Rank */}
        <td style={{ padding: '11px 0', width: '24px' }}>
          <span style={{ fontSize: '10px', color: C.textDim }}>{rank}</span>
        </td>

        {/* Player */}
        <td style={{ padding: '11px 8px 11px 0' }}>
          <div style={{ display: 'flex', alignItems: 'center' }}>
            <PosTag pos={player.position} />
            <span style={{ fontSize: '13px', color: C.text }}>{player.name}</span>
            <InjuryDot prob={player.play_probability ?? 1} />
            {weatherFlag && (
              <span title={weatherText} style={{ marginLeft: '6px', fontSize: '10px', cursor: 'help' }}>🌬</span>
            )}
          </div>
          <div style={{ fontSize: '10px', color: C.textDim, marginTop: '2px', paddingLeft: '36px' }}>
            {player.team ?? ''}
            {player.opp_def_rank && (
              <span style={{ marginLeft: '8px', color: defRankColor(player.opp_def_rank) }}>
                vs DEF {defRankLabel(player.opp_def_rank)}
              </span>
            )}
            <span style={{ marginLeft: '8px' }}>{player.percent_owned?.toFixed(0) ?? '?'}% owned</span>
          </div>
        </td>

        {/* Projected pts */}
        <td style={{ padding: '11px 0', width: '52px', textAlign: 'right' }}>
          <span style={{ fontSize: '14px', fontFamily: serif, color: ptsColor }}>
            {pts.toFixed(1)}
          </span>
        </td>

        {/* VORP */}
        <td style={{ padding: '11px 0 11px 12px', width: '52px', textAlign: 'right' }}>
          <span style={{ fontSize: '11px', color: vorpColor }}>
            {vorp >= 0 ? '+' : ''}{vorp.toFixed(1)}
          </span>
        </td>

        {/* FAAB */}
        <td style={{ padding: '11px 0 11px 12px', width: '72px', textAlign: 'right' }}>
          <FAABRange bid={faabBid} />
        </td>
      </tr>

      {/* Expanded detail */}
      {expanded && (
        <tr style={{ background: C.surface }}>
          <td colSpan={5} style={{ padding: '0 0 16px 32px' }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '20px', paddingTop: '12px' }}>

              {/* Stats */}
              <div>
                <div style={{ fontSize: '9px', letterSpacing: '0.14em', color: C.textDim, textTransform: 'uppercase', marginBottom: '10px' }}>
                  Player stats
                </div>
                <div style={{ marginBottom: '8px', display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                  <StatPill label="EPA/play" value={player.epa_per_play} color={C.accent} />
                  {player.position === 'RB' && <StatPill label="carry%" value={player.carry_share ? player.carry_share * 100 : null} color={POS_COLOR.RB} />}
                  {['WR', 'TE'].includes(player.position) && <StatPill label="tgt%" value={player.target_share ? player.target_share * 100 : null} color={POS_COLOR.WR} />}
                  <StatPill label="snap%" value={player.snap_pct ? player.snap_pct * 100 : null} color={C.textMid} />
                </div>
                <div style={{ fontSize: '10px', color: C.textDim, lineHeight: 1.7 }}>
                  <div>Avg pts (season): <span style={{ color: C.text }}>{player.avg_points?.toFixed(1) ?? '—'}</span></div>
                  <div>Proj pts: <span style={{ color: C.text }}>{pts.toFixed(1)}</span></div>
                  <div>% started: <span style={{ color: C.text }}>{player.percent_started?.toFixed(0) ?? '?'}%</span></div>
                </div>
                {weatherFlag && (
                  <div style={{ marginTop: '8px', fontSize: '10px', color: C.amber }}>
                    🌬 {weatherText}
                  </div>
                )}
              </div>

              {/* Add/Drop recommendation */}
              <div>
                <div style={{ fontSize: '9px', letterSpacing: '0.14em', color: C.textDim, textTransform: 'uppercase', marginBottom: '10px' }}>
                  Add / drop recommendation
                </div>

                {/* ADD */}
                <div style={{
                  padding:      '8px 12px',
                  background:   C.green + '12',
                  border:       `1px solid ${C.green}30`,
                  borderRadius: '4px',
                  marginBottom: '8px',
                }}>
                  <div style={{ fontSize: '9px', color: C.green, letterSpacing: '0.10em', textTransform: 'uppercase', marginBottom: '4px' }}>Add</div>
                  <div style={{ fontSize: '12px', color: C.text }}>{player.name}</div>
                  <div style={{ fontSize: '10px', color: C.textDim, marginTop: '3px' }}>
                    {pts.toFixed(1)} proj · VORP {vorp >= 0 ? '+' : ''}{vorp.toFixed(1)}
                  </div>
                  <div style={{ fontSize: '10px', color: C.amber, marginTop: '5px' }}>
                    FAAB: <FAABRange bid={faabBid} />
                    <span style={{ color: C.textDim, marginLeft: '8px' }}>(low / mid / aggressive)</span>
                  </div>
                </div>

                {/* DROP */}
                {dropCandidate ? (
                  <div style={{
                    padding:      '8px 12px',
                    background:   C.red + '10',
                    border:       `1px solid ${C.red}25`,
                    borderRadius: '4px',
                  }}>
                    <div style={{ fontSize: '9px', color: C.red, letterSpacing: '0.10em', textTransform: 'uppercase', marginBottom: '4px' }}>Drop</div>
                    <div style={{ fontSize: '12px', color: C.textMid }}>{dropCandidate.name}</div>
                    <div style={{ fontSize: '10px', color: C.textDim, marginTop: '3px' }}>
                      {(dropCandidate.projected_points ?? dropCandidate.avg_points ?? 0).toFixed(1)} proj
                      {dropCandidate.injury_status && dropCandidate.injury_status !== 'ACTIVE' && (
                        <span style={{ color: C.amber, marginLeft: '6px' }}>⚠ {dropCandidate.injury_status}</span>
                      )}
                    </div>
                    <div style={{ fontSize: '10px', color: C.textDim, marginTop: '3px' }}>
                      Pts gain: <span style={{ color: C.green }}>
                        +{Math.max(0, pts - (dropCandidate.projected_points ?? dropCandidate.avg_points ?? 0)).toFixed(1)}
                      </span>
                    </div>
                  </div>
                ) : (
                  <div style={{ fontSize: '10px', color: C.textDim, fontStyle: 'italic' }}>
                    No drop candidate at this position on your bench.
                  </div>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function WaiverWire() {
  const [posFilter,  setPosFilter]  = useState('ALL');
  const [sortBy,     setSortBy]     = useState('vorp'); // 'vorp' | 'pts' | 'owned'

  const myRoster   = MY_ROSTER   ?? [];
  const waiverPool = WAIVER_POOL ?? [];
  const fetchedAt  = LEAGUE_FETCHED_AT;
  const age        = fetchAge(fetchedAt);

  // Get my team's remaining FAAB
  const myTeamId      = MY_TEAM?.team_id;
  const remainingFAAB = myTeamId
    ? (FAAB_BUDGETS?.[String(myTeamId)] ?? 100)
    : 100;

  // Enrich waiver pool with nfl_data.js stats
  const enrichedPool = useMemo(() =>
    waiverPool.map(enrichWithNflData),
    [waiverPool]
  );

  // Compute replacement levels per position from the enriched pool
  const replacementMap = useMemo(() => {
    const map = {};
    for (const pos of ['QB', 'RB', 'WR', 'TE', 'K', 'DST']) {
      map[pos] = getReplacementLevel(pos, enrichedPool);
    }
    return map;
  }, [enrichedPool]);

  // Compute VORP for every waiver player
  const withVORP = useMemo(() =>
    enrichedPool.map(p => ({
      ...p,
      vorp: computeVORP(p, replacementMap[p.position] ?? 3.0),
    })),
    [enrichedPool, replacementMap]
  );

  // Max VORP per position for FAAB scaling
  const maxVORPByPos = useMemo(() => {
    const map = {};
    for (const pos of ['QB', 'RB', 'WR', 'TE', 'K', 'DST']) {
      const posPlayers = withVORP.filter(p => p.position === pos);
      map[pos] = Math.max(0, ...posPlayers.map(p => p.vorp ?? 0));
    }
    return map;
  }, [withVORP]);

  // Scarcity factors
  const scarcityMap = useMemo(() => {
    const map = {};
    for (const pos of ['QB', 'RB', 'WR', 'TE', 'K', 'DST']) {
      map[pos] = getScarcityFactor(pos, enrichedPool);
    }
    return map;
  }, [enrichedPool]);

  // Filter + sort
  const filtered = useMemo(() => {
    let players = posFilter === 'ALL'
      ? withVORP
      : withVORP.filter(p => p.position === posFilter);

    players = players.filter(p => (p.play_probability ?? 1.0) > 0.0); // skip definite outs

    if (sortBy === 'vorp')  players = [...players].sort((a, b) => (b.vorp ?? 0) - (a.vorp ?? 0));
    if (sortBy === 'pts')   players = [...players].sort((a, b) => (b.projected_points ?? b.avg_points ?? 0) - (a.projected_points ?? a.avg_points ?? 0));
    if (sortBy === 'owned') players = [...players].sort((a, b) => (b.percent_owned ?? 0) - (a.percent_owned ?? 0));

    return players.slice(0, 40); // top 40
  }, [withVORP, posFilter, sortBy]);

  // No data guard
  if (waiverPool.length === 0) {
    return (
      <div style={{ minHeight: '100vh', background: C.bg, color: C.text, fontFamily: font,
                    display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: '13px', color: C.textDim, marginBottom: '8px' }}>No waiver data</div>
          <div style={{ fontSize: '11px', color: C.textDim }}>
            Run <code style={{ color: C.accent }}>python3 scripts/fetch_espn_league.py</code>
          </div>
        </div>
      </div>
    );
  }

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Serif+Display&display=swap');
        @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: ${C.bg}; }
        tr { animation: fadeIn 0.15s ease both; }
      `}</style>

      <div style={{ minHeight: '100vh', background: C.bg, color: C.text, fontFamily: font }}>

        {/* Header */}
        <header style={{
          borderBottom: `1px solid ${C.border}`,
          padding:      '20px 40px',
          display:      'flex',
          alignItems:   'center',
          gap:          '20px',
        }}>
          <span style={{ color: C.border }}>|</span>
          <span style={{ fontSize: '11px', letterSpacing: '0.18em', textTransform: 'uppercase', color: C.accent }}>
            Gridiron Oracle
          </span>
          <span style={{ fontSize: '11px', color: C.textDim, letterSpacing: '0.10em' }}>
            Waiver Wire
          </span>
          <span style={{ marginLeft: 'auto', fontSize: '10px', color: C.textDim, display: 'flex', alignItems: 'center', gap: '16px' }}>
            <span>
              FAAB remaining:{' '}
              <span style={{ color: remainingFAAB > 50 ? C.green : remainingFAAB > 20 ? C.amber : C.red, fontWeight: '600' }}>
                ${remainingFAAB}
              </span>
            </span>
            <span>{waiverPool.length} available</span>
            {age && <span>updated {age}</span>}
          </span>
        </header>

        <div style={{ maxWidth: '860px', margin: '0 auto', padding: '28px 40px 100px' }}>

          {/* Controls */}
          <div style={{ display: 'flex', gap: '12px', marginBottom: '20px', alignItems: 'center', flexWrap: 'wrap' }}>

            {/* Position filter */}
            <div style={{ display: 'flex', gap: '2px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '5px', padding: '2px' }}>
              {POSITIONS.map(pos => (
                <button
                  key={pos}
                  onClick={() => setPosFilter(pos)}
                  style={{
                    background:    posFilter === pos ? C.accent : 'transparent',
                    color:         posFilter === pos ? '#0a0c0f' : C.textDim,
                    border:        'none',
                    borderRadius:  '3px',
                    padding:       '4px 10px',
                    fontSize:      '9px',
                    fontWeight:    posFilter === pos ? '700' : '400',
                    letterSpacing: '0.10em',
                    textTransform: 'uppercase',
                    cursor:        'pointer',
                    fontFamily:    font,
                    transition:    'all 0.15s',
                  }}
                >
                  {pos}
                </button>
              ))}
            </div>

            {/* Sort */}
            <div style={{ display: 'flex', gap: '2px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '5px', padding: '2px' }}>
              {[
                { key: 'vorp',  label: 'VORP'   },
                { key: 'pts',   label: 'Pts'    },
                { key: 'owned', label: '% owned' },
              ].map(({ key, label }) => (
                <button
                  key={key}
                  onClick={() => setSortBy(key)}
                  style={{
                    background:    sortBy === key ? C.surface : 'transparent',
                    color:         sortBy === key ? C.text : C.textDim,
                    border:        `1px solid ${sortBy === key ? C.borderMid : 'transparent'}`,
                    borderRadius:  '3px',
                    padding:       '4px 10px',
                    fontSize:      '9px',
                    letterSpacing: '0.10em',
                    textTransform: 'uppercase',
                    cursor:        'pointer',
                    fontFamily:    font,
                    transition:    'all 0.15s',
                  }}
                >
                  {label}
                </button>
              ))}
            </div>

            <span style={{ fontSize: '10px', color: C.textDim, marginLeft: 'auto' }}>
              Click a player to see add/drop recommendation
            </span>
          </div>

          {/* Replacement level context */}
          {posFilter !== 'ALL' && (
            <div style={{ marginBottom: '14px', fontSize: '10px', color: C.textDim }}>
              Replacement level ({posFilter}): <span style={{ color: C.textMid }}>
                {replacementMap[posFilter]?.toFixed(1)} pts
              </span>
              <span style={{ marginLeft: '12px' }}>
                Scarcity factor: <span style={{ color: scarcityMap[posFilter] > 1.1 ? C.amber : C.textMid }}>
                  {scarcityMap[posFilter]?.toFixed(2)}×
                </span>
              </span>
            </div>
          )}

          {/* Table */}
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ borderBottom: `1px solid ${C.borderMid}` }}>
                {[
                  { label: '#',     width: '24px',  align: 'left'  },
                  { label: 'Player',               align: 'left'  },
                  { label: 'Proj',  width: '52px',  align: 'right' },
                  { label: 'VORP',  width: '52px',  align: 'right' },
                  { label: 'FAAB',  width: '72px',  align: 'right' },
                ].map(({ label, width, align }) => (
                  <th key={label} style={{
                    fontSize:      '9px',
                    letterSpacing: '0.14em',
                    textTransform: 'uppercase',
                    color:         C.textDim,
                    fontWeight:    '400',
                    fontFamily:    font,
                    textAlign:     align,
                    padding:       '6px 0',
                    paddingRight:  align === 'right' ? '0' : '16px',
                    width,
                  }}>
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.map((player, i) => {
                const dropCandidate = findDropCandidate(player.position, myRoster, replacementMap);
                const faabBid       = estimateFAAB(
                  player.vorp ?? 0,
                  maxVORPByPos[player.position] ?? 1,
                  remainingFAAB,
                  scarcityMap[player.position] ?? 1.0,
                );
                return (
                  <WaiverRow
                    key={player.espn_id ?? i}
                    player={player}
                    rank={i + 1}
                    dropCandidate={dropCandidate}
                    faabBid={faabBid}
                    myRoster={myRoster}
                  />
                );
              })}
            </tbody>
          </table>

          {filtered.length === 0 && (
            <div style={{ padding: '40px', textAlign: 'center', color: C.textDim, fontSize: '12px' }}>
              No players found for this filter.
            </div>
          )}

        </div>
      </div>
    </>
  );
}
