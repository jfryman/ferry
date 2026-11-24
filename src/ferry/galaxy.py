"""ansible-galaxy on a change's requirements.yml, on the executor's side of the split (#26).

The PR picks what requirements.yml fetches (any Galaxy server, a git URL, a tarball URL), and
ansible-galaxy then unpacks archives and walks trees ferry doesn't control. So the install:

- runs in `ferry executor` when there is one (its own writable cache, none of the service's
  secrets), otherwise in the service with a minimal environment: PATH, locale, proxy and CA
  settings, and a throwaway HOME; nothing named FERRY_*, no tokens;
- is killed, with everything it started, after `timeout` seconds;
- with a pinned `server`, refuses requirements that name any other source (URLs, git, files,
  `source:`/`type:` overrides, roles) and passes `--server`;
- is cached per requirements digest; entries unused for `max_age_days` are pruned.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

KEEP_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE")
DEFAULT_TIMEOUT = 300.0
DEFAULT_MAX_AGE_DAYS = 14.0
MARK = ".installed"


class GalaxyError(RuntimeError):
    pass


def minimal_env(home: str, environ: dict | None = None, server: str | None = None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    env = {k: environ[k] for k in KEEP_ENV if environ.get(k)}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.update({"HOME": home, "ANSIBLE_NOCOLOR": "1", "ANSIBLE_LOCAL_TEMP": os.path.join(home, "tmp")})
    if server:
        env["ANSIBLE_GALAXY_SERVER"] = server
    return env


def check_pinned(requirements: Path) -> None:
    """With a pinned server, every entry must be a plain collection name from that server."""
    try:
        data = yaml.safe_load(requirements.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise GalaxyError(f"requirements.yml isn't readable YAML ({type(e).__name__})") from e
    if isinstance(data, list):
        data = {"collections": data}
    if not isinstance(data, dict):
        raise GalaxyError("requirements.yml must be a mapping with a `collections:` list")
    if data.get("roles"):
        raise GalaxyError("requirements.yml lists roles; with a pinned Galaxy server only collections install")
    cols = data.get("collections") or []
    if not isinstance(cols, list):
        raise GalaxyError("requirements.yml: `collections` must be a list")
    for c in cols:
        entry = {"name": c} if isinstance(c, str) else c
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise GalaxyError("requirements.yml: each collection needs a name")
        name = entry["name"]
        parts = name.split(".")
        if len(parts) != 2 or not all(p.replace("_", "").isalnum() for p in parts):
            raise GalaxyError(f"requirements.yml: `{name[:80]}` isn't a collection name from the pinned Galaxy server")
        if entry.get("source") or entry.get("type") not in (None, "galaxy"):
            raise GalaxyError(f"requirements.yml: `{name}` names another source; the Galaxy server is pinned")


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def install(requirements: Path, cache_root: Path, galaxy: str = "ansible-galaxy", timeout: float = DEFAULT_TIMEOUT,
            server: str | None = None, max_age_days: float = DEFAULT_MAX_AGE_DAYS,
            environ: dict | None = None) -> Path:
    """Install requirements' collections into cache_root/<digest>; return that directory."""
    requirements = Path(requirements)
    if server:
        check_pinned(requirements)
    digest = hashlib.sha256(requirements.read_bytes() + (server or "").encode()).hexdigest()[:16]
    dest = Path(cache_root) / digest
    if (dest / MARK).exists():
        os.utime(dest)  # in use: not pruned
        prune(cache_root, max_age_days, keep={dest})
        return dest
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)  # a failed or killed earlier attempt
    dest.mkdir(parents=True)
    cmd = [galaxy, "collection", "install", "-r", str(requirements), "-p", str(dest)]
    if server:
        cmd += ["--server", server]
    with tempfile.TemporaryDirectory(prefix="ferry-galaxy-home-") as home:
        os.makedirs(os.path.join(home, "tmp"))
        proc = subprocess.Popen(cmd, cwd=requirements.parent, env=minimal_env(home, environ, server),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.communicate()
            shutil.rmtree(dest, ignore_errors=True)
            raise GalaxyError(f"ansible-galaxy timed out after {timeout:g}s") from None
        finally:
            _kill_group(proc)  # anything it left behind
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise GalaxyError(f"ansible-galaxy failed (exit {proc.returncode}): {(out or '').strip()[-500:]}")
    (dest / MARK).touch()
    prune(cache_root, max_age_days, keep={dest})
    return dest


def prune(cache_root: Path, max_age_days: float, keep: set[Path] = frozenset(), now: float | None = None) -> list[Path]:
    """Remove cache entries not used for max_age_days (0: keep everything)."""
    if max_age_days <= 0 or not Path(cache_root).is_dir():
        return []
    now = time.time() if now is None else now
    keep = {Path(k).resolve() for k in keep}
    removed = []
    for d in Path(cache_root).iterdir():
        if not d.is_dir() or d.is_symlink() or d.resolve() in keep:
            continue
        if now - d.stat().st_mtime > max_age_days * 86400:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
    return removed


def collections_path(dest: Path | str) -> str:
    """ANSIBLE_COLLECTIONS_PATH: the job's collections ahead of the defaults (image-baked ones)."""
    return os.pathsep.join([str(dest), "~/.ansible/collections", "/usr/share/ansible/collections"])
