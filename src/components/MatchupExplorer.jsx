// MatchupExplorer.jsx — Gridiron Oracle
// True head-to-head matchup view: your lineup vs opponent's actual lineup
// Side-by-side by slot, player comparison panel, DEF rank grid

import { useState, useMemo } from 'react';
import { useTeam } from '../utils/TeamContext.jsx';
import { useMobile, contentPadding } from '../utils/useMobile.js';
import {
  ESPN_LEAGUE_DATA,
  ALL_ROSTERS,
  ALL_TEAMS,
} from '../utils/espn_league.js';
import { PLAYER_BY_GSIS_ID } from '../utils/nfl_data.js';
import { ESPN_TO_GSIS } from '../utils/id_mapping.js';
import {
  computeCompositeRating,
  projectPoints,
  getReplacementLevel,
  getOptimalLineup,
} from '../utils/simulator.js';
import { hasWeatherImpact, getWeatherAdvisory } from '../utils/weather_data.js';

// ---------------------------------------------------------------------------
// Design tokens
// ---------------------------------------------------------------------------

import { C, font, serif, POS_COLOR } from '../utils/theme.js';

// Canonical slot order for side-by-side display
const SLOT_ORDER = ['QB', 'RB', 'RB', 'WR', 'WR', 'TE', 'FLEX', 'K', 'DST'];

// ---------------------------------------------------------------------------
// Enrich a roster entry with nfl_data.js composite rating + projections
// ---------------------------------------------------------------------------

function enrichPlayer(p, allPlayers) {
  const gsisId  = p.gsisId ?? ESPN_TO_GSIS[String(p.espn_id ?? '')] ?? null;
  const nflData = gsisId ? PLAYER_BY_GSIS_ID?.[gsisId] : null;

  if (!nflData) {
    return {
      ...p,
      gsisId,
      projectedPts:    p.projected_points ?? p.avg_points ?? 0,
      compositeRating: 50,
      vorp:            0,
      hasNflData:      false,
    };
  }

  const compositeRating = computeCompositeRating(nflData, allPlayers);
  const projectedPts    = projectPoints(nflData, compositeRating, 0);
  const replacement     = getReplacementLevel(nflData.position, 12);

  return {
    ...p,
    ...nflData,
    gsisId,
    projectedPts,
    compositeRating,
    vorp:       projectedPts - replacement,
    hasNflData: true,
  };
}

function enrichRoster(rawRoster) {
  if (!rawRoster || rawRoster.length === 0) return [];
  const allNflPlayers = rawRoster
    .map(p => {
      const gsisId = p.gsisId ?? ESPN_TO_GSIS[String(p.espn_id ?? '')] ?? null;
      return gsisId ? PLAYER_BY_GSIS_ID?.[gsisId] : null;
    })
    .filter(Boolean);
  return rawRoster.map(p => enrichPlayer(p, allNflPlayers));
}

// ---------------------------------------------------------------------------
// Build opponent roster from espn_league.js
// Normalizes field names to match my roster shape
// ---------------------------------------------------------------------------

function buildOppRoster(oppTeamId) {
  if (!oppTeamId) return [];
  const raw = ALL_ROSTERS?.[String(oppTeamId)] ?? [];
  return raw.map(p => ({
    espn_id:          String(p.espn_id ?? ''),
    gsisId:           ESPN_TO_GSIS[String(p.espn_id ?? '')] ?? null,
    name:             p.name ?? 'Unknown',
    position:         p.position ?? 'UNK',
    team:             p.team ?? 'UNK',
    lineup_slot:      p.lineup_slot ?? 'BENCH',
    on_bench:         p.on_bench  ?? true,
    on_ir:            p.on_ir     ?? false,
    avg_points:       p.avg_points       ?? 0,
    projected_points: p.projected_points ?? p.avg_points ?? 0,
    play_probability: p.play_probability ?? 1.0,
    injury_status:    p.injury_status    ?? 'ACTIVE',
    injury_detail:    p.injury_detail    ?? '',
  }));
}

// ---------------------------------------------------------------------------
// Get optimal starting lineup (sorted by slot order)
// Falls back to ESPN-set lineup if no nfl_data match
// ---------------------------------------------------------------------------

