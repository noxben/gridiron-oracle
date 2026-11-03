import { randomUUID } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { NFL_DATA_META, NFL_PLAYERS } from '../src/utils/nfl_data.js';
import { ESPN_DATA } from '../src/utils/espn_data.js';
import { projectPoints } from '../src/utils/simulator.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const MODEL_VERSION = 'oracle-v1.5';

function normalizeName(name) {
  return String(name ?? '')
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/\./g, ' ')
    .replace(/[^a-zA-Z0-9 ]/g, '')
    .toLowerCase()
    .trim()
    .replace(/\s+/g, ' ');
}

function identityKey(name, position, team) {
  const parts = normalizeName(name).split(' ').filter(Boolean);
  if (parts.length < 2) return null;

  // NFL source names often use first initials, e.g. "C.Williams".
  const firstInitial = parts[0][0];
  const surname = parts.at(-1);

  return `${firstInitial}|${surname}|${position}|${team}`;
}

function buildEspnIndex(roster) {
  const index = new Map();

  for (const player of roster ?? []) {
    const key = identityKey(player.name, player.position, player.team);
    if (!key) continue;

    if (!index.has(key)) index.set(key, []);
    index.get(key).push(player);
  }

  return index;
}

function finiteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

export function buildArchivePayload({
  season,
  week,
  players = NFL_PLAYERS,
  nflMeta = NFL_DATA_META,
  espnData = ESPN_DATA,
  sourceRevision = process.env.GITHUB_SHA ?? null,
  capturedAt = new Date().toISOString(),
} = {}) {
  if (!Number.isInteger(season) || season < 2020 || season > 2035) {
    throw new Error('Provide a valid target season with --season (2020–2035).');
  }
  if (!Number.isInteger(week) || week < 1 || week > 18) {
    throw new Error('Provide a valid regular-season target week with --week (1–18).');
  }
  if (!Array.isArray(players) || players.length === 0) {
    throw new Error('NFL player data is empty.');
  }

  const runId = randomUUID();
  const espnIndex = buildEspnIndex(espnData?.roster);
  const seenIds = new Set();
  const snapshots = [];

  for (const player of players) {
    const playerId = String(player.gsis_id ?? '').trim();

    if (!playerId) {
      throw new Error(`Missing gsis_id for player: ${player.name ?? '(unnamed)'}`);
    }
    if (seenIds.has(playerId)) {
      throw new Error(`Duplicate gsis_id: ${playerId}`);
    }
    seenIds.add(playerId);

    const projection = projectPoints(player, 0);
    if (!finiteNumber(projection) || projection < 0 || projection > 100) {
      throw new Error(
        `Invalid projection for ${player.name} (${playerId}): ${projection}`,
      );
    }

    const key = identityKey(player.name, player.position, player.team);
    const candidates = key ? (espnIndex.get(key) ?? []) : [];
    const espnMatch = candidates.length === 1 ? candidates[0] : null;
    const espnProjection = espnMatch?.projected_points;

    snapshots.push({
      snapshot_id: randomUUID(),
      run_id: runId,
      captured_at: capturedAt,
      season,
      week,
      player_id: playerId,
      player_name: player.name ?? null,
      position: player.position ?? null,
      team: player.team ?? null,
      opponent: player.opponent_team ?? null,
      baseline_projection: finiteNumber(espnProjection) ? espnProjection : null,
      final_projection: projection,
      model_version: MODEL_VERSION,
      features: {
        ...player,
        source_provenance: {
          nfl_data_season: nflMeta?.season ?? null,
          nfl_data_week: nflMeta?.week ?? null,
          nfl_data_generated_at: nflMeta?.generated_at ?? null,
          espn_season: espnData?.league?.season ?? null,
          espn_week: espnData?.league?.current_week ?? null,
          espn_fetched_at: espnData?.fetched_at ?? null,
          espn_match_method: espnMatch ? 'initial_surname_position_team' : null,
        },
      },
    });
  }

  return {
    run: {
      run_id: runId,
      created_at: capturedAt,
      season,
      week,
      model_version: MODEL_VERSION,
      source_revision: sourceRevision,
      status: 'preview',
      row_count: snapshots.length,
      metadata: {
        archive_mode: 'local_preview',
        nfl_data_season: nflMeta?.season ?? null,
        nfl_data_week: nflMeta?.week ?? null,
        espn_season: espnData?.league?.season ?? null,
        espn_week: espnData?.league?.current_week ?? null,
        espn_benchmark_match_count: snapshots.filter(
          row => row.baseline_projection !== null,
        ).length,
        note: 'Source metadata is retained separately from the requested target season/week.',
      },
    },
    snapshots,
  };
}

function parseArgs(argv) {
  const args = {};

  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--season') args.season = Number(argv[++i]);
    else if (argv[i] === '--week') args.week = Number(argv[++i]);
    else if (argv[i] === '--output') args.output = argv[++i];
    else if (argv[i] === '--help') args.help = true;
    else throw new Error(`Unknown argument: ${argv[i]}`);
  }

  return args;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));

  if (args.help) {
    console.log(
      'Usage: node scripts/archive_forecasts.mjs --season YEAR --week WEEK [--output PATH]',
    );
    return;
  }

  if (args.season === undefined || args.week === undefined) {
    throw new Error('Both --season and --week are required. No target week is inferred.');
  }

  const payload = buildArchivePayload({
    season: args.season,
    week: args.week,
  });

  const output = resolve(
    ROOT,
    args.output ?? `outputs/forecast_archive/forecast_${args.season}_week_${args.week}.json`,
  );

  await mkdir(dirname(output), { recursive: true });
  await writeFile(output, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');

  const summary = {
    output,
    target: `${args.season} week ${args.week}`,
    source: {
      nfl: `${NFL_DATA_META.season} week ${NFL_DATA_META.week}`,
      espn: `${ESPN_DATA.league?.season ?? 'unknown'} week ${ESPN_DATA.league?.current_week ?? 'unknown'}`,
    },
    rows: payload.run.row_count,
    espnBenchmarks: payload.run.metadata.espn_benchmark_match_count,
    projectionMin: Math.min(...payload.snapshots.map(row => row.final_projection)),
    projectionMax: Math.max(...payload.snapshots.map(row => row.final_projection)),
    mode: 'local preview only; no BigQuery writes',
  };

  console.log(JSON.stringify(summary, null, 2));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    console.error(`Archive preview failed: ${error.message}`);
    process.exitCode = 1;
  });
}
