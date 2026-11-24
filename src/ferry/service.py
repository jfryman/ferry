"""ferry as a service: a GitHub App webhook receiver driving the engine (Atlantis-style).

  GitHub ──webhook──▶ Service.handle()  verify signature, dedupe, decide, enqueue (fast)
                              │
                         one worker thread (jobs run one at a time)
                              │
          Workspace: default branch (ferry.yaml) + PR at the exact head SHA (playbooks)
          job_credentials: OpenBao token, throwaway SSH key + CA certificate, vault password
          Engine.plan / Engine.apply / Engine.drift  ──▶ comments on the PR as the App

Events:
  pull_request opened/reopened/synchronize/ready_for_review  plan, if the author may (plan rule)
  pull_request closed                                        release the PR's locks
  issue_comment "/ferry plan [inventory] [--sha C]"           plan, if the commenter may
  issue_comment "/ferry apply [inventory] [--sha C]"          apply (the engine checks every gate)

Comment commands are pinned to the head the commenter saw: `--sha` names it; without it, ferry
uses the PR head as of the comment's created_at, which it knows from the signed pull_request events
(state.head_since). A head that arrived after the comment, or one ferry hasn't seen arrive, is
refused with a note; so is a job whose PR moved before the worker got to it. For someone else's PR
the comment vouches for code the commenter read; this is what makes that vouching hold.

Replays: X-GitHub-Delivery isn't covered by the signature, so a captured body could be resent under
a new delivery id. ferry remembers both the delivery id and a digest of the signed body in its state
file (state.SQLiteState, so it survives restarts), and refuses events whose own timestamps (the
comment's created_at/updated_at, the PR's updated_at) are older than max_event_age_minutes.

Plan rule: a plan runs the PR's playbooks (check mode, but check_mode: false tasks, lookups and
plugins run for real) with root SSH access, so ferry plans automatically only for authors with write
access; anyone else's PR waits for a `/ferry plan` comment from someone with write access.
ferry.yaml comes from the default branch, so a PR can't change these rules.

GitLab: gitlab_service.GitLabService is the same service with GitLab's webhook intake; everything
after the decision (the queue, the worker, the engine) is shared.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from . import config as config_mod
from .credentials import OpenBao, job_credentials
from .engine import Engine
from .locks import LockStore
from .runner import AnsibleRunner
from .sanitize import Redactor
from . import __version__
from .metrics import Metrics, reject_reason
from .state import MemoryState
from .vcs.github import GitHubBackend
from .webhook import COMMAND_RE
from .galaxy import collections_path as galaxy_path
from .workspace import CheckoutError, Workspace

log = logging.getLogger("ferry.service")

PLAN_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}
WAITING_MARKER = "<!-- ferry:waiting"


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256: 'sha256=' + HMAC-SHA256(secret, body), compared in constant time."""
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def parse_time(value) -> float | None:
    """A code host timestamp as epoch seconds: GitHub's '2026-01-02T03:04:05Z', GitLab's
    '2026-01-02 03:04:05 UTC' or ISO 8601 with an offset. None if missing or unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    s = re.sub(r"\s*(UTC|Z)$", "+00:00", s)
    s = re.sub(r"\s+([+-]\d{2}):?(\d{2})$", r"\1:\2", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def delivery_keys(delivery: str, body: bytes) -> list[str]:
    """What makes a delivery a duplicate: its id, or the exact signed body under any id."""
    return ([f"id:{delivery}"] if delivery else []) + ["body:" + hashlib.sha256(body).hexdigest()]


COMMANDS = ("plan", "apply")


def parse_allow_commands(value: str | None) -> tuple:
    """FERRY_ALLOW_COMMANDS: comma-separated commands; unset means all of them."""
    if value is None:
        return COMMANDS
    items = [x.strip().lower() for x in value.split(",") if x.strip()]
    bad = [x for x in items if x not in COMMANDS]
    if bad:
        raise ValueError(f"FERRY_ALLOW_COMMANDS: unknown command(s) {bad}; known: {list(COMMANDS)}")
    return tuple(dict.fromkeys(items))


def _valid_id(value) -> bool:
    """A code host object id: a positive int (JSON true is not one)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def job_result(result: dict) -> str:
    """One of metrics.RESULTS: ok; skipped (the PR moved on); refused (a gate said no, nothing ran);
    failed (ansible ran and wasn't clean); error (ferry couldn't finish)."""
    if result.get("skipped") is True:
        return "skipped"
    if result.get("error"):
        return "error"
    if result.get("ok"):
        return "ok"
    outcomes = list((result.get("inventories") or {}).values())
    if outcomes and all(o["ok"] or o.get("reasons") for o in outcomes):
        return "refused"
    return "failed"


