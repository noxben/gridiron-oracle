# Phase 1 implementation

Replace the old rating calculation with:

1. `src/model/ratings.py`
2. Import `compute_player_ratings()` from that module in `scripts/update_nfl_data.py`.
3. Replace the existing `compute_composite_ratings()` implementation with a thin wrapper:
   `return compute_player_ratings(df)`
4. In `simulator.js`, replace the old `computeCompositeRating(player, allPlayers)` implementation with the function in `simulator_phase1_rating_contract.js`.
5. Existing callers can continue passing `allPlayers` if changing every call immediately is inconvenient. The second argument is simply ignored during migration.
6. Do not change `projectPoints()`, variance, replacement-level logic, or Monte Carlo logic in this phase.

### Important

Do not delete the existing UI-facing score fields. `LineupOptimizer.jsx` already consumes them.

After regenerating `nfl_data.js`, verify:

- `composite_rating` is present.
- `epa_score`, `usage_score`, `snap_score`, `red_zone_score` are present.
- values are 0-100.
- simulator output uses those exact values.

This phase intentionally leaves team environment and matchup modeling alone. Those are separate model layers and should not be smuggled into the player-quality score just because humans enjoy putting everything into one number.
