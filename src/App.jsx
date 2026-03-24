// App.jsx — Gridiron Oracle
// Passcode gate → TeamContext → all views

import { useState } from 'react';
import { TeamProvider, useTeam } from './utils/TeamContext.jsx';
import PasscodeEntry    from './components/PasscodeEntry.jsx';
import LineupOptimizer  from './components/LineupOptimizer.jsx';
import MatchupExplorer  from './components/MatchupExplorer.jsx';
import LeagueHome       from './components/LeagueHome.jsx';
import InjuryDashboard  from './components/InjuryDashboard.jsx';
import WaiverWire       from './components/WaiverWire.jsx';
import TradeAnalyzer    from './components/TradeAnalyzer.jsx';

const VIEWS = {
  LEAGUE:  'league',
  LINEUP:  'lineup',
  MATCHUP: 'matchup',
  WAIVER:  'waiver',
  TRADE:   'trade',
  INJURY:  'injury',
};

const font = '"DM Mono", "Fira Mono", monospace';
const C    = { bg: '#1a1d23', border: '#333a45', accent: '#c8ff00', textDim: '#6a7585', text: '#a8b0bc' };

function Nav({ view, setView, onLogout }) {
  return (
    <div style={{
      position: 'fixed', bottom: '24px', left: '50%',
      transform: 'translateX(-50%)',
      display: 'flex', gap: '4px',
      background: C.bg,
      border: `1px solid ${C.border}`,
      borderRadius: '8px',
      padding: '4px',
      zIndex: 100,
      boxShadow: '0 4px 24px rgba(0,0,0,0.4)',
    }}>
      {[
        { key: VIEWS.LEAGUE,  label: 'League'  },
        { key: VIEWS.LINEUP,  label: 'Lineup'  },
        { key: VIEWS.MATCHUP, label: 'Matchup' },
        { key: VIEWS.WAIVER,  label: 'Waiver'  },
        { key: VIEWS.TRADE,   label: 'Trade'   },
        { key: VIEWS.INJURY,  label: 'Injury'  },
      ].map(({ key, label }) => (
        <button
          key={key}
          onClick={() => setView(key)}
          style={{
            background:    view === key ? C.accent : 'transparent',
            color:         view === key ? '#0a0c0f' : C.text,
            border:        'none',
            borderRadius:  '5px',
            padding:       '8px 14px',
            fontSize:      '11px',
            fontWeight:    view === key ? '700' : '400',
            letterSpacing: '0.12em',
            textTransform: 'uppercase',
            cursor:        'pointer',
            fontFamily:    font,
            transition:    'all 0.15s',
          }}
        >
          {label}
        </button>
      ))}
      <div style={{ width: '1px', background: C.border, margin: '4px 2px' }} />
      <button
        onClick={onLogout}
        title="Switch team"
        style={{
          background: 'transparent', color: C.textDim, border: 'none',
          borderRadius: '5px', padding: '8px 10px', fontSize: '11px',
          cursor: 'pointer', fontFamily: font, transition: 'color 0.15s',
        }}
      >
        ⏏
      </button>
    </div>
  );
}

function AppInner() {
  const { logout } = useTeam();
  const [view, setView] = useState(VIEWS.LINEUP);

  return (
    <>
      {view === VIEWS.LEAGUE  && <LeagueHome />}
      {view === VIEWS.LINEUP  && <LineupOptimizer />}
      {view === VIEWS.MATCHUP && <MatchupExplorer onBack={() => setView(VIEWS.LINEUP)} />}
      {view === VIEWS.WAIVER  && <WaiverWire />}
      {view === VIEWS.TRADE   && <TradeAnalyzer />}
      {view === VIEWS.INJURY  && <InjuryDashboard />}
      <Nav view={view} setView={setView} onLogout={logout} />
    </>
  );
}

function AppRoot() {
  const { teamId, login } = useTeam();
  if (!teamId) return <PasscodeEntry onSuccess={login} />;
  return <AppInner />;
}

export default function App() {
  return (
    <TeamProvider>
      <AppRoot />
    </TeamProvider>
  );
}
