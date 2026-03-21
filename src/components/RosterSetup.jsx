// RosterSetup.js — Gridiron Oracle
// ESPN credential entry + roster import + player confirmation
// Step 5 per spec §7.1

import { useState, useCallback } from 'react';
import {
  setESPNCredentials,
  hasESPNCredentials,
  importLeague,
  ESPNAuthError,
  ESPNApiError,
} from '../utils/espn_api.js';

// ---------------------------------------------------------------------------
// Styles — dark utilitarian scouting room aesthetic
// ---------------------------------------------------------------------------

const S = {
  root: {
    minHeight: '100vh',
    background: '#0a0c0f',
    color: '#e8e6e0',
    fontFamily: '"DM Mono", "Fira Mono", "Consolas", monospace',
    padding: '0',
  },
  header: {
    borderBottom: '1px solid #1e2328',
    padding: '24px 40px',
    display: 'flex',
    alignItems: 'baseline',
    gap: '16px',
  },
  wordmark: {
    fontSize: '13px',
    fontWeight: '600',
    letterSpacing: '0.18em',
    textTransform: 'uppercase',
    color: '#c8ff00',    // sharp chartreuse — the one accent color
  },
  wordmarkSub: {
    fontSize: '11px',
    letterSpacing: '0.12em',
    textTransform: 'uppercase',
    color: '#4a5058',
  },
  main: {
    maxWidth: '640px',
    margin: '0 auto',
    padding: '64px 40px',
  },

  // ── Credential form ──
  sectionLabel: {
    fontSize: '10px',
    letterSpacing: '0.20em',
    textTransform: 'uppercase',
    color: '#4a5058',
    marginBottom: '24px',
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
  },
  sectionLine: {
    flex: 1,
    height: '1px',
    background: '#1e2328',
  },
  heading: {
    fontSize: '28px',
    fontWeight: '400',
    color: '#e8e6e0',
    lineHeight: 1.2,
    marginBottom: '8px',
    fontFamily: '"DM Serif Display", "Georgia", serif',
    letterSpacing: '-0.01em',
  },
  subheading: {
    fontSize: '13px',
    color: '#5a6270',
    lineHeight: 1.6,
    marginBottom: '40px',
  },
  fieldGroup: {
    marginBottom: '20px',
  },
  label: {
    display: 'block',
    fontSize: '10px',
    letterSpacing: '0.16em',
    textTransform: 'uppercase',
    color: '#6a7380',
    marginBottom: '8px',
  },
  input: {
    width: '100%',
    background: '#0f1318',
    border: '1px solid #1e2328',
    borderRadius: '4px',
    padding: '12px 14px',
    color: '#e8e6e0',
    fontFamily: '"DM Mono", "Fira Mono", monospace',
    fontSize: '12px',
    outline: 'none',
    boxSizing: 'border-box',
    transition: 'border-color 0.15s',
  },
  inputFocus: {
    borderColor: '#c8ff00',
  },
  hint: {
    fontSize: '11px',
    color: '#3a4048',
    marginTop: '6px',
    lineHeight: 1.5,
  },
  btn: {
    background: '#c8ff00',
    color: '#0a0c0f',
    border: 'none',
    borderRadius: '4px',
    padding: '14px 28px',
    fontSize: '11px',
    fontWeight: '700',
    letterSpacing: '0.16em',
    textTransform: 'uppercase',
    cursor: 'pointer',
    fontFamily: '"DM Mono", "Fira Mono", monospace',
    transition: 'opacity 0.15s',
    marginTop: '8px',
  },
  btnDisabled: {
    opacity: 0.35,
    cursor: 'not-allowed',
  },
  btnSecondary: {
    background: 'transparent',
    color: '#6a7380',
    border: '1px solid #1e2328',
    borderRadius: '4px',
    padding: '10px 20px',
    fontSize: '11px',
    letterSpacing: '0.12em',
    textTransform: 'uppercase',
    cursor: 'pointer',
    fontFamily: '"DM Mono", "Fira Mono", monospace',
    transition: 'border-color 0.15s, color 0.15s',
  },

  // ── Status / error ──
  error: {
    background: '#1a0a0a',
    border: '1px solid #3a1515',
    borderRadius: '4px',
    padding: '14px 16px',
    fontSize: '12px',
    color: '#e06060',
    marginBottom: '24px',
    lineHeight: 1.5,
  },
  statusBar: {
    display: 'flex',
    alignItems: 'center',
    gap: '10px',
    fontSize: '11px',
    color: '#4a5058',
    padding: '16px 0',
  },
  spinner: {
    width: '14px',
    height: '14px',
    border: '2px solid #1e2328',
    borderTopColor: '#c8ff00',
    borderRadius: '50%',
    animation: 'spin 0.8s linear infinite',
  },

  // ── Roster table ──
  table: {
    width: '100%',
    borderCollapse: 'collapse',
    marginTop: '8px',
  },
  th: {
    fontSize: '9px',
    letterSpacing: '0.18em',
    textTransform: 'uppercase',
    color: '#3a4048',
    textAlign: 'left',
    padding: '8px 0',
    borderBottom: '1px solid #1e2328',
    fontWeight: '400',
  },
  tr: {
    borderBottom: '1px solid #12161a',
    transition: 'background 0.1s',
  },
  td: {
    padding: '12px 0',
    fontSize: '12px',
    color: '#c0bdb5',
    verticalAlign: 'middle',
  },
  posTag: {
    display: 'inline-block',
    padding: '2px 6px',
    borderRadius: '3px',
    fontSize: '9px',
    fontWeight: '700',
    letterSpacing: '0.12em',
    marginRight: '8px',
  },
  injuryBadge: {
    display: 'inline-block',
    padding: '2px 7px',
    borderRadius: '3px',
    fontSize: '9px',
    letterSpacing: '0.10em',
    textTransform: 'uppercase',
    fontWeight: '600',
  },
  unmatchedRow: {
    background: '#120d08',
  },
  unmatchedLabel: {
    fontSize: '10px',
    color: '#c87030',
    letterSpacing: '0.10em',
    textTransform: 'uppercase',
  },
  coverageStat: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: '14px 0',
    borderBottom: '1px solid #1e2328',
    fontSize: '12px',
    color: '#5a6270',
  },
  coverageVal: {
    color: '#c8ff00',
    fontWeight: '600',
  },
  confirmRow: {
    display: 'flex',
    gap: '12px',
    marginTop: '32px',
    alignItems: 'center',
  },
};

