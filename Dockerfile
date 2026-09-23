# syntax=docker/dockerfile:1.26.0@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
# Build tooling stays in the dependency stage. Runtime inputs share Python's ABI.
FROM ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc AS uv
FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS dependencies
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/
COPY packages/capture/pyproject.toml packages/capture/
COPY packages/derive/pyproject.toml packages/derive/
COPY packages/export/pyproject.toml packages/export/
COPY apps/api/pyproject.toml apps/api/
COPY cli/pyproject.toml cli/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --frozen --no-dev --no-install-workspace --no-editable
COPY packages/ packages/
COPY apps/ apps/
COPY cli/ cli/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --frozen --no-dev --no-editable \
    && uv pip check --python /app/.venv/bin/python

FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS runtime-files
# Git is the mirror substrate. Psycopg uses the distribution-maintained libpq
# instead of vendoring native libraries in its binary wheel. Refresh packages.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends git libpq5 \
    # Expat serves only git-http-push (WebDAV push), which mirrors never use.
    # Python's pyexpat bundles its own copy. Remove the Debian library and its
    # sole consumer instead of carrying a vulnerability disposition for them.
    && rm -f /usr/lib/git-core/git-http-push \
    && dpkg --purge --force-depends libexpat1 \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
        /usr/local/lib/python3.12/site-packages/* \
        /usr/local/lib/python3.12/ensurepip /usr/local/bin/pip* \
    && useradd --system --create-home sediment \
    && mkdir -p /data/mirror /data/export /data/staging \
    && chown -R sediment:sediment /data \
    && chmod 700 /data/staging
COPY --from=dependencies /app/.venv /app/.venv
RUN find / -xdev -type f -perm /6000 -exec chmod a-s {} + \
    && rm -f /usr/bin/infocmp

# Copy the cleaned filesystem so removed installer files don't survive in a
# delivered lower layer. Keep dpkg metadata for full runtime inventories.
FROM scratch
ARG SEDIMENT_SOURCE_REVISION=unverified
ARG SEDIMENT_SOURCE_DIGEST=unverified
LABEL org.opencontainers.image.revision=$SEDIMENT_SOURCE_REVISION \
    io.sediment.source-digest=$SEDIMENT_SOURCE_DIGEST \
    io.sediment.removed-packages="libexpat1,git-http-push"
COPY --from=runtime-files / /
WORKDIR /app
USER sediment
ENV PATH="/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin" \
    LANG=C.UTF-8 \
    SEDIMENT_MIRROR_PATH=/data/mirror
EXPOSE 8000
CMD ["uvicorn", "sediment_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
