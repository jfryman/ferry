# Terminology

- **Plan**: `ansible-playbook --check --diff --limit <scope>` on an MR, posted as a comment with a hidden marker.
- **Marker**: `<!-- ferry:plan|apply {json} -->` in ferry's own comment; the plan record apply checks.
- **Scope**: the hosts/groups an MR's changed files affect; becomes `--limit`. "everything" = no limit.
- **Clean plan**: ansible rc 0/2 and every host ok or changed (none failed, unreachable or blocked).
- **Blocked host**: a host whose guard task failed with the busy marker (`FERRY_HOST_BUSY: ...`).
- **Inventory lock**: `ferry-lock-<inventory>` tag; held from plan until the MR merges/closes.
- **Drift**: check-mode changes on the default branch; `ansible_drift_changed_tasks > 0`.
- **Driver**: what calls the engine (CLI under GitLab CI today; webhook service later).
- **Backend**: a `VCSBackend` implementation (GitLab today).