class _NoRunner:
    """For the credential-free apply pre-check: it must never run anything."""

    def run(self, req):
        raise RuntimeError("the apply pre-check must not run ansible")


class HeadMoved(Exception):
    """A pinned job's PR head isn't the commit the comment was about any more."""


class NotOpen(Exception):
    """The PR was closed or merged before the job ran."""


class NotHandled(Exception):
    """The PR targets a base branch ferry doesn't handle."""


@dataclass
class Job:
    action: str  # plan | apply | unlock | drift
    pr: int = 0
    sha: str = ""
    user: str = ""
    inventory: str | None = None
    delivery: str = ""
    pinned: bool = False  # from a comment: run exactly `sha`, and say so on the PR if it moved


@dataclass
class Settings:
    repo: str  # OWNER/NAME (GitLab: path_with_namespace) this service is for
    project_id: str | None = None  # GitLab: the numeric project id, also accepted in webhooks
    default_branch: str = "main"
    ssh_user: str | None = None
    drift_interval_hours: float = 0.0  # 0: no scheduled drift runs
    drift_write_report: bool = True
    max_queued_jobs: int = 50  # beyond this, webhooks get 503 (the code host retries or the user re-comments)
    job_timeout_minutes: float = 0.0  # 0: no limit. Keep it below the SSH certificate's TTL.
    # Events whose own timestamps are older than this are refused (replays, very late redeliveries).
    max_event_age_minutes: float = 10.0
    max_clock_skew_seconds: float = 300.0  # how far in the future an event's timestamp may be
    # HTTP: webhook bodies are small; cap them, and drop connections that stall.
    max_body_bytes: int = 1024 * 1024
    request_timeout_seconds: float = 15.0
    max_connections: int = 32
    # ansible-galaxy on the change's requirements.yml (#26): "install" or "skip" (images that ship
    # their collections; requirements.yml is then never read). Runs in the executor when there is one.
    galaxy: str = "install"
    galaxy_server: str | None = None  # pin the Galaxy server; requirements naming other sources fail
    galaxy_timeout_seconds: float = 300.0
    collections_max_age_days: float = 14.0
    executor_socket: str | None = None
    # Switches (#28), set by the operator, never by a repo: which comment commands this ferry takes
    # (FERRY_ALLOW_COMMANDS), automatic plans off (FERRY_DISABLE_AUTOPLAN), and a directory whose
    # `disable-apply` / `disable-autoplan` files switch those off while the service runs.
    allow_commands: tuple = ("plan", "apply")
    disable_autoplan: bool = False
    switch_dir: str | None = None


