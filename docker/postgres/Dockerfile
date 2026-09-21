# syntax=docker/dockerfile:1.26.0@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
FROM postgres:17.11-bookworm@sha256:051f7b7b3abdd564d5d1bd1e8c4b9c1b6e77087d1dd22020ede611c096a272e0 AS runtime-files
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*
RUN --mount=type=bind,source=docker/postgres/patch-entrypoint.sh,target=/tmp/patch-entrypoint.sh \
    sh /tmp/patch-entrypoint.sh /usr/local/bin/docker-entrypoint.sh \
    && rm /usr/local/bin/gosu \
    && setpriv --reuid=postgres --regid=postgres --init-groups true
RUN find / -xdev -type f -perm /6000 -exec chmod a-s {} + \
    && rm -f /usr/bin/infocmp

# Preserve the official filesystem, locale and initialization sequence without
# shipping the removed Go binary in a lower layer.
FROM scratch
ARG SEDIMENT_SOURCE_REVISION=unverified
ARG SEDIMENT_SOURCE_DIGEST=unverified
LABEL org.opencontainers.image.revision=$SEDIMENT_SOURCE_REVISION \
    io.sediment.source-digest=$SEDIMENT_SOURCE_DIGEST
COPY --from=runtime-files / /
ENV PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/postgresql/17/bin" \
    LANG=en_US.utf8 \
    PG_MAJOR=17 \
    PGDATA=/var/lib/postgresql/data
VOLUME /var/lib/postgresql/data
EXPOSE 5432
ENTRYPOINT ["docker-entrypoint.sh"]
STOPSIGNAL SIGINT
CMD ["postgres"]