function getStartingLineup(enrichedRoster) {
  // Use ESPN-set starters if available
  const espnStarters = enrichedRoster.filter(p => !p.on_bench && !p.on_ir);
  if (espnStarters.length >= 8) {
    return sortBySlot(espnStarters);
  }
  // Fallback: build optimal from projected pts
  return sortBySlot(enrichedRoster
    .filter(p => !p.on_ir)
    .sort((a, b) => (b.projectedPts ?? 0) - (a.projectedPts ?? 0))
    .slice(0, 9));
}

function sortBySlot(players) {
  const slotPriority = { QB: 0, RB: 1, WR: 2, TE: 3, FLEX: 4, K: 5, DST: 6 };
  return [...players].sort((a, b) => {
    const slotA = a.lineup_slot ?? a.lineupSlot ?? a.position ?? 'BENCH';
    const slotB = b.lineup_slot ?? b.lineupSlot ?? b.position ?? 'BENCH';
    return (slotPriority[slotA] ?? 7) - (slotPriority[slotB] ?? 7);
  });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function defRankColor(rank) {
  if (!rank) return C.textDim;
  if (rank <= 5)  return C.red;
  if (rank <= 12) return C.amber;
  if (rank >= 28) return C.green;
  if (rank >= 22) return '#90d060';
  return C.textMid;
}

function defRankLabel(rank) {
  if (!rank || rank === 16) return '—';
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
      fontSize: '8px', fontWeight: '700', letterSpacing: '0.10em',
      background: color + '20', color, padding: '2px 5px', borderRadius: '3px',
      minWidth: '28px', textAlign: 'center', display: 'inline-block',
    }}>{pos}</span>
  );
}

function InjuryDot({ prob }) {
  if (!prob || prob >= 1.0) return null;
  const color = prob === 0 ? C.red : prob <= 0.55 ? C.amber : '#d0c030';
  return (
    <span style={{
      width: '5px', height: '5px', borderRadius: '50%',
      background: color, display: 'inline-block', marginLeft: '5px',
      flexShrink: 0, verticalAlign: 'middle',
    }} title={`${Math.round(prob * 100)}% to play`} />
  );
}

// ---------------------------------------------------------------------------
// Side-by-side player row
// ---------------------------------------------------------------------------

