"""The service end to end: real git remote, real Workspace and SQLiteLocks, a fake GitHub API and
canned ansible runs. Webhooks are signed exactly as GitHub signs them."""

import hashlib
import hmac
import json
import subprocess
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from fakes import FakeRunner, ev_result, ev_stats, stats
from test_github import FakeGitHub, R, gql_pr, merge_rules, pr as pr_json

from ferry.locks import SQLiteLocks, new_lock
from ferry.service import Job, Service, Settings, make_handler, verify_signature
from ferry.vcs.github import GitHubBackend, GitHubClient
from ferry.workspace import Workspace

SECRET = b"whsec"
BOT = "ferry-acme[bot]"
NOW = 1_800_000_000.0
NOW_ISO = "2027-01-15T08:00:00Z"  # NOW, as GitHub writes timestamps
OPENED_ISO = "2027-01-15T07:58:00Z"  # when PR 1's head arrived


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()


def clean_run():
    return ([ev_result("ntp-1", "Write chrony.conf", "changed", role="chrony"),
             ev_stats(**{"ntp-1": stats(ok=5, changed=1)})], 0)


class World:
    """GitHub (fake API + real git remote), the service, and what it did."""

    closed = False  # what the API says about PR 1

    def __init__(self, repo, tmp_path, runs=()):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp_path
        # The remote: main is the conftest repo; PR 1 changes a host_vars file and, sneakily, ferry.yaml.
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "main")
        git(repo, "checkout", "-qb", "feature")
        hv = repo / "inventories/homelab/host_vars/ntp-1.yml"
        hv.parent.mkdir(parents=True, exist_ok=True)
        hv.write_text("chrony_servers: [10.0.0.4]\n")
        (repo / "ferry.yaml").write_text((repo / "ferry.yaml").read_text() + "  min_approvals: 0\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "pr")
        self.sha = git(repo, "rev-parse", "HEAD")
        bare = tmp_path / "remote.git"
        git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
        git(bare, "update-ref", "refs/pull/1/head", self.sha)

        self.gh = FakeGitHub()
        self.comments = []
        self.reviews = []
        self.writers = {"james", "pip"}
        g = self.gh
        g.routes[("GET", f"{R}/pulls/1")] = lambda q, d: (200, pr_json(number=1, sha=self.sha,
                                                                   state="closed" if self.closed else "open"))
        g.routes[("GET", f"{R}/pulls/1/files")] = [{"filename": "inventories/homelab/host_vars/ntp-1.yml"},
                                                   {"filename": "ferry.yaml"}]
        g.routes[("GET", f"{R}/issues/1/comments")] = lambda q, d: (200, self.comments)
        g.routes[("POST", f"{R}/issues/1/comments")] = self._post
        g.routes[("GET", f"{R}/pulls/1/reviews")] = lambda q, d: (200, self.reviews)
        # A ruleset needs one approval; GitHub's review decision follows the reviews.
        merge_rules(g, lambda: gql_pr(sha=self.sha, decision="APPROVED" if any(
                        r["state"] == "APPROVED" and r["commit_id"] == self.sha for r in self.reviews)
                        else "REVIEW_REQUIRED", merge_state="CLEAN"),
                    rules=[{"type": "pull_request", "ruleset_id": 1,
                            "parameters": {"required_approving_review_count": 1}}])
        for u in ("james", "pip", "mallory"):
            g.routes[("GET", f"{R}/collaborators/{u}/permission")] = (
                lambda q, d, u=u: (200, {"role_name": "write" if u in self.writers else "read"}))
        self.runner = FakeRunner(*runs)
        self.envs = []
        self.locks = SQLiteLocks(tmp_path / "state" / "locks.db")
        self.svc = Service(
            settings=Settings(repo="acme/ansible", ssh_user="root"),
            vcs=GitHubBackend(GitHubClient("tok", transport=g), "acme/ansible", bot_login=BOT),
            locks=self.locks,
            workspace=Workspace(tmp_path / "work", f"file://{bare}"),
            secret=SECRET,
            runner_factory=self._runner,
            clock=lambda: NOW,
        )
        self.svc.state.saw_head(1, self.sha, NOW - 120)  # ferry got PR 1's opened event two minutes ago

    def _runner(self, checkout, marker, env):
        self.envs.append((checkout, env))
        return self.runner

    def _post(self, q, d):
        c = {"id": len(self.comments) + 1, "user": {"login": BOT}, "body": d["body"], "created_at": "t"}
        self.comments.append(c)
        return 201, c

    def deliver(self, event, payload, delivery=None):
        self._n = getattr(self, "_n", 0) + 1
        # Real deliveries differ (ids, timestamps); a test's repeated payloads must too, or the
        # body-digest dedupe would (rightly) call them duplicates.
        body = json.dumps({**payload, "_seq": self._n}).encode()
        delivery = delivery or f"delivery-{self._n}"
        return self.svc.handle(event, delivery, sign(body), body)

    def pr_event(self, action="opened", author="pip", draft=False, fork=False, sender=None, at=OPENED_ISO):
        return {"action": action, "repository": {"full_name": "acme/ansible"},
                "sender": {"login": sender or author},
                "pull_request": {"number": 1, "draft": draft, "user": {"login": author}, "updated_at": at,
                                 "head": {"sha": self.sha, "repo": {"full_name": "evil/ansible" if fork else "acme/ansible"}}}}

    def comment_event(self, body, user="james", at=NOW_ISO, updated=None):
        return {"action": "created", "repository": {"full_name": "acme/ansible"},
                "issue": {"number": 1, "pull_request": {}},
                "comment": {"user": {"login": user}, "body": body, "created_at": at, "updated_at": updated or at}}

    def drain(self):
        out = []
        while not self.svc.jobs.empty():
            out.append(self.svc.run_job(self.svc.jobs.get()))
        return out


@pytest.fixture
def world(repo, tmp_path):
    return World(repo, tmp_path / "world", runs=(clean_run(), clean_run()))


# ---- webhook decisions ---------------------------------------------------------------------------


def test_signature_check():
    body = b'{"x":1}'
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, body, sign(b'{"x":2}'))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "sha1=abc")


