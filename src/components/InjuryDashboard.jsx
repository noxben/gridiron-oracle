// InjuryDashboard.jsx — Gridiron Oracle
// All injury-flagged players across your roster + replacement recommendations
// v2.0 Step 6 per spec §4.3 / §9

import { useMemo } from 'react';
import { MY_ROSTER, MY_TEAM, FETCHED_AT } from '../utils/espn_data.js';
import { WAIVER_POOL, ALL_ROSTERS, LEAGUE_FETCHED_AT } from '../utils/espn_league.js';
import { PLAYERS_BY_POSITION } from '../utils/nfl_data.js';
import { hasWeatherImpact, getWeatherAdvisory } from '../utils/weather_data.js';

// ---------------------------------------------------------------------------
// Design tokens — matches LineupOptimizer / LeagueHome exactly
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

// ---------------------------------------------------------------------------
// Severity helpers
// ---------------------------------------------------------------------------

function severityColor(prob) {
  if (prob === 0.0)  return C.red;
  if (prob <= 0.25)  return C.red;
  if (prob <= 0.55)  return C.amber;
  return '#d0c030';   // questionable / limited
}

function severityLabel(prob) {
  if (prob === 0.0)  return 'OUT';
  if (prob <= 0.25)  return 'DOUBTFUL';
  if (prob <= 0.55)  return 'QUESTIONABLE';
  return 'LIMITED';
}

function severityOrder(prob) {
  // Lower number = more severe = appears first
  if (prob === 0.0)  return 0;
  if (prob <= 0.25)  return 1;
  if (prob <= 0.55)  return 2;
  return 3;
}

function fetchAge(fetchedAt) {
  if (!fetchedAt) return null;
  const mins = Math.round((Date.now() - new Date(fetchedAt).getTime()) / 60000);
  if (mins < 60)   return `${mins}m ago`;
  if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
  return `${Math.round(mins / 1440)}d ago`;
}

// ---------------------------------------------------------------------------
// Build injured roster — union of espn_data.js and nfl_data.js injury fields
// espn_data.js is the authoritative source for lineup/bench status
// nfl_data.js adds EPA-based context for replacement recommendations
// ---------------------------------------------------------------------------

function buildInjuredRoster(myRoster) {
  if (!myRoster) return [];

  return myRoster
    .filter(p => (p.play_probability ?? 1.0) < 1.0)
    .map(p => ({
      espn_id:          p.espn_id,
      name:             p.name,
      position:         p.position,
      team:             p.team,
      play_probability: p.play_probability ?? 1.0,
      injury_detail:    p.injury_detail ?? p.injury_status ?? '',
      on_bench:         p.on_bench ?? false,
      on_ir:            p.on_ir    ?? false,
      lineup_slot:      p.lineup_slot ?? '',
      avg_points:       p.avg_points     ?? p.projected_points ?? 0,
      projected_points: p.projected_points ?? p.avg_points ?? 0,
    }))
    .sort((a, b) => severityOrder(a.play_probability) - severityOrder(b.play_probability));
}

// ---------------------------------------------------------------------------
// Find replacement — best available at same position
// Priority: bench player > waiver pool player
// ---------------------------------------------------------------------------

function findBenchReplacement(position, injuredEspnId, myRoster) {
  if (!myRoster) return null;

  return myRoster
    .filter(p =>
      p.position === position &&
      p.espn_id !== injuredEspnId &&
      (p.on_bench || p.lineup_slot === 'BENCH') &&
      !p.on_ir &&
      (p.play_probability ?? 1.0) > 0.5
    )
    .sort((a, b) => (b.projected_points ?? b.avg_points ?? 0) - (a.projected_points ?? a.avg_points ?? 0))[0] ?? null;
}

