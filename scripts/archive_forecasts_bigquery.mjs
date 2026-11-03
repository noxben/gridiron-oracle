import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { BigQuery } from '@google-cloud/bigquery';
import { buildArchivePayload } from './archive_forecasts.mjs';
import { NFL_DATA_META } from '../src/utils/nfl_data.js';
import { ESPN_DATA } from '../src/utils/espn_data.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const PROJECT_ID = 'ffl-oracle-513005';
const DATASET_ID = 'ffl_oracle';
const MODEL_VERSION = 'oracle-v1.5';

function hash(value) {
  return createHash('sha256').update(String(value)).digest('hex');
}

function normalizeId(value) {
  if (value === null || value === undefined) return null;
  const text = String(value).trim();
  if (!text || text.toLowerCase() === 'nan') return null;
  if (/^\d+\.0+$/.test(text)) return text.replace(/\.0+$/, '');
  return text;
}

function parseArgs(argv) {
  const args = { season: null, week: null, write: false, output: null };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--season') args.season = Number(argv[++i]);
    else if (arg === '--week') args.week = Number(argv[++i]);
    else if (arg === '--output') args.output = argv[++i];
    else if (arg === '--write') args.write = true;
    else if (arg === '--help') {
      console.log('Usage: node scripts/archive_forecasts_bigquery.mjs --season YEAR --week WEEK [--write] [--output FILE]');
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  if (!Number.isInteger(args.season) || !Number.isInteger(args.week)) {
    throw new Error('Both --season and --week are required.');
  }
  if (args.season < 2020 || args.season > 2035 || args.week < 1 || args.week > 18) {
    throw new Error('Season must be 2020–2035 and week must be 1–18.');
  }
  return args;
}

function loadCrosswalk() {
  const output = execFileSync(
    process.env.PYTHON ?? 'python3',
    ['scripts/export_player_id_crosswalk.py'],
    { cwd: ROOT, encoding: 'utf8', maxBuffer: 20 * 1024 * 1024 },
  );
  const records = JSON.parse(output);
  const map = new Map();
  const ambiguous = new Set();

  for (const row of records) {
    const espnId = normalizeId(row.espn_id);
    const gsisId = String(row.gsis_id ?? '').trim();
    if (!espnId || !gsisId) continue;
    if (map.has(espnId) && map.get(espnId) !== gsisId) {
      ambiguous.add(espnId);
    } else {
      map.set(espnId, gsisId);
    }
  }

  for (const id of ambiguous) map.delete(id);
  return { map, ambiguous };
}

function rosterEspnId(player) {
  return normalizeId(
    player?.id ?? player?.espn_id ?? player?.player_id ?? player?.athlete_id,
  );
}

function applyEspnBenchmarks(payload, crosswalk) {
  const byGsis = new Map();
  let unmatched = 0;
  let ambiguous = 0;

  for (const player of ESPN_DATA?.roster ?? []) {
    const espnId = rosterEspnId(player);
    if (!espnId) {
      unmatched++;
      continue;
    }
    if (crosswalk.ambiguous.has(espnId)) {
      ambiguous++;
      continue;
    }
    const gsisId = crosswalk.map.get(espnId);
    if (!gsisId) {
      unmatched++;
      continue;
    }

    const projection = Number(player.projected_points);
    if (!Number.isFinite(projection)) continue;

    if (byGsis.has(gsisId)) {
      byGsis.delete(gsisId);
      ambiguous++;
      continue;
    }
    byGsis.set(gsisId, projection);
  }

  let matched = 0;
  for (const snapshot of payload.snapshots) {
    if (byGsis.has(snapshot.player_id)) {
      snapshot.baseline_projection = byGsis.get(snapshot.player_id);
      snapshot.features.source_provenance.espn_match_method = 'espn_id_gsis_crosswalk';
      matched++;
    } else {
      snapshot.baseline_projection = null;
      snapshot.features.source_provenance.espn_match_method = null;
    }
  }

  payload.run.metadata.espn_benchmark_match_count = matched;
  payload.run.metadata.espn_crosswalk_unmatched_count = unmatched;
  payload.run.metadata.espn_crosswalk_ambiguous_count = ambiguous;
  payload.run.metadata.espn_crosswalk_match_method = 'nflreadpy_load_ff_playerids';
  return matched;
}

function makeDeterministicIds(payload) {
  const revision = payload.run.source_revision
    ?? `${NFL_DATA_META?.generated_at ?? 'unknown'}:${NFL_DATA_META?.season ?? 'unknown'}`;
  const identity = `${revision}:${payload.run.season}:${payload.run.week}:${MODEL_VERSION}`;
  const runId = hash(identity).slice(0, 32);

  payload.run.run_id = runId;
  payload.snapshots.forEach((row) => {
    row.run_id = runId;
    row.snapshot_id = hash(`${runId}:${row.player_id}`).slice(0, 32);
  });
}

const runSchema = [
  { name: 'run_id', type: 'STRING', mode: 'REQUIRED' },
  { name: 'created_at', type: 'TIMESTAMP', mode: 'REQUIRED' },
  { name: 'season', type: 'INT64', mode: 'REQUIRED' },
  { name: 'week', type: 'INT64', mode: 'REQUIRED' },
  { name: 'model_version', type: 'STRING', mode: 'REQUIRED' },
  { name: 'source_revision', type: 'STRING' },
  { name: 'status', type: 'STRING', mode: 'REQUIRED' },
  { name: 'row_count', type: 'INT64' },
  { name: 'metadata', type: 'JSON' },
];

const snapshotSchema = [
  { name: 'snapshot_id', type: 'STRING', mode: 'REQUIRED' },
  { name: 'run_id', type: 'STRING', mode: 'REQUIRED' },
  { name: 'captured_at', type: 'TIMESTAMP', mode: 'REQUIRED' },
  { name: 'season', type: 'INT64', mode: 'REQUIRED' },
  { name: 'week', type: 'INT64', mode: 'REQUIRED' },
  { name: 'player_id', type: 'STRING', mode: 'REQUIRED' },
  { name: 'player_name', type: 'STRING' },
  { name: 'position', type: 'STRING' },
  { name: 'team', type: 'STRING' },
  { name: 'opponent', type: 'STRING' },
  { name: 'baseline_projection', type: 'FLOAT64' },
  { name: 'final_projection', type: 'FLOAT64' },
  { name: 'model_version', type: 'STRING', mode: 'REQUIRED' },
  { name: 'features', type: 'JSON' },
];

async function loadAndMerge(bigquery, tableName, rows, schema, mergeSql) {
  if (!rows.length) return;

  const stagingName = `stg_${tableName}_${Date.now()}_${process.pid}`;
  const dataset = bigquery.dataset(DATASET_ID);
  const staging = dataset.table(stagingName);
  const dir = await mkdtemp(resolve(tmpdir(), 'ffl-archive-'));
  const file = resolve(dir, `${tableName}.ndjson`);

  try {
    await writeFile(file, rows.map(row => JSON.stringify(row)).join('\n') + '\n');
    await staging.create({
      schema: { fields: schema },
      expirationTime: Date.now() + 60 * 60 * 1000,
    });
    await staging.load(file, {
      sourceFormat: 'NEWLINE_DELIMITED_JSON',
      writeDisposition: 'WRITE_TRUNCATE',
    });
    await bigquery.query({
      query: mergeSql(stagingName),
      location: 'EU',
    });
  } finally {
    await staging.delete({ ignoreNotFound: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true });
  }
}

async function writeBigQuery(payload) {
  const bigquery = new BigQuery({ projectId: PROJECT_ID });
  const q = name => `\`${PROJECT_ID}.${DATASET_ID}.${name}\``;

  const pendingRun = {
    ...payload.run,
    status: 'pending',
  };

  await loadAndMerge(
    bigquery,
    'forecast_runs',
    [pendingRun],
    runSchema,
    staging => `
      MERGE ${q('forecast_runs')} T
      USING ${q(staging)} S
      ON T.run_id = S.run_id
      WHEN MATCHED THEN UPDATE SET
        created_at = S.created_at,
        season = S.season,
        week = S.week,
        model_version = S.model_version,
        source_revision = S.source_revision,
        status = S.status,
        row_count = S.row_count,
        metadata = S.metadata
      WHEN NOT MATCHED THEN INSERT
        (run_id, created_at, season, week, model_version,
         source_revision, status, row_count, metadata)
      VALUES
        (S.run_id, S.created_at, S.season, S.week,
         S.model_version, S.source_revision, S.status,
         S.row_count, S.metadata)
    `,
  );

  await loadAndMerge(
    bigquery,
    'forecast_snapshots',
    payload.snapshots,
    snapshotSchema,
    staging => `
      MERGE ${q('forecast_snapshots')} T
      USING ${q(staging)} S
      ON T.snapshot_id = S.snapshot_id
      WHEN MATCHED THEN UPDATE SET
        run_id = S.run_id, captured_at = S.captured_at,
        season = S.season, week = S.week, player_id = S.player_id,
        player_name = S.player_name, position = S.position,
        team = S.team, opponent = S.opponent,
        baseline_projection = S.baseline_projection,
        final_projection = S.final_projection,
        model_version = S.model_version, features = S.features
      WHEN NOT MATCHED THEN INSERT
        (snapshot_id, run_id, captured_at, season, week, player_id,
         player_name, position, team, opponent, baseline_projection,
         final_projection, model_version, features)
      VALUES
        (S.snapshot_id, S.run_id, S.captured_at, S.season, S.week,
         S.player_id, S.player_name, S.position, S.team, S.opponent,
         S.baseline_projection, S.final_projection, S.model_version,
         S.features)
    `,
  );

  const [verification] = await bigquery.query({
    query: `
      SELECT
        COUNT(*) AS row_count,
        COUNT(DISTINCT player_id) AS unique_players,
        COUNTIF(final_projection IS NULL) AS null_projections
      FROM ${q('forecast_snapshots')}
      WHERE run_id = @run_id
    `,
    params: { run_id: payload.run.run_id },
    location: 'EU',
  });

  const result = verification[0];
  const rowCount = Number(result.row_count);
  const uniquePlayers = Number(result.unique_players);
  const nullProjections = Number(result.null_projections);

  if (
    rowCount !== payload.snapshots.length ||
    uniquePlayers !== payload.snapshots.length ||
    nullProjections !== 0
  ) {
    throw new Error(
      `Snapshot verification failed: expected ${payload.snapshots.length}, ` +
      `got rows=${rowCount}, unique=${uniquePlayers}, ` +
      `null projections=${nullProjections}. Run remains pending.`,
    );
  }

  const completedRun = {
    ...payload.run,
    status: 'completed',
  };

  await loadAndMerge(
    bigquery,
    'forecast_runs',
    [completedRun],
    runSchema,
    staging => `
      MERGE ${q('forecast_runs')} T
      USING ${q(staging)} S
      ON T.run_id = S.run_id
      WHEN MATCHED THEN UPDATE SET
        status = S.status,
        row_count = S.row_count,
        metadata = S.metadata
      WHEN NOT MATCHED THEN INSERT
        (run_id, created_at, season, week, model_version,
         source_revision, status, row_count, metadata)
      VALUES
        (S.run_id, S.created_at, S.season, S.week, S.model_version,
         S.source_revision, S.status, S.row_count, S.metadata)
    `,
  );
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  let sourceRevision = process.env.GITHUB_SHA ?? null;
  if (!sourceRevision) {
    try {
      sourceRevision = execFileSync(
        'git',
        ['rev-parse', 'HEAD'],
        { cwd: ROOT, encoding: 'utf8' },
      ).trim() || null;
    } catch {
      sourceRevision = null;
    }
  }

  const payload = buildArchivePayload({
    season: args.season,
    week: args.week,
    sourceRevision,
  });

  const { map, ambiguous } = loadCrosswalk();
  const matched = applyEspnBenchmarks(payload, { map, ambiguous });
  makeDeterministicIds(payload);
  payload.run.status = args.write ? 'pending' : 'preview';
  payload.run.metadata.archive_mode = args.write
    ? 'bigquery'
    : 'local_preview';

  const sourceSeason = Number(NFL_DATA_META?.season);
  const sourceWeek = Number(NFL_DATA_META?.week);
  const sourceMatchesTarget = sourceSeason === args.season && sourceWeek < args.week;

  payload.run.metadata.nfl_source_season_matches_target = sourceMatchesTarget;
  payload.run.metadata.nfl_source_season = sourceSeason || null;
  payload.run.metadata.nfl_source_week = sourceWeek || null;
  payload.run.metadata.espn_source_season = ESPN_DATA?.league?.season ?? null;
  payload.run.metadata.espn_source_week = ESPN_DATA?.league?.current_week ?? null;

  const output = resolve(ROOT, args.output
    ?? `outputs/forecast_archive/forecast_${args.season}_week_${args.week}.json`);
  await mkdir(dirname(output), { recursive: true });
  await writeFile(output, `${JSON.stringify(payload, null, 2)}\n`);

  console.log(JSON.stringify({
    mode: args.write ? 'BIGQUERY_WRITE' : 'LOCAL_PREVIEW',
    target: `${args.season} week ${args.week}`,
    nflSource: `${sourceSeason} week ${sourceWeek}`,
    espnSource: `${ESPN_DATA?.league?.season ?? 'unknown'} week ${ESPN_DATA?.league?.current_week ?? 'unknown'}`,
    runId: payload.run.run_id,
    players: payload.snapshots.length,
    espnBenchmarksMatched: matched,
    output,
  }, null, 2));

  if (!args.write) {
    console.log('Preview only. BigQuery was not contacted.');
    return;
  }

  if (!sourceMatchesTarget) {
    throw new Error(
      `Refusing BigQuery write: NFL feature source season ${sourceSeason} ` +
      `must match target season ${args.season} and be earlier than target week ${args.week}. Preview was saved.`,
    );
  }

  if (!process.env.GOOGLE_APPLICATION_CREDENTIALS
      && !process.env.GOOGLE_GHA_CREDS_PATH
      && !process.env.GCLOUD_PROJECT) {
    console.warn('No explicit Google credential environment variable detected; ADC may still be available.');
  }

  await writeBigQuery(payload);
  console.log(`Archived ${payload.snapshots.length} snapshots to BigQuery.`);
}

main().catch(error => {
  console.error(error.stack ?? error);
  process.exitCode = 1;
});
