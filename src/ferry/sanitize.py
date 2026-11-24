"""Redaction for anything ferry posts outside the CI job (MR comments).

Layers, from strongest to weakest:
1. Ansible censors no_log results before callbacks see them (ferry_events also drops diff/msg).
2. Diffs for sensitive paths (shadow, keys, ...) are never rendered.
3. Exact values of named environment variables, and of any env var that looks like a credential
   (name contains TOKEN/SECRET/PASSWORD/PRIVATE/KEY and value is 8+ chars), are masked.
4. Regexes for well-known secret shapes (PEM keys, Vault/GitLab/Slack/AWS tokens, crypt hashes,
   password=... assignments).
Layer 4 is a safety net, not a guarantee; layer 1 (no_log on secret-bearing tasks) is the contract.
"""

from __future__ import annotations

import fnmatch
import os
import re

MASK = "[REDACTED]"

BUILTIN_PATTERNS = [
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*",  # truncated key (diff cut mid-block)
    r"\bhv[sbr]\.[A-Za-z0-9_\-]{20,}",  # Vault tokens
    r"\bglpat-[A-Za-z0-9_\-]{16,}",  # GitLab PAT
    r"\bglrt-[A-Za-z0-9_\-]{16,}",  # GitLab runner token
    r"\bxox[abprs]-[A-Za-z0-9\-]{10,}",  # Slack
    r"\bAKIA[0-9A-Z]{16}\b",  # AWS access key id
    r"\$(?:1|2[aby]?|5|6|y)\$[^\s:\"']{8,}",  # crypt(3) hashes
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|private[_-]?key)(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)",
]

_CRED_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE|_KEY$|^KEY", re.IGNORECASE)


class Redactor:
    def __init__(
        self,
        patterns: list[str] | None = None,
        env_vars: list[str] | None = None,
        sensitive_paths: list[str] | None = None,
        environ: dict[str, str] | None = None,
        values: list[str] | None = None,
    ):
        """`values`: exact secrets that live outside the environment (the vault password file's
        contents); masked like credential env values, regardless of length."""
        environ = os.environ if environ is None else environ
        self._regexes = [re.compile(p, re.DOTALL) for p in BUILTIN_PATTERNS + list(patterns or [])]
        found = set()
        for name, value in environ.items():
            if not value or len(value) < 8:
                continue
            if name in (env_vars or []) or _CRED_NAME.search(name):
                found.add(value)
        for value in values or []:
            if value:
                found.add(value)
        # Longest first so a value containing another is masked whole.
        self._values = sorted(found, key=len, reverse=True)
        self._paths = list(sensitive_paths or [])

    def text(self, s: str | None) -> str | None:
        if s is None:
            return None
        for v in self._values:
            s = s.replace(v, MASK)
        for rx in self._regexes:
            if rx.groups >= 3:
                s = rx.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", s)
            else:
                s = rx.sub(MASK, s)
        return s

    def sensitive_path(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, g) for g in self._paths)
