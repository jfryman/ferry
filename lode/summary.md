# ferry: summary

Atlantis-style plan/apply/drift for Ansible. The engine is independent of any one driver: it runs
from GitLab CI (`ferry plan|apply|drift|unlock`), or as a long-lived webhook service
(`ferry serve`) for a GitHub App or a GitLab project.

## State (v0.2.0)

- Engine, policy, scope (`--limit` from changed files), per-host results, drift textfile metrics.
- Drivers: GitLab CI (CLI, tag locks) and `ferry serve` (GitHub App or GitLab; SQLite locks in the
  service's state file; per-job OpenBao credentials: workload-identity login, throwaway SSH
  certificate, vault password).
- `ferry executor`: playbooks run as another user/container that can't read the service's secrets.
- Distribution: wheel on tags (GitLab CI, project PyPI registry); container image
  `ghcr.io/jfryman/ferry` (GitHub Actions, linux/amd64 + linux/arm64). The service and the executor
  are the same image with a different command. See practices.md for the release steps.

## Shape

```mermaid
flowchart LR
  CLI[ferry.cli\nGitLab CI driver] --> E[ferry.engine.Engine]
  SVC[ferry.service / gitlab_service\nferry serve] --> E
  SVC --> CR[credentials.py\nOpenBao per job]
  SVC --> WS[workspace.py\nexact-SHA checkouts]
  E --> VCS[VCSBackend\nvcs/github.py, vcs/gitlab.py]
  E --> L[LockStore\nlocks.py: SQLite / GitLab tags / memory]
  E --> R[Runner\nrunner.py, or executor.py over a socket]
  R --> CB[callback ferry_events\nJSON lines]
  CB --> RES[results.py\nper-host summary]
  E --> SC[scope.py\nfiles -> --limit]
  E --> P[policy.py]
  E --> RE[render.py + sanitize.py\ncomments]
```

See [decisions.md](decisions.md) for why it's built this way, README for user-facing behavior.
