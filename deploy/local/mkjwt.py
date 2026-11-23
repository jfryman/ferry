"""Mint a Nomad-style workload identity for the lab: an RS256 JWT plus the public key OpenBao's jwt
auth validates it with. In production Nomad signs it (identity block, aud openbao.io) and OpenBao
validates against Nomad's JWKS; the claims below are the ones the role binds on."""
import base64
import json
import sys
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

out = sys.argv[1]
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
open(f"{out}/jwt_pub.pem", "wb").write(pub)
b64 = lambda d: base64.urlsafe_b64encode(d).rstrip(b"=").decode()
now = int(time.time())
claims = {"iss": "nomad", "aud": "openbao.io", "sub": "global:default:ferry:ferry:ferry:openbao",
          "nomad_namespace": "default", "nomad_job_id": "ferry", "nomad_task": "ferry",
          "iat": now, "nbf": now, "exp": now + 12 * 3600}
signing_input = f'{b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())}.{b64(json.dumps(claims).encode())}'
sig = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
open(f"{out}/nomad_openbao.jwt", "w").write(f"{signing_input}.{b64(sig)}")
print(f"workload identity for job ferry, valid 12h: {out}/nomad_openbao.jwt")
