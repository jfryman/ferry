"""Per-job credentials from OpenBao for the ferry service.

Each plan/apply/drift job:
  1. logs in to OpenBao with the service's workload identity (a JWT, e.g. Nomad's), getting a token
     that lives no longer than the job needs;
  2. generates a throwaway ed25519 SSH key and has OpenBao's SSH CA sign it (a short certificate);
  3. optionally fetches the ansible-vault password into a 0600 file;
and hands ansible the matching environment. Everything sits in a temporary directory removed when
the job ends, so nothing outlives the job.

Optional: with no OpenBao settings the service runs ansible with whatever SSH setup the container has.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

Transport = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]
log = logging.getLogger("ferry.credentials")


class CredentialError(RuntimeError):
    pass


@dataclass
class OpenBaoSettings:
    addr: str
    jwt_file: str  # the workload identity token, e.g. ${NOMAD_SECRETS_DIR}/nomad_ferry.jwt
    auth_mount: str = "jwt-nomad"
    auth_role: str = "ferry"
    ssh_mount: str = "ssh-client-signer"
    ssh_role: str = "ferry"
    ca_cert: str | None = None
    vault_password_path: str | None = None  # kv-v2 data path, e.g. infra/data/ansible/vault-password
    vault_password_field: str = "password"
    # Asked for explicitly rather than left to the role's defaults: a certificate outlives the job and
    # its revoked token (revoking a token doesn't revoke a cert), so keep it close to the job timeout.
    ssh_ttl: str | None = None  # e.g. "20m"; None: the role's default ttl
    ssh_principal: str | None = None  # valid_principals to request; None: the role's default_user
    # Hand the job's OpenBao token to the playbook (VAULT_TOKEN, for community.hashi_vault lookups).
    # Off by default: that token can sign more SSH certificates until it expires, so without it the
    # token is revoked as soon as the job's credentials are fetched, before any playbook runs.
    playbook_token: bool = False


def urllib_transport_for(ca_cert: str | None) -> Transport:
    ctx = ssl.create_default_context(cafile=ca_cert) if ca_cert else None

    def transport(method, url, headers, body):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    return transport


class OpenBao:
    def __init__(self, settings: OpenBaoSettings, transport: Transport | None = None):
        self.s = settings
        self._transport = transport or urllib_transport_for(settings.ca_cert)

    def _call(self, method: str, path: str, token: str | None = None, data: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Vault-Token"] = token
        body = json.dumps(data).encode() if data is not None else None
        status, raw = self._transport(method, f"{self.s.addr.rstrip('/')}/v1/{path}", headers, body)
        if status >= 400:
            # OpenBao error bodies don't echo secrets; keep only the error list.
            try:
                errors = json.loads(raw).get("errors", [])
            except ValueError:
                errors = []
            raise CredentialError(f"OpenBao {method} {path} -> {status}: {errors}")
        return json.loads(raw) if raw else {}

    def login(self) -> str:
        jwt = Path(self.s.jwt_file).read_text().strip()
        auth = self._call("POST", f"auth/{self.s.auth_mount}/login", data={"role": self.s.auth_role, "jwt": jwt})
        return auth["auth"]["client_token"]

    def sign_ssh_key(self, token: str, public_key: str) -> str:
        req = {"public_key": public_key, "cert_type": "user"}
        if self.s.ssh_ttl:
            req["ttl"] = self.s.ssh_ttl
        if self.s.ssh_principal:
            req["valid_principals"] = self.s.ssh_principal
        data = self._call("POST", f"{self.s.ssh_mount}/sign/{self.s.ssh_role}", token, req)
        return data["data"]["signed_key"]

    def read_field(self, token: str, path: str, field: str) -> str:
        data = self._call("GET", path, token)
        try:
            return data["data"]["data"][field]
        except KeyError as e:
            raise CredentialError(f"{path} has no field {field!r}") from e

    def revoke(self, token: str) -> None:
        try:
            self._call("POST", "auth/token/revoke-self", token)
        except CredentialError as e:
            log.warning("couldn't revoke the job token (it expires on its own): %s", e)


@contextmanager
def job_credentials(bao: OpenBao | None, ssh_user: str | None = None,
                    keygen: str = "ssh-keygen") -> Iterator[dict[str, str]]:
    """Yield the env for one ansible run; clean up (files, token) afterwards, whatever happened."""
    if bao is None:
        yield {"ANSIBLE_REMOTE_USER": ssh_user} if ssh_user else {}
        return
    tmp = Path(tempfile.mkdtemp(prefix="ferry-creds-"))
    os.chmod(tmp, 0o700)
    token = None
    try:
        token = bao.login()
        key = tmp / "id_ed25519"
        proc = subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", "ferry-job", "-f", str(key)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise CredentialError(f"ssh-keygen failed: {proc.stderr.strip()}")
        cert = bao.sign_ssh_key(token, (tmp / "id_ed25519.pub").read_text())
        (tmp / "id_ed25519-cert.pub").write_text(cert.strip() + "\n")
        env = {
            # For the runner: the executor gets these files' contents (executor.py).
            "FERRY_CREDS_DIR": str(tmp),
            # ssh finds id_ed25519-cert.pub next to the key on its own.
            "ANSIBLE_PRIVATE_KEY_FILE": str(key),
            "ANSIBLE_SSH_ARGS": "-o IdentitiesOnly=yes -o ControlMaster=auto -o ControlPersist=30s",
            # Connection sharing stays inside this job: with ansible's default ~/.ansible/cp, the next
            # job would reuse this job's still-open master connection, i.e. run under this job's
            # certificate after its credentials were gone (found in the local lab, Nov 23).
            "ANSIBLE_SSH_CONTROL_PATH_DIR": str(tmp / "cp"),
        }
        if bao.s.playbook_token:
            # For lookups (community.hashi_vault reads these).
            env.update({"VAULT_ADDR": bao.s.addr, "VAULT_TOKEN": token,
                        "ANSIBLE_HASHI_VAULT_ADDR": bao.s.addr, "ANSIBLE_HASHI_VAULT_TOKEN": token})
            if bao.s.ca_cert:
                env["VAULT_CACERT"] = bao.s.ca_cert
                env["ANSIBLE_HASHI_VAULT_CA_CERT"] = bao.s.ca_cert
        if ssh_user:
            env["ANSIBLE_REMOTE_USER"] = ssh_user
        if bao.s.vault_password_path:
            pw = tmp / "vault-password"
            pw.write_text(bao.read_field(token, bao.s.vault_password_path, bao.s.vault_password_field))
            os.chmod(pw, 0o600)
            env["ANSIBLE_VAULT_PASSWORD_FILE"] = str(pw)
        if not bao.s.playbook_token:
            bao.revoke(token)  # everything is fetched: no playbook ever runs while it's valid
            token = None
        yield env
    finally:
        if token:
            bao.revoke(token)
        shutil.rmtree(tmp, ignore_errors=True)
