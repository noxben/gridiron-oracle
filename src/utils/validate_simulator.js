/* global process */
/**
 * Gridiron Oracle — Simulator Integration Validation
 *
 * Validates the boundary between generated player ratings and the
 * JavaScript simulator.
 *
 * Checks:
 *   1. Generated player data is present and structurally valid.
 *   2. Rating components are valid 0–100 values.
 *   3. Simulator consumes the stored composite_rating.
 *   4. Simulator returns the exact Python-generated rating values.
 *   5. Legacy matchup/system adjustments are not part of player rating.
 *
 * Run from project root:
 *   node src/utils/validate_simulator.js
 */

import fs from "fs";
import { NFL_PLAYERS } from "./nfl_data.js";
import { computeCompositeRating } from "./simulator.js";

const POSITIONS = ["QB", "RB", "WR", "TE"];

const RATING_COMPONENTS = [
  "epa_score",
  "usage_score",
  "snap_score",
  "red_zone_score",
  "composite_rating",
];

const errors = [];
const warnings = [];

function section(title) {
  console.log(`\n${title}`);
}

function fail(message) {
  errors.push(message);
}

function warn(message) {
  warnings.push(message);
}

function isValidRating(value) {
  return Number.isFinite(value) && value >= 0 && value <= 100;
}

/**
 * Validate generated NFL player data.
 */
function validateData(players) {
  section("DATA INTEGRITY");

  console.log(`  Players:                 ${players.length}`);

  const ids = players.map(player => player.gsis_id);
  const uniqueIds = new Set(ids);

  console.log(`  Unique GSIS IDs:         ${uniqueIds.size}`);

  if (players.length === 0) {
    fail("NFL_PLAYERS is empty");
  }

  if (uniqueIds.size !== players.length) {
    fail("Duplicate GSIS IDs detected");
  }

  for (const player of players) {
    const name = player.name || "Unknown";

    if (!player.gsis_id) {
      fail(`${name}: missing gsis_id`);
    }

    for (const component of RATING_COMPONENTS) {
      const value = Number(player[component]);

      if (!isValidRating(value)) {
        fail(
          `${name}: ${component}=${player[component]} is not a valid 0–100 rating`
        );
      }
    }

    const probability = Number(player.play_probability);

    if (
      !Number.isFinite(probability) ||
      probability < 0 ||
      probability > 1
    ) {
      fail(
        `${name}: invalid play_probability=${player.play_probability}`
      );
    }

    const oppRank = Number(player.opp_def_rank);

    if (
      !Number.isInteger(oppRank) ||
      oppRank < 1 ||
      oppRank > 32
    ) {
      fail(
        `${name}: invalid opp_def_rank=${player.opp_def_rank}`
      );
    }
  }

  console.log(
    `  Rating fields:          ${
      errors.length === 0 ? "OK" : "FAIL"
    }`
  );
}

/**
 * Validate position coverage.
 */
function validatePositions(players) {
  section("POSITION DISTRIBUTION");

  for (const position of POSITIONS) {
    const count = players.filter(
      player => player.position === position
    ).length;

    console.log(`  ${position}: ${count}`);

    if (count === 0) {
      fail(`${position}: no players found`);
    }
  }
}

/**
 * Validate that each rating component actually contains variation.
 */
function validateComponentHealth(players) {
  section("COMPONENT HEALTH");

  for (const position of POSITIONS) {
    const positionPlayers = players.filter(
      player => player.position === position
    );

    for (const component of RATING_COMPONENTS.slice(0, -1)) {
      const values = positionPlayers
        .map(player => Number(player[component]))
        .filter(Number.isFinite);

      const uniqueValues = new Set(values).size;

      const healthy = uniqueValues > 2;

      console.log(
        `  ${position} ${component.padEnd(16)} ` +
        `${healthy ? "OK" : "WARNING"} ` +
        `(unique=${uniqueValues})`
      );

      if (!healthy) {
        warn(
          `${position} ${component}: only ${uniqueValues} unique values`
        );
      }
    }
  }
}

/**
 * Verify the simulator returns the stored Python-generated values.
 */
