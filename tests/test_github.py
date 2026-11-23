import json
from urllib.parse import parse_qs, urlparse

import pytest

from ferry.locks import GitHubTagLocks, new_lock
from ferry.vcs.base import VCSBackend
from ferry.vcs.github import GitHubBackend, GitHubClient, GitHubError

API = "https://api.github.com"
R = "/repos/acme/ansible"


class FakeGitHub:
    """Routes (method, path-without-query) to handlers; records calls. Mimics GitHub's git refs/tags."""

    def __init__(self):
        self.calls = []
        self.routes = {}
        self.refs = {}  # "tags/ferry-lock-x" -> {"type": "tag", "sha": tag_sha}
        self.tag_objects = {}  # tag_sha -> tag object
        self.tokens_seen = []

    def __call__(self, method, url, headers, body):
        assert headers["Accept"] == "application/vnd.github+json"
        assert headers["X-GitHub-Api-Version"] == "2022-11-28"
        self.tokens_seen.append(headers["Authorization"])
        parsed = urlparse(url)
        path = parsed.path
        data = json.loads(body) if body else None
        self.calls.append((method, path, parse_qs(parsed.query), data))
        if path.startswith(f"{R}/git/"):
            return self._git(method, path[len(f"{R}/git/"):], data)
        handler = self.routes.get((method, path))
        if handler is None:
            return 404, b'{"message":"Not Found"}'
        status, obj = handler(parse_qs(parsed.query), data) if callable(handler) else (200, handler)
        return status, json.dumps(obj).encode()

    def _git(self, method, rest, data):
        if method == "POST" and rest == "tags":
            sha = f"tagsha{len(self.tag_objects) + 1}"
            self.tag_objects[sha] = {"sha": sha, "tag": data["tag"], "message": data["message"],
                                     "object": {"sha": data["object"], "type": data["type"]}}
            return 201, json.dumps(self.tag_objects[sha]).encode()
        if method == "POST" and rest == "refs":
            name = data["ref"][len("refs/"):]
            if name in self.refs:
                return 422, b'{"message":"Reference already exists"}'
            self.refs[name] = {"type": "tag", "sha": data["sha"]}
            return 201, json.dumps({"ref": data["ref"], "object": self.refs[name]}).encode()
        if method == "GET" and rest.startswith("ref/"):
            name = rest[len("ref/"):]
            if name not in self.refs:
                return 404, b'{"message":"Not Found"}'
            return 200, json.dumps({"ref": f"refs/{name}", "object": self.refs[name]}).encode()
        if method == "GET" and rest.startswith("tags/"):
            obj = self.tag_objects.get(rest[len("tags/"):])
            return (200, json.dumps(obj).encode()) if obj else (404, b'{"message":"Not Found"}')
        if method == "DELETE" and rest.startswith("refs/"):
            name = rest[len("refs/"):]
            if name not in self.refs:
                return 422, b'{"message":"Reference does not exist"}'
            del self.refs[name]
            return 204, b""
        return 404, b'{"message":"Not Found"}'


def pr(number=7, sha="abc", state="open", merged=False, draft=False, mergeable=True, mergeable_state="clean"):
    return {"number": number, "head": {"sha": sha, "ref": "feature"}, "base": {"ref": "main"},
            "user": {"login": "pip"}, "state": state, "merged": merged, "merged_at": "x" if merged else None,
            "draft": draft, "mergeable": mergeable, "mergeable_state": mergeable_state,
            "html_url": f"https://github.com/acme/ansible/pull/{number}"}


@pytest.fixture
def gh():
    fake = FakeGitHub()
    client = GitHubClient("tok", transport=fake)
    return fake, client, GitHubBackend(client, "acme/ansible", bot_login="ferry-acme[bot]")


def test_implements_the_backend_protocol(gh):
    _, _, be = gh
    for name in [m for m in dir(VCSBackend) if not m.startswith("_")]:
        assert callable(getattr(be, name)), name


def test_repo_must_be_owner_slash_name():
    with pytest.raises(ValueError):
        GitHubBackend(GitHubClient("tok"), "just-a-name")


