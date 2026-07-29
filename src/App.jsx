// App.jsx — Gridiron Oracle
// Passcode gate → TeamContext → all views

import { useState } from 'react';
import { TeamProvider, useTeam } from './utils/TeamContext.jsx';
import { useMobile } from './utils/useMobile.js';
import PasscodeEntry    from './components/PasscodeEntry.jsx';
import LineupOptimizer  from './components/LineupOptimizer.jsx';
import MatchupExplorer  from './components/MatchupExplorer.jsx';
import LeagueHome       from './components/LeagueHome.jsx';
import InjuryDashboard  from './components/InjuryDashboard.jsx';
import WaiverWire       from './components/WaiverWire.jsx';
import TradeAnalyzer    from './components/TradeAnalyzer.jsx';
import { C, font } from './utils/theme.js';

const VIEWS = {
  LEAGUE:  'league',
  LINEUP:  'lineup',
  MATCHUP: 'matchup',
  WAIVER:  'waiver',
  TRADE:   'trade',
  INJURY:  'injury',
};


// Nav labels — shorter on mobile
const NAV_ITEMS = [
  { key: VIEWS.LEAGUE,  label: 'League',  short: 'Lgue' },
  { key: VIEWS.LINEUP,  label: 'Lineup',  short: 'Lnup' },
  { key: VIEWS.MATCHUP, label: 'Matchup', short: 'Mtch' },
  { key: VIEWS.WAIVER,  label: 'Waiver',  short: 'Wvr'  },
  { key: VIEWS.TRADE,   label: 'Trade',   short: 'Trd'  },
  { key: VIEWS.INJURY,  label: 'Injury',  short: 'Inj'  },
];

function Nav({ view, setView, onLogout }) {
  const { isMobile, isNarrow } = useMobile();

  return (
    <div
      data-nav="main"
      style={{
        position:     'fixed',
        bottom:       '16px',
        left:         '50%',
        transform:    'translateX(-50%)',
        display:      'flex',
        gap:          isMobile ? '2px' : '4px',
        background:   C.bg,
        border:       `1px solid ${C.border}`,
        borderRadius: '8px',
        padding:      '3px',
        zIndex:       100,
        boxShadow:    '0 4px 24px rgba(0,0,0,0.4)',
        maxWidth:     'calc(100vw - 16px)',
        overflowX:    'auto',
      }}
    >
      {NAV_ITEMS.map(({ key, label, short }) => (
        <button
          key={key}
          onClick={() => setView(key)}
          style={{
            background:    view === key ? C.accent : 'transparent',
            color:         view === key ? '#0a0c0f' : C.text,
            border:        'none',
            borderRadius:  '5px',
            padding:       isMobile ? '7px 10px' : '8px 14px',
            fontSize:      isMobile ? '10px' : '11px',
            fontWeight:    view === key ? '700' : '400',
            letterSpacing: '0.10em',
            textTransform: 'uppercase',
            cursor:        'pointer',
            fontFamily:    font,
            transition:    'all 0.15s',
            whiteSpace:    'nowrap',
            flexShrink:    0,
          }}
        >
          {isNarrow ? short : label}
        </button>
      ))}
      <div style={{ width: '1px', background: C.border, margin: isMobile ? '3px 1px' : '4px 2px', flexShrink: 0 }} />
      <button
        onClick={onLogout}
        title="Switch team"
        style={{
          background:    'transparent',
          color:         C.textDim,
          border:        'none',
          borderRadius:  '5px',
          padding:       isMobile ? '7px 8px' : '8px 10px',
          fontSize:      '11px',
          cursor:        'pointer',
          fontFamily:    font,
          transition:    'color 0.15s',
          flexShrink:    0,
        }}
      >
        ⏏
      </button>
    </div>
  );
}

function AppInner({ isCommissioner = false }) {
  const { logout } = useTeam();
  const [view, setView] = useState(isCommissioner ? VIEWS.LEAGUE : VIEWS.LINEUP);

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
  return <AppInner isCommissioner={teamId === 'COMMISSIONER'} />;
}

export default function App() {
  return (
    <TeamProvider>
      <AppRoot />
    </TeamProvider>
  );
}
