"""The engine: plan, apply, drift, unlock. No CI or HTTP knowledge in here.

Drivers (ferry.cli for GitLab CI, ferry.webhook for a future service) build an Engine from a
Config, a VCSBackend, a LockStore and a Runner, then call one method.
"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import policy, render, scope as scope_mod
from .config import Config
from .locks import LockStore, new_lock
from .results import RunResult
from .runner import PLAYBOOK_DIR, RunRequest, Runner
from .sanitize import Redactor
from .vcs.base import VCSBackend


@dataclass
class Outcome:
    ok: bool
    action: str
    reasons: list[str] = field(default_factory=list)
    run: RunResult | None = None
    note_body: str = ""
    extra: dict = field(default_factory=dict)


class Engine:
    def __init__(
        self,
        cfg: Config,
        repo_dir: str | Path,
        runner: Runner,
        vcs: VCSBackend | None = None,
        locks: LockStore | None = None,
        redactor: Redactor | None = None,
        clock=time.time,
        job_url: str = "",
        apply_how: str | None = None,
    ):
        self.cfg = cfg
        self.repo = Path(repo_dir)
        self.runner = runner
        self.vcs = vcs
        self.locks = locks
        self.red = redactor or Redactor(
            cfg.redact.patterns, cfg.redact.env_vars, cfg.redact.sensitive_paths
        )
        self.clock = clock
        self.job_url = job_url
        # How a person triggers an apply, for the plan comment (a CI job on GitLab, a comment on GitHub).
        self.apply_how = apply_how

    # ---- helpers -------------------------------------------------------------------------------

    def scope_for(self, files: list[str], inventory: str | None = None) -> scope_mod.Scope:
        inv = self.cfg.inventory(inventory)
        return scope_mod.compute(
            self.repo, files, inv.path, self.cfg.playbook, self.cfg.scope.ignore, self.cfg.scope.roles_dir
        )

    def _apply_how(self) -> dict:
        return {"apply_how": self.apply_how} if self.apply_how else {}

    def _post(self, cr_id: int, body: str) -> None:
        if self.vcs is not None:
            self.vcs.post_note(cr_id, body)

    def _refuse(self, action: str, cr_id: int, inventory: str, sha: str, reasons: list[str]) -> Outcome:
        body = render.refusal_comment(action, inventory, sha, reasons, self.red)
        self._post(cr_id, body)
        return Outcome(False, action, reasons, note_body=body)

    def _lock_conflict(self, inventory: str, cr_id: int, sha: str, user: str) -> str | None:
        """Take (or confirm) the inventory lock for cr_id. Returns a reason string on conflict."""
        if self.locks is None:
            return None
        lock = new_lock(inventory, cr_id, sha, user)
        held = self.locks.try_acquire(lock)
        if held is None or held.holder == cr_id:
            return None
        # Stale if the holder MR isn't open any more: take it over.
        stale = False
        if held.holder and self.vcs is not None:
            try:
                stale = not self.vcs.get_change_request(held.holder).open
            except Exception:  # holder MR deleted
                stale = True
        if stale:
            self.locks.release(inventory, held.holder)
            if self.locks.try_acquire(lock) is None:
                return None
        return f"inventory `{inventory}` is locked by !{held.holder} (since {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(held.since))}); merge or close it first"

    def latest_plan(self, cr_id: int, inventory: str) -> dict | None:
        """Newest plan marker posted by ferry's own account (markers from anyone else are ignored)."""
        assert self.vcs is not None
        bot = self.vcs.bot_username()
        latest = None
        for note in self.vcs.list_notes(cr_id):
            if note.author != bot:
                continue
            for kind, data in render.parse_markers(note.body):
                if kind == "plan" and data.get("inventory") == inventory:
                    latest = data
        return latest

    # ---- commands ------------------------------------------------------------------------------

    def policy_check(self) -> list[str]:
        return policy.check(self.repo, self.cfg.policy)

    def plan(self, cr_id: int, sha: str, user: str, inventory: str | None = None) -> Outcome:
        assert self.vcs is not None
        inv = self.cfg.inventory(inventory)
        violations = self.policy_check()
        if violations:
            return self._refuse("plan", cr_id, inv.name, sha, ["policy check failed:"] + violations)
        files = self.vcs.changed_files(cr_id)
        sc = self.scope_for(files, inv.name)
        meta = {
            "v": 1,
            "inventory": inv.name,
            "sha": sha,
            "limit": sc.limit,
            "everything": sc.everything,
            "noop": sc.empty,
            "config": self.cfg.digest(),
            "at": self.clock(),
        }
        if sc.empty:
            meta["clean"] = True
            body = render.plan_comment(inv.name, sha, sc, None, meta, self.red, self.cfg.max_comment_chars, self.job_url,
                                       **self._apply_how())
            self._post(cr_id, body)
            return Outcome(True, "plan", note_body=body, extra=meta)
        conflict = self._lock_conflict(inv.name, cr_id, sha, user)
        if conflict:
            return self._refuse("plan", cr_id, inv.name, sha, [conflict])
        run = self.runner.run(RunRequest(self.cfg.playbook, inv.path, sc.limit, check=True, diff=True))
        meta["clean"] = run.clean
        meta["counts"] = run.counts()
        body = render.plan_comment(inv.name, sha, sc, run, meta, self.red, self.cfg.max_comment_chars, self.job_url,
                                   **self._apply_how())
        self._post(cr_id, body)
        return Outcome(run.clean, "plan", run=run, note_body=body, extra=meta)

    def apply_checks(self, cr_id: int, sha: str, user: str, inventory: str) -> tuple[list[str], dict | None]:
        assert self.vcs is not None
        reasons: list[str] = []
        cr = self.vcs.get_change_request(cr_id)
        if not cr.open:
            reasons.append(f"MR is {cr.state}")
        if cr.head_sha != sha:
            reasons.append(f"this run is for `{sha[:8]}` but the MR head is `{cr.head_sha[:8]}`; re-run for the newest commit")
        if cr.draft:
            reasons.append("MR is a draft")
        if cr.has_conflicts:
            reasons.append("MR has merge conflicts")
        if not cr.discussions_resolved:
            reasons.append("MR has unresolved blocking discussions")
        for r in cr.blocked_reasons:
            reasons.append(f"MR is not mergeable ({r})")
        # The code host's own rules (GitHub branch protection / rulesets) on top of ferry's: ferry never
        # applies what the host would refuse to merge. Only for the head being applied; a stale run is
        # already refused above.
        if cr.open and cr.head_sha == sha:
            reasons.extend(self.vcs.merge_rule_blockers(cr))
        # An approval counts only from someone who could apply it themselves: on a public GitHub repo
        # anyone can leave an approving review, and the code host's own rules may not require more.
        # ferry's own account never counts (a job that got hold of its credentials could approve).
        bot = self.vcs.bot_username()
        approvers = [a for a in self.vcs.approvers(cr_id)
                     if a != bot
                     and (self.cfg.apply.allow_author_approval or a != cr.author)
                     and self.vcs.can_apply(a)]
        if len(approvers) < self.cfg.apply.min_approvals:
            reasons.append(
                f"needs {self.cfg.apply.min_approvals} approval(s)"
                + ("" if self.cfg.apply.allow_author_approval else " from someone other than the author")
                + " with write access (Developer+ on GitLab)"
                + f", has {len(approvers)}"
            )
        allowed = self.cfg.apply.allowed_users
        if allowed and user not in allowed:
            reasons.append(f"`{user}` is not in apply.allowed_users")
        # Both checks: being listed doesn't outlive losing access to the repo.
        if not self.vcs.can_apply(user):
            reasons.append(f"`{user}` may not apply here (needs Developer+ on GitLab, write on GitHub)")

        plan = self.latest_plan(cr_id, inventory)
        if plan is None:
            reasons.append("no ferry plan on this MR yet")
        else:
            if plan.get("sha") != cr.head_sha:
                reasons.append(f"latest plan is for `{str(plan.get('sha'))[:8]}`, not the MR head `{cr.head_sha[:8]}`: re-run the plan")
            if plan.get("config") != self.cfg.digest():
                reasons.append("ferry.yaml changed since the plan: re-run the plan")
            if self.cfg.apply.require_clean_plan and not plan.get("clean"):
                reasons.append("latest plan was not clean (failed, unreachable or blocked hosts)")
            age_h = (self.clock() - float(plan.get("at", 0))) / 3600
            if age_h > self.cfg.apply.max_plan_age_hours:
                reasons.append(f"latest plan is {age_h:.1f}h old (max {self.cfg.apply.max_plan_age_hours}h): re-run the plan")
        return reasons, plan

    def apply(self, cr_id: int, sha: str, user: str, inventory: str | None = None) -> Outcome:
        assert self.vcs is not None
        inv = self.cfg.inventory(inventory)
        violations = self.policy_check()
        reasons, plan = self.apply_checks(cr_id, sha, user, inv.name)
        if violations:
            reasons += ["policy check failed:"] + violations
        if plan is not None and not reasons:
            # Recompute scope at this SHA: it must be exactly what was planned.
            sc = self.scope_for(self.vcs.changed_files(cr_id), inv.name)
            if sc.limit != plan.get("limit") or sc.empty != bool(plan.get("noop")):
                reasons.append("scope differs from the plan's: re-run the plan")
        if reasons:
            return self._refuse("apply", cr_id, inv.name, sha, reasons)
        assert plan is not None
        if plan.get("noop"):
            body = f"### ferry apply: `{inv.name}` @ `{sha[:8]}`\n\nNothing to apply: no host is affected."
            self._post(cr_id, body)
            return Outcome(True, "apply", note_body=body)
        conflict = self._lock_conflict(inv.name, cr_id, sha, user)
        if conflict:
            return self._refuse("apply", cr_id, inv.name, sha, [conflict])
        run = self.runner.run(RunRequest(self.cfg.playbook, inv.path, plan.get("limit"), check=False, diff=True))
        meta = {"v": 1, "inventory": inv.name, "sha": sha, "limit": plan.get("limit"), "ok": run.clean,
                "at": self.clock(), "user": user, "counts": run.counts()}
        body = render.apply_comment(inv.name, sha, run, meta, self.red, self.cfg.max_comment_chars, self.job_url)
        self._post(cr_id, body)
        return Outcome(run.clean, "apply", run=run, note_body=body, extra=meta)

    def unlock_for_commit(self, sha: str, inventory: str | None = None) -> list[int]:
        """After a merge to main: release locks held by the MRs that contained this commit."""
        assert self.vcs is not None and self.locks is not None
        released = []
        invs = [self.cfg.inventory(inventory)] if inventory else self.cfg.inventories
        for cr_id in self.vcs.change_requests_for_commit(sha):
            for inv in invs:
                if self.locks.release(inv.name, cr_id):
                    released.append(cr_id)
        return released

    # ---- drift ---------------------------------------------------------------------------------

    def drift(self, sha: str, inventory: str | None = None, write_report: bool = True,
              out_dir: str | Path | None = None) -> Outcome:
        """Check-mode run over every host at `sha` (main). Writes per-host metrics to each host's
        textfile collector (via ferry's drift_report playbook) and a combined .prom/.json locally."""
        inv = self.cfg.inventory(inventory)
        run = self.runner.run(RunRequest(self.cfg.playbook, inv.path, None, check=True, diff=True))
        now = self.clock()
        metrics = {}
        report_hosts = []
        summary = {}
        for name, h in sorted(run.hosts.items()):
            summary[name] = {"state": h.state, "changed": h.changed, "failed": h.failed, "blocked": h.blocked}
            if h.unreachable or h.blocked:
                continue  # can't (or mustn't) write there; its textfile goes stale -> alert
            metrics[name] = drift_textfile(inv.name, sha, now, h.changed, h.failed)
            report_hosts.append(name)
        out = Path(out_dir) if out_dir else None
        if out is not None:
            out.mkdir(parents=True, exist_ok=True)
            (out / "drift.json").write_text(json.dumps({"sha": sha, "inventory": inv.name, "at": now,
                                                        "hosts": summary}, indent=2, sort_keys=True))
            (out / "drift.prom").write_text(drift_combined(inv.name, sha, now, run))
        report_rc = None
        if write_report and report_hosts:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                json.dump({
                    "ferry_report_hosts": ",".join(report_hosts),
                    "ferry_drift_metrics": metrics,
                    "ferry_textfile_dir": self.cfg.drift.textfile_dir,
                    "ferry_textfile_name": self.cfg.drift.textfile_name,
                    "ferry_report_pre_role": self.cfg.drift.report_pre_role,
                    "ferry_report_become": self.cfg.drift.become,
                }, fh)
                vars_file = fh.name
            try:
                rep = self.runner.run(RunRequest(str(PLAYBOOK_DIR / "drift_report.yml"), inv.path,
                                                 ",".join(report_hosts), check=False, diff=False,
                                                 extra_vars_file=vars_file,
                                                 extra_env={"ANSIBLE_ROLES_PATH": str(self.repo / self.cfg.scope.roles_dir)}))
                report_rc = rep.rc
            finally:
                Path(vars_file).unlink(missing_ok=True)
        drifted = [n for n, s in summary.items() if s["changed"]]
        ok = run.rc in (0, 2) and not any(s["state"] in ("failed", "unreachable") for s in summary.values())
        return Outcome(ok, "drift", run=run, extra={"drifted": drifted, "hosts": summary, "report_rc": report_rc})


