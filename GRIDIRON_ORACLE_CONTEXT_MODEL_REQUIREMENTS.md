# Gridiron Oracle — Contextual Rating & Projection Model
## Requirements Specification v1.0

**Status:** Proposed / implementation baseline
**Date:** 2026-09-10
**Scope:** Player Quality, Team Environment, Matchup Context, Projection, and eventual Simulation integration

---

## 1. Purpose

Gridiron Oracle currently combines player EPA, usage, snap participation, red-zone role, recent fantasy production, and opponent defensive rank into a single composite rating that influences fantasy projections.

The next model iteration will separate these concepts into distinct layers:

1. **Player Quality** — how well the player performs independent of the weekly opponent.
2. **Opportunity / Role** — how much and in what situations the player is being used.
3. **Team Environment** — the quality and expected fantasy environment of the player's offense/team.
4. **Matchup** — how the opposing defense changes the player's expectation.
5. **Projection** — the final expected fantasy output after combining the above.
6. **Simulation** — the distribution of outcomes around the projection.

The objective is not to reproduce Nate Silver's QBERT or ELWAY models. Those models are proprietary and their exact formulas are not sufficiently observable to validate a faithful recreation. They are useful architectural references: QBERT represents the idea of a richer quarterback-quality model, while ELWAY represents contextual team/game strength.

Oracle will build its own transparent, testable equivalents optimized for fantasy football decisions.

---

# 2. Design Principles

### 2.1 Separate signal from interpretation

Raw NFL statistics must remain distinguishable from derived ratings.

Example:

`epa_per_play` is a raw/derived football statistic.

`player_quality` is an Oracle interpretation of multiple signals.

`projected_points` is a fantasy output.

These must not be treated as interchangeable.

### 2.2 Player quality must not contain matchup

A player's underlying quality must not change simply because his opponent changes.

Bad architecture:

`Player Quality + Opponent = Composite Player Quality`

Required architecture:

`Player Quality + Team Environment + Matchup = Projection`

### 2.3 Opportunity is not talent

A player can be highly efficient but have little opportunity.

A mediocre player can have enormous fantasy value because he receives enormous opportunity.

Oracle must preserve these as separate concepts.

### 2.4 Ratings must be stable across weeks

The current min/max normalization approach makes scores dependent on the current player population.

The new rating system should use stable, position-aware normalization so that a player's score has approximately consistent meaning from week to week.

### 2.5 Do not create unsupported metrics

A metric must only enter the model when Oracle has a reliable source for it.

If CPOE, pressure rate, sack rate, success rate, route participation, or another metric is unavailable, the model must not invent or silently approximate it.

### 2.6 Backward compatibility

The first implementation must continue producing the existing fields consumed by the frontend and auction model.

The new model should initially run alongside the old model.

### 2.7 Validate before replacing

The new model must be compared against the existing model before becoming the production projection driver.

More complexity is not evidence of improvement.

---

# 3. Current Model Baseline

The existing data pipeline currently provides, among other fields:

- `epa_per_play`
- `target_share`
- `carry_share`
- `snap_pct`
- `red_zone_share`
- `air_yards_share`
- `season_avg_pts`
- `last3_avg_pts`
- `opp_def_rank`
- `play_probability`
- injury information
- rushing/receiving/passing touchdown totals
- existing component scores
- `composite_rating`

The current system also exposes component scores for:

- EPA
- Usage
- Snap
- Red Zone

The existing projection system uses recent and season fantasy production and adjusts it using the composite rating.

The existing auction model also consumes the composite rating and opportunity score, so the initial implementation must preserve those interfaces.

---

# 4. Target Architecture

```text
                         RAW DATA
                            |
              +-------------+-------------+
              |                           |
              v                           v
       PLAYER SIGNALS              TEAM/GAME SIGNALS
              |                           |
              v                           v
       PLAYER QUALITY             TEAM ENVIRONMENT
              |                           |
              +-------------+-------------+
                            |
                            v
                         MATCHUP
                            |
                            v
                       OPPORTUNITY
                            |
                            v
                       PROJECTION
                            |
                 +----------+----------+
                 |                     |
                 v                     v
              VORP / VALUE        SIMULATION
```

The layers must remain logically separable even if implementation is eventually consolidated for performance.

---

# 5. Raw Signal Layer

## 5.1 Existing player signals

The new model should initially reuse the reliable existing fields wherever possible:

| Signal | Purpose |
|---|---|
| `epa_per_play` | Player efficiency / impact |
| `target_share` | Receiving opportunity |
| `carry_share` | Rushing opportunity |
| `snap_pct` | Role and participation |
| `red_zone_share` | High-value scoring opportunity |
| `air_yards_share` | Receiving depth/opportunity |
| `season_avg_pts` | Long-run fantasy baseline |
| `last3_avg_pts` | Recent form |
| `play_probability` | Availability |
| injury data | Availability/context |

## 5.2 Future signals

The architecture should permit addition of:

