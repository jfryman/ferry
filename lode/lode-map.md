# Lode map

- [summary.md](summary.md): what ferry is, current state, module shape
- [decisions.md](decisions.md): design decisions and why
- [terminology.md](terminology.md)
- [practices.md](practices.md): how to change, test, merge, release; image CI
- `tmp/`: session scratch (git-ignored)

User-facing behavior (gates, locks, drift metrics, threat model) is in the top-level README.
- `deploy/local/README.md`: the local lab (full service loop, OpenBao, SSH certificates), how it maps to production, things to try
- `CHANGELOG.md`: release notes per version
- `.github/workflows/`: `ci.yml` (tests), `image.yml` (multi-arch image to GHCR)

- [operator-runs.md](operator-runs.md): operator identity, shared lease locks, credential and audit boundaries
