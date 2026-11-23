"""A stand-in for api.github.com: just enough for ferry serve and one PR (number 1).

State lives in memory; /_state shows it, /_approve adds an approval of the PR head, /_head sets the
PR head SHA (after you push to the PR branch). Run with the ferry image's Python.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.environ.get("LAB_REPO", "acme/ansible")
R = f"/repos/{REPO}"
HEAD_FILE = os.environ.get("LAB_HEAD_FILE", "/lab/pr_head")
WRITERS = set(os.environ.get("LAB_WRITERS", "james,pip").split(","))
state = {"comments": [], "reviews": []}


def head():
    return open(HEAD_FILE).read().strip()


def pr():
    return {"number": 1, "head": {"sha": head(), "ref": "feature", "repo": {"full_name": REPO}},
            "base": {"ref": "main"}, "user": {"login": "pip"}, "state": "open", "merged": False,
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
        if p == f"{R}/pulls/1/reviews":
            return self._j(200, state["reviews"] if first else [])
        if p.startswith(f"{R}/collaborators/"):
            user = p.split("/")[-2]
            return self._j(200, {"role_name": "write" if user in WRITERS else "read"})
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
        if self.path == "/_approve":
            state["reviews"].append({"user": {"login": d.get("user", "james")}, "state": "APPROVED",
                                     "commit_id": head()})
            return self._j(200, {"approved": head()})
        self._j(404, {"message": "Not Found"})

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
