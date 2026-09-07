"""Installed skill lookup: `ajd skills [name]`.

Layout: <root>/skills/agent-java-debugger/SKILL.md is the tool's own skill
(the only one loaded into the agent context); <root>/skill-data/<name>/SKILL.md
holds on-demand extension content (e.g. `skills oinone` — fetched via the
command when needed, so it costs no context until then).  The root comes
from the AJD_ROOT env var (set by the npm launcher) or the package's
parent directory.
"""

import os
import re

_FRONTMATTER = re.compile(r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)", re.DOTALL)

MAIN_SKILL = "agent-java-debugger"


def root_dir():
    root = os.environ.get("AJD_ROOT")
    if root:
        return root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def skill_dir(name):
    """Directory holding SKILL.md for `name` (skills/ for the main skill,
    skill-data/ for extensions)."""
    base = os.path.join(root_dir(),
                        "skills" if name == MAIN_SKILL else "skill-data")
    return os.path.join(base, name)


def list_skills():
    """[(name, description), ...] — the main skill plus every extension."""
    out = []
    for base, entries in _roots():
        for entry in entries:
            path = os.path.join(base, entry, "SKILL.md")
            if os.path.isfile(path):
                out.append((entry, _description(path)))
    # main skill first, extensions after
    out.sort(key=lambda pair: pair[0] != MAIN_SKILL)
    return out


def _roots():
    """[(base_dir, entry_names), ...] for skills/ and skill-data/."""
    roots = []
    for name in ("skills", "skill-data"):
        base = os.path.join(root_dir(), name)
        if os.path.isdir(base):
            roots.append((base, sorted(os.listdir(base))))
    return roots


def read_skill(name):
    """Full SKILL.md text of a skill/extension, or None."""
    path = os.path.join(skill_dir(name), "SKILL.md")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read().lstrip("﻿")  # tolerate a UTF-8 BOM


def _description(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    text = text.lstrip("﻿")  # tolerate a UTF-8 BOM
    m = _FRONTMATTER.match(text)
    if not m:
        return ""
    value = None
    for line in m.group(1).splitlines():
        match = re.match(r"^description[ \t]*:(.*)$", line)
        if match:
            value = match.group(1).strip()
            break
    if value is None:
        return ""
    # Strip a surrounding single/double-quote pair (YAML quoted scalar).
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value
