// TradeAnalyzer.jsx — Gridiron Oracle
// Win probability delta (now) + rest-of-season value (long term) + fairness rating
// v2.0 Step 8 per spec §4.3 / §7

import { useState, useMemo, useCallback } from 'react';
import { MY_ROSTER, MY_TEAM, MATCHUP, LEAGUE } from '../utils/espn_data.js';
import {
  ESPN_LEAGUE_DATA,
  ALL_ROSTERS,
  FULL_SCHEDULE,
  LEAGUE_FETCHED_AT,
} from '../utils/espn_league.js';
import { PLAYER_BY_GSIS_ID } from '../utils/nfl_data.js';
import { ESPN_TO_GSIS } from '../utils/id_mapping.js';
import {
  prepareLineup,
  runSimulation,
  simulateMatchup,
  computeVORP,
  getReplacementLevel,
  getOptimalLineup,
} from '../utils/simulator.js';

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

// ---------------------------------------------------------------------------
// Trade fairness rating — per spec §7.2
// VORP delta of players exchanged, normalized to 0–100
// Near 50 = balanced. <35 = unfavorable. >65 = favorable.
// ---------------------------------------------------------------------------

function computeFairnessRating(givingPlayers, receivingPlayers, leagueSize = 12) {
  const vorpSum = (players) =>
    players.reduce((sum, p) => {
      const gsisId  = ESPN_TO_GSIS[String(p.espn_id)] ?? p.gsisId;
      const nflData = gsisId ? PLAYER_BY_GSIS_ID?.[gsisId] : null;
      const pts     = nflData?.season_avg_pts ?? p.projected_points ?? p.avg_points ?? 0;
      const replLvl = getReplacementLevel(p.position, leagueSize);
      return sum + (pts - replLvl);
    }, 0);

  const givingVORP    = vorpSum(givingPlayers);
  const receivingVORP = vorpSum(receivingPlayers);
  const delta         = receivingVORP - givingVORP;

  // Normalize: delta of 0 = 50, delta of ±10 VORP ≈ ±15 points on scale
  const normalized = 50 + (delta / 10) * 15;
  return Math.round(Math.max(0, Math.min(100, normalized)));
}

function fairnessLabel(score) {
  if (score >= 65) return { text: 'Favorable',   color: C.green };
  if (score >= 45) return { text: 'Balanced',     color: C.textMid };
  if (score >= 35) return { text: 'Slight loss',  color: C.amber };
  return              { text: 'Unfavorable',   color: C.red };
}

// ---------------------------------------------------------------------------
// Build post-trade roster — apply trade to a roster array
// ---------------------------------------------------------------------------

function applyTrade(roster, givingEspnIds, receivingPlayers) {
  const giveSet = new Set(givingEspnIds.map(String));
  // Remove players being sent away
  const withoutGiven = roster.filter(p => !giveSet.has(String(p.espn_id)));
  // Add players coming in — mark as bench initially
  const incoming = receivingPlayers.map(p => ({
    ...p,
    on_bench:         true,
    on_ir:            false,
    lineup_slot:      'BENCH',
    gsisId:           ESPN_TO_GSIS[String(p.espn_id)] ?? p.espn_id,
    projectedPts:     p.projected_points ?? p.avg_points ?? 0,
    play_probability: p.play_probability ?? 1.0,
  }));
  return [...withoutGiven, ...incoming];
}

// ---------------------------------------------------------------------------
// Normalize a roster for prepareLineup / simulateMatchup
// ---------------------------------------------------------------------------

function normalizeRoster(rosterEntries) {
  return rosterEntries.map(p => ({
    ...p,
    gsisId:           p.gsisId ?? ESPN_TO_GSIS[String(p.espn_id)] ?? p.espn_id,
    onBench:          p.on_bench ?? p.onBench ?? false,
    onIR:             p.on_ir    ?? p.onIR    ?? false,
    projectedPts:     p.projected_points ?? p.avg_points ?? p.projectedPts ?? 0,
    play_probability: p.play_probability ?? 1.0,
    lineupSlot:       p.lineup_slot ?? p.lineupSlot ?? 'BENCH',
  }));
}

// ---------------------------------------------------------------------------
// ROS value estimate — weighted sum of projected pts over remaining weeks
// Per spec §7.1: uses schedule data to count remaining games
// ---------------------------------------------------------------------------

