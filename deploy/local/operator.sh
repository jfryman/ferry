#!/bin/sh
# This container sees the config remote and ONE operator login token, not /lab, /secrets,
# the service JWT, its signing policy, state DB or executor socket.
set -eu
export BAO_ADDR=http://ferry-lab-bao:8200
export BAO_TOKEN="$(cat /operator/token)"
export ANSIBLE_HOST_KEY_CHECKING=False # throwaway target, as for webhook lab runs
export FERRY_SSH_USER=root
export ANSIBLE_REMOTE_USER=root
export HOME=/tmp/operator-home
mkdir -m 700 "$HOME"
# Check mode runs the PR branch; an apply must name the default-branch head (LAB_BRANCH=main).
git clone -q --branch "${LAB_BRANCH:-feature}" file:///remote/acme/ansible.git "$HOME/repo"
cd "$HOME/repo"
# OpenBao 2.7 identity token generation is a GET (bao read), not bao write.
exec ferry run -i lab --service http://ferry-lab-ferry:8080 --insecure-http \
  --token-command 'bao read -field=token identity/oidc/token/ferry' "$@"
