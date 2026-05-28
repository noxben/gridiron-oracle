# Gridiron Oracle

Fantasy football analytics engine for ESPN private leagues. Applies EPA-based composite player ratings, Monte Carlo simulation (10,000 runs), opponent-adjusted projections, injury probability weighting, and manual eye-test overrides to weekly lineup decisions.

**Current status:** v0.1 deployed — lineup optimizer and matchup explorer working against league 839979 (#siscocks). v2.0 in progress — full league data, waiver wire, trade analyzer, multi-user.

---

## What it does

- **Lineup optimizer** — recommends the optimal starting lineup based on composite ratings (EPA, usage, snap %, red zone share, opponent DEF rank). Shows win probability, score distribution (p10/p50/p90), and per-player VORP.
- **Win probability** — 10,000 Monte Carlo simulations per matchup. Injury probability rolls per player per sim. Results in under 8 seconds.
- **Matchup explorer** — head-to-head player comparison with stat bars, opponent DEF rank by position.
- **Override sliders** — manual eye-test adjustments (-150 to +150) that shift projections and re-run the sim in real time.

---

## Architecture

Three strictly separated layers:

```
Layer 1 — Data Pipeline (Python scripts, run on schedule)
          ↓ writes static .js files
Layer 2 — Simulation Engine (simulator.js, Monte Carlo)
          ↓ reads static files, runs in browser
Layer 3 — UI (React/Vite, deployed on Vercel)
```

The Python scripts never run in production — they run locally or via GitHub Actions and commit static data files to the repo. The React app reads those files at build time. No backend, no database, no API calls from the browser (except ESPN, which is handled via Python).

---

## Tech stack

| Layer | Tech |
|---|---|
| Frontend | React + Vite |
| Deployment | Vercel (auto-deploy from main) |
| Data pipeline | Python 3.12, espn_api, nfl_data_py, pandas |
| NFL stats | nflfastR via nfl_data_py |
| ESPN data | espn_api Python library |
| ID mapping | nfl_data_py import_ids() — ESPN → GSIS |
| Simulation | Vanilla JS, Float32Array, chunked setTimeout |

---

## Project structure

```
gridiron-oracle/
├── src/
│   ├── components/
│   │   ├── LineupOptimizer.jsx    # Main view — lineup, win prob, overrides
│   │   ├── MatchupExplorer.jsx    # Head-to-head comparison, DEF ranks
│   │   ├── RosterSetup.jsx        # ESPN credential entry (legacy)
│   │   ├── PlayerCard.jsx         # Individual player deep-dive
│   │   └── ManualOverrides.jsx    # Override slider panel
│   ├── utils/
│   │   ├── espn_data.js           # AUTO-GENERATED — your ESPN roster data
│   │   ├── nfl_data.js            # AUTO-GENERATED — nflfastR player stats
│   │   ├── id_mapping.js          # AUTO-GENERATED — ESPN → GSIS ID lookup
│   │   ├── espn_api.js            # ESPN API integration (browser)
│   │   └── simulator.js           # Monte Carlo engine, VORP, composite ratings
│   ├── App.jsx                    # Routing + nav
│   └── main.jsx
├── scripts/
│   ├── fetch_espn_roster.py       # Pull your roster from ESPN → espn_data.js
│   ├── update_nfl_data.py         # Pull nflfastR stats → nfl_data.js
│   ├── build_id_mapping.py        # Build ESPN → GSIS ID table → id_mapping.js
│   ├── injury_overlay.py          # Patch injury status → nfl_data.js (fast update)
│   └── validate_data.py           # Gate script — must pass before every deploy
├── .github/
│   └── workflows/
│       └── nfl_data_pipeline.yml  # GitHub Actions — auto Tue/Thu data refresh
├── .env                           # ESPN_S2 and SWID — never committed
├── .vercelignore                  # Excludes Python scripts from Vercel build
├── vercel.json
└── requirements.txt
```

---

## Setup

### Prerequisites

- Node.js 18+
- Python 3.12+
- ESPN private league credentials (ESPN_S2 + SWID cookies)

### Install

```bash
# Clone
git clone git@github.com:noxben/gridiron-oracle.git
cd gridiron-oracle

# Frontend dependencies
npm install

# Python dependencies
pip3 install espn_api nfl_data_py pandas python-dotenv
```

### Configure credentials

Create a `.env` file in the project root — **never commit this file**:

```
ESPN_S2=your_espn_s2_cookie_value_here
SWID={your_swid_cookie_value_here}
```

To find your ESPN cookies:
1. Go to `fantasy.espn.com` and log in
2. Open DevTools → Application → Cookies → `fantasy.espn.com`
3. Copy `ESPN_S2` (long string ~300 chars) and `SWID` (with curly braces)

---

## Running the data pipeline

Run these scripts before starting the dev server. They write the static data files the app reads.

```bash
# Build ESPN → GSIS ID mapping (run once per season)
python3 scripts/build_id_mapping.py --season 2025

# Pull your ESPN roster
python3 scripts/fetch_espn_roster.py

# Pull nflfastR stats (takes ~30 seconds)
python3 scripts/update_nfl_data.py --season 2025

# Validate before deploying
python3 scripts/validate_data.py
```

For quick injury updates (Thursday / Sunday morning):
```bash
python3 scripts/injury_overlay.py
```

---

## Development

```bash
npm run dev
# → http://localhost:5173
```

The app reads from `src/utils/espn_data.js` and `src/utils/nfl_data.js`. If those files are empty stubs, the sim runs on ESPN projected points as a fallback.

---

## Deployment

Auto-deploys to Vercel on every push to `main`. The pipeline:

1. Run data scripts locally → updates `espn_data.js` and/or `nfl_data.js`
2. `git add src/utils/espn_data.js && git commit -m "data: week X refresh"`
3. `git push` → Vercel picks up the change and redeploys in ~30 seconds

For automated data refresh (nflfastR stats only), GitHub Actions runs the pipeline every Tuesday and Thursday via `.github/workflows/nfl_data_pipeline.yml`.

---

## Data sources

| Source | What it provides | Cost |
|---|---|---|
| nflfastR (via nfl_data_py) | EPA per play, target/carry share, air yards, DEF ranks | Free |
| ESPN API (via espn_api) | Rosters, matchups, injury status, projected points | Free |
| nfl_data_py ID table | ESPN player ID → GSIS ID mapping | Free |
| open-meteo.com | Game-day weather (v2.0) | Free |

---

## Key design decisions

**GSIS ID as primary key — never name strings.** Every player lookup uses the GSIS ID from nflfastR. ESPN IDs are mapped to GSIS IDs at import time via `id_mapping.js`. This prevents the name-matching failures that plagued v0.1.

**Static files, no backend.** The Python scripts run on a schedule and commit data files to git. The React app reads those files — no API calls at runtime, no database, no server. Fast, simple, cheap to host.

**Monte Carlo in chunks.** 10,000 simulations run in 500-sim chunks via `setTimeout` to avoid blocking the browser UI thread. Results arrive in under 8 seconds on a modern machine.

**Injury probability roll, not binary.** In each simulation, each player's score is sampled normally with probability `play_probability`. A player with `play_probability = 0.55` (Questionable) plays in ~55% of simulations — contributing replacement-level points in the other 45%. This produces a realistic distribution rather than a point estimate.

---

## Roadmap

| Phase | Focus | Status |
|---|---|---|
| v0.1 | Lineup optimizer, win probability, matchup explorer | ✅ Deployed |
| v2.0 | Full league data, waiver wire, trade analyzer, multi-user | 🔨 In progress |
| v3.0 | Playoff simulator, schedule analyzer, power rankings | 📋 Planned |
| v4.0 | Multi-league, invite-link auth, commercialization | 📋 Planned |

---

## League

Private ESPN league — #siscocks (ID: 839979) — 12 teams, PPR scoring, standard lineup.

---

*Gridiron Oracle · Built March 2026 · Confidential*
