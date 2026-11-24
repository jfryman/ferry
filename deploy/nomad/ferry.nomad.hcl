# Example Nomad job for the ferry service. Adjust datacenter, image, host volume and the OpenBao
# paths to your cluster. One instance only: locks live in one SQLite file on the host volume.
job "ferry" {
  type = "service"

  group "ferry" {
    count = 1

    network {
      port "http" { to = 8080 }
    }

    # Persistent: the lock file (state/). Losing it frees every lock, so keep it on a host volume,
    # not the alloc dir. Only the ferry task mounts it; the executor never sees it.
    volume "ferry" {
      type      = "host"
      source    = "ferry"
      read_only = false
    }

    service {
      name = "ferry"
      port = "http"
      # Route only POST /webhook from GitHub (restrict to GitHub's hook IP ranges at the edge).
      tags = ["traefik.enable=true"]
      check {
        type     = "http"
        path     = "/healthz"
        interval = "30s"
        timeout  = "5s"
      }
    }

    task "ferry" {
      driver = "docker"

      config {
        image = "ghcr.io/jfryman/ferry:0.2.0"
        ports = ["http"]
      }
      user = "10001"

      volume_mount {
        volume      = "ferry"
        destination = "/var/lib/ferry"
      }

      # Workload identity for OpenBao (jwt auth role "ferry" bound to this job): per-job tokens,
      # SSH certificates from the ssh-client-signer CA, the ansible-vault password.
      identity {
        name        = "openbao"
        aud         = ["openbao.io"]
        file        = true
        change_mode = "restart"
      }

      env {
        # Checkouts and the executor's socket live in the alloc dir, which both tasks see at /alloc.
        # Files there belong to ferry (10001); the executor (10002) can read them, not change them.
        FERRY_WORKDIR                     = "${NOMAD_ALLOC_DIR}/work"
        FERRY_EXECUTOR_SOCKET             = "${NOMAD_ALLOC_DIR}/run/executor.sock"
        FERRY_GITHUB_REPO                 = "OWNER/ansible"
        FERRY_GITHUB_APP_ID               = "000000"
        FERRY_GITHUB_APP_KEY_FILE         = "${NOMAD_SECRETS_DIR}/app.pem"
        FERRY_WEBHOOK_SECRET_FILE         = "${NOMAD_SECRETS_DIR}/webhook-secret"
        FERRY_SSH_USER                    = "root"
        FERRY_OPENBAO_ADDR                = "https://active.openbao.service.consul:8200"
        FERRY_OPENBAO_JWT_FILE            = "${NOMAD_SECRETS_DIR}/nomad_openbao.jwt"
        FERRY_OPENBAO_CA_CERT             = "/etc/ssl/certs/ca-certificates.crt"
        FERRY_OPENBAO_VAULT_PASSWORD_PATH = "infra/data/ansible/vault-password"
        FERRY_DRIFT_INTERVAL_HOURS        = "6"
      }

      # The App key and webhook secret, from OpenBao (or Vault) via the job's own workload identity.
      template {
        destination = "${NOMAD_SECRETS_DIR}/app.pem"
        perms       = "0400"
        data        = "{{ with secret \"kvv2/data/ferry\" }}{{ .Data.data.app_private_key }}{{ end }}"
      }
      template {
        destination = "${NOMAD_SECRETS_DIR}/webhook-secret"
        perms       = "0400"
        data        = "{{ with secret \"kvv2/data/ferry\" }}{{ .Data.data.webhook_secret }}{{ end }}"
      }

      resources {
        cpu    = 200
        memory = 256
      }
    }

    # The playbooks run here, as another user, with none of the service's secrets: no identity, no
    # templates, no host volume. A plan's playbook (lookups, check_mode: false tasks, plugins) can
    # read only its own job's SSH certificate and vault password, which the service sends per job.
    task "executor" {
      driver = "docker"
      user   = "10002"

      lifecycle {
        hook    = "prestart"
        sidecar = true
      }

      config {
        image   = "ghcr.io/jfryman/ferry:0.2.0"
        command = "executor"
      }

      env {
        FERRY_WORKDIR                = "${NOMAD_ALLOC_DIR}/work"
        FERRY_EXECUTOR_SOCKET        = "${NOMAD_ALLOC_DIR}/run/executor.sock"
        # Refuse to start if the separation is broken.
        FERRY_EXECUTOR_MUST_NOT_READ = "/var/lib/ferry/state/locks.db"
        ANSIBLE_HOST_KEY_CHECKING    = "True" # and mount your known_hosts at /etc/ssh/ssh_known_hosts
      }

      resources {
        cpu    = 1000
        memory = 1024
      }
    }
  }
}
