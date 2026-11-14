"""Command line / GitLab CI driver.

  ferry plan    [--inventory NAME]       MR pipeline: check+diff the affected hosts, comment the plan
  ferry apply   [--inventory NAME]       MR pipeline (manual job): apply if the plan is current
  ferry drift   [--inventory NAME]       main (scheduled): check mode on every host, write metrics
  ferry unlock  [--commit SHA]           main: release locks held by the MR(s) that merged SHA
  ferry scope   FILE...                  print the --limit a set of changed files maps to
  ferry policy                           run the static policy checks

GitLab context comes from the standard CI variables (CI_API_V4_URL, CI_PROJECT_ID,
CI_MERGE_REQUEST_IID, CI_COMMIT_SHA, GITLAB_USER_LOGIN, CI_JOB_URL) plus FERRY_GITLAB_TOKEN.
Flags override them, so the same commands work from a laptop or a service.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import config as config_mod
from .engine import Engine
from .locks import GitLabTagLocks
from .runner import AnsibleRunner
from .vcs.gitlab import GitLabBackend, GitLabClient


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v else default


def build_engine(args, need_vcs: bool = True) -> Engine:
    repo = Path(args.repo).resolve()
    cfg = config_mod.load(repo / args.config)
    runner = AnsibleRunner(repo, cfg.guard.busy_marker)
    vcs = locks = None
    if need_vcs:
        api = args.api_url or _env("CI_API_V4_URL")
        project = args.project or _env("CI_PROJECT_ID")
        token = _env("FERRY_GITLAB_TOKEN")
        missing = [n for n, v in (("CI_API_V4_URL/--api-url", api), ("CI_PROJECT_ID/--project", project),
                                  ("FERRY_GITLAB_TOKEN", token)) if not v]
        if missing:
            sys.exit(f"ferry: missing {', '.join(missing)}")
        client = GitLabClient(api, token)
        vcs = GitLabBackend(client, project)
        locks = GitLabTagLocks(client, project)
    return Engine(cfg, repo, runner, vcs, locks, job_url=_env("CI_JOB_URL", "") or "")


def _mr_args(args) -> tuple[int, str, str]:
    mr = args.mr or _env("CI_MERGE_REQUEST_IID") or _env("FERRY_MR_IID")
    sha = args.sha or _env("CI_COMMIT_SHA")
    # Triggered pipelines (comment webhook) run as the trigger owner; the commenter is FERRY_USER.
    # Only trust FERRY_USER on trigger pipelines, so a manual job can't claim to be someone else.
    if _env("CI_PIPELINE_SOURCE") == "trigger":
        user = args.user or _env("FERRY_USER") or "unknown"
    else:
        user = args.user or _env("GITLAB_USER_LOGIN") or "unknown"
    if not mr or not sha:
        sys.exit("ferry: needs an MR (--mr or CI_MERGE_REQUEST_IID) and a commit (--sha or CI_COMMIT_SHA)")
    return int(mr), sha, user


def _write_summary(path: str | None, outcome) -> None:
    if not path:
        return
    data = {"ok": outcome.ok, "action": outcome.action, "reasons": outcome.reasons, **outcome.extra}
    if outcome.run is not None:
        data["hosts"] = {n: h.state for n, h in outcome.run.hosts.items()}
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ferry")
    p.add_argument("--repo", default=".", help="config repo checkout (default: cwd)")
    p.add_argument("--config", default="ferry.yaml")
    p.add_argument("--api-url")
    p.add_argument("--project")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("plan", "apply"):
        s = sub.add_parser(name)
        s.add_argument("--inventory")
        s.add_argument("--mr", type=int)
        s.add_argument("--sha")
        s.add_argument("--user")
        s.add_argument("--summary", help="write a JSON summary here (CI artifact)")

    s = sub.add_parser("drift")
    s.add_argument("--inventory")
    s.add_argument("--sha")
    s.add_argument("--no-report", action="store_true", help="don't write textfiles on the hosts")
    s.add_argument("--out", default="ferry-drift", help="directory for drift.json / drift.prom")

    s = sub.add_parser("unlock")
    s.add_argument("--inventory")
    s.add_argument("--commit")

    s = sub.add_parser("scope")
    s.add_argument("--inventory")
    s.add_argument("files", nargs="+")

    sub.add_parser("policy")

    args = p.parse_args(argv)

    if args.cmd in ("scope", "policy"):
        eng = build_engine(args, need_vcs=False)
        if args.cmd == "policy":
            problems = eng.policy_check()
            for prob in problems:
                print(prob)
            print(f"ferry policy: {len(problems)} problem(s)")
            return 1 if problems else 0
        sc = eng.scope_for(args.files, args.inventory)
        for f, why in sc.reasons:
            print(f"{f}: {why}")
        print(f"limit: {sc.limit if not sc.empty else '(none: nothing to run)'}")
        return 0

    if args.cmd == "drift":
        eng = build_engine(args, need_vcs=False)
        sha = args.sha or _env("CI_COMMIT_SHA") or "unknown"
        out = eng.drift(sha, args.inventory, write_report=not args.no_report, out_dir=args.out)
        print(json.dumps(out.extra, indent=2, sort_keys=True))
        return 0 if out.ok else 1

    eng = build_engine(args)
    if args.cmd == "unlock":
        sha = args.commit or _env("CI_COMMIT_SHA")
        released = eng.unlock_for_commit(sha, args.inventory)
        print(f"ferry unlock: released locks held by {['!%d' % r for r in released] or 'nobody'}")
        return 0

    mr, sha, user = _mr_args(args)
    if args.cmd == "plan":
        out = eng.plan(mr, sha, user, args.inventory)
    else:
        out = eng.apply(mr, sha, user, args.inventory)
    _write_summary(args.summary, out)
    for r in out.reasons:
        print(f"ferry {args.cmd}: {r}", file=sys.stderr)
    print(f"ferry {args.cmd}: {'ok' if out.ok else 'FAILED'}")
    return 0 if out.ok else 1


if __name__ == "__main__":
    sys.exit(main())
