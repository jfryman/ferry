"""ferry serve for GitLab end to end: real git remote with refs/merge-requests/N/head, real Workspace
and SQLiteLocks, a fake GitLab API and canned ansible runs. Webhooks carry X-Gitlab-Token as GitLab
sends it."""

import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from fakes import FakeRunner
from test_gitlab import API, FakeGitLab
from test_service import clean_run, git

from ferry.gitlab_service import GitLabService, verify_token
from ferry.locks import SQLiteLocks, new_lock
from ferry.service import Job, Settings, make_handler
from ferry.vcs.gitlab import GitLabBackend, GitLabClient
from ferry.workspace import Workspace, _git_env

SECRET = b"glsecret"
BOT = "project_9_bot"
NOW = 1_800_000_000.0
NOW_GL = "2027-01-15 08:00:00 UTC"  # NOW, as GitLab writes timestamps in hooks
P = "/projects/acme%2Fansible"
MR = f"{P}/merge_requests/1"
PUSHED = "2026-11-20T12:00:00.000Z"  # when the MR's current head arrived


class World:
    def __init__(self, repo, tmp_path, runs=()):
        tmp_path.mkdir(parents=True, exist_ok=True)
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
        git(repo, "commit", "-qm", "mr")
        self.sha = git(repo, "rev-parse", "HEAD")
        bare = tmp_path / "remote.git"
        git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
        git(bare, "update-ref", "refs/merge-requests/1/head", self.sha)

        self.gl = FakeGitLab()
        self.notes, self.approved_by = [], []
        self.merge_status = "mergeable"
        self.developers = {"alice", "bob"}
        g = self.gl
        g.routes[("GET", MR)] = lambda u, d: (200, self.mr_json())
        g.routes[("GET", f"{MR}/diffs")] = [{"old_path": "inventories/homelab/host_vars/ntp-1.yml",
                                             "new_path": "inventories/homelab/host_vars/ntp-1.yml"},
                                            {"old_path": "ferry.yaml", "new_path": "ferry.yaml"}]
        g.routes[("GET", f"{MR}/notes")] = lambda u, d: (200, self.notes)
        g.routes[("POST", f"{MR}/notes")] = self._post
        g.routes[("GET", f"{MR}/approvals")] = lambda u, d: (200, {"approved_by": self.approved_by})
        g.routes[("GET", f"{MR}/versions")] = [{"head_commit_sha": self.sha, "created_at": PUSHED},
                                               {"head_commit_sha": "0" * 40, "created_at": "2026-11-20T09:00:00Z"}]
        g.routes[("GET", "/user")] = {"username": BOT}
        ids = {"alice": 1, "bob": 2, "mallory": 3}
        g.routes[("GET", "/users")] = lambda u, d: (200, [{"id": i} for n, i in ids.items() if f"username={n}" in u])
        for name, i in ids.items():
            g.routes[("GET", f"{P}/members/all/{i}")] = (
                lambda u, d, name=name: (200, {"access_level": 30 if name in self.developers else 20}))
        self.runner = FakeRunner(*runs)
        self.envs = []
        self.locks = SQLiteLocks(tmp_path / "state" / "locks.db")
        self.svc = GitLabService(
            settings=Settings(repo="acme/ansible", project_id="9", ssh_user="root"),
            vcs=GitLabBackend(GitLabClient(API, "tok", transport=g), "acme/ansible", pipeline_blocks=True),
            locks=self.locks,
            workspace=Workspace(tmp_path / "work", f"file://{bare}", change_ref="refs/merge-requests/{number}/head",
                                git_user="oauth2"),
            secret=SECRET,
            runner_factory=self._runner,
            clock=lambda: NOW,
        )

    def mr_json(self):
        return {"iid": 1, "sha": self.sha, "source_branch": "feature", "target_branch": self.target,
                "author": {"username": self.author}, "state": "closed" if self.closed else "opened", "draft": False,
                "detailed_merge_status": self.merge_status, "web_url": "https://gitlab.example.com/acme/ansible/-/merge_requests/1"}

    author = "bob"
    target = "main"  # MR 1's target branch, as the API says
    closed = False  # what the API says about MR 1

    def _runner(self, checkout, marker, env):
        self.envs.append((checkout, env))
        return self.runner

    def _post(self, url, d):
        n = {"id": len(self.notes) + 1, "author": {"username": BOT}, "body": d["body"], "created_at": "t"}
        self.notes.append(n)
        return 201, n

    def deliver(self, event, payload, delivery=None, token=SECRET.decode()):
        self._n = getattr(self, "_n", 0) + 1
        body = json.dumps({**payload, "_seq": self._n}).encode()  # distinct bodies, like real deliveries
        return self.svc.handle(event, delivery or f"uuid-{self._n}", token, body)

    def mr_event(self, action="open", draft=False, fork=False, oldrev=None, changes=None, project=None, user="bob",
                 at=NOW_GL):
        attrs = {"iid": 1, "action": action, "draft": draft, "source_project_id": 7 if fork else 9,
                 "target_branch": self.target,
                 "target_project_id": 9, "last_commit": {"id": self.sha}, "author_id": 2, "updated_at": at}
        if oldrev:
            attrs["oldrev"] = oldrev
        return {"object_kind": "merge_request", "user": {"username": user},
                "project": project or {"id": 9, "path_with_namespace": "acme/ansible"},
                "object_attributes": attrs, "changes": changes or {}}

    def note_event(self, note, user="alice", noteable="MergeRequest", at=NOW_GL, updated=None, head=None):
        return {"object_kind": "note", "user": {"username": user},
                "project": {"id": 9, "path_with_namespace": "acme/ansible"},
                "object_attributes": {"note": note, "noteable_type": noteable, "action": "create",
                                      "created_at": at, "updated_at": updated or at},
                "merge_request": {"iid": 1, "state": "opened", "last_commit": {"id": head or self.sha}}}

    def drain(self):
        out = []
        while not self.svc.jobs.empty():
            out.append(self.svc.run_job(self.svc.jobs.get()))
        return out


