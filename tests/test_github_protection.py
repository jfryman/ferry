"""GitHub branch protection and rulesets on the apply gate (vcs/github_protection.py)."""

import pytest
from test_github import R, check_run, gql_pr, merge_rules, pr, status

from ferry.vcs.base import ChangeRequest
from ferry.vcs.github import GitHubBackend, GitHubClient

SHA = "abc"


@pytest.fixture
def gh():
    from test_github import FakeGitHub

    fake = FakeGitHub()
    be = GitHubBackend(GitHubClient("tok", transport=fake), "acme/ansible", bot_login="ferry-acme[bot]",
                       own_checks=["ferry apply"], app_id=4242)
    return fake, be


def cr(sha=SHA):
    return ChangeRequest(7, sha, "feature", "main", "pip", "opened")


def ruleset(kind, rid=10, **params):
    return {"type": kind, "ruleset_id": rid, "ruleset_source_type": "Repository", "parameters": params}


def classic(reviews=None, checks=None, conversations=False):
    out = {"required_conversation_resolution": {"enabled": conversations}}
    if reviews is not None:
        out["required_pull_request_reviews"] = reviews
    if checks is not None:
        out["required_status_checks"] = {"strict": False, "contexts": [c for c, _ in checks],
                                         "checks": [{"context": c, "app_id": a} for c, a in checks]}
    return out


def blockers(fake, be, **kw):
    merge_rules(fake, **kw)
    return be.merge_rule_blockers(cr())


# --- nothing configured / everything satisfied ---------------------------------------------------

def test_unprotected_branch_allows(gh):
    fake, be = gh
    assert blockers(fake, be) == []
    # Not protected: the classic endpoint (needs Administration: read) isn't even asked.
    assert not any(p.endswith("/protection") for _, p, _, _ in fake.calls)


def test_satisfied_ruleset_and_classic_allow(gh):
    fake, be = gh
    pull = gql_pr(decision="APPROVED", contexts=[check_run("lint"), status("ci/build")], threads=[True])
    assert blockers(fake, be, pull=pull,
                    rules=[ruleset("pull_request", required_approving_review_count=1,
                                   required_review_thread_resolution=True),
                           ruleset("required_status_checks", required_status_checks=[{"context": "lint"}])],
                    classic=classic(reviews={"required_approving_review_count": 1},
                                    checks=[("ci/build", None)], conversations=True)) == []


def test_ruleset_only_branch_reports_classic_404_as_no_protection(gh):
    fake, be = gh
    assert blockers(fake, be, pull=gql_pr(decision="APPROVED"), protected=True,
                    rules=[ruleset("pull_request", required_approving_review_count=1)]) == []


# --- reviews ------------------------------------------------------------------------------------

def test_required_count_two_with_one_approval(gh):
    fake, be = gh
    [reason] = blockers(fake, be, pull=gql_pr(decision="REVIEW_REQUIRED"),
                        classic=classic(reviews={"required_approving_review_count": 2}))
    assert "branch protection" in reason and "2 approving review(s)" in reason and "REVIEW_REQUIRED" in reason


def test_codeowners_required_and_missing(gh):
    fake, be = gh
    pull = gql_pr(decision="REVIEW_REQUIRED",
                  code_owner_requests=[{"__typename": "Team", "slug": "dns-owners"}])
    [reason] = blockers(fake, be, pull=pull,
                        rules=[ruleset("pull_request", rid=3, required_approving_review_count=1,
                                       require_code_owner_review=True)])
    assert "ruleset 3" in reason and "code owner review (waiting on @dns-owners)" in reason


def test_codeowners_required_and_given(gh):
    fake, be = gh
    assert blockers(fake, be, pull=gql_pr(decision="APPROVED"),
                    classic=classic(reviews={"required_approving_review_count": 1,
                                             "require_code_owner_reviews": True})) == []


def test_stale_approval_dismissed_by_github(gh):
    # The only approval was of an earlier commit and GitHub dismissed it on push.
    fake, be = gh
    [reason] = blockers(fake, be, pull=gql_pr(decision="REVIEW_REQUIRED"),
                        classic=classic(reviews={"required_approving_review_count": 1, "dismiss_stale_reviews": True}))
    assert "approvals of earlier commits were dismissed" in reason


