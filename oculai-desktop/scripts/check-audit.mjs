#!/usr/bin/env node
/**
 * npm audit gate with a narrow allowlist for advisories that are unfixable
 * downstream: @earendil-works/pi-coding-agent ships an npm-shrinkwrap.json,
 * so its nested dependency subtree ignores our package.json overrides and
 * cannot be patched from this repo (verified: even pi 0.82.1 still pins the
 * vulnerable brace-expansion 5.0.7).
 *
 * An advisory is excused ONLY if its GHSA id is allowlisted AND every
 * affected install path lies inside the pi shrinkwrap subtree. Anything
 * else at or above the "moderate" threshold fails, so the gate still
 * catches new advisories and fixable copies elsewhere in the tree.
 *
 * Usage: node scripts/check-audit.mjs [--omit=dev]
 */
import { execSync } from "node:child_process";

const ALLOWLIST = new Map([
  ["GHSA-3jxr-9vmj-r5cp", "brace-expansion DoS (exponential expansion)"],
  ["GHSA-mh99-v99m-4gvg", "brace-expansion DoS (unbounded expansion OOM)"],
  ["GHSA-j3f2-48v5-ccww", "protobufjs DoS (.proto option parsing loop)"],
]);
const SHRINKWRAP_SUBTREE = "node_modules/@earendil-works/pi-coding-agent/node_modules/";
const FAIL_SEVERITIES = new Set(["moderate", "high", "critical"]);

const KNOWN_FLAGS = new Set(["--omit=dev"]);
const args = process.argv.slice(2);
for (const arg of args) {
  if (!KNOWN_FLAGS.has(arg)) {
    console.error(`Unknown argument: ${arg}`);
    process.exit(2);
  }
}
let report;
try {
  report = JSON.parse(
    execSync(["npm", "audit", "--json", ...args].join(" "), {
      encoding: "utf8",
      maxBuffer: 64 * 1024 * 1024,
    }),
  );
} catch (err) {
  // npm audit exits non-zero when vulnerabilities exist; the JSON is still on stdout.
  if (!err.stdout) throw err;
  report = JSON.parse(err.stdout.toString());
}

const vulnerabilities = report.vulnerabilities ?? {};
const ghsaOf = (url) => (url?.match(/GHSA-[a-z0-9-]+/) ?? [null])[0];

// A package entry is excused when all of its advisory objects are allowlisted
// shrinkwrap-subtree findings, and all of its string `via` references point at
// entries that are themselves excused (chained flags like minimatch -> glob).
const excusedCache = new Map();
function isExcused(name, chain = new Set()) {
  if (excusedCache.has(name)) return excusedCache.get(name);
  if (chain.has(name)) return true; // cycle: judged by the rest of the chain
  chain.add(name);
  const entry = vulnerabilities[name];
  if (!entry) return false;
  const nodesOk = (entry.nodes ?? []).every((n) => n.startsWith(SHRINKWRAP_SUBTREE));
  const excused =
    nodesOk &&
    (entry.via ?? []).every((via) =>
      typeof via === "string"
        ? isExcused(via, chain)
        : ALLOWLIST.has(ghsaOf(via.url)),
    );
  excusedCache.set(name, excused);
  return excused;
}

const failures = [];
const excused = [];
for (const [name, entry] of Object.entries(vulnerabilities)) {
  if (!FAIL_SEVERITIES.has(entry.severity)) continue;
  (isExcused(name) ? excused : failures).push(`${name} (${entry.severity})`);
}

for (const item of excused) {
  console.log(`ALLOWLISTED (pi shrinkwrap, upstream-pinned): ${item}`);
}
if (failures.length > 0) {
  console.error(`FAIL: unallowlisted vulnerabilities at moderate+ severity:`);
  for (const item of failures) console.error(`  ${item}`);
  console.error(`Run: npm audit ${args.join(" ")}`.trim());
  process.exit(1);
}
console.log(`OK: npm audit ${args.join(" ")} clean apart from allowlisted upstream pins`.trim());
