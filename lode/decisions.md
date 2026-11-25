# Design decisions

## Engine first, CI as a thin driver
A CI workflow first and a service later, from the same code. All logic lives in
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
No Pushgateway assumed. Managed hosts usually already run node_exporter, so ferry
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

## Approvals must come from someone who could apply
Found reviewing the service (2026-11-23): `approvers()` returned every user whose latest review
approved the head, and the engine only dropped the author. On a public GitHub repo without branch
protection requiring reviews, anyone can leave an approving review, and a drive-by approval was
enough to apply in the local lab. The engine now counts an approval only if `can_apply()` is true for
that user (write on GitHub, Developer+ on GitLab) and it isn't ferry's own account (a job that got
hold of the service's credentials could otherwise approve). With `apply.allowed_users` set, the
applier must be listed **and** still have access.

## GitHub's own merge rules gate apply too
2026-11-24: ferry's approval check is its own floor, not the repo's rules. A branch could require two
reviews, a CODEOWNERS review or a passing check and ferry would still apply on one approval. Apply
now also asks GitHub (`merge_rule_blockers`, `vcs/github_protection.py`): GraphQL `reviewDecision`,
per-check `isRequired`, and `mergeStateStatus`, with classic protection and active rulesets read to
name the unmet rule and find required checks that never reported. GitHub's verdict is preferred over
re-implementing the rules; the rules are still checked independently because `mergeStateStatus` is
computed for the viewer (an App with bypass sees CLEAN). Checks posted by ferry's own App, or named in
`FERRY_GITHUB_OWN_CHECKS` (service env, never the repo), are ignored so a required ferry check can't
deadlock the apply. Unreadable rules (missing App permission) refuse, naming the permission. REST
`mergeable_state` `blocked` no longer refuses by itself: the new check explains it.

## Permission check before a job exists
Found reviewing the service (2026-11-23): `/ferry apply` from anyone was queued, and the worker did
the checkout, the OpenBao login and the SSH signing before the engine refused it. Now both commands
check `can_apply(commenter)` in the webhook handler, so a refused request costs one API call and no
credential (the engine still re-checks every gate). The queue is bounded (`max_queued_jobs`, 503 when
full), so commenters on a public repo can't pile up unlimited jobs, and `/healthz` (unauthenticated)
no longer returns error text, which can name internal hosts and paths.

## Certificate lifetime, job timeout, redaction of job credentials
Found reviewing the service (2026-11-23). A signed certificate outlives its job: revoking the job's
token doesn't revoke the cert. So the service can ask for an explicit `ttl` and `valid_principals`
(FERRY_OPENBAO_SSH_TTL / _PRINCIPAL) instead of taking the role's defaults, and runs can be killed
after FERRY_JOB_TIMEOUT_MINUTES (the whole process group, so ssh children go too). Keep the timeout
below the cert TTL, otherwise a long apply fails on new connections halfway. The job's OpenBao token
exists only in the per-job env, which the default Redactor (os.environ) never saw: the service now
builds the redactor from os.environ plus the job env, and the runner echoes redacted lines (in the
service the echo goes to the service log, not a CI log). A failed token revoke is logged, not swallowed.
Kubernetes auth needs no code: its login body is the same `{role, jwt}` as the jwt method.

## Playbooks run in `ferry executor`, not in the service (2026-11-24)
A PR's playbooks are untrusted code on the controller. Under the service's uid they could read the
App key or GitLab token, the webhook secret, the workload JWT and the lock DB, and they got the
service's whole environment. Now `ferry serve` with FERRY_EXECUTOR_SOCKET sends each run to
`ferry executor` in a second container (uid 10002) over a Unix socket. That container has no secret
mounts, has the work dir read-only, and gets the job's env plus its credential files' contents.
Chosen over a same-container uid switch, which needs root or CAP_SETUID and leaves the secrets on
the same filesystem, and over a pod/job per run, which needs a pods/create or job-submit grant.
The job's OpenBao token is revoked before any playbook runs (opt back in:
FERRY_OPENBAO_PLAYBOOK_TOKEN). As PID 1 the executor runs kill(-1) after each job and rebinds a
replaced socket, so leftovers can't catch the next job. The playbook still reads the job's cert and
vault password: inherent. The static policy is a lint, not a boundary: a lookup plugin in the repo
runs on the controller during a plan (the lab's `evil` uses one).

## ferry serve for GitLab
Same service, a different intake (2026-11-23): `GitLabService` subclasses `Service` and overrides only
the webhook fast path (X-Gitlab-Token compared in constant time, since GitLab doesn't sign bodies;
dedupe on Idempotency-Key, else X-Gitlab-Event-UUID; Merge Request / Note hooks to the same Jobs).
The HTTP handler calls `service.handle_request(headers, body)` so each backend reads its own
headers. Workspace takes the change ref template and git user (refs/merge-requests/<n>/head,
oauth2). Approvals of the current head only: GitLab has no commit on an approval, so
`GitLabBackend.approvers` compares `approved_at` with the created_at of the newest MR diff version
whose head is the MR SHA; without `approved_at` it trusts approvals only when the project resets
them on push, else counts none (benefits the CI driver too). `unchecked`/`checking`/`preparing`
refuse (GitHub's `unknown`). `ci_must_pass`/`ci_still_running` block in the service
(`pipeline_blocks=True`, like GitHub required checks) but not in the CI driver, whose apply job runs
inside that pipeline. Token: project access token, Developer, api + read_repository.


## The plan rule covers pushers, not only authors
A push into a writer's PR by someone without write access (a bot or App with contents write, a user
who lost access) would otherwise be planned automatically, and a plan runs the pushed code with root
SSH access. `synchronize` (GitHub, `sender`) and MR updates with new commits (GitLab, `user`) now
check the pusher too; a non-writer's push waits for `/ferry plan` like a non-writer's PR.

## GitLab intake gets the same pre-queue checks as GitHub
Both `/ferry plan` and `/ferry apply` check Developer+ in the webhook handler, before a job exists,
and every GitLab job goes through the bounded `_enqueue` (a full queue answers 503 instead of
blocking the HTTP thread).

## Host key checking on by default
The image sets `ANSIBLE_HOST_KEY_CHECKING=True`: without it, anything answering on a host's address
receives an apply's payload, including values decrypted with the vault password. Deployments mount
known_hosts at `/etc/ssh/ssh_known_hosts` (pinned keys or `@cert-authority`). The local lab turns it
off for its throwaway target.

## Retry the code host at startup
`ferry serve` reads the App/bot identity at start so a wrong credential fails fast. Under an
orchestrator, DNS or the network often isn't ready yet, and exiting then just waits for a restart
(the local lab hit it under podman). Transient failures (unreachable, 5xx) are retried with backoff
(`FERRY_STARTUP_ATTEMPTS`, default 8); a 4xx (bad token, wrong repo) still exits at once.

## Replays: dedupe on the body, refuse old events, persist (#11)
`X-GitHub-Delivery` isn't covered by the HMAC, and GitLab's token proves only that the sender knows
the secret, so a delivery id alone can't identify a replay. The state file (`state.SQLiteState`, the
same SQLite file as the locks) keeps both the delivery id and a SHA-256 of the body for 7 days,
capped at 20k rows; a delivery answered 5xx (queue full) is forgotten so the code host's retry
counts. On top, every event's own signed timestamps must be within `FERRY_WEBHOOK_MAX_AGE_MINUTES`
(10) and at most 5 minutes in the future, closes included: a replayed close of a reopened PR would
otherwise free its locks. An event with no readable timestamp is refused. Cost: a manual redelivery
more than 10 minutes late is refused; comment again instead.

