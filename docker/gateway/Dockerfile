# syntax=docker/dockerfile:1.26.0@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
FROM ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc AS uv
FROM ghcr.io/berriai/litellm:v1.102.0@sha256:32cfd7a427f6470033b4dcc9e75dfbea2aa32dbc298b0b511a40e962ce818362 AS runtime-files
# Vendor 1.102.0 predates these compatible fixes. Remove the Python overrides
# when a supported vendor image contains them; dependency consistency gates it.
# The supplied routing/callback does not use the legacy Langfuse v2 SDK. Its
# incompatible packaging constraint must not force an obsolete dependency tree.
# The no-database gateway also excludes the discontinued Prisma Python client,
# its embedded Prisma 5 engines and their Node runtime.
# The supplied Anthropic routes don't need Google GenAI/Vertex SDKs, whose
# constraints prevent a supported websockets release. The entrypoint rejects
# other providers before startup; see docker/gateway/README.md.
# The Anthropic routes also exclude optional PostgreSQL clients and the
# Bedrock real-time dependency tree, including its native awscrt library.
# The vendor pins OS package revisions in world. Release those revision locks
# so the signed distribution repository can supply security fixes.
# Pin reviewed released builds before upgrading: apk sorts development
# snapshots after the corresponding released version.
RUN sed -i 's/=.*//' /etc/apk/world \
    && apk add --no-cache \
        'python-3.13=3.13.15-r8' 'python-3.13-base=3.13.15-r8' \
        'zlib=1.3.2-r7' \
    && apk upgrade --no-cache \
    && apk del --no-cache nodejs libevent \
    && rm /usr/local/bin/pgbouncer
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv pip install --python /app/.venv/bin/python --no-cache \
        'anyio==4.14.2' \
        'tornado==6.5.10' 'pypdf==6.19.0' \
        'protobuf==6.33.6' 'grpcio==1.83.1' 'grpcio-status==1.83.1' \
        'opentelemetry-exporter-otlp==1.44.0' \
        'opentelemetry-instrumentation-fastapi==0.65b0' 'google-cloud-kms==3.16.0' \
        'typing-extensions==4.16.0' 'fastapi-sso==0.22.0' 'referencing==0.37.0' \
        'idna==3.20' 'python-dotenv==1.2.3' 'huggingface-hub==1.32.0' \
        'websockets==17.1' 'mcp==1.30.0' 'gunicorn==26.2.0' \
        'polars==1.44.2' 'cryptography==50.0.1' 'numpy==2.5.3' \
    && uv pip uninstall --python /app/.venv/bin/python \
        langfuse prisma nodeenv backoff google-genai google-cloud-aiplatform \
        psycopg psycopg-binary aws-sdk-bedrock-runtime aws-sdk-signers \
        smithy-aws-core smithy-aws-event-stream smithy-core smithy-http \
        smithy-json awscrt ijson \
    && uv pip check --python /app/.venv/bin/python
RUN --mount=type=bind,source=docker/gateway/patch_proxy.py,target=/tmp/patch_proxy.py \
    python /tmp/patch_proxy.py /app/.venv/lib/python3.13/site-packages/litellm/proxy
COPY docker/gateway/entrypoint.py /usr/local/bin/sediment-gateway.py
RUN rm -rf /opt/prisma /app/.venv/lib/python3.13/site-packages/prisma \
    && mkdir -p /data/delivery \
    && chown 65532:65532 /data/delivery \
    && chmod 0700 /data/delivery

FROM scratch
ARG SEDIMENT_SOURCE_REVISION=unverified
ARG SEDIMENT_SOURCE_DIGEST=unverified
LABEL org.opencontainers.image.revision=$SEDIMENT_SOURCE_REVISION \
    io.sediment.source-digest=$SEDIMENT_SOURCE_DIGEST
COPY --from=runtime-files / /
WORKDIR /app
USER 65532:65532
LABEL io.sediment.removed-packages="langfuse,prisma,nodeenv,nodejs,pgbouncer,libevent,backoff,@prisma/client,@prisma/engines,google-genai,google-cloud-aiplatform,psycopg,psycopg-binary,aws-sdk-bedrock-runtime,aws-sdk-signers,smithy-aws-core,smithy-aws-event-stream,smithy-core,smithy-http,smithy-json,awscrt,ijson" \
    io.sediment.source-patches="LiteLLM proxy database retry decorators removed; docker/gateway/patch_proxy.py" \
    io.sediment.patched-packages="tornado=6.5.10,pypdf=6.19.0,protobuf=6.33.6,grpcio=1.83.1,grpcio-status=1.83.1,opentelemetry-exporter-otlp=1.44.0,opentelemetry-instrumentation-fastapi=0.65b0,google-cloud-kms=3.16.0,typing-extensions=4.16.0,fastapi-sso=0.22.0,referencing=0.37.0,idna=3.20,python-dotenv=1.2.3,huggingface-hub=1.32.0,websockets=17.1,mcp=1.30.0,gunicorn=26.2.0,polars=1.44.2,cryptography=50.0.1,numpy=2.5.3"
ENV PATH="/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin" \
    HOME=/tmp \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
EXPOSE 4000
ENTRYPOINT ["python", "/usr/local/bin/sediment-gateway.py"]
CMD ["--port", "4000"]