def test_changes_requested(gh):
    fake, be = gh
    [reason] = blockers(fake, be, pull=gql_pr(decision="CHANGES_REQUESTED", merge_state="BLOCKED"),
                        classic=classic(reviews={"required_approving_review_count": 1}))
    assert "requested changes" in reason


def test_reviews_required_somewhere_ferry_cant_see_still_refuse(gh):
    fake, be = gh
    [reason] = blockers(fake, be, pull=gql_pr(decision="REVIEW_REQUIRED", merge_state="BLOCKED"))
    assert "REVIEW_REQUIRED" in reason


def test_verdict_checked_even_when_app_could_bypass(gh):
    # mergeStateStatus is computed for the viewer: an App allowed to bypass sees CLEAN.
    fake, be = gh
    assert blockers(fake, be, pull=gql_pr(decision="REVIEW_REQUIRED", merge_state="CLEAN"),
                    rules=[ruleset("pull_request", required_approving_review_count=1)])


# --- required checks ----------------------------------------------------------------------------

@pytest.mark.parametrize("ctx,word", [
    (check_run("lint", conclusion="FAILURE"), "failure"),
    (check_run("lint", conclusion=None, status="IN_PROGRESS"), "in_progress"),
    (status("lint", state="PENDING"), "pending"),
])
def test_required_check_not_passing(gh, ctx, word):
    fake, be = gh
    [reason] = blockers(fake, be, pull=gql_pr(contexts=[ctx], merge_state="BLOCKED"),
                        rules=[ruleset("required_status_checks", required_status_checks=[{"context": "lint"}])])
    assert "`lint`" in reason and word in reason


def test_required_check_never_reported(gh):
    fake, be = gh
    [reason] = blockers(fake, be, pull=gql_pr(merge_state="BLOCKED"),
                        classic=classic(checks=[("ci/build", None)]))
    assert "`ci/build`" in reason and "hasn't reported" in reason


def test_required_check_pinned_to_another_app(gh):
    fake, be = gh
    pull = gql_pr(contexts=[check_run("lint", app="impostor", app_id=666, required=False)], merge_state="BLOCKED")
    [reason] = blockers(fake, be, pull=pull,
                        rules=[ruleset("required_status_checks",
                                       required_status_checks=[{"context": "lint", "integration_id": 1}])])
    assert "`lint`" in reason


def test_check_github_marks_required_counts_without_reading_rules(gh):
    fake, be = gh
    [reason] = blockers(fake, be, pull=gql_pr(contexts=[status("deploy-preview", state="FAILURE")]))
    assert "`deploy-preview`" in reason


def test_non_required_failing_check_is_fine(gh):
    fake, be = gh
    assert blockers(fake, be, pull=gql_pr(contexts=[check_run("docs", "FAILURE", required=False)],
                                          merge_state="UNSTABLE")) == []


@pytest.mark.parametrize("ctx,rule", [
    (status("ferry apply", state="PENDING"), {"context": "ferry apply"}),            # named in FERRY_GITHUB_OWN_CHECKS
    (check_run("ferry/apply", None, status="QUEUED", app="ferry-acme", app_id=4242),  # posted by ferry's App
     {"context": "ferry/apply"}),
    (status("ferry", state="PENDING", creator="ferry-acme[bot]"), {"context": "ferry"}),  # posted by ferry's bot
    (None, {"context": "anything", "integration_id": 4242}),                          # pinned to ferry's App
])
def test_ferrys_own_required_check_does_not_deadlock(gh, ctx, rule):
    fake, be = gh
    pull = gql_pr(decision="APPROVED", contexts=[ctx] if ctx else [], merge_state="BLOCKED")
    assert blockers(fake, be, pull=pull,
                    rules=[ruleset("pull_request", required_approving_review_count=1),
                           ruleset("required_status_checks", required_status_checks=[rule])]) == []


