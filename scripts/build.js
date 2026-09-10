"use strict";
/**
 * Build dist/: stage the runtime files (ajd/, skill-data/, cli.js) into a
 * clean directory and verify them with the Python interpreter.  npm publish
 * runs this via prepublishOnly, so a publish always ships a freshly built,
 * syntax-checked artifact — never stale or half-updated files.
 */
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");


const DIST = path.join(ROOT, "dist");

function copyDir(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (entry.name === "__pycache__") {
      continue;
    }
    const from = path.join(src, entry.name);
    const to = path.join(dst, entry.name);
    if (entry.isDirectory()) {
      copyDir(from, to);
    } else {
      fs.copyFileSync(from, to);
    }
  }
}

function findPython() {
  const candidates = process.platform === "win32"
    ? [["py", ["-3"]], ["python"], ["python3"]]
    : [["python3"], ["python"], ["py", ["-3"]]];
  for (const [cmd, extra] of candidates) {
    const probe = spawnSync(cmd, [...(extra || []), "-c",
      "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"],
      { stdio: "ignore", windowsHide: true });
    if (probe.status === 0) {
      return [cmd, extra || []];
    }
  }
  return null;
}

function main() {
  // Skill frontmatter is linted by the shared scripts/check-skill.mjs (also
  // runnable as `npm run check-skill`) — gate the build on it so a publish
  // never ships a SKILL.md a strict YAML parser would reject.  Node
  // guarantees cross-platform execution (no bash dependency).
  const gate = spawnSync(process.execPath, [path.join(ROOT, "scripts", "check-skill.mjs")], {
    stdio: "inherit",
  });
  if (gate.status !== 0) {
    console.error("build: skill frontmatter check failed — aborting");
    process.exit(1);
  }

  fs.rmSync(DIST, { recursive: true, force: true });
  copyDir(path.join(ROOT, "ajd"), path.join(DIST, "ajd"));
  fs.copyFileSync(path.join(ROOT, "bin", "cli.js"), path.join(DIST, "cli.js"));
  // skills/ and skill-data/ ship verbatim at the package root — the
  // launcher resolves AJD_ROOT there, so dist copies would just drift.

  const py = findPython();
  if (!py) {
    console.error("build: no Python >= 3.10 interpreter found — aborting");
    process.exit(1);
  }
  const check = spawnSync(py[0], [...py[1], "-m", "compileall", "-q",
    path.join(DIST, "ajd")], { stdio: "inherit" });
  if (check.status !== 0) {
    console.error("build: Python syntax verification failed — aborting");
    process.exit(1);
  }
  // compileall leaves bytecode caches behind — never ship them.
  fs.rmSync(path.join(DIST, "ajd", "__pycache__"), { recursive: true, force: true });

  // Smoke-test the staged layout the way the installed launcher runs it:
  // PYTHONPATH points at dist/ (parent of the ajd package), AJD_ROOT at
  // the package root where skills/ and skill-data/ ship.  Catches layout
  // regressions (e.g. a PYTHONPATH pointing INTO the package) at build
  // time instead of on the user's machine.
  const smoke = spawnSync(py[0], [...py[1], "-m", "ajd", "--version"], {
    stdio: "pipe",
    env: {
      ...process.env,
      PYTHONPATH: DIST,
      AJD_ROOT: ROOT,
    },
  });
  if (smoke.status !== 0) {
    console.error("build: staged package failed the import smoke test — aborting");
    console.error(String(smoke.stderr));
    process.exit(1);
  }

  const count = fs.readdirSync(DIST, { recursive: true }).length;
  console.log(`build: dist/ staged and verified (${count} entries, python ${py[0]})`);
}

main();
