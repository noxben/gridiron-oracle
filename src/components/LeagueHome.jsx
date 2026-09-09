// LeagueHome.jsx — Gridiron Oracle
// League overview: standings, power rankings, this week's matchups, recent transactions
// v2.0 Step 5 per spec §4.3

import { useState } from 'react';
import {
  ESPN_LEAGUE_DATA,
  ALL_TEAMS,
  ALL_ROSTERS,
  ALL_MATCHUPS,
  STANDINGS,
  TRANSACTIONS,
  LEAGUE_WEEK,
  LEAGUE_FETCHED_AT,
} from '../utils/espn_league.js';
import { useTeam } from '../utils/TeamContext.jsx';
import { useMobile, contentPadding } from '../utils/useMobile.js';

// ---------------------------------------------------------------------------
// Design tokens — matches LineupOptimizer exactly
// ---------------------------------------------------------------------------

import { C, font, serif } from '../utils/theme.js';

// Power rankings
//
// Week 1:
//   50% starting lineup projected strength
//   25% roster depth
//   15% player availability
//   10% actual performance
//
// As the season progresses, actual performance gradually becomes
// more important while preseason/projection-based strength declines.

function computePowerRankings(teams, rosters, week = 1) {
  if (!teams?.length) return [];

  const clamp = (value, min = 0, max = 100) =>
    Math.max(min, Math.min(max, value));

  const safeNum = (value, fallback = 0) =>
    typeof value === 'number' && Number.isFinite(value)
      ? value
      : fallback;

  /*
   * Season progression.
   *
   * Week 1 = almost entirely projection/roster based.
   * By Week 8+, actual performance becomes the dominant signal.
   */
  const performanceWeight = clamp(
    ((week - 1) / 7) * 0.30,
    0,
    0.30
  );

  const projectionWeight = 0.50 - (performanceWeight * 0.50);
  const depthWeight = 0.25 - (performanceWeight * 0.25);
  const availabilityWeight = 0.15 - (performanceWeight * 0.15);
  const recordWeight = 0.10 + performanceWeight;

  /*
   * First calculate raw roster metrics for every team.
   */
  const metrics = teams.map(team => {
    const roster = rosters?.[String(team.team_id)] ?? [];

    const starters = roster.filter(
      p =>
        !p.on_bench &&
        !p.on_ir &&
        ['QB', 'RB', 'WR', 'TE', 'K', 'DST', 'D/ST'].includes(
          p.position
        )
    );

    const bench = roster.filter(
      p =>
        p.on_bench &&
        !p.on_ir
    );

    /*
     * Projected starter strength.
     *
     * FLEX isn't necessarily represented as a separate ESPN
     * lineup slot in the exported roster, so we take the best
     * eligible RB/WR/TE bench player as the potential FLEX.
     */
    const starterProjected = starters.reduce(
      (sum, p) =>
        sum +
        safeNum(p.projected_points) *
        safeNum(p.play_probability, 1),
      0
    );

    const flexCandidates = bench
      .filter(p =>
        ['RB', 'WR', 'TE'].includes(p.position)
      )
      .sort(
        (a, b) =>
          safeNum(b.projected_points) -
          safeNum(a.projected_points)
      );

    const flexProjected =
      safeNum(
        flexCandidates[0]?.projected_points,
        0
      ) *
      safeNum(
        flexCandidates[0]?.play_probability,
        1
      );

    const totalStarterProjection =
      starterProjected + flexProjected;

    /*
     * Bench depth.
     *
     * Best three bench players count, with diminishing weight.
     */
    const benchDepth =
      bench
        .sort(
          (a, b) =>
            safeNum(b.projected_points) -
            safeNum(a.projected_points)
        )
        .slice(0, 3)
        .reduce((sum, p, index) => {
          const depthMultiplier =
            index === 0 ? 0.35 :
            index === 1 ? 0.25 :
            0.15;

          return (
            sum +
            safeNum(p.projected_points) *
            safeNum(p.play_probability, 1) *
            depthMultiplier
          );
        }, 0);

    /*
     * Availability.
     *
     * Measure the percentage of projected starter production
     * that is actually available.
     */
    const availableStarterProjection =
      starters.reduce(
        (sum, p) =>
          sum +
          safeNum(p.projected_points) *
          safeNum(p.play_probability, 1),
        0
      ) +
      flexProjected;

    const totalStarterProjectionRaw =
      starters.reduce(
        (sum, p) =>
          sum + safeNum(p.projected_points),
        0
      ) +
      safeNum(
        flexCandidates[0]?.projected_points,
        0
      );

    const availability =
      totalStarterProjectionRaw > 0
        ? (
            availableStarterProjection /
            totalStarterProjectionRaw
          ) * 100
        : 100;

    /*
     * Actual performance.
     *
     * In Week 1 this is effectively neutral because there
     * are no results yet.
     */
    const totalGames =
      safeNum(team.wins) +
      safeNum(team.losses) +
      safeNum(team.ties);

    const winPct =
      totalGames > 0
        ? (
            safeNum(team.wins) +
            safeNum(team.ties) * 0.5
          ) / totalGames
        : 0.5;

    const performance =
      totalGames > 0
        ? winPct * 100
        : 50;

    return {
      ...team,
      starterProjection: totalStarterProjection,
      depthProjection: benchDepth,
      availability,
      performance,
    };
  });

  /*
   * Normalize projected/depth scores across the league.
   */
  const normalize = (values, value) => {
    const min = Math.min(...values);
    const max = Math.max(...values);

    if (!Number.isFinite(min) ||
        !Number.isFinite(max) ||
        max === min) {
      return 50;
    }

    return ((value - min) / (max - min)) * 100;
  };

  const starterValues =
    metrics.map(t => t.starterProjection);

  const depthValues =
    metrics.map(t => t.depthProjection);

  /*
   * Final score.
   */
  return metrics
    .map(team => {
      const starterScore = normalize(
        starterValues,
        team.starterProjection
      );

      const depthScore = normalize(
        depthValues,
        team.depthProjection
      );

      const availabilityScore =
        clamp(team.availability);

      const performanceScore =
        clamp(team.performance);

      const powerScore =
        starterScore * projectionWeight +
        depthScore * depthWeight +
        availabilityScore * availabilityWeight +
        performanceScore * recordWeight;

      return {
        ...team,
        starterScore,
        depthScore,
        availabilityScore,
        performanceScore,
        powerScore,
      };
    })
    .sort(
      (a, b) =>
        b.powerScore - a.powerScore
    )
    .map((team, index) => ({
      ...team,
      powerRank: index + 1,
    }));
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

function isMyTeam(teamId, myTeamId) {
  return myTeamId && String(teamId) === String(myTeamId);
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
        fontSize:      '10px',
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

function StandingsTable({ standings, powerRankedTeams, myTeamId }) {
  // Build a lookup: team_id → power rank
  const powerRankMap = {};
  powerRankedTeams.forEach(t => { powerRankMap[t.team_id] = t.powerRank; });

  return (
    <table style={{ width: '100%', borderCollapse: 'collapse' }}>
      <thead>
        <tr style={{ borderBottom: `1px solid ${C.borderMid}` }}>
          {['#', 'Team', 'W-L', 'PF', 'PA', 'PWR'].map((h, i) => (
            <th key={h} style={{
              fontSize:      '10px',
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
          const mine = isMyTeam(team.team_id, myTeamId);
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
                    fontSize:      '10px',
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
                    fontSize:   '10px',
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

function MatchupCard({ matchup, teams, myTeamId }) {
  const teamMap = {};
  teams.forEach(t => { teamMap[t.team_id] = t; });

  const home = teamMap[matchup.home_team_id];
  const away = teamMap[matchup.away_team_id];
  if (!home || !away) return null;

  const myHome = isMyTeam(matchup.home_team_id, myTeamId);
  const myAway = isMyTeam(matchup.away_team_id, myTeamId);
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
          <div style={{ fontSize: '12px', color: isMyTeam(home.team_id, myTeamId) ? C.accent : C.text, fontWeight: isMyTeam(home.team_id, myTeamId) ? '600' : '400' }}>
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
          <div style={{ fontSize: '12px', color: isMyTeam(away.team_id, myTeamId) ? C.accent : C.text, fontWeight: isMyTeam(away.team_id, myTeamId) ? '600' : '400' }}>
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
            background: isMyTeam(home.team_id, myTeamId) ? C.accent : C.textMid,
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

function PowerMetric({ label, value }) {
  return (
    <div>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          fontSize: '8px',
          color: C.textDim,
          marginBottom: '4px',
        }}
      >
        <span>{label}</span>
        <span>{value.toFixed(0)}</span>
      </div>

      <div
        style={{
          height: '3px',
          background: C.border,
          borderRadius: '2px',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            width: `${Math.max(
              0,
              Math.min(100, value)
            )}%`,
            height: '100%',
            background: C.textMid,
          }}
        />
      </div>
    </div>
  );
}

function PowerRankings({ teams, myTeamId, week }) {
  return (
    <div>
      <SectionHeader
        label="Power rankings"
        right={`Week ${week} · projection based`}
      />

      <div
        style={{
          fontSize: '11px',
          color: C.textDim,
          marginBottom: '14px',
          lineHeight: 1.5,
        }}
      >
        Rankings emphasize projected starting strength,
        roster depth, and player availability. Actual
        performance gains weight as the season progresses.
      </div>

      <div
        style={{
          border: `1px solid ${C.border}`,
          borderRadius: '6px',
          overflow: 'hidden',
        }}
      >
        {teams.map((team, index) => {
          const isMine =
            String(team.team_id) === String(myTeamId);

          return (
            <div
              key={team.team_id}
              style={{
                padding: '12px 14px',
                borderBottom:
                  index < teams.length - 1
                    ? `1px solid ${C.border}`
                    : 'none',
                background: isMine
                  ? C.red + '08'
                  : 'transparent',
              }}
            >
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns:
                    '32px minmax(180px, 1fr) 70px 90px',
                  gap: '12px',
                  alignItems: 'center',
                }}
              >
                {/* Rank */}
                <div
                  style={{
                    fontFamily: serif,
                    fontSize: '16px',
                    color:
                      team.powerRank <= 3
                        ? C.text
                        : C.textDim,
                  }}
                >
                  {team.powerRank}
                </div>

                {/* Team */}
                <div>
                  <div
                    style={{
                      fontSize: '12px',
                      fontWeight: 600,
                      color: C.text,
                    }}
                  >
                    {team.team_name}
                    {isMine && (
                      <span
                        style={{
                          marginLeft: '7px',
                          fontSize: '9px',
                          color: C.red,
                        }}
                      >
                        YOU
                      </span>
                    )}
                  </div>

                  <div
                    style={{
                      fontSize: '9px',
                      color: C.textDim,
                      marginTop: '3px',
                    }}
                  >
                    {team.wins}-{team.losses}
                    {team.ties
                      ? `-${team.ties}`
                      : ''}
                  </div>
                </div>

                {/* Score */}
                <div
                  style={{
                    textAlign: 'right',
                    fontFamily: serif,
                    fontSize: '18px',
                    color: C.text,
                  }}
                >
                  {team.powerScore.toFixed(1)}
                </div>

                {/* Projected */}
                <div
                  style={{
                    textAlign: 'right',
                    fontSize: '10px',
                    color: C.textMid,
                  }}
                >
                  <div>
                    {team.starterProjection.toFixed(1)}
                    {' '}proj.
                  </div>
                  <div
                    style={{
                      marginTop: '3px',
                      color: C.textDim,
                    }}
                  >
                    {team.availabilityScore.toFixed(0)}%
                    {' '}available
                  </div>
                </div>
              </div>

              {/* Detail bar */}
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns:
                    '1fr 1fr 1fr',
                  gap: '12px',
                  marginTop: '10px',
                  paddingLeft: '44px',
                }}
              >
                <PowerMetric
                  label="STARTERS"
                  value={team.starterScore}
                />

                <PowerMetric
                  label="DEPTH"
                  value={team.depthScore}
                />

                <PowerMetric
                  label="AVAILABILITY"
                  value={team.availabilityScore}
                />
              </div>
            </div>
          );
        })}
      </div>
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
  const { teamData } = useTeam();
  const { isMobile, isNarrow } = useMobile();
  const pad = contentPadding(isMobile, isNarrow);
  const myTeamId = teamData?.teamId ?? null;
  const [activeTab, setActiveTab] = useState('standings');

  const teams        = ALL_TEAMS       ?? [];
  const matchups     = ALL_MATCHUPS    ?? [];
  const standings    = STANDINGS       ?? [];
  const transactions = TRANSACTIONS    ?? [];
  const week         = LEAGUE_WEEK     ?? '—';
  const fetchedAt    = LEAGUE_FETCHED_AT;
  const age          = fetchAge(fetchedAt);

  const powerRanked = computePowerRankings(
    teams,
    ALL_ROSTERS,
    week
  );
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

        <div style={{ maxWidth: '860px', margin: '0 auto', padding: `32px ${pad} 100px` }}>

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
                <StandingsTable standings={standings} powerRankedTeams={powerRanked} myTeamId={myTeamId} />
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
                <PowerRankings
				  teams={powerRanked}
				  myTeamId={myTeamId}
				  week={week}
				/>
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
                  <MatchupCard key={i} matchup={m} teams={teams} myTeamId={myTeamId} />
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
