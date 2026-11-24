# Operations

Runbooks for running `ferry serve`.

- [Health check](#health-check)
- [Freeze applies](#freeze-applies)
- [Stop automatic plans](#stop-automatic-plans)
- [Release a stuck lock](#release-a-stuck-lock)
- [A drift lock survives a restart](#a-drift-lock-survives-a-restart)
- [Back up the state file](#back-up-the-state-file)
- [Rotate secrets](#rotate-secrets)
- [Metrics](#metrics)
- [Alerts](#alerts)
- [Job history](#job-history)
- [Upgrade](#upgrade)

The examples use `kubectl`. Use the equivalent `docker exec` or `nomad alloc exec` elsewhere. The
`curl` examples assume you are inside the private network, on the service's port.

## Health check

```bash
curl -s http://ferry:8080/healthz
```

```json
{"ok": true, "queued": 0, "last_job": {"job": "plan", "pr": 12, "ok": true, "seconds": 41.2},
 "switches": {"plan": true, "apply": true, "autoplan": true}}
```

`queued` is the number of jobs waiting. Jobs run one at a time, so a long apply delays everything
behind it.

## Freeze applies

For an incident, a change freeze, or a suspected compromise of a writer's account. The service
keeps running: locks are still released on close, and drift still runs.

```bash
kubectl -n ferry exec deploy/ferry -c ferry -- ferry switch apply off --reason "change freeze until Monday"
kubectl -n ferry exec deploy/ferry -c ferry -- ferry switch            # show the switches
kubectl -n ferry exec deploy/ferry -c ferry -- ferry switch apply on
```

- Takes effect at once. Applies already waiting in the queue are refused too.
- A refused `/ferry apply` gets a note on the change that includes the reason.
- It also refuses all new [`ferry run`](run.md) runs, check mode included.
- It does not revoke credentials already issued, and does not stop a job that is running.

`ferry switch` writes or removes a file named `disable-apply` in `FERRY_SWITCH_DIR` (default: the
directory of `FERRY_LOCK_DB`). Run it where that volume is mounted. The service reads the file on
every request and before each queued job. If the service cannot check the directory, applies and
automatic plans count as disabled.

For a permanent plan-only ferry, set `FERRY_ALLOW_COMMANDS=plan` and restart.

None of this is in `ferry.yaml`, so a merge cannot flip it.

## Stop automatic plans

```bash
kubectl -n ferry exec deploy/ferry -c ferry -- ferry switch autoplan off --reason "investigating"
```

Pushes no longer plan, and automatic plans waiting in the queue are dropped. `/ferry plan` still
works. The permanent form is `FERRY_DISABLE_AUTOPLAN=1`.

| Switch | How | Effect |
|---|---|---|
| `disable-apply` file | `ferry switch apply off` | Applies and CLI runs refused at once |
| `disable-autoplan` file | `ferry switch autoplan off` | No plans on push |
| `FERRY_ALLOW_COMMANDS=plan` | Environment, restart | Plan-only ferry |
| `FERRY_DISABLE_AUTOPLAN=1` | Environment, restart | Plans only on `/ferry plan` |

## Release a stuck lock

First find out who holds it. The refusal comment on the blocked change names the holder.

| Holder | What to do |
|---|---|
| An open change | Merge or close it, or comment `/ferry unlock` on **that** change (needs write access). Its plan stops counting. |
| A closed or merged change | Nothing. The next plan takes the lock over. If the code host was unreachable when ferry checked, retry. |
| An operator run (`run:<id>`) | It is released when the run finishes or its lease expires (`FERRY_RUN_LEASE_SECONDS`, 5 minutes by default, after the last renewal). `GET /jobs` shows who started it. |
| A scheduled job (`drift`) | A drift run in progress. It is released when the run ends. See the next section if it is not. |

Do not edit the state file while the service is running, except as the last resort described
below.

## A drift lock survives a restart

If the service is restarted during a drift run, the `drift` holder stays in the lock table.

- **With an executor:** the service recovers by itself. Once the executor confirms it is idle (the
  previous job and its cleanup have finished), the service releases stale `drift` holders. This
  happens once per service lifetime, and no new scheduled drift starts before it.
- **Without an executor:** the service cannot prove that the old Ansible process is gone, so it
  keeps the lock. Recover by hand:

  1. Stop the service.
  2. Verify that no `ansible-playbook` process from the old instance is still running.
  3. Remove the row, with the state volume mounted, and start the service:

     ```bash
     python -c 'import sqlite3; db = sqlite3.connect("/var/lib/ferry/state/locks.db"); db.execute("DELETE FROM locks WHERE holder = ?", ("drift",)); db.commit()'
     ```

## Back up the state file

`FERRY_LOCK_DB` is one SQLite file. It holds:

- the locks,
- seen webhook deliveries (7 days),
- when each change head arrived,
- the job history,
- operator run leases.

It holds no tokens or passwords.

**Losing it frees every lock.** A second change could then plan while the first is mid-flight.
After a loss on GitHub, ferry also no longer knows when existing heads arrived, so comment
commands on existing pull requests are refused until a new push arrives or the commenter uses
`--sha`.

Back it up with the volume. Run one service per file.

## Rotate secrets

| Secret | How |
|---|---|
| GitLab token | Replace the file at `FERRY_GITLAB_TOKEN_FILE`. It is re-read on use. No restart. |
| Webhook secret | Change it on the code host and in the service, then restart. Plan records are signed with a key derived from this secret, so existing plans stop counting: re-plan open changes. |
| GitHub App key | Replace the file and restart. |
| SSH CA, vault password | These live in OpenBao. ferry reads them per job. |

## Metrics

`GET /metrics`, Prometheus text format.

| Metric | Type | Labels |
|---|---|---|
| `ferry_jobs_total` | counter | `action` (plan, apply, unlock, drift), `result` (ok, failed, refused, skipped, error) |
| `ferry_jobs_by_trigger_total` | counter | `trigger` (webhook, schedule, cli), `action`, `result` |
| `ferry_job_duration_seconds` | histogram | `action` |
| `ferry_jobs_queued_total` | counter | `action` |
| `ferry_webhook_rejected_total` | counter | `reason` |
| `ferry_last_job_timestamp_seconds` | gauge | `action` |
| `ferry_queue_depth` | gauge | |
| `ferry_locks_held` | gauge | |
| `ferry_enabled` | gauge | `switch` (plan, apply, autoplan): 1 if enabled, 0 if switched off |
| `ferry_build_info` | gauge | `version` |

Job results:

| Result | Meaning |
|---|---|
| `ok` | Finished cleanly |
| `refused` | A gate said no. Nothing ran against the hosts. |
| `failed` | Ansible ran and was not clean |
| `skipped` | The change moved on or closed before the job ran |
| `error` | ferry could not finish. The service log says why. For a CLI run: the lease expired. |

Webhook reject reasons: `bad_auth`, `duplicate`, `stale`, `future`, `undated`, `bad_json`,
`foreign_repo`, `no_access`, `head_moved`, `disabled`, `queue_full`, `too_large`, `bad_request`.

Every label value comes from a fixed set. Usernames, commits, branches, error text, URLs and
credentials never appear in `/metrics`.

Per-host drift metrics are separate. ferry writes them on the managed hosts for node_exporter. See
[Drift](concepts.md#drift).

## Alerts

Starting points:

```promql
# ferry could not finish a job
increase(ferry_jobs_total{result="error"}[1h]) > 0

# jobs are piling up (use with "for: 15m")
ferry_queue_depth > 5

# someone is sending forged or replayed deliveries
increase(ferry_webhook_rejected_total{reason=~"bad_auth|stale"}[1h]) > 0

# applies have been switched off for a long time (use with "for: 24h")
ferry_enabled{switch="apply"} == 0

# a host has drifted from the default branch
ansible_drift_changed_tasks > 0

# the drift check has not run on a host for two days
time() - ansible_drift_last_run_timestamp_seconds > 172800
```

## Job history

```bash
curl -s 'http://ferry:8080/jobs?limit=20'
```

Returns the newest finished jobs as JSON: action, change number, short commit, user, inventory,
result, start time, duration and trigger. Operator runs add the subject, display name, run id,
mode, limit, playbook, reason and certificate serial. See [`ferry run`](run.md#audit).

The history is bounded (`FERRY_JOB_HISTORY`, default 200) and holds no error text or refusal
reasons. Those go on the change and into the service log.

## Upgrade

1. Read the [changelog](../CHANGELOG.md) for the versions you are crossing.
2. Wait for the queue to drain (`/healthz` shows `"queued": 0`), or switch applies off first.
3. Deploy the new image tag for both the service and the executor. Keep them on the same version.
4. Check `/healthz` and `ferry_build_info`.

The service runs as a single instance, so an upgrade is a short outage for webhooks. Do not count on
the code host to retry deliveries that arrive while it is down, and a manual redelivery more than
10 minutes late is refused as stale. Comment again, or push again.
A job that was running when the service stopped does not resume.
