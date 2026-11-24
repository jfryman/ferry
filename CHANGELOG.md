# Changelog

All notable changes to ferry. Versions follow [semver](https://semver.org); tags are `vX.Y.Z`.

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

[0.2.1]: https://github.com/jfryman/ferry/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jfryman/ferry/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/jfryman/ferry/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jfryman/ferry/releases/tag/v0.1.0
