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
  issue_comment "/ferry plan [inventory]"                     plan, if the commenter may
  issue_comment "/ferry apply [inventory]"                    apply (the engine checks every gate)

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
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from . import config as config_mod
from .credentials import OpenBao, job_credentials
from .engine import Engine
from .locks import LockStore
from .runner import AnsibleRunner
from .sanitize import Redactor
from .vcs.github import GitHubBackend
from .webhook import COMMAND_RE
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


@dataclass
class Job:
    action: str  # plan | apply | unlock | drift
    pr: int = 0
    sha: str = ""
    user: str = ""
    inventory: str | None = None
    delivery: str = ""


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
    runner_factory: Callable[[Path, str, dict], object] = field(
        default=lambda repo, marker, env: AnsibleRunner(repo, marker, env=env))
    clock: Callable[[], float] = time.time

    def __post_init__(self):
        self.jobs: "queue.Queue[Job | None]" = queue.Queue(maxsize=self.settings.max_queued_jobs)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_lock = threading.Lock()
        self.last_job: dict = {}

    # ---- webhook (fast path: decide and enqueue) ------------------------------------------------

    def handle_request(self, headers, body: bytes) -> tuple[int, str]:
        """The HTTP entry point: pick the code host's headers, then handle()."""
        return self.handle(headers.get("X-GitHub-Event", ""), headers.get("X-GitHub-Delivery", ""),
                           headers.get("X-Hub-Signature-256"), body)

    def handle(self, event: str, delivery: str, signature: str | None, body: bytes) -> tuple[int, str]:
        if not verify_signature(self.secret, body, signature):
            return 401, "bad signature"
        with self._seen_lock:
            if delivery and delivery in self._seen:
                return 200, "duplicate delivery"
            if delivery:
                self._seen[delivery] = None
                while len(self._seen) > 1000:
                    self._seen.popitem(last=False)
        try:
            payload = json.loads(body)
        except ValueError:
            return 400, "bad json"
        if payload.get("repository", {}).get("full_name", "").lower() != self.settings.repo.lower():
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
        if action == "closed":
            return self._enqueue(Job("unlock", number, sha, author, delivery=delivery))
        if action not in PLAN_ACTIONS:
            return 202, f"ignored pull_request.{action}"
        if pr.get("draft"):
            return 202, "draft: not planned"
        if pr["head"]["repo"]["full_name"].lower() != self.settings.repo.lower():
            return 202, "fork PRs are not planned"
        if not self.vcs.can_apply(author):
            self._say_waiting(number, sha, author)
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
        action, inventory = m.group(1), m.group(2)
        number = p["issue"]["number"]
        # Both commands need write access, checked here: a refused request never costs a checkout or
        # a credential (the engine checks again, along with every other apply gate).
        if not self.vcs.can_apply(user):
            self.vcs.post_note(number, f"`/ferry {action}` needs write access to this repo; @{user} doesn't have it.")
            return 202, f"{action} refused: no write access"
        cr = self.vcs.get_change_request(number)
        return self._enqueue(Job(action, number, cr.head_sha, user, inventory, delivery))

    def _enqueue(self, job: Job) -> tuple[int, str]:
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            log.warning("queue full (%s jobs): dropped %s for #%s", self.jobs.maxsize, job.action, job.pr)
            return 503, "queue full, try again later"
        return 202, f"{job.action} queued"

    def _say_waiting(self, number: int, sha: str, author: str) -> None:
        marker = f"{WAITING_MARKER} {sha} -->"
        if any(marker in n.body for n in self.vcs.list_notes(number) if n.author == self.vcs.bot_username()):
            return
        self.vcs.post_note(number, (
            f"ferry doesn't plan automatically for authors without {self.access} (@{author}): a plan runs "
            f"this {self.noun}'s playbooks against the hosts. Someone with {self.access} can comment "
            f"`/ferry plan`.\n\n{marker}"))

    # ---- worker (slow path: one job at a time) --------------------------------------------------

    def run_job(self, job: Job) -> dict:
        started = self.clock()
        result = {"job": job.action, "pr": job.pr, "sha": job.sha[:12], "ok": False}
        try:
            if job.action == "unlock":
                main_dir, _ = self.workspace.main(self.settings.default_branch)
                cfg = config_mod.load(main_dir / "ferry.yaml")
                released = [inv.name for inv in cfg.inventories if self.locks.release(inv.name, job.pr)]
                result.update(ok=True, released=released)
            elif job.action == "drift":
                result.update(self._drift())
            else:
                result.update(self._plan_or_apply(job))
        except CheckoutError as e:
            result["error"] = str(e)
            log.warning("job %s for #%s skipped: %s", job.action, job.pr, e)
        except Exception as e:  # report on the PR, keep the service up
            result["error"] = f"{type(e).__name__}: {e}"
            log.exception("job %s for #%s failed", job.action, job.pr)
            if job.pr and job.action in ("plan", "apply"):
                self._report_error(job, result["error"])
        result["seconds"] = round(self.clock() - started, 1)
        self.last_job = result
        log.info("job done: %s", result)
        return result

    def _redactor(self, cfg, env: dict) -> Redactor:
        # The job's own credentials (OpenBao token, ...) live only in the per-job env, not os.environ:
        # redact their values too, in comments and in the echoed log.
        return Redactor(cfg.redact.patterns, cfg.redact.env_vars, cfg.redact.sensitive_paths,
                        environ={**os.environ, **env})

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
                      clock=self.clock, apply_how="comment `/ferry apply` once the PR is approved")

    def _plan_or_apply(self, job: Job) -> dict:
        main_dir, _ = self.workspace.main(self.settings.default_branch)
        cfg = config_mod.load(main_dir / "ferry.yaml")  # from the default branch, never the PR
        checkout = self.workspace.pull_request(job.pr, job.sha)
        try:
            env = self.workspace.collections(checkout)
            with job_credentials(self.bao, self.settings.ssh_user) as creds:
                eng = self._engine(cfg, checkout, {**env, **creds})
                invs = [job.inventory] if job.inventory else [i.name for i in cfg.inventories]
                outcomes = {}
                for inv in invs:
                    out = eng.plan(job.pr, job.sha, job.user, inv) if job.action == "plan" else \
                        eng.apply(job.pr, job.sha, job.user, inv)
                    outcomes[inv] = {"ok": out.ok, "reasons": out.reasons}
            return {"ok": all(o["ok"] for o in outcomes.values()), "inventories": outcomes}
        finally:
            self.workspace.discard(checkout)

    def _drift(self) -> dict:
        main_dir, sha = self.workspace.main(self.settings.default_branch)
        cfg = config_mod.load(main_dir / "ferry.yaml")
        env = self.workspace.collections(main_dir)
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
        red = Redactor([], [], [])
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


