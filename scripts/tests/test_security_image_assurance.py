# SPDX-License-Identifier: AGPL-3.0-or-later
"""Artifact evidence must fail closed when source or deployment controls change."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import sysconfig
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def assurance():
    spec = importlib.util.spec_from_file_location(
        "security_image_assurance", ROOT / "scripts/security_image_assurance.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_tree(root):
    for name in (
        "Dockerfile",
        "uv.lock",
        "pyproject.toml",
        ".env.example",
        "docker-compose.yml",
        "packages/core/store.py",
        "apps/api/main.py",
        "cli/client.py",
        "litellm/callback.py",
        "docker/postgres/Dockerfile",
        "shims/pi/index.ts",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    return root


def test_source_digest_tracks_content_paths_new_files_and_executable_mode(tmp_path):
    module = assurance()
    root = source_tree(tmp_path)
    original = module.source_digest(root)
    assert original == module.source_digest(root)
    path = root / "packages/core/store.py"
    path.write_text("different")
    assert module.source_digest(root) != original
    path.write_text("packages/core/store.py")
    assert module.source_digest(root) == original
    path.chmod(0o755)
    assert module.source_digest(root) != original
    path.chmod(0o644)
    path.rename(path.with_name("renamed.py"))
    assert module.source_digest(root) != original
    path.with_name("renamed.py").rename(path)
    added = root / "apps/api/another.py"
    added.write_text("new deployed source")
    assert module.source_digest(root) != original


def test_source_digest_excludes_docs_tests_and_generated_caches(tmp_path):
    module = assurance()
    root = source_tree(tmp_path)
    original = module.source_digest(root)
    for name in (
        "docs/guide.md",
        "apps/api/README.md",
        "packages/core/tests/test_store.py",
        "shims/pi/node_modules/pkg/index.js",
        "cli/__pycache__/module.pyc",
        "packages/core/.pytest_cache/data",
        "apps/api/.venv/package.py",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not deployed source")
    assert module.source_digest(root) == original
    (root / "uv.lock").unlink()
    with pytest.raises(module.AssuranceFailure):
        module.source_digest(root)


def test_source_digest_rejects_external_source_links(tmp_path):
    module = assurance()
    root = source_tree(tmp_path / "root")
    external = tmp_path / "outside.py"
    external.write_text("outside review")
    (root / "packages/core/link.py").symlink_to(external)
    with pytest.raises(module.AssuranceFailure):
        module.source_digest(root)


def deployment(root):
    runtime = {
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "mem_limit": 1073741824,
        "cpus": 1,
        "pids_limit": 128,
        "logging": {
            "driver": "json-file",
            "options": {"max-size": "10m", "max-file": "3"},
        },
        "networks": {"database": {}},
    }
    services = {
        name: copy.deepcopy(runtime)
        for name in ("postgres", "migrate", "api", "operator", "gateway")
    }
    services["postgres"].update(
        cap_add=["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"],
        environment={
            "POSTGRES_USER": "sediment",
            "POSTGRES_DB": "sediment",
            "POSTGRES_PASSWORD": "db-bootstrap",
        },
        command=["postgres", "-c", "hba_file=/etc/postgresql/sediment-pg_hba.conf"],
        volumes=[
            {
                "type": "bind",
                "source": str(root / "docker/postgres/pg_hba.conf"),
                "target": "/etc/postgresql/sediment-pg_hba.conf",
                "read_only": True,
            }
        ],
    )
    services["migrate"].update(
        command=["sediment", "db", "provision"],
        environment={
            "SEDIMENT_BOOTSTRAP_DATABASE_URL": "postgresql+psycopg://sediment:db-bootstrap@postgres:5432/sediment",
            "SEDIMENT_MIGRATOR_PASSWORD": "db-migrator",
            "SEDIMENT_RUNTIME_PASSWORD": "db-runtime",
            "SEDIMENT_OPERATOR_PASSWORD": "db-operator",
        },
    )
    services["api"]["environment"] = {
        "SEDIMENT_DATABASE_URL": "postgresql+psycopg://sediment_runtime:db-runtime@postgres:5432/sediment",
        "SEDIMENT_OPERATOR_TOKEN": "operator-token",
        "SEDIMENT_INGEST_TOKENS": '{"gateway":"gateway-token"}',
        "SEDIMENT_DEV_MODE": "false",
    }
    services["operator"]["environment"] = {
        "SEDIMENT_DATABASE_URL": "postgresql+psycopg://sediment_operator:db-operator@postgres:5432/sediment"
    }
    services["api"]["networks"]["edge"] = {}
    services["gateway"].update(
        networks={"edge": {}},
        environment={
            "SEDIMENT_API_BEARER_TOKEN": "gateway-token",
            "SEDIMENT_INGEST_URL": "http://api:8000",
            "SEDIMENT_GATEWAY_LOCAL_HTTP_ORIGIN": "http://api:8000",
        },
    )
    return {
        "services": services,
        "networks": {"database": {"internal": True}, "edge": {}},
        "volumes": {},
    }


def test_deployment_controls_accept_only_the_scoped_posture(tmp_path):
    predicates = assurance().deployment_predicates(deployment(tmp_path), tmp_path)
    assert predicates and all(predicates.values()), predicates


@pytest.mark.parametrize(
    "mutation,predicate",
    [
        (
            lambda c: c["services"]["postgres"].update(ports=[{"published": "5432"}]),
            "database_isolated",
        ),
        (
            lambda c: c["networks"]["database"].update(internal=False),
            "database_isolated",
        ),
        (
            lambda c: c["services"]["gateway"]["networks"].update(database={}),
            "database_isolated",
        ),
        (
            lambda c: c["services"]["api"]["environment"].update(
                SEDIMENT_DATABASE_URL=c["services"]["migrate"]["environment"][
                    "SEDIMENT_BOOTSTRAP_DATABASE_URL"
                ]
            ),
            "database_credentials_separated",
        ),
        (
            lambda c: c["services"]["gateway"]["environment"].update(
                LEAK="operator-token"
            ),
            "database_credentials_separated",
        ),
        (
            lambda c: c["services"]["api"]["environment"].update(EXTRA="db-bootstrap"),
            "database_credentials_separated",
        ),
        (
            lambda c: c["services"]["api"].update(
                volumes=[
                    {
                        "type": "bind",
                        "source": "/var/run/docker.sock",
                        "target": "/var/run/docker.sock",
                        "read_only": True,
                    }
                ]
            ),
            "no_host_data_mounts",
        ),
        (
            lambda c: c["services"]["postgres"]["volumes"][0].update(read_only=False),
            "no_host_data_mounts",
        ),
        (lambda c: c["services"]["api"].update(mem_limit=0), "resource_limits"),
        (lambda c: c["services"]["gateway"].update(pids_limit=-1), "resource_limits"),
        (
            lambda c: c["services"]["operator"]["logging"]["options"].update(
                {"max-size": "0"}
            ),
            "resource_limits",
        ),
        (
            lambda c: c["services"]["api"].update(cap_add=["SYS_ADMIN"]),
            "capabilities_confined",
        ),
        (
            lambda c: c["services"]["postgres"]["cap_add"].append("SYS_ADMIN"),
            "capabilities_confined",
        ),
        (
            lambda c: c["services"]["gateway"].update(security_opt=[]),
            "capabilities_confined",
        ),
        (
            lambda c: c["services"]["api"].update(privileged=True),
            "capabilities_confined",
        ),
        (
            lambda c: c["services"]["postgres"].update(read_only=False),
            "read_only_roots",
        ),
        (
            lambda c: c["services"]["postgres"].update(command=["postgres"]),
            "scram_configured",
        ),
        (lambda c: c["services"].pop("operator"), "database_credentials_separated"),
    ],
)
def test_deployment_mutations_revoke_the_predicate(tmp_path, mutation, predicate):
    config = deployment(tmp_path)
    mutation(config)
    assert assurance().deployment_predicates(config, tmp_path)[predicate] is False


def test_missing_or_malformed_deployment_never_passes(tmp_path):
    for value in ({}, {"services": []}, {"services": {"api": {}}}):
        predicates = assurance().deployment_predicates(value, tmp_path)
        assert not any(predicates.values())


def test_compose_renderer_ignores_ambient_secrets_and_env_files(tmp_path, monkeypatch):
    module = assurance()
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    monkeypatch.setenv("SEDIMENT_OPERATOR_TOKEN", "never-inherit-this-secret")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert "SEDIMENT_OPERATOR_TOKEN" not in kwargs["env"]
        env_file = Path(command[command.index("--env-file") + 1])
        assert "never-inherit-this-secret" not in env_file.read_text()
        return "{}"

    monkeypatch.setattr(module, "run", run)
    assert module.render_deployment(tmp_path) == {}
    assert "--no-env-resolution" in calls[0][0]


def test_native_probe_failures_do_not_become_empty_success(monkeypatch):
    module = assurance()
    monkeypatch.setattr(module, "run", lambda *a, **k: "not-json")
    with pytest.raises(module.AssuranceFailure):
        module.probe_image("sha256:" + "a" * 64, "api", "arm64")


def test_checked_in_compose_satisfies_every_deployment_predicate():
    # The synthetic fixture can't see a volume added to docker-compose.yml.
    module = assurance()
    if shutil.which("docker") is None:
        pytest.skip("docker compose renders the checked-in recipe")
    rendered = module.deployment_predicates(module.render_deployment())
    assert {name for name, holds in rendered.items() if not holds} == set()


def test_real_images_and_postgresql_scram(tmp_path):
    module = assurance()
    image = os.environ.get("SEDIMENT_TEST_POSTGRES_IMAGE")
    if not image:
        pytest.skip(
            "set SEDIMENT_TEST_POSTGRES_IMAGE for disposable native image checks"
        )
    inspected = json.loads(module.run(["docker", "image", "inspect", image]))[0]
    observed = module.probe_image(
        inspected["Id"], "postgres", inspected["Architecture"]
    )
    assert observed["predicates"]["perl_64bit"] is True
    assert observed["predicates"]["libxml_python_bindings_absent"] is True
    postgres = module.probe_postgres(inspected["Id"], ROOT)
    assert postgres["scram_only"] is True
    assert postgres["postgres_uid"] == 999


def test_deployment_accepts_a_canonical_checkout_alias(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    assert all(assurance().deployment_predicates(deployment(alias), alias).values())


@pytest.mark.parametrize(
    "mutation,predicate",
    [
        (lambda c: c["services"]["api"].update(user="0"), "capabilities_confined"),
        (
            lambda c: c["services"]["api"].update(volumes_from=["host-container"]),
            "no_host_data_mounts",
        ),
        (
            lambda c: c["services"]["gateway"].update(
                command=["echo", "operator-token"]
            ),
            "database_credentials_separated",
        ),
        (
            lambda c: c["services"]["api"]["environment"].update(
                SEDIMENT_API_BEARER_TOKEN="operator-token"
            ),
            "database_credentials_separated",
        ),
    ],
)
def test_alternate_container_configuration_cannot_bypass_controls(
    tmp_path, mutation, predicate
):
    config = deployment(tmp_path)
    mutation(config)
    assert assurance().deployment_predicates(config, tmp_path)[predicate] is False


def test_api_cannot_mount_postgresql_data_even_through_a_named_volume(tmp_path):
    config = deployment(tmp_path)
    config["volumes"] = {"sediment-postgres": {}}
    config["services"]["api"]["volumes"] = [
        {"type": "volume", "source": "sediment-postgres", "target": "/data/postgres"}
    ]
    assert (
        assurance().deployment_predicates(config, tmp_path)["no_host_data_mounts"]
        is False
    )


def test_missing_native_predicate_is_an_observation_failure(monkeypatch):
    module = assurance()
    identifier = "sha256:" + "a" * 64
    monkeypatch.setattr(
        module,
        "run",
        lambda *a, **k: json.dumps([{"Id": identifier, "Architecture": "arm64"}]),
    )
    monkeypatch.setattr(
        module,
        "_container_probe",
        lambda *a, **k: (
            {"uid": 1000, "git_default_config": True}
            if not k.get("root_user")
            else {"no_suid_sgid": True}
        ),
    )
    with pytest.raises(module.AssuranceFailure, match="incomplete"):
        module.probe_image(identifier, "api", "arm64")


def test_conflicting_privilege_options_do_not_count_as_protection(tmp_path):
    config = deployment(tmp_path)
    config["services"]["gateway"]["security_opt"] = [
        "no-new-privileges:true",
        "no-new-privileges:false",
    ]
    assert (
        assurance().deployment_predicates(config, tmp_path)["capabilities_confined"]
        is False
    )


def test_gzip_write_api_reachability_accepts_the_reviewed_static_embeds():
    module = assurance()
    reviewed = next(iter(module.REVIEWED_STATIC_GZ_WRITE_EMBEDS))
    classification = module.gzip_write_api_reachability(
        {
            "usr/lib/libz.so.1.3.2": (set(), {"gzprintf", "gzwrite", "gz_write"}),
            "usr/bin/apk": ({"uncompress"}, set()),
            "usr/lib/python3.13/lib-dynload/zlib.so": (
                {"deflate", "inflate", "crc32"},
                set(),
            ),
            reviewed: (set(), {"gzprintf", "gzvprintf", "gzwrite"}),
        }
    )
    assert classification == {"dynamic_importers": [], "unexpected_static_embeds": []}


def test_gzip_write_api_reachability_flags_a_new_dynamic_importer():
    module = assurance()
    classification = module.gzip_write_api_reachability(
        {"app/plugin.so": ({"gzprintf"}, set())}
    )
    assert classification["dynamic_importers"] == ["app/plugin.so"]
    assert classification["unexpected_static_embeds"] == []


def test_gzip_write_api_reachability_flags_an_unreviewed_static_embed():
    module = assurance()
    classification = module.gzip_write_api_reachability(
        {"app/.venv/lib/python3.13/site-packages/unexpected.so": (set(), {"gzvprintf"})}
    )
    assert classification["dynamic_importers"] == []
    assert classification["unexpected_static_embeds"] == [
        "app/.venv/lib/python3.13/site-packages/unexpected.so"
    ]


def test_gzip_write_api_reachability_ignores_unrelated_symbols():
    module = assurance()
    classification = module.gzip_write_api_reachability(
        {
            "app/harmless.so": ({"printf", "malloc"}, {"gzip_helper_unrelated"}),
        }
    )
    assert classification == {"dynamic_importers": [], "unexpected_static_embeds": []}


def test_elf_symbol_names_parses_readelf_dyn_syms_output():
    module = assurance()
    output = (
        "   1: 0000000000000000     0 FUNC    GLOBAL DEFAULT  UND gzprintf@@ZLIB_1.2.7.1\n"
        "   2: 0000000000000000     0 FUNC    GLOBAL DEFAULT   11 deflate\n"
        "   3: 0000000000000000     0 OBJECT  GLOBAL DEFAULT  UND some_data\n"
    )
    assert module._elf_symbol_names(output, undefined_only=True) == {"gzprintf"}
    assert module._elf_symbol_names(output, undefined_only=False) == {"deflate"}


def test_gzip_write_api_unreachable_rejects_a_malformed_image_id():
    module = assurance()
    with pytest.raises(module.AssuranceFailure):
        module.gzip_write_api_unreachable("not-a-digest")


def test_real_gateway_image_gzip_write_api(tmp_path):
    module = assurance()
    image = os.environ.get("SEDIMENT_TEST_GATEWAY_IMAGE")
    if not image:
        pytest.skip(
            "set SEDIMENT_TEST_GATEWAY_IMAGE for a disposable native ELF-symbol scan"
        )
    inspected = json.loads(module.run(["docker", "image", "inspect", image]))[0]
    assert module.gzip_write_api_unreachable(inspected["Id"]) is True


HANDLER = "litellm/proxy/management_endpoints/sso/saml_sso.py"


def gateway_callers(root):
    root = root.resolve()
    for name in (HANDLER, "onelogin/saml2/utils.py", "onelogin/saml2/nested/extra.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    (root / "onelogin/saml2/ignored.txt").write_text("not Python source")
    return root


def execute_gateway_probe(module, root, monkeypatch):
    monkeypatch.setattr(sysconfig, "get_paths", lambda: {"purelib": str(root)})
    exec(module.GATEWAY_CALLER_PROBE, {})


def test_gateway_probe_hashes_every_actual_python_source(tmp_path, monkeypatch, capsys):
    module = assurance()
    root = gateway_callers(tmp_path)
    execute_gateway_probe(module, root, monkeypatch)
    files = json.loads(capsys.readouterr().out)
    assert files == {
        name: hashlib.sha256(name.encode()).hexdigest()
        for name in (
            HANDLER,
            "onelogin/saml2/utils.py",
            "onelogin/saml2/nested/extra.py",
        )
    }
    added = root / "onelogin/saml2/previously_unlisted.py"
    added.write_bytes(b"extra caller")
    (root / "onelogin/saml2/utils.py").write_bytes(b"modified caller")
    execute_gateway_probe(module, root, monkeypatch)
    changed = json.loads(capsys.readouterr().out)
    assert (
        changed[added.relative_to(root).as_posix()]
        == hashlib.sha256(b"extra caller").hexdigest()
    )
    assert changed["onelogin/saml2/utils.py"] != files["onelogin/saml2/utils.py"]
    assert set(changed) == set(files) | {added.relative_to(root).as_posix()}


@pytest.mark.parametrize("missing", [HANDLER, "onelogin/saml2", "all_saml_python"])
def test_gateway_probe_refuses_absent_sources(tmp_path, monkeypatch, capsys, missing):
    root = gateway_callers(tmp_path)
    if missing == "all_saml_python":
        for path in (root / "onelogin/saml2").rglob("*.py"):
            path.unlink()
    elif (root / missing).is_dir():
        shutil.rmtree(root / missing)
    else:
        (root / missing).unlink()
    with pytest.raises(RuntimeError, match="absent"):
        execute_gateway_probe(assurance(), root, monkeypatch)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "linked", [HANDLER, "onelogin", "onelogin/saml2/nested", "onelogin/saml2/utils.py"]
)
def test_gateway_probe_refuses_symlinked_sources(tmp_path, monkeypatch, capsys, linked):
    root = gateway_callers(tmp_path / "site-packages")
    path = root / linked
    target = tmp_path / "outside"
    path.rename(target)
    path.symlink_to(target, target_is_directory=target.is_dir())
    with pytest.raises(RuntimeError, match="symlink"):
        execute_gateway_probe(assurance(), root, monkeypatch)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("fails", [False, True])
def test_gateway_probe_uses_confined_exact_image_and_always_cleans_up(
    monkeypatch, fails
):
    module = assurance()
    image = "sha256:" + "a" * 64
    files = {HANDLER: "b" * 64, "onelogin/saml2/utils.py": "c" * 64}
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "start":
            if fails:
                raise module.AssuranceFailure("caller path is absent")
            return json.dumps(files)
        return ""

    monkeypatch.setattr(module, "run", run)
    if fails:
        with pytest.raises(module.AssuranceFailure, match="absent"):
            module.probe_gateway_callers(image)
    else:
        assert module.probe_gateway_callers(image) == files
    create, start, remove = calls
    for flag, value in (
        ("--network", "none"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--entrypoint", "/bin/sh"),
    ):
        assert create[create.index(flag) + 1] == value
    assert "--read-only" in create
    assert "--cap-add" not in create
    assert create[create.index("/bin/sh") + 1] == image
    assert module.GATEWAY_CALLER_PROBE in create[-1]
    name = create[create.index("--name") + 1]
    assert start == ["docker", "start", "--attach", name]
    assert remove == ["docker", "rm", "--force", "--volumes", name]


@pytest.mark.parametrize(
    "files",
    [
        {},
        {HANDLER: "b" * 64},
        {"onelogin/saml2/utils.py": "c" * 64},
        {HANDLER: "invalid", "onelogin/saml2/utils.py": "c" * 64},
        {HANDLER: "b" * 64, "onelogin/saml2/../escape.py": "c" * 64},
        {HANDLER: "b" * 64, "onelogin/saml2/utils.txt": "c" * 64},
        {HANDLER: "b" * 64, "onelogin/saml2/utils.py": "c" * 64, "other.py": "d" * 64},
    ],
)
def test_gateway_probe_rejects_incomplete_or_malformed_evidence(monkeypatch, files):
    module = assurance()
    monkeypatch.setattr(module, "_container_probe", lambda *a, **k: files)
    with pytest.raises(module.AssuranceFailure, match="incomplete"):
        module.probe_gateway_callers("sha256:" + "a" * 64)


def test_gateway_probe_rejects_a_mutable_image_reference(monkeypatch):
    module = assurance()
    monkeypatch.setattr(
        module, "_container_probe", lambda *a, **k: pytest.fail("ran image")
    )
    with pytest.raises(module.AssuranceFailure, match="exact"):
        module.probe_gateway_callers("gateway:latest")


@pytest.mark.parametrize("artifact", ["gateway", "api"])
@pytest.mark.parametrize("patched", [False, True])
@pytest.mark.parametrize("bytecode_present", [False, True])
def test_caller_evidence_is_retained_only_for_gateway(
    tmp_path, monkeypatch, artifact, patched, bytecode_present
):
    module = assurance()
    root = source_tree(tmp_path / "source")
    image = "sha256:" + "a" * 64
    files = {HANDLER: "b" * 64, "onelogin/saml2/utils.py": "c" * 64}
    monkeypatch.setattr(module, "probe_image", lambda *args: {"predicates": {}})
    monkeypatch.setattr(module, "render_deployment", deployment)

    def probe(image_id):
        assert artifact == "gateway"
        assert image_id == image
        return files

    monkeypatch.setattr(module, "probe_gateway_callers", probe)
    tarfile = {
        "path": "/usr/lib/python3.13/tarfile.py",
        "sha256": (
            "9600de643ae7efed27009ee6c86aee60cebe335c0e732797c06db74dc719cefd"
            if patched
            else "9fedddf7e814c226cb7e1ac0aa603092eda40047367ec00ad740a81484a17d01"
        ),
        "bytecode_present": bytecode_present,
    }
    monkeypatch.setattr(module, "_container_probe", lambda *args: tarfile)
    out = tmp_path / "evidence"
    result = module.collect_assurance(image, artifact, "amd64", out, root=root)
    retained = json.loads((out / f"{artifact}-amd64.assurance.json").read_text())
    assert retained == result
    assert result["image_id"] == image
    assert "gateway_caller_files" not in result["predicates"]
    if artifact == "gateway":
        assert result["gateway_caller_files"] == files
        assert result["gateway_tarfile"] == tarfile
        assert result["predicates"]["tarfile_hardlink_fix"] == (
            patched and not bytecode_present
        )
    else:
        assert "gateway_caller_files" not in result
        assert "gateway_tarfile" not in result
        assert "tarfile_hardlink_fix" not in result["predicates"]


def test_real_gateway_caller_evidence():
    module = assurance()
    image = os.environ.get("SEDIMENT_TEST_GATEWAY_IMAGE")
    if not image:
        pytest.skip("set SEDIMENT_TEST_GATEWAY_IMAGE for exact caller collection")
    inspected = json.loads(module.run(["docker", "image", "inspect", image]))[0]
    files = module.probe_gateway_callers(inspected["Id"])
    assert HANDLER in files
    assert any(name.startswith("onelogin/saml2/") for name in files)
