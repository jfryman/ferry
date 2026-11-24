# ferry

ferry is plan, apply and drift detection for Ansible, driven from pull requests. It follows the
[Atlantis](https://www.runatlantis.io/) model:

- A pull request gets a **plan**: `ansible-playbook --check --diff`, limited to the hosts the
  change affects, posted as a comment.
- An approved pull request whose plan is current can be **applied** with a comment or a CI job.
- An **inventory lock** keeps two pull requests from interleaving on the same hosts.
- A scheduled **drift** run reports hosts that no longer match the default branch.

It works with GitHub (as a GitHub App) and GitLab (as a webhook service or from GitLab CI).

## Why

Ansible has no plan file and no built-in review step. Teams end up running playbooks from laptops,
or from CI jobs that anyone with push access can rewrite. ferry puts the run behind the pull
request:

- Nothing is applied that was not planned, reviewed and approved at that exact commit.
- The service holds the SSH credentials. Its rules come from the default branch, so a pull request
  cannot change how it is handled.
- Each job gets its own short-lived SSH certificate from OpenBao, and every run is recorded.

## Quickstart (5 minutes)

The local lab runs the whole loop in containers: `ferry serve`, OpenBao, a managed host that trusts
the SSH CA, and a fake GitHub. It needs Docker or rootless podman, plus bash, git, curl and openssl.

```bash
git clone https://github.com/jfryman/ferry.git
cd ferry
deploy/local/up.sh             # build and start the lab
deploy/local/lab.sh open       # a PR is opened: ferry plans it
deploy/local/lab.sh comments   # read the plan comment
deploy/local/lab.sh apply      # refused: no approval yet
deploy/local/lab.sh approve
deploy/local/lab.sh apply      # applied
deploy/local/lab.sh motd       # the change is on the host
deploy/local/down.sh
```

The [lab guide](deploy/local/README.md) explains each step and lists more scenarios. To run ferry
against your own repository, start with [Drivers](docs/drivers.md).

## Features

- Plan comments with sanitized diffs, per-host results and a signed plan record.
- Apply gates checked against live state: head commit, plan freshness, approvals, mergeability,
  branch protection, the lock. See [Concepts](docs/concepts.md#apply-gates).
- Scope: changed files are mapped to hosts and groups, so a plan covers only what the change
  touches.
- Per-inventory locks, held from plan until merge or close. Lock checks fail closed.
- Drift detection with per-host Prometheus metrics. ferry alerts; it does not remediate.
- `ferry serve`: a webhook service for a GitHub App or a GitLab project, with `/ferry plan`,
  `/ferry apply`, `/ferry unlock` and `/ferry help` comment commands.
- `ferry executor`: playbooks run in a separate container that cannot read the service's secrets.
- Per-job OpenBao credentials: a workload-identity login, a throwaway SSH key signed by the SSH CA,
  and the ansible-vault password.
- `ferry run`: an operator runs a playbook from their own machine through the service's lock,
  signer and audit trail.
- Commit statuses, so branch protection can require an apply before merge.
- Kill switches for applies and automatic plans, a job history, and Prometheus metrics.
- A multi-arch container image, `ghcr.io/jfryman/ferry`.

## Documentation

| Page | What it covers |
|---|---|
| [Concepts](docs/concepts.md) | Plan, apply, apply gates, locks, scope, drift, busy hosts, policy checks, output redaction |
| [Drivers](docs/drivers.md) | GitLab CI or `ferry serve`: which to pick, and how to set up the CI driver |
| [Backends](docs/backends.md) | GitHub App permissions, GitLab token and webhook, branch protection and rulesets |
| [`ferry serve`](docs/serve.md) | Deploying the service: image, webhooks, comment commands, credentials, executor, endpoints |
| [`ferry run`](docs/run.md) | Operator runs from a workstation: identity, authorization, leases, audit |
| [Security model](docs/security.md) | Trust boundaries, threat model, the decisions behind them, known limits |
| [Configuration reference](docs/configuration.md) | `ferry.yaml` keys, environment variables, CLI flags, HTTP endpoints |
| [Operations](docs/operations.md) | Runbooks: freeze applies, stuck locks, backups, metrics, alerts, upgrades |
| [Troubleshooting](docs/troubleshooting.md) | Refusal messages and what to do about them |
| [Development](docs/development.md) | Tests, the local lab, merging and releasing |
| [AGENTS.md](AGENTS.md) | For coding agents: repository layout, invariants, where things are |
| [Changelog](CHANGELOG.md) | Release notes |

## Commands

```text
ferry plan       plan a merge request (CI driver)
ferry apply      apply a merge request (CI driver)
ferry drift      check mode on every host of the default branch; write drift metrics
ferry unlock     release the locks of merged merge requests (CI driver)
ferry scope      print the --limit a set of changed files maps to
ferry policy     run the static policy checks
ferry serve      the webhook service (GitHub App or GitLab)
ferry executor   run the service's playbooks as a separate user
ferry switch     switch applies or automatic plans off and on for a running service
ferry run        an operator run through the service
```

Flags are listed in the [configuration reference](docs/configuration.md#cli).

## Install

The container image runs the service and the executor:

```bash
docker pull ghcr.io/jfryman/ferry:latest
```

For CI jobs or a workstation, install from a release tag. ferry needs Python 3.11 or newer and
`ansible-core`:

```bash
pip install 'git+https://github.com/jfryman/ferry@vX.Y.Z' 'ansible-core~=2.20.0'
```

The project is not published on PyPI. A different package there is named `ferry`, so do not
install it by bare name.

## License

MIT. See [LICENSE](LICENSE).