function MatchupRow({ myPlayer, oppPlayer, slotLabel, onSelect, selectedId }) {
  const myPts  = myPlayer?.projectedPts  ?? 0;
  const oppPts = oppPlayer?.projectedPts ?? 0;
  const myWins = myPts > oppPts;
  const tied   = Math.abs(myPts - oppPts) < 0.5;

  const mySelected  = selectedId && (myPlayer?.gsisId === selectedId || myPlayer?.espn_id === selectedId);
  const oppSelected = selectedId && (oppPlayer?.gsisId === selectedId || oppPlayer?.espn_id === selectedId);

  const rowStyle = {
    display:      'grid',
    gridTemplateColumns: '1fr 48px 1fr',
    gap:          '8px',
    padding:      '10px 0',
    borderBottom: `1px solid ${C.border}`,
    alignItems:   'center',
  };

  const playerStyle = (isMe, player, isSelected) => ({
    display:      'flex',
    flexDirection: isMe ? 'row' : 'row-reverse',
    alignItems:   'center',
    gap:          '8px',
    padding:      '8px 10px',
    borderRadius: '5px',
    cursor:       player ? 'pointer' : 'default',
    background:   isSelected ? (isMe ? C.accent + '12' : C.red + '08') : 'transparent',
    border:       `1px solid ${isSelected ? (isMe ? C.accentDim : C.red + '40') : 'transparent'}`,
    transition:   'all 0.12s',
  });

  return (
    <div style={rowStyle}>
      {/* My player — left side */}
      <div
        style={playerStyle(true, myPlayer, mySelected)}
        onClick={() => myPlayer && onSelect(myPlayer)}
      >
        {myPlayer ? (
          <>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '2px' }}>
                <span style={{ fontSize: '12px', color: C.text, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {myPlayer.name}
                </span>
                <InjuryDot prob={myPlayer.play_probability} />
                {hasWeatherImpact(myPlayer.team) && (
                  <span title={getWeatherAdvisory(myPlayer.team).join(' · ')} style={{ fontSize: '9px' }}>🌬</span>
                )}
              </div>
              <div style={{ fontSize: '9px', color: C.textDim }}>
                {myPlayer.team}
                {myPlayer.opp_def_rank && (
                  <span style={{ marginLeft: '6px', color: defRankColor(myPlayer.opp_def_rank) }}>
                    vs {defRankLabel(myPlayer.opp_def_rank)}
                  </span>
                )}
              </div>
            </div>
            <div style={{ textAlign: 'right', flexShrink: 0 }}>
              <div style={{ fontSize: '16px', fontFamily: serif, color: (!tied && myWins) ? C.accent : C.textMid }}>
                {myPts.toFixed(1)}
              </div>
              {myPlayer.vorp != null && (
                <div style={{ fontSize: '9px', color: myPlayer.vorp >= 0 ? C.green : C.red }}>
                  {myPlayer.vorp >= 0 ? '+' : ''}{myPlayer.vorp.toFixed(1)}
                </div>
              )}
            </div>
          </>
        ) : (
          <span style={{ fontSize: '11px', color: C.textDim, fontStyle: 'italic' }}>—</span>
        )}
      </div>

      {/* Slot label — center */}
      <div style={{ textAlign: 'center' }}>
        <PosTag pos={slotLabel} />
        {!tied && (
          <div style={{ fontSize: '8px', color: myWins ? C.accent : C.red, marginTop: '4px' }}>
            {myWins ? '◀' : '▶'}
          </div>
        )}
      </div>

      {/* Opp player — right side */}
      <div
        style={playerStyle(false, oppPlayer, oppSelected)}
        onClick={() => oppPlayer && onSelect(oppPlayer)}
      >
        {oppPlayer ? (
          <>
            <div style={{ textAlign: 'left', flexShrink: 0 }}>
              <div style={{ fontSize: '16px', fontFamily: serif, color: (!tied && !myWins) ? C.red : C.textMid }}>
                {oppPts.toFixed(1)}
              </div>
              {oppPlayer.vorp != null && (
                <div style={{ fontSize: '9px', color: oppPlayer.vorp >= 0 ? C.green : C.red }}>
                  {oppPlayer.vorp >= 0 ? '+' : ''}{oppPlayer.vorp.toFixed(1)}
                </div>
              )}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '2px', justifyContent: 'flex-end' }}>
                {hasWeatherImpact(oppPlayer.team) && (
                  <span title={getWeatherAdvisory(oppPlayer.team).join(' · ')} style={{ fontSize: '9px' }}>🌬</span>
                )}
                <InjuryDot prob={oppPlayer.play_probability} />
                <span style={{ fontSize: '12px', color: C.textMid, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {oppPlayer.name}
                </span>
              </div>
              <div style={{ fontSize: '9px', color: C.textDim, textAlign: 'right' }}>
                {oppPlayer.team}
                {oppPlayer.opp_def_rank && (
                  <span style={{ marginLeft: '6px', color: defRankColor(oppPlayer.opp_def_rank) }}>
                    vs {defRankLabel(oppPlayer.opp_def_rank)}
                  </span>
                )}
              </div>
            </div>
          </>
        ) : (
          <span style={{ fontSize: '11px', color: C.textDim, fontStyle: 'italic' }}>—</span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Player detail panel — shown when a player is selected
// ---------------------------------------------------------------------------

function PlayerDetailPanel({ player, side, onClose }) {
  if (!player) return null;
  const color = side === 'me' ? C.accent : C.red;

  const stats = [
    { label: 'Projected pts',    val: player.projectedPts,    fmt: v => v?.toFixed(1) },
    { label: 'Composite rating', val: player.compositeRating, fmt: v => v?.toFixed(0) },
    { label: 'Season avg pts',   val: player.season_avg_pts ?? player.avg_points, fmt: v => v?.toFixed(1) },
    { label: 'EPA / play',       val: player.epa_per_play,    fmt: v => v?.toFixed(3) },
    { label: 'Snap %',           val: player.snap_pct,        fmt: v => v != null ? (v * 100).toFixed(0) + '%' : null },
    { label: 'Target share',     val: player.target_share,    fmt: v => v != null ? (v * 100).toFixed(0) + '%' : null },
    { label: 'Carry share',      val: player.carry_share,     fmt: v => v != null ? (v * 100).toFixed(0) + '%' : null },
    { label: 'Red zone share',   val: player.red_zone_share,  fmt: v => v != null ? (v * 100).toFixed(0) + '%' : null },
  ].filter(s => s.val != null && s.fmt(s.val) != null);

  return (
    <div style={{
      padding:      '20px',
      background:   C.surface,
      border:       `1px solid ${color}40`,
      borderRadius: '6px',
      marginTop:    '16px',
      animation:    'fadeIn 0.2s ease',
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '16px' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
            <PosTag pos={player.position} />
            <span style={{ fontSize: '15px', color: C.text }}>{player.name}</span>
            <InjuryDot prob={player.play_probability} />
          </div>
          <div style={{ fontSize: '10px', color: C.textDim }}>
            {player.team}
            {player.opp_def_rank && (
              <span style={{ marginLeft: '8px', color: defRankColor(player.opp_def_rank) }}>
                vs DEF {defRankLabel(player.opp_def_rank)}
              </span>
            )}
            {player.injury_detail && player.injury_detail !== 'ACTIVE' && (
              <span style={{ marginLeft: '8px', color: C.amber }}>⚠ {player.injury_detail}</span>
            )}
          </div>
        </div>
        <button
          onClick={onClose}
          style={{ background: 'none', border: 'none', color: C.textDim, cursor: 'pointer', fontSize: '14px', padding: '0' }}
        >
          ✕
        </button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
        {stats.map(({ label, val, fmt }) => (
          <div key={label} style={{
            padding:      '8px 10px',
            background:   C.bg,
            borderRadius: '4px',
            border:       `1px solid ${C.border}`,
          }}>
            <div style={{ fontSize: '9px', color: C.textDim, marginBottom: '3px', letterSpacing: '0.10em', textTransform: 'uppercase' }}>
              {label}
            </div>
            <div style={{ fontSize: '14px', fontFamily: serif, color }}>
              {fmt(val)}
            </div>
          </div>
        ))}
      </div>

      {hasWeatherImpact(player.team) && (
        <div style={{ marginTop: '12px', fontSize: '10px', color: C.amber, padding: '8px 10px', background: C.amber + '10', borderRadius: '4px' }}>
          🌬 {getWeatherAdvisory(player.team).join(' · ')}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Projected totals summary bar
// ---------------------------------------------------------------------------

function MatchupSummary({ myStarters, oppStarters, myTeamName, oppTeamName }) {
  const myTotal  = myStarters.reduce((s, p) => s + (p.projectedPts ?? 0), 0);
  const oppTotal = oppStarters.reduce((s, p) => s + (p.projectedPts ?? 0), 0);
  const total    = myTotal + oppTotal || 1;
  const myPct    = (myTotal / total) * 100;

  const myAdvantage  = myStarters.filter((p, i) => (p.projectedPts ?? 0) > (oppStarters[i]?.projectedPts ?? 0)).length;
  const oppAdvantage = oppStarters.filter((p, i) => (p.projectedPts ?? 0) > (myStarters[i]?.projectedPts ?? 0)).length;

  return (
    <div style={{
      padding:      '20px 24px',
      background:   C.surface,
      border:       `1px solid ${C.border}`,
      borderRadius: '6px',
      marginBottom: '24px',
    }}>
      {/* Team names + totals */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 80px 1fr', gap: '8px', alignItems: 'center', marginBottom: '16px' }}>
        <div>
          <div style={{ fontSize: '11px', color: C.textDim, letterSpacing: '0.10em', textTransform: 'uppercase', marginBottom: '4px' }}>
            {myTeamName}
          </div>
          <div style={{ fontSize: '28px', fontFamily: serif, color: myTotal >= oppTotal ? C.accent : C.textMid }}>
            {myTotal.toFixed(1)}
          </div>
          <div style={{ fontSize: '10px', color: C.textDim, marginTop: '2px' }}>
            {myAdvantage}/{myStarters.length} slot wins
          </div>
        </div>
        <div style={{ textAlign: 'center', fontSize: '11px', color: C.textDim, letterSpacing: '0.10em' }}>
          proj
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontSize: '11px', color: C.textDim, letterSpacing: '0.10em', textTransform: 'uppercase', marginBottom: '4px' }}>
            {oppTeamName}
          </div>
          <div style={{ fontSize: '28px', fontFamily: serif, color: oppTotal > myTotal ? C.red : C.textMid }}>
            {oppTotal.toFixed(1)}
          </div>
          <div style={{ fontSize: '10px', color: C.textDim, marginTop: '2px' }}>
            {oppAdvantage}/{oppStarters.length} slot wins
          </div>
        </div>
      </div>

      {/* Score bar */}
      <div style={{ height: '4px', background: C.border, borderRadius: '2px', overflow: 'hidden' }}>
        <div style={{
          height:     '100%',
          width:      `${myPct}%`,
          background: myTotal >= oppTotal ? C.accent : C.red,
          borderRadius: '2px',
          transition: 'width 0.6s ease',
        }} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function MatchupExplorer({ onBack }) {
  const { teamData } = useTeam();
  const { isMobile, isNarrow } = useMobile();
  const [selectedPlayer, setSelectedPlayer] = useState(null);
  const [selectedSide,   setSelectedSide]   = useState(null);
  const [showOppBench,   setShowOppBench]   = useState(false);

  const pad = contentPadding(isMobile, isNarrow);

  const myRoster  = teamData?.myRoster  ?? [];
  const matchup   = teamData?.matchup   ?? null;
  const myTeam    = teamData?.myTeam    ?? null;

  const oppTeamId   = matchup?.opp_team_id ?? null;
  const oppTeamName = matchup?.opp_team_name ?? 'Opponent';
  const oppTeam     = (ALL_TEAMS ?? []).find(t => t.team_id === oppTeamId);

  // Enrich my roster
  const myEnriched = useMemo(() => enrichRoster(myRoster), [myRoster]);

  // Build + enrich opponent roster
  const oppEnriched = useMemo(() => {
    const raw = buildOppRoster(oppTeamId);
    return enrichRoster(raw);
  }, [oppTeamId]);

  // Get starters for each side
  const myStarters  = useMemo(() => getStartingLineup(myEnriched),  [myEnriched]);
  const oppStarters = useMemo(() => getStartingLineup(oppEnriched), [oppEnriched]);
  const oppBench    = useMemo(() =>
    oppEnriched.filter(p => p.on_bench && !p.on_ir),
    [oppEnriched]
  );

  const handleSelect = (player, side) => {
    if (selectedPlayer?.gsisId === player.gsisId && selectedPlayer?.espn_id === player.espn_id) {
      setSelectedPlayer(null);
      setSelectedSide(null);
    } else {
      setSelectedPlayer(player);
      setSelectedSide(side);
    }
  };

  // No matchup guard
  if (!matchup) {
    return (
      <div style={{ minHeight: '100vh', background: C.bg, color: C.text, fontFamily: font }}>
        <header style={{ borderBottom: `1px solid ${C.border}`, padding: '20px 40px', display: 'flex', alignItems: 'center', gap: '20px' }}>
          <button onClick={onBack} style={{ background: 'none', border: 'none', color: C.textDim, cursor: 'pointer', fontSize: '11px', fontFamily: font }}>← back</button>
          <span style={{ color: C.border }}>|</span>
          <span style={{ fontSize: '11px', letterSpacing: '0.18em', textTransform: 'uppercase', color: C.accent }}>Gridiron Oracle</span>
          <span style={{ fontSize: '11px', color: C.textDim }}>Matchup Explorer</span>
        </header>
        <div style={{ maxWidth: '860px', margin: '80px auto', padding: '0 40px', textAlign: 'center' }}>
          <div style={{ fontSize: '13px', color: C.textDim, marginBottom: '8px' }}>No matchup data for this week.</div>
          <div style={{ fontSize: '11px', color: C.textDim }}>
            Run <code style={{ color: C.accent }}>python3 scripts/fetch_espn_league.py</code> to refresh.
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
          <button
            onClick={onBack}
            style={{ background: 'none', border: 'none', color: C.textDim, cursor: 'pointer', fontSize: '11px', letterSpacing: '0.12em', fontFamily: font, padding: '0' }}
          >
            ← back
          </button>
          <span style={{ color: C.border }}>|</span>
          <span style={{ fontSize: '11px', letterSpacing: '0.18em', textTransform: 'uppercase', color: C.accent }}>
            Gridiron Oracle
          </span>
          <span style={{ fontSize: '11px', color: C.textDim, letterSpacing: '0.10em' }}>
            Matchup Explorer
          </span>
          <span style={{ marginLeft: 'auto', fontSize: '11px', color: C.textDim }}>
            Week {matchup.week} · vs {oppTeamName}
          </span>
        </header>

        <div style={{ maxWidth: '900px', margin: '0 auto', padding: `28px ${pad} 100px` }}>

          {/* Summary bar */}
          <MatchupSummary
            myStarters={myStarters}
            oppStarters={oppStarters}
            myTeamName={isMobile ? (myTeam?.team_name?.split(' ')[0] ?? 'Me') : (myTeam?.team_name ?? 'My Team')}
            oppTeamName={isMobile ? (oppTeamName?.split(' ')[0] ?? 'Opp') : oppTeamName}
          />

          {/* Column headers */}
          <div style={{
            display:             'grid',
            gridTemplateColumns: '1fr 48px 1fr',
            gap:                 '8px',
            marginBottom:        '8px',
          }}>
            <div style={{ fontSize: '9px', letterSpacing: '0.16em', textTransform: 'uppercase', color: C.accent }}>
              {myTeam?.team_name ?? 'My Team'}
            </div>
            <div />
            <div style={{ fontSize: '9px', letterSpacing: '0.16em', textTransform: 'uppercase', color: C.textDim, textAlign: 'right' }}>
              {oppTeamName}
            </div>
          </div>

          {/* Sub-header row */}
          <div style={{
            display:             'grid',
            gridTemplateColumns: '1fr 48px 1fr',
            gap:                 '8px',
            marginBottom:        '4px',
            paddingBottom:       '8px',
            borderBottom:        `1px solid ${C.borderMid}`,
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', paddingRight: '10px' }}>
              <span style={{ fontSize: '9px', color: C.textDim, letterSpacing: '0.10em', textTransform: 'uppercase' }}>Player</span>
              <span style={{ fontSize: '9px', color: C.textDim, letterSpacing: '0.10em', textTransform: 'uppercase' }}>Proj / VORP</span>
            </div>
            <div />
            <div style={{ display: 'flex', justifyContent: 'space-between', paddingLeft: '10px' }}>
              <span style={{ fontSize: '9px', color: C.textDim, letterSpacing: '0.10em', textTransform: 'uppercase' }}>Proj / VORP</span>
              <span style={{ fontSize: '9px', color: C.textDim, letterSpacing: '0.10em', textTransform: 'uppercase' }}>Player</span>
            </div>
          </div>

          {/* Side-by-side rows */}
          {myStarters.map((myP, i) => {
            const oppP     = oppStarters[i] ?? null;
            const slotLabel = myP.lineup_slot ?? myP.lineupSlot ?? myP.position ?? 'FLEX';
            const selId    = selectedPlayer?.gsisId ?? selectedPlayer?.espn_id;
            return (
              <MatchupRow
                key={i}
                myPlayer={myP}
                oppPlayer={oppP}
                slotLabel={slotLabel}
                selectedId={selId}
                onSelect={(p) => {
                  const side = (p.gsisId && p.gsisId === myP?.gsisId) || p.espn_id === myP?.espn_id ? 'me' : 'opp';
                  handleSelect(p, side);
                }}
              />
            );
          })}

          {/* Player detail panel */}
          {selectedPlayer && (
            <PlayerDetailPanel
              player={selectedPlayer}
              side={selectedSide}
              onClose={() => { setSelectedPlayer(null); setSelectedSide(null); }}
            />
          )}

          {/* Opponent bench toggle */}
          {oppBench.length > 0 && (
            <div style={{ marginTop: '24px' }}>
              <button
                onClick={() => setShowOppBench(v => !v)}
                style={{
                  background:    'transparent',
                  border:        `1px solid ${C.border}`,
                  borderRadius:  '4px',
                  color:         C.textDim,
                  fontSize:      '10px',
                  letterSpacing: '0.12em',
                  textTransform: 'uppercase',
                  cursor:        'pointer',
                  fontFamily:    font,
                  padding:       '7px 14px',
                }}
              >
                {showOppBench ? 'Hide' : 'Show'} opponent bench ({oppBench.length})
              </button>

              {showOppBench && (
                <div style={{ marginTop: '12px', animation: 'fadeIn 0.2s ease' }}>
                  <div style={{ fontSize: '9px', letterSpacing: '0.16em', textTransform: 'uppercase', color: C.textDim, marginBottom: '10px' }}>
                    {oppTeamName} bench
                  </div>
                  {oppBench.map((p, i) => (
                    <div key={p.espn_id ?? i} style={{
                      display:      'flex',
                      alignItems:   'center',
                      gap:          '10px',
                      padding:      '8px 0',
                      borderBottom: `1px solid ${C.border}`,
                      opacity:      0.6,
                    }}>
                      <PosTag pos={p.position} />
                      <span style={{ fontSize: '12px', color: C.textMid, flex: 1 }}>{p.name}</span>
                      <InjuryDot prob={p.play_probability} />
                      <span style={{ fontSize: '11px', color: C.textDim }}>{(p.projectedPts ?? 0).toFixed(1)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

        </div>
      </div>
    </>
  );
}
