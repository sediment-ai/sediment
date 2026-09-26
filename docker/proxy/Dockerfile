FROM traefik:v3.7.13@sha256:24841fe2de7304c149343d877d2923b4c8800a38ba015dea9174c23b20e344a0
ARG SEDIMENT_SOURCE_REVISION=unverified
ARG SEDIMENT_SOURCE_DIGEST=unverified
LABEL org.opencontainers.image.revision=$SEDIMENT_SOURCE_REVISION \
    io.sediment.source-digest=$SEDIMENT_SOURCE_DIGEST
RUN mkdir -p /data && chown 65532:65532 /data && chmod 0700 /data
USER 65532:65532
