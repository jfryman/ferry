import json
from urllib.parse import parse_qs, urlparse

import pytest

from ferry.locks import GitLabTagLocks, new_lock
from ferry.vcs.gitlab import GitLabBackend, GitLabClient
from ferry.webhook import parse_note_event, trigger_pipeline, verify_token

API = "https://gitlab.example/api/v4"


class FakeGitLab:
    """Routes (method, path-without-query) to handlers; records calls. Mimics GitLab's tag semantics."""

    def __init__(self):
        self.calls = []
        self.tags = {}
        self.routes = {}

    def __call__(self, method, url, headers, body):
        assert headers["PRIVATE-TOKEN"] == "tok"
        path = url[len(API):].split("?", 1)[0]
        data = json.loads(body) if body else None
        self.calls.append((method, url, data))
        if "/repository/tags" in path:
            return self._tags(method, path, data)
        handler = self.routes.get((method, path))
        if handler is None:
            return 404, b'{"message":"404 Not Found"}'
        status, obj = handler(url, data) if callable(handler) else (200, handler)
        return status, json.dumps(obj).encode()

    def _tags(self, method, path, data):
        name = path.rsplit("/", 1)[-1].replace("%2F", "/")
        if method == "GET" and name == "tags":
            return 200, json.dumps(list(self.tags.values())).encode()
        if method == "POST":
            if data["tag_name"] in self.tags:
                return 400, b'{"message":"Tag ferry-lock-x already exists"}'
            self.tags[data["tag_name"]] = {"name": data["tag_name"], "message": data["message"], "target": data["ref"]}
            return 201, json.dumps(self.tags[data["tag_name"]]).encode()
        if name not in self.tags:
            return 404, b'{"message":"404 Tag Not Found"}'
        if method == "DELETE":
            del self.tags[name]
            return 204, b""
        return 200, json.dumps(self.tags[name]).encode()


@pytest.fixture
def gl():
    fake = FakeGitLab()
    client = GitLabClient(API, "tok", transport=fake)
    return fake, client, GitLabBackend(client, "infra/ansible")


def test_project_path_is_url_encoded(gl):
    fake, client, be = gl
    fake.routes[("GET", "/projects/infra%2Fansible/merge_requests/7")] = {
        "iid": 7, "sha": "abc", "source_branch": "f", "target_branch": "main", "author": {"username": "pip"},
        "state": "opened", "draft": False, "has_conflicts": False, "blocking_discussions_resolved": True,
        "detailed_merge_status": "need_rebase", "web_url": "u"}
    cr = be.get_change_request(7)
    assert cr.head_sha == "abc" and cr.author == "pip" and cr.blocked_reasons == ["need_rebase"]


def test_ci_still_running_does_not_block(gl):
    fake, client, be = gl
    fake.routes[("GET", "/projects/infra%2Fansible/merge_requests/7")] = {
        "iid": 7, "sha": "abc", "source_branch": "f", "target_branch": "main", "author": {"username": "pip"},
        "state": "opened", "detailed_merge_status": "ci_still_running"}
    assert be.get_change_request(7).blocked_reasons == []


def test_changed_files_paginates_and_includes_renames(gl):
    fake, client, be = gl

    def diffs(url, data):
        page = int(parse_qs(urlparse(url).query)["page"][0])
        if page == 1:
            return 200, [{"old_path": f"f{i}", "new_path": f"f{i}"} for i in range(100)]
        return 200, [{"old_path": "roles/a/x.yml", "new_path": "roles/b/x.yml"}]

    fake.routes[("GET", "/projects/infra%2Fansible/merge_requests/7/diffs")] = diffs
    files = be.changed_files(7)
    assert len(files) == 102 and "roles/a/x.yml" in files and "roles/b/x.yml" in files


def test_notes_skip_system_notes(gl):
    fake, client, be = gl
    fake.routes[("GET", "/projects/infra%2Fansible/merge_requests/7/notes")] = [
        {"id": 1, "author": {"username": "james"}, "body": "hi", "system": False},
        {"id": 2, "author": {"username": "james"}, "body": "approved this", "system": True}]
    assert [n.id for n in be.list_notes(7)] == [1]


def test_can_apply(gl):
    fake, client, be = gl
    fake.routes[("GET", "/users")] = lambda url, d: (200, [{"id": 5}] if "username=james" in url else [])
    fake.routes[("GET", "/projects/infra%2Fansible/members/all/5")] = {"access_level": 40}
    assert be.can_apply("james") and not be.can_apply("nobody")


MR7 = "/projects/infra%2Fansible/merge_requests/7"


def approvals(fake, approved_by, versions=(), reset_on_push=None):
    fake.routes[("GET", MR7)] = {"iid": 7, "sha": "head2"}
    fake.routes[("GET", f"{MR7}/approvals")] = {"approved_by": approved_by}
    fake.routes[("GET", f"{MR7}/versions")] = list(versions)
    if reset_on_push is not None:
        fake.routes[("GET", "/projects/infra%2Fansible/approvals")] = {"reset_approvals_on_push": reset_on_push}


