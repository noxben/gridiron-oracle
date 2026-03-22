// LineupOptimizer.jsx — Gridiron Oracle
// Main view: optimal lineup, win probability, score distribution, override sliders
// Step 6 per spec §7.2

import { useState, useEffect, useCallback, useRef } from 'react';
import { MY_ROSTER, MY_TEAM, MATCHUP, LEAGUE } from '../utils/espn_data.js';
import { ESPN_LEAGUE_DATA } from '../utils/espn_league.js';
import { ESPN_TO_GSIS } from '../utils/id_mapping.js';
import {
  prepareLineup,
  runSimulation,
  simulateMatchup,
  getOptimalLineup,
  rerunWithOverrides,
} from '../utils/simulator.js';
import { hasWeatherImpact, getWeatherAdvisory } from '../utils/weather_data.js';

// ---------------------------------------------------------------------------
// Design: dark analytical dashboard — think Bloomberg terminal meets
// a football war room. Monochrome base, chartreuse accent, data-dense
// but never cluttered. Every number earns its place.
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
  QB:  '#5a9ff0', RB: '#50c878', WR: '#c090f0',
  TE:  '#f0b840', K:  '#808080', DST: '#e06060', FLEX: '#c090f0',
};

const font  = '"DM Mono", "Fira Mono", "Consolas", monospace';
const serif = '"DM Serif Display", "Georgia", serif';

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function clamp(v, min, max) { return Math.max(min, Math.min(max, v)); }

function winColor(pct) {
  if (pct >= 65) return C.green;
  if (pct >= 50) return C.accent;
  if (pct >= 35) return C.amber;
  return C.red;
}

function defRankLabel(rank) {
  if (!rank) return '—';
  if (rank <= 5)  return `#${rank} (tough)`;
  if (rank >= 28) return `#${rank} (easy)`;
  return `#${rank}`;
}

