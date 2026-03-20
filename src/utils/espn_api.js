// espn_api.js — Gridiron Oracle ESPN API Integration
// =====================================================
// ALL ESPN API calls are isolated here per spec §4.1.
// If ESPN changes their API, fix this one file.
//
// ESPN's API is unofficial and undocumented — it works but can break
// without notice. Every call is wrapped in try/catch with clear error messages.
//
// Private league auth: requires ESPN_S2 + SWID cookies.
// Get these from your browser while logged into ESPN Fantasy:
//   Chrome → DevTools → Application → Cookies → fantasy.espn.com
//   Copy the values for "ESPN_S2" and "SWID"
//
// Usage:
//   import { fetchLeague, fetchRoster, fetchMatchup } from './espn_api.js';
//
// League ID: 839979 (private)

import { batchEspnToGsis } from './id_mapping.js';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const ESPN_BASE = 'https://fantasy.espn.com/apis/v3/games/ffl';
const SEASON    = 2024;
const LEAGUE_ID = 839979;

// Position slot IDs used by ESPN's lineup API
// These map ESPN's internal slot codes to readable position names
const ESPN_SLOT_MAP = {
  0:  'QB',
  2:  'RB',
  4:  'WR',
  6:  'TE',
  16: 'DST',
  17: 'K',
  20: 'BENCH',
  21: 'IR',
  23: 'FLEX',   // RB/WR/TE flex
};

// ESPN injury status → our play_probability (mirrors spec §7.3)
const ESPN_INJURY_MAP = {
  'ACTIVE':       1.0,
  'NORMAL':       1.0,
  'PROBABLE':     0.92,
  'QUESTIONABLE': 0.55,
  'DOUBTFUL':     0.25,
  'OUT':          0.0,
  'IR':           0.0,
  'SUSPENSION':   0.0,
};

// DST teams — mapped by team abbreviation, no GSIS ID needed
const DST_TEAM_MAP = {
  'ARI': { gsis_id: 'DST_ARI', name: 'Cardinals D/ST',  team: 'ARI', position: 'DST' },
  'ATL': { gsis_id: 'DST_ATL', name: 'Falcons D/ST',    team: 'ATL', position: 'DST' },
  'BAL': { gsis_id: 'DST_BAL', name: 'Ravens D/ST',     team: 'BAL', position: 'DST' },
  'BUF': { gsis_id: 'DST_BUF', name: 'Bills D/ST',      team: 'BUF', position: 'DST' },
  'CAR': { gsis_id: 'DST_CAR', name: 'Panthers D/ST',   team: 'CAR', position: 'DST' },
  'CHI': { gsis_id: 'DST_CHI', name: 'Bears D/ST',      team: 'CHI', position: 'DST' },
  'CIN': { gsis_id: 'DST_CIN', name: 'Bengals D/ST',    team: 'CIN', position: 'DST' },
  'CLE': { gsis_id: 'DST_CLE', name: 'Browns D/ST',     team: 'CLE', position: 'DST' },
  'DAL': { gsis_id: 'DST_DAL', name: 'Cowboys D/ST',    team: 'DAL', position: 'DST' },
  'DEN': { gsis_id: 'DST_DEN', name: 'Broncos D/ST',    team: 'DEN', position: 'DST' },
  'DET': { gsis_id: 'DST_DET', name: 'Lions D/ST',      team: 'DET', position: 'DST' },
  'GB':  { gsis_id: 'DST_GB',  name: 'Packers D/ST',    team: 'GB',  position: 'DST' },
  'HOU': { gsis_id: 'DST_HOU', name: 'Texans D/ST',     team: 'HOU', position: 'DST' },
  'IND': { gsis_id: 'DST_IND', name: 'Colts D/ST',      team: 'IND', position: 'DST' },
  'JAX': { gsis_id: 'DST_JAX', name: 'Jaguars D/ST',    team: 'JAX', position: 'DST' },
  'KC':  { gsis_id: 'DST_KC',  name: 'Chiefs D/ST',     team: 'KC',  position: 'DST' },
  'LAC': { gsis_id: 'DST_LAC', name: 'Chargers D/ST',   team: 'LAC', position: 'DST' },
  'LAR': { gsis_id: 'DST_LAR', name: 'Rams D/ST',       team: 'LAR', position: 'DST' },
  'LV':  { gsis_id: 'DST_LV',  name: 'Raiders D/ST',    team: 'LV',  position: 'DST' },
  'MIA': { gsis_id: 'DST_MIA', name: 'Dolphins D/ST',   team: 'MIA', position: 'DST' },
  'MIN': { gsis_id: 'DST_MIN', name: 'Vikings D/ST',    team: 'MIN', position: 'DST' },
  'NE':  { gsis_id: 'DST_NE',  name: 'Patriots D/ST',   team: 'NE',  position: 'DST' },
  'NO':  { gsis_id: 'DST_NO',  name: 'Saints D/ST',     team: 'NO',  position: 'DST' },
  'NYG': { gsis_id: 'DST_NYG', name: 'Giants D/ST',     team: 'NYG', position: 'DST' },
  'NYJ': { gsis_id: 'DST_NYJ', name: 'Jets D/ST',       team: 'NYJ', position: 'DST' },
  'PHI': { gsis_id: 'DST_PHI', name: 'Eagles D/ST',     team: 'PHI', position: 'DST' },
  'PIT': { gsis_id: 'DST_PIT', name: 'Steelers D/ST',   team: 'PIT', position: 'DST' },
  'SEA': { gsis_id: 'DST_SEA', name: 'Seahawks D/ST',   team: 'SEA', position: 'DST' },
  'SF':  { gsis_id: 'DST_SF',  name: '49ers D/ST',      team: 'SF',  position: 'DST' },
  'TB':  { gsis_id: 'DST_TB',  name: 'Buccaneers D/ST', team: 'TB',  position: 'DST' },
  'TEN': { gsis_id: 'DST_TEN', name: 'Titans D/ST',     team: 'TEN', position: 'DST' },
  'WSH': { gsis_id: 'DST_WSH', name: 'Commanders D/ST', team: 'WSH', position: 'DST' },
};

