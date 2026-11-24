# Practices

- Every engine behavior change gets a test in `tests/test_engine.py` with `FakeVCS`/`FakeRunner`.
  GitLab API shape changes get one in `tests/test_gitlab.py` (`FakeGitLab` transport).
- Service intake changes get a test per backend: `tests/test_service.py` (GitHub) and
  `tests/test_gitlab_service.py` (GitLab, same shape: real git remote, fake API).
- The callback plugin is covered by a real `ansible-playbook` run (local connection); keep it.
- Never add a gate to apply without a matching refusal test (`test_apply_refusals`).
- Keep `VCSBackend` small; anything backend-specific is computed inside the backend
  (e.g. `blocked_reasons` from GitLab's `detailed_merge_status`).
- Release: bump `pyproject.toml`, tag `vX.Y.Z`, CI publishes to project 98's PyPI registry.
  Consumers pin the version and install with `--no-deps` from that registry only (name clash with
  a public `ferry` package).
- Local: `pip install -e . 'ansible-core~=2.20.0' pytest && python -m pytest -q`.
- **The local lab is the integration test.** `deploy/local/up.sh` stands up ferry serve + OpenBao
  (Nomad-style JWT, SSH CA, vault password) + an sshd host trusting the CA + a fake GitHub; walk the
  loop with `deploy/local/lab.sh` (guide: `deploy/local/README.md`). Re-run it after any change to
  the service, credentials or workspace code. It caught the cross-job SSH ControlPath leak (Nov 23).
