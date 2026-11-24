# Operator runs

[Up: summary](summary.md) · [Decisions](decisions.md) · [User contract](../README.md#operator-runs-ferry-run)

- Service is the junction: only it signs, after identity, inventory authorization, switches and
  the shared SQLite inventory lock. OpenBao stores identity/tokens, never locks or job state.
- API is disabled without issuer + audience. Fixed RS256 verification uses issuer JWKS; no
  token-selected URL or algorithm. Allowlisted entity subject or signed groups plus explicit
  `apply.run_allowed` (both modes, inventory override) are required. Default deny.
- Short-term login is the lab's admin-created entity + token alias + constrained token role.
  Operator token policy mints identity JWTs only. Google OIDC is future login work, not a service
  dependency. OpenBao identity-token endpoint is GET (`bao read`, not `write`).
- Slow configuration/signing calls do not hold the active-lease mutex; renewals on other
  inventories remain available. In-flight signing retains its lock until bounded upstream
  calls return; a timed-out initial lease cannot be resurrected or return credentials.
- SQLite `runs` stores nonsecret lease metadata. Reaper expires persisted runs after restart.
  PR IDs remain integers; operator holders are `run:<id> (<subject>)`, never looked up as PRs.
- Signed certificate expiry is the hard lease cap; key ID is untouched, role-controlled.
  `/jobs` correlates sshd serial with subject, run ID, full SHA, inventory, limit and mode.
- Lease/finish require the same verified subject. A run ID isn't authorization. Credentials are
  returned once to the starting caller, never retained in SQLite.
- Operator CLI refuses dirty/unpublished code, generates a private temporary ed25519 key, renews
  leases, kills Ansible on lease failure and finishes/wipes in finally and SIGINT/SIGTERM.
- A local playbook can read everything its OS user can. Minimal env/temp HOME are not a sandbox.
  Dedicated operator containers/accounts are the recommended boundary; no inherited API tokens.
- Lease expiry/finish cannot revoke SSH certificates or established sessions. It frees the lock,
  not access. Short TTL + narrow principal are mandatory operational mitigations.
- Operator claims about SHA/mode/results are not execution attestation. An authorized identity
  is trusted to exercise its inventory credentials. Check mode is not a security sandbox.

[Local lab](../deploy/local/README.md): `run`, `run-apply`, `run-denied`, `run-contention`, `run-expiry`.

## Operator identity and routing
- Apply disable gates every new CLI run: check/apply is client-chosen and the certificate
  cannot enforce check mode. Existing credentials are not revoked by a switch.
- Authorization/ownership use `sub`. `FERRY_RUN_NAME_CLAIM` (default `name`) supplies a
  sanitized 128-character display name for lock holders and audit, never metric labels.
- `iat`/`nbf` permit 30 seconds skew; `exp` remains strict.
- One backend listener: ingress must expose only authenticated POST operator API routes,
  plus explicitly enabled code-host webhook routes. `/jobs` and `/metrics` stay private.

## Pending start and audit contract
- Starts persist `pending`; lease/finish refuse pending runs (409). After signing, the service
  verifies pending state and matching lock under the mutex before activation/credential return.
- Issuance appends an `issued` audit row before returning credentials; finish/expiry appends
  its own row. Metrics count completion only. Playbook is bounded repo-relative, client asserted.
- Scheduled drift acquires each inventory lock before credentials, records/skips held inventories,
  and releases its `drift` holder in finally. PR/operator paths refuse that string holder.
- CLI terminal streams are merged/drained and buffered up to 1 MiB then redacted (vault password/cert/private key,
  config patterns and built-ins, even short explicit secrets). On overflow withhold all text and
  report truncation. Whole-output processing safely covers arbitrary cross-line patterns; use
  no_log/diff:false for sensitive tasks regardless.

## Drift restart recovery
- Once per service lifetime, a serial-executor idle barrier acknowledges only after the previous
  job and cleanup finish. Only then release stale `drift` holders. Retry busy/unavailable probes;
  do not begin new scheduled drift before this succeeds. Never reap a new active drift.
- Without a sidecar, fail closed; a restarted service cannot prove old local Ansible is gone.
  Manual verified process shutdown precedes stale local lock removal. One service owns SQLite.
- Recheck apply switches immediately before signing and immediately before run activation/return.
