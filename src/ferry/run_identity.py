"""OpenBao identity JWT verification. Never accepts OpenBao API tokens or JWT-selected URLs."""
import base64
import json
import math
import threading
import time
import urllib.request


def reject_constant(value):
    raise ValueError('non-standard JSON constant')


def unb64(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


class IdentityError(ValueError):
    pass


class IdentityVerifier:
    """Keys are cached (#55): refetched after cache_seconds, or early (at most once per
    refetch_seconds) when a token names an unknown kid. If the issuer is unreachable, keys up to
    stale_seconds old are still used; with nothing usable cached, verification fails closed."""

    def __init__(self, issuer, audience, fetch=None, clock=time.time,
                 cache_seconds=300, refetch_seconds=30, stale_seconds=3600):
        self.issuer, self.audience, self.clock = issuer, audience, clock
        self.fetch = fetch or self._fetch
        self.cache_seconds, self.refetch_seconds, self.stale_seconds = cache_seconds, refetch_seconds, stale_seconds
        self._lock = threading.Lock()
        self._keys = None       # the last good JWKS 'keys' list
        self._fetched_at = None  # when _keys was fetched
        self._tried_at = None    # last fetch attempt, good or not

    def _refresh(self, now):
        self._tried_at = now
        try:
            keys = self.fetch()['keys']
            if not isinstance(keys, list):
                raise ValueError()
        except Exception:
            return False
        self._keys, self._fetched_at = keys, now
        return True

    def keys_for(self, kid):
        now = self.clock()
        with self._lock:
            age = None if self._fetched_at is None else now - self._fetched_at
            if age is None or age >= self.cache_seconds:
                if self._tried_at is None or now - self._tried_at >= self.refetch_seconds or age is None:
                    self._refresh(now)
            elif not any(isinstance(k, dict) and k.get('kid') == kid for k in self._keys):
                # A new kid may mean the issuer rotated keys: refetch, but rate-limited.
                if self._tried_at is None or now - self._tried_at >= self.refetch_seconds:
                    self._refresh(now)
            if self._fetched_at is None or now - self._fetched_at >= self.stale_seconds:
                raise ValueError('no usable keys')
            return [k for k in self._keys if isinstance(k, dict) and k.get('kid') == kid]

    def _fetch(self):
        with urllib.request.urlopen(self.issuer.rstrip('/') + '/.well-known/keys', timeout=10) as r:
            return json.loads(r.read(1024 * 1024))

    def verify(self, token):
        try:
            from cryptography.hazmat.primitives.asymmetric import rsa, padding
            from cryptography.hazmat.primitives import hashes
            from cryptography.exceptions import InvalidSignature
            a, b, c = token.split('.')
            header, claims = json.loads(unb64(a), parse_constant=reject_constant), json.loads(unb64(b), parse_constant=reject_constant)
            # No algorithm negotiation, symmetric keys, remote jku/x5u, or unsigned JWTs.
            if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str):
                raise ValueError()
            keys = [k for k in self.keys_for(header['kid'])
                    if k.get('kty') == 'RSA' and k.get('alg', 'RS256') == 'RS256'
                    and k.get('use', 'sig') == 'sig']
            if len(keys) != 1:
                raise ValueError()
            key = keys[0]
            pub = rsa.RSAPublicNumbers(int.from_bytes(unb64(key['e']), 'big'),
                                       int.from_bytes(unb64(key['n']), 'big')).public_key()
            pub.verify(unb64(c), (a + '.' + b).encode(), padding.PKCS1v15(), hashes.SHA256())
            now = self.clock()
            exp = claims['exp']
            if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp) or exp <= now:
                raise ValueError()
            # Fixed skew only for activation/issuance; expiration remains strict.
            for field in ('nbf', 'iat'):
                value = claims.get(field, 0)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value > now + 30:
                    raise ValueError()
            aud = claims.get('aud')
            if claims.get('iss') != self.issuer or not (aud == self.audience or
                    isinstance(aud, list) and self.audience in aud):
                raise ValueError()
            sub = claims.get('sub')
            if not isinstance(sub, str) or not sub or len(sub) > 256 or any(ord(c) < 32 for c in sub):
                raise ValueError()
            groups = claims.get('groups', [])
            if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
                raise ValueError()
            return claims
        except Exception:
            # Do not include parser, transport or crypto exceptions: they may contain token data.
            raise IdentityError('invalid identity token') from None
