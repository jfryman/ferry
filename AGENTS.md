# AGENTS.md

Instructions for coding agents working in this repository. Humans: see
[docs/development.md](docs/development.md).

ferry runs Ansible against real hosts, usually as root, on behalf of pull requests. Most of the
code exists to decide when **not** to run. Treat every check as load-bearing.

## Read first

| For | Read |
|---|---|
| What ferry is and the terms it uses | [docs/concepts.md](docs/concepts.md), [lode/terminology.md](lode/terminology.md) |
| Why it is built this way | [lode/decisions.md](lode/decisions.md) |
| The security model | [docs/security.md](docs/security.md) |
| How to test, merge and release | [lode/practices.md](lode/practices.md), [docs/development.md](docs/development.md) |
| `ferry run` internals | [lode/operator-runs.md](lode/operator-runs.md) |

## Commands

```bash
pip install -e '.[test]' 'ansible-core~=2.20.0'   # set up
python -m pytest -q                               # all tests; run before every commit
python -m pytest -q tests/test_engine.py -k apply # a subset
deploy/local/up.sh                                # the local lab (needs Docker or rootless podman)
deploy/local/lab.sh security                      # the lab's security scenarios
deploy/local/down.sh
```

In a sandbox that kills a command's process tree when it ends, the lab's containers die with it.
Start a persistent `podman system service --time=0` and export `CONTAINER_HOST` first. See
[deploy/local/README.md](deploy/local/README.md#rootless-podman).

## Where things are

| Task | Start in |
|---|---|
| A new apply gate, or a change to one | `src/ferry/engine.py` (`apply_checks`, `_apply_blockers`), tests in `tests/test_engine.py` |
| Lock behaviour | `src/ferry/engine.py` (`_lock_conflict`), `src/ferry/locks.py`, `tests/test_locks.py` |
| Webhook intake, comment commands, the queue | `src/ferry/service.py` (GitHub and shared), `src/ferry/gitlab_service.py`, `src/ferry/webhook.py` (command regexes) |
| A code host API call | `src/ferry/vcs/github.py`, `src/ferry/vcs/gitlab.py`. The interface is `src/ferry/vcs/base.py`. |
| GitHub branch protection and rulesets | `src/ferry/vcs/github_protection.py` |
| A `ferry.yaml` key | `src/ferry/config.py`, then [docs/configuration.md](docs/configuration.md) |
| An environment variable or CLI flag | `src/ferry/cli.py` (also update `SERVE_HELP`), then [docs/configuration.md](docs/configuration.md) |
| Comment text, plan records, escaping | `src/ferry/render.py`, `src/ferry/sanitize.py` |
| Per-job credentials | `src/ferry/credentials.py` |
| The executor and its protocol | `src/ferry/executor.py` |
| Operator runs | `src/ferry/runs.py` (API), `src/ferry/run_identity.py` (JWT), `src/ferry/operator.py` (client) |
| Metrics, job history | `src/ferry/metrics.py`, `src/ferry/state.py` |
| Scope computation | `src/ferry/scope.py`, `tests/test_scope.py` |

The engine knows nothing about CI or HTTP. Drivers build an `Engine` from a `Config`, a
`VCSBackend`, a `LockStore` and a `Runner`, and call one method. Keep it that way. Keep
`VCSBackend` small: anything specific to one code host is computed inside its backend.

## Invariants

Do not break these. If a task seems to require it, stop and ask.

### The approval gate

1. Apply never runs without `apply.min_approvals` approvals that count.
2. An approval counts only for the current head commit.
3. An approval counts only from a user who could apply (`can_apply`), and never from ferry's own
   account.
4. The author's approval, and one from anyone who authored, committed or pushed part of the
   change, does not count unless `allow_author_approval` is set.
5. Every gate is re-checked at apply time against live code host state. Never cache a gate result
   between plan and apply.
6. Every gate has a refusal test. A new gate without one is incomplete.

### The plan record matches the commit

7. Apply requires a trusted plan record whose `sha` is the change's head, whose config digest
   matches, and whose scope equals the scope computed now.
8. A plan record is trusted only as the last line of a comment by ferry's own account, with a valid
   HMAC under `ferry serve`. Never parse records from other comments or other positions.
9. Comment commands run the commit the comment was written about. Do not replace this with "the
   head at the time the worker starts", and do not use commit dates.

### Locks

10. A plan that affects hosts takes the inventory lock before Ansible runs. Apply confirms it.
11. Lock checks fail closed. Only a definite closed, merged, 404 or 410 makes a holder stale. Any
    other error keeps the lock and refuses.
12. A string holder (`run:...`, `drift`) is never looked up as a change and never taken over by
    one.
13. Locks are acquired before credentials are minted, on every path (webhook, drift, operator run).

### Fail closed

14. When ferry cannot determine something (mergeability, who pushed, whether a branch is behind, a
    rule source, a timestamp), it refuses. Never turn an error into "allowed". The table in
    [docs/security.md](docs/security.md#fail-closed-rules) lists the cases.

### Trust boundaries

15. Under `ferry serve`, `ferry.yaml` is read from the default branch, never from the change.
16. Service settings come from the environment or the switch directory, never from the repository.
17. Write access is checked in the webhook handler, before a job is queued. Apply gates run before
    credentials are minted, and again after.
18. The executor never receives the service's environment or secrets. Jobs get only their own
    environment and credential files.

### Secrets

19. No secret in argv, in a URL, in `.git/config`, or in a log line. Tokens reach git through
    `GIT_CONFIG_*` environment variables. Subprocess commands that involve a secret pass it by
    file or environment.
20. No secret, error text, username, commit, branch or URL in `/metrics`, `/healthz` or `/jobs`
    beyond the fields already there. Metric label values come only from the fixed sets in
    `metrics.py`.
21. Everything posted to a change goes through the `Redactor`, including error paths. Text that
    comes from a change or a host is escaped (`render.neutralize`, `render.inline`).
22. Error messages returned to API clients and printed by `ferry run` stay short and never echo
    upstream bodies.
23. The state file never stores tokens, passwords, keys or certificates.

### Compatibility

24. No new runtime dependencies. The package depends on `PyYAML`, and `cryptography` for the
    service.
25. Unknown `ferry.yaml` keys are an error. Adding a key changes the config digest, which
    invalidates existing plans. That is expected. Say so in the changelog.

## Changelog

`CHANGELOG.md` is written for the people who run ferry, not for the people who wrote it. Each
entry should tell an operator what changed for them and whether they need to do anything.

- **Every user-visible change gets an entry in the same pull request.** Features, behaviour
  changes, fixes, security fixes, new or changed flags, config keys, env vars, endpoints and
  metrics all count. Refactors, test-only and CI-only changes don't, unless they change behaviour.
- **Add entries under `## [Unreleased]` at the top.** Create that heading if it isn't there.
  A release renames it to `## [X.Y.Z] - YYYY-MM-DD` and adds the compare link (see
  [docs/development.md](docs/development.md)).
- **Group entries under `### Security`, `### Added`, `### Changed`, `### Deprecated`,
  `### Removed` and `### Fixed`**, in that order, and leave out empty groups.
- **Lead with the effect, in bold, and end with the issue or PR number:** "**A separate signing
  role for CLI certificates** (#69): ...". Then one or two sentences on what it means in practice,
  naming the exact flag, key or endpoint.
- **Spell out the upgrade impact.** If an operator has to do something (change config, re-plan,
  rotate a credential, change a CI pin), say so in the entry. Anything that breaks existing
  setups goes first in its group and starts with "**Breaking:**".
- **Security fixes say who is affected and what to do:** which versions, which setups, and
  "upgrade" or the workaround. Credit the reporter if they want it.
- **A release can open with a short paragraph** explaining the headline change before the
  groups, as 0.4.0 does.
- **Don't edit a published release's section** except to correct a fact. Never re-tag a
  published version; ship the fix in the next patch release instead.
- Same public-repo rule as everywhere else: no real hostnames, addresses, organisations or names.

## Working rules

- Add or change a test with every behaviour change. Both backends get every service change.
- When you add a flag, variable, config key, endpoint or metric: update
  [docs/configuration.md](docs/configuration.md), the topic page under `docs/`, and
  `CHANGELOG.md` under "Unreleased" (see [Changelog](#changelog)).
- Document only what exists in the code. Grep before you write it down.
- This is a public repository. Use `example.com` and example values. No real hostnames, addresses,
  organization names or people's names in code, docs, tests, commits or pull request text.
- Commits are signed. `main` takes squash merges of reviewed pull requests. Do not push to `main`.
- Record a design decision in [lode/decisions.md](lode/decisions.md) when you make one.
- Mermaid diagrams must render on GitHub. Validate them with `@mermaid-js/mermaid-cli` before
  committing.

## Documentation map

| Page | Topic |
|---|---|
| [README.md](README.md) | Overview and quickstart |
| [docs/concepts.md](docs/concepts.md) | Plan, apply gates, locks, scope, drift |
| [docs/drivers.md](docs/drivers.md) | GitLab CI or `ferry serve` |
| [docs/backends.md](docs/backends.md) | GitHub App, GitLab |
| [docs/serve.md](docs/serve.md) | The service |
| [docs/run.md](docs/run.md) | Operator runs |
| [docs/security.md](docs/security.md) | Security and threat model |
| [docs/configuration.md](docs/configuration.md) | Every key, variable and flag |
| [docs/operations.md](docs/operations.md) | Runbooks |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Refusal messages |
| [docs/development.md](docs/development.md) | Tests, lab, releasing |
| [deploy/local/README.md](deploy/local/README.md) | The local lab |
| [lode/](lode/lode-map.md) | Design notes |