### Quarterback
- CPOE
- pass success rate
- sack rate
- pressure rate
- explosive pass rate
- rushing EPA/value
- turnover-related efficiency indicators

### Running back
- rushing success rate
- rushing EPA
- explosive-run rate
- receiving efficiency
- route participation
- goal-line role

### WR / TE
- receiving EPA
- receiving success rate
- target efficiency
- route participation
- explosive-play rate
- first-down conversion
- red-zone role

These are future additions unless reliable data is already present.

---

# 6. Player Quality

Player Quality answers:

> How good is this player independent of this week's opponent?

It must be position-aware.

## 6.1 QB Quality

Initial conceptual components:

- EPA / dropback
- efficiency
- success rate where available
- sack avoidance where available
- explosive passing
- rushing contribution
- consistency/sample-size adjustment

QB Quality must not simply duplicate EPA under multiple labels.

## 6.2 RB Quality

Initial conceptual components:

- rushing efficiency
- rushing EPA
- rushing success
- explosive ability
- receiving efficiency
- role stability

## 6.3 WR / TE Quality

Initial conceptual components:

- receiving efficiency
- receiving EPA
- explosive ability
- target efficiency
- role stability

---

# 7. Opportunity / Role

Opportunity answers:

> How much fantasy-relevant work is this player likely to receive?

It should remain distinct from Player Quality.

Potential components:

- target share
- carry share
- snap percentage
- route participation
- red-zone share
- goal-line role
- recent role trend

The existing `opportunityScore` interface should remain available for downstream systems.

---

# 8. Team Environment

Team Environment answers:

> How favorable is the player's team/game environment for fantasy production?

Initial components should be based on reliable team-level data:

- offensive EPA/play
- offensive success rate
- defensive EPA/play
- defensive success rate
- play volume / pace
- scoring efficiency
- recent form
- opponent-adjusted team strength where supportable

The final Team Environment score should be position-aware where appropriate.

For example, team passing environment matters more directly to QB/WR/TE projection than RB rushing volume.

---

# 9. Matchup

Matchup answers:

> How does this particular opponent alter the player's expected performance?

The current `opp_def_rank` should be treated as a matchup input, not a component of Player Quality.

Future matchup improvements may include:

- opponent defensive EPA
- opponent success rate
- pressure rate
- position-specific efficiency allowed
- explosive plays allowed
- red-zone defense
- pace interaction

The system should avoid double-counting the same defensive information in both Team Environment and Matchup.

---

# 10. Normalization

## 10.1 Required change

The existing population min/max normalization should not be the long-term basis for Oracle ratings.

Required target:

```text
raw metric
    ->
position-specific reference distribution
    ->
normalized value
    ->
0–100 Oracle score
```

## 10.2 Requirements

Normalization must:

- be position-aware where appropriate
- be robust to extreme outliers
- handle missing values
- handle small samples
- produce bounded scores
- be reproducible
- remain reasonably stable across weekly updates
- avoid changing a player's score solely because an unrelated player entered or left the dataset

## 10.3 Small samples

Early-season or low-usage players require shrinkage toward an appropriate baseline.

No player should receive an elite efficiency rating from a handful of plays without an explicit sample-size adjustment.

---

# 11. Composite Rating

`composite_rating` is retained during the transition period.

It should eventually become a summary representation rather than the hidden variable controlling the entire projection engine.

Target conceptual structure:

```text
Player Quality
+
Opportunity / Role
+
Team Environment
+
Matchup
=
Projection
```

`composite_rating` may eventually be calculated as a human-readable summary of Player Quality and Opportunity, but it must not absorb matchup information.

---

# 12. Projection Model

The existing model uses season and recent fantasy scoring as a baseline.

The new projection system should preserve a historical fantasy baseline while incorporating contextual signals.

Conceptually:

```text
Base Fantasy Expectation
        +
Player Quality adjustment
        +
Opportunity adjustment
        +
Team Environment adjustment
        +
Matchup adjustment
        +
Availability adjustment
        =
Expected Fantasy Points
```

Adjustments must be bounded.

No single component should be capable of producing extreme projections by itself.

The model should explicitly guard against mathematical explosions such as unrealistic auction values or fantasy projections.

---

# 13. Simulation

The Monte Carlo simulator remains downstream of the deterministic projection model.

Initial implementation:

- keep the existing simulation architecture
- continue supporting player variance profiles
- continue supporting injury/play probability
- continue supporting opponent rosters
- continue supporting lineup optimization

Later improvements may derive variance from contextual factors.

Examples:

- stable role + stable environment → narrower distribution
- high efficiency + uncertain opportunity → wider distribution
- high-scoring environment + volatile role → wider ceiling
- poor matchup → lower median without necessarily destroying ceiling

Variance changes must be validated separately from projection accuracy.

---

# 14. Backward Compatibility

The first release must continue to expose:

```text
composite_rating
epa_score
usage_score
snap_score
red_zone_score
opp_def_rank
projected_points
opportunityScore
vorp
```