def test_own_check_doesnt_excuse_another_failing_check(gh):
    fake, be = gh
    pull = gql_pr(decision="APPROVED", merge_state="BLOCKED",
                  contexts=[status("ferry apply", "PENDING"), check_run("lint", "FAILURE")])
    [reason] = blockers(fake, be, pull=pull, rules=[ruleset("required_status_checks", required_status_checks=[
        {"context": "ferry apply"}, {"context": "lint"}])])
    assert "`lint`" in reason


def test_same_name_from_someone_else_isnt_ferrys(gh):
    # Only the configured name or ferry's own identity makes a check ferry's; a lookalike name isn't.
    fake, be = gh
    [reason] = blockers(fake, be, pull=gql_pr(contexts=[status("ferry/plan", "PENDING")], merge_state="BLOCKED"))
    assert "`ferry/plan`" in reason


def test_too_many_checks_to_confirm(gh):
    fake, be = gh
    [reason] = blockers(fake, be, pull=gql_pr(more_contexts=True, merge_state="BLOCKED"),
                        rules=[ruleset("required_status_checks", required_status_checks=[{"context": "lint"}])])
    assert "can't confirm" in reason


# --- conversations ------------------------------------------------------------------------------

def test_unresolved_conversations(gh):
    fake, be = gh
    [reason] = blockers(fake, be, pull=gql_pr(threads=[True, False, False], merge_state="BLOCKED"),
                        rules=[ruleset("pull_request", required_review_thread_resolution=True)])
    assert "2 unresolved" in reason


def test_unresolved_conversations_only_matter_when_required(gh):
    fake, be = gh
    assert blockers(fake, be, pull=gql_pr(threads=[False])) == []


def test_too_many_threads_to_confirm(gh):
    fake, be = gh
    [reason] = blockers(fake, be, pull=gql_pr(threads=[True] * 100, total_threads=150),
                        classic=classic(conversations=True))
    assert "can't confirm" in reason


# --- GitHub says BLOCKED for a reason ferry doesn't model ---------------------------------------

def test_blocked_by_opaque_rule(gh):
    fake, be = gh
    [reason] = blockers(fake, be, pull=gql_pr(merge_state="BLOCKED"), rules=[ruleset("merge_queue", rid=5)])
    assert "merge queue" in reason and "ruleset 5" in reason


def test_blocked_for_unknown_reason(gh):
    fake, be = gh
    [reason] = blockers(fake, be, pull=gql_pr(merge_state="BLOCKED"))
    assert "can't identify" in reason


def test_opaque_rule_with_clean_state_trusts_github(gh):
    fake, be = gh
    assert blockers(fake, be, pull=gql_pr(merge_state="CLEAN"), rules=[ruleset("required_signatures")]) == []


def test_head_moved_while_checking(gh):
    fake, be = gh
    [reason] = blockers(fake, be, pull=gql_pr(sha="def"))
    assert "head moved" in reason


# --- fail closed when ferry can't read the rules ------------------------------------------------

NO_ACCESS = (403, {"message": "Resource not accessible by integration"})


def test_no_permission_for_classic_protection(gh):
    fake, be = gh
    merge_rules(fake, protected=True)
    fake.routes[("GET", f"{R}/branches/main/protection")] = lambda q, d: NO_ACCESS
    [reason] = be.merge_rule_blockers(cr())
    assert "**Administration: read**" in reason and "403" in reason


def test_no_permission_for_rulesets(gh):
    fake, be = gh
    merge_rules(fake)
    fake.routes[("GET", f"{R}/rules/branches/main")] = lambda q, d: NO_ACCESS
    [reason] = be.merge_rule_blockers(cr())
    assert "rulesets" in reason and "**Metadata: read**" in reason


@pytest.mark.parametrize("path,perm", [
    (["repository", "pullRequest", "commits", "nodes", 0, "commit", "statusCheckRollup"],
     "Checks: read and Commit statuses: read"),
    (["repository", "pullRequest"], "Pull requests: read"),
])
def test_graphql_forbidden_names_the_permission(gh, path, perm):
    fake, be = gh
    merge_rules(fake)
    fake.routes[("POST", "/graphql")] = lambda q, d: (200, {"data": {"repository": {"pullRequest": None}}, "errors": [
        {"type": "FORBIDDEN", "path": path, "message": "Resource not accessible by integration"}]})
    [reason] = be.merge_rule_blockers(cr())
    assert f"**{perm}**" in reason


