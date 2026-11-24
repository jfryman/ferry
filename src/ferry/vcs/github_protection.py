"""Would GitHub's own rules let this PR merge? (branch protection and rulesets)

ferry's apply gate keeps its own approval check (an approval of the current head from someone with
write access). This module adds the code host's rules on top, so ferry never applies a change that
GitHub itself would refuse to merge.

GitHub's computed verdict is preferred over re-implementing the rules:

- `reviewDecision` (GraphQL) is GitHub's answer for required approving reviews, CODEOWNERS review,
  stale-review dismissal and requested changes, across classic protection and rulesets.
- `isRequired` on each check/status says whether GitHub requires it for this PR.
- `mergeStateStatus` BLOCKED says *something* blocks the merge.

The rules themselves (classic protection, active rulesets) are read only to name what is unmet, to
find required checks that never reported, to know whether conversations must be resolved, and to
tell whether a BLOCKED state is explained only by ferry's own required check (which can't pass
before ferry applies, so it must not deadlock the apply).

`mergeStateStatus` is computed for the viewer, so an App that may bypass the rules sees CLEAN.
That's why the named rules are checked independently of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# Rule types GitHub enforces at merge time that ferry doesn't evaluate itself. When one is active
# and GitHub says BLOCKED, ferry can't tell whether it's the cause, so it refuses.
OPAQUE_RULES = {
    "merge_queue": "merges must go through the merge queue",
    "required_deployments": "deployments must succeed first",
    "required_signatures": "commits must be signed",
    "code_scanning": "code scanning results are required",
    "workflows": "required workflows must pass",
    "lock_branch": "the branch is locked",
    "update": "the branch is locked (update restricted)",
}

PASSING_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}

PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      headRefOid
      reviewDecision
      mergeStateStatus
      reviewRequests(first: 50) {
        nodes { asCodeOwner requestedReviewer { __typename ... on User { login } ... on Team { slug } } }
      }
      reviewThreads(first: 100) { totalCount nodes { isResolved } }
      commits(last: 1) { nodes { commit { oid statusCheckRollup { contexts(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          __typename
          ... on CheckRun { name status conclusion isRequired(pullRequestNumber: $number)
                            checkSuite { app { slug databaseId } } }
          ... on StatusContext { context state isRequired(pullRequestNumber: $number) creator { login } }
        }
      } } } } }
    }
  }
}
"""


@dataclass
class RequiredCheck:
    name: str
    app_id: int | None  # only this app's check satisfies it (None/-1: any source)
    source: str


@dataclass
class Requirements:
    """What classic protection and active rulesets ask of a PR into one branch, merged (strictest wins)."""

    review_count: int = 0
    review_sources: list[str] = field(default_factory=list)
    code_owners: bool = False
    dismiss_stale: bool = False
    last_push_approval: bool = False
    conversation_resolution: str | None = None  # source that requires it
    checks: list[RequiredCheck] = field(default_factory=list)
    strict_checks: bool = False
    opaque: list[str] = field(default_factory=list)  # human-readable, with source

    def add_reviews(self, count: int, code_owners: bool, dismiss_stale: bool, last_push: bool, source: str):
        self.review_count = max(self.review_count, int(count or 0))
        self.code_owners |= bool(code_owners)
        self.dismiss_stale |= bool(dismiss_stale)
        self.last_push_approval |= bool(last_push)
        self.review_sources.append(source)

    @property
    def reviews_required(self) -> bool:
        return self.review_count > 0 or self.code_owners or self.last_push_approval