function computeROSValue(rosterEntries, myTeamId, leagueSize = 12) {
  const schedule      = FULL_SCHEDULE ?? [];
  const currentWeek   = ESPN_LEAGUE_DATA?.current_week ?? 1;
  const remainingWeeks = Math.max(0, 17 - currentWeek);

  // Get optimal starters from this roster
  const normalized = normalizeRoster(rosterEntries);
  const optimal    = getOptimalLineup(normalized, {}, leagueSize);

  // Sum projected points for all starters × remaining weeks
  // Weight last3 more than season avg (recency) — same as projectPoints()
  const weeklyProjection = optimal.reduce((sum, p) => {
    return sum + (p.projectedPts ?? 0) * (p.play_probability ?? 1.0);
  }, 0);

  return {
    weeklyProjection: Math.round(weeklyProjection * 10) / 10,
    remainingWeeks,
    totalProjected:   Math.round(weeklyProjection * remainingWeeks * 10) / 10,
    starterCount:     optimal.length,
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

function FairnessGauge({ score }) {
  const { text, color } = fairnessLabel(score);
  const r      = 36;
  const stroke = 5;
  const circ   = 2 * Math.PI * r;
  const dash   = (score / 100) * circ;

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
      <div style={{ position: 'relative', width: '88px', height: '88px', flexShrink: 0 }}>
        <svg width="88" height="88" viewBox="0 0 88 88">
          <circle cx="44" cy="44" r={r} fill="none" stroke={C.border} strokeWidth={stroke} />
          <circle
            cx="44" cy="44" r={r}
            fill="none"
            stroke={color}
            strokeWidth={stroke}
            strokeDasharray={`${dash} ${circ - dash}`}
            strokeLinecap="round"
            transform="rotate(-90 44 44)"
            style={{ transition: 'stroke-dasharray 0.8s cubic-bezier(0.4,0,0.2,1)' }}
          />
        </svg>
        <div style={{
          position: 'absolute', inset: 0,
          display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center',
        }}>
          <div style={{ fontSize: '18px', fontFamily: serif, color, lineHeight: 1 }}>{score}</div>
          <div style={{ fontSize: '8px', color: C.textDim, letterSpacing: '0.12em', textTransform: 'uppercase', marginTop: '2px' }}>/ 100</div>
        </div>
      </div>
      <div>
        <div style={{ fontSize: '14px', color, marginBottom: '4px' }}>{text}</div>
        <div style={{ fontSize: '10px', color: C.textDim, lineHeight: 1.6 }}>
          {score >= 65 && 'You gain more value than you give up.'}
          {score >= 45 && score < 65 && 'Roughly even exchange by VORP.'}
          {score >= 35 && score < 45 && 'You give up slightly more value.'}
          {score < 35  && 'You give up significantly more value.'}
        </div>
        <div style={{ fontSize: '9px', color: C.textDim, marginTop: '6px' }}>Based on VORP delta · not a recommendation</div>
      </div>
    </div>
  );
}

function WinDeltaDisplay({ before, after, label }) {
  if (before == null || after == null) return null;
  const delta     = after - before;
  const deltaColor = delta > 2 ? C.green : delta < -2 ? C.red : C.textMid;

  return (
    <div style={{ flex: 1 }}>
      <div style={{ fontSize: '9px', letterSpacing: '0.16em', color: C.textDim, textTransform: 'uppercase', marginBottom: '8px' }}>
        {label}
      </div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: '10px' }}>
        <span style={{ fontSize: '28px', fontFamily: serif, color: C.accent }}>
          {after?.toFixed(1)}%
        </span>
        <span style={{ fontSize: '13px', color: deltaColor }}>
          {delta >= 0 ? '+' : ''}{delta.toFixed(1)}%
        </span>
      </div>
      <div style={{ fontSize: '10px', color: C.textDim, marginTop: '4px' }}>
        Before: {before?.toFixed(1)}%
      </div>
    </div>
  );
}

