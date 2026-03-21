// MatchupExplorer.jsx — Gridiron Oracle
// Head-to-head player comparison + opponent DEF analysis
// Step 7 per spec §3.3

import { useState, useMemo } from 'react';
import { MY_ROSTER, MATCHUP, LEAGUE } from '../utils/espn_data.js';
import { PLAYER_BY_GSIS_ID, PLAYERS_BY_POSITION } from '../utils/nfl_data.js';
import { computeCompositeRating, projectPoints, getReplacementLevel } from '../utils/simulator.js';

// ---------------------------------------------------------------------------
// Styles — same design system as LineupOptimizer
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

const POS_COLOR = {
  QB: '#5a9ff0', RB: '#50c878', WR: '#c090f0',
  TE: '#f0b840', K: '#808080', DST: '#e06060',
};

const font  = '"DM Mono", "Fira Mono", "Consolas", monospace';
const serif = '"DM Serif Display", "Georgia", serif';

const POSITIONS = ['QB', 'RB', 'WR', 'TE', 'K', 'DST'];

// DEF rank → label + color
function defRankColor(rank) {
  if (!rank) return C.textDim;
  if (rank <= 5)  return C.red;
  if (rank <= 12) return C.amber;
  if (rank >= 28) return C.green;
  if (rank >= 22) return '#90d060';
  return C.textMid;
}

function defRankLabel(rank) {
  if (!rank || rank === 16) return 'Neutral';
  if (rank <= 5)  return `#${rank} — tough`;
  if (rank <= 12) return `#${rank} — above avg`;
  if (rank >= 28) return `#${rank} — exploitable`;
  if (rank >= 22) return `#${rank} — below avg`;
  return `#${rank}`;
}

// ---------------------------------------------------------------------------
// Build enriched player list from ESPN roster + nfl_data
// ---------------------------------------------------------------------------

