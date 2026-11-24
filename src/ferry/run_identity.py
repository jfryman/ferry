"""OpenBao identity JWT verification. Never accepts OpenBao API tokens or JWT-selected URLs."""
import base64
import json
import math
import time
import urllib.request


def unb64(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


class IdentityError(ValueError):
    pass


class IdentityVerifier:
    def __init__(self, issuer, audience, fetch=None, clock=time.time):
        self.issuer, self.audience, self.clock = issuer, audience, clock
        self.fetch = fetch or self._fetch

    def _fetch(self):
        with urllib.request.urlopen(self.issuer.rstrip('/') + '/.well-known/keys', timeout=10) as r:
            return json.loads(r.read(1024 * 1024))

    def verify(self, token):
        try:
            from cryptography.hazmat.primitives.asymmetric import rsa, padding
            from cryptography.hazmat.primitives import hashes
            from cryptography.exceptions import InvalidSignature
            a, b, c = token.split('.')
            header, claims = json.loads(unb64(a)), json.loads(unb64(b))
            # No algorithm negotiation, symmetric keys, remote jku/x5u, or unsigned JWTs.
            if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str):
                raise ValueError()
            keys = [k for k in self.fetch()['keys'] if k.get('kid') == header['kid']
                    and k.get('kty') == 'RSA' and k.get('alg', 'RS256') == 'RS256'
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
            if claims.get('nbf', 0) > now or claims.get('iat', 0) > now:
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