@pytest.fixture
def world(repo, tmp_path):
    return World(repo, tmp_path / "world", runs=(clean_run(), clean_run()))


# ---- webhook decisions ---------------------------------------------------------------------------


def test_token_check():
    assert verify_token(SECRET, "glsecret")
    assert not verify_token(SECRET, "glsecreT") and not verify_token(SECRET, None) and not verify_token(SECRET, "")
    assert not verify_token(SECRET, "glsecreté")  # non-ASCII doesn't raise
    assert not verify_token(b"", "")


def test_bad_token_duplicate_and_foreign_deliveries(world):
    ev = world.mr_event()
    assert world.deliver("Merge Request Hook", ev, "u1", token="wrong") == (401, "bad token")
    assert world.deliver("Merge Request Hook", ev, "u1", token=None)[0] == 401
    assert world.deliver("Merge Request Hook", ev, "u1")[1] == "plan queued"
    assert world.deliver("Merge Request Hook", ev, "u1")[1] == "duplicate delivery"
    other = world.mr_event(project={"id": 10, "path_with_namespace": "acme/other"})
    assert world.deliver("Merge Request Hook", other)[1] == "not this service's project"
    by_id = world.mr_event(project={"id": 9, "path_with_namespace": "acme/renamed"})
    assert world.deliver("Merge Request Hook", by_id)[1] == "plan queued"  # the project id still matches
    assert world.deliver("Push Hook", {"project": {"id": 9}})[1] == "ignored event Push Hook"
    assert world.svc.jobs.qsize() == 2


def test_which_mr_events_plan(world):
    assert world.deliver("Merge Request Hook", world.mr_event("reopen"))[1] == "plan queued"
    assert world.deliver("Merge Request Hook", world.mr_event("update", oldrev="a" * 40))[1] == "plan queued"
    ready = {"draft": {"previous": True, "current": False}}
    assert world.deliver("Merge Request Hook", world.mr_event("update", changes=ready))[1] == "plan queued"
    retitled = {"title": {"previous": "a", "current": "b"}}
    assert world.deliver("Merge Request Hook", world.mr_event("update", changes=retitled))[1] == \
        "ignored merge_request.update"
    assert world.deliver("Merge Request Hook", world.mr_event("approved"))[1] == "ignored merge_request.approved"
    job = world.svc.jobs.get()
    assert (job.action, job.pr, job.sha, job.user) == ("plan", 1, world.sha, "bob")


