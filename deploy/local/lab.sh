#!/usr/bin/env bash
# Drive the lab like GitHub would: signed webhooks, approvals, pushes. See up.sh.
set -euo pipefail
LAB=${LAB_DIR:-/tmp/ferry-lab}
SECRET=$(cat "$LAB/secrets/webhook-secret")
REPO=acme/ansible

deliver() {  # deliver EVENT BODY
  local sig
  sig="sha256=$(printf '%s' "$2" | openssl dgst -sha256 -hmac "$SECRET" -r | cut -d' ' -f1)"
  curl -s -X POST localhost:18080/webhook -H "X-GitHub-Event: $1" -H "X-GitHub-Delivery: lab-$(date +%s%N)" \
    -H "X-Hub-Signature-256: $sig" -H "Content-Type: application/json" --data-binary "$2"
}
pr_event() {  # pr_event ACTION AUTHOR
  deliver pull_request "{\"action\":\"$1\",\"repository\":{\"full_name\":\"$REPO\"},\"pull_request\":{\"number\":1,\"draft\":false,\"user\":{\"login\":\"$2\"},\"head\":{\"sha\":\"$(cat "$LAB/pr_head")\",\"repo\":{\"full_name\":\"$REPO\"}}}}"
}
comment() {  # comment USER BODY
  deliver issue_comment "{\"action\":\"created\",\"repository\":{\"full_name\":\"$REPO\"},\"issue\":{\"number\":1,\"pull_request\":{}},\"comment\":{\"user\":{\"login\":\"$1\"},\"body\":\"$2\"}}"
}
wait_idle() {  # until the worker has nothing queued and the last job changed
  local before; before=$(curl -s localhost:18080/healthz)
  for _ in $(seq 1 60); do
    sleep 1; now=$(curl -s localhost:18080/healthz)
    [ "$now" != "$before" ] && echo "$now" | grep -q '"queued": 0' && { echo "$now"; return; }
  done
  echo "timed out waiting for the job; see: docker logs ferry-lab-ferry"
}

case "${1:-help}" in
  open)     pr_event opened "${2:-pip}"; echo; [ "${2:-pip}" = pip ] && wait_idle ;;
  plan)     comment "${2:-james}" "/ferry plan"; echo; wait_idle ;;
  apply)    comment "${2:-james}" "/ferry apply"; echo; wait_idle ;;
  approve)  curl -s -X POST localhost:18999/_approve -d "{\"user\":\"${2:-james}\"}"; echo ;;
  push)     # push "TEXT": a new commit on the PR branch (the PR moves on), then synchronize
            git -C "$LAB/src" checkout -q feature
            sed -i.bak "s/^motd_text: .*/motd_text: \"${2:-pushed again}\"/" "$LAB/src/inventories/lab/host_vars/target.yml"
            rm -f "$LAB/src/inventories/lab/host_vars/target.yml.bak"
            git -C "$LAB/src" commit -qam "PR: ${2:-pushed again}"
            git -C "$LAB/src" push -q "$LAB/remote/acme/ansible.git" feature
            git -C "$LAB/remote/acme/ansible.git" update-ref refs/pull/1/head "$(git -C "$LAB/src" rev-parse HEAD)"
            git -C "$LAB/src" rev-parse HEAD > "$LAB/pr_head"
            pr_event synchronize pip; echo; wait_idle ;;
  close)    pr_event closed pip; echo; wait_idle ;;
  evil)     # a hostile PR (passes the static policy): during the plan, on the controller, it tries the
            # service's secret files, dumps its environment and every readable /proc/*/environ, and
            # ships it all to the target with the job's certificate. Then `lab.sh leakcheck`.
            git -C "$LAB/src" checkout -q feature
            cp "$(dirname "$0")/evil.yml" "$LAB/src/evil.yml"
            mkdir -p "$LAB/src/lookup_plugins" && cp "$(dirname "$0")/evil_lookup.py" "$LAB/src/lookup_plugins/loot.py"
            grep -q evil.yml "$LAB/src/site.yml" || printf -- '- import_playbook: evil.yml\n' >> "$LAB/src/site.yml"
            git -C "$LAB/src" add -A && git -C "$LAB/src" commit -qm "PR: totally normal change"
            git -C "$LAB/src" push -q "$LAB/remote/acme/ansible.git" feature
            git -C "$LAB/remote/acme/ansible.git" update-ref refs/pull/1/head "$(git -C "$LAB/src" rev-parse HEAD)"
            git -C "$LAB/src" rev-parse HEAD > "$LAB/pr_head"
            pr_event synchronize pip; echo; wait_idle ;;
  leakcheck) # what the hostile plan got, without printing any secret
            docker exec ferry-lab-target cat /tmp/ferry-leak > "$LAB/leak.txt"
            grep -E '^(DENIED|READ|LOOKUP|JOBCRED|ID) ' "$LAB/leak.txt" || true
            echo "env vars the playbook saw: $(sed -n '/^ENV$/,/^PROC$/p' "$LAB/leak.txt" | grep -c '=')"
            echo "  of them FERRY_*: $(sed -n '/^ENV$/,/^PROC$/p' "$LAB/leak.txt" | grep -c '^FERRY_' || true)"
            found=0
            for s in "$(cat "$LAB/secrets/webhook-secret")" "$(cat "$LAB/jwt/nomad_openbao.jwt")" \
                     "$(sed -n 2p "$LAB/secrets/app.pem")"; do
              grep -qF -- "$s" "$LAB/leak.txt" && found=$((found + 1))
            done
            echo "service secrets found in what the playbook shipped out: $found of 3" ;;
  comments) curl -s localhost:18999/_state | python3 -c "$(printf '%s\n' 'import json, sys' \
              'for c in json.load(sys.stdin)["comments"]:' \
              '    print("---- comment", c["id"]); print(c["body"]); print()')" ;;
  motd)     docker exec ferry-lab-target sh -c 'echo "motd: $(cat /etc/motd 2>/dev/null)"; echo "vaulted secret: $(cat /etc/lab-secret 2>/dev/null)"' ;;
  locks)    docker exec ferry-lab-ferry python -c 'import sqlite3; print(sqlite3.connect("/var/lib/ferry/state/locks.db").execute("select * from locks").fetchall())' ;;
  sshlog)   docker logs ferry-lab-target 2>&1 | grep -E "Accepted|refused|Refusing|CERT" | tail -${2:-5} ;;
  logs)     docker logs --tail "${2:-50}" ferry-lab-ferry ;;
  *)        sed -n 's/^  \([a-z]*\)).*/  \1/p' "$0"; echo "  (see deploy/local/README.md)";;
esac
