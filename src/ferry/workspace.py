"""Checkouts for the ferry service: the default branch (config) and a PR at an exact commit (code).

ferry.yaml is read from the default branch, never from the PR, so a PR can't change how ferry runs
it (scope rules, apply gates, policy). The playbooks run from the PR checkout: that's the change
under review.

The installation token reaches git through GIT_CONFIG_* environment variables (an http.extraHeader),
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


class CheckoutError(RuntimeError):
    pass


def _git_env(token: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"})
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"})
    return env


def _git(args: list[str], cwd: Path, token: str | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, env=_git_env(token), capture_output=True, text=True)
    if proc.returncode != 0:
        err = proc.stderr.strip().replace(token, "***") if token else proc.stderr.strip()
        raise CheckoutError(f"git {args[0]} failed: {err[-500:]}")
    return proc.stdout.strip()


def fetch_commit(remote: str, ref: str, expected_sha: str | None, dest: Path, token: str | None = None) -> str:
    """Shallow-fetch `ref` from `remote` into a fresh `dest` and check it out. Returns the commit SHA.

    With expected_sha, refuses if the ref has moved on (a newer push will bring its own event).
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    _git(["init", "-q"], dest)
    _git(["fetch", "-q", "--depth", "1", "--no-tags", remote, ref], dest, token)
    sha = _git(["rev-parse", "FETCH_HEAD"], dest)
    if expected_sha and sha != expected_sha:
        raise CheckoutError(f"{ref} is at {sha[:8]}, not {expected_sha[:8]}: the PR moved on")
    _git(["-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"], dest)
    return sha


class Workspace:
    """Directories under one work root: main/ (default branch), pr-<n>/ (a PR), collections cache."""

    def __init__(self, root: str | Path, remote: str, token: Callable[[], str] | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.remote = remote
        self._token = token

    def _tok(self) -> str | None:
        return self._token() if self._token else None

    def main(self, branch: str) -> tuple[Path, str]:
        dest = self.root / "main"
        return dest, fetch_commit(self.remote, f"refs/heads/{branch}", None, dest, self._tok())

    def pull_request(self, number: int, sha: str) -> Path:
        dest = self.root / f"pr-{number}"
        fetch_commit(self.remote, f"refs/pull/{number}/head", sha, dest, self._tok())
        return dest

    def discard(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def collections(self, checkout: Path, requirements: str = "requirements.yml",
                    galaxy: str = "ansible-galaxy") -> dict[str, str]:
        """Install the checkout's Ansible collections into a cache keyed by the file's hash; return
        the env that points ansible at them. No requirements file: nothing to do."""
        req = checkout / requirements
        if not req.is_file():
            return {}
        digest = hashlib.sha256(req.read_bytes()).hexdigest()[:16]
        dest = self.root / "collections" / digest
        if not (dest / ".installed").exists():
            dest.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run([galaxy, "collection", "install", "-r", str(req), "-p", str(dest)],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                raise CheckoutError(f"ansible-galaxy failed: {proc.stderr.strip()[-500:]}")
            (dest / ".installed").touch()
        # Ahead of the defaults, so collections baked into the image still resolve.
        return {"ANSIBLE_COLLECTIONS_PATH": os.pathsep.join(
            [str(dest), "~/.ansible/collections", "/usr/share/ansible/collections"])}
