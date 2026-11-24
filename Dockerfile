# ferry service image: ferry + ansible-core + git/ssh. Config repos' collections are installed per
# requirements.yml into the work dir at run time (cached by hash), so the image stays generic.
FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/jfryman/ferry" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY . /src
RUN pip install --no-cache-dir "/src[service]" "ansible-core~=2.20.0" \
 && rm -rf /src

# Not root: ferry needs no local privileges. It logs in to hosts with a per-job SSH certificate.
# Host keys are checked: mount known_hosts at /etc/ssh/ssh_known_hosts (pinned keys or @cert-authority).
# ferry-run runs the playbooks (`ferry executor`, normally its own container): a different user, so a
# playbook can't read or write what the service owns.
RUN useradd --uid 10001 --create-home --home-dir /var/lib/ferry ferry \
 && useradd --uid 10002 --create-home --home-dir /home/ferry-run ferry-run \
 && chmod 0755 /var/lib/ferry  # the executor reaches the work dir through it; secrets never live here
USER ferry
WORKDIR /var/lib/ferry
ENV FERRY_WORKDIR=/var/lib/ferry/work \
    FERRY_LOCK_DB=/var/lib/ferry/state/locks.db \
    ANSIBLE_HOST_KEY_CHECKING=True \
    PYTHONUNBUFFERED=1

EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status == 200 else 1)"
ENTRYPOINT ["ferry"]
CMD ["serve"]
