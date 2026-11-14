"""Changed files -> the hosts/groups a plan must cover.

Rules, in order (first match wins per file):
  ignore globs (docs, CI, tests)            -> nothing
  <inv>/host_vars/<h>[.yml|/...]            -> host h
  <inv>/group_vars/<g>[.yml|/...]           -> group g   (all -> everything)
  roles/<r>/...                             -> hosts of every play that uses r, directly, through
                                               include_role/import_role, or through role deps
  a playbook in the site.yml import graph   -> that playbook's plays' hosts (site.yml -> everything)
  anything else (inventory file, ansible.cfg, requirements, ferry.yaml, plugins, unknown) -> everything

Over-approximation is the safe direction: a play with a complex host pattern (`a:&b`, `!c`)
widens to everything rather than risk planning less than will be applied.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

ROLE_INCLUDE_KEYS = (
    "include_role",
    "import_role",
    "ansible.builtin.include_role",
    "ansible.builtin.import_role",
)
IMPORT_PLAYBOOK_KEYS = ("import_playbook", "ansible.builtin.import_playbook")
TASK_LIST_KEYS = ("tasks", "pre_tasks", "post_tasks", "handlers", "block", "rescue", "always")


@dataclass
class Scope:
    everything: bool = False
    patterns: set[str] = field(default_factory=set)
    reasons: list[tuple[str, str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.everything and not self.patterns

    @property
    def limit(self) -> str | None:
        """Value for --limit; None means no limit (all hosts)."""
        if self.everything:
            return None
        return ":".join(sorted(self.patterns))

    def describe(self) -> str:
        if self.everything:
            return "all hosts"
        if self.empty:
            return "no hosts"
        return ", ".join(sorted(self.patterns))


def _load_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text()) or []
    except (OSError, yaml.YAMLError):
        return []


def _role_name(entry) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("role") or entry.get("name")
    return None


def _roles_in_tasks(tasks) -> set[str]:
    found: set[str] = set()
    if not isinstance(tasks, list):
        return found
    for task in tasks:
        if not isinstance(task, dict):
            continue
        for key in ROLE_INCLUDE_KEYS:
            if key in task:
                name = _role_name(task[key])
                if name:
                    found.add(name)
        for key in ("block", "rescue", "always"):
            found |= _roles_in_tasks(task.get(key))
    return found


def _simple_pattern(hosts) -> list[str] | None:
    """Play hosts -> plain names, or None when the pattern needs set algebra."""
    if isinstance(hosts, list):
        parts = [str(h) for h in hosts]
    else:
        parts = [p for p in str(hosts).replace(",", ":").split(":") if p]
    if any(p.startswith(("&", "!")) or "*" in p or "{{" in p or "~" in p for p in parts):
        return None
    return parts


@dataclass
class Play:
    file: str
    hosts: list[str] | None  # None: complex pattern -> everything
    roles: set[str]


class RepoGraph:
    def __init__(self, repo: Path, playbook: str, roles_dir: str = "roles"):
        self.repo = repo
        self.roles_dir = roles_dir
        self.plays: list[Play] = []
        self.playbook_files: set[str] = set()
        self._walk(PurePosixPath(playbook))
        self.role_deps = self._role_deps()

    def _walk(self, rel: PurePosixPath):
        key = str(rel)
        if key in self.playbook_files:
            return
        self.playbook_files.add(key)
        data = _load_yaml(self.repo / rel)
        if not isinstance(data, list):
            return
        for play in data:
            if not isinstance(play, dict):
                continue
            imported = next((play[k] for k in IMPORT_PLAYBOOK_KEYS if k in play), None)
            if imported:
                self._walk(PurePosixPath(rel.parent, str(imported)))
                continue
            roles = {r for r in (_role_name(e) for e in play.get("roles", []) or []) if r}
            for k in ("tasks", "pre_tasks", "post_tasks", "handlers"):
                roles |= _roles_in_tasks(play.get(k))
            self.plays.append(Play(key, _simple_pattern(play.get("hosts", "all")), roles))

    def _role_deps(self) -> dict[str, set[str]]:
        deps: dict[str, set[str]] = {}
        root = self.repo / self.roles_dir
        if not root.is_dir():
            return deps
        for role in sorted(p for p in root.iterdir() if p.is_dir()):
            names: set[str] = set()
            meta = _load_yaml(role / "meta" / "main.yml")
            if isinstance(meta, dict):
                names |= {r for r in (_role_name(e) for e in meta.get("dependencies", []) or []) if r}
            for tasks_file in sorted((role / "tasks").glob("*.y*ml")) if (role / "tasks").is_dir() else []:
                names |= _roles_in_tasks(_load_yaml(tasks_file))
            deps[role.name] = names
        return deps

    def roles_using(self, role: str) -> set[str]:
        """role plus every role that (transitively) pulls it in."""
        users = {role}
        changed = True
        while changed:
            changed = False
            for name, deps in self.role_deps.items():
                if name not in users and deps & users:
                    users.add(name)
                    changed = True
        return users


def _first_component(rest: str) -> str:
    name = rest.split("/", 1)[0]
    for ext in (".yml", ".yaml", ".json"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def compute(
    repo: str | Path,
    changed_files: list[str],
    inventory_path: str,
    playbook: str,
    ignore: list[str],
    roles_dir: str = "roles",
) -> Scope:
    repo = Path(repo)
    graph = RepoGraph(repo, playbook, roles_dir)
    scope = Scope()
    inv = inventory_path.rstrip("/")
    inv_dir = inv if (repo / inv).is_dir() else str(PurePosixPath(inv).parent)
    vars_roots = [f"{inv_dir}/host_vars/", "host_vars/"], [f"{inv_dir}/group_vars/", "group_vars/"]

    def everything(reason: str, f: str):
        scope.everything = True
        scope.reasons.append((f, reason))

    for f in sorted(set(changed_files)):
        if any(fnmatch.fnmatch(f, g) for g in ignore):
            scope.reasons.append((f, "ignored"))
            continue
        host_root = next((r for r in vars_roots[0] if f.startswith(r)), None)
        group_root = next((r for r in vars_roots[1] if f.startswith(r)), None)
        if host_root:
            h = _first_component(f[len(host_root):])
            scope.patterns.add(h)
            scope.reasons.append((f, f"host {h}"))
            continue
        if group_root:
            g = _first_component(f[len(group_root):])
            if g == "all":
                everything("group all", f)
            else:
                scope.patterns.add(g)
                scope.reasons.append((f, f"group {g}"))
            continue
        if f.startswith(f"{roles_dir}/"):
            role = f[len(roles_dir) + 1:].split("/", 1)[0]
            users = graph.roles_using(role)
            plays = [p for p in graph.plays if p.roles & users]
            if not plays:
                scope.reasons.append((f, f"role {role} is not used by any play"))
                continue
            for p in plays:
                if p.hosts is None or "all" in p.hosts:
                    everything(f"role {role} (play in {p.file} targets all)", f)
                else:
                    scope.patterns.update(p.hosts)
                    scope.reasons.append((f, f"role {role} -> {', '.join(p.hosts)}"))
            continue
        if f == playbook:
            everything("top-level playbook", f)
            continue
        if f in graph.playbook_files:
            for p in (p for p in graph.plays if p.file == f):
                if p.hosts is None or "all" in p.hosts:
                    everything(f"playbook {f} targets all", f)
                else:
                    scope.patterns.update(p.hosts)
                    scope.reasons.append((f, f"playbook -> {', '.join(p.hosts)}"))
            continue
        everything("not a known host/group/role file", f)

    if scope.everything:
        scope.patterns.clear()
    return scope