def test_unsigned_duplicate_and_foreign_deliveries(world):
    body = json.dumps(world.pr_event()).encode()
    assert world.svc.handle("pull_request", "d1", "sha256=bad", body)[0] == 401
    assert world.svc.handle("pull_request", "d1", sign(body), body)[1] == "plan queued"
    assert world.svc.handle("pull_request", "d1", sign(body), body)[1] == "duplicate delivery"
    other = dict(world.pr_event(), repository={"full_name": "acme/other"})
    assert world.deliver("pull_request", other)[1] == "not this service's repo"
    assert world.deliver("ping", {"repository": {"full_name": "acme/ansible"}})[1] == "pong"
    assert world.svc.jobs.qsize() == 1


def test_plan_rule_waits_for_a_writer(world):
    assert world.deliver("pull_request", world.pr_event(author="mallory"))[1] == "waiting for /ferry plan"
    assert world.deliver("pull_request", world.pr_event("synchronize", author="mallory"))[1] == "waiting for /ferry plan"
    assert len(world.comments) == 1 and "/ferry plan" in world.comments[0]["body"]  # said once per head
    assert world.svc.jobs.empty()
    assert world.deliver("issue_comment", world.comment_event("/ferry plan", user="mallory"))[1] == \
        "plan refused: no write access"
    assert world.deliver("issue_comment", world.comment_event("/ferry plan", user="james"))[1] == "plan queued"
    job = world.svc.jobs.get()
    assert (job.action, job.pr, job.sha, job.user) == ("plan", 1, world.sha, "james")


def test_push_by_a_non_writer_waits(world):
    # pip (a writer) authored the PR; mallory pushed into it: her code, so the plan rule is hers.
    assert world.deliver("pull_request", world.pr_event("synchronize", sender="mallory"))[1] == \
        "waiting for /ferry plan"
    assert world.svc.jobs.empty() and "pushes from users" in world.comments[-1]["body"]
    assert world.deliver("pull_request", world.pr_event("synchronize", sender="james"))[1] == "plan queued"


