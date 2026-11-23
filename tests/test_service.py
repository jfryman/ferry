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
from test_github import FakeGitHub, R, pr as pr_json

from ferry.locks import SQLiteLocks
from ferry.service import Job, Service, Settings, make_handler, verify_signature
from ferry.vcs.github import GitHubBackend, GitHubClient
from ferry.workspace import Workspace

SECRET = b"whsec"
BOT = "ferry-acme[bot]"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()


def clean_run():
    return ([ev_result("ntp-1", "Write chrony.conf", "changed", role="chrony"),
             ev_stats(**{"ntp-1": stats(ok=5, changed=1)})], 0)


class World:
    """GitHub (fake API + real git remote), the service, and what it did."""

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
        g.routes[("GET", f"{R}/pulls/1")] = lambda q, d: (200, pr_json(number=1, sha=self.sha))
        g.routes[("GET", f"{R}/pulls/1/files")] = [{"filename": "inventories/homelab/host_vars/ntp-1.yml"},
                                                   {"filename": "ferry.yaml"}]
        g.routes[("GET", f"{R}/issues/1/comments")] = lambda q, d: (200, self.comments)
        g.routes[("POST", f"{R}/issues/1/comments")] = self._post
        g.routes[("GET", f"{R}/pulls/1/reviews")] = lambda q, d: (200, self.reviews)
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
            clock=lambda: 1_800_000_000.0,
        )

    def _runner(self, checkout, marker, env):
        self.envs.append((checkout, env))
        return self.runner

    def _post(self, q, d):
        c = {"id": len(self.comments) + 1, "user": {"login": BOT}, "body": d["body"], "created_at": "t"}
        self.comments.append(c)
        return 201, c

    def deliver(self, event, payload, delivery=None):
        body = json.dumps(payload).encode()
        self._n = getattr(self, "_n", 0) + 1
        delivery = delivery or f"delivery-{self._n}"
        return self.svc.handle(event, delivery, sign(body), body)

    def pr_event(self, action="opened", author="pip", draft=False, fork=False):
        return {"action": action, "repository": {"full_name": "acme/ansible"},
                "pull_request": {"number": 1, "draft": draft, "user": {"login": author},
                                 "head": {"sha": self.sha, "repo": {"full_name": "evil/ansible" if fork else "acme/ansible"}}}}

    def comment_event(self, body, user="james"):
        return {"action": "created", "repository": {"full_name": "acme/ansible"},
                "issue": {"number": 1, "pull_request": {}}, "comment": {"user": {"login": user}, "body": body}}

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
        bad = urllib.request.Request(f"{base}/webhook", data=body, method="POST",
                                     headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=0"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad)
        assert e.value.code == 401
    finally:
        httpd.shutdown()