## HTTP limits (#15)
Webhook bodies are small, so the cap is 1 MiB (`FERRY_MAX_BODY_BYTES`), chunked bodies are refused
(411), every socket read and write times out after 15 s, and `BoundedHTTPServer` closes connections
past 32 at once instead of starting a thread for each. Still meant to sit behind a proxy.

## Comments are pinned to the head they were written about (#12)
For a non-writer's PR, `/ferry plan` is the vouching step, so it must run what the commenter read,
not whatever the head is when the worker gets there. `--sha <commit>` names it. Without it, ferry
needs to know the head as of the comment's `created_at`. GitHub's `issue_comment` carries no head,
and a commit's dates are the author's to set, so the service records when each head arrived from
the signed `pull_request` events (`state.head_since`: a new head starts its clock, a repeat of the
same head doesn't, older events are ignored). GitLab notes carry `merge_request.last_commit`, and
the MR's diff versions give the server's own arrival time. A head that arrived at or after the
comment, or with no known arrival (a PR older than the service, unreadable versions), is refused
with a note that suggests `--sha`. Jobs from comments are `pinned`: if the head moved before the
worker runs them, they post "not run" and spend nothing. Rejected: comparing commit dates
(forgeable), and trusting the head at webhook time (the race the issue describes).

## Apply gates before credentials (#13)
`Engine.apply_precheck` runs `_apply_blockers` (apply_checks, the policy check, the scope compared
with the plan) with a runner that can't run, and posts the refusal. Only inventories that pass get a
`job_credentials` block, where `apply()` checks everything again.

## Unlock on close from the lock table (#14)
Unlocking read `ferry.yaml` from the default branch to list inventories, so an unreachable git
remote failed the job and leaked the lock (it isn't retried), and an inventory removed from
ferry.yaml kept its lock forever. `LockStore.release_all(holder)` deletes by PR number (SQLite,
memory and tag locks). The job first asks the API whether the PR is open again (a quick reopen
keeps its locks); if the API can't answer, the fresh, verified close event is trusted. Losing the
state file frees every lock, documented in the README.

## Metrics and job history without dependencies (#16)
`ferry.metrics` writes the Prometheus text format itself (counters, gauges, one histogram). It's
small, and the image stays `PyYAML` + `cryptography`. Label values come only from fixed sets
(`ACTIONS`, `RESULTS`, `REJECT_REASONS`), and the fixed series are emitted at zero, so a webhook can
neither add series nor smuggle text into `/metrics`. Rejects are classified from the intake's answer
(`reject_reason`) in `Service.receive` and the HTTP handler, the same for both backends. Job history
is a bounded `jobs` table in the state file (`FERRY_JOB_HISTORY`, default 200), served read-only at
`/jobs` without error text or reasons, for the same reason `/healthz` has none.

## Ack, then edit that comment into the result (#89)
A queued plan, apply or `/ferry unlock` posts one comment at once and the worker edits it to
`running` and then replaces it with its first result (`Service._post_result`, also what the job's
engines post through via `_JobVCS`). The plan record stays the last line of a comment by ferry's own
account, so `Engine._latest_plan_or_unlock` and invariant 8 are unchanged. That function takes the
newest record by comment position, and an edited comment keeps its creation position, so ordering
had to be kept: (1) the ack is posted and the job queued under one mutex (`_enqueue_mutex`), so acks
on a change are in queue order, which is the order jobs run; (2) when a job has to write a new
comment (an edit failed, a second inventory), every job queued before that moment has an older ack,
so `_fence[pr]` records the queue seq and those jobs write new comments too. Without the fence, a
plan that fell back to a new comment would sit below the edited ack of an unlock queued after it
and look newer. Rejected: ordering records by their `at` field (would change what makes a record
trusted), and deleting acks (notifications are already sent; a quiet skip edits the ack instead).
Automatic plans into a base branch ferry doesn't handle get no ack (bases from the last
`ferry.yaml` loaded). Jobs are opened in the state file at enqueue (`result` `queued`/`running`,
the ack id in a column `/jobs` doesn't serve); at startup `recover_acks` closes the ones a restart
left behind. A rate-limited progress line during long runs was left out: the runner has no event
stream to hang it on yet.
