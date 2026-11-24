# Drivers

A driver is what calls ferry's engine. The engine (plan, apply, drift, unlock) is the same code in
both. There are two drivers:

- **`ferry serve`**: a long-running webhook service. This is the Atlantis-style deployment and the
  one to use for a team.
- **GitLab CI**: the `ferry` CLI run from pipeline jobs. Simple to adopt, with a weaker trust
  model.

## Which one

| | `ferry serve` | GitLab CI driver |
|---|---|---|
| Code hosts | GitHub (App), GitLab | GitLab |
| Triggered by | Webhooks and `/ferry` comments | Pipeline jobs |
| Who holds the SSH credential | The service | The CI runner |
| Where `ferry.yaml` is read from | The default branch | The change's own checkout |
| Locks | SQLite file owned by the service | `ferry-lock-<inventory>` tags |
| Per-job OpenBao credentials | Yes | No (whatever the runner provides) |
| Playbooks isolated from service secrets | Yes, with `ferry executor` | No |
| Drift | Built-in schedule | A scheduled pipeline |
| Operator runs (`ferry run`) | Yes | No |
| Extra infrastructure | One service with persistent storage | None |

The important row is who holds the credential. In CI, anyone who can push a branch can also change
`.gitlab-ci.yml` and `ferry.yaml` on that branch, so the SSH credential is only as safe as push
access. That is acceptable for a single-owner repository. For a team, run the service. The
[security model](security.md) explains why.

## `ferry serve`

See [`ferry serve`](serve.md) for deployment and [Backends](backends.md) for the GitHub App and
GitLab setup.

## GitLab CI driver

### What it needs

| Needs | From |
|---|---|
| ferry and `ansible-core` installed in the job image | See [Install](../README.md#install) |
| GitLab API URL, project, merge request, commit, user | The standard CI variables: `CI_API_V4_URL`, `CI_PROJECT_ID`, `CI_MERGE_REQUEST_IID`, `CI_COMMIT_SHA`, `GITLAB_USER_LOGIN` |
| An API token for comments, approvals, members and lock tags | `FERRY_GITLAB_TOKEN`. ferry trusts plan records only from this token's user, so give ferry its own account or project access token. Tag locks need a role that may create and delete tags (Maintainer). |
| SSH access to the hosts | Whatever your runner provides |
| The default branch | `CI_DEFAULT_BRANCH`, or `FERRY_DEFAULT_BRANCH` to override |

Flags override the environment (`--api-url`, `--project`, `--mr`, `--sha`, `--user`), so the same
commands also work from a terminal.

### Jobs

| Job | Pipeline | Command |
|---|---|---|
| Plan | Merge request | `ferry plan` |
| Apply | Merge request, manual | `ferry apply` |
| Unlock | Default branch, after a merge | `ferry unlock` |
| Drift | Default branch, scheduled | `ferry drift` |

A starting point for `.gitlab-ci.yml`. Adapt the image and the SSH setup to your environment:

```yaml
workflow:
  rules:
    # Lock tags must never start pipelines.
    - if: $CI_COMMIT_TAG =~ /^ferry-lock-/
      when: never
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH

ferry:plan:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  script:
    - ferry plan --summary plan.json
  artifacts:
    when: always
    paths: [plan.json]

ferry:apply:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
      when: manual
  # A second guard next to the lock: one apply at a time.
  resource_group: ferry-apply
  script:
    - ferry apply --summary apply.json
  artifacts:
    when: always
    paths: [apply.json]

ferry:unlock:
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH && $CI_PIPELINE_SOURCE == "push"
  script:
    - ferry unlock

ferry:drift:
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH && $CI_PIPELINE_SOURCE == "schedule"
  script:
    - ferry drift
  artifacts:
    when: always
    paths: [ferry-drift/]
```

Notes:

- The plan comment tells the reader to run the manual `ferry:apply` job, so keep that job name.
- With several inventories, pass `--inventory NAME` to each command.
- `ferry unlock` releases the locks held by the merge requests that contained the merged commit.
  Until that pipeline runs, a merged merge request still holds its tag. The engine treats a merged
  holder as stale and takes the lock over, but
  [issue #62](https://github.com/jfryman/ferry/issues/62) is an open report of a plan refused in
  that window. If you hit it, wait for the default-branch pipeline.
- `ferry drift --no-report` skips writing the textfiles on the hosts. `--out DIR` changes where
  `drift.json` and `drift.prom` go (default `ferry-drift`).
- In the CI driver, a pending or running pipeline does not block the apply, because the apply job
  is part of that pipeline.

### Comment commands in CI

GitLab CI cannot start a job from a comment. The module `ferry.webhook` holds the pieces for a
small listener that turns a `/ferry plan` or `/ferry apply` note into a pipeline trigger with
`FERRY_ACTION`, `FERRY_MR_IID` and `FERRY_USER` (and `FERRY_INVENTORY` when one is named). The
triggered pipeline runs `ferry <action>`, and the engine checks every gate as usual. ferry trusts
`FERRY_USER` only in pipelines whose source is `trigger`.

There is no CLI entry point for this listener. If you want comment commands, `ferry serve` is the
supported way.

## The CLI against GitHub

`ferry plan` and `ferry apply` also run against GitHub with `--vcs github` (or `FERRY_VCS=github`),
for a service or a terminal:

| Needs | From |
|---|---|
| A token with contents read, and pull requests and issues write | `FERRY_GITHUB_TOKEN` |
| The lock file, on persistent storage | `--lock-db` or `FERRY_LOCK_DB` |
| The repository as `OWNER/NAME` | `--project`, `FERRY_GITHUB_REPO` or `GITHUB_REPOSITORY` |
| ferry's own login, for trusting plan records | `FERRY_GITHUB_BOT` (an App's `<slug>[bot]`). Without it, ferry asks `GET /user`. |
| The pull request, commit and user | `--mr`, `--sha`, `--user`. No CI variable is trusted for the user. |

Do not run ferry from `pull_request` workflows in GitHub Actions. That workflow file comes from the
pull request's branch, so a pull request could take the job's credentials. On GitHub, use
`ferry serve` with a GitHub App.
