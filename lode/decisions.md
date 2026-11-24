# Design decisions

## Engine first, CI as a thin driver
James wants a workflow now and a service later (and reuse at work). All logic lives in
`Engine`; the CLI only maps CI variables to arguments. A service calls the same methods.

## Own callback plugin instead of parsing stdout
`ferry_events` (aggregate callback) writes JSON lines; the default stdout callback keeps the human
log in the job. Callbacks receive Ansible's censored copy of no_log results; the plugin also drops
diff/msg when a result is censored. Verified by `test_callback_plugin_real_ansible_run_respects_no_log`.

## Plan record = hidden marker in ferry's own MR comment
`<!-- ferry:plan {...} -->` with sha, limit, clean, config digest, time. Apply trusts only markers
authored by ferry's token user (forged markers in human comments are ignored). No plan artifact is
needed: Ansible has no plan file, so apply re-runs live with the same `--limit`; staleness is bounded
by `max_plan_age_hours` (24h) and the head-SHA check.

## Locks as GitLab tags
Need atomic create without new infra. Tag create fails if it exists; tags show the holder in the
UI. Lock taken at plan, held until merge/close (Atlantis semantics); stale holders (closed/merged
MR) are taken over. Consumers must exclude `ferry-lock-*` tags in workflow rules. A service can
swap in a DB-backed LockStore.

## Scope over-approximates
Changed files map to hosts/groups (host_vars, group_vars, roles via play graph + meta deps +
include_role, imported playbooks). Anything unknown, `group_vars/all`, complex host patterns
(`&`, `!`, globs, templates) widen to every host. Docs/CI/tests are ignored.

## Busy marker, not a ferry-specific guard
ferry doesn't know about printers. A task failing with `FERRY_HOST_BUSY: <reason>` marks the host
blocked. The config repo's guard role emits it. Blocked != failed in comments and metrics, but a
plan with a blocked host is not clean (strict: re-plan when the printer is idle).

## Drift via node_exporter textfiles, not Pushgateway
No Pushgateway in the ox cluster (checked 2026-11-14). The hosts already run node_exporter, so ferry
writes one textfile per host; staleness of that file is the "drift check didn't run" signal and
needs no extra component. Cost: one write per host per drift run, skipped on blocked hosts.

## Alert, don't remediate
Drift raises alerts; auto-apply of main is not implemented (James to decide).

## No third-party deps beyond PyYAML
GitLab API via urllib so the package installs anywhere Ansible does.

## GitHub backend, for an Atlantis-style service
ferry can run as a long-running service (e.g. on Nomad) with a GitHub App (decided 2026-11-23): webhooks
in, clone with the App's installation token at the PR head SHA, comments as the App. In GitHub Actions
a `pull_request` workflow comes from the PR branch, so a PR could take the job's credentials; the
service keeps the credential and its config on the default branch. `GitHubBackend` is token-agnostic
(a string, or a callable returning a fresh installation token); minting the App token (RS256 JWT)
stays in the service so the library keeps its stdlib-only dependencies. Approvals count only the
latest decisive review per user and only for the current head SHA (COMMENTED doesn't undo an
approval; a stale approval doesn't count). `mergeable_state` "unknown" refuses apply rather than
guess.

## Service locks in the service's own file, not the repo
James, 2026-11-23: "Locks shouldn't be tags"; Atlantis keeps them in a local file (BoltDB) and the
service can do the same. `SQLiteLocks`: one row per inventory, primary key + BEGIN IMMEDIATE make
acquire atomic across processes; the file lives on persistent storage (Nomad host volume), one ferry
service per file. Tag locks remain only for the CI-only GitLab driver, which has no storage.

## The service (ferry serve)
James, 2026-11-23: one PR, working as a unit. The service owns the credential and its config:
webhooks are HMAC-verified and deduped by delivery id; jobs run one at a time on a worker thread (the
webhook only decides and enqueues, so GitHub's 10 s timeout is never at risk); ferry.yaml is loaded
from the default branch and passed to the Engine while the playbooks run from the PR checkout at the
exact head SHA (a moved PR is skipped quietly: the newer push brings its own event). The installation
token reaches git only via GIT_CONFIG_* env (http.extraHeader), never .git/config or argv. Plan rule:
auto-plan only for authors with write access; others wait for `/ferry plan` from a writer (a plan is
root-level code execution). Per-job OpenBao credentials (workload-identity login, throwaway SSH key +
CA certificate, vault password) live in a 0700 temp dir that is removed, and the token revoked, in a
finally block. Verified end to end Nov 23: real OpenBao 2.7.2 (JWT login, an SSH signing
role, sshd login with the certificate) and the built image against a fake GitHub API plus a real git
remote (plan, refused apply, apply, unlock).

## Per-job SSH connection sharing
Found in the local lab (2026-11-23): with Ansible's default ControlPath (~/.ansible/cp) a job reused
the previous job's open master connection, so it ran under the previous job's certificate after that
job's credentials were deleted, and sshd attributed it wrongly. credentials.job_credentials now sets
ANSIBLE_SSH_CONTROL_PATH_DIR inside the job's temp dir (and ControlPersist 30s): one certificate login
per job, verified by distinct serials in the target's sshd log.
