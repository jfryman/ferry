"""JWKS caching for operator identity tokens (#55)."""
import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ferry.run_identity import IdentityError, IdentityVerifier

ISSUER = 'https://bao.example.com/v1/identity/oidc'
AUDIENCE = 'ferry-operators'


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


def keypair(kid):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    n = key.public_key().public_numbers().n
    jwk = {'kid': kid, 'kty': 'RSA', 'alg': 'RS256', 'use': 'sig',
           'n': b64(n.to_bytes((n.bit_length() + 7) // 8, 'big')), 'e': b64((65537).to_bytes(3, 'big'))}
    return key, jwk


class World:
    def __init__(self):
        self.now = 1_800_000_000.0
        self.key, jwk = keypair('k1')
        self.jwks = {'keys': [jwk]}
        self.fetches = 0
        self.down = False

    def fetch(self):
        self.fetches += 1
        if self.down:
            raise OSError('issuer unreachable')
        return json.loads(json.dumps(self.jwks))

    def token(self, kid='k1', key=None):
        claims = dict(iss=ISSUER, aud=AUDIENCE, sub='operator-example', exp=self.now + 300, iat=self.now)
        msg = b64(json.dumps({'alg': 'RS256', 'kid': kid}).encode()) + '.' + b64(json.dumps(claims).encode())
        sig = (key or self.key).sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())
        return msg + '.' + b64(sig)


@pytest.fixture
def world():
    w = World()
    w.verifier = IdentityVerifier(ISSUER, AUDIENCE, fetch=w.fetch, clock=lambda: w.now)
    return w


def test_keys_are_cached(world):
    for _ in range(5):
        assert world.verifier.verify(world.token())['sub'] == 'operator-example'
    assert world.fetches == 1


def test_garbage_tokens_never_fetch(world):
    for bad in ('garbage', 'a.b.c', b64(b'{"alg":"none"}') + '.e30.', b64(b'{"alg":"HS256","kid":"k1"}') + '.e30.x'):
        with pytest.raises(IdentityError):
            world.verifier.verify(bad)
    assert world.fetches == 0


def test_cache_expires(world):
    world.verifier.verify(world.token())
    world.now += 301
    world.verifier.verify(world.token())
    assert world.fetches == 2


def test_unknown_kid_refetches_rate_limited(world):
    world.verifier.verify(world.token())
    other, _ = keypair('k2')
    world.now += 31  # the rate limit counts from the last fetch of any kind
    for _ in range(10):
        with pytest.raises(IdentityError):
            world.verifier.verify(world.token(kid='k2', key=other))
    assert world.fetches == 2  # one early refetch, then rate-limited
    world.now += 31
    with pytest.raises(IdentityError):
        world.verifier.verify(world.token(kid='k2', key=other))
    assert world.fetches == 3


def test_rotated_key_is_picked_up(world):
    world.verifier.verify(world.token())
    new, jwk = keypair('k2')
    world.jwks['keys'].append(jwk)
    world.now += 31
    assert world.verifier.verify(world.token(kid='k2', key=new))['sub'] == 'operator-example'


def test_unreachable_with_nothing_cached_fails_closed(world):
    world.down = True
    with pytest.raises(IdentityError):
        world.verifier.verify(world.token())


def test_unreachable_uses_recent_keys_then_fails_closed(world):
    world.verifier.verify(world.token())
    world.down = True
    world.now += 600  # past the cache TTL, within the stale window
    assert world.verifier.verify(world.token())['sub'] == 'operator-example'
    world.now += 3600  # past the stale window
    with pytest.raises(IdentityError):
        world.verifier.verify(world.token())
