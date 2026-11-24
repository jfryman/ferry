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
    base = "main"  # PR 1's base branch, as the API says

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
        g.routes[("GET", f"{R}/pulls/1")] = lambda q, d: (200, pr_json(number=1, sha=self.sha, base=self.base,
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

    def pr_event(self, action="opened", author="pip", draft=False, fork=False, sender=None, at=OPENED_ISO,
                 base=None, head_repo=...):
        repo = {"full_name": "evil/ansible" if fork else "acme/ansible"} if head_repo is ... else head_repo
        return {"action": action, "repository": {"full_name": "acme/ansible"},
                "sender": {"login": sender or author},
                "pull_request": {"number": 1, "draft": draft, "user": {"login": author}, "updated_at": at,
                                 "base": {"ref": base or self.base},
                                 "head": {"sha": self.sha, "repo": repo}}}

    def comment_event(self, body, user="james", at=NOW_ISO, updated=None, cid=None):
        self._cid = getattr(self, "_cid", 1000) + 1  # every comment is a new comment unless cid says
        return {"action": "created", "repository": {"full_name": "acme/ansible"},
                "issue": {"number": 1, "pull_request": {}},
                "comment": {"id": self._cid if cid is None else cid, "user": {"login": user}, "body": body,
                            "created_at": at, "updated_at": updated or at}}

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
        except ConnectionResetError:
            return out  # macOS resets an intentionally rejected connection
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


# ---- metrics and job history (#16) ---------------------------------------------------------------


def get(addr, path):
    with urllib.request.urlopen(f"http://{addr[0]}:{addr[1]}{path}") as resp:
        return resp.status, resp.headers.get("Content-Type"), resp.read().decode()


def test_metrics_and_job_history(world, http):
    world.svc.state = __import__("ferry.state", fromlist=["x"]).SQLiteState(world.tmp / "state.db", job_history=2)
    world.svc.state.saw_head(1, world.sha, NOW - 120)
    world.svc.bao = None
    body = json.dumps(world.comment_event("/ferry plan")).encode()
    hdr = b"POST /webhook HTTP/1.1\r\nHost: x\r\nX-GitHub-Event: issue_comment\r\nConnection: close\r\n"
    assert b" 401 " in raw(http, hdr + b"X-Hub-Signature-256: sha256=00\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    assert b" 413 " in raw(http, hdr + b"Content-Length: 99999\r\n\r\n")
    world.deliver("issue_comment", world.comment_event("/ferry plan", at="2027-01-15T07:00:00Z"))  # stale
    world.svc.count_reject(202, "stale event (older than 10 minutes)")  # deliver() bypasses HTTP: count it
    world.deliver("pull_request", world.pr_event())
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    [plan, apply] = world.drain()
    assert (plan["result"], apply["result"]) == ("ok", "refused")

    status, ctype, text = get(http, "/metrics")
    assert status == 200 and ctype.startswith("text/plain; version=0.0.4")
    for line in ('ferry_jobs_total{action="plan",result="ok"} 1',
                 'ferry_jobs_total{action="apply",result="refused"} 1',
                 'ferry_jobs_total{action="drift",result="error"} 0',
                 'ferry_jobs_queued_total{action="plan"} 1',
                 'ferry_job_duration_seconds_count{action="plan"} 1',
                 'ferry_job_duration_seconds_bucket{action="plan",le="+Inf"} 1',
                 'ferry_webhook_rejected_total{reason="bad_auth"} 1',
                 'ferry_webhook_rejected_total{reason="too_large"} 1',
                 'ferry_webhook_rejected_total{reason="stale"} 1',
                 'ferry_last_job_timestamp_seconds{action="apply"} 1800000000',
                 "ferry_queue_depth 0", "ferry_locks_held 1", "# TYPE ferry_job_duration_seconds histogram"):
        assert line in text.splitlines(), line
    assert 'ferry_build_info{version="' in text
    # Nothing request-derived or secret: no users, commits, repo, secret or token.
    for leak in ("james", "pip", world.sha[:7], "acme", SECRET.decode(), "tok"):
        assert leak not in text, leak

    status, ctype, text = get(http, "/jobs?limit=5")
    jobs = json.loads(text)["jobs"]
    assert ctype == "application/json" and [j["action"] for j in jobs] == ["apply", "plan"]
    assert jobs[0] == {"id": 2, "action": "apply", "pr": 1, "sha": world.sha[:12], "user": "james",
                       "inventory": None, "result": "refused", "started": NOW, "seconds": 0.0,
                       "trigger": "webhook", "run_id": None, "subject": None, "limit": None,
                       "mode": None, "certificate_serial": None, "counts": None, "display_name": None, "playbook": None}
    world.svc.jobs.put(Job("unlock", 1, world.sha, "pip"))
    world.closed = True
    world.drain()
    jobs = json.loads(get(http, "/jobs")[2])["jobs"]
    assert [j["action"] for j in jobs] == ["unlock", "apply"]  # bounded: the newest 2
    with pytest.raises(urllib.error.HTTPError):
        get(http, "/jobs?limit=x")


def test_job_results():
    from ferry.service import job_result
    assert job_result({"ok": True}) == "ok"
    assert job_result({"ok": False, "error": "x", "skipped": True}) == "skipped"
    assert job_result({"ok": False, "error": "RuntimeError: x"}) == "error"
    assert job_result({"ok": False, "inventories": {"a": {"ok": False, "reasons": ["no approval"]}}}) == "refused"
    assert job_result({"ok": False, "inventories": {"a": {"ok": False, "reasons": []}}}) == "failed"


def test_metric_labels_are_escaped_and_bounded():
    from ferry.metrics import Metrics, reject_reason
    m = Metrics()
    m.inc("ferry_jobs_total", action='pl"an\n', result="ok")
    assert 'action="pl\\"an\\n"' in m.render()
    assert reject_reason(202, "plan queued") is None and reject_reason(202, "waiting for /ferry plan") is None
    assert reject_reason(202, "apply refused: no Developer access") == "no_access"
    assert reject_reason(202, "plan refused: the head isn't the one commented on") == "head_moved"
    assert reject_reason(503, "queue full, try again later") == "queue_full"


# ---- #21: closed PRs get no plan, no credentials, no lock -----------------------------------------

def count_credentials(monkeypatch):
    import contextlib
    from ferry import service as service_mod
    minted = []

    @contextlib.contextmanager
    def creds(bao, ssh_user):
        minted.append(1)
        yield {"ANSIBLE_REMOTE_USER": "root"}
    monkeypatch.setattr(service_mod, "job_credentials", creds)
    return minted


@pytest.mark.parametrize("command", ["/ferry plan", "/ferry apply"])
def test_commands_on_a_closed_pr_are_refused_at_the_webhook(world, monkeypatch, command):
    minted = count_credentials(monkeypatch)
    world.closed = True
    status, text = world.deliver("issue_comment", world.comment_event(command))
    assert status == 202 and "closed" in text
    assert world.svc.jobs.empty() and not minted
    assert "is closed" in world.comments[-1]["body"]
    assert world.locks.get("homelab") is None


def test_commands_on_a_merged_pr_are_refused_at_the_webhook(world, monkeypatch):
    minted = count_credentials(monkeypatch)
    world.gh.routes[("GET", f"{R}/pulls/1")] = lambda q, d: (200, pr_json(number=1, sha=world.sha, state="closed",
                                                                          merged=True))
    status, text = world.deliver("issue_comment", world.comment_event("/ferry plan"))
    assert status == 202 and "merged" in text
    assert world.svc.jobs.empty() and not minted and "is merged" in world.comments[-1]["body"]


def test_a_plan_queued_before_the_pr_closed_runs_nothing(world, monkeypatch):
    minted = count_credentials(monkeypatch)
    assert world.deliver("issue_comment", world.comment_event("/ferry plan"))[0] == 202
    world.closed = True  # closed while the job waited in the queue
    [result] = world.drain()
    assert result["result"] == "skipped" and not minted and not world.runner.requests
    assert world.locks.get("homelab") is None
    assert "not run" in world.comments[-1]["body"] and "closed" in world.comments[-1]["body"]


# ---- #22: base branch -------------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", "ready_for_review"])
def test_a_pr_into_another_branch_is_not_planned(world, monkeypatch, action):
    minted = count_credentials(monkeypatch)
    world.base = "feature-x"
    world.deliver("pull_request", world.pr_event(action))
    world.drain()
    assert not minted and not world.runner.requests and world.locks.get("homelab") is None
    assert not any("ferry:plan" in c["body"] for c in world.comments)


@pytest.mark.parametrize("command", ["/ferry plan", "/ferry apply"])
def test_commands_on_a_pr_into_another_branch_are_refused(world, monkeypatch, command):
    minted = count_credentials(monkeypatch)
    world.base = "feature-x"
    world.deliver("issue_comment", world.comment_event(command))
    world.drain()
    assert not minted and not world.runner.requests
    assert "`feature-x`" in world.comments[-1]["body"] and "`main`" in world.comments[-1]["body"]


def test_a_pr_retargeted_after_its_plan_is_not_applied(world, monkeypatch):
    world.deliver("pull_request", world.pr_event())
    world.drain()
    assert world.locks.get("homelab").holder == 1
    minted = count_credentials(monkeypatch)
    world.base = "feature-x"  # retargeted after review
    world.reviews.append({"user": {"login": "james"}, "state": "APPROVED", "commit_id": world.sha})
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    [result] = world.drain()
    assert not result["ok"] and not minted and len(world.runner.requests) == 1  # only the plan ran


def test_an_extra_base_from_the_default_branch_ferry_yaml_is_planned(repo, tmp_path):
    # ferry.yaml on main allows `release`; the PR targets it.
    text = (repo / "ferry.yaml").read_text()
    (repo / "ferry.yaml").write_text("base_branches: [release]\n" + text)
    w = World(repo, tmp_path / "w", runs=(clean_run(),))
    w.base = "release"
    w.deliver("pull_request", w.pr_event())
    [result] = w.drain()
    assert result["ok"] and w.runner.requests


def test_a_pr_cannot_allow_its_own_base(repo, tmp_path):
    """The PR's ferry.yaml adding its base doesn't count: only the default branch's does."""
    w = World(repo, tmp_path / "w", runs=(clean_run(),))
    git(repo, "checkout", "-q", "feature")
    (repo / "ferry.yaml").write_text("base_branches: [feature-x]\n" + (repo / "ferry.yaml").read_text())
    git(repo, "commit", "-qam", "allow my base")
    sha = git(repo, "rev-parse", "HEAD")
    bare = w.tmp / "remote.git"
    git(repo, "push", "-q", str(bare), "feature")
    git(bare, "update-ref", "refs/pull/1/head", sha)
    w.sha = sha
    w.svc.state.saw_head(1, sha, NOW - 60)
    w.base = "feature-x"
    w.deliver("pull_request", w.pr_event())
    [result] = w.drain()
    assert not w.runner.requests and "targets `feature-x`" in result["error"]


def test_a_pr_whose_head_repo_was_deleted_is_not_planned(world):
    status, text = world.deliver("pull_request", world.pr_event(head_repo=None))
    assert status == 202 and "fork" in text and world.svc.jobs.empty()


# ---- #23: up to date with the base ------------------------------------------------------------------

def test_a_pr_behind_main_is_planned_with_a_warning_and_its_apply_refused(world, monkeypatch):
    world.gh.behind_by = 1
    world.deliver("pull_request", world.pr_event())
    [plan] = world.drain()
    assert "1 commit(s) behind `main`" in world.comments[-1]["body"]
    minted = count_credentials(monkeypatch)
    world.reviews.append({"user": {"login": "james"}, "state": "APPROVED", "commit_id": world.sha})
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    [refused] = world.drain()
    assert not refused["ok"] and not minted and len(world.runner.requests) == 1
    assert any("rebase or merge `main`" in r for r in refused["inventories"]["homelab"]["reasons"])
    world.gh.behind_by = 0  # rebased (same head here; the fake only answers the count)
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    [applied] = world.drain()
    assert applied["ok"] and len(minted) == 1


def test_main_moving_after_the_plan_refuses_the_apply(world, monkeypatch):
    world.deliver("pull_request", world.pr_event())
    world.drain()
    world.reviews.append({"user": {"login": "james"}, "state": "APPROVED", "commit_id": world.sha})
    world.gh.behind_by = 2  # another PR merged to main
    minted = count_credentials(monkeypatch)
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    [refused] = world.drain()
    assert not refused["ok"] and not minted


def test_a_compare_api_error_refuses_the_apply(world, monkeypatch):
    world.deliver("pull_request", world.pr_event())
    world.drain()
    world.reviews.append({"user": {"login": "james"}, "state": "APPROVED", "commit_id": world.sha})
    world.gh.routes[("GET", f"{R}/compare/main...{world.sha}")] = lambda q, d: (502, {"message": "x"})
    minted = count_credentials(monkeypatch)
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    [refused] = world.drain()
    assert not refused["ok"] and not minted
    assert any("couldn't tell" in r for r in refused["inventories"]["homelab"]["reasons"])


# ---- #37: a comment triggers at most one job -----------------------------------------------------

def reserialized(payload: dict) -> bytes:
    """The same JSON, different bytes (what a replayer that re-encodes the body sends)."""
    return json.dumps(payload, indent=1, sort_keys=True).encode()


def test_a_comment_replayed_with_a_fresh_delivery_id_and_new_bytes_is_refused(world):
    ev = world.comment_event("/ferry plan", cid=4242)
    first = json.dumps(ev).encode()
    assert world.svc.handle("issue_comment", "d-1", sign(first), first) == (202, "plan queued")
    again = reserialized(ev)
    assert again != first
    status, text = world.svc.handle("issue_comment", "d-2", sign(again), again)
    assert (status, text) == (200, "duplicate comment")
    assert world.svc.jobs.qsize() == 1


def test_two_comments_with_the_same_text_are_two_commands(world):
    assert world.deliver("issue_comment", world.comment_event("/ferry plan"))[1] == "plan queued"
    assert world.deliver("issue_comment", world.comment_event("/ferry plan"))[1] == "plan queued"


def test_the_comment_id_survives_a_restart(world, tmp_path):
    from ferry.state import SQLiteState
    world.svc.state = SQLiteState(tmp_path / "state.db")
    world.svc.state.saw_head(1, world.sha, NOW - 120)
    ev = world.comment_event("/ferry plan", cid=7)
    b1 = json.dumps(ev).encode()
    world.svc.handle("issue_comment", "d-1", sign(b1), b1)
    world.svc.state = SQLiteState(tmp_path / "state.db")  # a new process, same state file
    b2 = reserialized(ev)
    assert world.svc.handle("issue_comment", "d-2", sign(b2), b2)[1] == "duplicate comment"


def test_a_comment_refused_for_a_full_queue_can_be_retried(world):
    world.svc.jobs = __import__("queue").Queue(maxsize=1)
    world.svc.jobs.put(Job("drift"))
    ev = world.comment_event("/ferry plan", cid=9)
    b1 = json.dumps(ev).encode()
    assert world.svc.handle("issue_comment", "d-1", sign(b1), b1)[0] == 503
    world.svc.jobs.get()
    assert world.svc.handle("issue_comment", "d-1", sign(b1), b1)[1] == "plan queued"


@pytest.mark.parametrize("cid", [None, "", "7", 0, -1, True])
def test_a_command_without_a_usable_comment_id_is_refused(world, cid):
    ev = world.comment_event("/ferry plan")
    if cid is None:
        del ev["comment"]["id"]
    else:
        ev["comment"]["id"] = cid
    status, text = world.deliver("issue_comment", ev)
    assert status == 400 and "comment id" in text and world.svc.jobs.empty()


def test_duplicate_comments_are_counted(world):
    ev = world.comment_event("/ferry plan", cid=11)
    b1, b2 = json.dumps(ev).encode(), reserialized(ev)
    world.svc.receive({"X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": "a", "X-Hub-Signature-256": sign(b1)}, b1)
    world.svc.receive({"X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": "b", "X-Hub-Signature-256": sign(b2)}, b2)
    assert 'ferry_webhook_rejected_total{reason="duplicate"} 1' in world.svc.render_metrics()


# ---- #24: signed markers, the vault password and the error path ----------------------------------

def creds_with_vault_password(monkeypatch, tmp_path, password="vault-pw-Correct-Horse", token="s.jobtoken12345678"):
    import contextlib
    from ferry import service as service_mod
    pw = tmp_path / "vault-password"
    pw.write_text(password + "\n")

    @contextlib.contextmanager
    def creds(bao, ssh_user):
        yield {"ANSIBLE_REMOTE_USER": "root", "ANSIBLE_VAULT_PASSWORD_FILE": str(pw), "VAULT_TOKEN": token}
    monkeypatch.setattr(service_mod, "job_credentials", creds)
    return password, token


def test_the_service_signs_its_markers(world):
    from ferry import render
    world.deliver("pull_request", world.pr_event())
    world.drain()
    body = world.comments[-1]["body"]
    assert render.trusted_marker(body, world.svc.marker_key())[1]["sha"] == world.sha
    assert render.trusted_marker(body, b"other key") is None


def test_a_task_that_prints_the_vault_password_is_masked(world, monkeypatch, tmp_path):
    password, _ = creds_with_vault_password(monkeypatch, tmp_path)
    world.runner.queue = [([ev_result("ntp-1", "Leak", "changed", diff=[
        {"before": "", "after": f"pw={password.upper()} and {password}\n", "before_header": "/tmp/x",
         "after_header": "/tmp/x"}]), ev_stats(**{"ntp-1": stats(changed=1)})], 0)]
    world.deliver("pull_request", world.pr_event())
    world.drain()
    body = world.comments[-1]["body"]
    assert password not in body and "[REDACTED]" in body


def test_the_error_path_uses_the_jobs_redactor(world, monkeypatch, tmp_path):
    password, token = creds_with_vault_password(monkeypatch, tmp_path)

    class Boom:
        def run(self, req):
            raise RuntimeError(f"ansible died: token {token} password {password} secret=hunter22secret")
    world.svc.runner_factory = lambda checkout, marker, env: Boom()
    world.deliver("pull_request", world.pr_event())
    [result] = world.drain()
    body = world.comments[-1]["body"]
    assert "ferry plan: error" in body
    assert token not in body and password not in body and "hunter22secret" not in body
    assert "[REDACTED]" in body


def test_the_error_path_applies_ferry_yaml_patterns(repo, tmp_path, monkeypatch):
    text = (repo / "ferry.yaml").read_text()
    (repo / "ferry.yaml").write_text(text.replace("policy:", "redact:\n  patterns: ['corp-[0-9]{6}']\npolicy:"))
    w = World(repo, tmp_path / "w")

    class Boom:
        def run(self, req):
            raise RuntimeError("failed on corp-123456")
    w.svc.runner_factory = lambda checkout, marker, env: Boom()
    w.deliver("pull_request", w.pr_event())
    w.drain()
    assert "corp-123456" not in w.comments[-1]["body"]


# ---- #25: approvals from people who pushed to the PR ------------------------------------------------

def add_writer(world, user):
    world.writers.add(user)
    world.gh.routes[("GET", f"{R}/collaborators/{user}/permission")] = lambda q, d: (200, {"role_name": "write"})


def approve(world, user):
    world.reviews.append({"user": {"login": user}, "state": "APPROVED", "commit_id": world.sha})


def test_a_writer_who_pushed_then_approved_does_not_count(world, monkeypatch):
    world.deliver("pull_request", world.pr_event())  # pip's PR
    world.drain()
    world.deliver("pull_request", world.pr_event("synchronize", sender="james"))  # james pushes to it
    world.drain()
    approve(world, "james")
    minted = count_credentials(monkeypatch)
    world.deliver("issue_comment", world.comment_event("/ferry apply", user="james"))
    [refused] = world.drain()
    reasons = refused["inventories"]["homelab"]["reasons"]
    assert not refused["ok"] and not minted
    assert any("`james` pushed or authored" in r for r in reasons), reasons


def test_a_commit_author_from_the_api_does_not_count_either(world, monkeypatch):
    world.gh.routes[("GET", f"{R}/pulls/1/commits")] = [
        {"sha": world.sha, "author": {"login": "pip"}, "committer": {"login": "james"}}]
    world.deliver("pull_request", world.pr_event())
    world.drain()
    approve(world, "james")
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    [refused] = world.drain()
    assert not refused["ok"]
    assert any("`james`" in r for r in refused["inventories"]["homelab"]["reasons"])


def test_a_third_writer_approving_applies(world):
    add_writer(world, "carol")
    world.deliver("pull_request", world.pr_event())
    world.drain()
    world.deliver("pull_request", world.pr_event("synchronize", sender="james"))
    world.drain()
    approve(world, "carol")
    world.deliver("issue_comment", world.comment_event("/ferry apply", user="james"))
    [applied] = world.drain()
    assert applied["ok"], applied


def test_pushers_survive_a_restart(tmp_path):
    from ferry.state import SQLiteState
    a = SQLiteState(tmp_path / "s.db")
    a.saw_pusher(1, "james", NOW)
    a.saw_pusher(1, "", NOW)
    assert SQLiteState(tmp_path / "s.db").pushers(1) == {"james"}
    assert SQLiteState(tmp_path / "s.db").pushers(2) == set()


def test_unreadable_commits_refuse(world, monkeypatch):
    world.deliver("pull_request", world.pr_event())
    world.drain()
    approve(world, "james")
    world.gh.routes[("GET", f"{R}/pulls/1/commits")] = lambda q, d: (502, {"message": "x"})
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    [refused] = world.drain()
    assert not refused["ok"] and any("couldn't tell who pushed" in r for r in refused["inventories"]["homelab"]["reasons"])


# ---- #26: collections ---------------------------------------------------------------------------------

def add_requirements(world, repo):
    git(repo, "checkout", "-q", "feature")
    (repo / "requirements.yml").write_text("collections: [community.general]\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "requirements")
    world.sha = git(repo, "rev-parse", "HEAD")
    bare = world.tmp / "remote.git"
    git(repo, "push", "-q", str(bare), "feature")
    git(bare, "update-ref", "refs/pull/1/head", world.sha)
    world.svc.state.saw_head(1, world.sha, NOW - 60)


def test_galaxy_skip_never_reads_requirements(world, repo, monkeypatch):
    add_requirements(world, repo)
    world.svc.settings.galaxy = "skip"
    monkeypatch.setattr(world.svc.workspace, "collections", lambda *a, **k: pytest.fail("installed"))
    world.deliver("pull_request", world.pr_event())
    [r] = world.drain()
    assert r["ok"] and "ANSIBLE_COLLECTIONS_PATH" not in world.envs[-1][1]


def test_with_an_executor_galaxy_runs_there(world, repo, monkeypatch):
    from ferry import executor as executor_mod
    add_requirements(world, repo)
    world.svc.settings.executor_socket = "/run/ferry/executor.sock"
    asked = []
    monkeypatch.setattr(executor_mod, "install_collections",
                        lambda sock, co, **kw: asked.append((sock, kw)) or "/executor/collections/abc")
    monkeypatch.setattr(world.svc.workspace, "collections", lambda *a, **k: pytest.fail("ran in the service"))
    world.deliver("pull_request", world.pr_event())
    [r] = world.drain()
    assert r["ok"] and asked[0][0] == "/run/ferry/executor.sock" and asked[0][1]["timeout"] == 300.0
    assert world.envs[-1][1]["ANSIBLE_COLLECTIONS_PATH"].startswith("/executor/collections/abc")


def test_a_galaxy_timeout_fails_the_job_visibly_and_the_next_job_runs(world, repo, monkeypatch):
    from ferry import galaxy
    add_requirements(world, repo)

    calls = []

    def hang_once(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise galaxy.GalaxyError("ansible-galaxy timed out after 300s")
        return {}
    monkeypatch.setattr(world.svc.workspace, "collections", hang_once)
    world.deliver("pull_request", world.pr_event())
    [r] = world.drain()
    assert r["result"] == "error" and "timed out" in world.comments[-1]["body"]
    world.deliver("issue_comment", world.comment_event("/ferry plan"))
    [r2] = world.drain()
    assert r2["ok"], r2


# ---- #27: bare apply over several inventories ----------------------------------------------------

def failed_run():
    return ([ev_result("ntp-1", "Write chrony.conf", "failed", role="chrony", msg="boom"),
             ev_stats(**{"ntp-1": stats(ok=1, failures=1)})], 2)


def two_inventory_world(repo, tmp_path, apply_extra="", runs=()):
    (repo / "inventories/prod").mkdir(parents=True, exist_ok=True)
    (repo / "inventories/prod/hosts.yml").write_text("all: {hosts: {ntp-1: {}}}\n")
    (repo / "ferry.yaml").write_text(
        "inventories:\n  - {name: homelab, path: inventories/homelab}\n  - {name: prod, path: inventories/prod}\n"
        "policy:\n  check_mode_false_allow: ['roles/printer_guard/**']\n"
        "apply:\n  max_plan_age_hours: 24\n" + apply_extra)
    w = World(repo, tmp_path / "w", runs=runs)
    w.gh.routes[("GET", f"{R}/pulls/1/files")] = [{"filename": "inventories/homelab/host_vars/ntp-1.yml"},
                                                  {"filename": "inventories/prod/host_vars/ntp-1.yml"}]
    return w


def test_a_bare_apply_stops_at_the_first_inventory_that_fails(repo, tmp_path, monkeypatch):
    w = two_inventory_world(repo, tmp_path, runs=(clean_run(), clean_run(), failed_run(), clean_run()))
    w.deliver("issue_comment", w.comment_event("/ferry plan"))
    [plan] = w.drain()
    assert plan["ok"], plan
    w.reviews.append({"user": {"login": "james"}, "state": "APPROVED", "commit_id": w.sha})
    w.deliver("issue_comment", w.comment_event("/ferry apply"))
    [applied] = w.drain()
    assert not applied["ok"]
    assert len(w.runner.requests) == 3  # two plans and the failed homelab apply: prod never ran
    assert applied["inventories"]["prod"]["reasons"] == ["not run: `homelab` didn't apply cleanly"]
    assert "`prod`" in w.comments[-1]["body"] and "not run" in w.comments[-1]["body"]


def test_a_refused_first_inventory_stops_the_rest(repo, tmp_path, monkeypatch):
    w = two_inventory_world(repo, tmp_path, runs=(clean_run(), clean_run()))
    w.deliver("issue_comment", w.comment_event("/ferry plan prod"))
    w.drain()  # only prod planned: homelab has no plan and is refused
    w.reviews.append({"user": {"login": "james"}, "state": "APPROVED", "commit_id": w.sha})
    minted = count_credentials(monkeypatch)
    w.deliver("issue_comment", w.comment_event("/ferry apply"))
    [applied] = w.drain()
    assert not applied["ok"] and not minted and len(w.runner.requests) == 1


def test_require_inventory_refuses_a_bare_apply_before_credentials(repo, tmp_path, monkeypatch):
    w = two_inventory_world(repo, tmp_path, apply_extra="  require_inventory: true\n",
                            runs=(clean_run(), clean_run(), clean_run()))
    w.deliver("issue_comment", w.comment_event("/ferry plan"))
    w.drain()
    w.reviews.append({"user": {"login": "james"}, "state": "APPROVED", "commit_id": w.sha})
    minted = count_credentials(monkeypatch)
    w.deliver("issue_comment", w.comment_event("/ferry apply"))
    [refused] = w.drain()
    assert not refused["ok"] and not minted and "`homelab`, `prod`" in w.comments[-1]["body"]
    w.deliver("issue_comment", w.comment_event("/ferry apply homelab"))
    [applied] = w.drain()
    assert applied["ok"], applied and len(minted) == 1


def test_a_single_inventory_repo_applies_bare_as_before(world):
    world.svc.settings  # one inventory, require_inventory off: unchanged
    world.deliver("pull_request", world.pr_event())
    world.drain()
    world.reviews.append({"user": {"login": "james"}, "state": "APPROVED", "commit_id": world.sha})
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    [applied] = world.drain()
    assert applied["ok"]


# ---- #28: switches that stop applies / autoplans without stopping the service -------------------

def test_apply_not_in_allowed_commands_is_refused_at_the_webhook(world, monkeypatch):
    minted = count_credentials(monkeypatch)
    world.svc.settings.allow_commands = ("plan",)
    status, text = world.deliver("issue_comment", world.comment_event("/ferry apply"))
    assert status == 202 and "disabled" in text and world.svc.jobs.empty() and not minted
    assert "applies are disabled on this ferry" in world.comments[-1]["body"]
    assert world.deliver("issue_comment", world.comment_event("/ferry plan"))[1] == "plan queued"


def test_the_freeze_file_stops_applies_while_the_service_runs(world, tmp_path, monkeypatch):
    freeze = tmp_path / "switches" / "disable-apply"
    world.svc.settings.switch_dir = str(freeze.parent)
    world.deliver("pull_request", world.pr_event())
    world.drain()
    world.reviews.append({"user": {"login": "james"}, "state": "APPROVED", "commit_id": world.sha})
    freeze.parent.mkdir()
    freeze.write_text("change freeze until Monday\n")
    minted = count_credentials(monkeypatch)
    status, text = world.deliver("issue_comment", world.comment_event("/ferry apply"))
    assert "disabled" in text and world.svc.jobs.empty() and not minted
    assert "change freeze until Monday" in world.comments[-1]["body"]
    freeze.unlink()
    assert world.deliver("issue_comment", world.comment_event("/ferry apply"))[1] == "apply queued"
    [applied] = world.drain()
    assert applied["ok"]


def test_a_queued_apply_is_rechecked_when_the_worker_picks_it_up(world, tmp_path, monkeypatch):
    world.svc.settings.switch_dir = str(tmp_path)
    world.deliver("pull_request", world.pr_event())
    world.drain()
    world.reviews.append({"user": {"login": "james"}, "state": "APPROVED", "commit_id": world.sha})
    assert world.deliver("issue_comment", world.comment_event("/ferry apply"))[1] == "apply queued"
    (tmp_path / "disable-apply").touch()  # frozen while the job waited
    minted = count_credentials(monkeypatch)
    [r] = world.drain()
    assert not r["ok"] and r["result"] == "refused" and not minted and len(world.runner.requests) == 1
    assert "applies are disabled" in world.comments[-1]["body"]


def test_autoplan_disabled_by_env_or_file(world, tmp_path):
    world.svc.settings.disable_autoplan = True
    status, text = world.deliver("pull_request", world.pr_event())
    assert status == 202 and "autoplan" in text and world.svc.jobs.empty()
    assert world.deliver("issue_comment", world.comment_event("/ferry plan"))[1] == "plan queued"
    world.drain()
    world.svc.settings.disable_autoplan = False
    world.svc.settings.switch_dir = str(tmp_path)
    (tmp_path / "disable-autoplan").touch()
    assert "autoplan" in world.deliver("pull_request", world.pr_event("synchronize"))[1]
    (tmp_path / "disable-autoplan").unlink()
    assert world.deliver("pull_request", world.pr_event("synchronize"))[1] == "plan queued"


def test_a_queued_autoplan_is_dropped_once_autoplan_is_off(world, tmp_path, monkeypatch):
    world.svc.settings.switch_dir = str(tmp_path)
    world.deliver("pull_request", world.pr_event())
    (tmp_path / "disable-autoplan").touch()
    minted = count_credentials(monkeypatch)
    [r] = world.drain()
    assert r["result"] == "skipped" and not minted and not world.runner.requests


def test_close_still_releases_locks_while_frozen(world, tmp_path):
    world.deliver("pull_request", world.pr_event())
    world.drain()
    world.svc.settings.switch_dir = str(tmp_path)
    (tmp_path / "disable-apply").touch()
    (tmp_path / "disable-autoplan").touch()
    world.closed = True
    world.deliver("pull_request", world.pr_event("closed"))
    [r] = world.drain()
    assert r["released"] == ["homelab"]


def test_healthz_and_metrics_show_the_switches(world, tmp_path):
    world.svc.settings.switch_dir = str(tmp_path)
    (tmp_path / "disable-apply").touch()
    sw = world.svc.switches()
    assert sw == {"apply": False, "autoplan": True, "plan": True,
                  "why": {"apply": f"{tmp_path / 'disable-apply'} exists"}}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(world.svc))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        health = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/healthz").read())
        assert health["switches"] == {"apply": False, "autoplan": True, "plan": True}
        metrics = urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/metrics").read().decode()
        assert 'ferry_enabled{switch="apply"} 0' in metrics and 'ferry_enabled{switch="autoplan"} 1' in metrics
    finally:
        httpd.shutdown()


def test_an_unreadable_switch_dir_fails_closed(world, tmp_path, monkeypatch):
    world.svc.settings.switch_dir = str(tmp_path)
    import ferry.service as service_mod
    real = service_mod.os.path.lexists

    def broken(path):
        if str(path).startswith(str(tmp_path)):
            raise PermissionError("no")
        return real(path)
    monkeypatch.setattr(service_mod.os.path, "lexists", broken)
    sw = world.svc.switches()
    assert sw["apply"] is False and sw["autoplan"] is False


def test_allow_commands_from_the_environment(monkeypatch):
    from ferry.service import parse_allow_commands
    assert parse_allow_commands(None) == ("plan", "apply")
    assert parse_allow_commands("plan") == ("plan",)
    assert parse_allow_commands(" Plan , apply ") == ("plan", "apply")
    assert parse_allow_commands("") == ()
    with pytest.raises(ValueError):
        parse_allow_commands("plan,destroy")


def test_ferry_switch_cli_toggles_the_files_the_service_reads(world, tmp_path, monkeypatch, capsys):
    from ferry import cli
    monkeypatch.setenv("FERRY_SWITCH_DIR", str(tmp_path))
    world.svc.settings.switch_dir = str(tmp_path)
    assert cli.main(["switch", "apply", "off", "--reason", "incident 42"]) == 0
    assert world.svc.switches()["apply"] is False
    assert "incident 42" in world.svc._disabled("apply")
    assert cli.main(["switch"]) == 0 and "disable-apply: ON (incident 42)" in capsys.readouterr().out
    assert cli.main(["switch", "apply", "on"]) == 0
    assert world.svc.switches()["apply"] is True
    assert cli.main(["switch", "autoplan", "off"]) == 0 and world.svc.switches()["autoplan"] is False


def test_serve_reads_the_switch_environment(monkeypatch, tmp_path):
    from ferry import cli
    monkeypatch.setenv("FERRY_ALLOW_COMMANDS", "plan")
    monkeypatch.setenv("FERRY_LOCK_DB", str(tmp_path / "state" / "locks.db"))
    assert cli._allow_commands() == ("plan",) and cli._switch_dir() == str(tmp_path / "state")
    monkeypatch.setenv("FERRY_ALLOW_COMMANDS", "plan,nuke")
    with pytest.raises(SystemExit):
        cli._allow_commands()


# ---- #29: commit statuses ---------------------------------------------------------------------------

def record_statuses(world, status=201):
    posted = []

    def handler(q, d, sha=None):
        posted.append(d)
        return status, d
    world.gh.routes[("POST", f"{R}/statuses/{world.sha}")] = handler
    world.svc.settings.commit_status = True
    return posted


def states(posted, name):
    return [(p["state"], p["description"]) for p in posted if p["context"] == name]


def test_plan_and_apply_move_their_statuses(world):
    posted = record_statuses(world)
    world.deliver("pull_request", world.pr_event())
    world.drain()
    assert [s for s, _ in states(posted, "ferry/plan: homelab")] == ["pending", "success"]
    assert states(posted, "ferry/apply: homelab") == [("pending", "planned; not applied yet")]
    world.reviews.append({"user": {"login": "james"}, "state": "APPROVED", "commit_id": world.sha})
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    world.drain()
    assert [s for s, _ in states(posted, "ferry/apply: homelab")] == ["pending", "pending", "success"]


def test_a_failed_plan_and_a_failed_apply_fail_their_statuses(world):
    posted = record_statuses(world)
    world.runner.queue = [failed_run()]
    world.deliver("pull_request", world.pr_event())
    world.drain()
    assert states(posted, "ferry/plan: homelab")[-1][0] == "failure"
    assert not states(posted, "ferry/apply: homelab")  # nothing to apply from a failed plan
    world.runner.queue = [clean_run(), failed_run()]
    world.deliver("issue_comment", world.comment_event("/ferry plan"))
    world.drain()
    world.reviews.append({"user": {"login": "james"}, "state": "APPROVED", "commit_id": world.sha})
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    world.drain()
    assert states(posted, "ferry/apply: homelab")[-1][0] == "failure"


def test_a_refused_apply_leaves_the_apply_status_pending(world):
    posted = record_statuses(world)
    world.deliver("pull_request", world.pr_event())
    world.drain()
    world.deliver("issue_comment", world.comment_event("/ferry apply"))  # no approval: refused
    world.drain()
    assert [s for s, _ in states(posted, "ferry/apply: homelab")] == ["pending"]


def test_a_noop_plan_marks_apply_done(world):
    posted = record_statuses(world)
    world.gh.routes[("GET", f"{R}/pulls/1/files")] = [{"filename": "README.md"}]
    world.deliver("pull_request", world.pr_event())
    world.drain()
    assert states(posted, "ferry/apply: homelab") == [("success", "nothing to apply")]


def test_statuses_go_on_the_head_that_ran(world, repo):
    posted = record_statuses(world)
    world.deliver("pull_request", world.pr_event())
    world.drain()
    old = world.sha
    add_requirements(world, repo)  # a new push: a new head
    new_posted = record_statuses(world)
    assert world.sha != old and new_posted == []  # nothing on the new head until ferry plans it


def test_without_the_permission_statuses_are_skipped_and_the_job_works(world, caplog):
    posted = record_statuses(world, status=403)
    world.deliver("pull_request", world.pr_event())
    [plan] = world.drain()
    assert plan["ok"] and len(posted) == 1  # tried once, then stopped trying
    assert sum("commit status" in r.message for r in caplog.records) == 1


def test_statuses_are_off_by_default(world):
    posted = record_statuses(world)
    world.svc.settings.commit_status = False
    world.deliver("pull_request", world.pr_event())
    world.drain()
    assert posted == []


def test_an_error_mid_job_fails_the_running_status(world):
    posted = record_statuses(world)

    class Boom:
        def run(self, req):
            raise RuntimeError("ansible died")
    world.svc.runner_factory = lambda checkout, marker, env: Boom()
    world.deliver("pull_request", world.pr_event())
    world.drain()
    assert states(posted, "ferry/plan: homelab")[-1] == ("error", "ferry couldn't finish the plan")


# ---- /ferry unlock, /ferry help, commands that don't parse (#30) ---------------------------------


def test_unlock_by_a_writer_releases_the_lock_and_the_old_plan_no_longer_applies(repo, tmp_path):
    world = World(repo, tmp_path / "world", runs=(clean_run(), clean_run(), clean_run()))
    world.deliver("pull_request", world.pr_event())
    world.drain()
    assert world.locks.get("homelab").holder == 1
    assert world.deliver("issue_comment", world.comment_event("/ferry unlock"))[1] == "unlock queued"
    [unlock] = world.drain()
    assert unlock["ok"] and unlock["released"] == ["homelab"] and unlock["result"] == "ok"
    assert world.locks.get("homelab") is None
    assert "@james released the lock on `homelab`" in world.comments[-1]["body"]
    assert "ferry:unlock" in world.comments[-1]["body"].splitlines()[-1]
    # Another PR can plan the inventory now.
    assert world.locks.try_acquire(new_lock("homelab", 2, "b" * 40, "pip")) is None
    world.locks.release("homelab", 2)
    # Audited in the job history: who unlocked which PR.
    [entry] = [j for j in world.svc.state.jobs() if j["action"] == "unlock"]
    assert (entry["pr"], entry["user"], entry["result"]) == (1, "james", "ok")
    # The plan from before the unlock can't be applied, even approved and with the lock free.
    world.reviews.append({"user": {"login": "james"}, "state": "APPROVED", "commit_id": world.sha})
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    [refused] = world.drain()
    assert not refused["ok"] and refused["inventories"]["homelab"]["reasons"] == [
        "the lock was released with `/ferry unlock` by `james` after the latest plan: re-run the plan"]
    assert world.runner.requests[-1].check is True  # nothing was applied
    # A new plan makes it applicable again.
    world.deliver("issue_comment", world.comment_event("/ferry plan"))
    world.deliver("issue_comment", world.comment_event("/ferry apply"))
    plan, applied = world.drain()
    assert plan["ok"] and applied["ok"], (applied.get("inventories"), applied.get("error"))


def test_unlock_by_a_non_writer_is_refused_and_the_lock_stays(world):
    world.deliver("pull_request", world.pr_event())
    world.drain()
    assert world.deliver("issue_comment", world.comment_event("/ferry unlock", user="mallory"))[1] == \
        "unlock refused: no write access"
    assert world.svc.jobs.empty() and world.locks.get("homelab").holder == 1
    assert "`/ferry unlock` needs write access" in world.comments[-1]["body"]


def test_unlock_on_a_pr_without_the_lock_changes_nothing(world):
    world.locks.try_acquire(new_lock("homelab", 7, "c" * 40, "pip"))  # another PR holds it
    n = len(world.comments)
    world.deliver("issue_comment", world.comment_event("/ferry unlock homelab"))
    [unlock] = world.drain()
    assert not unlock["ok"] and unlock["result"] == "refused"
    assert world.locks.get("homelab").holder == 7
    assert len(world.comments) == n + 1 and "doesn't hold the lock on `homelab`" in world.comments[-1]["body"]


def test_unlock_names_one_inventory(world):
    world.deliver("pull_request", world.pr_event())
    world.drain()
    world.locks.try_acquire(new_lock("lab", 1, world.sha, "pip"))
    world.deliver("issue_comment", world.comment_event("/ferry unlock lab"))
    [unlock] = world.drain()
    assert unlock["released"] == ["lab"] and world.locks.get("homelab").holder == 1


def test_help_and_bad_commands_get_one_rate_limited_reply(world):
    n = len(world.comments)
    assert world.deliver("issue_comment", world.comment_event("/ferry help"))[1] == "help posted"
    assert "`/ferry unlock [inventory]`" in world.comments[-1]["body"]
    later = "2027-01-15T08:10:01Z"  # NOW + 601 s: the next 10-minute window
    world.svc.clock = lambda: NOW + 601
    assert world.deliver("issue_comment", world.comment_event("/ferry aply", at=later))[1] == \
        "not understood: help posted"
    assert "didn't understand that command: /ferry aply" in world.comments[-1]["body"]
    # A second bad command within the limit gets no reply.
    assert world.deliver("issue_comment", world.comment_event("/ferry plan prod extra", at=later))[1] == \
        "help already posted recently"
    assert len(world.comments) == n + 2 and world.svc.jobs.empty()


def test_only_comments_starting_with_ferry_get_a_reply(world):
    for body in ("lgtm", "please /ferry apply this", "/ferryman", "/plan", "> /ferry aply"):
        assert world.deliver("issue_comment", world.comment_event(body))[1] == "not a ferry command", body
    assert world.comments == [] and world.svc.jobs.empty()


def test_a_bad_command_reply_escapes_the_comment(world):
    world.deliver("issue_comment", world.comment_event("/ferry <img src=x> @everyone `x`"))
    body = world.comments[-1]["body"]
    assert "<img" not in body and "@everyone" not in body


@pytest.mark.parametrize('holder', [1, 'run:pending (operator)'])
def test_drift_skips_held_inventory_before_credentials(world, monkeypatch, holder):
    from ferry.locks import Lock
    from unittest.mock import Mock
    world.locks.try_acquire(Lock('homelab', holder, world.sha, 'operator', NOW))
    credentials = Mock(side_effect=AssertionError('must not mint'))
    monkeypatch.setattr('ferry.service.job_credentials', credentials)
    result = world.svc._drift()
    assert result['drift']['homelab'] == {'skipped': 'inventory locked'}
    credentials.assert_not_called()
    assert world.svc.state.jobs()[0]['result'] == 'skipped'
    assert world.locks.get('homelab').holder == holder


def test_drift_holds_lock_before_credentials_and_releases_on_error(world, monkeypatch):
    from contextlib import contextmanager
    @contextmanager
    def credentials(*args):
        assert world.locks.get('homelab').holder == 'drift'
        # A PR cannot reclaim a string holder as if it were a closed change request.
        from ferry.engine import Engine
        from ferry import config
        main, _ = world.svc.workspace.main('main')
        cfg = config.load(main/'ferry.yaml')
        eng = world.svc._engine(cfg, main, {})
        assert 'drift' in eng._lock_conflict('homelab', 1, world.sha, 'operator')
        raise RuntimeError('upstream unavailable')
        yield {}
    monkeypatch.setattr('ferry.service.job_credentials', credentials)
    with pytest.raises(RuntimeError, match='upstream unavailable'):
        world.svc._drift()
    assert world.locks.get('homelab') is None


def test_restart_recovers_drift_only_after_executor_idle(world, monkeypatch):
    from ferry.locks import Lock
    from unittest.mock import Mock
    world.locks.try_acquire(Lock('homelab', 'drift', world.sha, 'schedule', NOW))
    world.svc.settings.executor_socket = '/executor.sock'
    probe = Mock(return_value=False)
    monkeypatch.setattr('ferry.executor.idle_barrier', probe)
    # Fresh service lifetime starts pending. Busy/unavailable executor retains previous holder.
    world.svc.recover_drift()
    assert world.locks.get('homelab').holder == 'drift'
    assert world.svc._drift()['skipped'] == 'waiting for executor idle recovery'
    probe.return_value = True
    world.svc.recover_drift()
    assert world.locks.get('homelab') is None
    # The new service's active drift is never reaped by later periodic/API probes.
    world.locks.try_acquire(Lock('homelab', 'drift', world.sha, 'schedule', NOW))
    world.svc.recover_drift()
    assert world.locks.get('homelab').holder == 'drift'


def test_local_drift_recovery_fails_closed_without_executor(world):
    from ferry.locks import Lock
    world.locks.try_acquire(Lock('homelab', 'drift', world.sha, 'schedule', NOW))
    world.svc.recover_drift()
    assert world.locks.get('homelab').holder == 'drift'