// ---------------------------------------------------------------------------
// Auth helpers
// ---------------------------------------------------------------------------

/**
 * Build headers for ESPN private league requests.
 * ESPN_S2 and SWID are stored in sessionStorage (set via RosterSetup UI).
 * Never hardcoded — never committed to git.
 */
function getAuthHeaders() {
  const espnS2 = sessionStorage.getItem('ESPN_S2');
  const swid   = sessionStorage.getItem('SWID');

  if (!espnS2 || !swid) {
    throw new ESPNAuthError(
      'ESPN credentials not set. Enter your ESPN_S2 and SWID cookies in the setup screen.'
    );
  }

  return {
    'Content-Type': 'application/json',
    'Cookie': `ESPN_S2=${espnS2}; SWID=${swid}`,
  };
}

/**
 * Store ESPN credentials in sessionStorage.
 * Called from RosterSetup.js when the user pastes their cookies.
 * sessionStorage clears on tab close — credentials are never persisted to disk.
 */
export function setESPNCredentials(espnS2, swid) {
  if (!espnS2 || !swid) throw new Error('Both ESPN_S2 and SWID are required');
  sessionStorage.setItem('ESPN_S2', espnS2.trim());
  sessionStorage.setItem('SWID',    swid.trim());
}

export function clearESPNCredentials() {
  sessionStorage.removeItem('ESPN_S2');
  sessionStorage.removeItem('SWID');
}

export function hasESPNCredentials() {
  return !!(sessionStorage.getItem('ESPN_S2') && sessionStorage.getItem('SWID'));
}

// ---------------------------------------------------------------------------
// Error types
// ---------------------------------------------------------------------------

export class ESPNAuthError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ESPNAuthError';
  }
}

export class ESPNApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name   = 'ESPNApiError';
    this.status = status;
  }
}

// ---------------------------------------------------------------------------
// Core fetch wrapper
// ---------------------------------------------------------------------------

async function espnFetch(url, params = {}) {
  const urlObj = new URL(url);
  Object.entries(params).forEach(([k, v]) => urlObj.searchParams.set(k, v));

  let headers;
  try {
    headers = getAuthHeaders();
  } catch (e) {
    throw e; // re-throw auth errors as-is
  }

  const response = await fetch(urlObj.toString(), {
    method:      'GET',
    headers,
    credentials: 'include',
  });

  if (response.status === 401 || response.status === 403) {
    throw new ESPNAuthError(
      'ESPN auth failed — your ESPN_S2 or SWID may have expired. ' +
      'Go to fantasy.espn.com, log in, and re-copy your cookies.'
    );
  }

  if (!response.ok) {
    throw new ESPNApiError(
      `ESPN API returned ${response.status} for ${urlObj.pathname}`,
      response.status,
    );
  }

  return response.json();
}

