"""ferry.yaml: per-repo configuration.

Everything has a default so a repo with an empty ferry.yaml and an `inventories/<name>/` layout works.
The file lives in the *config* repo (the one holding playbooks), but in CI it is read from the MR's
checkout, so treat it as untrusted input: nothing here can name a command to run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml


# What an inventory may override from the top-level `apply` (#27): who may apply it, how many approvals.
INVENTORY_APPLY_KEYS = ("allowed_users", "min_approvals", "run_allowed")


@dataclass
class InventoryConfig:
    name: str
    path: str  # directory (or file) relative to the repo root, passed to ansible -i
    # Overrides of the top-level apply rules for this inventory: allowed_users, min_approvals.
    apply: dict = field(default_factory=dict)


@dataclass
class ScopeConfig:
    # Changed files matching these never affect hosts (docs, CI, tests).
    ignore: list[str] = field(
        default_factory=lambda: [
            "*.md",
            "docs/**",
            "lode/**",
            "tests/**",
            ".gitlab-ci.yml",
            ".gitignore",
            ".yamllint*",
            ".ansible-lint*",
            "ci/**",
        ]
    )
    roles_dir: str = "roles"


@dataclass
class RedactConfig:
    # Regexes whose matches are replaced in any text ferry posts (plan/apply comments).
    patterns: list[str] = field(default_factory=list)
    # Exact values of these environment variables are masked wherever they appear.
    env_vars: list[str] = field(default_factory=list)
    # Diffs for files matching these globs are never shown, only "changed".
    sensitive_paths: list[str] = field(
        default_factory=lambda: [
            "/etc/shadow*",
            "/etc/gshadow*",
            "*.key",
            "*.pem",
            "*/.vault-token",
            "*/id_rsa*",
            "*/id_ed25519*",
            "*/ssh_host_*_key",
        ]
    )


@dataclass
class ApplyConfig:
    # CLI requires an explicit matching subject or group for each inventory (both modes).
    run_allowed: list[str] = field(default_factory=list)
    min_approvals: int = 1
    # If false, the MR author's own approval doesn't count.
    allow_author_approval: bool = False
    # Empty: anyone with Developer+ on the project may apply.
    allowed_users: list[str] = field(default_factory=list)
    # Refuse to apply when the plan had failed/unreachable/guard-blocked hosts.
    require_clean_plan: bool = True
    # A plan older than this must be re-run (hosts may have moved since).
    max_plan_age_hours: float = 24.0
    # Refuse to apply a PR that is behind its base branch (Atlantis `undiverged`): ansible applies
    # whole roles, so a stale branch would put the hosts back to what the base had before.
    require_up_to_date: bool = True
    # With several inventories, a bare apply (no inventory named) is refused: name each one.
    require_inventory: bool = False


@dataclass
class GuardConfig:
    # A failed task whose message starts with this marker means "host refused to be touched"
    # (e.g. a 3D printer mid-print), not a broken run. Shown as blocked, not failed.
    busy_marker: str = "FERRY_HOST_BUSY"


@dataclass
class DriftConfig:
    textfile_dir: str = "/var/lib/prometheus/node-exporter"
    textfile_name: str = "ansible_drift.prom"
    # Role run on each host before ferry writes the drift textfile (e.g. the printer guard).
    # Empty string disables.
    report_pre_role: str = ""
    become: bool = True


@dataclass
class PolicyConfig:
    # Paths (globs) where `check_mode: false` is allowed (read-only probes that must run in plan).
    check_mode_false_allow: list[str] = field(default_factory=list)
    # Regexes that are never allowed anywhere in playbooks/roles.
    deny_patterns: list[str] = field(
        default_factory=lambda: [
            r"lookup\(\s*['\"]pipe['\"]",
            r"query\(\s*['\"]pipe['\"]",
            r"(^|\s)(ansible\.builtin\.)?reboot\s*:",
        ]
    )
    scan_globs: list[str] = field(
        default_factory=lambda: ["*.yml", "*.yaml", "playbooks/**/*.yml", "roles/**/*.yml", "roles/**/*.yaml"]
    )


@dataclass
class Config:
    playbook: str = "site.yml"
    inventories: list[InventoryConfig] = field(
        default_factory=lambda: [InventoryConfig("default", "inventory")]
    )
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    redact: RedactConfig = field(default_factory=RedactConfig)
    apply: ApplyConfig = field(default_factory=ApplyConfig)
    guard: GuardConfig = field(default_factory=GuardConfig)
    drift: DriftConfig = field(default_factory=DriftConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    max_comment_chars: int = 60000
    # Base branches ferry handles besides the default branch. A PR into any other branch is never
    # planned or applied: that branch's merge rules (usually none) would be the only review gate.
    # The service reads this from the default branch's ferry.yaml, so a PR can't add its own base.
    base_branches: list[str] = field(default_factory=list)

    def inventory(self, name: str | None) -> InventoryConfig:
        if name is None:
            return self.inventories[0]
        for inv in self.inventories:
            if inv.name == name:
                return inv
        raise KeyError(f"unknown inventory {name!r}; known: {[i.name for i in self.inventories]}")

    def apply_for(self, inventory: str | None) -> ApplyConfig:
        """The apply rules for one inventory: the top-level ones with its overrides."""
        inv = self.inventory(inventory)
        if not inv.apply:
            return self.apply
        return ApplyConfig(**{**asdict(self.apply), **inv.apply})

    def digest(self) -> str:
        """Stable hash: a plan made under one config is not valid for an apply under another."""
        blob = json.dumps(asdict(self), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


def _merge(cls, data: dict | None):
    data = data or {}
    obj = cls()
    for key, value in data.items():
        if not hasattr(obj, key):
            raise ValueError(f"{cls.__name__}: unknown key {key!r}")
        setattr(obj, key, value)
    return obj


def _check_inventory_apply(inv: InventoryConfig) -> None:
    where = f"ferry.yaml: inventory {inv.name!r}: apply"
    if not isinstance(inv.apply, dict):
        raise ValueError(f"{where} must be a mapping")
    for key, value in inv.apply.items():
        if key not in INVENTORY_APPLY_KEYS:
            raise ValueError(f"{where}: only {', '.join(INVENTORY_APPLY_KEYS)} can be set per inventory, not {key!r}")
    users = inv.apply.get("allowed_users", [])
    if not isinstance(users, list) or not all(isinstance(u, str) and u for u in users):
        raise ValueError(f"{where}: allowed_users must be a list of usernames")
    allowed = inv.apply.get("run_allowed", [])
    if not isinstance(allowed, list) or not all(isinstance(x, str) and x for x in allowed):
        raise ValueError(f"{where}: run_allowed must be a list of subjects/groups")
    n = inv.apply.get("min_approvals", 0)
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ValueError(f"{where}: min_approvals must be a whole number >= 0")


def load(path: str | Path) -> Config:
    path = Path(path)
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    raw = raw or {}
    cfg = Config()
    for key, value in raw.items():
        if key == "inventories":
            cfg.inventories = [InventoryConfig(**i) for i in value]
        elif key in ("scope", "redact", "apply", "guard", "drift", "policy"):
            sub = getattr(cfg, key)
            setattr(cfg, key, _merge(type(sub), value))
        elif hasattr(cfg, key):
            setattr(cfg, key, value)
        else:
            raise ValueError(f"ferry.yaml: unknown key {key!r}")
    if not cfg.inventories:
        raise ValueError("ferry.yaml: at least one inventory is required")
    for inv in cfg.inventories:
        _check_inventory_apply(inv)
    if not isinstance(cfg.apply.run_allowed, list) or not all(
            isinstance(x, str) and x for x in cfg.apply.run_allowed):
        raise ValueError("ferry.yaml: apply.run_allowed must be a list of subjects/groups")
    if not isinstance(cfg.base_branches, list) or not all(isinstance(b, str) and b for b in cfg.base_branches):
        raise ValueError("ferry.yaml: base_branches must be a list of branch names")
    return cfg