function defRankColor(rank) {
  if (!rank) return C.textDim;
  if (rank <= 5)  return C.red;
  if (rank <= 12) return C.amber;
  if (rank >= 28) return C.green;
  if (rank >= 22) return '#90d060';
  return C.textMid;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function PosTag({ pos }) {
  const color = POS_COLOR[pos] ?? C.textMid;
  return (
    <span style={{
      display: 'inline-block',
      padding: '1px 5px',
      borderRadius: '3px',
      fontSize: '9px',
      fontWeight: '700',
      letterSpacing: '0.10em',
      background: color + '18',
      color,
      marginRight: '8px',
      minWidth: '28px',
      textAlign: 'center',
    }}>{pos}</span>
  );
}

function InjuryDot({ prob }) {
  if (prob >= 1.0) return null;
  const color = prob === 0 ? C.red : prob <= 0.55 ? C.amber : '#d0c030';
  return (
    <span style={{
      display: 'inline-block',
      width: '6px', height: '6px',
      borderRadius: '50%',
      background: color,
      marginLeft: '6px',
      verticalAlign: 'middle',
      flexShrink: 0,
    }} title={`Play probability: ${Math.round(prob * 100)}%`} />
  );
}

function ScoreDist({ dist, label, color }) {
  if (!dist) return null;
  return (
    <div style={{ flex: 1 }}>
      <div style={{ fontSize: '9px', letterSpacing: '0.16em', color: C.textDim, textTransform: 'uppercase', marginBottom: '10px' }}>
        {label}
      </div>
      <div style={{ fontSize: '28px', fontFamily: serif, color, marginBottom: '4px' }}>
        {dist.p50?.toFixed(1)}
      </div>
      <div style={{ display: 'flex', gap: '12px', fontSize: '11px', color: C.textMid }}>
        <span title="10th percentile">↓ {dist.p10?.toFixed(1)}</span>
        <span title="90th percentile">↑ {dist.p90?.toFixed(1)}</span>
      </div>
    </div>
  );
}

function WinGauge({ pct }) {
  const color = winColor(pct);
  const r = 44, stroke = 6;
  const circ = 2 * Math.PI * r;
  const dash = (pct / 100) * circ;

  return (
    <div style={{ position: 'relative', width: '108px', height: '108px', flexShrink: 0 }}>
      <svg width="108" height="108" viewBox="0 0 108 108">
        <circle cx="54" cy="54" r={r} fill="none" stroke={C.border} strokeWidth={stroke} />
        <circle
          cx="54" cy="54" r={r}
          fill="none"
          stroke={color}
          strokeWidth={stroke}
          strokeDasharray={`${dash} ${circ - dash}`}
          strokeLinecap="round"
          transform="rotate(-90 54 54)"
          style={{ transition: 'stroke-dasharray 0.8s cubic-bezier(0.4,0,0.2,1), stroke 0.4s' }}
        />
      </svg>
      <div style={{
        position: 'absolute', inset: 0,
        display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center',
      }}>
        <div style={{ fontSize: '22px', fontFamily: serif, color, lineHeight: 1 }}>
          {pct?.toFixed(0)}%
        </div>
        <div style={{ fontSize: '9px', letterSpacing: '0.14em', color: C.textDim, textTransform: 'uppercase', marginTop: '2px' }}>
          win
        </div>
      </div>
    </div>
  );
}

function ProgressBar({ value, max, color }) {
  const pct = max > 0 ? clamp((value / max) * 100, 0, 100) : 0;
  return (
    <div style={{ height: '2px', background: C.border, borderRadius: '1px', overflow: 'hidden' }}>
      <div style={{
        height: '100%',
        width: `${pct}%`,
        background: color,
        borderRadius: '1px',
        transition: 'width 0.4s ease',
      }} />
    </div>
  );
}

function OverrideSlider({ value, onChange }) {
  const marks = [-150, -100, -50, 0, 50, 100, 150];
  return (
    <div style={{ paddingTop: '6px' }}>
      <input
        type="range"
        min="-150" max="150" step="50"
        value={value}
        onChange={e => onChange(Number(e.target.value))}
        style={{
          width: '100%',
          accentColor: value === 0 ? C.textDim : value > 0 ? C.accent : C.red,
          cursor: 'pointer',
          height: '2px',
        }}
      />
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '2px' }}>
        {marks.map(m => (
          <span key={m} style={{
            fontSize: '8px',
            color: m === value ? (value > 0 ? C.accent : value < 0 ? C.red : C.textMid) : C.textDim,
            fontWeight: m === value ? '700' : '400',
            transition: 'color 0.2s',
          }}>
            {m > 0 ? `+${m}` : m}
          </span>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Player row
// ---------------------------------------------------------------------------

function PlayerRow({ player, override, onOverride, rank, isVarianceKing }) {
  const [expanded, setExpanded] = useState(false);

  const projColor = player.projectedPts > 20 ? C.accent
    : player.projectedPts > 12 ? C.text
    : C.textMid;

  return (
    <>
      <tr
        onClick={() => setExpanded(e => !e)}
        style={{
          borderBottom: `1px solid ${C.border}`,
          cursor: 'pointer',
          background: expanded ? C.surface : 'transparent',
          transition: 'background 0.15s',
        }}
      >
        {/* Rank */}
        <td style={{ padding: '11px 0', width: '24px' }}>
          <span style={{ fontSize: '10px', color: C.textDim }}>{rank}</span>
        </td>

        {/* Player name + pos */}
        <td style={{ padding: '11px 8px 11px 0' }}>
          <div style={{ display: 'flex', alignItems: 'center' }}>
            <PosTag pos={player.position} />
            <span style={{ fontSize: '13px', color: C.text }}>{player.name}</span>
            <InjuryDot prob={player.play_probability ?? 1} />
            {isVarianceKing && (
              <span style={{
                marginLeft: '8px', fontSize: '8px',
                color: C.accentDim, letterSpacing: '0.12em',
                textTransform: 'uppercase',
              }}>⚡ key</span>
            )}

			{hasWeatherImpact(player.team) && (
			  <span title={getWeatherAdvisory(player.team).join(' · ')}
					style={{ marginLeft: '6px', fontSize: '10px' }}>🌬</span>
			)}
          </div>
          <div style={{ fontSize: '10px', color: C.textDim, marginTop: '2px', paddingLeft: '36px' }}>
            {player.team}
            {player.opp_def_rank && (
              <span style={{ marginLeft: '8px', color: defRankColor(player.opp_def_rank) }}>
                vs DEF {defRankLabel(player.opp_def_rank)}
              </span>
            )}
          </div>
        </td>

        {/* Slot */}
        <td style={{ padding: '11px 12px 11px 0', width: '44px' }}>
          <span style={{ fontSize: '9px', color: C.textDim, letterSpacing: '0.10em' }}>
            {player.lineupSlot}
          </span>
        </td>

        {/* Projected pts */}
        <td style={{ padding: '11px 0', width: '52px', textAlign: 'right' }}>
          <span style={{ fontSize: '14px', fontFamily: serif, color: projColor }}>
            {player.projectedPts?.toFixed(1)}
          </span>
        </td>

        {/* VORP */}
        <td style={{ padding: '11px 0 11px 12px', width: '48px', textAlign: 'right' }}>
          <span style={{
            fontSize: '11px',
            color: (player.vorp ?? 0) >= 0 ? C.green : C.red,
          }}>
            {(player.vorp ?? 0) >= 0 ? '+' : ''}{player.vorp?.toFixed(1)}
          </span>
        </td>

        {/* Override indicator */}
        <td style={{ padding: '11px 0 11px 12px', width: '32px', textAlign: 'right' }}>
          {override !== 0 && (
            <span style={{
              fontSize: '9px',
              color: override > 0 ? C.accent : C.red,
              fontWeight: '700',
            }}>
              {override > 0 ? `+${override}` : override}
            </span>
          )}
        </td>
      </tr>

      {/* Expanded detail row */}
      {expanded && (
        <tr style={{ background: C.surface }}>
          <td colSpan={6} style={{ padding: '0 0 16px 32px' }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '20px', paddingTop: '12px' }}>

              {/* Rating breakdown */}
              <div>
                <div style={{ fontSize: '9px', letterSpacing: '0.14em', color: C.textDim, textTransform: 'uppercase', marginBottom: '10px' }}>
                  Rating breakdown
                </div>
                {[
                  { label: 'EPA / play', val: player.scores?.epa },
                  { label: 'Usage', val: player.scores?.usage },
                  { label: 'Snap %', val: player.scores?.snap },
                  { label: 'Red zone', val: player.scores?.redZone },
                ].map(({ label, val }) => (
                  <div key={label} style={{ marginBottom: '7px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '3px' }}>
                      <span style={{ fontSize: '10px', color: C.textMid }}>{label}</span>
                      <span style={{ fontSize: '10px', color: C.text }}>{val?.toFixed(0)}</span>
                    </div>
                    <ProgressBar value={val ?? 0} max={100} color={C.accent} />
                  </div>
                ))}
                <div style={{ marginTop: '10px', display: 'flex', justifyContent: 'space-between' }}>
                  <span style={{ fontSize: '10px', color: C.textMid }}>Composite</span>
                  <span style={{ fontSize: '12px', color: C.accent, fontWeight: '600' }}>
                    {player.compositeRating?.toFixed(0)}
                  </span>
                </div>
              </div>

              {/* Override */}
              <div>
                <div style={{ fontSize: '9px', letterSpacing: '0.14em', color: C.textDim, textTransform: 'uppercase', marginBottom: '10px' }}>
                  Eye-test override
                </div>
                <div style={{ fontSize: '11px', color: C.textMid, marginBottom: '8px', lineHeight: 1.5 }}>
                  {override === 0
                    ? 'Trusting the model'
                    : override > 0
                      ? `+${override} → ~+${(override / 100 * 3).toFixed(1)} pts projected`
                      : `${override} → ~${(override / 100 * 3).toFixed(1)} pts projected`
                  }
                </div>
                <OverrideSlider value={override} onChange={onOverride} />

                {player.injuryDetail && (
                  <div style={{
                    marginTop: '12px', fontSize: '10px',
                    color: C.amber, letterSpacing: '0.08em',
                  }}>
                    ⚠ {player.injuryDetail} ({Math.round((player.play_probability ?? 1) * 100)}% to play)
                  </div>
                )}
                {player.varianceProfile && player.varianceProfile !== 'standard' && (
                  <div style={{ marginTop: '6px', fontSize: '10px', color: C.textDim }}>
                    Profile: {player.varianceProfile} ({player.varianceMult?.toFixed(2)}× SD)
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
// Bench row (condensed)
// ---------------------------------------------------------------------------

function BenchRow({ player }) {
  return (
    <tr style={{ borderBottom: `1px solid ${C.border}`, opacity: 0.55 }}>
      <td style={{ padding: '8px 0', width: '24px' }}>
        <span style={{ fontSize: '9px', color: C.textDim }}>—</span>
      </td>
      <td style={{ padding: '8px 8px 8px 0' }}>
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <PosTag pos={player.position} />
          <span style={{ fontSize: '12px', color: C.textMid }}>{player.name}</span>
          <InjuryDot prob={player.play_probability ?? 1} />
        </div>
      </td>
      <td style={{ padding: '8px 12px 8px 0' }}>
        <span style={{ fontSize: '9px', color: C.textDim }}>BENCH</span>
      </td>
      <td style={{ padding: '8px 0', textAlign: 'right' }}>
        <span style={{ fontSize: '12px', color: C.textDim }}>
          {player.projectedPts?.toFixed(1)}
        </span>
      </td>
      <td colSpan={2} />
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Simulation status bar
// ---------------------------------------------------------------------------

function SimStatus({ status, progress, elapsedMs }) {
  if (status === 'idle') return null;
  if (status === 'running') {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 0', fontSize: '11px', color: C.textMid }}>
        <div style={{
          width: '12px', height: '12px',
          border: `2px solid ${C.border}`,
          borderTopColor: C.accent,
          borderRadius: '50%',
          animation: 'spin 0.7s linear infinite',
          flexShrink: 0,
        }} />
        <span>Running {(10000).toLocaleString()} simulations...</span>
        <span style={{ color: C.textDim }}>{Math.round(progress * 100)}%</span>
      </div>
    );
  }
  if (status === 'done') {
    return (
      <div style={{ fontSize: '10px', color: C.textDim, padding: '4px 0' }}>
        {(10000).toLocaleString()} sims · {elapsedMs}ms
      </div>
    );
  }
  return null;
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function LineupOptimizer() {
  const [result,    setResult]    = useState(null);
  const [simStatus, setSimStatus] = useState('idle');
  const [progress,  setProgress]  = useState(0);
  const [overrides, setOverrides] = useState({});
  const [lineup,    setLineup]    = useState([]);
  const [bench,     setBench]     = useState([]);
  const debounceRef = useRef(null);

  const leagueSize  = LEAGUE?.team_count ?? 12;
  const matchup     = MATCHUP;

  // Build initial lineup on mount
  useEffect(() => {
  console.log('MY_ROSTER:', MY_ROSTER?.map(p => `${p.name} | on_bench:${p.on_bench} | slot:${p.lineup_slot}`));
  if (!MY_ROSTER || MY_ROSTER.length === 0) return;

  const starters = MY_ROSTER
    .filter(p => !p.on_bench && !p.on_ir)
    .map(p => ({
      ...p,
      gsisId:          p.espn_id,
      projectedPts:    p.projected_points ?? p.avg_points ?? 0,
      play_probability: p.play_probability ?? 1.0,
      compositeRating: 50,
      vorp:            0,
      varianceMult:    1.0,
      varianceProfile: 'standard',
      scores:          { epa: 50, usage: 50, snap: 50, redZone: 50 },
      opp_def_rank:    16,
      lineupSlot:      p.lineup_slot,
    }));

  const benchPlayers = MY_ROSTER
    .filter(p => p.on_bench || p.on_ir)
    .map(p => ({
      ...p,
      gsisId:          p.espn_id,
      projectedPts:    p.projected_points ?? p.avg_points ?? 0,
      play_probability: p.play_probability ?? 1.0,
    }));

  setLineup(starters);
  setBench(benchPlayers);
  runFallbackSim(starters);
}, []);

  // Fallback: build lineup from ESPN data alone (before nfl_data.js is populated)
  const buildFallbackLineup = useCallback(() => {
    if (!MY_ROSTER) return;

    const slotOrder = ['QB', 'RB', 'WR', 'TE', 'FLEX', 'K', 'DST'];
    const starters  = MY_ROSTER
      .filter(p => !p.on_bench && !p.on_ir)
      .map(p => ({
        gsisId:          p.espn_id,
        espn_id:         p.espn_id,
        name:            p.name,
        position:        p.position,
        team:            p.team,
        lineupSlot:      p.lineup_slot,
        projectedPts:    p.projected_points ?? p.avg_points ?? 0,
        play_probability: p.play_probability ?? 1.0,
        injuryDetail:    p.injury_detail ?? '',
        compositeRating: 50,
        vorp:            0,
        varianceMult:    1.0,
        varianceProfile: 'standard',
        scores:          { epa: 50, usage: 50, snap: 50, redZone: 50 },
        opp_def_rank:    16,
      }))
      .sort((a, b) => slotOrder.indexOf(a.lineupSlot) - slotOrder.indexOf(b.lineupSlot));

    const benchPlayers = MY_ROSTER
      .filter(p => p.on_bench || p.on_ir)
      .map(p => ({
        ...p,
        gsisId:          p.espn_id,
        projectedPts:    p.projected_points ?? p.avg_points ?? 0,
        play_probability: p.play_probability ?? 1.0,
      }));

    setLineup(starters);
    setBench(benchPlayers);
    runFallbackSim(starters);
  }, []);

  // Run simulation with current lineup + overrides
  // v2.0: uses real opponent roster from espn_league.js via simulateMatchup
  const runSim = useCallback(async (currentLineup, currentOverrides) => {
    if (!currentLineup || currentLineup.length === 0) return;

    setSimStatus('running');
    setProgress(0);

    try {
      const oppTeamId = matchup?.opp_team_id;

      // v2.0 path: real opponent roster available
      if (oppTeamId && ESPN_LEAGUE_DATA?.rosters?.[String(oppTeamId)]) {
        const myRoster = currentLineup.map(p => ({
          ...p,
          gsisId:           p.gsisId ?? p.espn_id,
          projectedPts:     p.projectedPts ?? p.projected_points ?? p.avg_points ?? 5,
          play_probability: p.play_probability ?? 1.0,
          varianceMult:     p.varianceMult ?? 1.0,
          position:         p.position ?? 'WR',
          onBench:          false,
          onIR:             false,
        }));

        const simResult = await simulateMatchup(
          myRoster,
          oppTeamId,
          ESPN_LEAGUE_DATA,
          ESPN_TO_GSIS,
          currentOverrides ?? {},
          { leagueSize, onProgress: setProgress },
        );
        setResult(simResult);
        setSimStatus('done');
        return;
      }

      // Fallback: espn_league.js not loaded or opponent not found — use projected total
      console.warn('[LineupOptimizer] espn_league.js not available — falling back to synthetic opponent');
      const oppProjected = matchup?.opp_projected ?? 100;
      const oppLineup    = buildSyntheticOpponent(oppProjected);

      const prepared = currentLineup.map(p => ({
        ...p,
        projectedPts:     p.projectedPts ?? p.projected_points ?? p.avg_points ?? 5,
        play_probability: p.play_probability ?? 1.0,
        varianceMult:     p.varianceMult ?? 1.0,
        position:         p.position ?? 'WR',
      }));

      const simResult = await runSimulation(prepared, oppLineup, {
        leagueSize,
        onProgress: setProgress,
      });
      setResult(simResult);
      setSimStatus('done');
    } catch (err) {
      console.error('Simulation failed:', err);
      setSimStatus('idle');
    }
  }, [leagueSize, matchup]);

  // Fallback sim — called on mount before nfl_data.js composite ratings are applied
  // Still uses simulateMatchup for the opponent if espn_league.js is available
  const runFallbackSim = useCallback(async (starters) => {
    setSimStatus('running');
    setProgress(0);
    try {
      const oppTeamId = matchup?.opp_team_id;

      if (oppTeamId && ESPN_LEAGUE_DATA?.rosters?.[String(oppTeamId)]) {
        const myRoster = starters.map(p => ({
          ...p,
          gsisId:           p.gsisId ?? p.espn_id,
          onBench:          false,
          onIR:             false,
        }));
        const simResult = await simulateMatchup(
          myRoster,
          oppTeamId,
          ESPN_LEAGUE_DATA,
          ESPN_TO_GSIS,
          {},
          { leagueSize, onProgress: setProgress },
        );
        setResult(simResult);
        setSimStatus('done');
        return;
      }

      // Pure fallback — no league data
      const oppProjected = matchup?.opp_projected ?? 100;
      const oppLineup    = buildSyntheticOpponent(oppProjected);
      const simResult    = await runSimulation(starters, oppLineup, {
        leagueSize,
        onProgress: setProgress,
      });
      setResult(simResult);
      setSimStatus('done');
    } catch (err) {
      console.error('Fallback sim failed:', err);
      setSimStatus('idle');
    }
  }, [leagueSize, matchup]);

  // Override change — debounce re-sim by 600ms
  const handleOverride = useCallback((gsisId, value) => {
    const next = { ...overrides, [gsisId]: value };
    setOverrides(next);

    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      runSim(lineup, next);
    }, 600);
  }, [overrides, lineup, runSim]);

  // Highest-variance player
  const varianceKingId = result?.highestVariancePlayer?.gsisId;

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Serif+Display&display=swap');
        @keyframes spin { to { transform: rotate(360deg); } }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: ${C.bg}; }
        input[type=range] { -webkit-appearance: none; appearance: none; background: transparent; }
        input[type=range]::-webkit-slider-runnable-track { height: 2px; background: ${C.border}; border-radius: 1px; }
        input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; width: 12px; height: 12px; border-radius: 50%; background: currentColor; margin-top: -5px; cursor: pointer; }
        tr { animation: fadeIn 0.2s ease both; }
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

          <span style={{ color: C.border }}>|</span>
          <span style={{ fontSize: '11px', letterSpacing: '0.18em', textTransform: 'uppercase', color: C.accent }}>
            Gridiron Oracle
          </span>
          <span style={{ fontSize: '11px', color: C.textDim, letterSpacing: '0.10em' }}>
            Lineup Optimizer
          </span>
          {matchup && (
            <span style={{ marginLeft: 'auto', fontSize: '11px', color: C.textDim }}>
              Week {matchup.week} · vs {matchup.opp_team_name}
            </span>
          )}
        </header>

        <div style={{ maxWidth: '860px', margin: '0 auto', padding: '40px 40px' }}>

          {/* Win probability + score distribution */}
          <div style={{
            display: 'flex',
            alignItems: 'center',
            gap: '40px',
            padding: '28px 32px',
            background: C.surface,
            border: `1px solid ${C.border}`,
            borderRadius: '6px',
            marginBottom: '32px',
          }}>
            <WinGauge pct={result?.winProbability ?? 50} />

            <div style={{ flex: 1, display: 'flex', gap: '32px' }}>
              <ScoreDist
                dist={result?.myScore}
				label={MY_TEAM?.team_name ?? 'My team'}                
				color={C.accent}
              />
              <div style={{ width: '1px', background: C.border, alignSelf: 'stretch' }} />
              <ScoreDist
                dist={result?.oppScore}
                label={matchup?.opp_team_name ?? 'Opponent'}
                color={C.textMid}
              />
            </div>

            <div style={{ textAlign: 'right' }}>
              <SimStatus
                status={simStatus}
                progress={progress}
                elapsedMs={result?.elapsedMs}
              />
              {result && (
                <div style={{ fontSize: '10px', color: C.textDim, marginTop: '4px' }}>
                  p10 / p50 / p90
                </div>
              )}
              {result?.usedRealOppRoster && (
                <div style={{ fontSize: '9px', color: C.green, marginTop: '3px', letterSpacing: '0.08em' }}>
                  ✓ real opponent roster
                </div>
              )}
            </div>
          </div>

          {/* Lineup table */}
          <div style={{ marginBottom: '8px', display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
            <div style={{ fontSize: '9px', letterSpacing: '0.18em', textTransform: 'uppercase', color: C.textDim }}>
              Starting lineup
            </div>
            <div style={{ fontSize: '10px', color: C.textDim }}>
              Click a player to expand · drag slider to override
            </div>
          </div>

          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ borderBottom: `1px solid ${C.borderMid}` }}>
                <th style={{ ...thStyle, width: '24px' }}>#</th>
                <th style={{ ...thStyle }}>Player</th>
                <th style={{ ...thStyle, width: '44px' }}>Slot</th>
                <th style={{ ...thStyle, textAlign: 'right', width: '52px' }}>Proj</th>
                <th style={{ ...thStyle, textAlign: 'right', width: '48px' }}>VORP</th>
                <th style={{ ...thStyle, textAlign: 'right', width: '32px' }}>Ovr</th>
              </tr>
            </thead>
            <tbody>
              {lineup.map((player, i) => (
                <PlayerRow
                  key={player.gsisId ?? player.espn_id ?? i}
                  player={player}
                  override={overrides[player.gsisId] ?? 0}
                  onOverride={(val) => handleOverride(player.gsisId, val)}
                  rank={i + 1}
                  isVarianceKing={player.gsisId === varianceKingId}
                />
              ))}
            </tbody>
          </table>

          {/* Bench */}
          {bench.length > 0 && (
            <>
              <div style={{
                fontSize: '9px', letterSpacing: '0.18em',
                textTransform: 'uppercase', color: C.textDim,
                margin: '24px 0 8px',
              }}>
                Bench
              </div>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <tbody>
                  {bench.map((player, i) => (
                    <BenchRow
                      key={player.espn_id ?? i}
                      player={player}
                    />
                  ))}
                </tbody>
              </table>
            </>
          )}

          {/* Variance callout */}
          {result?.highestVariancePlayer && (
            <div style={{
              marginTop: '32px',
              padding: '14px 18px',
              background: C.surface,
              border: `1px solid ${C.accentDim}`,
              borderRadius: '4px',
              display: 'flex',
              gap: '12px',
              alignItems: 'flex-start',
            }}>
              <span style={{ color: C.accent, fontSize: '14px', flexShrink: 0 }}>⚡</span>
              <div>
                <div style={{ fontSize: '11px', color: C.accent, marginBottom: '3px', letterSpacing: '0.08em' }}>
                  Key variance player
                </div>
                <div style={{ fontSize: '12px', color: C.textMid, lineHeight: 1.5 }}>
                  <strong style={{ color: C.text }}>{result.highestVariancePlayer.name}</strong>
                  {' '}has the highest boom/bust impact on your win probability this week.
                  Profile: {result.highestVariancePlayer.varianceProfile}.
                </div>
              </div>
            </div>
          )}

        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const thStyle = {
  fontSize: '9px',
  letterSpacing: '0.16em',
  textTransform: 'uppercase',
  color: '#6a7585',   // was #2e3540
  textAlign: 'left',
  padding: '8px 0',
  fontWeight: '400',
  fontFamily: '"DM Mono", monospace',
};

/**
 * Synthetic opponent fallback — used only when espn_league.js is unavailable.
 * Distributes a projected total across positions using historical weights.
 * v2.0: this should rarely fire — simulateMatchup uses the real roster instead.
 */
function buildSyntheticOpponent(totalProjected) {
  const positions = ['QB', 'RB', 'RB', 'WR', 'WR', 'TE', 'FLEX', 'K', 'DST'];
  const weights   = [0.18, 0.12, 0.10, 0.12, 0.11, 0.09, 0.10, 0.05, 0.08];
  const replacement = { QB: 15, RB: 8, WR: 9, TE: 6, FLEX: 9, K: 7, DST: 7 };

  return positions.map((pos, i) => ({
    gsisId:           `opp_${pos}_${i}`,
    name:             `Opp ${pos}`,
    position:         pos,
    team:             'OPP',
    lineupSlot:       pos,
    projectedPts:     Math.max(replacement[pos] ?? 8, totalProjected * weights[i]),
    play_probability: 1.0,
    varianceMult:     pos === 'DST' ? 1.3 : 1.0,
    scores:           { epa: 50, usage: 50, snap: 50, redZone: 50 },
    opp_def_rank:     16,
    compositeRating:  50,
    vorp:             0,
  }));
}
