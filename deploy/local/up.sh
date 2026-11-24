#!/usr/bin/env bash
# Stand up the full ferry loop locally, in Docker: OpenBao (dev) with Nomad-style JWT auth, the SSH
# user CA and the vault password; a fake GitHub API; a git remote with one PR; an sshd host that
# trusts the CA; and the ferry service. Needs: docker or rootless podman, bash, git, curl, openssl.
#
#   deploy/local/up.sh          # build and start (re-run to rebuild from scratch)
#   deploy/local/lab.sh open    # deliver a signed "PR opened" webhook, then see lab.sh help
#   deploy/local/down.sh        # stop everything
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
LAB=${LAB_DIR:-/tmp/ferry-lab}
NET=ferry-lab
BAO_IMAGE=${BAO_IMAGE:-openbao/openbao:2.7.2}
. "$HERE/runtime.sh"
say() { printf '\n==> %s\n' "$*"; }

"$HERE/down.sh" >/dev/null 2>&1 || true
wipe "$LAB"
mkdir -p "$LAB"/{ca,jwt,secrets,remote,state,src,work,run,operators}
chmod 777 "$LAB"/{ca,jwt,secrets,state,work,run}

say "Building images (ferry:lab, ferry-target:lab)"
docker build -q -t ferry:lab "$ROOT" >/dev/null
docker build -q -t ferry-target:lab "$HERE/target" >/dev/null
docker build -q --build-arg OPERATOR_UID="$(id -u)" --build-arg BAO_IMAGE="$BAO_IMAGE" -t ferry-operator:lab -f "$HERE/operator.Dockerfile" "$HERE" >/dev/null
docker network create "$NET" >/dev/null
run_py() { docker run --rm -i -u "$(id -u):$(id -g)" -v "$LAB:/lab" -v "$HERE:/lab-tools:ro" --entrypoint python ferry:lab "$@"; }

say "OpenBao (dev): SSH CA, ferry signing role, vault password, Nomad-style JWT auth"
docker run -d --name ferry-lab-bao --network "$NET" -p 18200:8200 --cap-add IPC_LOCK "$BAO_IMAGE" \
  server -dev -dev-root-token-id=lab -dev-listen-address=0.0.0.0:8200 >/dev/null