// Position tag colors
const POS_COLORS = {
  QB:  { bg: '#1a2535', color: '#6ab0ff' },
  RB:  { bg: '#0f2018', color: '#6adf90' },
  WR:  { bg: '#1f1520', color: '#d090f0' },
  TE:  { bg: '#1f1808', color: '#f0b840' },
  K:   { bg: '#1a1a1a', color: '#808080' },
  DST: { bg: '#1a1010', color: '#e06060' },
  FLEX:{ bg: '#1f1520', color: '#c090d0' },
};

// Injury badge colors
const INJ_COLORS = {
  OUT:          { bg: '#2a0808', color: '#e04040' },
  IR:           { bg: '#2a0808', color: '#e04040' },
  DOUBTFUL:     { bg: '#2a1208', color: '#e08030' },
  QUESTIONABLE: { bg: '#1f1a08', color: '#d0b030' },
  GTD:          { bg: '#1f1a08', color: '#d0b030' },
  LIMITED:      { bg: '#1a1f08', color: '#a0c030' },
};

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function PositionTag({ position }) {
  const colors = POS_COLORS[position] ?? { bg: '#1a1a1a', color: '#808080' };
  return (
    <span style={{ ...S.posTag, background: colors.bg, color: colors.color }}>
      {position}
    </span>
  );
}

function InjuryBadge({ status, playProbability }) {
  if (!status || status === 'ACTIVE' || status === 'NORMAL') return null;
  const colors = INJ_COLORS[status] ?? { bg: '#1a1818', color: '#a06060' };
  return (
    <span style={{ ...S.injuryBadge, background: colors.bg, color: colors.color }}>
      {status} ({Math.round((playProbability ?? 1) * 100)}%)
    </span>
  );
}