def test_token_callable_is_asked_on_every_request(gh):
    fake, _, _ = gh
    tokens = iter(["t1", "t2"])
    client = GitHubClient(lambda: next(tokens), transport=fake)
    be = GitHubBackend(client, "acme/ansible", bot_login="b[bot]")
    fake.routes[("GET", f"{R}/pulls/7")] = pr()
    be.get_change_request(7)
    be.get_change_request(7)
    assert fake.tokens_seen == ["Bearer t1", "Bearer t2"]


def test_open_pr_maps_to_change_request(gh):
    fake, _, be = gh
    fake.routes[("GET", f"{R}/pulls/7")] = pr()
    cr = be.get_change_request(7)
    assert (cr.id, cr.head_sha, cr.source_branch, cr.target_branch, cr.author) == (7, "abc", "feature", "main", "pip")
    assert cr.open and not cr.draft and not cr.has_conflicts and cr.blocked_reasons == []
    assert cr.web_url.endswith("/pull/7")


@pytest.mark.parametrize("state,merged,expected", [("closed", True, "merged"), ("closed", False, "closed")])
def test_closed_and_merged_states(gh, state, merged, expected):
    fake, _, be = gh
    fake.routes[("GET", f"{R}/pulls/7")] = pr(state=state, merged=merged, mergeable_state="unknown")
    cr = be.get_change_request(7)
    assert cr.state == expected and not cr.open
    assert cr.blocked_reasons == []  # only open PRs report merge blockers


@pytest.mark.parametrize("merge_state,blocked", [
    ("clean", False), ("unstable", False), ("has_hooks", False),
    ("blocked", True), ("behind", True), ("dirty", True), ("draft", True), ("unknown", True),
])
def test_merge_state_blockers(gh, merge_state, blocked):
    fake, _, be = gh
    fake.routes[("GET", f"{R}/pulls/7")] = pr(mergeable_state=merge_state)
    cr = be.get_change_request(7)
    assert (cr.blocked_reasons == [merge_state]) is blocked
    assert cr.merge_status == merge_state


def test_conflicts_and_draft(gh):
    fake, _, be = gh
    fake.routes[("GET", f"{R}/pulls/7")] = pr(mergeable=False, mergeable_state="dirty", draft=True)
    cr = be.get_change_request(7)
    assert cr.has_conflicts and cr.draft
    fake.routes[("GET", f"{R}/pulls/7")] = pr(mergeable=None, mergeable_state="unknown")
    assert be.get_change_request(7).has_conflicts is False  # not computed yet is not a conflict


def test_changed_files_pages_and_counts_renames(gh):
    fake, _, be = gh
    page1 = [{"filename": f"roles/r{i}/tasks/main.yml"} for i in range(99)]
    page1.append({"filename": "group_vars/new.yml", "previous_filename": "group_vars/old.yml"})
    pages = {"1": page1, "2": [{"filename": "site.yml"}]}
    fake.routes[("GET", f"{R}/pulls/7/files")] = lambda q, d: (200, pages[q["page"][0]])
    files = be.changed_files(7)
    assert len(files) == 102
    assert {"group_vars/new.yml", "group_vars/old.yml", "site.yml"} <= set(files)


def test_approvers_use_latest_decisive_review_of_the_head(gh):
    fake, _, be = gh
    fake.routes[("GET", f"{R}/pulls/7")] = pr(sha="head")
    fake.routes[("GET", f"{R}/pulls/7/reviews")] = [
        {"user": {"login": "ana"}, "state": "APPROVED", "commit_id": "head"},
        {"user": {"login": "ana"}, "state": "COMMENTED", "commit_id": "head"},  # doesn't undo approval
        {"user": {"login": "bo"}, "state": "APPROVED", "commit_id": "head"},
        {"user": {"login": "bo"}, "state": "CHANGES_REQUESTED", "commit_id": "head"},
        {"user": {"login": "cy"}, "state": "APPROVED", "commit_id": "older"},  # stale
        {"user": {"login": "di"}, "state": "APPROVED", "commit_id": "head"},
        {"user": {"login": "di"}, "state": "DISMISSED", "commit_id": "head"},
        {"user": None, "state": "APPROVED", "commit_id": "head"},  # deleted account
    ]
    assert be.approvers(7) == ["ana"]


