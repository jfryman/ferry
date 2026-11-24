#!/usr/bin/env bash
# Stop the lab. LAB_DIR (default /tmp/ferry-lab) is kept for inspection; up.sh wipes it.
docker rm -f ferry-lab-ferry ferry-lab-executor ferry-lab-github ferry-lab-target ferry-lab-bao >/dev/null 2>&1
docker network rm ferry-lab >/dev/null 2>&1
echo "lab stopped"