VERSIONS = [{"head_commit_sha": "head2", "created_at": "2026-11-20T12:00:00.000Z"},
            {"head_commit_sha": "head1", "created_at": "2026-11-20T10:00:00.000Z"}]


def test_approvals_count_only_after_the_head_arrived(gl):
    fake, client, be = gl
    approvals(fake, [{"user": {"username": "alice"}, "approved_at": "2026-11-20T11:00:00Z"},  # head1
                     {"user": {"username": "lee"}, "approved_at": "2026-11-20T12:00:00.000Z"},  # same instant
                     {"user": {"username": "kim"}, "approved_at": "2026-11-20T13:30:00.000+01:00"}],  # 12:30Z
              VERSIONS)
    assert be.approvers(7) == ["kim", "lee"]


def test_head_pushed_back_counts_from_its_latest_arrival(gl):
    fake, client, be = gl
    versions = [{"head_commit_sha": "head2", "created_at": "2026-11-20T14:00:00Z"}, *VERSIONS]
    approvals(fake, [{"user": {"username": "alice"}, "approved_at": "2026-11-20T13:00:00Z"}], versions)
    assert be.approvers(7) == []


def test_no_version_for_the_head_counts_nothing(gl):
    fake, client, be = gl
    approvals(fake, [{"user": {"username": "alice"}, "approved_at": "2026-11-20T13:00:00Z"}], VERSIONS[1:])
    assert be.approvers(7) == []


@pytest.mark.parametrize("reset, expected", [(True, ["alice"]), (False, []), (None, [])])
def test_approvals_without_timestamps_need_reset_on_push(gl, reset, expected):
    fake, client, be = gl
    approvals(fake, [{"user": {"username": "alice"}}], VERSIONS, reset_on_push=reset)  # None: endpoint 404s
    assert be.approvers(7) == expected


@pytest.mark.parametrize("status, ci, service", [
    ("mergeable", [], []), ("checking", ["checking"], ["checking"]), ("unchecked", ["unchecked"], ["unchecked"]),
    ("ci_must_pass", [], ["ci_must_pass"]), ("ci_still_running", [], ["ci_still_running"])])
def test_merge_status_for_ci_driver_and_service(gl, status, ci, service):
    fake, client, be = gl
    fake.routes[("GET", MR7)] = {
        "iid": 7, "sha": "abc", "source_branch": "f", "target_branch": "main", "author": {"username": "bob"},
        "state": "opened", "detailed_merge_status": status}
    assert be.get_change_request(7).blocked_reasons == ci
    assert GitLabBackend(client, "infra/ansible", pipeline_blocks=True).get_change_request(7).blocked_reasons == service


def test_token_may_be_a_callable(gl):
    fake, _, _ = gl
    fake.routes[("GET", "/user")] = {"username": "bot"}
    assert GitLabBackend(GitLabClient(API, lambda: "tok", transport=fake), 1).bot_username() == "bot"


def test_tag_locks_are_exclusive(gl):
    fake, client, be = gl
    locks = GitLabTagLocks(client, "infra/ansible")
    assert locks.try_acquire(new_lock("homelab", 1, "sha1", "pip")) is None
    held = locks.try_acquire(new_lock("homelab", 2, "sha2", "pip"))
    assert held is not None and held.holder == 1
    assert locks.release("homelab", 2) is False  # not the holder
    assert locks.release("homelab", 1) is True
    assert locks.get("homelab") is None


def test_tag_locks_release_all(gl):
    fake, client, be = gl
    locks = GitLabTagLocks(client, "infra/ansible")
    for inv, holder in (("homelab", 1), ("work", 1), ("lab", 2)):
        locks.try_acquire(new_lock(inv, holder, "sha", "pip"))
    fake.tags["v1.0"] = {"name": "v1.0", "message": "", "target": "x"}
    assert locks.release_all(1) == ["homelab", "work"]
    assert sorted(fake.tags) == ["ferry-lock-lab", "v1.0"]


def test_unreadable_lock_is_held_not_free(gl):
    fake, client, be = gl
    fake.tags["ferry-lock-homelab"] = {"name": "ferry-lock-homelab", "message": "garbage", "target": "x"}
    locks = GitLabTagLocks(client, "infra/ansible")
    assert locks.get("homelab").holder == 0


def note_event(note="/ferry apply", user="james", state="opened", fork=False):
    return {"object_kind": "note", "user": {"username": user},
            "object_attributes": {"noteable_type": "MergeRequest", "note": note},
            "merge_request": {"iid": 4, "state": state, "source_branch": "feat", "source_project_id": 2 if fork else 1,
                              "target_project_id": 1}}