def from_classic(protection: dict | None, req: Requirements) -> None:
    """GET /repos/{r}/branches/{b}/protection."""
    if not protection:
        return
    src = "branch protection"
    rv = protection.get("required_pull_request_reviews")
    if rv:
        req.add_reviews(rv.get("required_approving_review_count", 0), rv.get("require_code_owner_reviews"),
                        rv.get("dismiss_stale_reviews"), rv.get("require_last_push_approval"), src)
    sc = protection.get("required_status_checks")
    if sc:
        req.strict_checks |= bool(sc.get("strict"))
        seen = set()
        for c in sc.get("checks") or []:
            req.checks.append(RequiredCheck(c["context"], c.get("app_id"), src))
            seen.add(c["context"])
        for name in sc.get("contexts") or []:  # legacy list, same names as `checks`
            if name not in seen:
                req.checks.append(RequiredCheck(name, None, src))
    if (protection.get("required_conversation_resolution") or {}).get("enabled"):
        req.conversation_resolution = req.conversation_resolution or src
    if (protection.get("required_signatures") or {}).get("enabled"):
        req.opaque.append(f"{OPAQUE_RULES['required_signatures']} ({src})")
    if (protection.get("lock_branch") or {}).get("enabled"):
        req.opaque.append(f"{OPAQUE_RULES['lock_branch']} ({src})")


def from_rules(rules: list[dict], req: Requirements) -> None:
    """GET /repos/{r}/rules/branches/{b}: the active rules from every ruleset that targets the branch."""
    for rule in rules or []:
        kind = rule.get("type")
        params = rule.get("parameters") or {}
        src = f"ruleset {rule['ruleset_id']}" if rule.get("ruleset_id") is not None else "ruleset"
        if kind == "pull_request":
            req.add_reviews(params.get("required_approving_review_count", 0), params.get("require_code_owner_review"),
                            params.get("dismiss_stale_reviews_on_push"), params.get("require_last_push_approval"), src)
            if params.get("required_review_thread_resolution"):
                req.conversation_resolution = req.conversation_resolution or src
        elif kind == "required_status_checks":
            req.strict_checks |= bool(params.get("strict_required_status_checks_policy"))
            for c in params.get("required_status_checks") or []:
                req.checks.append(RequiredCheck(c["context"], c.get("integration_id"), src))
        elif kind in OPAQUE_RULES:
            req.opaque.append(f"{OPAQUE_RULES[kind]} ({src})")
        # creation, deletion, non_fast_forward, required_linear_history, commit message patterns...:
        # these restrict pushes or the merge method, not whether a PR may merge.


@dataclass
class Context:
    """One check run or commit status on the PR head."""

    name: str
    state: str  # SUCCESS, FAILURE, PENDING, ... (check runs: conclusion, or status while running)
    required: bool
    app_slug: str | None = None
    app_id: int | None = None
    creator: str | None = None

    @property
    def passing(self) -> bool:
        return self.state in PASSING_CONCLUSIONS


def contexts(pr: dict) -> tuple[list[Context], bool]:
    """(contexts on the head commit, whether GitHub returned all of them)."""
    out: list[Context] = []
    nodes = (pr.get("commits") or {}).get("nodes") or []
    rollup = (nodes[0]["commit"].get("statusCheckRollup") if nodes else None) or {}
    page = rollup.get("contexts") or {}
    for n in page.get("nodes") or []:
        if n.get("__typename") == "CheckRun":
            app = (n.get("checkSuite") or {}).get("app") or {}
            state = n.get("conclusion") or n.get("status") or "PENDING"
            out.append(Context(n["name"], state, bool(n.get("isRequired")),
                               app_slug=app.get("slug"), app_id=app.get("databaseId")))
        elif n.get("__typename") == "StatusContext":
            out.append(Context(n["context"], n.get("state") or "PENDING", bool(n.get("isRequired")),
                               creator=(n.get("creator") or {}).get("login")))
    return out, not (page.get("pageInfo") or {}).get("hasNextPage")


def _who(rr: dict) -> str:
    r = rr.get("requestedReviewer") or {}
    return "@" + (r.get("login") or r.get("slug") or "?")