function ROSComparison({ before, after }) {
  if (!before || !after) return null;
  const delta      = after.weeklyProjection - before.weeklyProjection;
  const deltaColor = delta > 2 ? C.green : delta < -2 ? C.red : C.textMid;

  return (
    <div style={{
      display:      'flex',
      gap:          '24px',
      padding:      '20px 24px',
      background:   C.surface,
      border:       `1px solid ${C.border}`,
      borderRadius: '6px',
    }}>
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: '9px', letterSpacing: '0.14em', color: C.textDim, textTransform: 'uppercase', marginBottom: '8px' }}>
          Weekly proj (before)
        </div>
        <div style={{ fontSize: '24px', fontFamily: serif, color: C.textMid }}>
          {before.weeklyProjection}
        </div>
      </div>
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: '9px', letterSpacing: '0.14em', color: C.textDim, textTransform: 'uppercase', marginBottom: '8px' }}>
          Weekly proj (after)
        </div>
        <div style={{ fontSize: '24px', fontFamily: serif, color: C.accent }}>
          {after.weeklyProjection}
          <span style={{ fontSize: '14px', color: deltaColor, marginLeft: '8px' }}>
            {delta >= 0 ? '+' : ''}{delta.toFixed(1)}
          </span>
        </div>
      </div>
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: '9px', letterSpacing: '0.14em', color: C.textDim, textTransform: 'uppercase', marginBottom: '8px' }}>
          ROS total ({after.remainingWeeks} weeks)
        </div>
        <div style={{ fontSize: '24px', fontFamily: serif, color: C.textMid }}>
          {after.totalProjected}
        </div>
      </div>
    </div>
  );
}