bao() { docker exec -i -e BAO_ADDR=http://127.0.0.1:8200 -e BAO_TOKEN=lab ferry-lab-bao bao "$@"; }
for _ in $(seq 1 30); do bao status >/dev/null 2>&1 && break; sleep 1; done

# = what production OpenBao config sets up: mount, CA, role "ferry", policy ssh-sign-ferry.
bao secrets enable -path=ssh-client-signer ssh >/dev/null
bao write ssh-client-signer/config/ca generate_signing_key=true key_type=ed25519 >/dev/null
bao write ssh-client-signer/roles/ferry - >/dev/null <<'JSON'
{"key_type": "ca", "allow_user_certificates": true, "allow_host_certificates": false,
 "allowed_users": "ferry", "default_user": "ferry",
 "allowed_extensions": "permit-pty", "default_extensions": {"permit-pty": ""},
 "allowed_user_key_lengths": {"ed25519": [0]}, "key_id_format": "{{token_display_name}}",
 "ttl": "30m", "max_ttl": "1h"}
JSON
bao policy write ssh-sign-ferry - >/dev/null <<'HCL'
path "ssh-client-signer/sign/ferry" { capabilities = ["update"] }
HCL
bao read -field=public_key ssh-client-signer/config/ca > "$LAB/ca/user_ca.pub"

# The ansible-vault password (prod: infra/ kv-v2; dev mode's kv-v2 is secret/).
bao kv put -mount=secret ansible/vault-password password=lab-vault-pass >/dev/null
bao policy write ferry-vault-password - >/dev/null <<'HCL'
path "secret/data/ansible/vault-password" { capabilities = ["read"] }
HCL

# Workload identity: prod = Nomad signs, OpenBao checks Nomad's JWKS. Lab = a static key.
run_py /lab-tools/mkjwt.py /lab/jwt
bao auth enable -path=jwt-nomad jwt >/dev/null
docker cp "$LAB/jwt/jwt_pub.pem" ferry-lab-bao:/tmp/jwt_pub.pem
bao write auth/jwt-nomad/config jwt_validation_pubkeys=@/tmp/jwt_pub.pem >/dev/null
bao write auth/jwt-nomad/role/ferry - >/dev/null <<'JSON'
{"role_type": "jwt", "bound_audiences": ["openbao.io"], "user_claim": "sub",
 "bound_claims": {"nomad_job_id": "ferry"},
 "token_policies": ["ssh-sign-ferry", "ferry-vault-password"], "token_ttl": "15m", "token_max_ttl": "1h"}
JSON

# Operator identity only: no SSH signing, vault-password or service credentials. In production
# an OIDC login supplies the entity-bound token; dev's root token creates these demo logins.
say "OpenBao identity OIDC: RS256, audience ferry, allowed and denied demo operators"
bao write identity/oidc/config issuer=http://ferry-lab-bao:8200 >/dev/null
bao write identity/oidc/key/ferry algorithm=RS256 allowed_client_ids=ferry >/dev/null
bao write identity/oidc/role/ferry key=ferry client_id=ferry ttl=5m \
  template='{"name": {{identity.entity.name}}}'  >/dev/null
bao policy write ferry-operator-identity - >/dev/null <<'HCL'
path "identity/oidc/token/ferry" { capabilities = ["read"] }
HCL
TOKEN_ACCESSOR=$(bao auth list -format=json | run_py -c 'import json,sys; print(json.load(sys.stdin)["token/"]["accessor"])')
for person in demo denied; do
  role=ferry-operator
  [ "$person" = denied ] && role=ferry-operator-denied
  entity=$(bao write -field=id identity/entity name="lab-$person")
  printf '%s\n' "$entity" > "$LAB/operators/$person.entity"
  bao write identity/entity-alias name="lab-$person" canonical_id="$entity" mount_accessor="$TOKEN_ACCESSOR" >/dev/null
  bao write "auth/token/roles/$role" allowed_policies=ferry-operator-identity \
    allowed_entity_aliases="lab-$person" orphan=true renewable=false token_explicit_max_ttl=24h >/dev/null
  bao token create -role="$role" -entity-alias="lab-$person" -policy=ferry-operator-identity \
    -field=token > "$LAB/operators/$person.token"
done
chmod 600 "$LAB/operators/"*.token

say "Config repo with one PR, and its git remote"
(
  cd "$LAB/src"
  git init -q -b main && git config user.email lab@lab && git config user.name lab
  mkdir -p inventories/lab/host_vars roles/motd/tasks
  cat > ferry.yaml <<'YAML'
inventories:
  - {name: lab, path: inventories/lab}
YAML
  printf 'apply:\n  run_allowed: ["%s"]\n' "$(cat "$LAB/operators/demo.entity")" >> ferry.yaml
  cat > inventories/lab/hosts.yml <<'YAML'
all:
  hosts:
    target:
      ansible_host: ferry-lab-target
      ansible_python_interpreter: /usr/bin/python3
YAML
  cat > site.yml <<'YAML'
- hosts: all
  gather_facts: false
  roles: [motd]
YAML
  cat > roles/motd/tasks/main.yml <<'YAML'
- name: Write the motd
  ansible.builtin.copy:
    dest: /etc/motd
    content: "{{ motd_text }}\n"

# Proves the vault password reached the job: the value is vaulted in host_vars.
- name: Write the vaulted secret
  ansible.builtin.copy:
    dest: /etc/lab-secret
    content: "{{ lab_secret }}\n"
    mode: "0600"
  no_log: true
YAML
  # A real, committed long-running playbook: contention/expiry must not dirty the operator tree.
  cat > slow.yml <<'YAML'
- hosts: all
  gather_facts: false
  tasks:
    - name: Hold the operator lease for the lab scenarios
      ansible.builtin.pause:
        seconds: 45
YAML
  secret=$(docker run --rm --entrypoint sh ferry:lab -c \
    'printf lab-vault-pass > /tmp/p && ansible-vault encrypt_string --vault-password-file /tmp/p "s3cret-from-vault" --name lab_secret 2>/dev/null')
  { echo 'motd_text: "hello from main"'; echo "$secret"; } > inventories/lab/host_vars/target.yml
  git add -A && git commit -qm "main"
  git checkout -qb feature
  sed -i.bak 's/hello from main/hello from the PR/' inventories/lab/host_vars/target.yml && rm -f inventories/lab/host_vars/target.yml.bak
  git commit -qam "PR: change the motd"
  git clone -q --bare . "$LAB/remote/acme/ansible.git"
  git remote add origin file:///remote/acme/ansible.git
  git -C "$LAB/remote/acme/ansible.git" update-ref refs/pull/1/head "$(git rev-parse HEAD)"
  git rev-parse HEAD > "$LAB/pr_head"
)

say "Fake GitHub API, the managed host, the executor, the ferry service"
run_py -c "
from cryptography.hazmat.primitives.asymmetric import rsa; from cryptography.hazmat.primitives import serialization
k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
open('/lab/secrets/app.pem', 'wb').write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))"
openssl rand -hex 20 > "$LAB/secrets/webhook-secret"
chmod 644 "$LAB/secrets/"*

