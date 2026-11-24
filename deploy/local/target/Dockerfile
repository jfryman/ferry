# A managed host for the lab: sshd that trusts the SSH user CA exactly like a CA-managed production host's
# roles/ssh_ca: root accepts certificates only for principal "ferry", no authorized_keys at all.
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-server python3 \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd /etc/ssh/auth_principals \
 && echo ferry > /etc/ssh/auth_principals/root \
 && printf '%s\n' \
    'TrustedUserCAKeys /lab/ca/user_ca.pub' \
    'AuthorizedPrincipalsFile /etc/ssh/auth_principals/%u' \
    'PermitRootLogin prohibit-password' \
    'PasswordAuthentication no' \
    'AuthorizedKeysFile none' \
    > /etc/ssh/sshd_config.d/40-ferry-user-ca.conf
# Host keys at start (not baked), then sshd in the foreground, logging auth to stderr.
CMD ["sh", "-c", "ssh-keygen -A >/dev/null && exec /usr/sbin/sshd -D -e"]