// ---------------------------------------------------------------------------
// League info
// ---------------------------------------------------------------------------

/**
 * Fetch basic league info: name, teams, current week, settings.
 * @returns {Promise<{leagueName, teamCount, currentWeek, scoringPeriodId, teams}>}
 */
export async function fetchLeagueInfo() {
  const url  = `${ESPN_BASE}/seasons/${SEASON}/segments/0/leagues/${LEAGUE_ID}`;
  const data = await espnFetch(url, { view: 'mSettings' });

  return {
    leagueName:     data.settings?.name ?? 'Unknown League',
    teamCount:      data.settings?.size ?? 0,
    currentWeek:    data.status?.currentMatchupPeriod ?? 1,
    scoringPeriodId: data.status?.latestScoringPeriod ?? 1,
    teams: (data.teams ?? []).map(t => ({
      id:           t.id,
      name:         `${t.location ?? ''} ${t.nickname ?? ''}`.trim(),
      abbrev:       t.abbrev,
      wins:         t.record?.overall?.wins ?? 0,
      losses:       t.record?.overall?.losses ?? 0,
    })),
  };
}

// ---------------------------------------------------------------------------
// Roster import — the core Step 3 function
// ---------------------------------------------------------------------------

/**
 * Fetch the logged-in manager's roster for a given scoring period.
 * Maps ESPN player IDs → GSIS IDs using id_mapping.js.
 * Returns matched players + unmatched list for manual assignment.
 *
 * Per spec §7.1 — only fetches the logged-in manager's team (no cross-team visibility).
 *
 * @param {number} teamId      - ESPN team ID for the logged-in manager
 * @param {number} scoringPeriodId
 * @returns {Promise<{matched, unmatched, teamName}>}
 */
export async function fetchRoster(teamId, scoringPeriodId) {
  const url  = `${ESPN_BASE}/seasons/${SEASON}/segments/0/leagues/${LEAGUE_ID}`;
  const data = await espnFetch(url, {
    view:            'mRoster',
    scoringPeriodId: scoringPeriodId,
  });

  const team = data.teams?.find(t => t.id === teamId);
  if (!team) {
    throw new ESPNApiError(`Team ID ${teamId} not found in league ${LEAGUE_ID}`);
  }

  const entries = team.roster?.entries ?? [];
  const espnPlayers = entries.map(entry => parseRosterEntry(entry));

  // Separate DST (handled by team abbrev) and skill players (need GSIS mapping)
  const dstPlayers     = espnPlayers.filter(p => p.position === 'DST');
  const skillPlayers   = espnPlayers.filter(p => p.position !== 'DST');
  const kPlayers       = skillPlayers.filter(p => p.position === 'K');
  const nonKSkill      = skillPlayers.filter(p => p.position !== 'K');

  // Map skill players (QB/RB/WR/TE) via GSIS ID
  const espnIds        = nonKSkill.map(p => p.espnId);
  const { matched: gsisMatched, unmatched } = batchEspnToGsis(espnIds);

  // Build matched player records
  const matched = gsisMatched.map(({ espnId, gsisId }) => {
    const espnPlayer = nonKSkill.find(p => String(p.espnId) === String(espnId));
    return {
      ...espnPlayer,
      gsisId,
      source: 'mapped',
    };
  });

  // Add DST players (mapped by team abbrev)
  dstPlayers.forEach(p => {
    const dst = DST_TEAM_MAP[p.team];
    if (dst) {
      matched.push({ ...p, gsisId: dst.gsis_id, source: 'dst' });
    } else {
      unmatched.push(p.espnId);
    }
  });

  // Add kickers — map by name/team fallback (low stakes position)
  kPlayers.forEach(p => {
    matched.push({
      ...p,
      gsisId: `K_${p.team}_${p.espnId}`,  // synthetic key for kickers
      source: 'kicker',
    });
  });

  return {
    teamName: `${team.location ?? ''} ${team.nickname ?? ''}`.trim(),
    teamId,
    scoringPeriodId,
    matched,
    unmatched: unmatched.map(espnId => ({
      espnId,
      player: nonKSkill.find(p => String(p.espnId) === String(espnId)),
    })),
    totalRoster: entries.length,
    mappingCoverage: `${matched.length}/${entries.length}`,
  };
}

