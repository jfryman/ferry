# Lode map

- [summary.md](summary.md): what ferry is, current state, module shape
- [decisions.md](decisions.md): design decisions and why
- [terminology.md](terminology.md)
- [practices.md](practices.md): how to change, test, merge, release; image CI
- `tmp/`: session scratch (git-ignored)

User-facing behavior is in `docs/` (map in the top-level README): gates, locks and drift in
`docs/concepts.md`, the threat model in `docs/security.md`, settings in `docs/configuration.md`.
Rules for coding agents are in the top-level `AGENTS.md`.
- `deploy/local/README.md`: the local lab (full service loop, OpenBao, SSH certificates), how it maps to production, things to try
- `CHANGELOG.md`: release notes per version
- `.github/workflows/`: `ci.yml` (tests), `image.yml` (multi-arch image to GHCR)

- [operator-runs.md](operator-runs.md): operator identity, shared lease locks, credential and audit boundaries
