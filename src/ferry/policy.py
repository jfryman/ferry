"""Static checks run before every plan/apply.

A plan runs the MR's own code with the inventory's SSH key, so `check_mode: false` (runs for real
during a plan) and controller-side pipe lookups (run commands on the CI runner, where the key is)
are the obvious ways for an unreviewed MR to do more than report a diff. This doesn't make an
untrusted MR safe; it makes the honest mistakes loud. See the threat model in the ferry README.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from .config import PolicyConfig

CHECK_MODE_FALSE = re.compile(r"^\s*-?\s*check_mode\s*:\s*(false|no|False|NO|off)\s*(#.*)?$")


def _files(repo: Path, globs: list[str]) -> list[Path]:
    seen: dict[str, Path] = {}
    for g in globs:
        for p in repo.glob(g):
            if p.is_file() and ".git" not in p.parts:
                seen[str(p)] = p
    return [seen[k] for k in sorted(seen)]


def check(repo: Path, cfg: PolicyConfig) -> list[str]:
    deny = [re.compile(p) for p in cfg.deny_patterns]
    problems = []
    for path in _files(repo, cfg.scan_globs):
        rel = str(path.relative_to(repo))
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(lines, 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if CHECK_MODE_FALSE.match(line) and not any(fnmatch.fnmatch(rel, g) for g in cfg.check_mode_false_allow):
                problems.append(f"{rel}:{n}: `check_mode: false` outside policy.check_mode_false_allow")
            for rx in deny:
                if rx.search(line):
                    problems.append(f"{rel}:{n}: matches denied pattern `{rx.pattern}`")
    return problems