@dataclass
class Service:
    # How the waiting note names things; the GitLab service says MR / Developer access.
    noun = "PR"
    access = "write access"

    settings: Settings
    vcs: GitHubBackend
    locks: LockStore
    workspace: Workspace
    secret: bytes
    bao: OpenBao | None = None
    state: MemoryState = field(default_factory=MemoryState)  # seen deliveries; SQLiteState in serve
    metrics: Metrics = field(default_factory=Metrics)
    runner_factory: Callable[[Path, str, dict], object] = field(
        default=lambda repo, marker, env: AnsibleRunner(repo, marker, env=env))
    clock: Callable[[], float] = time.time

    def __post_init__(self):
        self.jobs: "queue.Queue[Job | None]" = queue.Queue(maxsize=self.settings.max_queued_jobs)
        self.last_job: dict = {}
        self._job_red: Redactor | None = None  # the running job's redactor, for its error report

    def marker_key(self) -> bytes:
        """Signs ferry's plan/apply markers: derived from the webhook secret, which only the service
        (and the code host) holds, never a PR or a playbook."""
        return hmac.new(self.secret, b"ferry plan marker v1", hashlib.sha256).digest()

    # ---- webhook (fast path: decide and enqueue) ------------------------------------------------

    def receive(self, headers, body: bytes) -> tuple[int, str]:
        """The HTTP entry point: handle the delivery, and count it if it was refused."""
        status, text = self.handle_request(headers, body)
        self.count_reject(status, text)
        return status, text

    def count_reject(self, status: int, text: str) -> None:
        reason = reject_reason(status, text)
        if reason:
            self.metrics.inc("ferry_webhook_rejected_total", reason=reason)

    # ---- switches (#28) ------------------------------------------------------------------------

    def _switch(self, name: str) -> tuple[bool, str]:
        """(on, reason) for the operator's switch file `name` in switch_dir. Read on every request,
        so `touch` and `rm` take effect at once. A directory ferry can't check counts as on."""
        d = self.settings.switch_dir
        if not d:
            return False, ""
        path = os.path.join(d, name)
        try:
            if not os.path.lexists(path):
                return False, ""
        except OSError:
            return True, f"can't check {path}"
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                note = fh.readline(201).strip()
        except OSError:
            note = ""
        return True, note or f"{path} exists"

    def switches(self) -> dict:
        """What this ferry does right now: {"plan", "apply", "autoplan": bool, "why": {off: reason}}."""
        st, why = self.settings, {}
        on = {c: c in st.allow_commands for c in COMMANDS}
        for c in COMMANDS:
            if not on[c]:
                why[c] = "FERRY_ALLOW_COMMANDS"
        frozen, reason = self._switch("disable-apply")
        if on["apply"] and frozen:
            on["apply"], why["apply"] = False, reason
        auto = on["plan"]
        if not on["plan"]:
            why["autoplan"] = "plan is disabled"
        elif st.disable_autoplan:
            auto, why["autoplan"] = False, "FERRY_DISABLE_AUTOPLAN"
        else:
            off, reason = self._switch("disable-autoplan")
            if off:
                auto, why["autoplan"] = False, reason
        return {"apply": on["apply"], "autoplan": auto, "plan": on["plan"], "why": why}

    def _disabled(self, action: str) -> str | None:
        """Why `action` won't run on this ferry now, as the note says it; None if it may."""
        sw = self.switches()
        if action in COMMANDS and not sw[action]:
            why = sw["why"].get(action, "")
            if why.startswith(("FERRY_", "/", "can't check")) or why.endswith(" exists"):
                why = "the operator switched them off"  # paths and variable names stay in the log
            from .render import inline
            return f"{ {'apply': 'applies', 'plan': 'plans'}[action]} are disabled on this ferry: {inline(why)}"
        return None

    def render_metrics(self) -> str:
        try:
            sw = self.switches()
            for k in ("plan", "apply", "autoplan"):
                self.metrics.set("ferry_enabled", 1.0 if sw[k] else 0.0, switch=k)
        except Exception:  # never fail the endpoint
            log.exception("couldn't read the switches")
        count = getattr(self.locks, "count", None)
        live = {"ferry_queue_depth": self.jobs.qsize}
        if count is not None:
            live["ferry_locks_held"] = count
        return self.metrics.render(live, version=__version__)

    def handle_request(self, headers, body: bytes) -> tuple[int, str]:
        """Pick the code host's headers, then handle()."""
        return self.handle(headers.get("X-GitHub-Event", ""), headers.get("X-GitHub-Delivery", ""),
                           headers.get("X-Hub-Signature-256"), body)

    def handle(self, event: str, delivery: str, signature: str | None, body: bytes) -> tuple[int, str]:
        if not verify_signature(self.secret, body, signature):
            return 401, "bad signature"
        return self._deduped(delivery, body, lambda: self._dispatch(event, delivery, body))

    def _deduped(self, delivery: str, body: bytes, dispatch) -> tuple[int, str]:
        """Run dispatch() once per delivery (by id and by body digest), persisted across restarts."""
        keys = delivery_keys(delivery, body)
        if not self.state.first_sight(keys, self.clock()):
            return 200, "duplicate delivery"
        status, text = dispatch()
        if status >= 500:  # not acted on: let the code host's retry through
            self.state.forget(keys)
        return status, text

    def _stale(self, *stamps) -> str | None:
        """Why an event's own timestamps make it unacceptable (too old, in the future, missing)."""
        now = self.clock()
        parsed = [parse_time(s) for s in stamps]
        if not parsed or any(t is None for t in parsed):
            return "event has no readable timestamp"
        oldest, newest = min(parsed), max(parsed)
        if now - oldest > self.settings.max_event_age_minutes * 60:
            return f"stale event (older than {self.settings.max_event_age_minutes:g} minutes)"
        if newest - now > self.settings.max_clock_skew_seconds:
            return "event timestamp is in the future"
        return None

    def _dispatch(self, event: str, delivery: str, body: bytes) -> tuple[int, str]:
        try:
            payload = json.loads(body)
        except ValueError:
            return 400, "bad json"
        if not isinstance(payload, dict) or \
                (payload.get("repository") or {}).get("full_name", "").lower() != self.settings.repo.lower():
            return 202, "not this service's repo"
        if event == "ping":
            return 200, "pong"
        if event == "pull_request":
            return self._on_pull_request(payload, delivery)
        if event == "issue_comment":
            return self._on_comment(payload, delivery)
        return 202, f"ignored event {event}"

    def _on_pull_request(self, p: dict, delivery: str) -> tuple[int, str]:
        pr, action = p["pull_request"], p.get("action")
        number, sha, author = pr["number"], pr["head"]["sha"], pr["user"]["login"]
        # Every action, close included: a replayed `closed` would release an open PR's locks.
        stale = self._stale(pr.get("updated_at"))
        if stale:
            return 202, stale
        # When this head arrived, for pinning comments to the head their author saw (#12).
        self.state.saw_head(number, sha, parse_time(pr["updated_at"]))
        if action == "synchronize":  # whoever pushed: their approval of this PR won't count (#25)
            self.state.saw_pusher(number, (p.get("sender") or {}).get("login") or "", self.clock())
        if action == "closed":
            return self._enqueue(Job("unlock", number, sha, author, delivery=delivery))
        if action not in PLAN_ACTIONS:
            return 202, f"ignored pull_request.{action}"
        if not self.switches()["autoplan"]:
            return 202, "autoplan disabled"
        if pr.get("draft"):
            return 202, "draft: not planned"
        # A deleted fork has head.repo null: a fork all the same.
        if str((pr["head"].get("repo") or {}).get("full_name") or "").lower() != self.settings.repo.lower():
            return 202, "fork PRs are not planned"
        if not self.vcs.can_apply(author):
            self._say_waiting(number, sha, author)
            return 202, "waiting for /ferry plan"
        # A push into a writer's PR by someone else (a bot or App with contents write, a user who lost
        # access) is that pusher's code: the plan rule applies to them too.
        pusher = (p.get("sender") or {}).get("login") or author
        if action == "synchronize" and pusher != author and not self.vcs.can_apply(pusher):
            self._say_waiting(number, sha, pusher, who="pushes from users")
            return 202, "waiting for /ferry plan"
        return self._enqueue(Job("plan", number, sha, author, delivery=delivery))

    def _on_comment(self, p: dict, delivery: str) -> tuple[int, str]:
        if p.get("action") != "created" or "pull_request" not in p.get("issue", {}):
            return 202, "not a new PR comment"
        user = p["comment"]["user"]["login"]
        if user == self.vcs.bot_username():
            return 202, "own comment"
        m = COMMAND_RE.match(p["comment"].get("body", "").strip().splitlines()[0] if p["comment"].get("body") else "")
        if not m or not p["comment"]["body"].lstrip().startswith("/ferry"):
            return 202, "not a ferry command"
        stale = self._stale(p["comment"].get("created_at"), p["comment"].get("updated_at"))
        if stale:
            return 202, stale
        cid = p["comment"].get("id")
        if not _valid_id(cid):
            return 400, "comment id missing or malformed"
        return self._once(self._comment_key("comment", self.settings.repo.lower(), cid),
                          lambda: self._on_command(p, m, delivery))

    def _on_command(self, p: dict, m, delivery: str) -> tuple[int, str]:
        action, inventory, named = m.group(1), m.group(2), m.group(3)
        number = p["issue"]["number"]
        user = p["comment"]["user"]["login"]
        # Both commands need write access, checked here: a refused request never costs a checkout or
        # a credential (the engine checks again, along with every other apply gate).
        if not self.vcs.can_apply(user):
            self.vcs.post_note(number, f"`/ferry {action}` needs write access to this repo; @{user} doesn't have it.")
            return 202, f"{action} refused: no write access"
        off = self._disabled(action)
        if off:
            self.vcs.post_note(number, f"`/ferry {action}` didn't run: {off}.")
            return 202, f"{action} refused: disabled"
        cr = self.vcs.get_change_request(number)
        if not self._open_or_say(cr, action):
            return 202, f"{action} refused: {self.noun} is {cr.state}"
        sha = self._pin_head(cr, action, parse_time(p["comment"]["created_at"]), named)
        if sha is None:
            return 202, f"{action} refused: the head isn't the one commented on"
        return self._enqueue(Job(action, number, sha, user, inventory, delivery, pinned=True))

    @staticmethod
    def _comment_key(kind: str, where, cid: int) -> str:
        return f"{kind}:{where}:{cid}"

    def _once(self, key: str, dispatch) -> tuple[int, str]:
        """A comment triggers at most one job, whatever delivery id or bytes it arrives with (#37): a
        replay re-encoded under a fresh delivery id gets past the delivery dedupe, not this one."""
        if not self.state.first_sight([key], self.clock()):
            return 200, "duplicate comment"
        try:
            status, text = dispatch()
        except BaseException:  # not acted on (the code host failed mid-check): retryable
            self.state.forget([key])
            raise
        if status >= 500:  # not acted on (queue full): the same comment may be retried
            self.state.forget([key])
        return status, text

    def _open_or_say(self, cr, action: str) -> bool:
        """Commands run on open PRs only (as Atlantis): a closed one never sends another close event,
        so a plan's lock would never be released."""
        if cr.open:
            return True
        self.vcs.post_note(cr.id, f"`/ferry {action}` didn't run: this {self.noun} is {cr.state}.")
        return False

    def _head_since(self, number: int, sha: str) -> float | None:
        """When `sha` became the head of `number` (GitHub: from the pull_request events ferry got)."""
        return self.state.head_since(number, sha)

    def _pin_head(self, cr, action: str, said_at: float, named: str | None,
                  payload_head: str | None = None) -> str | None:
        """The head a comment at `said_at` was about, if it's still the head; else note why and None."""
        head = cr.head_sha
        if named:
            if head.lower().startswith(named.lower()):
                return head
            why = f"the comment names `{named[:12]}`, but the {self.noun} head is `{head[:12]}`"
        elif payload_head and payload_head != head:
            why = f"the {self.noun} head moved from `{payload_head[:12]}` to `{head[:12]}` since the comment"
        else:
            since = self._head_since(cr.id, head)
            if since is not None and since < said_at:
                return head
            why = (f"the {self.noun} head `{head[:12]}` arrived after the comment" if since is not None else
                   f"ferry hasn't seen when the {self.noun} head `{head[:12]}` arrived, so it can't tell "
                   "whether the comment was about it")
        self.vcs.post_note(cr.id, (
            f"`/ferry {action}` didn't run: {why}. Read the {self.noun}'s latest commits, then comment "
            f"again, or name the commit you read: `/ferry {action} --sha {head[:12]}`."))
        return None

    def _enqueue(self, job: Job) -> tuple[int, str]:
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            log.warning("queue full (%s jobs): dropped %s for #%s", self.jobs.maxsize, job.action, job.pr)
            return 503, "queue full, try again later"
        self.metrics.inc("ferry_jobs_queued_total", action=job.action)
        return 202, f"{job.action} queued"

    def _say_waiting(self, number: int, sha: str, author: str, who: str = "authors") -> None:
        marker = f"{WAITING_MARKER} {sha} -->"
        if any(marker in n.body for n in self.vcs.list_notes(number) if n.author == self.vcs.bot_username()):
            return
        self.vcs.post_note(number, (
            f"ferry doesn't plan automatically for {who} without {self.access} (@{author}): a plan runs "
            f"this {self.noun}'s playbooks against the hosts. Someone with {self.access} can comment "
            f"`/ferry plan` after reading it (`/ferry plan --sha {sha[:12]}` pins this commit).\n\n{marker}"))

    # ---- worker (slow path: one job at a time) --------------------------------------------------

    def run_job(self, job: Job) -> dict:
        started = self.clock()
        result = {"job": job.action, "pr": job.pr, "sha": job.sha[:12], "ok": False}
        self._job_red = None
        try:
            off = self._disabled(job.action) if job.action in COMMANDS else None
            if job.action == "plan" and not job.pinned and not self.switches()["autoplan"]:
                result.update(error="autoplan disabled", skipped=True)  # switched off while queued
            elif off:  # switched off while the job waited in the queue: nothing runs
                result.update(inventories={"*": {"ok": False, "reasons": [off]}})
                if job.pr:
                    self._report_not_run(job, f"{off}. Nothing ran against the hosts.")
            elif job.action == "unlock":
                result.update(self._unlock(job))
            elif job.action == "drift":
                result.update(self._drift())
            else:
                result.update(self._plan_or_apply(job))
        except CheckoutError as e:
            result.update(error=str(e), skipped=True)
            log.warning("job %s for #%s skipped: %s", job.action, job.pr, e)
            if job.pinned:  # someone asked for this commit: tell them it didn't run
                self._report_moved(job, str(e))
        except HeadMoved as e:
            result.update(error=str(e), skipped=True)
            self._report_moved(job, str(e))
        except NotOpen as e:
            result.update(error=str(e), skipped=True)
            if job.pinned:
                self._report_not_run(job, f"{e}. Nothing ran against the hosts.")
        except NotHandled as e:
            result.update(error=str(e), skipped=True)
            log.info("job %s for #%s not run: %s", job.action, job.pr, e)
            if job.pinned:
                self._report_not_run(job, f"{e}. Nothing ran against the hosts.")
        except Exception as e:  # report on the PR, keep the service up
            result["error"] = f"{type(e).__name__}: {e}"
            log.exception("job %s for #%s failed", job.action, job.pr)
            if job.pr and job.action in ("plan", "apply"):
                self._report_error(job, result["error"])
        finished = self.clock()
        result["seconds"] = round(finished - started, 1)
        result["result"] = job_result(result)
        self.last_job = result
        log.info("job done: %s", result)
        try:
            self.metrics.job_done(job.action, result["result"], finished - started, finished)
            self.state.record_job({"action": job.action, "pr": job.pr or None, "sha": job.sha[:12] or None,
                                   "user": job.user or None, "inventory": job.inventory,
                                   "result": result["result"], "started": started, "seconds": result["seconds"]})
        except Exception:  # bookkeeping never fails a job
            log.exception("couldn't record job %s for #%s", job.action, job.pr)
        return result

    def _unlock(self, job: Job) -> dict:
        """Release the PR's locks straight from the lock table: no git, no ferry.yaml, so an
        unreachable code host can't leak a lock (#14). A PR that was reopened in the meantime keeps
        its locks; if its state can't be read, the (fresh, verified) close event is trusted."""
        try:
            if self.vcs.get_change_request(job.pr).open:
                return {"ok": True, "released": [], "skipped": f"{self.noun} is open again"}
        except Exception as e:
            log.warning("#%s: couldn't read its state (%s); releasing its locks anyway", job.pr, e)
        return {"ok": True, "released": self.locks.release_all(job.pr)}

    def _redactor(self, cfg, env: dict) -> Redactor:
        # The job's own credentials (OpenBao token, ...) live only in the per-job env, not os.environ:
        # redact their values too, in comments and in the echoed log. The vault password lives in a
        # file: its contents are masked as well (#24).
        values = []
        pw_file = env.get("ANSIBLE_VAULT_PASSWORD_FILE")
        if pw_file:
            try:
                raw = Path(pw_file).read_text()
                values += [raw, raw.strip()]
            except OSError:
                log.warning("couldn't read the vault password file to redact it")
        red = Redactor(cfg.redact.patterns, cfg.redact.env_vars, cfg.redact.sensitive_paths,
                       environ={**os.environ, **env}, values=values)
        self._job_red = red  # the newest one has the most to mask: the error report uses it
        return red

    def _runner(self, cfg, checkout: Path, env: dict, red: Redactor):
        runner = self.runner_factory(checkout, cfg.guard.busy_marker, env)
        if isinstance(runner, AnsibleRunner):
            runner.echo = red.text
            if self.settings.job_timeout_minutes > 0:
                runner.timeout = self.settings.job_timeout_minutes * 60
        return runner

    def _engine(self, cfg, checkout: Path, env: dict) -> Engine:
        red = self._redactor(cfg, env)
        return Engine(cfg, checkout, self._runner(cfg, checkout, env, red), self.vcs, self.locks, red,
                      clock=self.clock, apply_how="comment `/ferry apply` once the PR is approved",
                      default_branch=self.settings.default_branch, marker_key=self.marker_key(),
                      pushers=self.state.pushers)

    def _report_moved(self, job: Job, why: str) -> None:
        self._report_not_run(job, f"{why}. Nothing ran against the hosts. Read the new commits, then "
                                  f"comment `/ferry {job.action}` again.")

    def _report_not_run(self, job: Job, text: str) -> None:
        try:
            self.vcs.post_note(job.pr, f"### ferry {job.action}: not run\n\n{text}")
        except Exception:
            log.exception("couldn't report on #%s", job.pr)

    def _plan_or_apply(self, job: Job) -> dict:
        cr = self.vcs.get_change_request(job.pr)
        if not cr.open:  # closed while queued: no checkout, no credentials, no lock
            raise NotOpen(f"the {self.noun} is {cr.state}")
        if job.pinned:
            head = cr.head_sha
            if head != job.sha:
                raise HeadMoved(f"the {self.noun} head moved from `{job.sha[:12]}` to `{head[:12]}` "
                                f"after the `/ferry {job.action}` comment")
        main_dir, _ = self.workspace.main(self.settings.default_branch)
        cfg = config_mod.load(main_dir / "ferry.yaml")  # from the default branch, never the PR
        self._redactor(cfg, {})  # the error report masks ferry.yaml's patterns from here on
        # Only PRs into the default branch or ferry.yaml's base_branches (#22): checked before the PR
        # is checked out or any credential exists. The engine checks again against live state.
        why = Engine(cfg, main_dir, _NoRunner(), default_branch=self.settings.default_branch).base_reason(cr)
        if why:
            raise NotHandled(why.replace("MR", self.noun))
        checkout = self.workspace.pull_request(job.pr, job.sha)
        try:
            env = self._collections(checkout)
            invs = [job.inventory] if job.inventory else [i.name for i in cfg.inventories]
            outcomes: dict = {}
            culprit, why = None, ""
            if job.action == "apply":
                # Every apply gate first, without credentials: a refused apply (no approval, a stale
                # plan) costs no OpenBao login and no certificate. apply() checks again with them.
                pre = Engine(cfg, checkout, _NoRunner(), self.vcs, self.locks, self._redactor(cfg, env),
                             clock=self.clock, default_branch=self.settings.default_branch,
                             marker_key=self.marker_key(), pushers=self.state.pushers)
                bare = pre.bare_apply_reason(job.inventory)
                if bare:  # apply.require_inventory (#27): nothing runs, not even the first one
                    out = pre.apply_precheck(job.pr, job.sha, job.user, None)
                    return {"ok": False, "inventories": {"*": {"ok": False, "reasons": out.reasons}}}
                for inv in invs:
                    out = pre.apply_precheck(job.pr, job.sha, job.user, inv)
                    if out is not None:
                        outcomes[inv] = {"ok": False, "reasons": out.reasons}
                        culprit, why = inv, "was refused"
                        break  # in order: what comes after a refused inventory doesn't run (#27)
            todo = invs[: invs.index(culprit)] if culprit else list(invs)
            if todo:
                with job_credentials(self.bao, self.settings.ssh_user) as creds:
                    eng = self._engine(cfg, checkout, {**env, **creds})
                    for inv in todo:
                        out = eng.plan(job.pr, job.sha, job.user, inv) if job.action == "plan" else \
                            eng.apply(job.pr, job.sha, job.user, inv)
                        outcomes[inv] = {"ok": out.ok, "reasons": out.reasons}
                        if job.action == "apply" and not out.ok:
                            culprit, why = inv, "didn't apply cleanly" if out.run is not None else "was refused"
                            break  # a half-applied staging doesn't go on to production (#27)
            skipped = [inv for inv in invs if inv not in outcomes]
            for inv in skipped:
                outcomes[inv] = {"ok": False, "reasons": [f"not run: `{culprit}` {why}"]}
            if skipped:
                names = ", ".join(f"`{i}`" for i in skipped)
                self.vcs.post_note(job.pr, (
                    f"### ferry apply: {names} not run\n\n`{culprit}` {why}, and ferry stops at the first "
                    f"inventory that doesn't apply. Nothing ran against {names}. Fix `{culprit}`, then apply "
                    "again, or name an inventory: " + ", ".join(f"`/ferry apply {i}`" for i in skipped) + "."))
            return {"ok": all(o["ok"] for o in outcomes.values()),
                    "inventories": {inv: outcomes[inv] for inv in invs}}
        finally:
            self.workspace.discard(checkout)

    def _collections(self, checkout: Path) -> dict:
        """The change's collections, installed on the executor's side when there is one (#26)."""
        st = self.settings
        if st.galaxy == "skip" or not (checkout / "requirements.yml").is_file():
            return {}
        if st.executor_socket:
            from .executor import install_collections
            path = install_collections(st.executor_socket, checkout, timeout=st.galaxy_timeout_seconds,
                                       server=st.galaxy_server, max_age_days=st.collections_max_age_days)
            return {"ANSIBLE_COLLECTIONS_PATH": galaxy_path(path)}
        return self.workspace.collections(checkout, timeout=st.galaxy_timeout_seconds, server=st.galaxy_server,
                                          max_age_days=st.collections_max_age_days)

    def _drift(self) -> dict:
        main_dir, sha = self.workspace.main(self.settings.default_branch)
        cfg = config_mod.load(main_dir / "ferry.yaml")
        env = self._collections(main_dir)
        results = {}
        with job_credentials(self.bao, self.settings.ssh_user) as creds:
            job_env = {**env, **creds}
            red = self._redactor(cfg, job_env)
            eng = Engine(cfg, main_dir, self._runner(cfg, main_dir, job_env, red), redactor=red, clock=self.clock)
            for inv in cfg.inventories:
                out = eng.drift(sha, inv.name, write_report=self.settings.drift_write_report,
                                out_dir=self.workspace.root / "drift" / inv.name)
                results[inv.name] = out.extra
        return {"ok": True, "sha": sha[:12], "drift": results}

    def _report_error(self, job: Job, error: str) -> None:
        # The job's own redactor (ferry.yaml patterns, its credentials and vault password), or, if the
        # job failed before it had one, the built-in patterns and the service's environment.
        red = self._job_red or Redactor([], [], [])
        try:
            self.vcs.post_note(job.pr, f"### ferry {job.action}: error\n\nferry couldn't finish: "
                                       f"`{red.text(error)[:500]}`. Check the service log, then try "
                                       f"`/ferry {job.action}` again.")
        except Exception:
            log.exception("couldn't report the error on #%s", job.pr)

    def worker(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                job = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            if job is None:
                break
            self.run_job(job)

    def drift_scheduler(self, stop: threading.Event) -> None:
        interval = self.settings.drift_interval_hours * 3600
        if interval <= 0:
            return
        while not stop.wait(interval):
            self._enqueue(Job("drift"))


def make_handler(service: Service, max_body: int | None = None):
    max_body = max_body or service.settings.max_body_bytes

    class Handler(BaseHTTPRequestHandler):
        server_version = "ferry"
        # Socket timeout for every read and write: a client that stalls (slowloris) is dropped.
        timeout = service.settings.request_timeout_seconds

        def _send(self, status: int, text: str, ctype: str = "text/plain") -> None:
            data = (text + "\n").encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            path, query = url.path, urllib.parse.parse_qs(url.query)
            if path == "/healthz":
                # No error text here: it can name internal hosts and paths, and /healthz is unauthenticated.
                last = {k: service.last_job[k] for k in ("job", "pr", "ok", "seconds") if k in service.last_job}
                sw = service.switches()
                self._send(200, json.dumps({"ok": True, "queued": service.jobs.qsize(), "last_job": last,
                                            "switches": {k: sw[k] for k in ("plan", "apply", "autoplan")}}))
            elif path == "/metrics":
                self._send(200, service.render_metrics().rstrip("\n"), "text/plain; version=0.0.4; charset=utf-8")
            elif path == "/jobs":
                # Read-only job history: action, PR, short SHA, user, inventory, result, timing. No
                # error text or reasons (unauthenticated, like /healthz).
                try:
                    limit = min(max(int((query.get("limit") or ["50"])[0]), 1), 200)
                except ValueError:
                    return self._send(400, "bad limit")
                self._send(200, json.dumps({"jobs": service.state.jobs(limit)}), "application/json")
            else:
                self._send(404, "not found")

        def _refuse(self, status: int, text: str) -> None:
            service.count_reject(status, text)
            self._send(status, text)

        def do_POST(self):
            if self.path != "/webhook":
                return self._send(404, "not found")
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                return self._refuse(411, "Content-Length required")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > max_body:
                self.close_connection = True  # don't read (or wait for) a body we refuse
                return self._refuse(413 if length > max_body else 400, "bad length")
            body = self.rfile.read(length)
            if len(body) != length:
                return  # the client went away mid-body
            status, text = service.receive(self.headers, body)
            self._send(status, text)

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

    return Handler


class BoundedHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a cap on concurrent connections: past it, new ones are closed at once
    instead of each getting a thread."""

    daemon_threads = True

    def __init__(self, addr, handler, max_connections: int = 32):
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(addr, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            log.warning("too many connections: dropped one from %s", client_address[0])
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve(service: Service, host: str = "0.0.0.0", port: int = 8080) -> None:
    stop = threading.Event()
    threading.Thread(target=service.worker, args=(stop,), daemon=True, name="ferry-worker").start()
    threading.Thread(target=service.drift_scheduler, args=(stop,), daemon=True, name="ferry-drift").start()
    httpd = BoundedHTTPServer((host, port), make_handler(service), service.settings.max_connections)
    log.info("ferry service for %s listening on %s:%s", service.settings.repo, host, port)
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        service.jobs.put(None)
        httpd.server_close()