Existing frontend consumers should not need to be rewritten during the raw-signal implementation phase.

New fields may be added:

```text
player_quality
player_quality_score
team_environment
team_environment_score
matchup_score
```

Exact naming should be finalized before implementation.

---

# 15. Data Quality Requirements

Every new metric must have:

1. Defined source.
2. Defined calculation.
3. Defined time window.
4. Defined missing-value behavior.
5. Defined sample-size behavior.
6. Defined normalization method.
7. Defined validation range.
8. Unit tests where practical.

The generated `nfl_data.js` remains a generated artifact and must not be manually edited.

The existing GSIS ID remains the primary player key.

---

# 16. Testing & Validation

## 16.1 Unit tests

Test:

- normalization
- missing values
- small samples
- position handling
- bounded adjustments
- score ranges
- matchup isolation
- projection calculations

## 16.2 Data validation

Every pipeline run should validate:

- unique player IDs
- valid probabilities
- valid score ranges
- valid positions
- valid team IDs
- expected field presence

## 16.3 Historical backtest

Before replacing the existing projection model, compare old vs new using historical weekly data.

Primary evaluation metrics should include:

- MAE
- RMSE
- rank correlation
- hit rate for start/sit decisions where definable
- calibration of predicted ranges
- fantasy points above/below projection

For simulation:

- win-probability calibration
- percentile calibration
- distribution coverage

The new model is accepted only if it demonstrates meaningful improvement or provides a clearly justified capability that the existing model cannot provide.

---

# 17. Implementation Phases

## Phase 1 — Raw signal and normalization foundation

- Audit the exact current data schema.
- Centralize normalization.
- Add new raw signals only where data is available.
- Preserve existing output fields.
- Add tests.
- Do not change production projections yet.

## Phase 2 — Player Quality

- Create position-specific Player Quality calculations.
- Separate performance from opportunity.
- Build QB-specific quality signals.
- Preserve legacy composite output.

## Phase 3 — Team Environment

- Add team offensive/defensive signals.
- Create Team Environment score.
- Validate against historical team performance.
- Prevent defensive double-counting.

## Phase 4 — Matchup

- Move opponent effects out of Player Quality.
- Replace simple defensive rank dependence with richer matchup signals where data supports it.
- Validate matchup effects historically.

## Phase 5 — Projection

- Combine baseline, quality, opportunity, environment, matchup, and availability.
- Add bounded adjustments.
- Run old and new projections side by side.

## Phase 6 — Simulation

- Feed the new projections into the Monte Carlo engine.
- Reassess variance assumptions.
- Validate simulated distributions.

## Phase 7 — Production migration

Only after validation:

- make the new model the default
- retain legacy fields temporarily
- remove obsolete calculations
- update UI labels/tooltips
- update documentation

---

# 18. Explicit Non-Goals

The project will NOT:

- scrape Silver Bulletin to reproduce QBERT/ELWAY
- claim to reproduce proprietary Silver models
- add `nfl_data_py`
- replace `nflreadpy` solely to obtain equivalent functionality
- create arbitrary 0–100 scores without documented meaning
- use matchup strength as a hidden component of player talent
- allow a regression/model component to produce unbounded fantasy or auction values
- optimize for model complexity instead of predictive performance

---

# 19. Initial Acceptance Criteria

Phase 1 is complete when:

- [ ] Current player data schema is fully documented.
- [ ] All existing rating inputs are identified.
- [ ] Normalization exists in one location.
- [ ] Normalization is position-aware where required.
- [ ] Missing and low-sample behavior is defined.
- [ ] Existing frontend fields continue to work.
- [ ] Existing auction functionality continues to work.
- [ ] Existing simulator functionality continues to work.
- [ ] New Player Quality / Team Environment / Matchup fields can be added without breaking consumers.
- [ ] Automated validation passes.
- [ ] Old and new calculations can be compared on the same player dataset.

---

# 20. Immediate Engineering Task

The first implementation task is **not** to rewrite the simulator.

It is:

```text
scripts/update_nfl_data.py
        |
        +--> audit existing raw signals
        |
        +--> centralize normalization
        |
        +--> preserve existing schema
        |
        +--> expose new model-ready fields
        |
        v
generated nfl_data.js
```

After that passes validation, Player Quality can be implemented.

The simulator and frontend should remain untouched until the new deterministic model has been validated.

---

## Decision Log

### D001 — Do not reproduce QBERT/ELWAY
Oracle will build transparent internal equivalents rather than attempting to scrape or reverse-engineer proprietary models.

### D002 — Separate player quality from matchup
Opponent strength affects projection, not underlying player quality.

### D003 — Separate talent from opportunity
Efficiency and role are independent signals.

### D004 — Replace population min/max normalization
Ratings should have stable meaning across weekly datasets.

### D005 — Preserve legacy interfaces during migration
The new model will initially coexist with the current `composite_rating` system.

### D006 — Validate before migration
The new model must earn the right to replace the old model through historical testing.
