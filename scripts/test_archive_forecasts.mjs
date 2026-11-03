import test from 'node:test';
import assert from 'node:assert/strict';

import { NFL_DATA_META, NFL_PLAYERS } from '../src/utils/nfl_data.js';
import { ESPN_DATA } from '../src/utils/espn_data.js';
import { projectPoints } from '../src/utils/simulator.js';
import { buildArchivePayload } from './archive_forecasts.mjs';

const FIXED_TIME = '2026-10-29T00:00:00.000Z';

function build(overrides = {}) {
  return buildArchivePayload({
    season: 2026,
    week: 8,
    capturedAt: FIXED_TIME,
    sourceRevision: 'test-revision',
    ...overrides,
  });
}

test('archives every NFL player with unique IDs and valid projections', () => {
  const payload = build();

  assert.equal(payload.run.row_count, NFL_PLAYERS.length);
  assert.equal(payload.snapshots.length, NFL_PLAYERS.length);

  const ids = payload.snapshots.map(row => row.player_id);
  assert.equal(new Set(ids).size, ids.length);

  for (const row of payload.snapshots) {
    assert.ok(row.player_id.length > 0);
    assert.ok(Number.isFinite(row.final_projection));
    assert.ok(row.final_projection >= 0 && row.final_projection <= 100);
    assert.equal(row.final_projection, projectPoints(
      NFL_PLAYERS.find(player => player.gsis_id === row.player_id),
      0,
    ));
    assert.equal(row.season, 2026);
    assert.equal(row.week, 8);
    assert.equal(row.model_version, 'oracle-v1.5');
    assert.equal(row.captured_at, FIXED_TIME);
  }
});

test('preserves source-season provenance without confusing it with target week', () => {
  const payload = build();
  const first = payload.snapshots[0];

  assert.equal(first.season, 2026);
  assert.equal(first.week, 8);
  assert.equal(
    first.features.source_provenance.nfl_data_season,
    NFL_DATA_META.season,
  );
  assert.equal(
    first.features.source_provenance.nfl_data_week,
    NFL_DATA_META.week,
  );
  assert.equal(
    first.features.source_provenance.espn_season,
    ESPN_DATA.league?.season ?? null,
  );
  assert.equal(payload.run.status, 'preview');
});

test('rejects missing or invalid target season/week', () => {
  assert.throws(() => build({ season: undefined }), /target season/);
  assert.throws(() => build({ season: 2036 }), /target season/);
  assert.throws(() => build({ week: 0 }), /target week/);
  assert.throws(() => build({ week: 19 }), /target week/);
});

test('rejects empty player data, missing IDs, and duplicate IDs', () => {
  assert.throws(() => build({ players: [] }), /empty/);

  assert.throws(() => build({
    players: [{ name: 'Missing ID', position: 'RB', team: 'ABC' }],
  }), /Missing gsis_id/);

  assert.throws(() => build({
    players: [
      { gsis_id: 'same-id', name: 'Player One', position: 'RB', team: 'ABC' },
      { gsis_id: 'same-id', name: 'Player Two', position: 'RB', team: 'XYZ' },
    ],
  }), /Duplicate gsis_id/);
});

test('keeps ESPN benchmark separate and null when identity is ambiguous', () => {
  const player = {
    gsis_id: 'test-id',
    name: 'C.Williams',
    position: 'QB',
    team: 'CHI',
    season_avg_pts: 10,
    last3_avg_pts: 10,
  };

  const espnData = {
    league: { season: 2026, current_week: 8 },
    roster: [
      {
        name: 'Caleb Williams',
        position: 'QB',
        team: 'CHI',
        projected_points: 17.25,
      },
    ],
  };

  const matched = build({ players: [player], espnData });
  assert.equal(matched.snapshots[0].baseline_projection, 17.25);
  assert.notEqual(
    matched.snapshots[0].baseline_projection,
    matched.snapshots[0].final_projection,
  );

  const ambiguous = build({
    players: [player],
    espnData: {
      ...espnData,
      roster: [
        ...espnData.roster,
        { ...espnData.roster[0], espn_id: 'second-id' },
      ],
    },
  });

  assert.equal(ambiguous.snapshots[0].baseline_projection, null);
});

test('rejects non-finite or implausible projection output', () => {
  const badPlayer = {
    gsis_id: 'bad-projection',
    name: 'Bad Projection',
    position: 'QB',
    team: 'ABC',
    season_avg_pts: 10000,
    last3_avg_pts: 10000,
    epa_score: 100,
    usage_score: 100,
    snap_score: 100,
    red_zone_score: 100,
    matchup_score: 100,
    team_pass_epa: 1,
    team_pass_success_rate: 1,
  };

  assert.throws(() => build({ players: [badPlayer] }), /Invalid projection/);
});
