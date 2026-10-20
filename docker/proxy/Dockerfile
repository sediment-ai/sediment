FROM traefik:v3.7.15@sha256:0f6150202bc1fed72a7bd2107460eaa510e5b8a905f3fdb5b84f2c8619c33368
ARG SEDIMENT_SOURCE_REVISION=unverified
ARG SEDIMENT_SOURCE_DIGEST=unverified
LABEL org.opencontainers.image.revision=$SEDIMENT_SOURCE_REVISION \
    io.sediment.source-digest=$SEDIMENT_SOURCE_DIGEST
RUN mkdir -p /data && chown 65532:65532 /data && chmod 0700 /data
USER 65532:65532