/**
 * Parse a single roster entry from ESPN's API response.
 */
function parseRosterEntry(entry) {
  const player      = entry.playerPoolEntry?.player ?? {};
  const playerInfo  = entry.playerPoolEntry ?? {};
  const slotId      = entry.lineupSlotId ?? 20;
  const injuryStatus = playerInfo.injuryStatus ?? 'ACTIVE';

  // ESPN team abbreviation (for DST mapping)
  const proTeamId   = player.proTeamId ?? 0;

  return {
    espnId:          String(player.id ?? ''),
    name:            player.fullName ?? 'Unknown',
    position:        mapESPNPosition(player.defaultPositionId),
    team:            mapESPNTeamId(proTeamId),
    lineupSlot:      ESPN_SLOT_MAP[slotId] ?? 'BENCH',
    onBench:         slotId === 20,
    onIR:            slotId === 21,
    injuryStatus,
    playProbability: ESPN_INJURY_MAP[injuryStatus] ?? 1.0,
    injuryDetail:    injuryStatus !== 'ACTIVE' ? injuryStatus : '',
    seasonAvgPts:    playerInfo.averagePoints ?? 0,
    projectedPts:    entry.playerPoolEntry?.projectedStats?.appliedTotal ?? 0,
  };
}

// ---------------------------------------------------------------------------
// Matchup data
// ---------------------------------------------------------------------------

/**
 * Fetch this week's matchup for a given team.
 * Returns opponent team info and their projected lineup.
 *
 * @param {number} teamId
 * @param {number} scoringPeriodId
 * @returns {Promise<{homeTeam, awayTeam, myScore, oppScore, oppTeamId}>}
 */
export async function fetchMatchup(teamId, scoringPeriodId) {
  const url  = `${ESPN_BASE}/seasons/${SEASON}/segments/0/leagues/${LEAGUE_ID}`;
  const data = await espnFetch(url, {
    view:            'mMatchup',
    scoringPeriodId: scoringPeriodId,
  });

  const schedule = data.schedule ?? [];
  const matchup  = schedule.find(m =>
    m.home?.teamId === teamId || m.away?.teamId === teamId
  );

  if (!matchup) {
    throw new ESPNApiError(`No matchup found for team ${teamId} in week ${scoringPeriodId}`);
  }

  const isHome   = matchup.home?.teamId === teamId;
  const mySlot   = isHome ? matchup.home  : matchup.away;
  const oppSlot  = isHome ? matchup.away  : matchup.home;

  return {
    matchupId:      matchup.id,
    scoringPeriodId,
    myTeamId:       teamId,
    oppTeamId:      oppSlot?.teamId ?? null,
    myProjected:    mySlot?.totalProjectedPointsLive  ?? mySlot?.totalPointsLive  ?? 0,
    oppProjected:   oppSlot?.totalProjectedPointsLive ?? oppSlot?.totalPointsLive ?? 0,
    myActual:       mySlot?.totalPoints  ?? 0,
    oppActual:      oppSlot?.totalPoints ?? 0,
  };
}

// ---------------------------------------------------------------------------
// Player universe (for waiver / free agent lookups)
// ---------------------------------------------------------------------------

/**
 * Fetch the full player universe for a scoring period.
 * Used for replacement-level calculations in the scarcity model.
 *
 * @param {number} scoringPeriodId
 * @returns {Promise<Array>}
 */
export async function fetchPlayerUniverse(scoringPeriodId) {
  const url  = `${ESPN_BASE}/seasons/${SEASON}/segments/0/leagues/${LEAGUE_ID}`;
  const data = await espnFetch(url, {
    view:            'players_wl',
    scoringPeriodId: scoringPeriodId,
  });

  const players = data.players ?? [];
  return players.map(p => ({
    espnId:       String(p.id ?? ''),
    name:         p.fullName ?? 'Unknown',
    position:     mapESPNPosition(p.defaultPositionId),
    onRoster:     p.onTeamId != null,
    avgPoints:    p.averagePoints ?? 0,
    projectedPts: p.projectedStats?.appliedTotal ?? 0,
  }));
}

