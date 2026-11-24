# `ferry run`

`ferry run` lets an operator run a playbook from their own machine, through the running service's
inventory lock, SSH signer and audit trail, without a pull request.

**It is the break-glass path.** It skips the pull request workflow: no review, no approval, no
plan to compare against. Use it when that path is unavailable or too slow for an incident, and
follow up with a pull request that records what changed. What still holds:

- The caller must be on the service's allow-list and on the inventory's `run_allowed`.
- An apply must be at the default-branch head, so only merged code is applied.
- The inventory lock is shared with pull request jobs and drift.
- The apply switch stops CLI runs.
- Every run is audited.

The operator never holds a long-lived SSH credential. The service signs a short certificate for
one run, after it has verified who is asking.

- [Install](#install)
- [Usage](#usage)
- [What the CLI does](#what-the-cli-does)
- [Enabling the run API](#enabling-the-run-api)
- [Identity](#identity)
- [Authorization](#authorization)
- [Leases and locks](#leases-and-locks)
- [Audit](#audit)
- [The run API](#the-run-api)
- [Limits you should know](#limits-you-should-know)
- [Demo login with an admin-created token](#demo-login-with-an-admin-created-token)

## Install

With Homebrew, on macOS or Linux:

```bash
brew install jfryman/tap/ferry
```

Without Homebrew, use pipx with a release tag. Don't install `ferry` from PyPI: that is an
unrelated project.

```bash
pipx install 'git+https://github.com/jfryman/ferry@vX.Y.Z'
```

Neither installs Ansible. Install the `ansible-core` version your repository pins, for example
`pipx install 'ansible-core~=2.20.0'`, so that `ansible-playbook` is on your `PATH`. The other
requirements are below.

## Usage

```bash
# Check mode with diffs (the default)
ferry run -i staging --service https://ferry.example.com site.yml

# Apply. Explicit, and only at the default-branch head.
ferry run -i staging --service https://ferry.example.com --apply \
  --reason "restore DNS after failed upgrade" site.yml

# Limit to some hosts
ferry run -i staging --service https://ferry.example.com --limit web1 site.yml
```

| Flag | Meaning |
|---|---|
| `playbook` | Path to the playbook, inside the repository |
| `-i`, `--inventory NAME` | Inventory name from `ferry.yaml`. Defaults to the first inventory. |
| `--limit PATTERN` | Passed to `ansible-playbook --limit` |
| `--apply` | Run for real. Without it, the run is `--check --diff`. |
| `--reason TEXT` | Why the run is needed. Optional. Recorded in the audit, at most 256 characters. |
| `--service URL` | The service's base URL. Default: `FERRY_SERVICE_URL`. |
| `--token-command CMD` | Command that prints an identity JWT. Default: `FERRY_RUN_TOKEN_COMMAND`, else `bao read -field=token identity/oidc/token/ferry`. Parsed without a shell. |
| `--insecure-http` | Throwaway lab only: allow plain HTTP to a non-local service |
| `--no-host-key-checking` | Throwaway lab only: do not check SSH host keys |

The last two flags are separate. Neither implies the other. The global `--repo DIR` flag selects
the repository checkout (default: the current directory).

Requirements on the workstation: `ansible-playbook`, `git`, `ssh-keygen`, and a way to get an
identity token (by default the `bao` CLI, logged in).

## What the CLI does

1. Refuses a dirty tree, untracked files included.
2. Fetches `origin` and refuses if `HEAD` is not on an `origin` branch. Only published commits
   run.
3. Generates a throwaway ed25519 key in a private temporary directory.
4. Runs the token command and calls `POST /api/runs` with the inventory, limit, mode, commit,
   playbook path, reason and public key.
5. Receives the certificate, the vault password, the inventory path and a lease.
6. Runs `ansible-playbook` with a minimal environment: no inherited SSH agent, OpenBao token or
   Ansible configuration, a temporary `HOME`, a private SSH ControlPath, and a copy of your
   `~/.ssh/known_hosts`.
7. Renews the lease while the playbook runs. If renewal fails, or the lease deadline passes, it
   terminates Ansible.
8. On exit, SIGINT or SIGTERM: reports the result to the service and deletes the credential
   directory.

A SIGKILL cannot run cleanup. The service then expires the lease on its own.

**Output appears after Ansible exits.** The CLI buffers stdout and stderr together, up to 1 MiB,
and redacts the whole output before printing: the vault password, the certificate, the private
key, the built-in patterns, and your `ferry.yaml` `redact` settings. If the output exceeds the
limit, all of it is withheld and a truncation notice is printed. Buffering is deliberate, since
multi-line secrets cannot be redacted safely in a stream.

Use HTTPS, and pin SSH host keys in `~/.ssh/known_hosts`. Plain HTTP is accepted only for
`localhost`, or with `--insecure-http`.

## Enabling the run API

The API is off by default. `POST /api/runs` answers 404 unless **both** of these are set on the
service:

| Variable | Value |
|---|---|
| `FERRY_RUN_OIDC_ISSUER` | The identity issuer, for example `https://bao.example.com/v1/identity/oidc` |
| `FERRY_RUN_OIDC_AUDIENCE` | The audience the identity tokens carry |

It also needs OpenBao credentials configured (`FERRY_OPENBAO_ADDR`). Without them a start answers
503.

Other settings:

| Variable | Default | Meaning |
|---|---|---|
| `FERRY_RUN_ALLOWED_SUBJECTS` | empty | Comma-separated subjects (OpenBao entity IDs) allowed to use the API |
| `FERRY_RUN_ALLOWED_GROUPS` | empty | Comma-separated group names, matched against the token's signed `groups` claim |
| `FERRY_RUN_LEASE_SECONDS` | `300` | Lease length. Renewal never extends past the certificate's expiry. |
| `FERRY_RUN_CERT_TTL` | `15m` | The TTL requested for CLI certificates. Also the longest a run can last. |
| `FERRY_RUN_NAME_CLAIM` | `name` | The signed claim used as a display name |
| `FERRY_RUN_SSH_ROLE` | the service's role | The SSH signing role for CLI certificates, on the same mount |
| `FERRY_RUN_SSH_PRINCIPAL` | the service's principal | The principal requested for CLI certificates |

### A separate signing role for CLI certificates

The service's own role is built for where the service runs. Most deployments pin its
`source-address` to the service's network, so a CLI certificate signed with it is refused by sshd
when used from an operator's machine ("valid certificate but not from a permitted source address").
Set `FERRY_RUN_SSH_ROLE` to a second role meant for operators, and keep the service's role as narrow
as it was. The operator networks are configuration in OpenBao, not in ferry:

```hcl
resource "vault_ssh_secret_backend_role" "ferry_run" {
  backend                 = "ssh"
  name                    = "ferry-run"
  key_type                = "ca"
  allow_user_certificates = true
  allowed_users           = "ferry"   # the principal hosts already trust, or a new one
  default_user            = "ferry"
  ttl                     = "900"     # matches FERRY_RUN_CERT_TTL
  max_ttl                 = "1200"
  default_extensions      = { "permit-pty" = "" }
  # Where operators run from (office LAN, VPN pool, tailnet), not the service's network.
  default_critical_options = { "source-address" = "192.0.2.0/24,198.51.100.0/24" }
}
```

The service's OpenBao policy needs `update` on `ssh/sign/ferry-run` too, with the same
`allowed_parameters` limits as on its own role. Operators never get signing rights on either role:
only the service signs, after its checks and the lock. A separate principal (for example
`ferry-operator`) lets hosts trust CLI runs separately from webhook jobs.

## Identity

The operator sends an **identity JWT**, never an OpenBao API token. The service checks, on every
call:

- The algorithm is RS256. Nothing else is accepted, and the token cannot choose a key URL.
- The signature, against the keys at `<issuer>/.well-known/keys`.
- The issuer and audience match the service's settings.
- The token is not expired. Expiry has no clock-skew allowance. `iat` and `nbf` allow 30 seconds.

The issuer is OpenBao's identity OIDC issuer, not its login endpoint. Configure the identity key
as RS256.

**Key caching.** The service caches the issuer's keys for 5 minutes. A token with an unknown `kid`
triggers at most one refetch every 30 seconds. If the issuer is unreachable, keys fetched within
the last hour are still used. After that, runs are refused.

No webhook signature authorizes the run API, and no identity token authorizes the webhook.

## Authorization

Two checks, both required. The default is deny.

1. **Service allow-list.** The token's `sub` is in `FERRY_RUN_ALLOWED_SUBJECTS`, or one of its
   `groups` is in `FERRY_RUN_ALLOWED_GROUPS`. Empty entries and empty group claims never match.
2. **Inventory allow-list.** The subject or one of its groups is listed in `apply.run_allowed` for
   the inventory, in the default branch's `ferry.yaml`:

```yaml
inventories:
  - name: staging
    path: inventories/staging
    apply:
      run_allowed: [operators]  # check and apply; an empty list refuses both
apply:
  run_allowed: []               # the default for inventories without an override
```

`run_allowed` covers both check and apply. Check or apply is chosen by the client and is not
enforced by the certificate: both modes receive the same inventory credential.

**Apply runs only the default-branch head.** For `--apply`, the commit must be the current head of
the default branch as the service fetched it. Otherwise the service answers 409. Check mode may
name any published commit.

**Switches.** `ferry switch apply off`, or `FERRY_ALLOW_COMMANDS=plan`, refuses all new CLI runs,
check included, because the mode is client-chosen. It does not revoke credentials already issued.

## Leases and locks

A run takes the same inventory lock as a pull request:

- A lock held by a change refuses the run (409).
- A lock held by a run refuses plans and applies of changes.

The run holds a lease, which the CLI renews. When a lease expires, the service releases the lock
and records the run as `error`. Leases are stored in the state file and survive a restart. The
state file holds no tokens or passwords.

The certificate's actual expiry is the hard cap on a run. A certificate that comes back from
OpenBao longer than `FERRY_RUN_CERT_TTL` (plus 60 seconds) is refused.

Scheduled drift takes the same lock, so a drift run and an operator run cannot overlap. See
[Operations](operations.md#a-drift-lock-survives-a-restart) for drift locks after a restart.

## Audit

Every run leaves a record in two places.

**Job records**, in the state file and at `GET /jobs`. A run has an `issued` record when
credentials are returned, then a completion record: `ok`, `failed`, or `error` when the lease
expired. A start that is refused for a verified identity (subject not allowed, inventory not
allowed, inventory locked, apply not at the default-branch head) gets a `refused` record. Records
carry:

- `trigger: cli`
- `subject` (the stable `sub`) and `display_name`
- the run id, inventory, limit, mode and full commit
- the client-asserted `playbook` path
- the `reason`, if one was given
- the certificate **serial**, so sshd's log lines can be matched to the run

**Log lines**, one per event, from the `ferry.runs` logger:

```text
cli run issued: run="..." subject="..." name="..." inventory="staging" sha="..." mode="apply" ...
```

Events are `issued`, `ok`, `failed`, `expired` and `refused`. Values are JSON-quoted, and control
characters are removed from names and reasons, so a line cannot be forged. `issued` adds the
certificate serial and expiry. A request with an invalid identity token is logged, but creates no
job record.

Authorization and run ownership always use `sub`, never the display name. The display name is
stripped of control characters and bounded to 128 characters.

**The operator's identity never appears on the code host.** A change blocked by a run sees only
``an operator run (`run:<id>`)``. The full holder stays in the lock table and in `/jobs`.

Metric: `ferry_jobs_by_trigger_total{trigger="cli", action="plan|apply", result="..."}` counts
finished runs. Names are never metric labels.

## The run API

All calls need `Authorization: Bearer <identity JWT>` and a JSON body.

**`POST /api/runs`** starts a run.

| Field | Value |
|---|---|
| `inventory` | Inventory name. Optional: the first inventory if omitted. |
| `mode` | `check` or `apply` |
| `sha` | The full 40-character commit |
| `playbook` | Repository-relative path, at most 512 characters |
| `public_key` | An ed25519 SSH public key |
| `limit` | Optional, at most 1024 characters |
| `reason` | Optional. Stored with control characters removed, cut to 256 characters. Over 1024 characters is refused. |

Answers 201 with `run_id`, `certificate`, `vault_password`, `inventory_path`, `ssh_user` and
`lease_expires_at`. Credentials are returned once and are never stored by the service.

**`POST /api/runs/{id}/lease`** renews the lease. Answers `lease_expires_at`.

**`POST /api/runs/{id}/finish`** ends the run. Body: `ok` (boolean) and optionally `counts`, an
object with the integer keys `hosts`, `changed_tasks` and `failed_tasks`.

Only the subject that started a run may renew or finish it. A run id alone grants nothing.

| Status | Meaning |
|---|---|
| 400 | Malformed request |
| 401 | Missing or invalid identity token |
| 403 | Subject or inventory not allowed, a different subject, or runs disabled by a switch |
| 404 | Unknown run, or the API is not enabled |
| 409 | Inventory locked, apply not at the default-branch head, or the run is still pending |
| 503 | Credentials not configured, or the service could not complete the request |

Error bodies are short and never echo upstream errors.

## Limits you should know

- **The certificate is not scoped to the locked inventory.** It is valid wherever the signing
  role's principal is trusted, until it expires. Keep the TTL short and the principal narrow. See
  [issue #53](https://github.com/jfryman/ferry/issues/53).
- **Certificates cannot be revoked** by finishing a run or by missing a lease. An abandoned client
  may keep host access until the certificate expires, and established SSH sessions may outlive it.
  Lease expiry frees the lock, not the access.
- **A stolen identity JWT** can request credentials for the inventories its subject is allowed,
  until it expires, and can renew or finish that subject's runs.
- **The playbook runs as your local user.** It can read its own key and password, and any file
  your user can read. The minimal environment and temporary `HOME` are hygiene, not a sandbox. Use
  a dedicated operator account or container for code you do not trust.
- **Results are operator-reported.** The commit, limit, mode, playbook path and result are
  assertions by an authorized operator, not proof of what ran.
- **Redaction is a safety net.** Put `no_log: true` and `diff: false` on tasks that handle
  secrets.

Protect operator identities as privileged credentials, and watch sshd's certificate serials.

## Demo login with an admin-created token

The [local lab](../deploy/local/README.md#operator-runs-through-the-same-service) provisions a
complete demo. In outline, an OpenBao admin configures:

- an RS256 identity key and an identity role `ferry` with audience `ferry`,
- an entity, and an entity alias on the token auth mount,
- a token role `ferry-operator` whose `allowed_entity_aliases` is restricted to that alias, with a
  policy that can only read `identity/oidc/token/ferry`. It cannot sign SSH keys or read secrets.

Then:

```bash
# As the admin, once:
bao token create -role=ferry-operator -entity-alias=demo

# As the operator, with the token that printed:
bao login
bao read -field=token identity/oidc/token/ferry   # what ferry run calls by default
ferry run -i staging --service https://ferry.example.com site.yml
```

The identity token endpoint is a GET, so it is `bao read`, not `bao write`. A real deployment would
authenticate the same entity with an OIDC login. The service does not change.
