# Changelog

All notable changes to ferry. Versions follow [semver](https://semver.org); tags are `vX.Y.Z`.

## [0.4.0] - 2026-11-24

`ferry run`: operators run Ansible from their own machine, **through the running service**. The
service owns the inventory lock, mints the per-run credentials and records the audit, as it does for
webhook jobs (#50, #52). It's a break-glass path: no PR, review or approval. The API is off unless
`FERRY_RUN_OIDC_ISSUER` and `FERRY_RUN_OIDC_AUDIENCE` are both set.

### Added
- **`ferry run [-i inventory] [--limit …] [--apply] [--reason …] playbook`** (#52): check/diff by
  default, `--apply` to write. The CLI gets an OpenBao identity token from `--token-command` and
  sends it as a bearer token to `POST /api/runs`, then `/api/runs/<id>/lease` and `/finish`. It
  generates a throwaway ed25519 key, keeps credentials in a private temporary directory, renews
  its lease while Ansible runs, and stops Ansible if the lease can't be renewed.
- **Authorization:** RS256 identity tokens verified against the issuer's JWKS, with a fixed issuer
  and audience. The caller must be on `FERRY_RUN_ALLOWED_SUBJECTS`/`_GROUPS` **and** the inventory's
  `apply.run_allowed` in the default branch's `ferry.yaml`.
- **Locks are shared:** CLI runs, PR jobs and scheduled drift all use the same inventory lock. A CLI run
  blocks a PR plan or apply and the reverse. Leases expire if a client dies, and survive restarts.
- **Audit** (#64): job records (`/jobs`) and a `ferry.runs` log line for every CLI run event:
  issued, ok, failed, expired and refused. Each has the identity subject and display name,
  inventory, SHA, mode, limit, playbook, reason, time, and the certificate serial. Starts
  refused for a verified identity are recorded too. `--reason` is optional (at most 256 characters).
- Lab scenarios for operator check/apply, unmerged apply, denied identity, contention and lease expiry.

### Security
- **An apply must be the default-branch head** (#58): the service refuses (409) a `--apply`
  whose SHA isn't the head it fetched, so operators apply only merged code. Check mode may name any
  published SHA. Empty allow-list entries and empty group claims never match.
- **Short CLI certificates** (#53, #59): the service asks OpenBao for `FERRY_RUN_CERT_TTL`
  (default `15m`) and refuses a certificate that comes back longer. Certificates are still not scoped
  to the locked inventory (#53 stays open for later).
- **`--insecure-http` no longer disables SSH host-key checking** (#54, #60); that's the separate
  `--no-host-key-checking`, lab only.
- **JWKS cache** (#55, #63): keys are cached for 5 minutes; an unknown `kid` refetches at most every
  30 s; bad tokens never trigger a fetch. If the issuer is unreachable, keys up to an hour old are
  still used, then requests fail closed.
- **No operator identity in code-host comments** (#57, #64): a PR blocked by a CLI run sees
  `run:<id>` only.
- The apply switch stops new CLI runs in both modes, and is re-checked right before signing and
  before credentials are returned. No identity token, OpenBao token, key, vault password or output is
  stored. All supplied credentials are masked in CLI output regardless of length; output over 1 MiB
  is withheld rather than truncated.

### Changed
- **Scheduled drift takes the inventory lock** before minting credentials, and skips (and records)
  inventories that are held. After a restart, stale drift locks are released only once the executor
  confirms it's idle.
- Job records have new columns (`trigger`, `run_id`, `subject`, `display_name`, `mode`, `limit`,
  `playbook`, `certificate_serial`, `counts`, `reason`). Existing state databases are migrated in
  place on start.
- Metrics: new `ferry_jobs_by_trigger_total{action,result,trigger}` (`webhook`, `schedule`, `cli`);
  existing series are unchanged.
- Local lab: the executor socket is on a named volume (Docker Desktop on macOS can't chmod a socket on
  a bind mount).

### Upgrading
- Nothing to do if you don't use `ferry run`: the API stays off. To enable it, see the README's
  "Operator runs" section: an OpenBao identity token role, the two `FERRY_RUN_OIDC_*` settings, an
  allow-list, `apply.run_allowed` per inventory, and an ingress route for `POST /api/runs` and its
  lease/finish routes only. Never route `/jobs` or `/metrics` publicly.

## [0.3.0] - 2026-11-24

Security fixes and Atlantis-parity features for `ferry serve`, from the 0.2.1 review (#21–#30,
#37). Both backends (GitHub and GitLab) get every change.

### Security
- **Locks fail closed** (#21, #38): an API error while checking a lock holder (timeout, 5xx, rate
  limit) refuses and keeps the lock instead of giving it away; only a definite 404/410 makes a lock
  stale. Commands on closed or merged PRs are refused at the webhook, and a queued plan for a PR
  that closed runs nothing.
- **Only PRs into the default branch** (#22, #39), or a branch listed in the default branch's
  `ferry.yaml` `base_branches`. A PR can't allow its own base; a PR retargeted after its plan is
  not applied.
- **Apply refuses a PR behind its base** (#23, #40): applying it would revert what merged since.
  The plan warns, and the apply says to rebase or merge the base first.
- **Comment commands dedupe on the comment/note id** (#37, #41): a comment re-sent with a fresh
  delivery id and re-encoded bytes runs at most once.
- **Comments** (#24, #42): the plan marker must be the last line of ferry's own comment and carries
  an HMAC; PR-controlled text is escaped; the error path and the vault password are redacted; the
  size is bounded.
- **Pushers' approvals don't count** (#25, #43): an approval from anyone who pushed or authored
  commits in the PR doesn't meet the approval rule.
- **`ansible-galaxy` runs in the executor** (#26, #44), with a timeout
  (`FERRY_GALAXY_TIMEOUT_SECONDS`) and none of the service's environment. `FERRY_GALAXY=skip`
  never reads `requirements.yml`.

### Added
- **Commit statuses** (#29, #47): with `FERRY_COMMIT_STATUS=1`, `ferry/plan: <inventory>` and
  `ferry/apply: <inventory>` on the head that ran, so branch protection can require an apply before
  merge. Ferry's own required status never blocks its own apply (GitHub rulesets and classic
  protection; GitLab "pipelines must succeed"); the same name posted by anyone else still counts.
- **`/ferry unlock [inventory]`** (#30, #48): write access, queued like any job and recorded in the
  job history. The change's plan no longer applies after it; plan again.
- **`/ferry help`**, and a reply to a `/ferry …` comment that doesn't parse (#30, #48), at most one
  per change per 10 minutes. Other comments are never answered.
- **Switches** (#28, #46): `ferry switch apply|autoplan off --reason …` (files in
  `FERRY_SWITCH_DIR`), `FERRY_ALLOW_COMMANDS=plan` and `FERRY_DISABLE_AUTOPLAN=1` stop applies or
  automatic plans without stopping the service, queued jobs included.
- **`apply.require_inventory`** and per-inventory apply rules (#27, #45). A bare `/ferry apply`
  stops at the first inventory that fails.
- Local lab: the fake GitHub reports commit statuses; `lab.sh security` covers #21–#23, #25, #29
  and #37.

## [0.2.1] - 2026-11-24

Hardening and observability for `ferry serve`, from the 0.2.0 maintainer review (#11–#17). Both
backends (GitHub and GitLab) get every change.

### Security
- **Replays** (#11, #20): a delivery counts as a duplicate if its id *or* a digest of its exact
  body has been seen. The id isn't covered by GitHub's signature. Seen deliveries are kept in the
  state file for 7 days, so this holds across restarts. Events whose own timestamps are older than
  `FERRY_WEBHOOK_MAX_AGE_MINUTES` (default 10) are refused, close events included: a replayed close
  would otherwise free a reopened PR's locks. The timestamps checked are a comment's
  `created_at`/`updated_at` and a PR's `updated_at`.
- **Comments run the commit they were written about** (#12, #32). `/ferry plan|apply` is pinned to
  the head as of the comment, and `--sha <commit>` names it outright. A head that arrived after the
  comment, or one ferry never saw arrive, is refused with a note. A queued job whose PR moved posts
  "not run".
- **Apply gates run before credentials** (#13, #32): a refused apply costs no OpenBao login and no
  certificate.
- **HTTP limits** (#15, #20): a 1 MiB body cap (`FERRY_MAX_BODY_BYTES`), a 15 s socket timeout and
  at most 32 concurrent connections. Chunked bodies and bad lengths are refused.

### Added
- **`GET /metrics`** (#16, #34), in Prometheus format: jobs by action and result, job durations,
  jobs queued, webhook rejects by reason, queue depth, locks held and the build version. Label values come
  only from fixed sets: no users, commits, error text or credentials.
- **`GET /jobs`** (#16, #34): a bounded, read-only job history in the state file
  (`FERRY_JOB_HISTORY`, default 200).
- **The local lab runs under rootless podman** (#17, #35): `deploy/local/podman-docker` (keep-id
  user namespaces), a persistent `podman system service` via `CONTAINER_HOST`, and `lab.sh replay`.

### Fixed
- Closing a PR releases its locks straight from the lock table, with no git fetch and no
  `ferry.yaml`. An unreachable code host no longer leaks the lock, and an inventory dropped from
  `ferry.yaml` no longer keeps its lock forever. A PR reopened before the job runs keeps its locks
  (#14, #33).

## [0.2.0] - 2026-11-24

ferry runs as a service: `ferry serve`, an Atlantis-style webhook service for a GitHub App or a
GitLab project that holds its own credentials. The GitLab CI driver is unchanged.

### Added
- **`ferry serve` for GitHub** (#1). The service:
  - verifies and dedupes HMAC-signed webhooks;
  - posts plan and apply comments as the App, with installation tokens refreshed before they expire;
  - supports `/ferry plan|apply [inventory]` and scheduled drift;
  - serves `/webhook` and `/healthz`.
- **`ferry serve` for GitLab** (#5): MR and note hooks, `X-Gitlab-Token`, approvals that count only after the current head arrived, and Developer+ to plan or apply.
- **Service locks in SQLite** (#1): the service's own state file, atomic across processes, with nothing stored in the repo.
- **Workspace** (#1). `ferry.yaml` comes from the default branch, and playbooks from the PR at its exact head SHA. Git credentials go through the environment only. Collections are cached per `requirements.yml`.
- **Per-job OpenBao credentials** (#1). Each job gets a workload-identity login, a throwaway ed25519 key signed by the SSH CA, and the vault password. These sit in a 0700 temp dir that is removed, and the token revoked, in a `finally` block. Each job also gets its own SSH ControlPath, so no job reuses another job's connection.
- **`ferry executor`** (#7). Playbooks run as another user or container that can't read the service's secrets. The service sends each job over a Unix socket. The job's token is revoked before the play, unless `FERRY_OPENBAO_PLAYBOOK_TOKEN=1`. The executor refuses to start if it can read any path in `FERRY_EXECUTOR_MUST_NOT_READ`.
- **GitHub merge rules gate apply** (#8): the review decision, required checks, branch protection and rulesets. Checks from ferry's own App are ignored.
- **Container image** `ghcr.io/jfryman/ferry`, for linux/amd64 and linux/arm64, with SBOM and provenance attestations. It is non-root, and the same image runs `serve` (the default) and `executor`. Also included: an example Nomad job (`deploy/nomad/`) and the local lab (`deploy/local/`), which runs the whole loop in containers.
- **GitHub Actions**: tests on every PR and push to `main`, and the image build and publish.
- `FERRY_JOB_TIMEOUT_MINUTES`, `FERRY_OPENBAO_SSH_TTL`, `FERRY_OPENBAO_SSH_PRINCIPAL`, `FERRY_STARTUP_ATTEMPTS`.

### Security
- Only approvals from users who could apply count, and never ferry's own account. With `apply.allowed_users`, the applier must be listed **and** still have access (#2).
- Write access (Developer+ on GitLab) is checked in the webhook handler, before a job or any credential exists. The job queue is bounded (503 when full). `/healthz` returns no error text (#3, #10).
- Certificates are requested with an explicit TTL and principal. The job's credentials are redacted from output, and failed revokes are logged (#4).
- The plan rule covers pushers as well as authors. A non-writer's push into a writer's PR waits for `/ferry plan` (#10).
- The image checks host keys by default (`ANSIBLE_HOST_KEY_CHECKING=True`). Mount known_hosts at `/etc/ssh/ssh_known_hosts` (#10).

### Changed
- Docs and lab examples use generic names (#9, #10).
- `ferry serve` retries the code host at startup while it's unreachable (#10).

## [0.1.1] - 2026-11-14

### Fixed
- Diffs show the target path, not the controller's temp template.

## [0.1.0] - 2026-11-14

### Added
- The plan/apply/drift engine, the GitLab backend, tag locks, the CLI driver for GitLab CI, and drift metrics as node_exporter textfiles.

[0.4.0]: https://github.com/jfryman/ferry/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jfryman/ferry/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/jfryman/ferry/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jfryman/ferry/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/jfryman/ferry/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jfryman/ferry/releases/tag/v0.1.0