function enrichRoster(roster) {
  if (!roster || roster.length === 0) return [];
  const allPlayers = roster
    .map(p => PLAYER_BY_GSIS_ID[p.espn_id])
    .filter(Boolean);

  return roster.map(p => {
    const nflData = PLAYER_BY_GSIS_ID[p.espn_id];
    if (!nflData) {
      return {
        ...p,
        gsisId:          p.espn_id,
        projectedPts:    p.projected_points ?? p.avg_points ?? 0,
        compositeRating: 50,
        vorp:            0,
        hasNflData:      false,
      };
    }
    const compositeRating = computeCompositeRating(nflData, allPlayers);
    const projectedPts    = projectPoints(nflData, compositeRating, 0);
    const replacement     = getReplacementLevel(nflData.position, LEAGUE?.team_count ?? 12);
    const vorp            = projectedPts - replacement;
    return {
      ...p,
      ...nflData,
      gsisId:          p.espn_id,
      projectedPts,
      compositeRating,
      vorp,
      hasNflData:      true,
    };
  });
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function PosTab({ pos, active, onClick }) {
  const color = POS_COLOR[pos] ?? C.textMid;
  return (
    <button
      onClick={() => onClick(pos)}
      style={{
        background:   active ? color + '22' : 'transparent',
        border:       `1px solid ${active ? color : C.border}`,
        borderRadius: '4px',
        padding:      '6px 14px',
        color:        active ? color : C.textDim,
        fontSize:     '10px',
        fontWeight:   active ? '700' : '400',
        letterSpacing:'0.12em',
        textTransform:'uppercase',
        cursor:       'pointer',
        fontFamily:   font,
        transition:   'all 0.15s',
      }}
    >
      {pos}
    </button>
  );
}

function StatBar({ label, value, max, color, fmt }) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  const display = fmt ? fmt(value) : value?.toFixed(1) ?? '—';
  return (
    <div style={{ marginBottom: '10px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
        <span style={{ fontSize: '10px', color: C.textMid }}>{label}</span>
        <span style={{ fontSize: '11px', color: C.text }}>{display}</span>
      </div>
      <div style={{ height: '2px', background: C.border, borderRadius: '1px' }}>
        <div style={{
          height: '100%',
          width: `${pct}%`,
          background: color ?? C.accent,
          borderRadius: '1px',
          transition: 'width 0.4s ease',
        }} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Player card (detailed)
// ---------------------------------------------------------------------------

function PlayerCard({ player, isSelected, onClick }) {
  const color = POS_COLOR[player.position] ?? C.textMid;
  const injColor = (player.play_probability ?? 1) < 1
    ? (player.play_probability === 0 ? C.red : C.amber)
    : null;

  return (
    <div
      onClick={() => onClick(player)}
      style={{
        padding:      '12px 16px',
        background:   isSelected ? C.surface : 'transparent',
        border:       `1px solid ${isSelected ? color + '60' : C.border}`,
        borderRadius: '6px',
        cursor:       'pointer',
        transition:   'all 0.15s',
        marginBottom: '6px',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span style={{
            fontSize: '9px', fontWeight: '700', letterSpacing: '0.10em',
            background: color + '20', color, padding: '2px 6px', borderRadius: '3px',
          }}>
            {player.position}
          </span>
          <span style={{ fontSize: '13px', color: C.text }}>{player.name}</span>
          {injColor && (
            <span style={{
              width: '6px', height: '6px', borderRadius: '50%',
              background: injColor, display: 'inline-block', flexShrink: 0,
            }} />
          )}
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontSize: '16px', fontFamily: serif, color: C.accent }}>
            {player.projectedPts?.toFixed(1)}
          </div>
          <div style={{ fontSize: '9px', color: C.textDim }}>proj pts</div>
        </div>
      </div>

      <div style={{ display: 'flex', gap: '16px', marginTop: '8px', fontSize: '10px', color: C.textDim }}>
        <span>{player.team}</span>
        {player.opp_def_rank && (
          <span style={{ color: defRankColor(player.opp_def_rank) }}>
            vs DEF {defRankLabel(player.opp_def_rank)}
          </span>
        )}
        <span style={{ marginLeft: 'auto', color: (player.vorp ?? 0) >= 0 ? C.green : C.red }}>
          VORP {(player.vorp ?? 0) >= 0 ? '+' : ''}{player.vorp?.toFixed(1)}
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Comparison panel — two players side by side
// ---------------------------------------------------------------------------

function ComparisonPanel({ playerA, playerB, onClear }) {
  if (!playerA && !playerB) return null;

  const stats = [
    { label: 'Projected pts',    keyA: 'projectedPts',    keyB: 'projectedPts',    max: 35,  fmt: v => v?.toFixed(1) ?? '—' },
    { label: 'Composite rating', keyA: 'compositeRating', keyB: 'compositeRating', max: 100, fmt: v => v?.toFixed(0) ?? '—' },
    { label: 'VORP',             keyA: 'vorp',            keyB: 'vorp',            max: 20,  fmt: v => v != null ? (v >= 0 ? '+' : '') + v.toFixed(1) : '—' },
    { label: 'Season avg pts',   keyA: 'avg_points',      keyB: 'avg_points',      max: 30,  fmt: v => v?.toFixed(1) ?? '—' },
    { label: 'EPA / play',       keyA: 'epa_per_play',    keyB: 'epa_per_play',    max: 0.5, fmt: v => v?.toFixed(3) ?? '—' },
    { label: 'Snap %',           keyA: 'snap_pct',        keyB: 'snap_pct',        max: 1,   fmt: v => v != null ? (v * 100).toFixed(0) + '%' : '—' },
    { label: 'Target share',     keyA: 'target_share',    keyB: 'target_share',    max: 0.4, fmt: v => v != null ? (v * 100).toFixed(0) + '%' : '—' },
    { label: 'Carry share',      keyA: 'carry_share',     keyB: 'carry_share',     max: 0.7, fmt: v => v != null ? (v * 100).toFixed(0) + '%' : '—' },
    { label: 'Red zone share',   keyA: 'red_zone_share',  keyB: 'red_zone_share',  max: 0.4, fmt: v => v != null ? (v * 100).toFixed(0) + '%' : '—' },
    { label: 'Play probability', keyA: 'play_probability',keyB: 'play_probability',max: 1,   fmt: v => v != null ? (v * 100).toFixed(0) + '%' : '—' },
  ];

  const winner = (keyA, keyB) => {
    const a = playerA?.[keyA];
    const b = playerB?.[keyB];
    if (a == null || b == null) return null;
    if (a > b) return 'A';
    if (b > a) return 'B';
    return 'tie';
  };

  return (
    <div style={{
      background: C.surface,
      border:     `1px solid ${C.border}`,
      borderRadius: '8px',
      padding:    '24px',
      marginTop:  '24px',
    }}>
      {/* Header */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 48px 1fr', gap: '12px', marginBottom: '20px', alignItems: 'center' }}>
        <div>
          <div style={{ fontSize: '14px', color: C.text, marginBottom: '2px' }}>
            {playerA?.name ?? <span style={{ color: C.textDim }}>Select player A</span>}
          </div>
          <div style={{ fontSize: '10px', color: C.textDim }}>{playerA?.team ?? '—'}</div>
        </div>
        <div style={{ textAlign: 'center', fontSize: '10px', color: C.textDim, letterSpacing: '0.12em' }}>
          VS
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontSize: '14px', color: C.text, marginBottom: '2px' }}>
            {playerB?.name ?? <span style={{ color: C.textDim }}>Select player B</span>}
          </div>
          <div style={{ fontSize: '10px', color: C.textDim }}>{playerB?.team ?? '—'}</div>
        </div>
      </div>

      {/* Stat rows */}
      {stats.map(({ label, keyA, keyB, max, fmt }) => {
        const valA = playerA?.[keyA];
        const valB = playerB?.[keyB];
        const w    = winner(keyA, keyB);
        const pctA = max > 0 && valA != null ? Math.min(Math.max(valA / max, 0), 1) * 100 : 0;
        const pctB = max > 0 && valB != null ? Math.min(Math.max(valB / max, 0), 1) * 100 : 0;

        return (
          <div key={label} style={{ marginBottom: '12px' }}>
            <div style={{ fontSize: '9px', color: C.textDim, letterSpacing: '0.12em', textTransform: 'uppercase', textAlign: 'center', marginBottom: '4px' }}>
              {label}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 80px 1fr', gap: '8px', alignItems: 'center' }}>
              {/* Bar A (fills right to left) */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', justifyContent: 'flex-end' }}>
                <span style={{ fontSize: '11px', color: w === 'A' ? C.accent : C.textMid, fontWeight: w === 'A' ? '700' : '400' }}>
                  {fmt(valA)}
                </span>
                <div style={{ width: '80px', height: '3px', background: C.border, borderRadius: '2px', overflow: 'hidden', direction: 'rtl' }}>
                  <div style={{ height: '100%', width: `${pctA}%`, background: w === 'A' ? C.accent : C.borderMid, borderRadius: '2px', transition: 'width 0.4s' }} />
                </div>
              </div>
              {/* Center label */}
              <div style={{ textAlign: 'center' }}>
                {w === 'tie' && <span style={{ fontSize: '9px', color: C.textDim }}>tie</span>}
              </div>
              {/* Bar B */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <div style={{ width: '80px', height: '3px', background: C.border, borderRadius: '2px', overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: `${pctB}%`, background: w === 'B' ? C.accent : C.borderMid, borderRadius: '2px', transition: 'width 0.4s' }} />
                </div>
                <span style={{ fontSize: '11px', color: w === 'B' ? C.accent : C.textMid, fontWeight: w === 'B' ? '700' : '400' }}>
                  {fmt(valB)}
                </span>
              </div>
            </div>
          </div>
        );
      })}

      <button
        onClick={onClear}
        style={{
          marginTop: '16px', background: 'transparent', border: `1px solid ${C.border}`,
          borderRadius: '4px', padding: '8px 16px', color: C.textDim,
          fontSize: '10px', letterSpacing: '0.12em', textTransform: 'uppercase',
          cursor: 'pointer', fontFamily: font,
        }}
      >
        Clear comparison
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// DEF rank table for this week's opponent
// ---------------------------------------------------------------------------

function DefRankTable({ players }) {
  const byPosition = useMemo(() => {
    const map = {};
    for (const p of players) {
      if (!p.opp_def_rank || !p.position) continue;
      if (!map[p.position]) map[p.position] = [];
      map[p.position].push(p);
    }
    return map;
  }, [players]);

  const positions = Object.keys(byPosition).filter(pos => POSITIONS.includes(pos));
  if (positions.length === 0) return null;

  return (
    <div style={{
      background: C.surface,
      border:     `1px solid ${C.border}`,
      borderRadius: '8px',
      padding:    '20px 24px',
      marginTop:  '24px',
    }}>
      <div style={{ fontSize: '9px', letterSpacing: '0.18em', textTransform: 'uppercase', color: C.textDim, marginBottom: '16px' }}>
        Opponent DEF rank this week
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '16px' }}>
        {positions.map(pos => {
          const posPlayers = byPosition[pos];
          const rank = posPlayers[0]?.opp_def_rank;
          const color = defRankColor(rank);
          return (
            <div key={pos} style={{ padding: '12px', background: C.bg, borderRadius: '6px', border: `1px solid ${C.border}` }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: '6px' }}>
                <span style={{ fontSize: '10px', color: POS_COLOR[pos] ?? C.textMid, fontWeight: '700', letterSpacing: '0.10em' }}>
                  {pos}
                </span>
                <span style={{ fontSize: '18px', fontFamily: serif, color }}>
                  #{rank ?? '—'}
                </span>
              </div>
              <div style={{ fontSize: '10px', color, marginBottom: '8px' }}>
                {defRankLabel(rank)}
              </div>
              <div style={{ fontSize: '10px', color: C.textDim }}>
                {posPlayers.map(p => p.name).join(', ')}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function MatchupExplorer({ onBack }) {
  const [activePos,  setActivePos]  = useState('WR');
  const [selectedA,  setSelectedA]  = useState(null);
  const [selectedB,  setSelectedB]  = useState(null);

  const enriched = useMemo(() => enrichRoster(MY_ROSTER ?? []), []);

  const filtered = useMemo(() =>
    enriched
      .filter(p => p.position === activePos)
      .sort((a, b) => (b.projectedPts ?? 0) - (a.projectedPts ?? 0)),
    [enriched, activePos]
  );

  const handleSelect = (player) => {
    if (!selectedA || (selectedA?.gsisId === player.gsisId)) {
      setSelectedA(player);
    } else if (!selectedB || (selectedB?.gsisId === player.gsisId)) {
      setSelectedB(player);
    } else {
      // Replace A, shift B to A
      setSelectedA(selectedB);
      setSelectedB(player);
    }
  };

  const handleClear = () => {
    setSelectedA(null);
    setSelectedB(null);
  };

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Serif+Display&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: ${C.bg}; }
        button:hover { opacity: 0.85; }
      `}</style>

      <div style={{ minHeight: '100vh', background: C.bg, color: C.text, fontFamily: font }}>

        {/* Header */}
        <header style={{
          borderBottom: `1px solid ${C.border}`,
          padding: '20px 40px',
          display: 'flex',
          alignItems: 'center',
          gap: '20px',
        }}>
          <button
            onClick={onBack}
            style={{
              background: 'none', border: 'none', color: C.textDim,
              cursor: 'pointer', fontSize: '11px', letterSpacing: '0.12em',
              fontFamily: font, padding: '0',
            }}
          >
            ← back
          </button>
          <span style={{ color: C.border }}>|</span>
          <span style={{ fontSize: '11px', letterSpacing: '0.18em', textTransform: 'uppercase', color: C.accent }}>
            Gridiron Oracle
          </span>
          <span style={{ fontSize: '11px', color: C.textDim }}>
            Matchup Explorer
          </span>
          {MATCHUP && (
            <span style={{ marginLeft: 'auto', fontSize: '11px', color: C.textDim }}>
              Week {MATCHUP.week} · vs {MATCHUP.opp_team_name}
            </span>
          )}
        </header>

        <div style={{ maxWidth: '860px', margin: '0 auto', padding: '40px' }}>

          {/* Instruction */}
          <div style={{ fontSize: '12px', color: C.textMid, marginBottom: '24px', lineHeight: 1.6 }}>
            Select two players to compare head-to-head. Filter by position below.
          </div>

          {/* Position tabs */}
          <div style={{ display: 'flex', gap: '8px', marginBottom: '24px', flexWrap: 'wrap' }}>
            {POSITIONS.map(pos => (
              <PosTab
                key={pos}
                pos={pos}
                active={activePos === pos}
                onClick={setActivePos}
              />
            ))}
          </div>

          {/* Player list */}
          <div>
            {filtered.length === 0 ? (
              <div style={{ fontSize: '12px', color: C.textDim, padding: '20px 0' }}>
                No {activePos} players on your roster.
              </div>
            ) : (
              filtered.map(player => (
                <PlayerCard
                  key={player.gsisId}
                  player={player}
                  isSelected={
                    selectedA?.gsisId === player.gsisId ||
                    selectedB?.gsisId === player.gsisId
                  }
                  onClick={handleSelect}
                />
              ))
            )}
          </div>

          {/* Comparison panel */}
          <ComparisonPanel
            playerA={selectedA}
            playerB={selectedB}
            onClear={handleClear}
          />

          {/* DEF rank table */}
          <DefRankTable players={enriched} />

        </div>
      </div>
    </>
  );
}
