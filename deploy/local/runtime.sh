# Sourced by up.sh, down.sh and lab.sh: which container runtime to drive.
#   LAB_RUNTIME=docker   Docker (or anything that behaves like it), as `docker`
#   LAB_RUNTIME=podman   rootless podman, through ./podman-docker
# Unset: docker if it's installed and isn't podman's docker alias, else podman.
_lab_here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [ -z "${LAB_RUNTIME:-}" ]; then
  if command -v docker >/dev/null 2>&1 && ! docker --version 2>/dev/null | grep -qi podman; then
    LAB_RUNTIME=docker
  elif command -v podman >/dev/null 2>&1; then
    LAB_RUNTIME=podman
  else
    echo "the lab needs docker or podman" >&2; exit 1
  fi
fi
if [ "$LAB_RUNTIME" = podman ]; then
  docker() { "$_lab_here/podman-docker" "$@"; }
  # Files written inside a container's user namespace can belong to your subuids: delete them there.
  wipe() { env -u CONTAINER_HOST podman unshare rm -rf "$@"; }  # unshare is local-only
else
  wipe() { rm -rf "$@"; }
fi
