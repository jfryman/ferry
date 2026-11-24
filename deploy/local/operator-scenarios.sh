# Sourced by lab.sh. Container lifecycle here is only for short-lived operator clients;
# up.sh/down.sh remain the owner of the lab services.
operator() {
  local person=${LAB_OPERATOR:-demo}
  docker run --rm -u "$(id -u):$(id -g)" --network ferry-lab \
    -e LAB_BRANCH="${LAB_BRANCH:-feature}" \
    -v "$LAB/remote:/remote:ro" -v "$LAB/operators/$person.token:/operator/token:ro" \
    ferry-operator:lab "$@"
}
assert_output() {
  if ! printf '%s' "$3" | grep -qiE -- "$2"; then
    printf 'FAIL %s: wanted /%s/, got: %s\n' "$1" "$2" "$3" >&2
    return 1
  fi
  printf 'PASS %s\n' "$1"
}
start_operator() {
  docker rm -f ferry-lab-operator >/dev/null 2>&1 || true
  docker run -d --name ferry-lab-operator -u "$(id -u):$(id -g)" --network ferry-lab \
    -v "$LAB/remote:/remote:ro" -v "$LAB/operators/demo.token:/operator/token:ro" \
    ferry-operator:lab slow.yml >/dev/null
  # Observe the actual shared lock, not just container liveness or a fixed sleep.
  local subject rows
  subject=$(cat "$LAB/operators/demo.entity")
  for _ in $(seq 1 30); do
    rows=$("$0" locks)
    if printf '%s' "$rows" | grep -qE "run:|$subject"; then return 0; fi
    if [ "$(docker inspect -f '{{.State.Running}}' ferry-lab-operator)" != true ]; then break; fi
    sleep 1
  done
  docker logs ferry-lab-operator >&2
  echo 'operator did not acquire a lock' >&2
  return 1
}
stop_operator() { docker rm -f ferry-lab-operator >/dev/null 2>&1 || true; }
# These scenarios intentionally alter PR state; close releases the PR lock before starting.
case "$1" in
  run) operator site.yml ;;
  run-apply) LAB_BRANCH=main operator --apply site.yml ;;
  run-apply-unmerged)
    if out=$(operator --apply site.yml 2>&1); then
      echo 'FAIL apply of an unmerged SHA was accepted' >&2; exit 1
    fi
    assert_output apply-needs-main-head '409' "$out" ;;
  run-denied)
    if out=$(LAB_OPERATOR=denied operator site.yml 2>&1); then
      echo 'FAIL denied subject was accepted' >&2; exit 1
    fi
    assert_output denied-subject '403|forbidden|not authorized|not allowed|unauthorized' "$out" ;;
  run-contention)
    # Leave a clean plan comment for this exact SHA, but release its lock before the CLI.
    "$0" close >/dev/null
    "$0" open >/dev/null
    assert_output clean-pr-plan 'plan is clean' "$(last_comment)"
    "$0" approve >/dev/null
    "$0" close >/dev/null
    trap stop_operator EXIT
    start_operator
    # A real PR apply is refused before credentials, while the operator renews its lease.
    curl -sf -X POST localhost:18999/_pr_state -d '{"state":"open"}' >/dev/null
    "$0" approve >/dev/null
    comment james '/ferry apply' >/dev/null; wait_idle >/dev/null
    assert_output operator-blocks-pr 'lock|held|busy' "$(last_comment)"
    stop_operator
    sleep 10 # killed clients cannot finish: wait beyond the eight-second lease
    # PR plan takes and retains the same inventory lock; CLI now gets HTTP 409.
    "$0" open >/dev/null
    if out=$(operator site.yml 2>&1); then
      echo 'FAIL PR-held lock accepted an operator run' >&2; exit 1
    fi
    assert_output pr-blocks-operator '409|lock|held|busy' "$out"
    "$0" close >/dev/null
    trap - EXIT ;;
  run-expiry)
    "$0" close >/dev/null
    trap stop_operator EXIT
    start_operator
    docker kill --signal KILL ferry-lab-operator >/dev/null
    sleep 10 # no renewal/finally possible after SIGKILL; next request must reap the lease
    operator site.yml
    echo 'PASS killed operator lease expired; next operator acquired and finished'
    stop_operator
    trap - EXIT ;;
  *) echo "unknown operator scenario: $1" >&2; exit 2 ;;
esac
