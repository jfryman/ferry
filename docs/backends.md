# Backends

A backend is ferry's adapter for a code host. There are two: GitHub and GitLab. Both support
`ferry serve`. The GitLab backend also supports the [CI driver](drivers.md#gitlab-ci-driver).

- [GitHub App](#github-app)
- [Branch protection and rulesets](#branch-protection-and-rulesets)
- [GitLab](#gitlab)
- [How code host concepts map](#how-code-host-concepts-map)

## GitHub App

`ferry serve` talks to GitHub as a GitHub App installed on the config repository. It posts comments
as the App and fetches code with short-lived installation tokens.

### Create the App

1. Create a GitHub App and install it on the repository that holds your playbooks.
2. Set the webhook URL to `https://ferry.example.com/webhook` and set a webhook secret.
3. Subscribe to the **Pull request** and **Issue comment** events.
4. Grant the permissions below.
5. Generate a private key and give it to the service.

| App permission | Why |
|---|---|
| Contents: read | Fetch the default branch and the pull request head |
| Pull requests: write | Read pull requests, reviews and the review decision |
| Issues: write | Post plan and apply comments |
| Metadata: read (always granted) | Collaborator permission, and the branch's active rulesets |
| Checks: read, Commit statuses: read | See which required checks passed on the head |
| Commit statuses: write | Only with `FERRY_COMMIT_STATUS=1` |
| Administration: read | Only if the target branch uses classic branch protection. ferry refuses to apply until it can read the rules. |

Do not give the App a bypass on the branch's rulesets. It does not need one, and GitHub computes
the merge state for the viewer, so an App with bypass would see every pull request as mergeable.

### Service settings

| Variable | Value |
|---|---|
| `FERRY_GITHUB_REPO` | `OWNER/NAME` of the config repository (required) |
| `FERRY_GITHUB_APP_ID` | The App's id (required) |
| `FERRY_GITHUB_APP_KEY_FILE` | Path to the App's private key, PEM (required) |
| `FERRY_GITHUB_INSTALLATION_ID` | Optional. Looked up from the repository if unset. |
| `FERRY_WEBHOOK_SECRET_FILE` or `FERRY_WEBHOOK_SECRET` | The webhook secret (required) |
| `FERRY_DEFAULT_BRANCH` | Defaults to `main` |
| `GITHUB_API_URL`, `FERRY_GITHUB_GIT_URL` | For GitHub Enterprise Server |
| `FERRY_GITHUB_OWN_CHECKS` | Comma-separated names of required checks that only pass after ferry applies. See below. |

The remaining settings are shared with GitLab. See [`ferry serve`](serve.md).

### Events

| Event | What ferry does |
|---|---|
| `pull_request` opened, reopened, synchronize, ready_for_review | Plan, if the [plan rule](serve.md#the-plan-rule) allows |
| `pull_request` closed | Release the pull request's locks |
| `issue_comment` created, starting with `/ferry` | Run the [comment command](serve.md#comment-commands) |
| `ping` | Answer `pong` |

Webhooks are verified with `X-Hub-Signature-256` (HMAC-SHA256 of the body, compared in constant
time). Draft and fork pull requests are not planned automatically.

## Branch protection and rulesets

On apply, ferry asks GitHub whether the pull request could merge under the target branch's classic
branch protection and active rulesets. It refuses with a comment that names the unmet rule. ferry's
own approval check still applies. Both must pass.

ferry uses GitHub's own verdict where GitHub computes one, and reads the rules to name what is
unmet:

| Rule | How ferry checks it |
|---|---|
| Required approving reviews, CODEOWNERS review, dismiss stale reviews, last-push approval, requested changes | GraphQL `reviewDecision` must be `APPROVED` when any review rule applies. The comment names the count and the code owners still requested. |
| Required status checks | Each check required by a rule, or marked `isRequired` by GitHub, must have passed (success, neutral or skipped) on the head, from the pinned App if the rule pins one. |
| Conversation resolution | No unresolved review threads. |
| Anything else (`mergeStateStatus` is `BLOCKED`) | If GitHub says blocked and none of the above explains it, ferry refuses and lists the rules it cannot evaluate (merge queue, required deployments, signed commits, locked branch). |

**ferry's own required check.** A branch can require a check that only passes once ferry has
applied: a ferry [commit status](serve.md#commit-statuses), or a CI job that depends on the apply.
Requiring it would deadlock the apply. ferry therefore ignores a required check that was posted by
its own App, or whose name is listed in `FERRY_GITHUB_OWN_CHECKS`. That variable comes from the
service's environment, never from the repository. A `BLOCKED` merge state explained only by such a
check does not refuse the apply.

**Fails closed.** If ferry cannot read a rule source (a missing App permission, an API error, more
than 100 checks or review threads), it refuses and names the App permission to add.

**Gaps.**

- Checks are matched by name. A check with the right name from another App satisfies an unpinned
  required check, as it does on GitHub itself.
- Rules ferry cannot evaluate are trusted to GitHub's merge state, which is computed for the App.
- Required linear history, the merge method and push restrictions do not affect whether a pull
  request may merge, so they are not checked.
- "Require branches to be up to date" is GitHub's `behind` state, which ferry already refuses.

## GitLab

`ferry serve` runs against a GitLab project when `FERRY_VCS=gitlab` is set, or when
`FERRY_GITLAB_URL` is set and `FERRY_VCS` is not.

### Service settings

| Variable | Value |
|---|---|
| `FERRY_GITLAB_URL` | The GitLab base URL, for example `https://gitlab.example.com` (required) |
| `FERRY_GITLAB_PROJECT` | The config project, `GROUP/NAME` or its numeric id (required) |
| `FERRY_GITLAB_TOKEN_FILE` or `FERRY_GITLAB_TOKEN` | A project access token (required). The file is re-read on use, so the token can be rotated without a restart. |
| `FERRY_WEBHOOK_SECRET_FILE` or `FERRY_WEBHOOK_SECRET` | The webhook's secret token (required) |
| `FERRY_DEFAULT_BRANCH` | Defaults to the project's default branch |
| `FERRY_GITLAB_GIT_URL` | Optional. Defaults to the project's `http_url_to_repo`. |

**The token** needs role **Developer** and scopes **`api`** (notes, approvals, members) and
**`read_repository`** (checkouts). Developer is enough because the service keeps locks in its own
file, not in tags. The token reaches git only as an `http.extraHeader` in the environment. It is
never placed in a URL, in argv or in `.git/config`.

### Webhook

In the project, under **Settings > Webhooks**:

- URL: `https://ferry.example.com/webhook`
- A **Secret token**
- Triggers: **Merge request events** and **Comments**
- **Enable SSL verification** on

GitLab does not sign the body. It sends the secret in `X-Gitlab-Token`, which ferry compares in
constant time. The webhook must therefore use HTTPS. Deliveries for another project are ignored.

### Behaviour

- **Plan** runs on open, reopen, a push of new commits, and draft to ready, under the
  [plan rule](serve.md#the-plan-rule): automatically only for authors and pushers with Developer or
  higher. Drafts and fork merge requests (source project differs from target project) are not
  planned automatically. Close or merge releases the merge request's locks.
- **Notes are pinned to the head the commenter saw.** The hook's `merge_request.last_commit` must
  still be the head, and that head's diff version must predate the note. `--sha <commit>` names the
  commit instead.
- **Approvals count only for the current head.** GitLab does not record which commit an approval
  was for. ferry counts an approval only if its `approved_at` is at or after the moment the head
  arrived (the merge request diff version whose head is the current SHA). On a GitLab that does not
  report `approved_at`, approvals count only if the project removes approvals when commits are
  added. Otherwise none count, and the service logs why.
- **Mergeability** comes from `detailed_merge_status`. `unchecked`, `checking` and `preparing` mean
  GitLab has not decided yet: the apply is refused, and you retry. A pending or failed merge
  request pipeline (`ci_still_running`, `ci_must_pass`) also refuses in the service. The CI driver
  ignores those two, because its apply job is part of that pipeline.
- **Who pushed** comes from GitLab's push events for the source branch. GitLab records the pusher
  itself. Commit author fields are whatever the committer typed.

## How code host concepts map

| ferry | GitHub | GitLab |
|---|---|---|
| Change | Pull request | Merge request |
| Comment | Issue comment | Note |
| May plan and apply | `write`, `maintain` or `admin` permission | Developer or higher |
| Approval of the head | The latest decisive review per user, if it approves the current head commit | An approval whose `approved_at` is not older than the head |
| Not mergeable | `mergeable_state` of `dirty`, `behind`, `draft` or `unknown`, plus the rule check above | A blocking `detailed_merge_status` |
| Behind the base | The compare API's `behind_by` | `diverged_commits_count` |
| Contributors whose approval does not count | Commit author and committer logins, plus pushers seen in webhooks | Push events to the source branch, plus pushers seen in webhooks |
| Checkout ref | `refs/pull/<n>/head` | `refs/merge-requests/<n>/head` |
