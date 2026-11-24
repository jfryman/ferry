import json
from urllib.parse import parse_qs, urlparse

import pytest

from ferry.locks import SQLiteLocks, new_lock
from ferry.vcs.base import VCSBackend
from ferry.vcs.github import GitHubBackend, GitHubClient, GitHubError

API = "https://api.github.com"
R = "/repos/acme/ansible"


class FakeGitHub:
    """Routes (method, path-without-query) to handlers; records calls."""

    def __init__(self):
        self.calls = []
        self.routes = {}
        self.tokens_seen = []
        self.behind_by = 0  # what /compare/BASE...HEAD answers when no route is set (#23)

    def __call__(self, method, url, headers, body):
        assert headers["Accept"] == "application/vnd.github+json"
        assert headers["X-GitHub-Api-Version"] == "2022-11-28"
        self.tokens_seen.append(headers["Authorization"])
        parsed = urlparse(url)
        path = parsed.path
        data = json.loads(body) if body else None
        self.calls.append((method, path, parse_qs(parsed.query), data))
        handler = self.routes.get((method, path))
        if handler is None and method == "GET" and path.endswith("/commits") and "/pulls/" in path:
            handler = []  # #25: no commits listed means nobody but the author contributed
        if handler is None and method == "GET" and "/compare/" in path:
            handler = {"status": "behind" if self.behind_by else "ahead", "behind_by": self.behind_by, "ahead_by": 1}
        if handler is None:
            return 404, b'{"message":"Not Found"}'
        status, obj = handler(parse_qs(parsed.query), data) if callable(handler) else (200, handler)
        return status, json.dumps(obj).encode()


def pr(number=7, sha="abc", state="open", merged=False, draft=False, mergeable=True, mergeable_state="clean",
       base="main"):
    return {"number": number, "head": {"sha": sha, "ref": "feature"}, "base": {"ref": base},
            "user": {"login": "pip"}, "state": state, "merged": merged, "merged_at": "x" if merged else None,
            "draft": draft, "mergeable": mergeable, "mergeable_state": mergeable_state,
            "html_url": f"https://github.com/acme/ansible/pull/{number}"}


def gql_pr(sha="abc", decision=None, merge_state="CLEAN", contexts=(), threads=(), code_owner_requests=(),
           more_contexts=False, total_threads=None):
    """GraphQL pullRequest, as protection.PR_QUERY asks for it."""
    return {"headRefOid": sha, "reviewDecision": decision, "mergeStateStatus": merge_state,
            "reviewRequests": {"nodes": [{"asCodeOwner": True, "requestedReviewer": r} for r in code_owner_requests]},
            "reviewThreads": {"totalCount": len(threads) if total_threads is None else total_threads,
                              "nodes": [{"isResolved": t} for t in threads]},
            "commits": {"nodes": [{"commit": {"oid": sha, "statusCheckRollup": {"contexts": {
                "pageInfo": {"hasNextPage": more_contexts}, "nodes": list(contexts)}}}}]}}


def check_run(name, conclusion="SUCCESS", required=True, app="ci", app_id=1, status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
            "isRequired": required, "checkSuite": {"app": {"slug": app, "databaseId": app_id}}}


def status(context, state="SUCCESS", required=True, creator="ci-bot"):
    return {"__typename": "StatusContext", "context": context, "state": state, "isRequired": required,
            "creator": {"login": creator}}


def merge_rules(fake, pull=None, rules=(), classic=None, protected=None, branch="main"):
    """Serve the rule sources for `branch` and the GraphQL PR (a dict, or a callable returning one)."""
    fake.routes[("GET", f"{R}/rules/branches/{branch}")] = list(rules)
    is_protected = classic is not None if protected is None else protected
    fake.routes[("GET", f"{R}/branches/{branch}")] = {"name": branch, "protected": is_protected}
    if classic is not None:
        fake.routes[("GET", f"{R}/branches/{branch}/protection")] = classic
    elif is_protected:
        fake.routes[("GET", f"{R}/branches/{branch}/protection")] = lambda q, d: (404, {"message": "Branch not protected"})
    get = pull if callable(pull) else (lambda: pull if pull is not None else gql_pr())
    fake.routes[("POST", "/graphql")] = lambda q, d: (200, {"data": {"repository": {"pullRequest": get()}}})


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
    ("blocked", False),  # left to merge_rule_blockers, which names the rule
    ("behind", True), ("dirty", True), ("draft", True), ("unknown", True),
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


