# syntax=docker/dockerfile:1.26.0@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
FROM ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 AS uv
FROM ghcr.io/berriai/litellm:v1.103.2@sha256:f63fb81b831b170ec16851e23c36ac5bf52ef106b271406429524a2ed730bbfd AS runtime-files
# Vendor 1.103.2 retains dependencies that need these compatible updates.
# Remove the Python overrides when a supported vendor image contains them;
# dependency consistency gates it.
# The supplied routing/callback does not use the legacy Langfuse v2 SDK. Its
# incompatible packaging constraint must not force an obsolete dependency tree.
# The no-database gateway also excludes the discontinued Prisma Python client,
# its embedded Prisma 5 engines and their Node runtime.
# The supplied Anthropic routes don't need Google GenAI/Vertex SDKs, whose
# constraints prevent a supported websockets release. The entrypoint rejects
# other providers before startup; see docker/gateway/README.md.
# The Anthropic routes also exclude optional PostgreSQL clients and the
# Bedrock real-time dependency tree, including its native awscrt library,
# plus the Vertex speech SDK.
# The vendor pins OS package revisions in world. Release those revision locks
# so the signed distribution repository can supply security fixes.
# Wolfi builds Python 3.13 from the CPython maintenance branch. Pin the reviewed
# build: 3.13.16_git20261002-r0 is the v3.13.16 tag plus its post-release
# version bump, linked against OpenSSL 3.6. Its -r1 rebuild moves to OpenSSL 4.
# Keep the reviewed OpenSSL 3.6 package set: the 4.0 transition has conflicting
# configuration ownership.
# Other pins retain the package revisions in security/maintenance.json.
RUN sed -i 's/=.*//' /etc/apk/world \
    && apk add --no-cache \
        'python-3.13=3.13.16_git20261002-r0' \
        'python-3.13-base=3.13.16_git20261002-r0' \
        'zlib=1.3.2-r7' \
        'openssl=3.6.4-r7' 'libcrypto3=3.6.4-r7' 'libssl3=3.6.4-r7' \
        'openssl-provider-legacy-allowed=4.0.2-r3' \
        'apk-tools=2.14.10-r14' 'ca-certificates-bundle=20260909-r0' \
        'libuuid=2.42.3-r4' \
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
        'typing-extensions==4.16.0' 'fastapi-sso==0.23.0' 'oauthlib==4.0.0' 'referencing==0.37.0' \
        'idna==3.20' 'python-dotenv==1.2.4' 'tokenizers==0.23.1' \
        'websockets==17.2' 'gunicorn==26.2.0' \
        'PyJWT==2.15.1' 'urllib3==2.8.0' 'polars==1.44.2' 'cryptography==50.0.2' 'numpy==2.5.3' \
    && uv pip uninstall --python /app/.venv/bin/python \
        langfuse prisma nodeenv backoff google-genai google-cloud-aiplatform \
        psycopg psycopg-binary aws-sdk-bedrock-runtime aws-sdk-signers \
        smithy-aws-core smithy-aws-event-stream smithy-core smithy-http \
        smithy-json awscrt ijson google-cloud-speech \
    && uv pip check --python /app/.venv/bin/python
# Tokenizers' Rust Hub download caller retains its API in Hub 2. Keep the
# compatibility declaration explicit, hash guarded, and tested at that caller.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=docker/gateway/patch_tokenizers.py,target=/tmp/patch_tokenizers.py \
    python /tmp/patch_tokenizers.py /app/.venv/lib/python3.13/site-packages/tokenizers-0.23.1.dist-info \
    && uv pip install --python /app/.venv/bin/python --no-cache \
        'huggingface-hub==2.1.1' 'httpx2==2.13.1' 'httpcore2==2.13.1' \
        'truststore==0.10.4' 'mcp==2.3.0' 'mcp-types==2.3.0' \
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
LABEL io.sediment.removed-packages="langfuse,prisma,nodeenv,nodejs,pgbouncer,libevent,backoff,@prisma/client,@prisma/engines,google-genai,google-cloud-aiplatform,psycopg,psycopg-binary,aws-sdk-bedrock-runtime,aws-sdk-signers,smithy-aws-core,smithy-aws-event-stream,smithy-core,smithy-http,smithy-json,awscrt,ijson,google-cloud-speech" \
    io.sediment.source-patches="LiteLLM database retries removed, absent Prisma predicates guarded, and master-key errors normalized; docker/gateway/patch_proxy.py; Tokenizers 0.23.1 tested Hub 2.1.1 dependency declaration; docker/gateway/patch_tokenizers.py" \
    io.sediment.patched-packages="tornado=6.5.10,pypdf=6.19.0,protobuf=6.33.6,grpcio=1.83.1,grpcio-status=1.83.1,opentelemetry-exporter-otlp=1.44.0,opentelemetry-instrumentation-fastapi=0.65b0,google-cloud-kms=3.16.0,typing-extensions=4.16.0,fastapi-sso=0.23.0,oauthlib=4.0.0,referencing=0.37.0,idna=3.20,python-dotenv=1.2.4,tokenizers=0.23.1,huggingface-hub=2.1.1,httpx2=2.13.1,httpcore2=2.13.1,truststore=0.10.4,mcp=2.3.0,mcp-types=2.3.0,websockets=17.2,gunicorn=26.2.0,PyJWT=2.15.1,urllib3=2.8.0,polars=1.44.2,cryptography=50.0.2,numpy=2.5.3"
ENV PATH="/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin" \
    HOME=/tmp \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
EXPOSE 4000
ENTRYPOINT ["python", "/usr/local/bin/sediment-gateway.py"]
CMD ["--port", "4000"]