def test_notes_are_issue_comments(gh):
    fake, _, be = gh
    fake.routes[("GET", f"{R}/issues/7/comments")] = [
        {"id": 1, "user": {"login": "ferry-acme[bot]"}, "body": "plan", "created_at": "t1"},
        {"id": 2, "user": {"login": "pip"}, "body": None, "created_at": "t2"},
    ]
    notes = be.list_notes(7)
    assert [(n.id, n.author, n.body) for n in notes] == [(1, "ferry-acme[bot]", "plan"), (2, "pip", "")]
    fake.routes[("POST", f"{R}/issues/7/comments")] = lambda q, d: (
        201, {"id": 3, "user": {"login": "ferry-acme[bot]"}, "body": d["body"], "created_at": "t3"})
    assert be.post_note(7, "hello").body == "hello"


def test_bot_username_prefers_configured_login(gh):
    fake, client, be = gh
    assert be.bot_username() == "ferry-acme[bot]"
    assert not fake.calls  # App tokens can't call /user
    fake.routes[("GET", "/user")] = {"login": "ferry-machine-user"}
    assert GitHubBackend(client, "acme/ansible").bot_username() == "ferry-machine-user"


@pytest.mark.parametrize("perm,ok", [
    ({"permission": "admin", "role_name": "admin"}, True),
    ({"permission": "write", "role_name": "maintain"}, True),
    ({"permission": "write", "role_name": "write"}, True),
    ({"permission": "read", "role_name": "triage"}, False),
    ({"permission": "read", "role_name": "read"}, False),
    ({"permission": "write"}, True),
])
def test_can_apply_needs_write(gh, perm, ok):
    fake, _, be = gh
    fake.routes[("GET", f"{R}/collaborators/pip/permission")] = perm
    assert be.can_apply("pip") is ok


def test_can_apply_unknown_user_is_false(gh):
    _, _, be = gh
    assert be.can_apply("stranger") is False


def test_other_errors_raise(gh):
    fake, _, be = gh
    fake.routes[("GET", f"{R}/collaborators/pip/permission")] = lambda q, d: (500, {"message": "boom"})
    with pytest.raises(GitHubError):
        be.can_apply("pip")


def test_change_requests_for_commit(gh):
    fake, _, be = gh
    fake.routes[("GET", f"{R}/commits/deadbeef/pulls")] = [{"number": 9}, {"number": 4}]
    assert be.change_requests_for_commit("deadbeef") == [4, 9]


# Locks


@pytest.fixture
def locks(gh):
    fake, client, _ = gh
    return fake, GitHubTagLocks(client, "acme/ansible")


def test_lock_acquire_get_release(locks):
    fake, store = locks
    assert store.get("prod") is None
    assert store.try_acquire(new_lock("prod", 7, "abc", "pip")) is None
    held = store.get("prod")
    assert (held.holder, held.sha, held.user) == (7, "abc", "pip")
    assert "tags/ferry-lock-prod" in fake.refs
    assert store.release("prod", 8) is False  # not the holder
    assert store.release("prod", 7) is True
    assert store.get("prod") is None


def test_second_acquire_returns_the_holder(locks):
    _, store = locks
    store.try_acquire(new_lock("prod", 7, "abc", "pip"))
    conflict = store.try_acquire(new_lock("prod", 8, "def", "ana"))
    assert conflict is not None and conflict.holder == 7


def test_unreadable_lock_is_held_not_free(locks):
    fake, store = locks
    fake.tag_objects["junk"] = {"sha": "junk", "message": "not json", "object": {"sha": "c0ffee"}}
    fake.refs["tags/ferry-lock-prod"] = {"type": "tag", "sha": "junk"}
    held = store.get("prod")
    assert held.holder == 0 and held.sha == "c0ffee"
    assert store.try_acquire(new_lock("prod", 7, "abc", "pip")).holder == 0


# CLI wiring