def test_graphql_other_error_refuses(gh):
    fake, be = gh
    merge_rules(fake)
    fake.routes[("POST", "/graphql")] = lambda q, d: (502, {"message": "Bad gateway"})
    [reason] = be.merge_rule_blockers(cr())
    assert "refusing" in reason


def test_graphql_url_for_enterprise_server():
    assert GitHubClient("t", api_url="https://ghe.example/api/v3").graphql_url() == "https://ghe.example/api/graphql"
    assert GitHubClient("t").graphql_url() == "https://api.github.com/graphql"


def test_graphql_request_asks_for_review_decision_and_required_checks(gh):
    fake, be = gh
    merge_rules(fake)
    be.merge_rule_blockers(cr())
    [(_, _, _, body)] = [c for c in fake.calls if c[1] == "/graphql"]
    assert body["variables"] == {"owner": "acme", "name": "ansible", "number": 7}
    for field in ("reviewDecision", "mergeStateStatus", "isRequired(pullRequestNumber: $number)", "asCodeOwner",
                  "reviewThreads"):
        assert field in body["query"]


# --- through the engine: both gates must pass ---------------------------------------------------

def test_engine_refuses_when_github_rules_unmet_even_with_ferry_approval(gh, repo, tmp_path):
    from fakes import FakeRunner, ev_stats, stats

    from ferry import config
    from ferry.engine import Engine
    from ferry.locks import SQLiteLocks

    fake, be = gh
    sha = "c0ffee" + "0" * 34
    comments = []

    def post(q, d):
        c = {"id": len(comments) + 1, "user": {"login": "ferry-acme[bot]"}, "body": d["body"], "created_at": "t"}
        comments.append(c)
        return 201, c

    fake.routes[("GET", f"{R}/pulls/1")] = pr(number=1, sha=sha, mergeable_state="blocked")
    fake.routes[("GET", f"{R}/pulls/1/files")] = [{"filename": "inventories/homelab/host_vars/dns-1.yml"}]
    fake.routes[("GET", f"{R}/issues/1/comments")] = lambda q, d: (200, comments)
    fake.routes[("POST", f"{R}/issues/1/comments")] = post
    reviews = [{"user": {"login": "james"}, "state": "APPROVED", "commit_id": sha}]
    fake.routes[("GET", f"{R}/pulls/1/reviews")] = lambda q, d: (200, reviews)
    fake.routes[("GET", f"{R}/collaborators/james/permission")] = {"role_name": "write"}
    fake.routes[("GET", f"{R}/collaborators/pip/permission")] = {"role_name": "write"}
    decision = {"v": "REVIEW_REQUIRED"}
    merge_rules(fake, lambda: gql_pr(sha=sha, decision=decision["v"],
                                     merge_state="CLEAN" if decision["v"] == "APPROVED" else "BLOCKED",
                                     code_owner_requests=[{"__typename": "User", "login": "owner1"}]),
                classic=classic(reviews={"required_approving_review_count": 1, "require_code_owner_reviews": True}))

    cfg = config.load(repo / "ferry.yaml")
    run = ([ev_stats(**{"dns-1": stats(ok=5)})], 0)
    eng = Engine(cfg, repo, FakeRunner(run, run), be, SQLiteLocks(tmp_path / "l.db"), clock=lambda: 1_800_000_000.0)
    assert eng.plan(1, sha, "pip").ok

    out = eng.apply(1, sha, "pip")
    assert not out.ok
    assert "code owner review (waiting on @owner1)" in comments[-1]["body"]

    # GitHub is satisfied now, but ferry's own check isn't: the only approval is of an older commit.
    decision["v"] = "APPROVED"
    reviews[0]["commit_id"] = "0" * 40
    out = eng.apply(1, sha, "pip")
    assert not out.ok and "approval" in comments[-1]["body"] and "code owner" not in comments[-1]["body"]

    reviews[0]["commit_id"] = sha
    assert eng.apply(1, sha, "pip").ok