def evaluate(pr: dict, req: Requirements, is_own: Callable[[str, Context | None, int | None], bool]) -> list[str]:
    """Reasons GitHub's rules would refuse the merge; [] if they'd allow it.

    `is_own(name, context, app_id)` says whether a check is ferry's own (it can't pass before ferry
    applies, so it's ignored rather than deadlocking the apply).
    """
    reasons: list[str] = []
    explained = False  # found a cause that accounts for mergeStateStatus BLOCKED
    own_pending = False

    # Reviews: GitHub's own decision covers required count, CODEOWNERS, stale dismissal, last push.
    decision = pr.get("reviewDecision")
    if decision == "CHANGES_REQUESTED":
        reasons.append("GitHub: a reviewer requested changes")
        explained = True
    elif req.reviews_required and decision != "APPROVED":
        need = []
        if req.review_count:
            need.append(f"{req.review_count} approving review(s)")
        if req.code_owners:
            owners = [_who(r) for r in (pr.get("reviewRequests") or {}).get("nodes") or [] if r.get("asCodeOwner")]
            need.append("code owner review" + (f" (waiting on {', '.join(owners)})" if owners else ""))
        if req.last_push_approval:
            need.append("approval of the most recent push by someone other than its pusher")
        stale = "; approvals of earlier commits were dismissed" if req.dismiss_stale else ""
        reasons.append(f"GitHub ({', '.join(sorted(set(req.review_sources)))}) requires {' and '.join(need)}"
                       f"{stale}; review decision is {decision or 'none'}")
        explained = True
    elif not req.reviews_required and decision in ("REVIEW_REQUIRED",):
        # GitHub requires reviews through something ferry couldn't read: trust GitHub.
        reasons.append("GitHub requires more reviews (review decision REVIEW_REQUIRED)")
        explained = True

    # Required checks, other than ferry's own.
    ctxs, complete = contexts(pr)
    by_name: dict[str, list[Context]] = {}
    for c in ctxs:
        by_name.setdefault(c.name, []).append(c)
    required: dict[str, tuple[int | None, str]] = {}
    for rc in req.checks:
        required.setdefault(rc.name, (rc.app_id if rc.app_id not in (None, -1) else None, rc.source))
    for c in ctxs:
        if c.required:
            required.setdefault(c.name, (None, "required by GitHub"))
    for name, (app_id, source) in sorted(required.items()):
        candidates = [c for c in by_name.get(name, []) if app_id is None or c.app_id == app_id]
        if is_own(name, candidates[0] if candidates else None, app_id):
            if not any(c.passing for c in candidates):
                own_pending = True
            continue
        if any(c.passing for c in candidates):
            continue
        explained = True
        if not candidates:
            if not complete:
                reasons.append(f"GitHub ({source}) requires check `{name}`; the PR has more checks than ferry "
                               "reads (100), so ferry can't confirm it")
            else:
                reasons.append(f"GitHub ({source}) requires check `{name}`, which hasn't reported on the head commit")
        else:
            reasons.append(f"GitHub ({source}) requires check `{name}`, which is {candidates[0].state.lower()}")

    # Conversation resolution.
    if req.conversation_resolution:
        threads = pr.get("reviewThreads") or {}
        nodes = threads.get("nodes") or []
        open_threads = sum(1 for t in nodes if not t.get("isResolved"))
        if open_threads:
            reasons.append(f"GitHub ({req.conversation_resolution}) requires conversations to be resolved; "
                           f"{open_threads} unresolved")
            explained = True
        elif threads.get("totalCount", 0) > len(nodes):
            reasons.append(f"GitHub ({req.conversation_resolution}) requires conversations to be resolved; the PR has "
                           f"more than {len(nodes)} threads, so ferry can't confirm they are")
            explained = True

    # GitHub's overall verdict. BEHIND/DIRTY/DRAFT/UNKNOWN are reported from the REST mergeable_state.
    if pr.get("mergeStateStatus") == "BLOCKED" and not explained:
        if req.opaque:
            reasons.append("GitHub reports the PR as blocked, and ferry can't check these rules: " + "; ".join(req.opaque))
        elif not own_pending:
            reasons.append("GitHub reports the PR as blocked by a rule ferry can't identify; check the PR's merge box")
        # else: the only thing ferry can see blocking the merge is ferry's own required check.
    return reasons
