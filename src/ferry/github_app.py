"""GitHub App authentication for the ferry service.

The App signs a short JWT with its private key (RS256) to ask for an installation token, which is
what every API call and git fetch uses. Installation tokens last an hour; `InstallationTokens`
caches one and fetches a new one five minutes before it expires.

Needs the `cryptography` package (`pip install 'ferry[service]'`); nothing else in ferry does.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime
from typing import Callable

from .vcs.github import API, GitHubError, Transport, urllib_transport

REFRESH_MARGIN = 300  # seconds before expiry to fetch a new installation token


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str | int, private_key_pem: bytes, now: float | None = None) -> str:
    """A JWT identifying the App: valid from 60 s ago (clock drift) for 9 minutes (GitHub's max is 10)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(time.time() if now is None else now)
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": now - 60, "exp": now + 540, "iss": str(app_id)}
    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}." \
                    f"{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


class GitHubApp:
    def __init__(self, app_id: str | int, private_key_pem: bytes, api_url: str = API,
                 transport: Transport = urllib_transport, clock: Callable[[], float] = time.time):
        self.app_id = app_id
        self._key = private_key_pem
        self.api_url = api_url.rstrip("/")
        self._transport = transport
        self._clock = clock

    def _app_request(self, method: str, path: str) -> dict:
        headers = {
            "Authorization": f"Bearer {app_jwt(self.app_id, self._key, self._clock())}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ferry",
        }
        status, raw = self._transport(method, f"{self.api_url}{path}", headers, None)
        if status >= 400:
            raise GitHubError(status, method, path, raw.decode(errors="replace"))
        return json.loads(raw) if raw else {}

    def bot_login(self) -> str:
        """The login the App comments as: '<slug>[bot]'. Plan markers are trusted only from it."""
        return f"{self._app_request('GET', '/app')['slug']}[bot]"

    def installation_id(self, repo: str) -> int:
        return int(self._app_request("GET", f"/repos/{repo}/installation")["id"])

    def new_installation_token(self, installation_id: int) -> tuple[str, float]:
        data = self._app_request("POST", f"/app/installations/{installation_id}/access_tokens")
        expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        return data["token"], expires


class InstallationTokens:
    """A callable returning a current installation token; safe to share between threads."""

    def __init__(self, app: GitHubApp, installation_id: int, clock: Callable[[], float] = time.time):
        self.app = app
        self.installation_id = installation_id
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires = 0.0

    def __call__(self) -> str:
        with self._lock:
            if self._token is None or self._clock() >= self._expires - REFRESH_MARGIN:
                self._token, self._expires = self.app.new_installation_token(self.installation_id)
            return self._token