def make_handler(service: Service, max_body: int = 25 * 1024 * 1024):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ferry"

        def _send(self, status: int, text: str) -> None:
            data = (text + "\n").encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/healthz":
                # No error text here: it can name internal hosts and paths, and /healthz is unauthenticated.
                last = {k: service.last_job[k] for k in ("job", "pr", "ok", "seconds") if k in service.last_job}
                self._send(200, json.dumps({"ok": True, "queued": service.jobs.qsize(), "last_job": last}))
            else:
                self._send(404, "not found")

        def do_POST(self):
            if self.path != "/webhook":
                return self._send(404, "not found")
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > max_body:
                return self._send(413 if length > max_body else 400, "bad length")
            body = self.rfile.read(length)
            status, text = service.handle_request(self.headers, body)
            self._send(status, text)

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

    return Handler


def serve(service: Service, host: str = "0.0.0.0", port: int = 8080) -> None:
    stop = threading.Event()
    threading.Thread(target=service.worker, args=(stop,), daemon=True, name="ferry-worker").start()
    threading.Thread(target=service.drift_scheduler, args=(stop,), daemon=True, name="ferry-drift").start()
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    log.info("ferry service for %s listening on %s:%s", service.settings.repo, host, port)
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        service.jobs.put(None)
        httpd.server_close()
