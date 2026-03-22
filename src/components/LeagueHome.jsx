// LeagueHome.jsx — Gridiron Oracle
// League overview: standings, power rankings, this week's matchups, recent transactions
// v2.0 Step 5 per spec §4.3

import { useState } from 'react';
import {
  ESPN_LEAGUE_DATA,
  ALL_TEAMS,
  ALL_MATCHUPS,
  STANDINGS,
  TRANSACTIONS,
  LEAGUE_WEEK,
  LEAGUE_FETCHED_AT,
} from '../utils/espn_league.js';
import { MY_TEAM } from '../utils/espn_data.js';

// ---------------------------------------------------------------------------
// Design tokens — matches LineupOptimizer exactly
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
// Power rankings — algorithm-based per spec §3.1
// Formula: 60% points_for + 40% win% (normalized across all teams)
// ---------------------------------------------------------------------------

function computePowerRankings(teams) {
  if (!teams || teams.length === 0) return [];

  const maxPF  = Math.max(...teams.map(t => t.points_for));
  const minPF  = Math.min(...teams.map(t => t.points_for));
  const pfRange = maxPF - minPF || 1;

  return teams
    .map(t => {
      const totalGames = (t.wins + t.losses + (t.ties ?? 0)) || 1;
      const winPct     = (t.wins + (t.ties ?? 0) * 0.5) / totalGames;
      const pfScore    = (t.points_for - minPF) / pfRange;   // 0–1
      const power      = (0.60 * pfScore) + (0.40 * winPct); // weighted
      return { ...t, powerScore: power, winPct };
    })
    .sort((a, b) => b.powerScore - a.powerScore)
    .map((t, i) => ({ ...t, powerRank: i + 1 }));
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fetchAge(fetchedAt) {
  if (!fetchedAt) return null;
  const mins = Math.round((Date.now() - new Date(fetchedAt).getTime()) / 60000);
  if (mins < 60)  return `${mins}m ago`;
  if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
  return `${Math.round(mins / 1440)}d ago`;
}

function recordStr(t) {
  const ties = t.ties ?? 0;
  return ties > 0 ? `${t.wins}-${t.losses}-${ties}` : `${t.wins}-${t.losses}`;
}

function isMyTeam(teamId) {
  return MY_TEAM && String(teamId) === String(MY_TEAM.team_id);
}

function shortName(name) {
  // Truncate long team names gracefully
  return name?.length > 22 ? name.slice(0, 20) + '…' : name;
}

function txTypeLabel(type) {
  if (type === 'ADD')    return { label: 'ADD',   color: C.green };
  if (type === 'DROP')   return { label: 'DROP',  color: C.red };
  if (type === 'TRADED') return { label: 'TRADE', color: C.amber };
  return { label: type,  color: C.textDim };
}

function txDate(isoStr) {
  if (!isoStr) return '';
  try {
    return new Date(isoStr).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  } catch { return ''; }
}

// ---------------------------------------------------------------------------
// Section header
// ---------------------------------------------------------------------------

function SectionHeader({ label, right }) {
  return (
    <div style={{
      display:        'flex',
      justifyContent: 'space-between',
      alignItems:     'baseline',
      marginBottom:   '10px',
    }}>
      <div style={{
        fontSize:      '9px',
        letterSpacing: '0.18em',
        textTransform: 'uppercase',
        color:         C.textDim,
      }}>
        {label}
      </div>
      {right && (
        <div style={{ fontSize: '10px', color: C.textDim }}>{right}</div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Standings table
// ---------------------------------------------------------------------------

function StandingsTable({ standings, powerRankedTeams }) {
  // Build a lookup: team_id → power rank
  const powerRankMap = {};
  powerRankedTeams.forEach(t => { powerRankMap[t.team_id] = t.powerRank; });

  return (
    <table style={{ width: '100%', borderCollapse: 'collapse' }}>
      <thead>
        <tr style={{ borderBottom: `1px solid ${C.borderMid}` }}>
          {['#', 'Team', 'W-L', 'PF', 'PA', 'PWR'].map((h, i) => (
            <th key={h} style={{
              fontSize:      '9px',
              letterSpacing: '0.14em',
              textTransform: 'uppercase',
              color:         C.textDim,
              fontWeight:    '400',
              fontFamily:    font,
              textAlign:     i >= 2 ? 'right' : 'left',
              padding:       '6px 0',
              paddingRight:  i < 5 ? '16px' : '0',
            }}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {standings.map((team, i) => {
          const mine = isMyTeam(team.team_id);
          const pwr  = powerRankMap[team.team_id];
          const pwrDelta = team.seed - pwr; // positive = ranked higher by power than by record

          return (
            <tr key={team.team_id} style={{
              borderBottom: `1px solid ${C.border}`,
              background:   mine ? C.accent + '08' : 'transparent',
            }}>
              {/* Seed */}
              <td style={{ padding: '10px 16px 10px 0', width: '24px' }}>
                <span style={{ fontSize: '11px', color: mine ? C.accent : C.textDim }}>
                  {team.seed}
                </span>
              </td>
              {/* Team name */}
              <td style={{ padding: '10px 16px 10px 0' }}>
                <span style={{
                  fontSize:   '12px',
                  color:      mine ? C.accent : C.text,
                  fontWeight: mine ? '600' : '400',
                }}>
                  {shortName(team.team_name)}
                </span>
                {mine && (
                  <span style={{
                    marginLeft:    '8px',
                    fontSize:      '8px',
                    color:         C.accentDim,
                    letterSpacing: '0.12em',
                    textTransform: 'uppercase',
                  }}>you</span>
                )}
              </td>
              {/* Record */}
              <td style={{ padding: '10px 16px 10px 0', textAlign: 'right' }}>
                <span style={{ fontSize: '11px', color: C.textMid }}>
                  {recordStr(team)}
                </span>
              </td>
              {/* Points for */}
              <td style={{ padding: '10px 16px 10px 0', textAlign: 'right' }}>
                <span style={{ fontSize: '11px', color: C.text }}>
                  {team.points_for?.toFixed(0)}
                </span>
              </td>
              {/* Points against */}
              <td style={{ padding: '10px 16px 10px 0', textAlign: 'right' }}>
                <span style={{ fontSize: '11px', color: C.textDim }}>
                  {team.points_against?.toFixed(0)}
                </span>
              </td>
              {/* Power rank + delta */}
              <td style={{ padding: '10px 0', textAlign: 'right' }}>
                <span style={{ fontSize: '11px', color: C.textMid }}>#{pwr}</span>
                {pwrDelta !== 0 && (
                  <span style={{
                    marginLeft: '4px',
                    fontSize:   '9px',
                    color:      pwrDelta > 0 ? C.red : C.green,
                  }}>
                    {pwrDelta > 0 ? `▼${pwrDelta}` : `▲${Math.abs(pwrDelta)}`}
                  </span>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

// ---------------------------------------------------------------------------
// Matchup card
// ---------------------------------------------------------------------------

function MatchupCard({ matchup, teams }) {
  const teamMap = {};
  teams.forEach(t => { teamMap[t.team_id] = t; });

  const home = teamMap[matchup.home_team_id];
  const away = teamMap[matchup.away_team_id];
  if (!home || !away) return null;

  const myHome = isMyTeam(matchup.home_team_id);
  const myAway = isMyTeam(matchup.away_team_id);
  const involved = myHome || myAway;

  const homeProj = matchup.home_projected ?? 0;
  const awayProj = matchup.away_projected ?? 0;
  const total    = homeProj + awayProj || 1;
  const homePct  = (homeProj / total) * 100;

  return (
    <div style={{
      padding:      '14px 16px',
      background:   involved ? C.accent + '06' : C.surface,
      border:       `1px solid ${involved ? C.accentDim : C.border}`,
      borderRadius: '5px',
      marginBottom: '8px',
    }}>
      {/* Teams row */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' }}>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: '12px', color: isMyTeam(home.team_id) ? C.accent : C.text, fontWeight: isMyTeam(home.team_id) ? '600' : '400' }}>
            {shortName(home.team_name)}
          </div>
          <div style={{ fontSize: '10px', color: C.textDim, marginTop: '2px' }}>{recordStr(home)}</div>
        </div>

        <div style={{ textAlign: 'center', padding: '0 16px' }}>
          <div style={{ fontSize: '9px', letterSpacing: '0.14em', color: C.textDim, textTransform: 'uppercase' }}>vs</div>
          {homeProj > 0 && (
            <div style={{ fontSize: '10px', color: C.textMid, marginTop: '3px' }}>
              {homeProj.toFixed(1)} – {awayProj.toFixed(1)}
            </div>
          )}
        </div>

        <div style={{ flex: 1, textAlign: 'right' }}>
          <div style={{ fontSize: '12px', color: isMyTeam(away.team_id) ? C.accent : C.text, fontWeight: isMyTeam(away.team_id) ? '600' : '400' }}>
            {shortName(away.team_name)}
          </div>
          <div style={{ fontSize: '10px', color: C.textDim, marginTop: '2px' }}>{recordStr(away)}</div>
        </div>
      </div>

      {/* Projected score bar */}
      {homeProj > 0 && (
        <div style={{ height: '2px', background: C.border, borderRadius: '1px', overflow: 'hidden' }}>
          <div style={{
            height:     '100%',
            width:      `${homePct}%`,
            background: isMyTeam(home.team_id) ? C.accent : C.textMid,
            borderRadius: '1px',
            transition: 'width 0.6s ease',
          }} />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Power rankings strip
// ---------------------------------------------------------------------------

function PowerRankings({ rankedTeams }) {
  const top3 = rankedTeams.slice(0, 3);
  const rest = rankedTeams.slice(3);

  return (
    <div>
      {/* Top 3 — highlighted */}
      {top3.map((team, i) => (
        <div key={team.team_id} style={{
          display:      'flex',
          alignItems:   'center',
          padding:      '10px 0',
          borderBottom: `1px solid ${C.border}`,
          gap:          '12px',
        }}>
          <span style={{
            fontSize:   '18px',
            fontFamily: serif,
            color:      i === 0 ? C.accent : i === 1 ? C.textMid : C.textDim,
            width:      '24px',
            flexShrink: 0,
          }}>
            {i + 1}
          </span>
          <div style={{ flex: 1 }}>
            <div style={{
              fontSize:   '12px',
              color:      isMyTeam(team.team_id) ? C.accent : C.text,
              fontWeight: isMyTeam(team.team_id) ? '600' : '400',
            }}>
              {shortName(team.team_name)}
            </div>
            <div style={{ fontSize: '10px', color: C.textDim, marginTop: '2px' }}>
              {recordStr(team)} · {team.points_for?.toFixed(0)} pts
            </div>
          </div>
          {/* Power score bar */}
          <div style={{ width: '80px' }}>
            <div style={{ height: '2px', background: C.border, borderRadius: '1px', overflow: 'hidden' }}>
              <div style={{
                height:     '100%',
                width:      `${team.powerScore * 100}%`,
                background: i === 0 ? C.accent : C.textMid,
                borderRadius: '1px',
              }} />
            </div>
          </div>
        </div>
      ))}

      {/* Ranks 4–12 — condensed */}
      {rest.map(team => (
        <div key={team.team_id} style={{
          display:      'flex',
          alignItems:   'center',
          padding:      '7px 0',
          borderBottom: `1px solid ${C.border}`,
          gap:          '12px',
        }}>
          <span style={{ fontSize: '10px', color: C.textDim, width: '24px', flexShrink: 0 }}>
            {team.powerRank}
          </span>
          <span style={{
            fontSize:   '11px',
            color:      isMyTeam(team.team_id) ? C.accent : C.textMid,
            fontWeight: isMyTeam(team.team_id) ? '600' : '400',
            flex:       1,
          }}>
            {shortName(team.team_name)}
          </span>
          <span style={{ fontSize: '10px', color: C.textDim }}>
            {recordStr(team)}
          </span>
          <span style={{ fontSize: '10px', color: C.textDim, width: '52px', textAlign: 'right' }}>
            {team.points_for?.toFixed(0)} pts
          </span>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Transactions feed
// ---------------------------------------------------------------------------

function TransactionsFeed({ transactions }) {
  // Deduplicate and show last 15
  const seen    = new Set();
  const deduped = [];
  for (const tx of transactions) {
    const key = `${tx.date}-${tx.type}-${tx.espn_id}`;
    if (!seen.has(key)) { seen.add(key); deduped.push(tx); }
    if (deduped.length >= 15) break;
  }

  if (deduped.length === 0) {
    return <div style={{ fontSize: '11px', color: C.textDim, padding: '12px 0' }}>No recent transactions.</div>;
  }

  return (
    <div>
      {deduped.map((tx, i) => {
        const { label, color } = txTypeLabel(tx.type);
        return (
          <div key={i} style={{
            display:      'flex',
            alignItems:   'center',
            gap:          '10px',
            padding:      '8px 0',
            borderBottom: `1px solid ${C.border}`,
          }}>
            {/* Type badge */}
            <span style={{
              fontSize:      '8px',
              fontWeight:    '700',
              letterSpacing: '0.10em',
              color,
              background:    color + '18',
              padding:       '2px 5px',
              borderRadius:  '3px',
              minWidth:      '38px',
              textAlign:     'center',
              flexShrink:    0,
            }}>
              {label}
            </span>
            {/* Player name */}
            <span style={{ fontSize: '11px', color: C.text, flex: 1 }}>
              {tx.player}
            </span>
            {/* Date */}
            <span style={{ fontSize: '10px', color: C.textDim, flexShrink: 0 }}>
              {txDate(tx.date)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function LeagueHome() {
  const [activeTab, setActiveTab] = useState('standings');

  const teams        = ALL_TEAMS       ?? [];
  const matchups     = ALL_MATCHUPS    ?? [];
  const standings    = STANDINGS       ?? [];
  const transactions = TRANSACTIONS    ?? [];
  const week         = LEAGUE_WEEK     ?? '—';
  const fetchedAt    = LEAGUE_FETCHED_AT;
  const age          = fetchAge(fetchedAt);

  const powerRanked  = computePowerRankings(teams);

  // No data guard
  if (teams.length === 0) {
    return (
      <div style={{ minHeight: '100vh', background: C.bg, color: C.text, fontFamily: font,
                    display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: '13px', color: C.textDim, marginBottom: '8px' }}>No league data</div>
          <div style={{ fontSize: '11px', color: C.textDim }}>
            Run <code style={{ color: C.accent }}>python3 scripts/fetch_espn_league.py</code>
          </div>
        </div>
      </div>
    );
  }

  const tabs = [
    { key: 'standings',   label: 'Standings' },
    { key: 'power',       label: 'Power' },
    { key: 'matchups',    label: `Week ${week}` },
    { key: 'transactions', label: 'Activity' },
  ];

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
            League Home
          </span>
          <span style={{ marginLeft: 'auto', fontSize: '10px', color: C.textDim }}>
            {ESPN_LEAGUE_DATA?.league_id} · Week {week}
            {age && <span style={{ marginLeft: '10px' }}>updated {age}</span>}
          </span>
        </header>

        <div style={{ maxWidth: '860px', margin: '0 auto', padding: '32px 40px 100px' }}>

          {/* Tab bar */}
          <div style={{
            display:      'flex',
            gap:          '2px',
            marginBottom: '28px',
            borderBottom: `1px solid ${C.border}`,
          }}>
            {tabs.map(({ key, label }) => (
              <button
                key={key}
                onClick={() => setActiveTab(key)}
                style={{
                  background:    'transparent',
                  border:        'none',
                  borderBottom:  activeTab === key ? `2px solid ${C.accent}` : '2px solid transparent',
                  color:         activeTab === key ? C.accent : C.textDim,
                  padding:       '8px 16px',
                  fontSize:      '11px',
                  letterSpacing: '0.12em',
                  textTransform: 'uppercase',
                  cursor:        'pointer',
                  fontFamily:    font,
                  marginBottom:  '-1px',
                  transition:    'all 0.15s',
                }}
              >
                {label}
              </button>
            ))}
          </div>

          {/* Standings tab */}
          {activeTab === 'standings' && (
            <div style={{ animation: 'fadeIn 0.2s ease' }}>
              <SectionHeader
                label="Current standings"
                right="PWR = power rank vs seed"
              />
              <div style={{
                background:   C.surface,
                border:       `1px solid ${C.border}`,
                borderRadius: '6px',
                padding:      '0 20px',
              }}>
                <StandingsTable standings={standings} powerRankedTeams={powerRanked} />
              </div>
              <div style={{ marginTop: '12px', fontSize: '10px', color: C.textDim, lineHeight: 1.6 }}>
                PWR column shows power ranking (60% scoring, 40% win%). ▲ = ranked higher by power than record. ▼ = ranked lower.
              </div>
            </div>
          )}

          {/* Power rankings tab */}
          {activeTab === 'power' && (
            <div style={{ animation: 'fadeIn 0.2s ease' }}>
              <SectionHeader
                label="Power rankings"
                right="60% scoring · 40% win%"
              />
              <div style={{
                background:   C.surface,
                border:       `1px solid ${C.border}`,
                borderRadius: '6px',
                padding:      '0 20px',
              }}>
                <PowerRankings rankedTeams={powerRanked} />
              </div>
              <div style={{ marginTop: '12px', fontSize: '10px', color: C.textDim, lineHeight: 1.6 }}>
                Power ranking rewards teams scoring well regardless of record. A team with bad luck (high PA) will rank higher here than in the standings.
              </div>
            </div>
          )}

          {/* Matchups tab */}
          {activeTab === 'matchups' && (
            <div style={{ animation: 'fadeIn 0.2s ease' }}>
              <SectionHeader
                label={`Week ${week} matchups`}
                right={matchups[0]?.home_projected > 0 ? 'ESPN projected scores shown' : undefined}
              />
              {matchups.length === 0 ? (
                <div style={{ fontSize: '11px', color: C.textDim, padding: '20px 0' }}>
                  No matchup data for week {week}.
                </div>
              ) : (
                matchups.map((m, i) => (
                  <MatchupCard key={i} matchup={m} teams={teams} />
                ))
              )}
            </div>
          )}

          {/* Activity tab */}
          {activeTab === 'transactions' && (
            <div style={{ animation: 'fadeIn 0.2s ease' }}>
              <SectionHeader
                label="Recent activity"
                right="Last 15 transactions"
              />
              <div style={{
                background:   C.surface,
                border:       `1px solid ${C.border}`,
                borderRadius: '6px',
                padding:      '0 20px',
              }}>
                <TransactionsFeed transactions={transactions} />
              </div>
            </div>
          )}

        </div>
      </div>
    </>
  );
}
