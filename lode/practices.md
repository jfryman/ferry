# Practices

- Every engine behavior change gets a test in `tests/test_engine.py` with `FakeVCS`/`FakeRunner`.
  GitLab API shape changes get one in `tests/test_gitlab.py` (`FakeGitLab` transport).
- Service intake changes get a test per backend: `tests/test_service.py` (GitHub) and
  `tests/test_gitlab_service.py` (GitLab, same shape: real git remote, fake API).
- The callback plugin is covered by a real `ansible-playbook` run (local connection); keep it.
- Never add a gate to apply without a matching refusal test (`test_apply_refusals`).
- Keep `VCSBackend` small; anything backend-specific is computed inside the backend
  (e.g. `blocked_reasons` from GitLab's `detailed_merge_status`).
- Local: `pip install -e . 'ansible-core~=2.20.0' pytest && python -m pytest -q`.
- **The local lab is the integration test.** `deploy/local/up.sh` stands up ferry serve + OpenBao
  (Nomad-style JWT, SSH CA, vault password) + an sshd host trusting the CA + a fake GitHub; walk the
  loop with `deploy/local/lab.sh` (guide: `deploy/local/README.md`). Re-run it after any change to
  the service, credentials or workspace code. It caught the cross-job SSH ControlPath leak (Nov 23).

## Merging

- `main` takes squash merges of reviewed PRs; feature stacks land into their feature branch first
  (merge commits are fine there), with `python -m pytest -q` green after each merge and the local
  lab run once at the end. Delete merged branches.
- GitHub Actions `ci` (`.github/workflows/ci.yml`) runs the tests on Python 3.12 and 3.13 for every
  PR and push to `main`, and checks `ferry.__version__` against `pyproject.toml`.
- Public repo: no environment-specific hostnames, addresses or organization names in code, docs,
  commits or release notes.

## Releasing

Semver. A new feature or env var is a minor bump; fixes only are a patch.

1. In a PR: bump `version` in `pyproject.toml` and `__version__` in `src/ferry/__init__.py`, and add
   a `CHANGELOG.md` section (`## [X.Y.Z] - date`, Added/Security/Changed/Fixed, PR numbers, plus
   the compare link at the bottom). Merge it.
2. Tag the merge commit on `main` with an annotated tag: `git tag -a vX.Y.Z -m "ferry X.Y.Z"`, then
   `git push <github-remote> vX.Y.Z`.
3. The `image` workflow checks that the tag matches `pyproject.toml`, then pushes
   `ghcr.io/jfryman/ferry:vX.Y.Z`, `X.Y` and `latest`.
4. `gh release create vX.Y.Z --title "ferry X.Y.Z" --notes-file <the changelog section>`.
5. Verify the image: `docker manifest inspect ghcr.io/jfryman/ferry:vX.Y.Z` lists linux/amd64
   and linux/arm64, and `docker pull` works anonymously.
6. The wheel: a GitLab CI pipeline (`.gitlab-ci.yml`) publishes it to its project's PyPI registry
   when the same tag reaches that copy of the repo. Consumers pin the version and install it with
   `--no-deps` from that registry only, because of the name clash with a public `ferry` package.

## Image CI (`.github/workflows/image.yml`)

- buildx + QEMU, `linux/amd64,linux/arm64`, one multi-arch index, with SBOM and provenance
  attestations (`sbom: true`, `provenance: mode=max`).
- Tags: `vX.Y.Z`, `X.Y`, `latest` on release tags; `edge` and `sha-<short>` on `main`. PRs that
  touch the image inputs build without pushing.
- OCI labels and annotations come from `docker/metadata-action`, and the `Dockerfile` carries
  `org.opencontainers.image.source`, which links the GHCR package to the repo.
- Pushes with `GITHUB_TOKEN` (`permissions: packages: write`). Every action is pinned by full commit
  SHA, with the version in a comment. Bump them deliberately.
- The service and `ferry executor` are the same image; only the command and the user differ.