def _cli_args(repo, **kw):
    import argparse
    base = dict(repo=str(repo), config="ferry.yaml", api_url=None, project=None, vcs=None,
                mr=None, sha=None, user=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_builds_github_backend(repo, monkeypatch):
    from ferry import cli
    monkeypatch.setenv("FERRY_VCS", "github")
    monkeypatch.setenv("FERRY_GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/ansible")
    monkeypatch.setenv("FERRY_GITHUB_BOT", "ferry-acme[bot]")
    eng = cli.build_engine(_cli_args(repo))
    assert isinstance(eng.vcs, GitHubBackend) and isinstance(eng.locks, GitHubTagLocks)
    assert eng.vcs.bot_username() == "ferry-acme[bot]"


def test_cli_github_flag_overrides_env_and_reports_missing(repo, monkeypatch):
    from ferry import cli
    monkeypatch.delenv("FERRY_VCS", raising=False)
    monkeypatch.delenv("FERRY_GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="FERRY_GITHUB_TOKEN"):
        cli.build_engine(_cli_args(repo, vcs="github", project="acme/ansible"))


def test_cli_github_user_comes_only_from_flag(repo, monkeypatch):
    from ferry import cli
    monkeypatch.setenv("FERRY_VCS", "github")
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "trigger")
    monkeypatch.setenv("FERRY_USER", "someone-else")
    monkeypatch.setenv("GITLAB_USER_LOGIN", "also-not")
    assert cli._mr_args(_cli_args(repo, mr=7, sha="abc")) == (7, "abc", "unknown")
    assert cli._mr_args(_cli_args(repo, mr=7, sha="abc", user="pip")) == (7, "abc", "pip")


def test_cli_rejects_unknown_vcs(repo, monkeypatch):
    from ferry import cli
    monkeypatch.setenv("FERRY_VCS", "bitbucket")
    with pytest.raises(SystemExit, match="unknown VCS"):
        cli.build_engine(_cli_args(repo))


# The engine end to end on the GitHub backend: plan comments and takes the lock (a tag ref), apply
# passes the gates on live PR state, and a forged plan marker from someone else is ignored.


def test_engine_plan_then_apply_on_github(repo, gh):
    from fakes import FakeRunner, ev_result, ev_stats, stats

    from ferry import config
    from ferry.engine import Engine
    from ferry.sanitize import Redactor

    fake, client, be = gh
    sha = "c0ffee" + "0" * 34
    comments = []

    def post(q, d):
        c = {"id": len(comments) + 1, "user": {"login": "ferry-acme[bot]"}, "body": d["body"], "created_at": "t"}
        comments.append(c)
        return 201, c

    fake.routes[("GET", f"{R}/pulls/1")] = pr(number=1, sha=sha)
    fake.routes[("GET", f"{R}/pulls/1/files")] = [{"filename": "inventories/homelab/host_vars/dns-1.yml"}]
    fake.routes[("GET", f"{R}/issues/1/comments")] = lambda q, d: (200, comments)
    fake.routes[("POST", f"{R}/issues/1/comments")] = post
    fake.routes[("GET", f"{R}/pulls/1/reviews")] = [{"user": {"login": "james"}, "state": "APPROVED", "commit_id": sha}]
    fake.routes[("GET", f"{R}/collaborators/james/permission")] = {"permission": "write", "role_name": "write"}

    cfg = config.load(repo / "ferry.yaml")
    red = Redactor(cfg.redact.patterns, cfg.redact.env_vars, cfg.redact.sensitive_paths, environ={})
    run = ([ev_result("dns-1", "Write chrony.conf", "changed", role="chrony"),
            ev_stats(**{"dns-1": stats(ok=5, changed=1)})], 0)
    locks = GitHubTagLocks(client, "acme/ansible")

    eng = Engine(cfg, repo, FakeRunner(run), be, locks, red, lambda: 1_800_000_000.0)
    assert eng.plan(1, sha, "pip").ok
    assert "tags/ferry-lock-homelab" in fake.refs and locks.get("homelab").holder == 1
    assert "ferry:plan" in comments[-1]["body"]

    # Someone else pastes a copy of the plan marker: apply must not trust it over ferry's own.
    comments.append({"id": 99, "user": {"login": "mallory"}, "body": comments[-1]["body"], "created_at": "t"})

    runner = FakeRunner(run)
    out = Engine(cfg, repo, runner, be, locks, red, lambda: 1_800_000_000.0).apply(1, sha, "james")
    assert out.ok, out.reasons
    assert runner.requests[0].check is False and runner.requests[0].limit == "dns-1"

    # Unapproved head: refused.
    fake.routes[("GET", f"{R}/pulls/1/reviews")] = [{"user": {"login": "james"}, "state": "APPROVED", "commit_id": "old"}]
    out = Engine(cfg, repo, FakeRunner(run), be, locks, red, lambda: 1_800_000_000.0).apply(1, sha, "james")
    assert not out.ok and any("approval" in r for r in out.reasons)