function SectionLabel({ text }) {
  return (
    <div style={S.sectionLabel}>
      <span>{text}</span>
      <span style={S.sectionLine} />
    </div>
  );
}

function FocusInput({ label, hint, value, onChange, placeholder, type = 'text', monospace = true }) {
  const [focused, setFocused] = useState(false);
  return (
    <div style={S.fieldGroup}>
      <label style={S.label}>{label}</label>
      <input
        type={type}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        style={{
          ...S.input,
          ...(focused ? S.inputFocus : {}),
          fontFamily: monospace ? '"DM Mono", "Fira Mono", monospace' : 'inherit',
        }}
        spellCheck={false}
        autoComplete="off"
      />
      {hint && <div style={S.hint}>{hint}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Roster table
// ---------------------------------------------------------------------------

function RosterTable({ roster }) {
  const { matched = [], unmatched = [] } = roster;

  // Sort: starters first, then bench, IR last
  const slotOrder = ['QB', 'RB', 'WR', 'TE', 'FLEX', 'K', 'DST', 'BENCH', 'IR'];
  const sorted = [...matched].sort((a, b) => {
    return slotOrder.indexOf(a.lineupSlot) - slotOrder.indexOf(b.lineupSlot);
  });

  return (
    <table style={S.table}>
      <thead>
        <tr>
          <th style={{ ...S.th, width: '30px' }}>#</th>
          <th style={S.th}>Player</th>
          <th style={S.th}>Slot</th>
          <th style={{ ...S.th, textAlign: 'right' }}>Avg pts</th>
          <th style={{ ...S.th, textAlign: 'right' }}>Status</th>
        </tr>
      </thead>
      <tbody>
        {sorted.map((player, i) => (
          <tr key={player.gsisId ?? i} style={S.tr}>
            <td style={{ ...S.td, color: '#2a3038', fontSize: '10px' }}>{i + 1}</td>
            <td style={S.td}>
              <PositionTag position={player.position} />
              <span style={{ color: '#e8e6e0' }}>{player.name}</span>
              <span style={{ color: '#2a3038', marginLeft: '8px', fontSize: '11px' }}>
                {player.team}
              </span>
            </td>
            <td style={{ ...S.td, color: '#3a4048', fontSize: '10px', letterSpacing: '0.10em' }}>
              {player.lineupSlot}
            </td>
            <td style={{ ...S.td, textAlign: 'right', color: '#6a7380' }}>
              {(player.seasonAvgPts ?? 0).toFixed(1)}
            </td>
            <td style={{ ...S.td, textAlign: 'right' }}>
              <InjuryBadge
                status={player.injuryStatus}
                playProbability={player.playProbability}
              />
            </td>
          </tr>
        ))}

        {unmatched.map((item, i) => (
          <tr key={`unmatched-${i}`} style={{ ...S.tr, ...S.unmatchedRow }}>
            <td style={{ ...S.td, color: '#2a3038', fontSize: '10px' }}>—</td>
            <td style={S.td}>
              <span style={S.unmatchedLabel}>⚠ unmatched</span>
              <span style={{ color: '#6a5040', marginLeft: '8px', fontSize: '11px' }}>
                ESPN ID: {item.espnId}
              </span>
            </td>
            <td colSpan={3} style={{ ...S.td, color: '#4a3828', fontSize: '11px' }}>
              New callup or edge case — assign manually in next step
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ---------------------------------------------------------------------------
// Step 1 — Credential entry
// ---------------------------------------------------------------------------

function CredentialStep({ onImport }) {
  const [espnS2, setEspnS2] = useState('');
  const [swid,   setSwid]   = useState('');
  const [loading, setLoading] = useState(false);
  const [error,   setError]  = useState(null);

  const canSubmit = espnS2.trim().length > 20 && swid.trim().length > 10;

  const handleImport = useCallback(async () => {
    if (!canSubmit) return;
    setLoading(true);
    setError(null);

    try {
      setESPNCredentials(espnS2.trim(), swid.trim());
      const result = await importLeague();
      onImport(result);
    } catch (err) {
      if (err instanceof ESPNAuthError) {
        setError(`Auth failed: ${err.message}`);
      } else if (err instanceof ESPNApiError) {
        setError(`ESPN API error (${err.status}): ${err.message}`);
      } else {
        setError(`Unexpected error: ${err.message}`);
      }
      setLoading(false);
    }
  }, [espnS2, swid, canSubmit, onImport]);

  return (
    <div>
      <SectionLabel text="01 / credentials" />
      <h1 style={S.heading}>Connect your ESPN league</h1>
      <p style={S.subheading}>
        League 839979 is private — requires your ESPN session cookies.
        These are stored in memory only and cleared when you close the tab.
      </p>

      {error && <div style={S.error}>{error}</div>}

      <FocusInput
        label="ESPN_S2 cookie"
        value={espnS2}
        onChange={setEspnS2}
        placeholder="AEBxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx..."
        hint={
          <>
            Chrome: DevTools → Application → Cookies → fantasy.espn.com → ESPN_S2
          </>
        }
      />

      <FocusInput
        label="SWID cookie"
        value={swid}
        onChange={setSwid}
        placeholder="{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}"
        hint="Same location — the value includes the curly braces"
      />

      {loading ? (
        <div style={S.statusBar}>
          <div style={S.spinner} />
          <span>Connecting to ESPN · League 839979...</span>
        </div>
      ) : (
        <button
          style={{ ...S.btn, ...(canSubmit ? {} : S.btnDisabled) }}
          onClick={handleImport}
          disabled={!canSubmit}
        >
          Import roster →
        </button>
      )}

      <div style={{ marginTop: '40px', borderTop: '1px solid #1e2328', paddingTop: '24px' }}>
        <div style={{ fontSize: '11px', color: '#2a3038', lineHeight: 1.8 }}>
          <div>How to find your cookies in Chrome:</div>
          <div style={{ color: '#3a4048', marginTop: '6px' }}>
            1. Go to fantasy.espn.com and log in<br />
            2. Open DevTools (⌘+Option+I)<br />
            3. Application tab → Storage → Cookies → https://fantasy.espn.com<br />
            4. Copy ESPN_S2 value (long string) and SWID value (with curly braces)
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 2 — Team selection (fallback if SWID matching fails)
// ---------------------------------------------------------------------------

function TeamSelectStep({ allTeams, onSelect }) {
  return (
    <div>
      <SectionLabel text="01b / team selection" />
      <h1 style={S.heading}>Which team is yours?</h1>
      <p style={S.subheading}>
        Couldn't auto-detect your team from SWID. Pick it from the list.
      </p>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '8px' }}>
        {allTeams.map(team => (
          <button
            key={team.id}
            onClick={() => onSelect(team.id)}
            style={{
              ...S.btnSecondary,
              textAlign: 'left',
              padding: '14px 16px',
              fontSize: '13px',
              letterSpacing: '0',
              textTransform: 'none',
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
            }}
          >
            <span>{team.name}</span>
            <span style={{ color: '#3a4048', fontSize: '10px' }}>Team {team.id}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 3 — Roster confirmation
// ---------------------------------------------------------------------------

function RosterConfirmStep({ importResult, onConfirm, onReset }) {
  const { leagueInfo, teamName, roster, matchup } = importResult;
  const { matched = [], unmatched = [], mappingCoverage } = roster;
  const coveragePct = matched.length / (matched.length + unmatched.length) * 100;

  return (
    <div>
      <SectionLabel text="02 / roster confirmation" />
      <h1 style={S.heading}>{teamName}</h1>
      <p style={S.subheading}>
        {leagueInfo?.leagueName} · Week {leagueInfo?.scoringPeriodId} ·{' '}
        {leagueInfo?.teamCount}-team league
      </p>

      {/* Coverage stats */}
      <div style={{ marginBottom: '28px' }}>
        <div style={S.coverageStat}>
          <span>Players imported</span>
          <span style={S.coverageVal}>{matched.length + unmatched.length}</span>
        </div>
        <div style={S.coverageStat}>
          <span>GSIS ID coverage</span>
          <span style={{
            ...S.coverageVal,
            color: coveragePct >= 98 ? '#c8ff00' : '#e08030',
          }}>
            {coveragePct.toFixed(0)}% ({mappingCoverage})
          </span>
        </div>
        {matchup && (
          <div style={S.coverageStat}>
            <span>This week's opponent</span>
            <span style={S.coverageVal}>Team {matchup.oppTeamId}</span>
          </div>
        )}
        {unmatched.length > 0 && (
          <div style={S.coverageStat}>
            <span>Unmatched players</span>
            <span style={{ color: '#c87030' }}>
              {unmatched.length} — new callups, assign manually
            </span>
          </div>
        )}
      </div>

      <RosterTable roster={roster} />

      <div style={S.confirmRow}>
        <button style={S.btn} onClick={onConfirm}>
          Confirm roster →
        </button>
        <button style={S.btnSecondary} onClick={onReset}>
          Re-import
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

const STEPS = {
  CREDENTIALS: 'credentials',
  TEAM_SELECT: 'team_select',
  CONFIRM:     'confirm',
  DONE:        'done',
};

export default function RosterSetup({ onRosterConfirmed }) {
  const [step,         setStep]         = useState(
    hasESPNCredentials() ? STEPS.CREDENTIALS : STEPS.CREDENTIALS
  );
  const [importResult, setImportResult] = useState(null);
  const [allTeams,     setAllTeams]     = useState([]);

  const handleImport = useCallback((result) => {
    if (result.needsTeamSelection) {
      setAllTeams(result.allTeams);
      setStep(STEPS.TEAM_SELECT);
    } else {
      setImportResult(result);
      setStep(STEPS.CONFIRM);
    }
  }, []);

  const handleTeamSelect = useCallback(async (teamId) => {
    // Re-run import with explicit team ID selected
    try {
      const { fetchRoster, fetchLeagueInfo, fetchMatchup } = await import('../utils/espn_api.js');
      const leagueInfo = await fetchLeagueInfo();
      const roster     = await fetchRoster(teamId, leagueInfo.scoringPeriodId);
      const matchup    = await fetchMatchup(teamId, leagueInfo.scoringPeriodId);
      const team       = allTeams.find(t => t.id === teamId);
      setImportResult({
        leagueInfo,
        teamId,
        teamName: team?.name ?? `Team ${teamId}`,
        roster,
        matchup,
        importedAt: new Date().toISOString(),
      });
      setStep(STEPS.CONFIRM);
    } catch (err) {
      console.error('Team select import failed:', err);
    }
  }, [allTeams]);

  const handleConfirm = useCallback(() => {
    setStep(STEPS.DONE);
    onRosterConfirmed?.(importResult);
  }, [importResult, onRosterConfirmed]);

  const handleReset = useCallback(() => {
    setImportResult(null);
    setStep(STEPS.CREDENTIALS);
  }, []);

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Serif+Display&display=swap');
        @keyframes spin { to { transform: rotate(360deg); } }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #0a0c0f; }
        input::placeholder { color: #2a3038; }
        button:hover:not(:disabled) { opacity: 0.85; }
        tr:hover { background: #0d1015 !important; }
      `}</style>

      <div style={S.root}>
        <header style={S.header}>
          <span style={S.wordmark}>Gridiron Oracle</span>
          <span style={S.wordmarkSub}>Fantasy Analytics Engine</span>
          <span style={{ ...S.wordmarkSub, marginLeft: 'auto' }}>
            League 839979
          </span>
        </header>

        <main style={S.main}>
          {step === STEPS.CREDENTIALS && (
            <CredentialStep onImport={handleImport} />
          )}
          {step === STEPS.TEAM_SELECT && (
            <TeamSelectStep allTeams={allTeams} onSelect={handleTeamSelect} />
          )}
          {step === STEPS.CONFIRM && importResult && (
            <RosterConfirmStep
              importResult={importResult}
              onConfirm={handleConfirm}
              onReset={handleReset}
            />
          )}
          {step === STEPS.DONE && (
            <div>
              <SectionLabel text="03 / ready" />
              <h1 style={S.heading}>Roster locked.</h1>
              <p style={S.subheading}>
                {importResult?.roster?.matched?.length} players confirmed.
                Loading lineup optimizer...
              </p>
            </div>
          )}
        </main>
      </div>
    </>
  );
}
