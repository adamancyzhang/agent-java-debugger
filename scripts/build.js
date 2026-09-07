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
 * Lint the frontmatter of every SKILL.md (skills/ + skill-data/).
 *
 * Dependency-free heuristic, not a YAML parser — it guards the specific
 * regression we hit: a `: ` (colon + space) inside a PLAIN scalar value
 * makes strict YAML parsers fail with "mapping values are not allowed
 * here".  Quoted scalars and block scalars (| / >) are legal YAML and
 * are skipped.  Also enforces that each file carries exactly one `name`
 * and one `description` key, and that skills/ and skill-data/ never
 * register the same skill name twice.
 */
function checkFrontmatter() {
  let bad = false;
  const seen = new Set();
  for (const dir of ["skills", "skill-data"]) {
    const base = path.join(ROOT, dir);
    if (!fs.existsSync(base)) continue;
    for (const entry of fs.readdirSync(base)) {
      const skill = path.join(base, entry, "SKILL.md");
      if (!fs.existsSync(skill)) continue;  // symlinked dirs resolve fine
      if (seen.has(entry)) {
        console.error(`build: duplicate skill name "${entry}" across ` +
          "skills/ and skill-data/ — aborting");
        bad = true;
      }
      seen.add(entry);
      const text = fs.readFileSync(skill, "utf8").replace(/^﻿/, "");
      const m = text.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
      if (!m) {
        console.error(`build: ${skill}: no frontmatter — aborting`);
        bad = true;
        continue;
      }
      let name = 0;
      let description = 0;
      for (const line of m[1].split(/\r?\n/)) {
        const kv = line.match(/^([A-Za-z][\w-]*):[ \t]*(.*)$/);
        if (!kv) continue;
        if (kv[1] === "name") name++;
        if (kv[1] === "description") description++;
        const value = kv[2].trim();
        if (value === "" || value === "|" || value === ">") continue; // block scalar
        if (/^['"]/.test(value)) continue;  // quoted scalar — legal YAML
        if (/:\s/.test(value)) {
          console.error(
            `build: ${skill}: illegal ": " inside the ${kv[1]} value — ` +
            "quote the value or rewrite without a colon+space");
          bad = true;
        }
      }
      if (name !== 1 || description !== 1) {
        console.error(`build: ${skill}: frontmatter needs exactly one ` +
          `"name" and one "description" key (name=${name}, ` +
          `description=${description}) — aborting`);
        bad = true;
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
