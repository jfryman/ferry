import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ferry.github_app import GitHubApp, InstallationTokens, app_jwt


@pytest.fixture(scope="module")
def key():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return k, pem


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_jwt_is_rs256_signed_with_github_claims(key):
    k, pem = key
    token = app_jwt(12345, pem, now=1_800_000_000)
    header, payload, sig = token.split(".")
    assert json.loads(_b64d(header)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_b64d(payload))
    assert claims == {"iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540, "iss": "12345"}
    k.public_key().verify(_b64d(sig), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())


class FakeAppAPI:
    def __init__(self):
        self.calls = []
        self.n = 0

    def __call__(self, method, url, headers, body):
        assert headers["Authorization"].startswith("Bearer ey")  # the App JWT
        path = url.split("api.github.com", 1)[1]
        self.calls.append((method, path))
        if path == "/app":
            return 200, b'{"slug": "ferry-atomic"}'
        if path == "/repos/acme/ansible/installation":
            return 200, b'{"id": 42}'
        if method == "POST" and path == "/app/installations/42/access_tokens":
            self.n += 1
            exp = "2027-01-15T08:00:00Z" if self.n == 1 else "2027-01-15T09:00:00Z"
            return 201, json.dumps({"token": f"ghs_{self.n}", "expires_at": exp}).encode()
        return 404, b'{"message":"Not Found"}'


def test_app_lookups(key):
    _, pem = key
    api = FakeAppAPI()
    app = GitHubApp(1, pem, transport=api)
    assert app.bot_login() == "ferry-atomic[bot]"
    assert app.installation_id("acme/ansible") == 42


def test_installation_token_is_cached_then_refreshed_before_expiry(key):
    _, pem = key
    api = FakeAppAPI()
    expiry = 1_800_000_000 + 0  # placeholder, replaced below
    from datetime import datetime, timezone
    expiry = datetime(2027, 1, 15, 8, 0, tzinfo=timezone.utc).timestamp()
    now = {"t": expiry - 3600}
    app = GitHubApp(1, pem, transport=api, clock=lambda: now["t"])
    tokens = InstallationTokens(app, 42, clock=lambda: now["t"])
    assert tokens() == "ghs_1"
    now["t"] = expiry - 600  # 10 min left: still fine
    assert tokens() == "ghs_1"
    now["t"] = expiry - 200  # inside the 5-minute margin: refresh
    assert tokens() == "ghs_2"
    assert sum(1 for c in api.calls if c[0] == "POST") == 2