@pytest.mark.parametrize("kw,why", [({"draft": True}, "draft"), ({"fork": True}, "fork")])
def test_drafts_and_forks_are_not_planned(world, kw, why):
    assert why in world.deliver("Merge Request Hook", world.mr_event(**kw))[1]
    assert world.svc.jobs.empty()


def test_plan_rule_waits_for_a_developer(world):
    world.author = "mallory"
    assert world.deliver("Merge Request Hook", world.mr_event())[1] == "waiting for /ferry plan"
    assert world.deliver("Merge Request Hook", world.mr_event("update", oldrev="a" * 40))[1] == "waiting for /ferry plan"
    assert len(world.notes) == 1  # said once per head
    assert "Developer access" in world.notes[0]["body"] and "MR's playbooks" in world.notes[0]["body"]
    assert world.svc.jobs.empty()
    assert world.deliver("Note Hook", world.note_event("/ferry plan", user="mallory"))[1] == \
        "plan refused: no Developer access"
    assert "needs Developer access" in world.notes[-1]["body"]
    assert world.deliver("Note Hook", world.note_event("/ferry plan", user="alice"))[1] == "plan queued"
    job = world.svc.jobs.get()
    assert (job.action, job.pr, job.sha, job.user) == ("plan", 1, world.sha, "alice")


def test_push_by_a_non_developer_waits(world):
    # bob (a Developer) authored the MR; mallory pushed into it: her code, so the plan rule is hers.
    assert world.deliver("Merge Request Hook", world.mr_event("update", oldrev="a" * 40, user="mallory"))[1] == \
        "waiting for /ferry plan"
    assert world.svc.jobs.empty() and "pushes from users" in world.notes[-1]["body"]
    assert world.deliver("Merge Request Hook", world.mr_event("update", oldrev="a" * 40, user="alice"))[1] == \
        "plan queued"


def test_apply_from_a_non_developer_is_refused_before_any_job(world):
    calls = []
    world.svc.runner_factory = lambda *a: calls.append(a)
    assert world.deliver("Note Hook", world.note_event("/ferry apply", user="mallory"))[1] == \
        "apply refused: no Developer access"
    assert world.svc.jobs.empty() and not calls  # no checkout, no credentials, no runner
    assert "needs Developer access" in world.notes[-1]["body"]


def test_queue_is_bounded(world):
    world.svc.settings.max_queued_jobs = 2
    world.svc.__post_init__()
    assert world.deliver("Note Hook", world.note_event("/ferry plan"))[0] == 202
    assert world.deliver("Note Hook", world.note_event("/ferry plan"))[0] == 202
    assert world.deliver("Note Hook", world.note_event("/ferry plan")) == (503, "queue full, try again later")
    assert world.deliver("Merge Request Hook", world.mr_event("close"))[0] == 503
    assert world.svc.jobs.qsize() == 2


def test_note_parsing(world):
    assert world.deliver("Note Hook", world.note_event("/plan"))[1] == "not a ferry command"
    assert world.deliver("Note Hook", world.note_event("lgtm"))[1] == "not a ferry command"
    assert world.deliver("Note Hook", world.note_event("/ferry apply", user=BOT))[1] == "own comment"
    assert world.deliver("Note Hook", world.note_event("/ferry apply", noteable="Issue"))[1] == "not a new MR comment"
    edited = world.note_event("/ferry apply")
    edited["object_attributes"]["action"] = "update"
    assert world.deliver("Note Hook", edited)[1] == "not a new MR comment"
    assert world.deliver("Note Hook", world.note_event("/ferry apply homelab"))[1] == "apply queued"
    assert world.svc.jobs.get().inventory == "homelab"


# ---- jobs end to end -----------------------------------------------------------------------------


def apply_reasons(world, delivery):
    world.deliver("Note Hook", world.note_event("/ferry apply"), delivery=delivery)
    [result] = world.drain()
    return result, result.get("inventories", {}).get("homelab", {}).get("reasons", [])


