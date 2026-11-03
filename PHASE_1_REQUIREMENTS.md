# FFL Oracle — Phase 1 Requirements
## Centralized Player Ratings + Single Source of Truth

### Objective
Establish a stable, transparent rating layer for Gridiron Oracle before changing projections or Monte Carlo simulation.

Phase 1 deliberately does **not** attempt to reproduce proprietary QBERT/ELWAY formulas. It creates Oracle-native ratings from available NFL data and makes Python the single source of truth.

### Architecture

NFL raw data
→ Python feature/rating pipeline
→ `nfl_data.js`
→ JavaScript simulator
→ fantasy projection
→ Monte Carlo simulation

### Phase 1 scope

1. Python computes player component scores.
2. Component normalization is position-relative rather than one global min/max scale.
3. Missing values are treated as neutral rather than automatically as zero.
4. Python computes and stores the authoritative `composite_rating`.
5. JavaScript consumes those stored ratings instead of recalculating them.
6. Existing fields remain available for UI compatibility:
   - `epa_score`
   - `usage_score`
   - `snap_score`
   - `red_zone_score`
   - `composite_rating`
7. Existing projection, variance, replacement-level and Monte Carlo formulas remain unchanged.
8. Existing hardcoded team-system and defense-rank adjustments are not allowed to overwrite the Python rating in Phase 1. They remain identified as later migration work.

### Rating components

For QB/RB/WR/TE:
- EPA score
- Usage score
- Snap score
- Red-zone score

Usage:
- QB: temporary EPA-based proxy retained for compatibility. A true QB opportunity model is Phase 5/6 work.
- RB: carry share
- WR/TE: target share

K and DST:
- retain neutral/fallback component values until position-specific models are added.

### Normalization

Use percentile/rank-based scoring within position instead of min-max normalization.

Target:
- 0–100 scale
- median-ish player ≈ 50
- resistant to extreme outliers
- comparable within a position

Missing data:
- neutral score rather than a zero-performance penalty.

### Composite weights

Keep current Python weights during Phase 1 to minimize model drift:

Default:
- EPA 35%
- Usage 30%
- Snap 20%
- Red zone 15%

WR/TE:
- EPA 30%
- Usage 35%
- Snap 20%
- Red zone 15%

RB:
- EPA 35%
- Usage 30%
- Snap 15%
- Red zone 20%

These weights are configuration, not permanent truth. They will be validated later.

### JavaScript contract

The simulator should treat these fields from `nfl_data.js` as authoritative:

```js
{
  epa_score,
  usage_score,
  snap_score,
  red_zone_score,
  composite_rating
}
```

The simulator may read these values, but should not independently normalize raw EPA/usage/snap/red-zone values.

### Explicitly deferred

- Team Environment / ELWAY-like team context
- Continuous defensive efficiency score
- Matchup score
- QB-specific efficiency model
- Sample-size shrinkage for EPA
- Historical backtesting/calibration
- Projection formula redesign
- Monte Carlo redesign
- Removal of legacy `opp_def_rank`
- Removal of hardcoded system adjustments

### Acceptance criteria

Phase 1 is complete when:

- Running the Python data update produces valid 0–100 component scores.
- Every relevant player has a single Python-generated `composite_rating`.
- Running the simulator with identical `nfl_data.js` produces the same component scores and composite rating.
- No JavaScript code recalculates player ratings from raw component data.
- `LineupOptimizer.jsx` continues receiving the existing score fields without UI changes.
- Projection and simulation outputs otherwise remain structurally compatible.

### Files

New architecture:
- `src/model/ratings.py` — reusable rating/normalization logic
- `scripts/update_nfl_data.py` — data acquisition and record assembly
- `src/utils/nfl_data.js` — generated data contract
- `src/model/simulator.js` — simulation/projection consumer

The first implementation should isolate rating logic from data acquisition rather than making `update_nfl_data.py` grow into an enormous football-data swamp, because apparently one file was not enough suffering already.
