// TeamContext.jsx — Gridiron Oracle
// Provides active team_id and derived roster/matchup data to all views.
// Replaces direct imports of MY_ROSTER/MY_TEAM from espn_data.js for
// multi-user support — each manager sees their own team based on passcode.

import { createContext, useContext, useState, useEffect, useMemo } from 'react';
import { ALL_ROSTERS, ALL_MATCHUPS, ALL_TEAMS, ESPN_LEAGUE_DATA } from './espn_league.js';
import { ESPN_TO_GSIS } from './id_mapping.js';

// ---------------------------------------------------------------------------
// Passcode → team_id map
// Change any code by editing this object — regenerate and re-share as needed
// ---------------------------------------------------------------------------

export const TEAM_CODES = {
  "6022": 28,  // Wild Thornberries
  "7680": 29,  // Luck of the Irish
  "4025": 14,  // Kicked in the Nooksack!
  "0165": 7,   // Tennessee Standby4Recknng
  "2580": 25,  // spokane  sanders
  "8631": 24,  // Jackie Moon
  "9300": 15,  // Hurts Me to Hurts You
  "8687": 27,  // Kettle King
  "5935": 18,  // Blue Steel
  "8685": 23,  // Los Pollos Hermanos
  "7022": 21,  // Local Sports Team
  "0212": 8,   // Super e-Bowl-a
};

export const SESSION_KEY = 'go_team_id'; // sessionStorage key

// ---------------------------------------------------------------------------
// Normalize a roster entry from espn_league.js into the shape all views expect
// Mirrors the field names from espn_data.js MY_ROSTER so views don't need changes
// ---------------------------------------------------------------------------

function normalizePlayer(p) {
  return {
    // Identity
    espn_id:          String(p.espn_id ?? ''),
    gsisId:           ESPN_TO_GSIS[String(p.espn_id)] ?? null,
    name:             p.name ?? 'Unknown',
    position:         p.position ?? 'UNK',
    team:             p.team ?? 'UNK',

    // Lineup slot
    lineup_slot:      p.lineup_slot ?? 'BENCH',
    on_bench:         p.on_bench  ?? true,
    on_ir:            p.on_ir     ?? false,

    // Injury
    injury_status:    p.injury_status    ?? 'ACTIVE',
    play_probability: p.play_probability ?? 1.0,
    injury_detail:    p.injury_detail    ?? '',

    // Scoring
    avg_points:       p.avg_points       ?? 0,
    total_points:     p.total_points     ?? 0,
    projected_points: p.projected_points ?? p.avg_points ?? 0,

    // Availability
    percent_owned:    p.percent_owned   ?? 0,
    percent_started:  p.percent_started ?? 0,
  };
}

// ---------------------------------------------------------------------------
// Derive matchup for a given team_id from this week's matchups
// ---------------------------------------------------------------------------

function deriveMatchup(teamId) {
  if (!ALL_MATCHUPS || !teamId) return null;
  const m = ALL_MATCHUPS.find(
    m => m.home_team_id === teamId || m.away_team_id === teamId
  );
  if (!m) return null;

  const isHome    = m.home_team_id === teamId;
  const oppTeamId = isHome ? m.away_team_id : m.home_team_id;
  const oppTeam   = (ALL_TEAMS ?? []).find(t => t.team_id === oppTeamId);

  return {
    week:           m.week,
    my_team_id:     teamId,
    opp_team_id:    oppTeamId,
    opp_team_name:  oppTeam?.team_name ?? `Team ${oppTeamId}`,
    my_projected:   isHome ? m.home_projected : m.away_projected,
    opp_projected:  isHome ? m.away_projected : m.home_projected,
    my_actual:      isHome ? m.home_score     : m.away_score,
    opp_actual:     isHome ? m.away_score     : m.home_score,
  };
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

const TeamContext = createContext(null);

export function TeamProvider({ children }) {
  const [teamId, setTeamId] = useState(() => {
    // Restore from sessionStorage on load
    const stored = sessionStorage.getItem(SESSION_KEY);
    return stored ? Number(stored) : null;
  });

  // Persist to sessionStorage on change
  useEffect(() => {
    if (teamId) {
      sessionStorage.setItem(SESSION_KEY, String(teamId));
    } else {
      sessionStorage.removeItem(SESSION_KEY);
    }
  }, [teamId]);

  // Derive team data from espn_league.js
  const teamData = useMemo(() => {
    if (!teamId) return null;

    const rawRoster  = ALL_ROSTERS?.[String(teamId)] ?? [];
    const roster     = rawRoster.map(normalizePlayer);
    const myTeam     = (ALL_TEAMS ?? []).find(t => t.team_id === teamId) ?? null;
    const matchup    = deriveMatchup(teamId);

    return {
      teamId,
      myTeam,
      myRoster:  roster,
      matchup,
    };
  }, [teamId]);

  const login  = (id) => setTeamId(id);
  const logout = () => setTeamId(null);

  return (
    <TeamContext.Provider value={{ teamId, teamData, login, logout }}>
      {children}
    </TeamContext.Provider>
  );
}

export function useTeam() {
  const ctx = useContext(TeamContext);
  if (!ctx) throw new Error('useTeam must be used inside TeamProvider');
  return ctx;
}
