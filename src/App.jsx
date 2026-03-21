// App.jsx — Gridiron Oracle
// ESPN data is fetched via python3 scripts/fetch_espn_roster.py
// which writes src/utils/espn_data.js — no browser login needed.

import LineupOptimizer from './components/LineupOptimizer.jsx';

export default function App() {
  return <LineupOptimizer />;
}
