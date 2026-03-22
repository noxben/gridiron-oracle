// App.jsx — Gridiron Oracle
// ESPN data fetched via: python3 scripts/fetch_espn_roster.py
// League data fetched via: python3 scripts/fetch_espn_league.py

import { useState } from 'react';
import LineupOptimizer  from './components/LineupOptimizer.jsx';
import MatchupExplorer  from './components/MatchupExplorer.jsx';
import LeagueHome       from './components/LeagueHome.jsx';

const VIEWS = {
  LINEUP:  'lineup',
  MATCHUP: 'matchup',
  LEAGUE:  'league',
};

const font = '"DM Mono", "Fira Mono", monospace';
const C    = { bg: '#1a1d23', border: '#333a45', accent: '#c8ff00', textDim: '#6a7585', text: '#a8b0bc' };

function Nav({ view, setView }) {
  return (
    <div style={{
      position:   'fixed', bottom: '24px', left: '50%',
      transform:  'translateX(-50%)',
      display:    'flex', gap: '4px',
      background: C.bg,
      border:     `1px solid ${C.border}`,
      borderRadius: '8px',
      padding:    '4px',
      zIndex:     100,
      boxShadow:  '0 4px 24px rgba(0,0,0,0.4)',
    }}>
      {[
        { key: VIEWS.LEAGUE,  label: 'League'  },
        { key: VIEWS.LINEUP,  label: 'Lineup'  },
        { key: VIEWS.MATCHUP, label: 'Matchup' },
      ].map(({ key, label }) => (
        <button
          key={key}
          onClick={() => setView(key)}
          style={{
            background:    view === key ? C.accent : 'transparent',
            color:         view === key ? '#0a0c0f' : C.text,
            border:        'none',
            borderRadius:  '5px',
            padding:       '8px 20px',
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
    </div>
  );
}

export default function App() {
  const [view, setView] = useState(VIEWS.LINEUP);

  return (
    <>
      {view === VIEWS.LEAGUE  && <LeagueHome />}
      {view === VIEWS.LINEUP  && <LineupOptimizer />}
      {view === VIEWS.MATCHUP && <MatchupExplorer onBack={() => setView(VIEWS.LINEUP)} />}
      <Nav view={view} setView={setView} />
    </>
  );
}
