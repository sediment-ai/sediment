# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opt-in pilot acceptance with real containers, local TLS, and synthetic inference.

Set SEDIMENT_TEST_COMPOSE=1 and SEDIMENT_TEST_{API,POSTGRES,GATEWAY,PROXY}_IMAGE.
No public DNS, certificate authority, or model provider receives test traffic.
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = "sediment.example.com"


@pytest.mark.skipif(
    os.environ.get("SEDIMENT_TEST_COMPOSE") != "1", reason="opt-in Compose acceptance"
)
def test_pilot_https_capture_auth_streaming_and_restart(tmp_path):
    images = {}
    for kind in ("API", "POSTGRES", "GATEWAY", "PROXY"):
        images[kind.lower()] = os.environ.get(f"SEDIMENT_TEST_{kind}_IMAGE")
        assert images[kind.lower()], f"Set SEDIMENT_TEST_{kind}_IMAGE"
    project = "sediment-pilot-test-" + uuid4().hex[:12]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("SEDIMENT_", "COMPOSE_", "ANTHROPIC_", "LITELLM_", "POSTGRES_")
        )
    }

    def command(args, *, timeout=120):
        result = subprocess.run(
            args,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        assert result.returncode == 0, result.stderr[-8000:]
        return result.stdout

    # The public-domain input is real; only certificate issuance is substituted.
    setup_env = {
        **environment,
        "ANTHROPIC_API_KEY": "sk-ant-test-only-12345678901234567890",
        "SEDIMENT_ACME_EMAIL": "ops@example.com",
    }
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/create_deploy_env.py"),
            "--domain",
            DOMAIN,
            "--output",
            str(tmp_path / ".env"),
        ],
        cwd=ROOT,
        env=setup_env,
        capture_output=True,
        check=True,
    )
    credentials = dotenv_values(tmp_path / ".env")
    rendered = command(
        [
            "docker",
            "compose",
            "--env-file",
            str(tmp_path / ".env"),
            "config",
            "--format",
            "json",
        ]
    )
    config = json.loads(rendered)
    config.pop("name", None)
    services = config["services"]
    for name, service in services.items():
        service.pop("build", None)
        service.pop("profiles", None)
        service["image"] = images["api" if name == "migrate" else name]
        for port in service.get("ports", []):
            port["host_ip"] = "127.0.0.1"
            port["published"] = "0"
    for resource in (*config["networks"].values(), *config["volumes"].values()):
        resource.pop("name", None)

    certificates = tmp_path / "certificates"
    certificates.mkdir(mode=0o755)
    command(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            f"/CN={DOMAIN}",
            "-addext",
            f"subjectAltName=DNS:{DOMAIN},IP:127.0.0.1",
            "-keyout",
            str(certificates / "key.pem"),
            "-out",
            str(certificates / "cert.pem"),
        ]
    )
    (certificates / "key.pem").chmod(
        0o644
    )  # Synthetic key inside a private test directory.
    context = ssl.create_default_context(cafile=str(certificates / "cert.pem"))
    routes = yaml.safe_load((ROOT / "docker/proxy/routes.yml").read_text())
    for router in routes["http"]["routers"].values():
        router["tls"] = {}
    routes["tls"] = {
        "certificates": [
            {
                "certFile": "/test-certificates/cert.pem",
                "keyFile": "/test-certificates/key.pem",
            }
        ]
    }
    routes["tls"]["stores"] = {
        "default": {
            "defaultCertificate": {
                "certFile": "/test-certificates/cert.pem",
                "keyFile": "/test-certificates/key.pem",
            }
        }
    }
    (tmp_path / "routes.yml").write_text(yaml.safe_dump(routes))
    proxy = services["proxy"]
    proxy["environment"] = {
        k: v
        for k, v in proxy["environment"].items()
        if not k.startswith("TRAEFIK_CERTIFICATESRESOLVERS_")
    }
    proxy["entrypoint"] = ["traefik"]
    for mount in proxy["volumes"]:
        if mount["target"] == "/etc/traefik/routes.yml":
            mount["source"] = str(tmp_path / "routes.yml")
    proxy["volumes"].append(
        {
            "type": "bind",
            "source": str(certificates),
            "target": "/test-certificates",
            "read_only": True,
        }
    )
    gateway_config = yaml.safe_load((ROOT / "litellm/config.yaml").read_text())
    for route in gateway_config["model_list"]:
        route["litellm_params"]["api_base"] = "http://provider:9100"
    (tmp_path / "gateway.yml").write_text(yaml.safe_dump(gateway_config))
    for mount in services["gateway"]["volumes"]:
        if mount["target"] == "/app/config.yaml":
            mount["source"] = str(tmp_path / "gateway.yml")
    services["gateway"]["environment"]["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    services["provider"] = {
        "image": images["api"],
        "entrypoint": ["python", "/provider.py"],
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "networks": {"edge": {}},
        "volumes": [
            {
                "type": "bind",
                "source": str(ROOT / "scripts/tests/fixtures/pilot_provider.py"),
                "target": "/provider.py",
                "read_only": True,
            }
        ],
    }
    manifest = tmp_path / "compose.json"
    manifest.write_text(json.dumps(config))
    manifest.chmod(0o600)
    compose = ["docker", "compose", "--project-name", project, "-f", str(manifest)]

    def port(target):
        return int(
            command([*compose, "port", "proxy", str(target)]).strip().rsplit(":", 1)[1]
        )

    def request(path, *, token=None, host=DOMAIN, method="GET", body=None, extra=None):
        connection = http.client.HTTPSConnection(
            "127.0.0.1", https_port, context=context, timeout=15
        )
        headers = {"Host": host, **(extra or {})}
        if token:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(
            method, path, json.dumps(body) if body is not None else None, headers
        )
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def facts():
        status, body = request(
            "/v1/facts", token=credentials["SEDIMENT_OPERATOR_TOKEN"]
        )
        assert status == 200
        return json.loads(body)

    try:
        command([*compose, "up", "-d", "--wait", "--wait-timeout", "180"], timeout=240)
        https_port = port(8443)
        connection = http.client.HTTPConnection("127.0.0.1", port(8080), timeout=5)
        connection.request("GET", "/health", headers={"Host": DOMAIN})
        response = connection.getresponse()
        assert response.status in (301, 308)
        assert response.getheader("Location") == f"https://{DOMAIN}/health"
        response.read()
        connection.close()
        assert request("/health")[0] == 200
        assert request("/llm/health/liveliness")[0] == 200
        assert request("/health", host="wrong.example.com")[0] == 404
        assert request("/v1/facts")[0] == 401
        assert (
            request("/v1/facts", token=credentials["SEDIMENT_GATEWAY_INGEST_TOKEN"])[0]
            == 403
        )
        assert request("/llm/v1/models")[0] == 401
        assert request("/llm/v1/models", token="sk-incorrect")[0] == 401
        assert (
            request("/llm/v1/models", token=credentials["LITELLM_MASTER_KEY"])[0] == 200
        )
        assert facts()["sessions"] == 0

        for index, (endpoint, streaming) in enumerate(
            [
                ("/v1/chat/completions", False),
                ("/v1/chat/completions", True),
                ("/v1/messages", False),
                ("/v1/messages", True),
            ],
            1,
        ):
            session = str(uuid4())
            body = {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "Compose acceptance test"}],
                "stream": streaming,
            }
            headers = {
                "Host": DOMAIN,
                "Authorization": "Bearer " + credentials["LITELLM_MASTER_KEY"],
                "Content-Type": "application/json",
                "x-sediment-session": session,
                "anthropic-version": "2023-06-01",
            }
            connection = http.client.HTTPSConnection(
                "127.0.0.1", https_port, context=context, timeout=15
            )
            connection.request("POST", "/llm" + endpoint, json.dumps(body), headers)
            response = connection.getresponse()
            assert response.status == 200, response.read()[:500]
            if streaming:
                start = time.monotonic()
                pieces = []
                while b"Compose capture works" not in b"".join(pieces):
                    line = response.readline()
                    assert line, pieces
                    pieces.append(line)
                assert time.monotonic() - start < 1.5, "proxy buffered the stream"
                response.read()
            else:
                assert b"Compose capture works" in response.read()
            connection.close()
            for _ in range(40):
                if facts()["tables"]["inference_calls"]["total"] == index:
                    break
                time.sleep(0.25)
            assert facts()["tables"]["inference_calls"]["total"] == index
            status, data = request(
                "/query/session/" + session,
                token=credentials["SEDIMENT_OPERATOR_TOKEN"],
            )
            assert status == 200
            dossier = json.loads(data)
            assert dossier["found"] and dossier["session_id"] == session
            assert dossier["coverage"]["inference_calls"]["visible"] == 1

        # Reuse connections so the burst tests the limiter rather than TLS handshakes.
        def burst(worker):
            connection = http.client.HTTPSConnection(
                "127.0.0.1", https_port, context=context, timeout=15
            )
            statuses = []
            for number in range(30):
                connection.request(
                    "GET",
                    "/health",
                    headers={
                        "Host": DOMAIN,
                        "X-Forwarded-For": f"198.51.{worker}.{number}",
                    },
                )
                response = connection.getresponse()
                statuses.append(response.status)
                response.read()
            connection.close()
            return statuses

        with ThreadPoolExecutor(max_workers=20) as pool:
            statuses = [
                status for batch in pool.map(burst, range(20)) for status in batch
            ]
        assert 429 in statuses, "forwarded address spoofing bypassed request throttling"
        assert set(statuses) <= {200, 429}
        command(
            [
                *compose,
                "exec",
                "-T",
                "proxy",
                "sh",
                "-ec",
                "test $(id -u) != 0; echo retained > /data/retention-check",
            ]
        )
        command([*compose, "down"])
        command([*compose, "up", "-d", "--wait", "--wait-timeout", "180"], timeout=240)
        https_port = port(8443)
        assert facts()["sessions"] == 4
        assert facts()["tables"]["inference_calls"]["total"] == 4
        assert (
            command(
                [*compose, "exec", "-T", "proxy", "cat", "/data/retention-check"]
            ).strip()
            == "retained"
        )
    finally:
        command([*compose, "down", "--volumes", "--remove-orphans"])