@pytest.mark.parametrize("kw,why", [({"draft": True}, "draft"), ({"fork": True}, "fork")])
def test_drafts_and_forks_are_not_planned(world, kw, why):
    assert why in world.deliver("pull_request", world.pr_event(**kw))[1]
    assert world.svc.jobs.empty()


def test_comment_parsing(world):
    assert world.deliver("issue_comment", world.comment_event("/plan"))[1] == "not a ferry command"
    assert world.deliver("issue_comment", world.comment_event("lgtm"))[1] == "not a ferry command"
    assert world.deliver("issue_comment", world.comment_event("/ferry apply", user=BOT))[1] == "own comment"
    assert world.deliver("issue_comment", world.comment_event("/ferry apply homelab"))[1] == "apply queued"
    assert world.svc.jobs.get().inventory == "homelab"


def test_apply_from_a_non_writer_is_refused_before_any_job(world):
    calls = []
    world.svc.runner_factory = lambda *a: calls.append(a)
    assert world.deliver("issue_comment", world.comment_event("/ferry apply", user="mallory"))[1] == \
        "apply refused: no write access"
    assert world.svc.jobs.empty() and not calls  # no checkout, no credentials, no runner
    assert "needs write access" in world.comments[-1]["body"]


def test_queue_is_bounded(world):
    world.svc.settings.max_queued_jobs = 2
    world.svc.__post_init__()
    assert world.deliver("issue_comment", world.comment_event("/ferry plan"))[0] == 202
    assert world.deliver("issue_comment", world.comment_event("/ferry plan"))[0] == 202
    assert world.deliver("issue_comment", world.comment_event("/ferry plan")) == (503, "queue full, try again later")
    assert world.svc.jobs.qsize() == 2


# ---- jobs end to end -----------------------------------------------------------------------------


def test_plan_then_apply_then_close(world):
    world.deliver("pull_request", world.pr_event())
    [plan] = world.drain()
    assert plan["ok"], plan
    assert "ferry:plan" in world.comments[-1]["body"]
    assert "comment `/ferry apply`" in world.comments[-1]["body"] and "pipeline" not in world.comments[-1]["body"]
    assert world.locks.get("homelab").holder == 1
    checkout, env = world.envs[-1]
    assert env["ANSIBLE_REMOTE_USER"] == "root" and not checkout.exists()  # PR checkout discarded

    # The PR set min_approvals: 0 in its own ferry.yaml; ferry reads the default branch's, so apply
    # still needs an approval.
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    [refused] = world.drain()
    assert not refused["ok"] and any("approval" in r for r in refused["inventories"]["homelab"]["reasons"])

    world.reviews.append({"user": {"login": "james"}, "state": "APPROVED", "commit_id": world.sha})
    world.deliver("issue_comment", world.comment_event("/ferry apply"), delivery="apply-2")
    [applied] = world.drain()
    assert applied["ok"], applied
    assert world.runner.requests[-1].check is False
    assert "ferry apply" in world.comments[-1]["body"]

    world.closed = True
    world.deliver("pull_request", world.pr_event("closed"))
    [unlock] = world.drain()
    assert unlock["released"] == ["homelab"] and world.locks.get("homelab") is None


def test_pr_that_moved_on_is_skipped_quietly(world):
    world.svc.jobs.put(Job("plan", 1, "0" * 40, "pip"))
    [result] = world.drain()
    assert "moved on" in result["error"] and not world.comments
    assert not world.runner.requests


def test_errors_are_reported_on_the_pr(world):
    def broken(checkout, marker, env):
        raise RuntimeError("ansible-playbook not found")
    world.svc.runner_factory = broken
    world.deliver("pull_request", world.pr_event())
    [result] = world.drain()
    assert "RuntimeError" in result["error"]
    assert "ferry couldn't finish" in world.comments[-1]["body"]


def test_drift_runs_on_the_default_branch(world):
    world.svc.settings.drift_write_report = False
    world.svc.jobs.put(Job("drift"))
    [result] = world.drain()
    assert result["ok"] and "homelab" in result["drift"]
    checkout, _ = world.envs[-1]
    assert checkout.name == "main"
    assert world.runner.requests[-1].check is True and world.runner.requests[-1].limit is None