def test_plan_then_apply_then_close(world):
    world.deliver("Merge Request Hook", world.mr_event())
    [plan] = world.drain()
    assert plan["ok"], plan
    assert "ferry:plan" in world.notes[-1]["body"] and "comment `/ferry apply`" in world.notes[-1]["body"]
    assert world.locks.get("homelab").holder == 1
    checkout, env = world.envs[-1]
    assert env["ANSIBLE_REMOTE_USER"] == "root" and not checkout.exists()

    # The MR set min_approvals: 0 in its own ferry.yaml; the default branch's still needs one.
    result, reasons = apply_reasons(world, "a1")
    assert not result["ok"] and any("approval" in r for r in reasons)

    # An approval given before the current head was pushed is for an older commit.
    world.approved_by = [{"user": {"username": "alice"}, "approved_at": "2026-11-20T11:59:59Z"}]
    result, reasons = apply_reasons(world, "a2")
    assert not result["ok"] and any("has 0" in r for r in reasons)

    # GitLab hasn't worked out mergeability yet, or the MR's pipeline is still running: refuse.
    world.approved_by = [{"user": {"username": "alice"}, "approved_at": "2026-11-20T12:05:00Z"}]
    for status, n in (("checking", "a3"), ("ci_still_running", "a4")):
        world.merge_status = status
        result, reasons = apply_reasons(world, n)
        assert not result["ok"] and f"MR is not mergeable ({status})" in reasons
    world.merge_status = "mergeable"

    result, reasons = apply_reasons(world, "a5")
    assert result["ok"], result
    assert world.runner.requests[-1].check is False
    assert "ferry apply" in world.notes[-1]["body"]

    world.closed = True
    world.deliver("Merge Request Hook", world.mr_event("merge"))
    [unlock] = world.drain()
    assert unlock["released"] == ["homelab"] and world.locks.get("homelab") is None


def test_close_releases_locks(world):
    world.deliver("Merge Request Hook", world.mr_event())
    world.drain()
    assert world.locks.get("homelab").holder == 1
    world.closed = True
    assert world.deliver("Merge Request Hook", world.mr_event("close"))[1] == "unlock queued"
    [unlock] = world.drain()
    assert unlock["released"] == ["homelab"]


def test_mr_that_moved_on_is_skipped_quietly(world):
    world.svc.jobs.put(Job("plan", 1, "0" * 40, "bob"))
    [result] = world.drain()
    assert "moved on" in result["error"] and not world.notes and not world.runner.requests


def test_gitlab_git_auth_header():
    env = _git_env("glpat-x", "oauth2")
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Basic " + base64.b64encode(b"oauth2:glpat-x").decode()


# ---- HTTP ----------------------------------------------------------------------------------------


