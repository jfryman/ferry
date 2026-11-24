# Development

How to work on ferry: set up, test, run the lab, merge and release. Coding agents should also read
[AGENTS.md](../AGENTS.md).

## Set up

ferry needs Python 3.11 or newer. The tests use the real `git` and `ssh-keygen`.

```bash
git clone https://github.com/jfryman/ferry.git
cd ferry
pip install -e '.[test]' 'ansible-core~=2.20.0'
python -m pytest -q
```

Runtime dependencies are kept minimal on purpose: `PyYAML`, plus `cryptography` for the service.
Code host APIs, OpenBao and the metrics endpoint are written against the standard library.

## Layout

| Path | What |
|---|---|
| `src/ferry/engine.py` | Plan, apply, drift, unlock. No CI or HTTP knowledge. |
| `src/ferry/cli.py` | The CLI and the GitLab CI driver. Builds the service from the environment. |
| `src/ferry/service.py`, `gitlab_service.py` | `ferry serve`: webhook intake, queue, worker |
| `src/ferry/vcs/` | The backends: `github.py`, `github_protection.py`, `gitlab.py`, and the `VCSBackend` interface in `base.py` |
| `src/ferry/locks.py`, `state.py` | Lock stores, and the service's state file |
| `src/ferry/runs.py`, `run_identity.py`, `operator.py` | `ferry run`: the API, identity verification, the client |
| `src/ferry/executor.py`, `runner.py`, `credentials.py`, `workspace.py`, `galaxy.py` | Running Ansible: the executor, per-job credentials, checkouts, collections |
| `src/ferry/scope.py`, `policy.py`, `render.py`, `sanitize.py`, `results.py`, `metrics.py` | Scope, policy, comments, redaction, results, metrics |
| `tests/` | Unit tests with fake code hosts and real git |
| `deploy/local/` | The local lab |
| `deploy/nomad/` | An example Nomad job |
| `lode/` | Design notes: [decisions](../lode/decisions.md), [practices](../lode/practices.md), [terminology](../lode/terminology.md) |

## Tests

```bash
python -m pytest -q                      # everything
python -m pytest -q tests/test_engine.py # one file
```

Conventions:

- Every engine behaviour change gets a test in `tests/test_engine.py`, using `FakeVCS` and
  `FakeRunner` from `tests/fakes.py`.
- **Every apply gate has a refusal test.** Do not add a gate without one.
- Service intake changes get a test per backend: `tests/test_service.py` (GitHub) and
  `tests/test_gitlab_service.py` (GitLab).
- GitLab API shape changes get a test in `tests/test_gitlab.py`.
- The callback plugin is covered by a real `ansible-playbook` run over a local connection. Keep it.

CI runs the tests on Python 3.12 and 3.13 for every pull request and push to `main`, and checks
that `ferry.__version__` matches `pyproject.toml`.

## The local lab

The lab is the integration test. It runs `ferry serve`, the executor, OpenBao (JWT login, SSH CA,
vault password), an sshd host that trusts the CA, and a fake GitHub, all in containers.

```bash
deploy/local/up.sh             # build and start; re-run to start from scratch
deploy/local/lab.sh open       # walk the loop
deploy/local/lab.sh security   # the security scenarios
deploy/local/lab.sh run        # an operator run
deploy/local/lab.sh logs       # the service log
deploy/local/down.sh
```

Run it again after any change to the service, credentials, executor or workspace code. The
[lab guide](../deploy/local/README.md) has the full walkthrough, the rootless podman notes, and
experiments to try.

## Documentation

- User documentation lives in `docs/`, one topic per page. The README stays short.
- Document only what exists in the code. Check flags, keys, variables and endpoints against the
  source before writing them down.
- Use example values: `example.com`, `example-org`. No real hostnames, addresses or organization
  names.
- Mermaid diagrams must render on GitHub. Quote labels that contain punctuation, and avoid `;`
  and angle brackets in message text.

## Merging

- `main` takes squash merges of reviewed pull requests.
- Tests must pass. Run the local lab once before merging anything that touches the service.
- Delete merged branches.
- This is a public repository: no environment-specific hostnames, addresses or organization names
  in code, docs, commits or release notes.

## Releasing

Versions follow semver. A new feature or environment variable is a minor bump. Fixes only are a
patch.

1. In a pull request: bump `version` in `pyproject.toml` and `__version__` in
   `src/ferry/__init__.py`, and add a `CHANGELOG.md` section with the compare link. Merge it.
2. Tag the merge commit on `main` with an annotated tag and push it:

   ```bash
   git tag -a vX.Y.Z -m "ferry X.Y.Z"
   git push origin vX.Y.Z
   ```

3. The `image` workflow checks that the tag matches `pyproject.toml`, then pushes
   `ghcr.io/jfryman/ferry:vX.Y.Z`, `X.Y` and `latest`.
4. Create the GitHub release from the changelog section:

   ```bash
   gh release create vX.Y.Z --title "ferry X.Y.Z" --notes-file notes.md
   ```

5. Verify the image: `docker manifest inspect ghcr.io/jfryman/ferry:vX.Y.Z` lists linux/amd64 and
   linux/arm64.

More detail, including the image workflow, is in [lode/practices.md](../lode/practices.md).
