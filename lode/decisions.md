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
Atomic runs ferry as a long-running service on Nomad with a GitHub App (James, 2026-11-23): webhooks
in, clone with the App's installation token at the PR head SHA, comments as the App. In GitHub Actions
a `pull_request` workflow comes from the PR branch, so a PR could take the job's credentials; the
service keeps the credential and its config on the default branch. `GitHubBackend` is token-agnostic
(a string, or a callable returning a fresh installation token); minting the App token (RS256 JWT)
stays in the service so the library keeps its stdlib-only dependencies. Approvals count only the
latest decisive review per user and only for the current head SHA (COMMENTED doesn't undo an
approval; a stale approval doesn't count). `mergeable_state` "unknown" refuses apply rather than
guess. Locks are annotated tags made in two steps (tag object, then the ref); creating the ref is the
atomic step (422 if held).
