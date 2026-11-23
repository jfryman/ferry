import json
import os
import stat

import pytest

from ferry.credentials import CredentialError, OpenBao, OpenBaoSettings, job_credentials


class FakeBao:
    def __init__(self, fail_sign=False):
        self.calls = []
        self.fail_sign = fail_sign

    def __call__(self, method, url, headers, body):
        path = url.split("/v1/", 1)[1]
        data = json.loads(body) if body else None
        self.calls.append((method, path, headers.get("X-Vault-Token"), data))
        if path == "auth/jwt-nomad/login":
            assert data == {"role": "ferry", "jwt": "workload.jwt.token"}
            return 200, b'{"auth": {"client_token": "s.job"}}'
        if path == "ssh-client-signer/sign/ferry":
            if self.fail_sign:
                return 400, b'{"errors": ["root is not a valid value for valid_principals"]}'
            assert headers["X-Vault-Token"] == "s.job" and data["public_key"].startswith("ssh-ed25519 ")
            return 200, b'{"data": {"signed_key": "ssh-ed25519-cert-v01@openssh.com AAAAcert"}}'
        if path == "infra/data/ansible/vault-password":
            return 200, b'{"data": {"data": {"password": "hunter2"}}}'
        if path == "auth/token/revoke-self":
            return 204, b""
        return 404, b'{"errors": []}'


@pytest.fixture
def settings(tmp_path):
    jwt = tmp_path / "nomad.jwt"
    jwt.write_text("workload.jwt.token\n")
    return OpenBaoSettings(addr="https://bao:8200", jwt_file=str(jwt), ca_cert="/ca.pem",
                           vault_password_path="infra/data/ansible/vault-password")


def test_job_gets_a_signed_key_token_and_vault_password_then_everything_goes(settings):
    fake = FakeBao()
    with job_credentials(OpenBao(settings, fake), ssh_user="root") as env:
        key = env["ANSIBLE_PRIVATE_KEY_FILE"]
        tmp = os.path.dirname(key)
        assert stat.S_IMODE(os.stat(tmp).st_mode) == 0o700
        assert open(key + "-cert.pub").read().startswith("ssh-ed25519-cert-v01@openssh.com")
        assert open(env["ANSIBLE_VAULT_PASSWORD_FILE"]).read() == "hunter2"
        assert stat.S_IMODE(os.stat(env["ANSIBLE_VAULT_PASSWORD_FILE"]).st_mode) == 0o600
        assert env["VAULT_TOKEN"] == env["ANSIBLE_HASHI_VAULT_TOKEN"] == "s.job"
        assert env["VAULT_CACERT"] == "/ca.pem" and env["ANSIBLE_REMOTE_USER"] == "root"
        assert "IdentitiesOnly=yes" in env["ANSIBLE_SSH_ARGS"]
    assert not os.path.exists(tmp)
    assert fake.calls[-1][1] == "auth/token/revoke-self"


def test_failure_still_cleans_up_and_revokes(settings):
    fake = FakeBao(fail_sign=True)
    with pytest.raises(CredentialError, match="valid_principals"):
        with job_credentials(OpenBao(settings, fake)):
            pass
    assert fake.calls[-1][1] == "auth/token/revoke-self"


def test_without_openbao_only_the_remote_user():
    with job_credentials(None, ssh_user="root") as env:
        assert env == {"ANSIBLE_REMOTE_USER": "root"}
    with job_credentials(None) as env:
        assert env == {}
