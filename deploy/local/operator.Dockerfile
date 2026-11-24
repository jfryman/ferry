# Only the demo operator needs the bao CLI. Keep it out of the service image.
ARG BAO_IMAGE=openbao/openbao:2.7.2
FROM ${BAO_IMAGE} AS bao
FROM ferry:lab
USER root
ARG OPERATOR_UID=10001
RUN getent passwd "$OPERATOR_UID" >/dev/null || useradd --uid "$OPERATOR_UID" --create-home lab-operator
COPY --from=bao /usr/bin/bao /usr/local/bin/bao
COPY operator.sh /usr/local/bin/lab-operator
USER ferry
ENTRYPOINT ["/usr/local/bin/lab-operator"]
