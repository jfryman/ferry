# Terminology

- **Plan**: `ansible-playbook --check --diff --limit <scope>` on an MR, posted as a comment with a hidden marker.
- **Marker**: `<!-- ferry:plan|apply {json} -->` in ferry's own comment; the plan record apply checks.
- **Scope**: the hosts/groups an MR's changed files affect; becomes `--limit`. "everything" = no limit.
- **Clean plan**: ansible rc 0/2 and every host ok or changed (none failed, unreachable or blocked).
- **Blocked host**: a host whose guard task failed with the busy marker (`FERRY_HOST_BUSY: ...`).
- **Inventory lock**: held from plan until the MR/PR merges or closes. Under CI it's a `ferry-lock-<inventory>` tag; under `ferry serve` it's a row in the service's SQLite file.
- **Drift**: check-mode changes on the default branch; `ansible_drift_changed_tasks > 0`.
- **Driver**: what calls the engine: the CLI under GitLab CI, or `ferry serve` (webhook service).
- **Backend**: a `VCSBackend` implementation (GitLab, GitHub).
- **Executor**: `ferry executor`, which runs the playbooks for `ferry serve` as another user or container.
- **Plan rule**: plans run automatically only for authors and pushers with write access. Anyone else needs `/ferry plan`.
