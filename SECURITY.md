# Security policy

ferry runs Ansible as root on the machines it manages, so security bugs here can be serious. Thanks for reporting them responsibly.

## Reporting a vulnerability

**Please don't open a public issue for a security problem.**

Report it privately through GitHub: go to the repository's **Security** tab and choose **Report a vulnerability**. This opens a private advisory that only the maintainers can see.

Please include:

- what you found and why it's a security issue (for example: an apply without the required approval, a lock bypass, credentials showing up in logs, a webhook that isn't authenticated);
- the ferry version (`pip show ferry` or the image tag), the backend (GitHub or GitLab) and the driver (CI, `ferry serve` or `ferry run`);
- steps to reproduce, or a proof of concept against a lab setup;
- whether you've told anyone else.

Never include real secrets, tokens or hostnames from your environment. Redact them.

## What to expect

- We'll acknowledge your report within **5 working days**.
- We'll confirm whether it's a vulnerability, agree on a severity with you, and keep you updated while we work on a fix.
- Once a fixed release is out, we publish the advisory and credit you if you'd like.

This is a small project maintained on a best-effort basis, so these are targets, not guarantees.

## Supported versions

Only the latest minor release gets security fixes. Upgrade to the latest release before reporting if you can.

| Version | Supported |
|---|---|
| 0.4.x | yes |
| < 0.4 | no |

### Known advisory: approvals in 0.1.x

In 0.1.x, the GitLab backend counted any approval on a merge request, including an approval given before a later push. Head-aware approvals arrived in 0.2.0. If you run 0.1.x anywhere, including a pinned version in CI, upgrade.

## Scope

In scope: the ferry code in this repository, its container image, and the example deployments under `deploy/`.

Out of scope: bugs in Ansible, OpenBao or Vault, GitHub, GitLab, or your own playbooks and inventories. Report those to their maintainers. Misconfiguration that the docs already warn against (for example, disabling TLS or host-key checking outside a lab) isn't a vulnerability, but tell us if the docs could make the warning clearer.

## Security model

How ferry decides who may plan and apply, how locks and approvals work, and what it trusts is all in [docs/security.md](docs/security.md).