// ---------------------------------------------------------------------------
// ESPN position ID → position string
// ---------------------------------------------------------------------------

const ESPN_POSITION_MAP = {
  1:  'QB',
  2:  'RB',
  3:  'WR',
  4:  'TE',
  5:  'K',
  16: 'DST',
};

function mapESPNPosition(positionId) {
  return ESPN_POSITION_MAP[positionId] ?? 'UNKNOWN';
}

// ---------------------------------------------------------------------------
// ESPN pro team ID → NFL team abbreviation
// Needed for DST lookup and opponent DEF rank matching
// ---------------------------------------------------------------------------

const ESPN_TEAM_ID_MAP = {
  1:  'ATL', 2:  'BUF', 3:  'CHI', 4:  'CIN', 5:  'CLE',
  6:  'DAL', 7:  'DEN', 8:  'DET', 9:  'GB',  10: 'TEN',
  11: 'IND', 12: 'KC',  13: 'LV',  14: 'LAR', 15: 'MIA',
  16: 'MIN', 17: 'NE',  18: 'NO',  19: 'NYG', 20: 'NYJ',
  21: 'PHI', 22: 'ARI', 23: 'PIT', 24: 'LAC', 25: 'SF',
  26: 'SEA', 27: 'TB',  28: 'WSH', 29: 'CAR', 30: 'JAX',
  33: 'BAL', 34: 'HOU',
};

function mapESPNTeamId(espnTeamId) {
  return ESPN_TEAM_ID_MAP[espnTeamId] ?? 'UNK';
}

// ---------------------------------------------------------------------------
// Convenience: find the logged-in manager's team ID
// ---------------------------------------------------------------------------

/**
 * Find which team ID belongs to the logged-in user.
 * ESPN embeds the SWID in team ownership data — match against stored SWID.
 *
 * @returns {Promise<{teamId, teamName}>}
 */
export async function findMyTeamId() {
  const url  = `${ESPN_BASE}/seasons/${SEASON}/segments/0/leagues/${LEAGUE_ID}`;
  const data = await espnFetch(url, { view: 'mTeam' });
  const swid = sessionStorage.getItem('SWID');

  if (!swid) throw new ESPNAuthError('SWID not set — cannot identify your team');

  const myTeam = data.teams?.find(t =>
    t.owners?.some(o => o === swid || o === `{${swid}}`)
  );

  if (!myTeam) {
    // Fallback: if ownership matching fails, return all teams so user can pick
    return {
      teamId:   null,
      teamName: null,
      allTeams: (data.teams ?? []).map(t => ({
        id:   t.id,
        name: `${t.location ?? ''} ${t.nickname ?? ''}`.trim(),
      })),
    };
  }

  return {
    teamId:   myTeam.id,
    teamName: `${myTeam.location ?? ''} ${myTeam.nickname ?? ''}`.trim(),
    allTeams: null,
  };
}

// ---------------------------------------------------------------------------
// Full import flow — called by RosterSetup.js
// ---------------------------------------------------------------------------

/**
 * Full roster import sequence:
 *   1. Find the manager's team ID from SWID
 *   2. Fetch league info (current week)
 *   3. Fetch roster for current week
 *   4. Fetch this week's matchup
 *
 * This is the single entry point RosterSetup.js calls.
 * Returns everything needed to render the roster confirmation screen.
 *
 * @returns {Promise<ImportResult>}
 */
export async function importLeague() {
  // Step 1 — identify manager's team
  const { teamId, teamName, allTeams } = await findMyTeamId();

  if (!teamId) {
    return { needsTeamSelection: true, allTeams };
  }

  // Step 2 — league info
  const leagueInfo = await fetchLeagueInfo();
  const { scoringPeriodId } = leagueInfo;

  // Step 3 — roster
  const roster = await fetchRoster(teamId, scoringPeriodId);

  // Step 4 — matchup
  const matchup = await fetchMatchup(teamId, scoringPeriodId);

  return {
    needsTeamSelection: false,
    leagueInfo,
    teamId,
    teamName,
    roster,
    matchup,
    importedAt: new Date().toISOString(),
  };
}

export { SEASON, LEAGUE_ID, ESPN_SLOT_MAP, DST_TEAM_MAP };
