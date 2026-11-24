#!/usr/bin/env bash
. "$(cd "$(dirname "$0")" && pwd)/runtime.sh"
# Stop the lab. LAB_DIR (default /tmp/ferry-lab) is kept for inspection; up.sh wipes it.
docker rm -f ferry-lab-ferry ferry-lab-executor ferry-lab-github ferry-lab-target ferry-lab-bao >/dev/null 2>&1
docker volume rm -f ferry-lab-run >/dev/null 2>&1 || true
docker network rm ferry-lab >/dev/null 2>&1
echo "lab stopped"