function PlayerPicker({ label, color, players, selected, onToggle }) {
  return (
    <div style={{ flex: 1 }}>
      <div style={{
        fontSize:      '9px',
        letterSpacing: '0.16em',
        textTransform: 'uppercase',
        color,
        marginBottom:  '10px',
      }}>
        {label}
      </div>
      <div style={{
        border:       `1px solid ${C.border}`,
        borderRadius: '5px',
        overflow:     'hidden',
        maxHeight:    '320px',
        overflowY:    'auto',
      }}>
        {players.map(p => {
          const isSelected = selected.has(String(p.espn_id));
          const pts        = p.projected_points ?? p.avg_points ?? 0;
          return (
            <div
              key={p.espn_id}
              onClick={() => onToggle(String(p.espn_id), p)}
              style={{
                display:       'flex',
                alignItems:    'center',
                padding:       '9px 12px',
                borderBottom:  `1px solid ${C.border}`,
                cursor:        'pointer',
                background:    isSelected ? color + '15' : 'transparent',
                borderLeft:    isSelected ? `2px solid ${color}` : '2px solid transparent',
                transition:    'all 0.1s',
              }}
            >
              <PosTag pos={p.position} />
              <span style={{ fontSize: '12px', color: isSelected ? C.text : C.textMid, flex: 1 }}>
                {p.name}
              </span>
              <span style={{ fontSize: '10px', color: C.textDim }}>
                {pts.toFixed(1)}
              </span>
              {isSelected && (
                <span style={{ marginLeft: '8px', fontSize: '10px', color }}>✓</span>
              )}
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

export default function TradeAnalyzer() {
  // Trade selection state
  const [givingIds,    setGivingIds]    = useState(new Map()); // espnId → player
  const [receivingIds, setReceivingIds] = useState(new Map()); // espnId → player
  const [tradePartner, setTradePartner] = useState('');        // team_id string

  // Simulation results
  const [simResult,   setSimResult]   = useState(null);   // { before, after }
  const [rosResult,   setROSResult]   = useState(null);   // { before, after }
  const [fairness,    setFairness]    = useState(null);
  const [simStatus,   setSimStatus]   = useState('idle'); // 'idle' | 'running' | 'done' | 'error'

  const myRoster    = MY_ROSTER ?? [];
  const leagueSize  = LEAGUE?.team_count ?? 12;
  const fetchedAt   = LEAGUE_FETCHED_AT;
  const age         = fetchAge(fetchedAt);

  // All teams except mine for partner picker
  const otherTeams = useMemo(() =>
    (ESPN_LEAGUE_DATA?.teams ?? []).filter(t => String(t.team_id) !== String(MY_TEAM?.team_id)),
    []
  );

  // Partner's roster
  const partnerRoster = useMemo(() => {
    if (!tradePartner) return [];
    const raw = ALL_ROSTERS?.[String(tradePartner)] ?? [];
    return raw.map(p => ({
      ...p,
      gsisId:       ESPN_TO_GSIS[String(p.espn_id)] ?? p.espn_id,
      projectedPts: p.projected_points ?? p.avg_points ?? 0,
    }));
  }, [tradePartner]);

  // Toggle selection helpers
  const toggleGiving = useCallback((espnId, player) => {
    setGivingIds(prev => {
      const next = new Map(prev);
      next.has(espnId) ? next.delete(espnId) : next.set(espnId, player);
      return next;
    });
  }, []);

  const toggleReceiving = useCallback((espnId, player) => {
    setReceivingIds(prev => {
      const next = new Map(prev);
      next.has(espnId) ? next.delete(espnId) : next.set(espnId, player);
      return next;
    });
  }, []);

  const givingPlayers    = Array.from(givingIds.values());
  const receivingPlayers = Array.from(receivingIds.values());
  const canAnalyze = givingPlayers.length > 0 && receivingPlayers.length > 0;

  // ---------------------------------------------------------------------------
  // Run analysis
  // ---------------------------------------------------------------------------

  const runAnalysis = useCallback(async () => {
    if (!canAnalyze) return;
    setSimStatus('running');
    setSimResult(null);
    setROSResult(null);
    setFairness(null);

    try {
      // 1. Fairness rating (instant — no sim needed)
      const score = computeFairnessRating(givingPlayers, receivingPlayers, leagueSize);
      setFairness(score);

      // 2. Build pre-trade and post-trade rosters
      const givingEspnIds     = givingPlayers.map(p => p.espn_id);
      const postTradeRoster   = applyTrade(myRoster, givingEspnIds, receivingPlayers);

      // 3. ROS value (sync — no simulation needed)
      const rosBefore = computeROSValue(myRoster,       MY_TEAM?.team_id, leagueSize);
      const rosAfter  = computeROSValue(postTradeRoster, MY_TEAM?.team_id, leagueSize);
      setROSResult({ before: rosBefore, after: rosAfter });

      // 4. Win probability delta — run sim with current matchup opponent
      const oppTeamId = MATCHUP?.opp_team_id;
      if (oppTeamId && ESPN_LEAGUE_DATA?.rosters?.[String(oppTeamId)]) {
        // Before trade
        const beforeRoster = normalizeRoster(myRoster);
        const beforeResult = await simulateMatchup(
          beforeRoster,
          oppTeamId,
          ESPN_LEAGUE_DATA,
          ESPN_TO_GSIS,
          {},
          { leagueSize },
        );

        // After trade — run optimal lineup from post-trade roster
        const afterRoster  = normalizeRoster(postTradeRoster);
        const afterOptimal = getOptimalLineup(afterRoster, {}, leagueSize)
          .map(p => ({ ...p, onBench: false, onIR: false }));

        const afterResult  = await simulateMatchup(
          afterRoster,
          oppTeamId,
          ESPN_LEAGUE_DATA,
          ESPN_TO_GSIS,
          {},
          { leagueSize },
        );

        setSimResult({
          before: beforeResult.winProbability,
          after:  afterResult.winProbability,
        });
      } else {
        // No matchup data — skip win prob, show ROS only
        setSimResult(null);
      }

      setSimStatus('done');
    } catch (err) {
      console.error('Trade analysis failed:', err);
      setSimStatus('error');
    }
  }, [canAnalyze, givingPlayers, receivingPlayers, myRoster, leagueSize]);

  const resetTrade = useCallback(() => {
    setGivingIds(new Map());
    setReceivingIds(new Map());
    setTradePartner('');
    setSimResult(null);
    setROSResult(null);
    setFairness(null);
    setSimStatus('idle');
  }, []);

  // No data guard
  if (myRoster.length === 0) {
    return (
      <div style={{ minHeight: '100vh', background: C.bg, color: C.text, fontFamily: font,
                    display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: '13px', color: C.textDim, marginBottom: '8px' }}>No roster data</div>
          <div style={{ fontSize: '11px', color: C.textDim }}>
            Run <code style={{ color: C.accent }}>python3 scripts/fetch_espn_roster.py</code>
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
        @keyframes spin   { to { transform: rotate(360deg); } }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: ${C.bg}; }
        ::-webkit-scrollbar { width: 4px; } ::-webkit-scrollbar-track { background: ${C.bg}; } ::-webkit-scrollbar-thumb { background: ${C.border}; border-radius: 2px; }
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
            Trade Analyzer
          </span>
          <span style={{ marginLeft: 'auto', fontSize: '10px', color: C.textDim }}>
            {MY_TEAM?.team_name ?? ''}
            {age && <span style={{ marginLeft: '10px' }}>updated {age}</span>}
          </span>
        </header>

        <div style={{ maxWidth: '900px', margin: '0 auto', padding: '28px 40px 100px' }}>

          {/* Trade partner picker */}
          <div style={{ marginBottom: '24px' }}>
            <div style={{ fontSize: '9px', letterSpacing: '0.18em', textTransform: 'uppercase', color: C.textDim, marginBottom: '8px' }}>
              Trade partner
            </div>
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
              {otherTeams.map(t => (
                <button
                  key={t.team_id}
                  onClick={() => { setTradePartner(String(t.team_id)); setReceivingIds(new Map()); }}
                  style={{
                    background:    String(tradePartner) === String(t.team_id) ? C.accent : C.surface,
                    color:         String(tradePartner) === String(t.team_id) ? '#0a0c0f' : C.textMid,
                    border:        `1px solid ${String(tradePartner) === String(t.team_id) ? C.accent : C.border}`,
                    borderRadius:  '4px',
                    padding:       '6px 12px',
                    fontSize:      '10px',
                    cursor:        'pointer',
                    fontFamily:    font,
                    transition:    'all 0.15s',
                    letterSpacing: '0.06em',
                  }}
                >
                  {t.team_name}
                </button>
              ))}
            </div>
          </div>

          {/* Roster pickers */}
          <div style={{ display: 'flex', gap: '20px', marginBottom: '24px' }}>
            <PlayerPicker
              label={`You give (${givingPlayers.length} selected)`}
              color={C.red}
              players={myRoster.filter(p => !p.on_ir).map(p => ({
                ...p,
                projected_points: p.projected_points ?? p.avg_points ?? 0,
              }))}
              selected={givingIds}
              onToggle={toggleGiving}
            />

            {/* Arrow */}
            <div style={{ display: 'flex', alignItems: 'center', paddingTop: '32px', flexShrink: 0 }}>
              <span style={{ fontSize: '20px', color: C.borderMid }}>⇄</span>
            </div>

            <PlayerPicker
              label={tradePartner
                ? `You receive (${receivingPlayers.length} selected)`
                : 'Select a trade partner first'}
              color={C.green}
              players={partnerRoster.filter(p => !p.on_ir)}
              selected={receivingIds}
              onToggle={tradePartner ? toggleReceiving : () => {}}
            />
          </div>

          {/* Selected players summary */}
          {canAnalyze && (
            <div style={{
              display:      'flex',
              gap:          '16px',
              padding:      '14px 18px',
              background:   C.surface,
              border:       `1px solid ${C.border}`,
              borderRadius: '5px',
              marginBottom: '20px',
              alignItems:   'center',
              animation:    'fadeIn 0.2s ease',
            }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: '9px', color: C.red, letterSpacing: '0.12em', textTransform: 'uppercase', marginBottom: '5px' }}>
                  You give
                </div>
                {givingPlayers.map(p => (
                  <span key={p.espn_id} style={{ display: 'inline-flex', alignItems: 'center', marginRight: '10px', marginBottom: '4px' }}>
                    <PosTag pos={p.position} />
                    <span style={{ fontSize: '11px', color: C.text }}>{p.name}</span>
                  </span>
                ))}
              </div>
              <span style={{ fontSize: '16px', color: C.borderMid, flexShrink: 0 }}>⇄</span>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: '9px', color: C.green, letterSpacing: '0.12em', textTransform: 'uppercase', marginBottom: '5px' }}>
                  You receive
                </div>
                {receivingPlayers.map(p => (
                  <span key={p.espn_id} style={{ display: 'inline-flex', alignItems: 'center', marginRight: '10px', marginBottom: '4px' }}>
                    <PosTag pos={p.position} />
                    <span style={{ fontSize: '11px', color: C.text }}>{p.name}</span>
                  </span>
                ))}
              </div>
              <button
                onClick={runAnalysis}
                disabled={simStatus === 'running'}
                style={{
                  background:    simStatus === 'running' ? C.surface : C.accent,
                  color:         simStatus === 'running' ? C.textDim : '#0a0c0f',
                  border:        'none',
                  borderRadius:  '5px',
                  padding:       '10px 20px',
                  fontSize:      '11px',
                  fontWeight:    '700',
                  letterSpacing: '0.12em',
                  textTransform: 'uppercase',
                  cursor:        simStatus === 'running' ? 'not-allowed' : 'pointer',
                  fontFamily:    font,
                  flexShrink:    0,
                  transition:    'all 0.15s',
                }}
              >
                {simStatus === 'running' ? 'Analyzing…' : 'Analyze'}
              </button>
            </div>
          )}

          {/* Results */}
          {simStatus === 'running' && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px', padding: '20px', color: C.textDim, fontSize: '11px' }}>
              <div style={{
                width: '14px', height: '14px',
                border: `2px solid ${C.border}`,
                borderTopColor: C.accent,
                borderRadius: '50%',
                animation: 'spin 0.7s linear infinite',
              }} />
              Running simulations…
            </div>
          )}

          {simStatus === 'done' && (
            <div style={{ animation: 'fadeIn 0.3s ease' }}>

              {/* Fairness */}
              <div style={{
                padding:      '24px',
                background:   C.surface,
                border:       `1px solid ${C.border}`,
                borderRadius: '6px',
                marginBottom: '16px',
              }}>
                <div style={{ fontSize: '9px', letterSpacing: '0.18em', textTransform: 'uppercase', color: C.textDim, marginBottom: '16px' }}>
                  Trade fairness
                </div>
                <FairnessGauge score={fairness} />
              </div>

              {/* Win probability delta */}
              {simResult && (
                <div style={{
                  display:      'flex',
                  gap:          '32px',
                  padding:      '24px',
                  background:   C.surface,
                  border:       `1px solid ${C.border}`,
                  borderRadius: '6px',
                  marginBottom: '16px',
                }}>
                  <WinDeltaDisplay
                    before={simResult.before}
                    after={simResult.after}
                    label="Win prob this week (after trade)"
                  />
                  <div style={{ width: '1px', background: C.border, alignSelf: 'stretch' }} />
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: '9px', letterSpacing: '0.16em', color: C.textDim, textTransform: 'uppercase', marginBottom: '8px' }}>
                      Context
                    </div>
                    <div style={{ fontSize: '11px', color: C.textMid, lineHeight: 1.7 }}>
                      {simResult.after > simResult.before + 2 && 'This trade improves your win probability this week.'}
                      {simResult.after < simResult.before - 2 && 'This trade hurts your win probability this week.'}
                      {Math.abs(simResult.after - simResult.before) <= 2 && 'Minimal win probability impact this week.'}
                    </div>
                    <div style={{ fontSize: '10px', color: C.textDim, marginTop: '8px' }}>
                      10,000 simulations · real opponent roster
                    </div>
                  </div>
                </div>
              )}

              {/* ROS value */}
              {rosResult && (
                <div style={{ marginBottom: '16px' }}>
                  <div style={{ fontSize: '9px', letterSpacing: '0.18em', textTransform: 'uppercase', color: C.textDim, marginBottom: '10px' }}>
                    Rest-of-season value
                  </div>
                  <ROSComparison before={rosResult.before} after={rosResult.after} />
                  <div style={{ fontSize: '10px', color: C.textDim, marginTop: '8px' }}>
                    Based on optimal lineup projection × {rosResult.after?.remainingWeeks ?? '—'} remaining weeks
                  </div>
                </div>
              )}

              {/* Reset */}
              <button
                onClick={resetTrade}
                style={{
                  background:    'transparent',
                  color:         C.textDim,
                  border:        `1px solid ${C.border}`,
                  borderRadius:  '4px',
                  padding:       '7px 14px',
                  fontSize:      '10px',
                  letterSpacing: '0.10em',
                  textTransform: 'uppercase',
                  cursor:        'pointer',
                  fontFamily:    font,
                  marginTop:     '8px',
                }}
              >
                Reset
              </button>
            </div>
          )}

          {simStatus === 'error' && (
            <div style={{ padding: '16px', color: C.red, fontSize: '11px', background: C.red + '10', borderRadius: '4px' }}>
              Analysis failed. Check console for details.
            </div>
          )}

        </div>
      </div>
    </>
  );
}
