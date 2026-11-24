# Troubleshooting

ferry explains a refusal in a comment on the change. This page lists the common messages and what
to do. Messages say "MR" or "PR" depending on the driver.

Where to look, in order:

1. The comment on the change.
2. The service log. It has the full Ansible output and the reason for any `error` result.
3. `GET /healthz`, `GET /jobs` and `GET /metrics`. See [Operations](operations.md).

- [ferry did not react at all](#ferry-did-not-react-at-all)
- [Plan problems](#plan-problems)
- [Apply refusals](#apply-refusals)
- [Lock problems](#lock-problems)
- [Service problems](#service-problems)
- [`ferry run` problems](#ferry-run-problems)

## ferry did not react at all

Check the code host's webhook delivery log for the response ferry gave.

| Response | Cause | Fix |
|---|---|---|
| `401 bad signature` | The webhook secret does not match | Set the same secret on both sides |
| `202 not this service's repo` | The delivery is for another repository | Point the webhook at the right service |
| `202 draft: not planned` | Draft changes are not planned automatically | Mark it ready, or comment `/ferry plan` |
| `202 fork PRs are not planned` | Changes from forks are not planned automatically | Someone with write access comments `/ferry plan` after reading it |
| `202 waiting for /ferry plan` | The author or pusher has no write access. ferry posted a note. | Someone with write access comments `/ferry plan` |
| `202 autoplan disabled` | The autoplan switch is off | `/ferry plan`, or switch it on |
| `202 not a ferry command` | The comment does not start with `/ferry` | The command must be at the start of the first line |
| `202 stale event ...` | The event is older than 10 minutes | Do not redeliver. Comment or push again. |
| `200 duplicate delivery`, `200 duplicate comment` | Already handled | Nothing. Write a new comment to run again. |
| `503 queue full, try again later` | 50 jobs are waiting | Wait, then comment again |
| `413`, `411` | Body too large, or chunked | Check the proxy in front of ferry |
| No delivery at all | The webhook is missing, or the proxy does not route `POST /webhook` | Fix the webhook or the route |

## Plan problems

**"ferry doesn't plan automatically for authors without write access"**
This is the [plan rule](serve.md#the-plan-rule). Someone with write access reads the change and
comments `/ferry plan`.

**"`/ferry plan` didn't run: the PR head ... arrived after the comment"**
A push landed after the comment was written. Read the new commits and comment again.

**"ferry hasn't seen when the PR head ... arrived"**
The change is older than the service, or the state file was lost. Name the commit:
`/ferry plan --sha <commit>`.

**"this MR targets `X`; ferry only handles MRs into ..."**
The target branch is not the default branch and is not in `base_branches`. Retarget the change, or
add the branch to `base_branches` on the default branch.

**"policy check failed"**
The lines that follow name the file and line. Either fix the playbook, or allow the path in
`policy.check_mode_false_allow` on the default branch. See
[Policy checks](concepts.md#policy-checks).

**The plan is not clean**
A host failed, was unreachable, or was blocked by a busy guard. Fix the cause and plan again. A
plan that is not clean cannot be applied.

**The plan covers every host**
Scope widened. A changed file matched no narrower rule, or a play uses a complex host pattern. Run
`ferry scope FILE...` locally to see which file caused it. See [Scope](concepts.md#scope).

**The plan warns that the change is behind its base**
Rebase or merge the base branch into the change, then plan again. Apply will refuse until then.

## Apply refusals

| Message | Fix |
|---|---|
| "no ferry plan on this MR yet" | Plan first |
| "latest plan is for `...`, not the MR head" | A push came after the plan. Plan the new head. |
| "this run is for `...` but the MR head is `...`" | The apply was for an older commit. Run it for the newest. |
| "latest plan is N h old" | Plans expire after `apply.max_plan_age_hours`. Plan again. |
| "ferry.yaml changed since the plan" | Plan again |
| "scope differs from the plan's" | Plan again |
| "latest plan was not clean" | Fix the failing hosts, plan again |
| "the lock was released with `/ferry unlock` ... re-run the plan" | Plan again |
| "needs N approval(s) ..., has M" | Get an approval of the current head from someone with write access who did not contribute to the change |
| "the approval from `X` doesn't count: `X` pushed or authored commits" | Get an approval from someone else |
| "ferry couldn't tell who pushed to this MR" | The code host API failed. Try again. |
| "`X` is not in apply.allowed_users" | Someone listed must apply |
| "`X` may not apply here" | The user needs write access (Developer or higher on GitLab) |
| "MR is a draft", "has merge conflicts", "unresolved blocking discussions" | Resolve it on the code host |
| "MR is not mergeable (unknown)" or "(unchecked)", "(checking)", "(preparing)" | The code host has not computed mergeability yet. Try again shortly. |
| "MR is not mergeable (ci_still_running)" or "(ci_must_pass)" | The pipeline must pass first |
| "this MR is N commit(s) behind `X`" | Rebase or merge the base, then plan again |
| "name the inventory to apply (apply.require_inventory)" | `/ferry apply <inventory>` |
| "applies are disabled on this ferry: ..." | The apply switch is off. See [Operations](operations.md#freeze-applies). |
| A message naming a GitHub rule, check or App permission | See [Backends](backends.md#branch-protection-and-rulesets). If it names a missing App permission, grant it. |

**"`X`, `Y` not run"** after a bare apply: an earlier inventory was refused or did not apply
cleanly, and ferry stops at the first one. Fix it and apply again, or name the inventory.

**A new approval is needed after every push.** That is intended. Approvals count only for the
current head.

**The required `ferry/apply` status never turns green.** Check that `FERRY_COMMIT_STATUS=1` is
set and that the App has "Commit statuses: write". Without the permission, ferry logs it once and
stops posting statuses until restarted.

## Lock problems

**"inventory `X` is locked by !N (since ...); merge or close it first"**
Another open change holds the lock. Merge or close it, or comment `/ferry unlock` on that change.

**"... and ferry couldn't check whether !N is still open"**
The code host API failed while ferry checked the holder. The lock is kept on purpose. Try again.

**"inventory `X` is locked by an operator run (`run:...`)"**
A [`ferry run`](run.md) holds it. It ends when the run finishes or its lease expires. `GET /jobs`
shows who started it.

**"inventory `X` is locked by a scheduled job (`drift`)"**
A drift run is in progress. If the service was restarted mid-run, see
[Operations](operations.md#a-drift-lock-survives-a-restart).

**"`/ferry unlock`: this PR doesn't hold any inventory lock"**
Comment on the change that holds the lock, not the one that is blocked.

## Service problems

**The service exits at startup with "missing ..."**
A required variable is unset. See the [configuration reference](configuration.md#ferry-serve).

**The service exits at startup after retries**
It could not reach the code host. A 4xx (wrong token, wrong repository) exits at once without
retrying.

**"ferry executor: not separated from the service"**
The executor can read a path in `FERRY_EXECUTOR_MUST_NOT_READ`, or it runs as root. Fix the mounts
and the user.

**"### ferry plan: error" with "ferry couldn't finish"**
The job failed outside Ansible: a checkout, OpenBao, or the executor. The comment has a short,
redacted error. The service log has the rest.

**SSH fails with host key verification errors**
Host keys are checked. Mount `known_hosts` at `/etc/ssh/ssh_known_hosts` in the container that runs
the playbooks (the executor, if you use one).

**SSH fails partway through a long apply**
The certificate expired. Keep `FERRY_JOB_TIMEOUT_MINUTES` below the certificate TTL
(`FERRY_OPENBAO_SSH_TTL`), and raise the TTL if runs legitimately take longer.

**Collections fail to install**
Check `FERRY_GALAXY_SERVER` (requirements naming another source are refused) and
`FERRY_GALAXY_TIMEOUT_SECONDS`. For images that ship their collections, set `FERRY_GALAXY=skip`.

## `ferry run` problems

The CLI prints short errors on purpose. It never echoes service or token-command output.

| Message | Cause |
|---|---|
| "refusing dirty tree (including untracked files)" | Commit or remove local changes |
| "HEAD is not on origin" | Push the commit first |
| "HTTPS required" | Use an `https://` service URL |
| "identity token command failed" | The token command failed or printed nothing. Run it by hand. With the default command, log in with `bao login` first. |
| "service refused request (HTTP 401)" | The identity token is invalid or expired, or its issuer or audience does not match the service |
| "service refused request (HTTP 403)" | The subject is not allowed by the service or for this inventory, or applies are switched off |
| "service refused request (HTTP 404)" | The run API is not enabled, or the proxy does not route `/api/runs` |
| "service refused request (HTTP 409)" | The inventory is locked, or `--apply` was not at the default-branch head. Merge, pull and retry. |
| "service refused request (HTTP 503)" | OpenBao is not configured or not reachable from the service |
| "output truncated (1 MiB limit; captured output withheld)" | The run produced more than 1 MiB of output. Use `--limit`, or lower verbosity. |
| "finish unavailable; service lease will expire" | The result could not be reported. The lock frees itself when the lease expires. |
