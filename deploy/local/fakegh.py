"""A stand-in for api.github.com: just enough for ferry serve and one PR (number 1).

State lives in memory; /_state shows it, /_approve adds an approval of the PR head, /_pr_state
{"state": "closed"|"open"} closes or reopens the PR, /_pr {"base": NAME} retargets it and /_pr {"behind": N} makes it N commits behind its base. The branch has one ruleset (one approving review);
POST /_rules {"rules": [...]} replaces its rules (GitHub's /rules/branches shape), e.g. to require
two approvals and watch apply refuse. Commit statuses ferry posts (#29) are kept per head and context
and appear in GraphQL the way GitHub reports an App's status (creator: Bot, login = the App's slug). Run with the ferry image's Python.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.environ.get("LAB_REPO", "acme/ansible")
R = f"/repos/{REPO}"
HEAD_FILE = os.environ.get("LAB_HEAD_FILE", "/lab/pr_head")
WRITERS = set(os.environ.get("LAB_WRITERS", "james,pip,carol").split(","))
# Like a fresh repo with one ruleset: one approving review on main.
state = {"comments": [], "reviews": [], "statuses": {}, "pr_state": "open", "base": "main", "behind": 0,
         "rules": [{"type": "pull_request", "ruleset_id": 1,
                    "parameters": {"required_approving_review_count": 1}}]}


def head():
    return open(HEAD_FILE).read().strip()


def approvals():
    latest = {}
    for rv in state["reviews"]:
        latest[rv["user"]["login"]] = rv
    return sum(1 for rv in latest.values() if rv["state"] == "APPROVED" and rv["commit_id"] == head())


def required_checks():
    return {c["context"] for r in state["rules"] if r["type"] == "required_status_checks"
            for c in r["parameters"].get("required_status_checks", [])}


def gql_pr():
    """GitHub's verdict, computed from the rules like GitHub would (reviews and commit statuses)."""
    need = max([r["parameters"].get("required_approving_review_count", 0)
                for r in state["rules"] if r["type"] == "pull_request"] or [0])
    decision = None if not need else ("APPROVED" if approvals() >= need else "REVIEW_REQUIRED")
    req, sts = required_checks(), state["statuses"].get(head(), {})
    nodes = [{"__typename": "StatusContext", "context": name, "state": st["state"].upper(),
              "isRequired": name in req, "creator": {"__typename": "Bot", "login": "ferry-lab"}}
             for name, st in sts.items()]
    checks_ok = all(sts.get(name, {}).get("state") == "success" for name in req)
    rollup = {"contexts": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}} if nodes else None
    return {"headRefOid": head(), "reviewDecision": decision,
            "mergeStateStatus": "BLOCKED" if decision == "REVIEW_REQUIRED" or not checks_ok else "CLEAN",
            "reviewRequests": {"nodes": []}, "reviewThreads": {"totalCount": 0, "nodes": []},
            "commits": {"nodes": [{"commit": {"oid": head(), "statusCheckRollup": rollup}}]}}


def pr():
    return {"number": 1, "head": {"sha": head(), "ref": "feature", "repo": {"full_name": REPO}},
            "base": {"ref": state["base"]}, "user": {"login": "pip"}, "state": state["pr_state"], "merged": False,
            "draft": False, "mergeable": True, "mergeable_state": "clean", "html_url": "http://lab/pull/1"}


class H(BaseHTTPRequestHandler):
    def _j(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        first = "page=" not in self.path or "page=1" in self.path
        if p == "/app":
            return self._j(200, {"slug": "ferry-lab"})
        if p == f"{R}/installation":
            return self._j(200, {"id": 1})
        if p == f"{R}/pulls/1":
            return self._j(200, pr())
        if p == f"{R}/pulls/1/files":
            return self._j(200, [{"filename": "inventories/lab/host_vars/target.yml"}] if first else [])
        if p == f"{R}/issues/1/comments":
            return self._j(200, state["comments"] if first else [])
        if p == f"{R}/pulls/1/commits":  # who GitHub links the PR's commits to (#25)
            return self._j(200, [{"sha": head(), "author": {"login": "pip"}, "committer": {"login": "pip"}}]
                           if first else [])
        if p == f"{R}/pulls/1/reviews":
            return self._j(200, state["reviews"] if first else [])
        if p.startswith(f"{R}/collaborators/"):
            user = p.split("/")[-2]
            return self._j(200, {"role_name": "write" if user in WRITERS else "read"})
        if p == f"{R}/rules/branches/main":
            return self._j(200, state["rules"] if first else [])
        if p == f"{R}/branches/main":
            return self._j(200, {"name": "main", "protected": bool(state["rules"])})
        if p == f"{R}/branches/main/protection":
            return self._j(404, {"message": "Branch not protected"})  # rulesets only
        if p.startswith(f"{R}/compare/"):
            return self._j(200, {"status": "diverged" if state["behind"] else "ahead",
                                 "behind_by": state["behind"], "ahead_by": 1})
        if p == "/_state":
            return self._j(200, state)
        self._j(404, {"message": "Not Found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        d = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/app/installations/1/access_tokens":
            return self._j(201, {"token": "ghs_lab", "expires_at": "2099-01-01T00:00:00Z"})
        if self.path == f"{R}/issues/1/comments":
            c = {"id": len(state["comments"]) + 1, "user": {"login": "ferry-lab[bot]"}, "body": d["body"],
                 "created_at": "now"}
            state["comments"].append(c)
            return self._j(201, c)
        if self.path.startswith(f"{R}/statuses/"):  # #29: the latest state per context wins
            sha = self.path.rsplit("/", 1)[-1]
            state["statuses"].setdefault(sha, {})[d["context"]] = {
                "state": d["state"], "description": d.get("description", "")}
            return self._j(201, {"context": d["context"], "state": d["state"]})
        if self.path == "/graphql":
            return self._j(200, {"data": {"repository": {"pullRequest": gql_pr()}}})
        if self.path == "/_rules":
            state["rules"] = d.get("rules", [])
            return self._j(200, {"rules": state["rules"]})
        if self.path == "/_pr_state":  # {"state": "closed"} before a close webhook, "open" to reopen
            state["pr_state"] = d.get("state", "open")
            return self._j(200, {"state": state["pr_state"]})
        if self.path == "/_pr":  # {"base": "feature-x"} retargets the PR, {"behind": 2} makes it stale
            for k in ("base", "behind"):
                if k in d:
                    state[k] = d[k]
            return self._j(200, {k: state[k] for k in ("base", "behind")})
        if self.path == "/_approve":
            state["reviews"].append({"user": {"login": d.get("user", "james")}, "state": "APPROVED",
                                     "commit_id": head()})
            return self._j(200, {"approved": head()})
        self._j(404, {"message": "Not Found"})

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
