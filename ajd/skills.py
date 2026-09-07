"""Installed skill lookup: `ajd skills [name]` reads the skill-data dir.

Layout: <root>/skill-data/<skill-name>/SKILL.md (+ references/).  The root
comes from the AJD_ROOT env var (set by the npm launcher) or the package's
parent directory.
"""

import os
import re

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def root_dir():
    root = os.environ.get("AJD_ROOT")
    if root:
        return root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def skill_dir(name):
    return os.path.join(root_dir(), "skill-data", name)


def list_skills():
    """[(name, description), ...] for every skill in skill-data/."""
    base = os.path.join(root_dir(), "skill-data")
    out = []
    if not os.path.isdir(base):
        return out
    for entry in sorted(os.listdir(base)):
        path = os.path.join(base, entry, "SKILL.md")
        if not os.path.isfile(path):
            continue
        out.append((entry, _description(path)))
    return out


def read_skill(name):
    """Full SKILL.md text of a skill, or None."""
    path = os.path.join(skill_dir(name), "SKILL.md")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _description(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    m = _FRONTMATTER.match(text)
    if not m:
        return ""
    for line in m.group(1).splitlines():
        if line.startswith("description:"):
            return line[len("description:"):].strip()
    return ""
