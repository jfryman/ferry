"""Comment commands (`/ferry plan`, `/ferry apply`) via a GitLab note webhook.

GitLab CI can't run a job when someone comments, so comment-driven applies need a small listener.
This module is that listener's core, kept separate from HTTP so it's unit-testable and portable:

  parse_note_event(payload, bot)  -> Command | None
  trigger_pipeline(client, project, trigger_token, cmd)  -> pipeline id

The trigger starts a pipeline on the MR's source branch with FERRY_ACTION/FERRY_MR_IID/FERRY_USER;
the config repo's CI runs `ferry <action>` in it, and the engine performs every check (head SHA,
approvals, lock, Developer access of FERRY_USER) exactly as for the manual job. `serve()` is a minimal
stdlib server for trying this out; a production service would sit behind the cluster ingress.
"""

from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

from .vcs.gitlab import GitLabClient

# /ferry plan|apply [inventory] [--sha COMMIT]: group 1 the action, 2 the inventory, 3 the commit.
COMMAND_RE = re.compile(r"^\s*/(?:ferry\s+)?(plan|apply)(?:\s+(?:-i\s+|--inventory[ =])?([A-Za-z0-9_.-]+))?"
                        r"(?:\s+--sha[ =]([0-9a-fA-F]{7,40}))?\s*$")
# /ferry unlock [inventory] (#30): group 1 the inventory.
UNLOCK_RE = re.compile(r"^\s*/ferry\s+unlock(?:\s+(?:-i\s+|--inventory[ =])?([A-Za-z0-9_.-]+))?\s*$")
HELP_RE = re.compile(r"^\s*/ferry(?:\s+(?:help|--help|-h))\s*$")
FERRY_PREFIX_RE = re.compile(r"^/ferry(?:\s|$)")


def classify_command(text: str):
    """What a comment asks ferry for, from its first line (#30): ("run", m) for plan/apply (the
    COMMAND_RE match), ("unlock", m), ("help", None), ("bad", None) for a comment that starts with
    `/ferry` but doesn't parse, or None for any other comment (ferry stays silent on those)."""
    text = (text or "").strip()
    if not FERRY_PREFIX_RE.match(text):
        return None
    first = text.splitlines()[0]
    for kind, rx in (("run", COMMAND_RE), ("unlock", UNLOCK_RE), ("help", HELP_RE)):
        m = rx.match(first)
        if m:
            return kind, (m if kind != "help" else None)
    return "bad", None


@dataclass
class Command:
    action: str
    mr_iid: int
    source_branch: str
    user: str
    inventory: str | None = None


def parse_note_event(payload: dict, bot_username: str) -> Command | None:
    if payload.get("object_kind") != "note":
        return None
    attrs = payload.get("object_attributes") or {}
    if attrs.get("noteable_type") != "MergeRequest":
        return None
    user = (payload.get("user") or {}).get("username", "")
    if not user or user == bot_username:
        return None
    first = (attrs.get("note") or "").strip().splitlines()[:1]
    if not first:
        return None
    m = COMMAND_RE.match(first[0])
    if not m:
        return None
    mr = payload.get("merge_request") or {}
    if mr.get("state") != "opened":
        return None
    if mr.get("source_project_id") != mr.get("target_project_id"):
        return None  # fork MRs never get the inventory's credentials
    return Command(m.group(1), int(mr["iid"]), mr["source_branch"], user, m.group(2))


def verify_token(header_value: str | None, expected: str) -> bool:
    return bool(header_value) and hmac.compare_digest(header_value, expected)


def trigger_pipeline(client: GitLabClient, project: str, trigger_token: str, cmd: Command) -> int:
    variables = {"FERRY_ACTION": cmd.action, "FERRY_MR_IID": str(cmd.mr_iid), "FERRY_USER": cmd.user}
    if cmd.inventory:
        variables["FERRY_INVENTORY"] = cmd.inventory
    data = {"token": trigger_token, "ref": cmd.source_branch, "variables": variables}
    from urllib.parse import quote

    res = client.request("POST", f"/projects/{quote(str(project), safe='')}/trigger/pipeline", data=data)
    return int(res["id"])


def serve(host: str, port: int, secret: str, client: GitLabClient, project: str, trigger_token: str,
          bot_username: str) -> None:  # pragma: no cover - thin HTTP shell
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if not verify_token(self.headers.get("X-Gitlab-Token"), secret):
                self.send_response(401)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length))
            except ValueError:
                self.send_response(400)
                self.end_headers()
                return
            cmd = parse_note_event(payload, bot_username)
            if cmd is not None:
                trigger_pipeline(client, project, trigger_token, cmd)
            self.send_response(202 if cmd else 204)
            self.end_headers()

    HTTPServer((host, port), Handler).serve_forever()