function findWaiverReplacement(position, waiverPool) {
  if (!waiverPool) return null;

  // FLEX positions — RB/WR/TE can fill FLEX
  const flexPositions = position === 'FLEX' ? ['RB', 'WR', 'TE'] : [position];

  return waiverPool
    .filter(p =>
      flexPositions.includes(p.position) &&
      (p.play_probability ?? 1.0) > 0.5 &&
      (p.projected_points ?? p.avg_points ?? 0) > 0
    )
    .sort((a, b) => (b.projected_points ?? b.avg_points ?? 0) - (a.projected_points ?? a.avg_points ?? 0))[0] ?? null;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function SeverityBadge({ prob }) {
  const color = severityColor(prob);
  const label = severityLabel(prob);
  return (
    <span style={{
      display:       'inline-block',
      padding:       '2px 6px',
      borderRadius:  '3px',
      fontSize:      '8px',
      fontWeight:    '700',
      letterSpacing: '0.12em',
      background:    color + '20',
      color,
      minWidth:      '80px',
      textAlign:     'center',
    }}>
      {label}
    </span>
  );
}

function PosTag({ pos }) {
  const POS_COLOR = {
    QB: '#5a9ff0', RB: '#50c878', WR: '#c090f0',
    TE: '#f0b840', K: '#808080', DST: '#e06060', FLEX: '#c090f0',
  };
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

function ReplacementCard({ label, player, isWaiver }) {
  if (!player) return null;

  const pts = player.projected_points ?? player.avg_points ?? 0;

  return (
    <div style={{
      marginTop:    '10px',
      padding:      '10px 12px',
      background:   C.bg,
      border:       `1px solid ${C.border}`,
      borderRadius: '4px',
      display:      'flex',
      alignItems:   'center',
      gap:          '10px',
    }}>
      <span style={{
        fontSize:      '8px',
        letterSpacing: '0.12em',
        textTransform: 'uppercase',
        color:         isWaiver ? C.amber : C.green,
        background:    (isWaiver ? C.amber : C.green) + '18',
        padding:       '2px 5px',
        borderRadius:  '3px',
        flexShrink:    0,
        minWidth:      '48px',
        textAlign:     'center',
      }}>
        {isWaiver ? 'WAIVER' : 'BENCH'}
      </span>
      <PosTag pos={player.position} />
      <span style={{ fontSize: '12px', color: C.text, flex: 1 }}>{player.name}</span>
      <span style={{ fontSize: '11px', color: C.textMid, flexShrink: 0 }}>
        {pts.toFixed(1)} proj
      </span>
      {isWaiver && (
        <span style={{ fontSize: '10px', color: C.textDim, flexShrink: 0 }}>
          {player.percent_owned?.toFixed(0) ?? '?'}% owned
        </span>
      )}
    </div>
  );
}

function InjuryCard({ player, myRoster, waiverPool }) {
  const benchRep  = findBenchReplacement(player.position, player.espn_id, myRoster);
  const waiverRep = !benchRep ? findWaiverReplacement(player.position, waiverPool) : null;

  const isStarter     = !player.on_bench && !player.on_ir;
  const probColor     = severityColor(player.play_probability);
  const probPct       = Math.round(player.play_probability * 100);
  const weatherImpact = hasWeatherImpact(player.team);
  const weatherText   = weatherImpact ? getWeatherAdvisory(player.team).join(' · ') : null;

  return (
    <div style={{
      padding:      '16px 20px',
      background:   C.surface,
      border:       `1px solid ${isStarter ? probColor + '40' : C.border}`,
      borderLeft:   `3px solid ${probColor}`,
      borderRadius: '5px',
      marginBottom: '10px',
    }}>
      {/* Player header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: '12px' }}>
        <div style={{ flex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
            <PosTag pos={player.position} />
            <span style={{ fontSize: '13px', color: C.text, fontWeight: '500' }}>
              {player.name}
            </span>
            {isStarter && (
              <span style={{
                fontSize:      '8px',
                letterSpacing: '0.12em',
                textTransform: 'uppercase',
                color:         C.accent,
                background:    C.accent + '15',
                padding:       '1px 5px',
                borderRadius:  '3px',
              }}>STARTER</span>
            )}
          </div>
          <div style={{ fontSize: '10px', color: C.textDim, paddingLeft: '36px', lineHeight: 1.5 }}>
            {player.team}
            {player.injury_detail && (
              <span style={{ marginLeft: '8px', color: probColor }}>
                {player.injury_detail}
              </span>
            )}
          </div>
        </div>

        {/* Right side: severity + play probability */}
        <div style={{ textAlign: 'right', flexShrink: 0 }}>
          <SeverityBadge prob={player.play_probability} />
          <div style={{ fontSize: '11px', color: probColor, marginTop: '5px' }}>
            {probPct}% to play
          </div>
          <div style={{ fontSize: '10px', color: C.textDim, marginTop: '2px' }}>
            {player.projected_points?.toFixed(1) ?? player.avg_points?.toFixed(1) ?? '—'} proj pts
          </div>
        </div>
      </div>

      {/* Weather overlay if relevant */}
      {weatherImpact && (
        <div style={{
          marginTop:    '10px',
          padding:      '6px 10px',
          background:   C.amber + '12',
          border:       `1px solid ${C.amber}30`,
          borderRadius: '3px',
          fontSize:     '10px',
          color:        C.amber,
        }}>
          🌬 {weatherText}
        </div>
      )}

      {/* Replacement recommendations */}
      {isStarter && (
        <div style={{ marginTop: '10px' }}>
          <div style={{ fontSize: '9px', letterSpacing: '0.14em', textTransform: 'uppercase', color: C.textDim, marginBottom: '4px' }}>
            {benchRep || waiverRep ? 'Replacement' : 'No replacement available'}
          </div>
          {benchRep  && <ReplacementCard label="bench"  player={benchRep}  isWaiver={false} />}
          {waiverRep && <ReplacementCard label="waiver" player={waiverRep} isWaiver={true}  />}
        </div>
      )}

      {/* IR / bench note */}
      {!isStarter && (
        <div style={{ marginTop: '8px', fontSize: '10px', color: C.textDim }}>
          {player.on_ir ? 'On IR — not affecting your active lineup' : 'On bench — monitor before setting lineup'}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function InjuryDashboard() {
  const myRoster   = MY_ROSTER   ?? [];
  const waiverPool = WAIVER_POOL ?? [];
  const fetchedAt  = FETCHED_AT  ?? LEAGUE_FETCHED_AT;
  const age        = fetchAge(fetchedAt);

  const injured = useMemo(() => buildInjuredRoster(myRoster), [myRoster]);

  const starters = injured.filter(p => !p.on_bench && !p.on_ir);
  const bench    = injured.filter(p => p.on_bench && !p.on_ir);
  const ir       = injured.filter(p => p.on_ir);

  // No data guard
  if (myRoster.length === 0) {
    return (
      <div style={{
        minHeight: '100vh', background: C.bg, color: C.text, fontFamily: font,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
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
          <span style={{ color: C.border }}>|</span>
          <span style={{ fontSize: '11px', letterSpacing: '0.18em', textTransform: 'uppercase', color: C.accent }}>
            Gridiron Oracle
          </span>
          <span style={{ fontSize: '11px', color: C.textDim, letterSpacing: '0.10em' }}>
            Injury Dashboard
          </span>
          <span style={{ marginLeft: 'auto', fontSize: '10px', color: C.textDim }}>
            {MY_TEAM?.team_name ?? ''}
            {age && <span style={{ marginLeft: '10px' }}>updated {age}</span>}
          </span>
        </header>

        <div style={{ maxWidth: '860px', margin: '0 auto', padding: '32px 40px 100px' }}>

          {/* Summary bar */}
          <div style={{
            display:      'flex',
            gap:          '16px',
            marginBottom: '28px',
            padding:      '16px 20px',
            background:   C.surface,
            border:       `1px solid ${C.border}`,
            borderRadius: '6px',
          }}>
            {[
              { label: 'Injured starters', count: starters.length, color: starters.length > 0 ? C.red : C.green },
              { label: 'Injured bench',    count: bench.length,    color: bench.length > 0    ? C.amber : C.textDim },
              { label: 'On IR',            count: ir.length,       color: ir.length > 0       ? C.textMid : C.textDim },
              { label: 'Total flagged',    count: injured.length,  color: injured.length > 0  ? C.amber : C.green },
            ].map(({ label, count, color }) => (
              <div key={label} style={{ flex: 1, textAlign: 'center' }}>
                <div style={{ fontSize: '22px', fontFamily: serif, color, marginBottom: '4px' }}>
                  {count}
                </div>
                <div style={{ fontSize: '9px', letterSpacing: '0.14em', textTransform: 'uppercase', color: C.textDim }}>
                  {label}
                </div>
              </div>
            ))}
          </div>

          {/* All clear */}
          {injured.length === 0 && (
            <div style={{
              padding:      '40px',
              textAlign:    'center',
              color:        C.textDim,
              fontSize:     '13px',
              border:       `1px solid ${C.border}`,
              borderRadius: '6px',
              background:   C.surface,
              animation:    'fadeIn 0.3s ease',
            }}>
              <div style={{ fontSize: '24px', marginBottom: '10px' }}>✓</div>
              <div style={{ color: C.green, marginBottom: '6px' }}>All rostered players healthy</div>
              <div style={{ fontSize: '11px' }}>No injury flags on your roster</div>
            </div>
          )}

          {/* Injured starters */}
          {starters.length > 0 && (
            <section style={{ marginBottom: '28px', animation: 'fadeIn 0.2s ease' }}>
              <div style={{
                fontSize:      '9px',
                letterSpacing: '0.18em',
                textTransform: 'uppercase',
                color:         C.red,
                marginBottom:  '12px',
                display:       'flex',
                alignItems:    'center',
                gap:           '8px',
              }}>
                <span>⚠ Injured starters</span>
                <span style={{ color: C.textDim }}>— set lineup before kickoff</span>
              </div>
              {starters.map(p => (
                <InjuryCard
                  key={p.espn_id}
                  player={p}
                  myRoster={myRoster}
                  waiverPool={waiverPool}
                />
              ))}
            </section>
          )}

          {/* Injured bench */}
          {bench.length > 0 && (
            <section style={{ marginBottom: '28px', animation: 'fadeIn 0.2s ease' }}>
              <div style={{
                fontSize:      '9px',
                letterSpacing: '0.18em',
                textTransform: 'uppercase',
                color:         C.amber,
                marginBottom:  '12px',
              }}>
                Injured bench players
              </div>
              {bench.map(p => (
                <InjuryCard
                  key={p.espn_id}
                  player={p}
                  myRoster={myRoster}
                  waiverPool={waiverPool}
                />
              ))}
            </section>
          )}

          {/* IR */}
          {ir.length > 0 && (
            <section style={{ animation: 'fadeIn 0.2s ease' }}>
              <div style={{
                fontSize:      '9px',
                letterSpacing: '0.18em',
                textTransform: 'uppercase',
                color:         C.textDim,
                marginBottom:  '12px',
              }}>
                IR — not affecting active lineup
              </div>
              {ir.map(p => (
                <InjuryCard
                  key={p.espn_id}
                  player={p}
                  myRoster={myRoster}
                  waiverPool={waiverPool}
                />
              ))}
            </section>
          )}

          {/* Staleness warning */}
          {age && age.includes('d') && (
            <div style={{
              marginTop:    '24px',
              padding:      '12px 16px',
              background:   C.amber + '10',
              border:       `1px solid ${C.amber}30`,
              borderRadius: '4px',
              fontSize:     '11px',
              color:        C.amber,
            }}>
              ⚠ Injury data last updated {age}. Run{' '}
              <code style={{ color: C.accent }}>python3 scripts/injury_overlay.py</code>
              {' '}for the latest report.
            </div>
          )}

        </div>
      </div>
    </>
  );
}
