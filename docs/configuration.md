# Configuration reference

ferry has two kinds of configuration:

- **`ferry.yaml`**, in the config repository. It describes the repository: inventories, scope,
  apply rules. Under `ferry serve` it is read from the default branch.
- **Environment variables**, on the service, the executor or the CI job. They describe the
  deployment: credentials, paths, limits, switches. Nothing in the repository can change them.

Contents:

- [`ferry.yaml`](#ferryyaml)
- [Environment: `ferry serve`](#ferry-serve)
- [Environment: `ferry executor`](#ferry-executor)
- [Environment: CI driver and CLI](#ci-driver-and-cli)
- [Environment: `ferry run`](#ferry-run)
- [CLI](#cli)
- [HTTP endpoints](#http-endpoints)

## `ferry.yaml`

Every key has a default. A repository with no `ferry.yaml`, a `site.yml` and an `inventory`
directory works. Unknown keys are an error.

A fuller example:

```yaml
playbook: site.yml
inventories:
  - name: staging
    path: inventories/staging
  - name: production
    path: inventories/production
    apply:
      allowed_users: [alice, bob]
      min_approvals: 2
      run_allowed: [operators]
apply:
  require_inventory: true
base_branches: [release]
drift:
  report_pre_role: host_guard
policy:
  check_mode_false_allow: ["roles/probe/tasks/*.yml"]
redact:
  env_vars: [MY_SERVICE_TOKEN]
```

### Top level

| Key | Default | Meaning |
|---|---|---|
| `playbook` | `site.yml` | The playbook that plan, apply and drift run |
| `inventories` | one inventory named `default` at `inventory` | See [Inventories](#inventories). At least one is required. |
| `base_branches` | `[]` | Branches, besides the default branch, that changes may target. Changes into any other branch are never planned or applied. |
| `max_comment_chars` | `60000` | Upper bound on the size of a comment ferry posts |
| `apply`, `scope`, `drift`, `guard`, `policy`, `redact` | | Sections below |

### Inventories

Each entry in `inventories`:

| Key | Meaning |
|---|---|
| `name` | The inventory's name, used in commands, locks and statuses |
| `path` | Directory or file relative to the repository root, passed to `ansible-playbook -i` |
| `apply` | Optional overrides for this inventory. Only `allowed_users`, `min_approvals` and `run_allowed` can be set here. |

### `apply`

| Key | Default | Meaning |
|---|---|---|
| `min_approvals` | `1` | Approvals needed to apply |
| `allow_author_approval` | `false` | If true, the author's own approval counts, and so do approvals from people who pushed to the change. For single-maintainer repositories. |
| `allowed_users` | `[]` | If set, only these users may apply. They still need write access. Empty: anyone with write access. |
| `require_clean_plan` | `true` | Refuse to apply when the plan had failed, unreachable or blocked hosts |
| `max_plan_age_hours` | `24` | A plan older than this must be re-run |
| `require_up_to_date` | `true` | Refuse to apply a change that is behind its base branch |
| `require_inventory` | `false` | With several inventories, refuse a bare apply. Each inventory must be named. |
| `run_allowed` | `[]` | Subjects or groups that may use [`ferry run`](run.md#authorization) on an inventory. Empty refuses all. |

### `scope`

| Key | Default | Meaning |
|---|---|---|
| `ignore` | `*.md`, `docs/**`, `lode/**`, `tests/**`, `.gitlab-ci.yml`, `.gitignore`, `.yamllint*`, `.ansible-lint*`, `ci/**` | Changed files matching these globs never affect hosts |
| `roles_dir` | `roles` | Where roles live |

### `drift`

| Key | Default | Meaning |
|---|---|---|
| `textfile_dir` | `/var/lib/prometheus/node-exporter` | The node_exporter textfile directory on the hosts |
| `textfile_name` | `ansible_drift.prom` | The file ferry writes there |
| `report_pre_role` | empty | A role run on each host before ferry writes the file, for example a busy-host guard. Empty disables it. |
| `become` | `true` | Whether the report play uses `become` |

### `guard`

| Key | Default | Meaning |
|---|---|---|
| `busy_marker` | `FERRY_HOST_BUSY` | A failed task whose message starts with this means the host refused to be touched. See [Busy hosts](concepts.md#busy-hosts). |

### `policy`

| Key | Default | Meaning |
|---|---|---|
| `check_mode_false_allow` | `[]` | Globs of files where `check_mode: false` is allowed |
| `deny_patterns` | `pipe` lookups and queries, and the `reboot` module | Regexes never allowed on any scanned line |
| `scan_globs` | `*.yml`, `*.yaml`, `playbooks/**/*.yml`, `roles/**/*.yml`, `roles/**/*.yaml` | The files the policy scans |

### `redact`

| Key | Default | Meaning |
|---|---|---|
| `patterns` | `[]` | Extra regexes whose matches are masked in anything ferry posts |
| `env_vars` | `[]` | Environment variables whose exact values are masked |
| `sensitive_paths` | `/etc/shadow*`, `/etc/gshadow*`, `*.key`, `*.pem`, `*/.vault-token`, `*/id_rsa*`, `*/id_ed25519*`, `*/ssh_host_*_key` | Diffs of files matching these globs are never shown |

## `ferry serve`

`ferry serve --help` prints most of this list.

### Core

| Variable | Default | Meaning |
|---|---|---|
| `FERRY_VCS` | `gitlab` if `FERRY_GITLAB_URL` is set, else `github` | `github` or `gitlab`. The global `--vcs` flag overrides it. |
| `FERRY_LOCK_DB` | required (the image sets `/var/lib/ferry/state/locks.db`) | The state file. Keep it on persistent storage. |
| `FERRY_WEBHOOK_SECRET_FILE`, `FERRY_WEBHOOK_SECRET` | required | The webhook secret, from a file or the environment |
| `FERRY_WORKDIR` | `/var/lib/ferry/work` | Checkouts and caches |
| `FERRY_LISTEN` | `0.0.0.0:8080` | `host:port` to listen on |
| `FERRY_DEFAULT_BRANCH` | GitHub: `main`. GitLab: the project's default branch. | The branch `ferry.yaml` is read from |
| `FERRY_SSH_USER` | unset | The remote user for Ansible, for example `root` |
| `FERRY_STARTUP_ATTEMPTS` | `8` | Attempts to reach the code host at start, with backoff |
| `FERRY_JOB_HISTORY` | `200` | Finished jobs kept for `GET /jobs` |
| `FERRY_JOB_TIMEOUT_MINUTES` | `0` (no limit) | Kill a plan, apply or drift run after this long |
| `FERRY_DRIFT_INTERVAL_HOURS` | `0` (disabled) | Run drift this often |
| `FERRY_COMMIT_STATUS` | off | `1` posts [commit statuses](serve.md#commit-statuses) |

### GitHub

| Variable | Default | Meaning |
|---|---|---|
| `FERRY_GITHUB_REPO` | required | `OWNER/NAME` of the config repository |
| `FERRY_GITHUB_APP_ID` | required | The App's id |
| `FERRY_GITHUB_APP_KEY_FILE` | required | The App's private key, PEM |
| `FERRY_GITHUB_INSTALLATION_ID` | looked up | The installation id |
| `FERRY_GITHUB_OWN_CHECKS` | empty | Comma-separated required check names that ferry's merge-rule gate ignores |
| `GITHUB_API_URL` | `https://api.github.com` | For GitHub Enterprise Server |
| `FERRY_GITHUB_GIT_URL` | `https://github.com` | For GitHub Enterprise Server |

### GitLab

| Variable | Default | Meaning |
|---|---|---|
| `FERRY_GITLAB_URL` | required | The GitLab base URL |
| `FERRY_GITLAB_PROJECT` | required | `GROUP/NAME` or the numeric id |
| `FERRY_GITLAB_TOKEN_FILE`, `FERRY_GITLAB_TOKEN` | required | A project access token. The file is re-read on use. |
| `FERRY_GITLAB_GIT_URL` | the project's `http_url_to_repo` | The git remote |

### Webhook intake

| Variable | Default | Meaning |
|---|---|---|
| `FERRY_WEBHOOK_MAX_AGE_MINUTES` | `10` | Refuse events whose own timestamps are older |
| `FERRY_MAX_BODY_BYTES` | `1048576` | Larger request bodies get 413 |

### Switches

| Variable | Default | Meaning |
|---|---|---|
| `FERRY_ALLOW_COMMANDS` | `plan,apply` | The comment commands this ferry takes. `plan` alone makes it plan-only. |
| `FERRY_DISABLE_AUTOPLAN` | off | `1` plans only on `/ferry plan`, never on push |
| `FERRY_SWITCH_DIR` | the directory of `FERRY_LOCK_DB` | Where the `disable-apply` and `disable-autoplan` switch files live |

### OpenBao

Per-job credentials are enabled by setting `FERRY_OPENBAO_ADDR`.

| Variable | Default | Meaning |
|---|---|---|
| `FERRY_OPENBAO_ADDR` | unset | The OpenBao address |
| `FERRY_OPENBAO_JWT_FILE` | required with the address | The workload identity token file |
| `FERRY_OPENBAO_CA_CERT` | system trust | CA bundle for OpenBao's TLS certificate |
| `FERRY_OPENBAO_AUTH_MOUNT` | `jwt-nomad` | The auth mount to log in at |
| `FERRY_OPENBAO_AUTH_ROLE` | `ferry` | The auth role |
| `FERRY_OPENBAO_SSH_MOUNT` | `ssh-client-signer` | The SSH secrets engine mount |
| `FERRY_OPENBAO_SSH_ROLE` | `ferry` | The SSH signing role |
| `FERRY_OPENBAO_SSH_TTL` | the role's `ttl` | Certificate TTL to request, for example `20m` |
| `FERRY_OPENBAO_SSH_PRINCIPAL` | the role's `default_user` | `valid_principals` to request |
| `FERRY_OPENBAO_VAULT_PASSWORD_PATH` | unset | kv-v2 data path of the ansible-vault password |
| `FERRY_OPENBAO_VAULT_PASSWORD_FIELD` | `password` | The field at that path |
| `FERRY_OPENBAO_PLAYBOOK_TOKEN` | off | `1` gives playbooks the job's token as `VAULT_TOKEN` |

### Executor and collections

| Variable | Default | Meaning |
|---|---|---|
| `FERRY_EXECUTOR_SOCKET` | unset | Run playbooks in `ferry executor` over this Unix socket |
| `FERRY_GALAXY` | `install` | `install` or `skip` for the change's `requirements.yml` |
| `FERRY_GALAXY_SERVER` | unset | Pin the Galaxy server. Requirements naming another source fail. |
| `FERRY_GALAXY_TIMEOUT_SECONDS` | `300` | Kill `ansible-galaxy` after this long |
| `FERRY_COLLECTIONS_MAX_AGE_DAYS` | `14` | Prune collection caches unused this long. `0`: never. |
| `FERRY_GIT_TIMEOUT_SECONDS` | `300` | Timeout for any one git fetch or checkout |

### Operator runs

| Variable | Default | Meaning |
|---|---|---|
| `FERRY_RUN_OIDC_ISSUER` | unset | The identity issuer. The run API is off unless this and the audience are set. |
| `FERRY_RUN_OIDC_AUDIENCE` | unset | The expected audience |
| `FERRY_RUN_ALLOWED_SUBJECTS` | empty | Comma-separated subjects allowed to use the API |
| `FERRY_RUN_ALLOWED_GROUPS` | empty | Comma-separated groups allowed to use the API |
| `FERRY_RUN_LEASE_SECONDS` | `300` | Lease length |
| `FERRY_RUN_CERT_TTL` | `15m` | Certificate TTL for CLI runs. Accepts `900`, `900s`, `15m`, `1h`. |
| `FERRY_RUN_NAME_CLAIM` | `name` | The signed claim used as a display name |
| `FERRY_RUN_SSH_ROLE` | the service's role | SSH signing role for CLI certificates ([run.md](run.md)) |
| `FERRY_RUN_SSH_PRINCIPAL` | the service's principal | Principal requested for CLI certificates |

## `ferry executor`

| Variable | Default | Meaning |
|---|---|---|
| `FERRY_EXECUTOR_SOCKET` | required | The Unix socket to listen on, for example `/run/ferry/executor.sock` |
| `FERRY_WORKDIR` | `/var/lib/ferry/work` | The service's work directory. Jobs may only run in checkouts under it. |
| `FERRY_EXECUTOR_MUST_NOT_READ` | empty | Colon-separated paths of service secrets. The executor refuses to start if it can read any. |
| `FERRY_EXECUTOR_COLLECTIONS` | `$TMPDIR/ferry-collections` | A writable cache for collections |
| `FERRY_EXECUTOR_ANSIBLE_PLAYBOOK` | `ansible-playbook` | The `ansible-playbook` binary to run |
| `FERRY_EXECUTOR_ANSIBLE_GALAXY` | `ansible-galaxy` | The `ansible-galaxy` binary to run |

## CI driver and CLI

Used by `ferry plan`, `apply`, `unlock`, `drift`, `scope` and `policy`.

| Variable | Meaning |
|---|---|
| `FERRY_VCS` | `gitlab` (the default for these commands) or `github` |
| `FERRY_GITLAB_TOKEN` | GitLab API token (required for GitLab) |
| `CI_API_V4_URL`, `CI_PROJECT_ID`, `CI_MERGE_REQUEST_IID`, `CI_COMMIT_SHA`, `GITLAB_USER_LOGIN` | GitLab CI's own variables: API URL, project, merge request, commit, user |
| `CI_JOB_URL` | Linked from comments |
| `CI_DEFAULT_BRANCH`, `FERRY_DEFAULT_BRANCH` | The default branch. `FERRY_DEFAULT_BRANCH` wins. |
| `FERRY_MR_IID` | The merge request, when `CI_MERGE_REQUEST_IID` is not set |
| `FERRY_USER` | The commenting user. Trusted only when `CI_PIPELINE_SOURCE` is `trigger`. |
| `FERRY_GITHUB_TOKEN` | GitHub token (required for GitHub) |
| `FERRY_GITHUB_REPO`, `GITHUB_REPOSITORY` | The repository as `OWNER/NAME` |
| `FERRY_GITHUB_BOT` | ferry's own login on GitHub |
| `FERRY_GITHUB_OWN_CHECKS` | As for the service |
| `GITHUB_API_URL` | For GitHub Enterprise Server |
| `FERRY_LOCK_DB` | The lock file (GitHub), and the default location of `ferry switch` files |

## `ferry run`

On the operator's workstation:

| Variable | Meaning |
|---|---|
| `FERRY_SERVICE_URL` | Default for `--service`. Set this or pass the flag. |
| `FERRY_RUN_TOKEN_COMMAND` | Default for `--token-command` |

## CLI

Global flags, placed before the command:

| Flag | Meaning |
|---|---|
| `--repo DIR` | The config repository checkout. Default: the current directory. |
| `--config FILE` | Default: `ferry.yaml` |
| `--vcs gitlab\|github` | The code host. Default: `FERRY_VCS`, else `gitlab`. |
| `--api-url URL` | The code host API URL |
| `--project ID` | GitLab project id or path, or GitHub `OWNER/NAME` |
| `--lock-db FILE` | GitHub: the lock file |

Commands:

| Command | Flags |
|---|---|
| `ferry plan` | `--inventory NAME`, `--mr N`, `--sha SHA`, `--user NAME`, `--summary FILE` (write a JSON summary) |
| `ferry apply` | The same as `plan` |
| `ferry drift` | `--inventory NAME`, `--sha SHA`, `--no-report` (do not write textfiles on hosts), `--out DIR` (default `ferry-drift`) |
| `ferry unlock` | `--inventory NAME`, `--commit SHA` (default `CI_COMMIT_SHA`) |
| `ferry scope FILE...` | `--inventory NAME` |
| `ferry policy` | none |
| `ferry serve` | none. Settings come from the environment. |
| `ferry executor` | none. Settings come from the environment. |
| `ferry switch [apply\|autoplan] [on\|off]` | `--reason TEXT`. With no arguments, shows the switches. |
| `ferry run PLAYBOOK` | `-i`/`--inventory NAME`, `--limit PATTERN`, `--apply`, `--reason TEXT`, `--service URL`, `--token-command CMD`, `--insecure-http`, `--no-host-key-checking`. See [`ferry run`](run.md#usage). |

Exit codes: `plan`, `apply`, `drift` and `policy` exit 1 when the result is not ok. `ferry run`
returns Ansible's exit code, 1 for a refusal, and 130 when interrupted.

## HTTP endpoints

See [`ferry serve`](serve.md#http-endpoints-and-edge-routing) for the table and the routing rules,
and [`ferry run`](run.md#the-run-api) for the run API.
