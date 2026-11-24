"""Command line / GitLab CI driver.

  ferry plan    [--inventory NAME]       MR pipeline: check+diff the affected hosts, comment the plan
  ferry apply   [--inventory NAME]       MR pipeline (manual job): apply if the plan is current
  ferry drift   [--inventory NAME]       main (scheduled): check mode on every host, write metrics
  ferry unlock  [--commit SHA]           main: release locks held by the MR(s) that merged SHA
  ferry scope   FILE...                  print the --limit a set of changed files maps to
  ferry policy                           run the static policy checks
  ferry serve                            webhook service, GitHub App or GitLab (see `ferry serve --help`)

GitLab context comes from the standard CI variables (CI_API_V4_URL, CI_PROJECT_ID,
CI_MERGE_REQUEST_IID, CI_COMMIT_SHA, GITLAB_USER_LOGIN, CI_JOB_URL) plus FERRY_GITLAB_TOKEN.

GitHub (--vcs github or FERRY_VCS=github): FERRY_GITHUB_TOKEN, the repo as OWNER/NAME
(--project, FERRY_GITHUB_REPO or GITHUB_REPOSITORY), FERRY_GITHUB_BOT (the App's "<slug>[bot]"
login, needed with an App installation token), FERRY_LOCK_DB (the service's lock file, kept
on persistent storage) and optionally GITHUB_API_URL. The PR, commit and
user come from --mr/--sha/--user (or FERRY_MR_IID). Flags override the environment, so the same
commands work from a laptop or a service.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path

from . import config as config_mod
from .engine import Engine
from .locks import GitLabTagLocks, SQLiteLocks
from .state import SQLiteState
from .runner import AnsibleRunner
from .vcs.github import API as GITHUB_API
from .vcs.github import GitHubBackend, GitHubClient
from .vcs.gitlab import GitLabBackend, GitLabClient


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v else default


def _vcs_kind(args) -> str:
    kind = (getattr(args, "vcs", None) or _env("FERRY_VCS", "gitlab")).lower()
    if kind not in ("gitlab", "github"):
        sys.exit(f"ferry: unknown VCS {kind!r} (gitlab or github)")
    return kind


def build_engine(args, need_vcs: bool = True) -> Engine:
    repo = Path(args.repo).resolve()
    cfg = config_mod.load(repo / args.config)
    runner = AnsibleRunner(repo, cfg.guard.busy_marker)
    vcs = locks = None
    if need_vcs and _vcs_kind(args) == "github":
        repo_name = args.project or _env("FERRY_GITHUB_REPO") or _env("GITHUB_REPOSITORY")
        token = _env("FERRY_GITHUB_TOKEN")
        lock_db = getattr(args, "lock_db", None) or _env("FERRY_LOCK_DB")
        missing = [n for n, v in (("FERRY_GITHUB_REPO/--project", repo_name), ("FERRY_GITHUB_TOKEN", token),
                                  ("FERRY_LOCK_DB/--lock-db", lock_db)) if not v]
        if missing:
            sys.exit(f"ferry: missing {', '.join(missing)}")
        client = GitHubClient(token, api_url=args.api_url or _env("GITHUB_API_URL", GITHUB_API))
        vcs = GitHubBackend(client, repo_name, bot_login=_env("FERRY_GITHUB_BOT"),
                            own_checks=(_env("FERRY_GITHUB_OWN_CHECKS") or "").split(","))
        locks = SQLiteLocks(lock_db)
    elif need_vcs:
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
    return Engine(cfg, repo, runner, vcs, locks, job_url=_env("CI_JOB_URL", "") or "",
                  default_branch=_env("FERRY_DEFAULT_BRANCH") or _env("CI_DEFAULT_BRANCH"))


def _mr_args(args) -> tuple[int, str, str]:
    mr = args.mr or _env("CI_MERGE_REQUEST_IID") or _env("FERRY_MR_IID")
    sha = args.sha or _env("CI_COMMIT_SHA")
    # Triggered pipelines (comment webhook) run as the trigger owner; the commenter is FERRY_USER.
    # Only trust FERRY_USER on trigger pipelines, so a manual job can't claim to be someone else.
    if _vcs_kind(args) == "github":
        # No CI variables to trust here: the service (or a person at a terminal) passes --user.
        user = args.user or "unknown"
    elif _env("CI_PIPELINE_SOURCE") == "trigger":
        user = args.user or _env("FERRY_USER") or "unknown"
    else:
        user = args.user or _env("GITLAB_USER_LOGIN") or "unknown"
    if not mr or not sha:
        sys.exit("ferry: needs an MR/PR (--mr, CI_MERGE_REQUEST_IID or FERRY_MR_IID) and a commit (--sha or CI_COMMIT_SHA)")
    return int(mr), sha, user


def _write_summary(path: str | None, outcome) -> None:
    if not path:
        return
    data = {"ok": outcome.ok, "action": outcome.action, "reasons": outcome.reasons, **outcome.extra}
    if outcome.run is not None:
        data["hosts"] = {n: h.state for n, h in outcome.run.hosts.items()}
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True, default=str))


SERVE_HELP = """Run ferry as an Atlantis-style service. Settings come from the environment:

  FERRY_VCS                      github or gitlab (or --vcs); unset: gitlab if FERRY_GITLAB_URL is set
  FERRY_WEBHOOK_SECRET_FILE      file holding the webhook secret (or FERRY_WEBHOOK_SECRET)
  FERRY_LOCK_DB                  state file on persistent storage (required): locks, seen deliveries,
                                 PR head arrival times, job history
  FERRY_JOB_HISTORY              200 (default): finished jobs kept for GET /jobs
  FERRY_WORKDIR                  checkouts and caches (default /var/lib/ferry/work)
  FERRY_DEFAULT_BRANCH           GitHub: default main; GitLab: default the project's default branch
  FERRY_LISTEN                   host:port, default 0.0.0.0:8080 (POST /webhook; GET /healthz, /metrics,
                                 /jobs: unauthenticated, no secrets; keep them inside the cluster)
  FERRY_SSH_USER                 remote user for ansible (e.g. root)
  FERRY_DRIFT_INTERVAL_HOURS     0 (default) disables scheduled drift runs
  FERRY_STARTUP_ATTEMPTS         8 (default): tries to reach the code host at start, with backoff
  FERRY_WEBHOOK_MAX_AGE_MINUTES  10 (default): refuse events whose own timestamps are older (replays)
  FERRY_MAX_BODY_BYTES           1048576 (default): larger webhook bodies get 413

  GitHub App:
  FERRY_GITHUB_REPO              OWNER/NAME of the config repo (required)
  FERRY_GITHUB_APP_ID            the App's id (required)
  FERRY_GITHUB_APP_KEY_FILE      the App's private key, PEM (required)
  FERRY_GITHUB_INSTALLATION_ID   optional; looked up from the repo if unset
  GITHUB_API_URL / FERRY_GITHUB_GIT_URL   for GitHub Enterprise

  GitLab (FERRY_VCS=gitlab):
  FERRY_GITLAB_URL               e.g. https://gitlab.example.com (required)
  FERRY_GITLAB_PROJECT           GROUP/NAME or numeric id of the config project (required)
  FERRY_GITLAB_TOKEN_FILE        project access token, Developer, scopes api + read_repository
                                 (or FERRY_GITLAB_TOKEN); the file is re-read, so it can be rotated
  FERRY_GITLAB_GIT_URL           optional; default the project's http_url_to_repo

  OpenBao (optional; per-job token, SSH certificate and vault password):
  FERRY_OPENBAO_ADDR, FERRY_OPENBAO_JWT_FILE (workload identity), FERRY_OPENBAO_CA_CERT,
  FERRY_OPENBAO_AUTH_MOUNT (jwt-nomad), FERRY_OPENBAO_AUTH_ROLE (ferry),
  FERRY_OPENBAO_SSH_MOUNT (ssh-client-signer), FERRY_OPENBAO_SSH_ROLE (ferry),
  FERRY_OPENBAO_VAULT_PASSWORD_PATH (kv-v2 data path) and _FIELD (password),
  FERRY_OPENBAO_SSH_TTL (e.g. 20m; default: the role's ttl), FERRY_OPENBAO_SSH_PRINCIPAL
  (valid_principals to request; default: the role's default_user)
  FERRY_GALAXY                   install (default) or skip: ansible-galaxy on the change's
                                 requirements.yml (in the executor when there is one); skip for
                                 images that ship their collections (requirements.yml isn't read)
  FERRY_GALAXY_SERVER            pin the Galaxy server; requirements naming any other source fail
  FERRY_GALAXY_TIMEOUT_SECONDS   300 (default): kill ansible-galaxy (and what it started) after this
  FERRY_COLLECTIONS_MAX_AGE_DAYS 14 (default): prune collection caches unused this long (0: never)
  FERRY_GIT_TIMEOUT_SECONDS      300 (default): any one git fetch/checkout
  FERRY_COMMIT_STATUS            1: post `ferry/plan: <inv>` / `ferry/apply: <inv>` commit statuses on
                                 the PR head (GitHub App: "Commit statuses: write"), so merging can
                                 require an applied change; ferry's own never blocks its apply
  FERRY_ALLOW_COMMANDS           plan,apply (default): comment commands this ferry takes; `plan`
                                 alone makes it plan-only
  FERRY_DISABLE_AUTOPLAN         1: plan only on `/ferry plan`, never on push
  FERRY_SWITCH_DIR               where `disable-apply` / `disable-autoplan` switch files live
                                 (default: FERRY_LOCK_DB's directory); see `ferry switch --help`
  FERRY_JOB_TIMEOUT_MINUTES      kill a plan/apply/drift run after this long (0: no limit); keep it
                                 below the certificate TTL
  FERRY_EXECUTOR_SOCKET          run playbooks in `ferry executor` (a separate user/container that
                                 can't read this service's secrets) instead of as this process
  FERRY_OPENBAO_PLAYBOOK_TOKEN   1: give playbooks the job's OpenBao token (VAULT_TOKEN) for lookups.
                                 Default off: the token is revoked before any playbook runs
  Kubernetes: OpenBao's kubernetes (or jwt) auth takes the same login, so set
  FERRY_OPENBAO_AUTH_MOUNT=kubernetes and FERRY_OPENBAO_JWT_FILE to the service account token
  (/var/run/secrets/kubernetes.io/serviceaccount/token, or a projected token with an audience).
"""


def _openbao():
    from .credentials import OpenBao, OpenBaoSettings

    if not _env("FERRY_OPENBAO_ADDR"):
        return None
    return OpenBao(OpenBaoSettings(
        addr=_env("FERRY_OPENBAO_ADDR"),
        jwt_file=_env("FERRY_OPENBAO_JWT_FILE") or sys.exit("ferry serve: FERRY_OPENBAO_JWT_FILE is required with FERRY_OPENBAO_ADDR"),
        auth_mount=_env("FERRY_OPENBAO_AUTH_MOUNT", "jwt-nomad"),
        auth_role=_env("FERRY_OPENBAO_AUTH_ROLE", "ferry"),
        ssh_mount=_env("FERRY_OPENBAO_SSH_MOUNT", "ssh-client-signer"),
        ssh_role=_env("FERRY_OPENBAO_SSH_ROLE", "ferry"),
        ca_cert=_env("FERRY_OPENBAO_CA_CERT"),
        vault_password_path=_env("FERRY_OPENBAO_VAULT_PASSWORD_PATH"),
        vault_password_field=_env("FERRY_OPENBAO_VAULT_PASSWORD_FIELD", "password"),
        ssh_ttl=_env("FERRY_OPENBAO_SSH_TTL"),
        ssh_principal=_env("FERRY_OPENBAO_SSH_PRINCIPAL"),
        playbook_token=_env("FERRY_OPENBAO_PLAYBOOK_TOKEN", "") in ("1", "true", "yes"),
    ))


def _secret_file_or_env(file_var: str, env_var: str):
    """A callable returning the secret (re-reading the file each time), or None if neither is set."""
    if _env(file_var):
        path = Path(_env(file_var))
        return lambda: path.read_text().strip()
    value = _env(env_var)
    return (lambda: value) if value else None


def _transient(e: BaseException) -> bool:
    """A startup failure worth retrying: the code host (or DNS) isn't reachable yet, or it answered 5xx."""
    import urllib.error

    from .vcs.gitlab import GitLabError
    from .vcs.github import GitHubError

    if isinstance(e, (GitHubError, GitLabError)):
        return e.status >= 500
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500
    return isinstance(e, (urllib.error.URLError, ConnectionError, TimeoutError))


def build_service_with_retry(vcs: str | None = None, attempts: int | None = None, sleep=None):
    """build_service, retried with backoff while the code host is unreachable (container DNS or the
    network often lags at start under an orchestrator). Config errors still exit at once."""
    import logging
    import time

    attempts = attempts or int(_env("FERRY_STARTUP_ATTEMPTS", "8"))
    sleep = sleep or time.sleep
    delay = 1.0
    for i in range(1, attempts + 1):
        try:
            return build_service(vcs)
        except Exception as e:  # noqa: BLE001 - filtered by _transient
            if i == attempts or not _transient(e):
                raise
            logging.getLogger("ferry.serve").warning("startup attempt %d/%d failed (%s); retrying in %.0fs",
                                                     i, attempts, e, delay)
            sleep(delay)
            delay = min(delay * 2, 30.0)


def build_service(vcs: str | None = None):
    import logging

    from .service import Settings

    # Unset: GitLab if FERRY_GITLAB_URL is set, else GitHub (the service's original backend).
    kind = (vcs or _env("FERRY_VCS") or ("gitlab" if _env("FERRY_GITLAB_URL") else "github")).lower()
    if kind not in ("gitlab", "github"):
        sys.exit(f"ferry serve: unknown FERRY_VCS {kind!r} (github or gitlab)")
    names = ("FERRY_GITLAB_URL", "FERRY_GITLAB_PROJECT") if kind == "gitlab" else \
        ("FERRY_GITHUB_REPO", "FERRY_GITHUB_APP_ID", "FERRY_GITHUB_APP_KEY_FILE")
    required = {n: _env(n) for n in (*names, "FERRY_LOCK_DB")}
    secret = _secret_file_or_env("FERRY_WEBHOOK_SECRET_FILE", "FERRY_WEBHOOK_SECRET")
    token = _secret_file_or_env("FERRY_GITLAB_TOKEN_FILE", "FERRY_GITLAB_TOKEN") if kind == "gitlab" else True
    missing = [n for n, v in required.items() if not v] + ([] if secret else ["FERRY_WEBHOOK_SECRET(_FILE)"]) \
        + ([] if token else ["FERRY_GITLAB_TOKEN(_FILE)"])
    if missing:
        sys.exit(f"ferry serve: missing {', '.join(missing)}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    common = dict(run_name_claim=_env("FERRY_RUN_NAME_CLAIM", "name"), run_oidc_issuer=_env("FERRY_RUN_OIDC_ISSUER"),
                  run_oidc_audience=_env("FERRY_RUN_OIDC_AUDIENCE"),
                  run_allowed_subjects=tuple(x.strip() for x in (_env("FERRY_RUN_ALLOWED_SUBJECTS") or "").split(",") if x.strip()),
                  run_allowed_groups=tuple(x.strip() for x in (_env("FERRY_RUN_ALLOWED_GROUPS") or "").split(",") if x.strip()),
                  run_lease_seconds=float(_env("FERRY_RUN_LEASE_SECONDS", "300")),
                  run_cert_ttl=_env("FERRY_RUN_CERT_TTL", "15m"), ssh_user=_env("FERRY_SSH_USER"), drift_interval_hours=float(_env("FERRY_DRIFT_INTERVAL_HOURS", "0")),
                  job_timeout_minutes=float(_env("FERRY_JOB_TIMEOUT_MINUTES", "0")),
                  max_event_age_minutes=float(_env("FERRY_WEBHOOK_MAX_AGE_MINUTES", "10")),
                  max_body_bytes=int(_env("FERRY_MAX_BODY_BYTES", str(1024 * 1024))),
                  galaxy=_env("FERRY_GALAXY", "install"), galaxy_server=_env("FERRY_GALAXY_SERVER"),
                  galaxy_timeout_seconds=float(_env("FERRY_GALAXY_TIMEOUT_SECONDS", "300")),
                  collections_max_age_days=float(_env("FERRY_COLLECTIONS_MAX_AGE_DAYS", "14")),
                  executor_socket=_env("FERRY_EXECUTOR_SOCKET"),
                  allow_commands=_allow_commands(), disable_autoplan=_env("FERRY_DISABLE_AUTOPLAN") in ("1", "true", "yes"),
                  switch_dir=_switch_dir(),
                  commit_status=_env("FERRY_COMMIT_STATUS") in ("1", "true", "yes"))
    if common["galaxy"] not in ("install", "skip"):
        sys.exit("ferry serve: FERRY_GALAXY must be install or skip")
    workdir = _env("FERRY_WORKDIR", "/var/lib/ferry/work")
    if kind == "gitlab":
        return _build_gitlab_service(required, secret(), token, common, workdir)

    from .github_app import GitHubApp, InstallationTokens
    from .service import Service
    from .workspace import Workspace

    repo = required["FERRY_GITHUB_REPO"]
    api = _env("GITHUB_API_URL", GITHUB_API)
    app = GitHubApp(required["FERRY_GITHUB_APP_ID"], Path(required["FERRY_GITHUB_APP_KEY_FILE"]).read_bytes(), api)
    installation = int(_env("FERRY_GITHUB_INSTALLATION_ID") or app.installation_id(repo))
    tokens = InstallationTokens(app, installation)
    vcs = GitHubBackend(GitHubClient(tokens, api_url=api), repo, bot_login=app.bot_login(),
                        own_checks=(_env("FERRY_GITHUB_OWN_CHECKS") or "").split(","),
                        app_id=int(required["FERRY_GITHUB_APP_ID"]) if required["FERRY_GITHUB_APP_ID"].isdigit() else None)
    git_url = _env("FERRY_GITHUB_GIT_URL", "https://github.com").rstrip("/")
    settings = Settings(repo=repo, default_branch=_env("FERRY_DEFAULT_BRANCH", "main"), **common)
    return Service(
        settings=settings,
        vcs=vcs,
        locks=SQLiteLocks(required["FERRY_LOCK_DB"]),
        state=SQLiteState(required["FERRY_LOCK_DB"], job_history=int(_env("FERRY_JOB_HISTORY", "200"))),
        workspace=Workspace(workdir, f"{git_url}/{repo}.git", tokens, git_timeout=_git_timeout()),
        secret=secret().encode(),
        bao=_openbao(),
        **_runner_factory(),
    )


def _build_gitlab_service(required: dict, secret: str, token, common: dict, workdir: str):
    from .gitlab_service import GitLabService
    from .service import Settings
    from .workspace import Workspace

    client = GitLabClient(required["FERRY_GITLAB_URL"].rstrip("/") + "/api/v4", token)
    project = required["FERRY_GITLAB_PROJECT"]
    info = client.request("GET", f"/projects/{urllib.parse.quote(project, safe='')}")
    vcs = GitLabBackend(client, info["id"], pipeline_blocks=True, own_status_prefix="ferry/")
    vcs.bot_username()  # fail at startup, not on the first webhook, if the token is wrong
    settings = Settings(repo=info["path_with_namespace"], project_id=str(info["id"]),
                        default_branch=_env("FERRY_DEFAULT_BRANCH") or info.get("default_branch") or "main",
                        **common)
    remote = _env("FERRY_GITLAB_GIT_URL") or info["http_url_to_repo"]
    return GitLabService(
        settings=settings,
        vcs=vcs,
        locks=SQLiteLocks(required["FERRY_LOCK_DB"]),
        state=SQLiteState(required["FERRY_LOCK_DB"], job_history=int(_env("FERRY_JOB_HISTORY", "200"))),
        workspace=Workspace(workdir, remote, token, change_ref="refs/merge-requests/{number}/head",
                            git_user="oauth2", git_timeout=_git_timeout()),
        secret=secret.encode(),
        bao=_openbao(),
        **_runner_factory(),
    )


def _allow_commands() -> tuple:
    from .service import parse_allow_commands
    try:
        return parse_allow_commands(os.environ.get("FERRY_ALLOW_COMMANDS"))
    except ValueError as e:
        sys.exit(f"ferry serve: {e}")


def _switch_dir() -> str | None:
    """FERRY_SWITCH_DIR, else the state file's directory (on the persistent volume)."""
    d = _env("FERRY_SWITCH_DIR")
    if d:
        return d
    db = _env("FERRY_LOCK_DB")
    return str(Path(db).parent) if db else None


SWITCH_HELP = """Switch applies or automatic plans off (or back on) on a running `ferry serve`, without a restart.

  ferry switch apply off --reason "change freeze until Monday"
  ferry switch autoplan off
  ferry switch apply on
  ferry switch                      # show the switch files

Writes or removes `disable-apply` / `disable-autoplan` in FERRY_SWITCH_DIR (default: the directory
of FERRY_LOCK_DB). Run it where the service's state volume is mounted, e.g.
`kubectl exec deploy/ferry -c ferry -- ferry switch apply off --reason "incident"`. The service reads
the files on every request and again before each queued job runs; the reason is shown on the PR.
"""


def run_switch(args) -> int:
    d = _switch_dir()
    if not d:
        sys.exit("ferry switch: set FERRY_SWITCH_DIR or FERRY_LOCK_DB")
    if not args.what:
        for name in ("disable-apply", "disable-autoplan"):
            path = Path(d) / name
            print(f"{name}: {'ON (' + (path.read_text().strip() or 'no reason') + ')' if path.exists() else 'off'}")
        return 0
    if args.state is None:
        sys.exit("ferry switch: say on or off")
    path = Path(d) / f"disable-{args.what}"
    if args.state == "off":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((args.reason or f"{args.what} switched off by an operator") + "\n")
        print(f"{args.what}: switched off ({path})")
    else:
        path.unlink(missing_ok=True)
        print(f"{args.what}: switched on")
    return 0


def _git_timeout() -> float:
    return float(_env("FERRY_GIT_TIMEOUT_SECONDS", "300"))


def _runner_factory() -> dict:
    """With FERRY_EXECUTOR_SOCKET, playbooks run in `ferry executor` (another user/container)."""
    sock = _env("FERRY_EXECUTOR_SOCKET")
    if not sock:
        return {}
    from .executor import ExecutorRunner
    return {"runner_factory": lambda repo, marker, env: ExecutorRunner(sock, repo, marker, env=env)}


EXECUTOR_HELP = """Run the playbooks for `ferry serve`, as a user that can't read the service's secrets.

Start it as a different user than the service, ideally in its own container that mounts none of the
service's secrets, sharing two directories with the service: the work dir (read-only here) and the
socket's directory. Point the service at the same socket with FERRY_EXECUTOR_SOCKET.

  FERRY_EXECUTOR_SOCKET          Unix socket to listen on (required), e.g. /run/ferry/executor.sock
  FERRY_WORKDIR                  the service's work dir: jobs may only run in checkouts under it
                                 (default /var/lib/ferry/work)
  FERRY_EXECUTOR_MUST_NOT_READ   colon-separated paths of service secrets; the executor refuses to
                                 start if it can read any of them (or if it runs as root)
  FERRY_EXECUTOR_COLLECTIONS     writable cache for the changes' collections (ansible-galaxy runs
                                 here, not in the service); default $TMPDIR/ferry-collections

As its container's PID 1 it also kills every other process in the container after each job.
"""


def run_executor() -> int:
    import logging
    import signal

    from .executor import Executor

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sock = _env("FERRY_EXECUTOR_SOCKET") or sys.exit("ferry executor: FERRY_EXECUTOR_SOCKET is required")
    problems = Executor.check_separation((_env("FERRY_EXECUTOR_MUST_NOT_READ") or "").split(":"))
    if problems:
        sys.exit(f"ferry executor: not separated from the service: {'; '.join(problems)}")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # PID 1 has no default SIGTERM action
    Executor(sock, [_env("FERRY_WORKDIR", "/var/lib/ferry/work")],
             ansible_playbook=_env("FERRY_EXECUTOR_ANSIBLE_PLAYBOOK", "ansible-playbook"),
             galaxy=_env("FERRY_EXECUTOR_ANSIBLE_GALAXY", "ansible-galaxy"),
             collections_root=_env("FERRY_EXECUTOR_COLLECTIONS")).serve_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ferry")
    p.add_argument("--repo", default=".", help="config repo checkout (default: cwd)")
    p.add_argument("--config", default="ferry.yaml")
    p.add_argument("--api-url")
    p.add_argument("--project", help="GitLab project id/path, or GitHub OWNER/NAME")
    p.add_argument("--vcs", choices=("gitlab", "github"), help="code host (default: FERRY_VCS or gitlab)")
    p.add_argument("--lock-db", help="GitHub: the service's lock file (default: FERRY_LOCK_DB)")
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
    s = sub.add_parser("run", help="run locally with a service-owned lock and per-run credentials")
    s.add_argument("playbook")
    s.add_argument("-i", "--inventory")
    s.add_argument("--limit")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--service", default=_env("FERRY_SERVICE_URL", "https://ferry.example.com"))
    s.add_argument("--token-command", default=_env("FERRY_RUN_TOKEN_COMMAND",
                   "bao read -field=token identity/oidc/token/ferry"))
    s.add_argument("--insecure-http", action="store_true", help="throwaway lab only: HTTP and no host key checking")

    sub.add_parser("serve", help="webhook service (GitHub App or GitLab)", description=SERVE_HELP,
                   formatter_class=argparse.RawDescriptionHelpFormatter)
    sub.add_parser("executor", help="run serve's playbooks as a separate, unprivileged user",
                   description=EXECUTOR_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = sub.add_parser("switch", help="switch applies or automatic plans off/on on a running service",
                       description=SWITCH_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument("what", nargs="?", choices=("apply", "autoplan"))
    s.add_argument("state", nargs="?", choices=("on", "off"))
    s.add_argument("--reason", help="shown on the PR when a command is refused")

    args = p.parse_args(argv)

    if args.cmd == "run":
        from .operator import run, OperatorError
        try:
            return run(args)
        except OperatorError as e:
            print(f"ferry run: {e}", file=sys.stderr)
            return 1

    if args.cmd == "executor":
        return run_executor()

    if args.cmd == "switch":
        return run_switch(args)

    if args.cmd == "serve":
        from .service import serve
        host, _, port = (_env("FERRY_LISTEN", "0.0.0.0:8080")).rpartition(":")
        serve(build_service_with_retry(args.vcs), host or "0.0.0.0", int(port))
        return 0

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