# CLI wiring


def _cli_args(repo, **kw):
    import argparse
    base = dict(repo=str(repo), config="ferry.yaml", api_url=None, project=None, vcs=None, lock_db=None,
                mr=None, sha=None, user=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_builds_github_backend(repo, monkeypatch, tmp_path):
    from ferry import cli
    monkeypatch.setenv("FERRY_LOCK_DB", str(tmp_path / "state" / "locks.db"))
    monkeypatch.setenv("FERRY_VCS", "github")
    monkeypatch.setenv("FERRY_GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/ansible")
    monkeypatch.setenv("FERRY_GITHUB_BOT", "ferry-acme[bot]")
    eng = cli.build_engine(_cli_args(repo))
    assert isinstance(eng.vcs, GitHubBackend) and isinstance(eng.locks, SQLiteLocks)
    assert eng.locks.path == tmp_path / "state" / "locks.db"
    assert eng.vcs.bot_username() == "ferry-acme[bot]"


def test_cli_github_flag_overrides_env_and_reports_missing(repo, monkeypatch):
    from ferry import cli
    monkeypatch.delenv("FERRY_VCS", raising=False)
    monkeypatch.delenv("FERRY_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("FERRY_LOCK_DB", raising=False)
    with pytest.raises(SystemExit, match="FERRY_GITHUB_TOKEN.*FERRY_LOCK_DB"):
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


# The engine end to end on the GitHub backend: plan comments and takes the lock (the service's file), apply
# passes the gates on live PR state, and a forged plan marker from someone else is ignored.


def test_engine_plan_then_apply_on_github(repo, gh, tmp_path):
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
    merge_rules(fake, gql_pr(sha=sha, decision="APPROVED"),
                rules=[{"type": "pull_request", "ruleset_id": 1, "parameters": {"required_approving_review_count": 1}}])

    cfg = config.load(repo / "ferry.yaml")
    red = Redactor(cfg.redact.patterns, cfg.redact.env_vars, cfg.redact.sensitive_paths, environ={})
    run = ([ev_result("dns-1", "Write chrony.conf", "changed", role="chrony"),
            ev_stats(**{"dns-1": stats(ok=5, changed=1)})], 0)
    locks = SQLiteLocks(tmp_path / "locks.db")

    eng = Engine(cfg, repo, FakeRunner(run), be, locks, red, lambda: 1_800_000_000.0)
    assert eng.plan(1, sha, "pip").ok
    assert locks.get("homelab").holder == 1
    assert not any("/git/" in path for _, path, _, _ in fake.calls)  # nothing written to the repo
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


@pytest.mark.parametrize("status, taken", [(500, False), (502, False), (403, False), (401, False),
                                           (404, True), (410, True)])
def test_lock_holder_lookup_errors_through_the_real_client(repo, tmp_path, status, taken):
    """#21: only a definite 404/410 for the holder PR gives its lock away."""
    from ferry import config
    from ferry.engine import Engine
    from ferry.sanitize import Redactor
    from fakes import FakeRunner, ev_stats

    fake = FakeGitHub()
    be = GitHubBackend(GitHubClient("tok", transport=fake), "acme/ansible", bot_login="ferry-acme[bot]")
    fake.routes[("GET", f"{R}/pulls/1")] = pr(number=1, sha="a" * 40)
    fake.routes[("GET", f"{R}/pulls/2")] = lambda q, d: (status, {"message": "x"})
    fake.routes[("GET", f"{R}/pulls/1/files")] = [{"filename": "inventories/homelab/host_vars/dns-1.yml"}]
    fake.routes[("POST", f"{R}/issues/1/comments")] = lambda q, d: (201, {"id": 1, "user": {"login": "b"},
                                                                         "body": d["body"]})
    cfg = config.load(repo / "ferry.yaml")
    locks = SQLiteLocks(tmp_path / "locks.db")
    locks.try_acquire(new_lock("homelab", 2, "b" * 40, "james"))
    runner = FakeRunner(([ev_stats()], 0))
    out = Engine(cfg, repo, runner, be, locks, Redactor([], [], [], environ={}), lambda: 1.0).plan(1, "a" * 40, "pip")
    assert out.ok is taken
    assert locks.get("homelab").holder == (1 if taken else 2)
    assert bool(runner.requests) is taken


def test_behind_by_uses_the_compare_api(gh_backend=None):
    from ferry.vcs.base import ChangeRequest
    fake = FakeGitHub()
    be = GitHubBackend(GitHubClient("tok", transport=fake), "acme/ansible", bot_login="b[bot]")
    fake.routes[("GET", f"{R}/compare/main...{'a' * 40}")] = {"status": "diverged", "behind_by": 3, "ahead_by": 1}
    cr = ChangeRequest(1, "a" * 40, "feature", "main", "pip", "opened")
    assert be.behind_by(cr) == 3
    fake.routes[("GET", f"{R}/compare/main...{'a' * 40}")] = {"status": "ahead", "behind_by": 0, "ahead_by": 1}
    assert be.behind_by(cr) == 0
    fake.routes[("GET", f"{R}/compare/main...{'a' * 40}")] = lambda q, d: (500, {"message": "x"})
    with pytest.raises(GitHubError):
        be.behind_by(cr)
    fake.routes[("GET", f"{R}/compare/main...{'a' * 40}")] = {"status": "ahead"}  # no behind_by: unknown
    with pytest.raises(ValueError):
        be.behind_by(cr)


def test_contributors_are_commit_authors_and_committers():
    from ferry.vcs.base import ChangeRequest
    fake = FakeGitHub()
    be = GitHubBackend(GitHubClient("tok", transport=fake), "acme/ansible", bot_login="b[bot]")
    fake.routes[("GET", f"{R}/pulls/1/commits")] = [
        {"sha": "1", "author": {"login": "alice"}, "committer": {"login": "web-flow"}},
        {"sha": "2", "author": {"login": "alice"}, "committer": {"login": "bob"}},  # bob rebased/amended
        {"sha": "3", "author": None, "committer": None,  # emails not linked to an account
         "commit": {"author": {"email": "x@y"}, "committer": {"email": "x@y"}}},
        {"sha": "4", "author": {"login": "Carol"}, "committer": {"login": "carol"}},
    ]
    cr = ChangeRequest(1, "a" * 40, "f", "main", "alice", "opened")
    assert be.contributors(cr) == {"alice", "bob", "Carol", "carol"}
    fake.routes[("GET", f"{R}/pulls/1/commits")] = lambda q, d: (500, {"message": "x"})
    with pytest.raises(GitHubError):
        be.contributors(cr)


def test_contributors_refuse_a_pr_with_more_commits_than_github_lists():
    from ferry.vcs.base import ChangeRequest
    fake = FakeGitHub()
    be = GitHubBackend(GitHubClient("tok", transport=fake), "acme/ansible", bot_login="b[bot]")
    page = [{"sha": str(i), "author": {"login": "alice"}, "committer": {"login": "alice"}} for i in range(100)]
    fake.routes[("GET", f"{R}/pulls/1/commits")] = lambda q, d: (200, page if int(q["page"][0]) <= 3 else [])
    with pytest.raises(ValueError, match="250"):
        be.contributors(ChangeRequest(1, "a" * 40, "f", "main", "alice", "opened"))
