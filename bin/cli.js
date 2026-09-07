#!/usr/bin/env node
/**
 * Cross-platform launcher for the ajd debugger.
 *
 * The implementation is pure Python (stdlib only, no compiled extensions),
 * so it runs wherever Python 3.10+ runs — Windows/macOS/Linux on amd64,
 * arm64, or any other architecture.  This launcher only has to find the
 * interpreter and add the package's ajd/ directory to PYTHONPATH.
 */
"use strict";

const { spawnSync } = require("child_process");
const path = require("path");

// Shipped as dist/cli.js next to dist/ajd and dist/skill-data — resolve
// everything relative to this file's own directory.
const DIST_DIR = __dirname;
const AJD_DIR = path.join(DIST_DIR, "ajd");

function probe(cmd, extraArgs) {
  try {
    const r = spawnSync(cmd, [...(extraArgs || []), "-c", "import sys; print('.'.join(map(str, sys.version_info[:2])))"],
      { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"], windowsHide: true });
    if (r.status === 0 && r.stdout && r.stdout.trim()) {
      return { cmd, args: extraArgs || [], version: r.stdout.trim() };
    }
  } catch (_) {
    /* not found — try next candidate */
  }
  return null;
}

function findPython() {
  // Windows rarely ships a `python3`; try the launcher and `python` first.
  // POSIX prefers `python3` (macOS/Linux default installs).
  const candidates = process.platform === "win32"
    ? [["py", ["-3"]], ["python"], ["python3"]]
    : [["python3"], ["python"], ["py", ["-3"]]];
  for (const [cmd, extra] of candidates) {
    const found = probe(cmd, extra);
    if (found) {
      const [major, minor] = found.version.split(".").map(Number);
      if (major > 3 || (major === 3 && minor >= 10)) {
        return found;
      }
      process.stderr.write(
        `agent-java-debugger: found ${cmd} ${found.version}, but Python >= 3.10 is required\n`);
      return null;
    }
  }
  process.stderr.write(
    "agent-java-debugger: no Python 3.10+ interpreter found " +
    "(tried: py -3, python3, python). Install it from https://python.org\n");
  return null;
}

function main() {
  const py = findPython();
  if (!py) {
    process.exit(1);
  }
  const sep = process.platform === "win32" ? ";" : ":";
  const env = { ...process.env };
  env.PYTHONPATH = AJD_DIR + (env.PYTHONPATH ? sep + env.PYTHONPATH : "");
  env.AJD_ROOT = DIST_DIR;  // skill-data/ resolution for `ajd skills`
  const result = spawnSync(py.cmd, [...py.args, "-m", "ajd", ...process.argv.slice(2)],
    { stdio: "inherit", env, windowsHide: false });
  if (result.error) {
    process.stderr.write(`agent-java-debugger: failed to run: ${result.error.message}\n`);
    process.exit(1);
  }
  process.exit(result.status === null ? 1 : result.status);
}

main();