docker run -d --name ferry-lab-github --network "$NET" -p 18999:8080 -v "$LAB:/lab:ro" -v "$HERE:/lab-tools:ro" \
  -e LAB_HEAD_FILE=/lab/pr_head --entrypoint python ferry:lab /lab-tools/fakegh.py >/dev/null
docker run -d --name ferry-lab-target --network "$NET" -v "$LAB/ca:/lab/ca:ro" ferry-target:lab >/dev/null
# The playbooks run here: user ferry-run, none of the service's secrets mounted, the work dir
# read-only. It refuses to start if it can read any of the paths in FERRY_EXECUTOR_MUST_NOT_READ.
# The socket lives on a named volume: Docker Desktop (macOS) can't chmod a socket on a host bind mount.
docker volume rm -f ferry-lab-run >/dev/null 2>&1 || true
docker volume create ferry-lab-run >/dev/null
# Checkout paths are deleted/recreated for every job. Docker Desktop bind-mount inode caching
# can make a fresh git cwd look deleted; keep shared checkouts on a native runtime volume too.
docker volume rm -f ferry-lab-work >/dev/null 2>&1 || true
docker volume create ferry-lab-work >/dev/null
docker run --rm -u 0 -v ferry-lab-work:/w --entrypoint chmod ferry:lab 777 /w
docker run --rm -u 0 -v ferry-lab-run:/r --entrypoint chmod ferry:lab 1777 /r
docker run -d --name ferry-lab-executor --network "$NET" -u 10002 \
  -v ferry-lab-work:/var/lib/ferry/work:ro -v ferry-lab-run:/run/ferry \
  -e FERRY_EXECUTOR_SOCKET=/run/ferry/executor.sock -e ANSIBLE_HOST_KEY_CHECKING=False \
  -e FERRY_EXECUTOR_MUST_NOT_READ=/secrets/app.pem:/secrets/webhook-secret:/jwt/nomad_openbao.jwt:/var/lib/ferry/state/locks.db \
  ferry:lab executor >/dev/null
# Rootless Docker/Podman shows the host's files as another owner; let git read the lab's remote.
printf '[safe]\n\tdirectory = *\n' > "$LAB/gitconfig"
docker run -d --name ferry-lab-ferry --network "$NET" -p 18080:8080 -v "$LAB/gitconfig:/etc/gitconfig:ro" \
  -v "$LAB/secrets:/secrets:ro" -v "$LAB/jwt:/jwt:ro" -v "$LAB/remote:/remote:ro" -v "$LAB/state:/var/lib/ferry/state" \
  -v ferry-lab-work:/var/lib/ferry/work -v ferry-lab-run:/run/ferry -e FERRY_EXECUTOR_SOCKET=/run/ferry/executor.sock \
  -e FERRY_GITHUB_REPO=acme/ansible -e FERRY_GITHUB_APP_ID=1 -e FERRY_GITHUB_APP_KEY_FILE=/secrets/app.pem \
  -e FERRY_WEBHOOK_SECRET_FILE=/secrets/webhook-secret \
  -e FERRY_COMMIT_STATUS=1 \
  -e FERRY_RUN_OIDC_ISSUER=http://ferry-lab-bao:8200/v1/identity/oidc \
  -e FERRY_RUN_OIDC_AUDIENCE=ferry -e FERRY_RUN_ALLOWED_SUBJECTS="$(cat "$LAB/operators/demo.entity")" \
  -e FERRY_RUN_LEASE_SECONDS=8 \
  -e GITHUB_API_URL=http://ferry-lab-github:8080 -e FERRY_GITHUB_GIT_URL=file:///remote \
  -e FERRY_SSH_USER=root -e ANSIBLE_HOST_KEY_CHECKING=False \
  -e FERRY_OPENBAO_ADDR=http://ferry-lab-bao:8200 -e FERRY_OPENBAO_JWT_FILE=/jwt/nomad_openbao.jwt \
  -e FERRY_OPENBAO_VAULT_PASSWORD_PATH=secret/data/ansible/vault-password \
  ferry:lab >/dev/null
for _ in $(seq 1 30); do curl -sf localhost:18080/healthz >/dev/null && break; sleep 1; done
curl -sf localhost:18080/healthz >/dev/null || { docker logs ferry-lab-ferry; exit 1; }

say "Up. Lab dir: $LAB"
cat <<TXT
  ferry       http://localhost:18080  (POST /webhook, GET /healthz /metrics /jobs)
  fake GitHub http://localhost:18999/_state
  OpenBao     http://localhost:18200  (token: lab)
  Operator:      $HERE/lab.sh run | run-apply | run-contention | run-expiry | run-denied
  Walk the loop:  $HERE/lab.sh open | comments | apply | approve | apply | motd | close
TXT