def drift_textfile(inventory: str, sha: str, now: float, changed: int, failed: int) -> str:
    lab = f'inventory="{inventory}"'
    return "\n".join([
        "# HELP ansible_drift_last_run_timestamp_seconds When ferry last finished a check-mode drift run on this host.",
        "# TYPE ansible_drift_last_run_timestamp_seconds gauge",
        f"ansible_drift_last_run_timestamp_seconds{{{lab}}} {now:.0f}",
        "# HELP ansible_drift_changed_tasks Tasks that check mode says would change (0 = host matches main).",
        "# TYPE ansible_drift_changed_tasks gauge",
        f"ansible_drift_changed_tasks{{{lab}}} {changed}",
        "# HELP ansible_drift_failed_tasks Tasks that failed during the drift check (the check itself is broken).",
        "# TYPE ansible_drift_failed_tasks gauge",
        f"ansible_drift_failed_tasks{{{lab}}} {failed}",
        "# HELP ansible_drift_info Commit the drift run compared against.",
        "# TYPE ansible_drift_info gauge",
        f'ansible_drift_info{{{lab},commit="{sha[:12]}"}} 1',
        "",
    ])


def drift_combined(inventory: str, sha: str, now: float, run: RunResult) -> str:
    """All hosts in one file (CI artifact / Pushgateway-style consumers); labelled by host."""
    lines = [
        "# TYPE ansible_drift_host_state gauge",
    ]
    for name, h in sorted(run.hosts.items()):
        lines.append(f'ansible_drift_host_state{{inventory="{inventory}",host="{name}",state="{h.state}"}} 1')
        lines.append(f'ansible_drift_changed_tasks{{inventory="{inventory}",host="{name}"}} {h.changed}')
    lines.append(f'ansible_drift_run_timestamp_seconds{{inventory="{inventory}",commit="{sha[:12]}"}} {now:.0f}')
    return "\n".join(lines) + "\n"