def test_http_endpoints(world):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(world.svc))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    body = json.dumps(world.mr_event()).encode()

    def post(token, key):
        return urllib.request.Request(f"{base}/webhook", data=body, method="POST", headers={
            "X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": token, "Idempotency-Key": key,
            "X-Gitlab-Event-UUID": "same-for-both", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(post("glsecret", "k1")) as resp:
            assert resp.status == 202 and b"plan queued" in resp.read()
        with urllib.request.urlopen(post("glsecret", "k1")) as resp:  # GitLab's retry: same key
            assert b"duplicate delivery" in resp.read()
        with urllib.request.urlopen(f"{base}/healthz") as resp:
            assert json.loads(resp.read())["queued"] == 1
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(post("nope", "k2"))
        assert e.value.code == 401
    finally:
        httpd.shutdown()


# ---- ferry serve wiring --------------------------------------------------------------------------


def test_build_gitlab_service_from_env(tmp_path, monkeypatch):
    from ferry import cli

    fake = FakeGitLab()
    fake.routes[("GET", "/projects/acme%2Fansible")] = {
        "id": 9, "path_with_namespace": "acme/ansible", "default_branch": "trunk",
        "http_url_to_repo": "https://gitlab.example.com/acme/ansible.git"}
    fake.routes[("GET", "/user")] = {"username": BOT}
    monkeypatch.setattr(cli, "GitLabClient", lambda api, token: GitLabClient(api, token, transport=fake))
    (tmp_path / "token").write_text("tok\n")
    (tmp_path / "secret").write_text("glsecret\n")
    for k in [k for k in __import__("os").environ if k.startswith(("FERRY_", "GITHUB_"))]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("FERRY_GITLAB_URL", "https://gitlab.example/")
    with pytest.raises(SystemExit, match="FERRY_GITLAB_PROJECT.*FERRY_LOCK_DB.*SECRET.*TOKEN"):
        cli.build_service()
    monkeypatch.setenv("FERRY_GITLAB_PROJECT", "acme/ansible")
    monkeypatch.setenv("FERRY_GITLAB_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("FERRY_WEBHOOK_SECRET_FILE", str(tmp_path / "secret"))
    monkeypatch.setenv("FERRY_LOCK_DB", str(tmp_path / "locks.db"))
    monkeypatch.setenv("FERRY_WORKDIR", str(tmp_path / "work"))
    svc = cli.build_service()  # FERRY_VCS unset: FERRY_GITLAB_URL picks GitLab
    assert isinstance(svc, GitLabService) and svc.secret == SECRET
    assert (svc.settings.repo, svc.settings.project_id, svc.settings.default_branch) == ("acme/ansible", "9", "trunk")
    assert svc.workspace.remote == "https://gitlab.example.com/acme/ansible.git"
    assert svc.workspace.git_user == "oauth2" and svc.workspace._tok() == "tok"
    assert svc.vcs.pipeline_blocks and fake.calls[0][1] == f"{API}/projects/acme%2Fansible"


def test_serve_startup_retries_while_the_code_host_is_unreachable(monkeypatch):
    import urllib.error

    from ferry import cli
    from ferry.vcs.gitlab import GitLabError

    calls, waits = [], []

    def flaky(vcs=None):
        calls.append(vcs)
        if len(calls) < 3:
            raise urllib.error.URLError("Temporary failure in name resolution")
        return "svc"

    monkeypatch.setattr(cli, "build_service", flaky)
    assert cli.build_service_with_retry(attempts=5, sleep=waits.append) == "svc"
    assert len(calls) == 3 and waits == [1.0, 2.0]

    def unauthorized(vcs=None):  # a wrong token is not transient: fail at once
        calls.append(vcs)
        raise GitLabError(401, "GET", "/user", "unauthorized")

    calls.clear()
    monkeypatch.setattr(cli, "build_service", unauthorized)
    with pytest.raises(GitLabError):
        cli.build_service_with_retry(attempts=5, sleep=waits.append)
    assert len(calls) == 1


# ---- replays (#11) -------------------------------------------------------------------------------


def test_a_replayed_body_is_a_duplicate_under_any_uuid(world, tmp_path):
    from ferry.state import SQLiteState
    world.svc.state = SQLiteState(tmp_path / "state.db")
    body = json.dumps(world.note_event("/ferry plan")).encode()
    assert world.svc.handle("Note Hook", "u1", SECRET.decode(), body)[1] == "plan queued"
    assert world.svc.handle("Note Hook", "u2", SECRET.decode(), body)[1] == "duplicate delivery"
    world.svc.state = SQLiteState(tmp_path / "state.db")  # after a restart too
    assert world.svc.handle("Note Hook", "u3", SECRET.decode(), body)[1] == "duplicate delivery"
    assert world.svc.jobs.qsize() == 1


@pytest.mark.parametrize("kw,why", [
    ({"at": "2027-01-15 07:49:00 UTC"}, "stale event"),
    ({"updated": "2027-01-15T07:40:00.000Z"}, "stale event"),
    ({"at": "2027-01-15 08:10:00 UTC"}, "in the future"),
    ({"at": None}, "no readable timestamp"),
])
def test_old_notes_are_refused(world, kw, why):
    assert why in world.deliver("Note Hook", world.note_event("/ferry apply", **kw))[1]
    assert world.svc.jobs.empty()


def test_a_replayed_mr_close_releases_nothing(world):
    assert "stale" in world.deliver("Merge Request Hook", world.mr_event("close", at="2027-01-14 08:00:00 UTC"))[1]
    assert world.svc.jobs.empty()
    assert world.deliver("Merge Request Hook", world.mr_event("close"))[1] == "unlock queued"


# ---- comments pin the head (#12) and refused applies cost no credentials (#13) -------------------


def test_a_note_runs_the_head_it_was_written_against(world):
    # The hook says the MR head was another commit: the author pushed after the note.
    assert world.deliver("Note Hook", world.note_event("/ferry plan", head="b" * 40))[1] == \
        "plan refused: the head isn't the one commented on"
    assert "moved from `bbbbbbbbbbbb`" in world.notes[-1]["body"] and world.svc.jobs.empty()
    assert world.deliver("Note Hook", world.note_event("/ferry plan"))[1] == "plan queued"
    job = world.svc.jobs.get()
    assert (job.sha, job.pinned) == (world.sha, True)


def test_a_head_that_arrived_after_the_note_is_refused(world):
    world.gl.routes[("GET", f"{MR}/versions")] = [{"head_commit_sha": world.sha, "created_at": "2027-01-15T08:00:30.000Z"}]
    assert world.deliver("Note Hook", world.note_event("/ferry plan"))[1].startswith("plan refused")
    assert "arrived after the comment" in world.notes[-1]["body"]
    assert f"--sha {world.sha[:12]}" in world.notes[-1]["body"]
    # Naming the commit is explicit: it runs that commit, if it's still the head.
    assert world.deliver("Note Hook", world.note_event(f"/ferry plan --sha {world.sha[:12]}"))[1] == "plan queued"
    assert world.deliver("Note Hook", world.note_event("/ferry plan --sha 1234567"))[1].startswith("plan refused")
    assert "names `1234567`" in world.notes[-1]["body"]


def test_unreadable_diff_versions_refuse(world):
    world.gl.routes[("GET", f"{MR}/versions")] = lambda u, d: (500, {"message": "boom"})
    assert world.deliver("Note Hook", world.note_event("/ferry apply"))[1].startswith("apply refused")
    assert "hasn't seen when" in world.notes[-1]["body"]


def test_a_pinned_job_whose_mr_moved_is_reported_and_runs_nothing(world):
    assert world.deliver("Note Hook", world.note_event("/ferry plan"))[1] == "plan queued"
    world.sha = "c" * 40  # a push lands while the job waits in the queue
    [result] = world.drain()
    assert "moved from" in result["error"] and not world.runner.requests
    assert "ferry plan: not run" in world.notes[-1]["body"]


def test_a_refused_apply_mints_no_credentials(world, monkeypatch):
    import contextlib
    from ferry import service as service_mod
    minted = []

    @contextlib.contextmanager
    def creds(bao, ssh_user):
        minted.append(1)
        yield {"ANSIBLE_REMOTE_USER": "root"}
    monkeypatch.setattr(service_mod, "job_credentials", creds)
    world.deliver("Merge Request Hook", world.mr_event())
    world.drain()
    assert len(minted) == 1  # the plan
    result, reasons = apply_reasons(world, "x1")
    assert not result["ok"] and any("approval" in r for r in reasons)
    assert len(minted) == 1 and "ferry apply" in world.notes[-1]["body"]  # refused on the MR, no credentials
    world.approved_by = [{"user": {"username": "alice"}, "approved_at": "2026-11-20T12:05:00Z"}]
    result, _ = apply_reasons(world, "x2")
    assert result["ok"] and len(minted) == 2


# ---- unlock without git (#14) --------------------------------------------------------------------


def test_close_releases_locks_without_reaching_git(world):
    world.deliver("Merge Request Hook", world.mr_event())
    world.drain()
    world.locks.try_acquire(new_lock("retired", 1, world.sha, "pip"))  # an inventory no longer in ferry.yaml
    world.svc.workspace.remote = "file:///nonexistent/gone.git"  # git is unreachable
    world.closed = True
    world.deliver("Merge Request Hook", world.mr_event("close"))
    [unlock] = world.drain()
    assert unlock["ok"] and unlock["released"] == ["homelab", "retired"]
    assert world.locks.get("homelab") is None


def test_a_reopened_mr_keeps_its_locks(world):
    world.deliver("Merge Request Hook", world.mr_event())
    world.drain()
    world.deliver("Merge Request Hook", world.mr_event("close"))  # but it's open again by the time the job runs
    [unlock] = world.drain()
    assert unlock["released"] == [] and "open again" in unlock["skipped"]
    assert world.locks.get("homelab").holder == 1


# ---- metrics and job history (#16) ---------------------------------------------------------------


def test_metrics_count_gitlab_rejects_and_jobs(world):
    import socket
    from ferry.service import BoundedHTTPServer
    httpd = BoundedHTTPServer(("127.0.0.1", 0), make_handler(world.svc))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(token, payload, key):
        body = json.dumps(payload).encode()
        req = urllib.request.Request(f"{base}/webhook", data=body, method="POST", headers={
            "X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": token, "Idempotency-Key": key})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code
    try:
        assert post("nope", world.note_event("/ferry plan"), "k1") == 401
        ev = world.note_event("/ferry plan")
        assert "plan queued" in post(SECRET.decode(), ev, "k2")
        assert "duplicate" in post(SECRET.decode(), ev, "k3")
        assert "stale" in post(SECRET.decode(), world.note_event("/ferry plan", at="2027-01-15 07:00:00 UTC"), "k4")
        assert "no Developer access" in post(SECRET.decode(), world.note_event("/ferry plan", user="mallory"), "k5")
        world.drain()
        with urllib.request.urlopen(f"{base}/metrics") as resp:
            text = resp.read().decode()
        for reason in ("bad_auth", "duplicate", "stale", "no_access"):
            assert f'ferry_webhook_rejected_total{{reason="{reason}"}} 1' in text, reason
        assert 'ferry_jobs_total{action="plan",result="ok"} 1' in text
        for leak in ("alice", "mallory", world.sha[:7], SECRET.decode(), "acme"):
            assert leak not in text, leak
        with urllib.request.urlopen(f"{base}/jobs") as resp:
            [job] = json.loads(resp.read())["jobs"]
        assert (job["action"], job["pr"], job["user"], job["result"]) == ("plan", 1, "alice", "ok")
        assert not {"error", "reasons"} & set(job)
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---- #21: closed MRs get no plan, no credentials, no lock -----------------------------------------

@pytest.mark.parametrize("command", ["/ferry plan", "/ferry apply"])
def test_commands_on_a_closed_mr_are_refused_at_the_webhook(world, monkeypatch, command):
    from test_service import count_credentials
    minted = count_credentials(monkeypatch)
    world.closed = True
    status, text = world.deliver("Note Hook", world.note_event(command))
    assert status == 202 and "closed" in text
    assert world.svc.jobs.empty() and not minted
    assert "is closed" in world.notes[-1]["body"]
    assert world.locks.get("homelab") is None


def test_a_plan_queued_before_the_mr_closed_runs_nothing(world, monkeypatch):
    from test_service import count_credentials
    minted = count_credentials(monkeypatch)
    assert world.deliver("Note Hook", world.note_event("/ferry plan"))[0] == 202
    world.closed = True
    [result] = world.drain()
    assert result["result"] == "skipped" and not minted and not world.runner.requests
    assert world.locks.get("homelab") is None


# ---- #22: target branch -----------------------------------------------------------------------------

def test_an_mr_into_another_branch_is_not_planned(world, monkeypatch):
    from test_service import count_credentials
    minted = count_credentials(monkeypatch)
    world.target = "feature-x"
    world.deliver("Merge Request Hook", world.mr_event())
    world.drain()
    assert not minted and not world.runner.requests and world.locks.get("homelab") is None


@pytest.mark.parametrize("command", ["/ferry plan", "/ferry apply"])
def test_commands_on_an_mr_into_another_branch_are_refused(world, monkeypatch, command):
    from test_service import count_credentials
    minted = count_credentials(monkeypatch)
    world.target = "feature-x"
    world.deliver("Note Hook", world.note_event(command))
    world.drain()
    assert not minted and not world.runner.requests
    assert "`feature-x`" in world.notes[-1]["body"]


def test_an_mr_retargeted_after_its_plan_is_not_applied(world, monkeypatch):
    world.deliver("Merge Request Hook", world.mr_event())
    world.drain()
    minted = count_credentials_gl(monkeypatch)
    world.target = "feature-x"
    world.approved_by = [{"user": {"username": "alice"}, "approved_at": "2026-11-20T12:05:00Z"}]
    world.deliver("Note Hook", world.note_event("/ferry apply"))
    [result] = world.drain()
    assert not result["ok"] and not minted and len(world.runner.requests) == 1


def count_credentials_gl(monkeypatch):
    from test_service import count_credentials
    return count_credentials(monkeypatch)