@pytest.mark.parametrize("note, action, inv", [
    ("/ferry apply", "apply", None), ("/apply", "apply", None), ("/plan homelab", "plan", "homelab"),
    ("/ferry plan -i work", "plan", "work"), ("  /apply  \nthanks", "apply", None)])
def test_webhook_parses_commands(note, action, inv):
    cmd = parse_note_event(note_event(note), "ferry-bot")
    assert (cmd.action, cmd.inventory, cmd.mr_iid, cmd.user) == (action, inv, 4, "james")


@pytest.mark.parametrize("event", [
    note_event("please /apply"), note_event("/apply", user="ferry-bot"), note_event(state="merged"),
    note_event(fork=True), {"object_kind": "push"}])
def test_webhook_ignores(event):
    assert parse_note_event(event, "ferry-bot") is None


def test_webhook_token_and_trigger(gl):
    fake, client, be = gl
    assert verify_token("s3cret", "s3cret") and not verify_token("nope", "s3cret") and not verify_token(None, "s")
    fake.routes[("POST", "/projects/infra%2Fansible/trigger/pipeline")] = lambda url, d: (201, {"id": 99})
    pid = trigger_pipeline(client, "infra/ansible", "trig", parse_note_event(note_event(), "ferry-bot"))
    assert pid == 99
    sent = fake.calls[-1][2]
    assert sent["ref"] == "feat" and sent["variables"] == {"FERRY_ACTION": "apply", "FERRY_MR_IID": "4", "FERRY_USER": "james"}


@pytest.mark.parametrize("status, taken", [(500, False), (429, False), (401, False), (404, True)])
def test_lock_holder_lookup_errors_through_the_real_client(repo, tmp_path, status, taken):
    """#21 on GitLab: only a definite 404 for the holder MR gives its lock away."""
    from ferry import config
    from ferry.engine import Engine
    from ferry.locks import SQLiteLocks
    from ferry.sanitize import Redactor
    from fakes import FakeRunner, ev_stats

    fake = FakeGitLab()
    be = GitLabBackend(GitLabClient(API, "tok", transport=fake), "infra/ansible")
    p = "/projects/infra%2Fansible/merge_requests"
    fake.routes[("GET", f"{p}/1")] = {"iid": 1, "sha": "a" * 40, "source_branch": "f", "target_branch": "main",
                                      "author": {"username": "bob"}, "state": "opened",
                                      "detailed_merge_status": "mergeable"}
    fake.routes[("GET", f"{p}/2")] = lambda url, d: (status, {"message": "x"})
    fake.routes[("GET", f"{p}/1/diffs")] = [{"old_path": "inventories/homelab/host_vars/dns-1.yml",
                                             "new_path": "inventories/homelab/host_vars/dns-1.yml"}]
    fake.routes[("POST", f"{p}/1/notes")] = lambda url, d: (201, {"id": 1, "author": {"username": "b"},
                                                                  "body": d["body"]})
    cfg = config.load(repo / "ferry.yaml")
    locks = SQLiteLocks(tmp_path / "locks.db")
    locks.try_acquire(new_lock("homelab", 2, "b" * 40, "james"))
    runner = FakeRunner(([ev_stats()], 0))
    out = Engine(cfg, repo, runner, be, locks, Redactor([], [], [], environ={}), lambda: 1.0).plan(1, "a" * 40, "bob")
    assert out.ok is taken
    assert locks.get("homelab").holder == (1 if taken else 2)


def test_behind_by_uses_diverged_commits_count(gl):
    from ferry.vcs.base import ChangeRequest
    from ferry.vcs.gitlab import GitLabError
    fake, client, be = gl
    mr = {"iid": 7, "sha": "a" * 40, "source_branch": "f", "target_branch": "main", "author": {"username": "bob"},
          "state": "opened", "diverged_commits_count": 2}
    seen = []

    def route(url, d):
        seen.append(url)
        return 200, mr
    fake.routes[("GET", "/projects/infra%2Fansible/merge_requests/7")] = route
    cr = ChangeRequest(7, "a" * 40, "f", "main", "bob", "opened")
    assert be.behind_by(cr) == 2 and "include_diverged_commits_count=true" in seen[-1]
    mr["diverged_commits_count"] = 0
    assert be.behind_by(cr) == 0
    del mr["diverged_commits_count"]
    with pytest.raises(ValueError):
        be.behind_by(cr)
    mr["diverged_commits_count"] = 1
    mr["sha"] = "b" * 40  # the MR moved while ferry looked: the count isn't about this head
    with pytest.raises(ValueError):
        be.behind_by(cr)
    fake.routes[("GET", "/projects/infra%2Fansible/merge_requests/7")] = lambda u, d: (502, {})
    with pytest.raises(GitLabError):
        be.behind_by(cr)