# ---- HTTP ----------------------------------------------------------------------------------------


def test_http_endpoints(world):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(world.svc))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        body = json.dumps(world.pr_event()).encode()
        req = urllib.request.Request(f"{base}/webhook", data=body, method="POST", headers={
            "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "http-1", "X-Hub-Signature-256": sign(body),
            "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 202 and b"plan queued" in resp.read()
        with urllib.request.urlopen(f"{base}/healthz") as resp:
            health = json.loads(resp.read())
            assert health["ok"] and health["queued"] == 1
        world.svc.last_job = {"job": "plan", "pr": 1, "ok": False, "error": "git fetch https://internal.example/x failed"}
        with urllib.request.urlopen(f"{base}/healthz") as resp:
            assert "error" not in json.loads(resp.read())["last_job"]  # unauthenticated: no error text
        bad = urllib.request.Request(f"{base}/webhook", data=body, method="POST",
                                     headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=0"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad)
        assert e.value.code == 401
    finally:
        httpd.shutdown()


def test_job_credentials_are_redacted(world, repo):
    # The OpenBao token lives only in the per-job env, not os.environ.
    from ferry import config as config_mod
    cfg = config_mod.load(repo / "ferry.yaml")
    red = world.svc._redactor(cfg, {"VAULT_TOKEN": "s.jobtoken-abcdef123456"})
    assert "s.jobtoken-abcdef123456" not in red.text("debug: s.jobtoken-abcdef123456")


# ---- replays (#11) -------------------------------------------------------------------------------


def test_a_replayed_body_is_a_duplicate_under_any_delivery_id(world):
    body = json.dumps(world.comment_event("/ferry plan")).encode()
    assert world.svc.handle("issue_comment", "d1", sign(body), body)[1] == "plan queued"
    # X-GitHub-Delivery isn't signed: the same signed body under a fresh id is still the same event.
    assert world.svc.handle("issue_comment", "d2", sign(body), body)[1] == "duplicate delivery"
    assert world.svc.handle("issue_comment", "", sign(body), body)[1] == "duplicate delivery"
    assert world.svc.jobs.qsize() == 1


def test_seen_deliveries_survive_a_restart(world, tmp_path):
    from ferry.state import SQLiteState
    world.svc.state = SQLiteState(tmp_path / "state.db")
    world.svc.state.saw_head(1, world.sha, NOW - 120)
    body = json.dumps(world.comment_event("/ferry plan")).encode()
    assert world.svc.handle("issue_comment", "d1", sign(body), body)[1] == "plan queued"
    world.svc.state = SQLiteState(tmp_path / "state.db")  # a new process, same file
    assert world.svc.handle("issue_comment", "d9", sign(body), body)[1] == "duplicate delivery"


@pytest.mark.parametrize("kw,why", [
    ({"at": "2027-01-15T07:49:00Z"}, "stale event"),                           # 11 minutes old
    ({"at": "2027-01-15T07:30:00Z", "updated": NOW_ISO}, "stale event"),         # created long ago
    ({"updated": "2027-01-15T07:40:00Z"}, "stale event"),                       # updated_at past the window
    ({"at": "2027-01-15T08:10:00Z"}, "in the future"),
    ({"at": ""}, "no readable timestamp"),
])
def test_old_comments_are_refused(world, kw, why):
    assert why in world.deliver("issue_comment", world.comment_event("/ferry apply", **kw))[1]
    assert world.svc.jobs.empty() and not world.comments


def test_recent_comments_pass(world):
    assert world.deliver("issue_comment", world.comment_event("/ferry plan", at="2027-01-15T07:59:00Z"))[1] == \
        "plan queued"


def test_a_replayed_close_releases_nothing(world):
    # An old `closed` replayed after the PR reopened would free an open PR's locks.
    assert "stale" in world.deliver("pull_request", world.pr_event("closed", at="2027-01-14T08:00:00Z"))[1]
    assert "stale" in world.deliver("pull_request", world.pr_event("opened", at="2027-01-14T08:00:00Z"))[1]
    assert world.svc.jobs.empty()


def test_a_delivery_refused_for_a_full_queue_can_be_retried(world):
    world.svc.settings.max_queued_jobs = 1
    world.svc.__post_init__()
    world.deliver("issue_comment", world.comment_event("/ferry plan"))
    body = json.dumps(world.comment_event("/ferry apply")).encode()
    assert world.svc.handle("issue_comment", "r1", sign(body), body)[0] == 503
    world.svc.jobs.get()
    assert world.svc.handle("issue_comment", "r1", sign(body), body)[1] == "apply queued"  # the retry


def test_parse_time():
    from ferry.service import parse_time
    for s in ("2027-01-15T08:00:00Z", "2027-01-15 08:00:00 UTC", "2027-01-15T08:00:00.000Z",
              "2027-01-15T09:00:00+01:00", "2027-01-15 09:00:00 +0100"):
        assert parse_time(s) == NOW, s
    assert parse_time("yesterday") is None and parse_time(None) is None and parse_time("") is None


def test_state_is_bounded(tmp_path, monkeypatch):
    from ferry import state as state_mod
    monkeypatch.setattr(state_mod, "DELIVERY_MAX_ROWS", 3)
    for st in (state_mod.MemoryState(), state_mod.SQLiteState(tmp_path / "s.db")):
        for i in range(5):
            assert st.first_sight([f"k{i}"], 1000.0 + i)
        assert st.first_sight(["k0"], 1006.0)  # evicted by the cap
        assert not st.first_sight(["k4"], 1006.0)
        assert st.first_sight(["k4"], 1006.0 + state_mod.DELIVERY_TTL + 10)  # expired by the TTL


# ---- HTTP limits (#15) ---------------------------------------------------------------------------


@pytest.fixture
def http(world):
    from ferry.service import BoundedHTTPServer
    world.svc.settings.max_body_bytes = 4096
    world.svc.settings.request_timeout_seconds = 0.5
    httpd = BoundedHTTPServer(("127.0.0.1", 0), make_handler(world.svc), max_connections=2)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address
    httpd.shutdown()
    httpd.server_close()


def raw(addr, data: bytes, wait=2.0) -> bytes:
    import socket
    with socket.create_connection(addr, timeout=wait) as s:
        s.sendall(data)
        out = b""
        try:
            while chunk := s.recv(4096):
                out += chunk
        except TimeoutError:
            return out + b"<no hangup>"
        return out  # the server closed the connection


def test_http_body_limits(http):
    head = b"POST /webhook HTTP/1.1\r\nHost: x\r\nX-GitHub-Event: ping\r\n"
    assert b" 413 " in raw(http, head + b"Content-Length: 5000\r\n\r\n")
    assert b" 400 " in raw(http, head + b"Content-Length: nope\r\n\r\n")
    assert b" 400 " in raw(http, head + b"\r\n")
    assert b" 411 " in raw(http, head + b"Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n")


def test_http_drops_a_stalled_client(http):
    import time as _time
    t = _time.monotonic()
    # Promises 100 bytes, sends 2, then waits: the server hangs up after its timeout.
    assert raw(http, b"POST /webhook HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n{}", wait=5) == b""
    assert _time.monotonic() - t < 3


def test_http_caps_concurrent_connections(http):
    import socket
    held = [socket.create_connection(http) for _ in range(2)]  # both slots, stalled
    try:
        _ = [s.sendall(b"GET /healthz HTTP/1.1\r\n") for s in held]
        import time as _time
        _time.sleep(0.1)
        assert raw(http, b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n", wait=0.3) == b""  # dropped at once
    finally:
        _ = [s.close() for s in held]
    import time as _time
    _time.sleep(0.7)  # the stalled ones time out, freeing their slots
    assert b" 200 " in raw(http, b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")


# ---- comments pin the head (#12) and refused applies cost no credentials (#13) -------------------


def test_a_comment_runs_the_head_it_was_written_against(world):
    assert world.deliver("issue_comment", world.comment_event("/ferry plan"))[1] == "plan queued"
    job = world.svc.jobs.get()
    assert (job.sha, job.pinned) == (world.sha, True)


def test_a_head_pushed_after_the_comment_is_refused(world):
    # ferry hears of a push at 07:59:30; the comment was written at 07:59:00, about the old head.
    old = world.sha
    world.sha = "b" * 40
    world.deliver("pull_request", world.pr_event("synchronize", at="2027-01-15T07:59:30Z"))
    world.svc.jobs.queue.clear()
    assert world.deliver("issue_comment", world.comment_event("/ferry plan", at="2027-01-15T07:59:00Z"))[1] == \
        "plan refused: the head isn't the one commented on"
    assert "arrived after the comment" in world.comments[-1]["body"]
    assert world.svc.jobs.empty() and old != world.sha
    # A comment after the push, or one naming the new commit, is about it.
    assert world.deliver("issue_comment", world.comment_event("/ferry plan"))[1] == "plan queued"
    assert world.deliver("issue_comment", world.comment_event(f"/ferry plan --sha {'b' * 12}",
                                                              at="2027-01-15T07:59:00Z"))[1] == "plan queued"


def test_a_head_ferry_never_saw_arrive_is_refused(world):
    from ferry.state import MemoryState
    world.svc.state = MemoryState()  # e.g. the PR predates the service
    assert world.deliver("issue_comment", world.comment_event("/ferry apply"))[1].startswith("apply refused")
    assert "hasn't seen when" in world.comments[-1]["body"]
    assert f"/ferry apply --sha {world.sha[:12]}" in world.comments[-1]["body"]
    assert world.deliver("issue_comment", world.comment_event("/ferry apply --sha 0000000"))[1].startswith(
        "apply refused")
    assert world.deliver("issue_comment", world.comment_event(f"/ferry apply --sha {world.sha}"))[1] == "apply queued"


def test_a_pinned_job_whose_pr_moved_is_reported_and_runs_nothing(world):
    assert world.deliver("issue_comment", world.comment_event("/ferry plan"))[1] == "plan queued"
    world.sha = "c" * 40
    [result] = world.drain()
    assert "moved from" in result["error"] and not world.runner.requests
    assert "ferry plan: not run" in world.comments[-1]["body"]


def test_a_refused_apply_mints_no_credentials(world, monkeypatch):
    import contextlib
    from ferry import service as service_mod
    minted = []

    @contextlib.contextmanager
    def creds(bao, ssh_user):
        minted.append(1)
        yield {"ANSIBLE_REMOTE_USER": "root"}
    monkeypatch.setattr(service_mod, "job_credentials", creds)
    world.deliver("pull_request", world.pr_event())
    world.drain()
    assert len(minted) == 1
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    [refused] = world.drain()
    assert not refused["ok"] and any("approval" in r for r in refused["inventories"]["homelab"]["reasons"])
    assert len(minted) == 1 and "ferry apply" in world.comments[-1]["body"]
    world.reviews.append({"user": {"login": "james"}, "state": "APPROVED", "commit_id": world.sha})
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    [applied] = world.drain()
    assert applied["ok"], applied
    assert len(minted) == 2


# ---- unlock without git (#14) --------------------------------------------------------------------


def test_close_releases_locks_without_reaching_git(world):
    world.deliver("pull_request", world.pr_event())
    world.drain()
    world.locks.try_acquire(new_lock("retired", 1, world.sha, "pip"))  # an inventory no longer in ferry.yaml
    world.svc.workspace.remote = "file:///nonexistent/gone.git"  # git is unreachable
    world.closed = True
    world.deliver("pull_request", world.pr_event("closed"))
    [unlock] = world.drain()
    assert unlock["ok"] and unlock["released"] == ["homelab", "retired"]
    assert world.locks.get("homelab") is None


def test_a_reopened_pr_keeps_its_locks(world):
    world.deliver("pull_request", world.pr_event())
    world.drain()
    world.deliver("pull_request", world.pr_event("closed"))  # but it's open again by the time the job runs
    [unlock] = world.drain()
    assert unlock["released"] == [] and "open again" in unlock["skipped"]
    assert world.locks.get("homelab").holder == 1
