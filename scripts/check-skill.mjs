#!/usr/bin/env node
/**
 * Lint the frontmatter of every SKILL.md under skills/ and skill-data/.
 *
 * Dependency-free heuristic (not a full YAML parser) that guards the
 * regressions that break strict parsers and skill loaders:
 *   - the frontmatter block must exist (file starts with ---);
 *   - exactly one `name` and one `description` key per file;
 *   - a PLAIN scalar value may not contain ": " (colon + space) — strict
 *     YAML parsers reject it with "mapping values are not allowed here".
 *     Quoted scalars ('...' or "...") and block scalars (| / >) are legal
 *     YAML and are skipped;
 *   - skills/ and skill-data/ never register the same skill name twice.
 *
 * usage: node scripts/check-skill.mjs   (exit 0 = OK)
 * checkSkills() is exported so build scripts can import it, or callers can
 * spawn `node scripts/check-skill.mjs`.
 */
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

export function checkSkills(root = ROOT) {
  let bad = false;
  const seen = new Set();
  for (const dir of ['skills', 'skill-data']) {
    const base = join(root, dir);
    if (!existsSync(base)) continue;
    for (const entry of readdirSync(base)) {
      const skill = join(base, entry, 'SKILL.md');
      if (!existsSync(skill)) continue; // symlinked dirs resolve fine
      const rel = `${dir}/${entry}/SKILL.md`;
      if (seen.has(entry)) {
        console.error(
          `check: ${rel}: duplicate skill name "${entry}" across skills/ and skill-data/`,
        );
        bad = true;
      }
      seen.add(entry);
      const text = readFileSync(skill, 'utf8').replace(/^﻿/, '');
      const m = text.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
      if (!m) {
        console.error(`check: ${rel}: no frontmatter block`);
        bad = true;
        continue;
      }
      let name = 0;
      let description = 0;
      for (const line of m[1].split(/\r?\n/)) {
        const kv = line.match(/^([A-Za-z][\w-]*):[ \t]*(.*)$/);
        if (!kv) continue;
        const [, key, raw] = kv;
        if (key === 'name') name++;
        if (key === 'description') description++;
        const value = raw.trim();
        if (value === '' || value === '|' || value === '>') continue; // block scalar
        if (/^['"]/.test(value)) continue; // quoted scalar — legal YAML
        if (/:\s/.test(value)) {
          console.error(
            `check: ${rel}: illegal ": " inside the ${key} value — quote the value or rewrite without a colon+space`,
          );
          bad = true;
        }
      }
      if (name !== 1 || description !== 1) {
        console.error(
          `check: ${rel}: frontmatter needs exactly one "name" and one "description" key (name=${name}, description=${description})`,
        );
        bad = true;
      }
    }
  }
  return !bad;
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  if (!checkSkills()) process.exit(1);
  console.log('skill frontmatter OK');
}
