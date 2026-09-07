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

/**
 * Validate the YAML frontmatter of every SKILL.md (skills/ + skill-data/).
 * The descriptions are plain scalars, so a `: ` (colon + space) inside the
 * value breaks strict YAML parsers ("mapping values are not allowed here").
 * Dependency-free check — the same rule the parser enforces.
 */
function checkFrontmatter() {
  let bad = 0;
  for (const dir of ["skills", "skill-data"]) {
    const base = path.join(ROOT, dir);
    if (!fs.existsSync(base)) continue;
    for (const entry of fs.readdirSync(base, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const skill = path.join(base, entry.name, "SKILL.md");
      if (!fs.existsSync(skill)) continue;
      const text = fs.readFileSync(skill, "utf8");
      const m = text.match(/^---\n(.*?)\n---\n/s);
      if (!m) {
        console.error(`build: ${skill}: no frontmatter — aborting`);
        bad++;
        continue;
      }
      for (const line of m[1].split("\n")) {
        const kv = line.match(/^([A-Za-z][\w-]*):\s+(.*)$/);
        if (kv && /:\s/.test(kv[2])) {
          console.error(
            `build: ${skill}: illegal ": " inside the ${kv[1]} value — ` +
            `rewrite without a colon+space (strict YAML parsers reject it)`);
          bad++;
        }
      }
    }
  }
  if (bad) process.exit(1);
}


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
  checkFrontmatter();
  fs.rmSync(DIST, { recursive: true, force: true });
  copyDir(path.join(ROOT, "ajd"), path.join(DIST, "ajd"));
  copyDir(path.join(ROOT, "skills"), path.join(DIST, "skills"));
  copyDir(path.join(ROOT, "skill-data"), path.join(DIST, "skill-data"));
  fs.copyFileSync(path.join(ROOT, "bin", "cli.js"), path.join(DIST, "cli.js"));

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

  const count = fs.readdirSync(DIST, { recursive: true }).length;
  console.log(`build: dist/ staged and verified (${count} entries, python ${py[0]})`);
}

main();
