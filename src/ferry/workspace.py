"""Checkouts for the ferry service: the default branch (config) and a PR at an exact commit (code).

ferry.yaml is read from the default branch, never from the PR, so a PR can't change how ferry runs
it (scope rules, apply gates, policy). The playbooks run from the PR checkout: that's the change
under review.

The same code serves GitHub (refs/pull/<n>/head, user x-access-token) and GitLab
(refs/merge-requests/<n>/head, user oauth2): both take a token as the Basic-auth password.

The token reaches git through GIT_CONFIG_* environment variables (an http.extraHeader),
so it's never written to .git/config, a remote URL, or the process list.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from . import galaxy as galaxy_mod


class CheckoutError(RuntimeError):
    pass


class WorkspaceTimeout(RuntimeError):
    """A git command took longer than the workspace's timeout (a remote that never answers)."""


GIT_TIMEOUT = 300.0


def _git_env(token: str | None, user: str = "x-access-token") -> dict[str, str]:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"})
    if token:
        basic = base64.b64encode(f"{user}:{token}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"})
    return env


def _git(args: list[str], cwd: Path, token: str | None = None, user: str = "x-access-token",
         timeout: float | None = GIT_TIMEOUT) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, env=_git_env(token, user), capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise WorkspaceTimeout(f"git {args[0]} timed out after {timeout:g}s") from None
    if proc.returncode != 0:
        err = proc.stderr.strip().replace(token, "***") if token else proc.stderr.strip()
        raise CheckoutError(f"git {args[0]} failed: {err[-500:]}")
    return proc.stdout.strip()


def fetch_commit(remote: str, ref: str, expected_sha: str | None, dest: Path, token: str | None = None,
                 user: str = "x-access-token", timeout: float | None = GIT_TIMEOUT) -> str:
    """Shallow-fetch `ref` from `remote` into a fresh `dest` and check it out. Returns the commit SHA.

    With expected_sha, refuses if the ref has moved on (a newer push will bring its own event).
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    _git(["init", "-q"], dest, timeout=timeout)
    _git(["fetch", "-q", "--depth", "1", "--no-tags", remote, ref], dest, token, user, timeout)
    sha = _git(["rev-parse", "FETCH_HEAD"], dest, timeout=timeout)
    if expected_sha and sha != expected_sha:
        raise CheckoutError(f"{ref} is at {sha[:8]}, not {expected_sha[:8]}: the PR moved on")
    _git(["-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"], dest, timeout=timeout)
    return sha


class Workspace:
    """Directories under one work root: main/ (default branch), pr-<n>/ (a PR/MR), collections cache.

    change_ref is where the code host publishes a change's head: GitHub's refs/pull/<n>/head by
    default, GitLab's refs/merge-requests/<n>/head (with git_user "oauth2").
    """

    def __init__(self, root: str | Path, remote: str, token: Callable[[], str] | None = None,
                 change_ref: str = "refs/pull/{number}/head", git_user: str = "x-access-token",
                 git_timeout: float | None = GIT_TIMEOUT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.remote = remote
        self._token = token
        self.change_ref = change_ref
        self.git_user = git_user
        self.git_timeout = git_timeout

    def _tok(self) -> str | None:
        return self._token() if self._token else None

    def main(self, branch: str) -> tuple[Path, str]:
        dest = self.root / "main"
        return dest, fetch_commit(self.remote, f"refs/heads/{branch}", None, dest, self._tok(), self.git_user,
                                  self.git_timeout)

    def pull_request(self, number: int, sha: str) -> Path:
        dest = self.root / f"pr-{number}"
        fetch_commit(self.remote, self.change_ref.format(number=number), sha, dest, self._tok(), self.git_user,
                     self.git_timeout)
        return dest

    def discard(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def collections(self, checkout: Path, requirements: str = "requirements.yml",
                    galaxy: str = "ansible-galaxy", timeout: float = galaxy_mod.DEFAULT_TIMEOUT,
                    server: str | None = None, max_age_days: float = galaxy_mod.DEFAULT_MAX_AGE_DAYS) -> dict[str, str]:
        """Install the checkout's collections here, in this process (no executor): see ferry.galaxy.
        Returns the env that points ansible at them. No requirements file: nothing to do."""
        req = checkout / requirements
        if not req.is_file():
            return {}
        dest = galaxy_mod.install(req, self.root / "collections", galaxy=galaxy, timeout=timeout, server=server,
                                  max_age_days=max_age_days)  # GalaxyError: the job errors, visibly
        return {"ANSIBLE_COLLECTIONS_PATH": galaxy_mod.collections_path(dest)}