function validateRuntime(players) {
  section("RUNTIME INTEGRATION");

  for (const position of POSITIONS) {
    const player = players.find(
      candidate => candidate.position === position
    );

    if (!player) {
      fail(`${position}: no runtime test player available`);
      continue;
    }

    const result = computeCompositeRating(player, players);

    const expectedRating = Number(player.composite_rating);

    if (!isValidRating(result.rating)) {
      fail(
        `${player.name}: simulator returned invalid rating=${result.rating}`
      );
    }

    if (Math.abs(result.rating - expectedRating) > 0.001) {
      fail(
        `${player.name}: simulator rating=${result.rating}, ` +
        `stored rating=${expectedRating}`
      );
    }

    const componentChecks = [
      ["epaScore", "epa_score"],
      ["usageScore", "usage_score"],
      ["snapScore", "snap_score"],
      ["redZoneScore", "red_zone_score"],
    ];

    for (const [runtimeKey, dataKey] of componentChecks) {
      const runtimeValue = Number(result[runtimeKey]);
      const storedValue = Number(player[dataKey]);

      if (!isValidRating(runtimeValue)) {
        fail(
          `${player.name}: simulator returned invalid ${runtimeKey}=${runtimeValue}`
        );
        continue;
      }

      if (Math.abs(runtimeValue - storedValue) > 0.001) {
        fail(
          `${player.name}: ${runtimeKey}=${runtimeValue}, ` +
          `stored ${dataKey}=${storedValue}`
        );
      }
    }

    console.log(
      `  ${position} ${player.name.padEnd(20)} ` +
      `rating=${result.rating.toFixed(2)} ` +
      `components=OK`
    );
  }
}

/**
 * Inspect simulator source to ensure the old rating engine has not
 * been reintroduced.
 */
function validateSimulatorSource() {
  section("SIMULATOR SOURCE");

  const simulatorPath = new URL("./simulator.js", import.meta.url);
  const source = fs.readFileSync(simulatorPath, "utf8");

  const requiredReferences = [
    "player.composite_rating",
    "player.epa_score",
    "player.usage_score",
    "player.snap_score",
    "player.red_zone_score",
  ];

  for (const reference of requiredReferences) {
    const present = source.includes(reference);

    console.log(
      `  ${reference.padEnd(32)} ${present ? "OK" : "FAIL"}`
    );

    if (!present) {
      fail(`Simulator missing ${reference}`);
    }
  }

  const hasLocalWeights =
    /const\s+WEIGHTS\s*=\s*\{/s.test(source);

  console.log(
    `  Local rating weights:           ${
      hasLocalWeights ? "FAIL" : "OK"
    }`
  );

  if (hasLocalWeights) {
    fail(
      "Simulator contains a local WEIGHTS rating engine"
    );
  }

  // ---------------------------------------------------------------------
  // Legacy adjustment isolation
  // ---------------------------------------------------------------------
  // Phase 1 allows the legacy adjustment definitions to remain in the
  // simulator. They must not be referenced by computeCompositeRating().

  const computeStart = source.indexOf(
    "export function computeCompositeRating"
  );

  const nextFunction = source.indexOf(
    "\n/**",
    computeStart
  );

  if (computeStart === -1 || nextFunction === -1) {
    console.log("  Legacy adjustments in rating: FAIL");
    fail("Could not isolate computeCompositeRating()");
  } else {
    const computeBody = source.slice(
      computeStart,
      nextFunction
    );

    const forbiddenReferences = [
      "DEF_RANK_ADJ",
      "SYSTEM_ADJUSTMENTS",
      "getSystemAdjustment",
    ];

    const found = forbiddenReferences.filter(
      reference => computeBody.includes(reference)
    );

    const clean = found.length === 0;

    console.log(
      `  Legacy adjustments in rating: ${clean ? "OK" : "FAIL"}`
    );

    if (!clean) {
      fail(
        `Legacy adjustment reference inside computeCompositeRating(): ${found.join(", ")}`
      );
    }
  }
}

/**
 * Final result.
 */
function reportResult() {
  console.log("\n" + "=".repeat(72));

  if (warnings.length > 0) {
    console.log("WARNINGS");

    for (const warning of warnings) {
      console.log(`  ! ${warning}`);
    }
  }

  if (errors.length > 0) {
    console.log("\nERRORS");

    for (const error of errors.slice(0, 50)) {
      console.log(`  ! ${error}`);
    }

    if (errors.length > 50) {
      console.log(`  ... and ${errors.length - 50} more`);
    }

    console.log("\nRESULT: FAIL");
    console.log("=".repeat(72));

    process.exitCode = 1;
    return;
  }

  console.log(
    `\nRESULT: ${
      warnings.length > 0 ? "PASS WITH WARNINGS" : "PASS"
    }`
  );

  console.log("=".repeat(72));
}

/**
 * Main validation sequence.
 */
function main() {
  console.log("=".repeat(72));
  console.log("GRIDIRON ORACLE — SIMULATOR INTEGRATION VALIDATION");
  console.log("=".repeat(72));

  validateData(NFL_PLAYERS);
  validatePositions(NFL_PLAYERS);
  validateComponentHealth(NFL_PLAYERS);
  validateRuntime(NFL_PLAYERS);
  validateSimulatorSource();
  reportResult();
}

main();