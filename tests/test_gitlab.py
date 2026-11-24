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


# ---- GitLab's own approval rules (Premium: approvals_required / approvals_left; Free/CE: neither) ----


def rules(fake, approvals_left, approvals_required, approved_by, approved=None, state=None):
    """EE-shaped approvals for MR 7 at head2 (arrived 12:00); state: approval_state rules, None = 404."""
    data = {"approvals_required": approvals_required, "approvals_left": approvals_left, "approved_by": approved_by}
    if approved is not None:
        data["approved"] = approved
    fake.routes[("GET", MR7)] = {"iid": 7, "sha": "head2", "detailed_merge_status": "mergeable"}
    fake.routes[("GET", f"{MR7}/approvals")] = data
    fake.routes[("GET", f"{MR7}/versions")] = VERSIONS
    if state is not None:
        fake.routes[("GET", f"{MR7}/approval_state")] = {"rules": state}


OLD = {"user": {"username": "alice"}, "approved_at": "2026-11-20T11:00:00Z"}  # approved head1
NEW = {"user": {"username": "kim"}, "approved_at": "2026-11-20T12:30:00Z"}  # approved head2


def rules_backend(client):
    return GitLabBackend(client, "infra/ansible", approval_rules=True)


def test_approval_rules_off_asks_nothing(gl):
    fake, client, be = gl
    rules(fake, 2, 2, [])
    assert be.approval_rule_blockers(7) == [] and fake.calls == []


@pytest.mark.parametrize("approvals", [{"approved_by": [], "approved": False},  # CE 18.x, nobody approved
                                       {"approved_by": [NEW], "approved": True, "user_can_approve": False},
                                       None])  # None: the endpoint 404s
def test_free_and_ce_have_no_rules_to_enforce(gl, approvals):
    fake, client, _ = gl
    if approvals is not None:
        fake.routes[("GET", f"{MR7}/approvals")] = approvals
    assert rules_backend(client).approval_rule_blockers(7) == []


@pytest.mark.parametrize("left, approved", [(1, None), (2, False), (0, False)])
def test_rules_not_met_block(gl, left, approved):
    fake, client, _ = gl
    rules(fake, left, 2, [NEW], approved=approved)
    [reason] = rules_backend(client).approval_rule_blockers(7)
    assert reason == f"GitLab's approval rules aren't met ({left} more approval(s) needed)"


def test_rules_met_by_head_approvals_pass(gl):
    fake, client, _ = gl
    rules(fake, 0, 1, [NEW], approved=True)
    assert rules_backend(client).approval_rule_blockers(7) == []
    assert not any("approval_state" in c[1] for c in fake.calls)  # all approvals are for the head


def test_no_required_approvals_pass_even_with_stale_ones(gl):
    fake, client, _ = gl
    rules(fake, 0, 0, [OLD], approved=True)
    assert rules_backend(client).approval_rule_blockers(7) == []


def test_rule_met_only_by_a_stale_approval_blocks(gl):
    fake, client, _ = gl
    state = [{"name": "infra-owners", "approvals_required": 1, "approved_by": [{"username": "alice"}]},
             {"name": "All Members", "approvals_required": 1, "approved_by": [{"username": "kim"}]}]
    rules(fake, 0, 2, [OLD, NEW], approved=True, state=state)
    assert rules_backend(client).approval_rule_blockers(7) == [
        "GitLab approval rule `infra-owners` needs 1 approval(s) of the current head, has 0"]


def test_rules_each_met_by_head_approvals_pass_despite_a_stale_extra(gl):
    fake, client, _ = gl
    state = [{"name": "infra-owners", "approvals_required": 1, "approved_by": [{"username": "kim"}]},
             {"name": "optional", "approvals_required": 0, "approved_by": [{"username": "alice"}]}]
    rules(fake, 0, 1, [OLD, NEW], approved=True, state=state)
    assert rules_backend(client).approval_rule_blockers(7) == []


def test_stale_approvals_without_approval_state_refuse(gl):
    fake, client, _ = gl
    rules(fake, 0, 1, [OLD], approved=True, state=None)
    [reason] = rules_backend(client).approval_rule_blockers(7)
    assert "older commit" in reason


def test_approvals_api_errors_are_not_swallowed(gl):
    fake, client, _ = gl
    fake.routes[("GET", f"{MR7}/approvals")] = lambda u, d: (500, {"message": "boom"})
    with pytest.raises(Exception, match="500"):
        rules_backend(client).approval_rule_blockers(7)


def test_not_approved_merge_status_blocks_only_with_approval_rules(gl):
    fake, client, be = gl
    fake.routes[("GET", MR7)] = {"iid": 7, "sha": "head2", "source_branch": "f", "target_branch": "main",
                                 "author": {"username": "pip"}, "state": "opened",
                                 "detailed_merge_status": "not_approved"}
    assert be.get_change_request(7).blocked_reasons == []  # the CI driver: unchanged
    assert rules_backend(client).get_change_request(7).blocked_reasons == ["not_approved"]
