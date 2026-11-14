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


def test_approvers_and_can_apply(gl):
    fake, client, be = gl
    fake.routes[("GET", "/projects/infra%2Fansible/merge_requests/7/approvals")] = {
        "approved_by": [{"user": {"username": "james"}}]}
    fake.routes[("GET", "/users")] = lambda url, d: (200, [{"id": 5}] if "username=james" in url else [])
    fake.routes[("GET", "/projects/infra%2Fansible/members/all/5")] = {"access_level": 40}
    assert be.approvers(7) == ["james"]
    assert be.can_apply("james") and not be.can_apply("nobody")


def test_tag_locks_are_exclusive(gl):
    fake, client, be = gl
    locks = GitLabTagLocks(client, "infra/ansible")
    assert locks.try_acquire(new_lock("homelab", 1, "sha1", "pip")) is None
    held = locks.try_acquire(new_lock("homelab", 2, "sha2", "pip"))
    assert held is not None and held.holder == 1
    assert locks.release("homelab", 2) is False  # not the holder
    assert locks.release("homelab", 1) is True
    assert locks.get("homelab") is None


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
