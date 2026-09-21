# SPDX-License-Identifier: AGPL-3.0-or-later
"""Experimental taskset-level ``environment.yaml`` for Sediment exports.

The Sediment target may write this optional sidecar beside ``tasks.jsonl``.
SWE-bench and NeMo Gym targets never write it. The manifest remains
experimental because its OpenEnv shape follows an unmerged RFC; field-name
similarity does not establish runtime compatibility.

**Pinned against.** Re-verified live at implementation time (2026-07-19)
against ``huggingface/OpenEnv`` PR #795 ("RFC 006 — HF RL Environment
Datasets"), still **In Review**, head commit ``2ef60ddfe5edc0b0e9a80cf8d86e126fda16708b``
of ``rfcs/006-hf-rl-environment-datasets.md`` (file created 2026-05-21). The
shape matches what `docs/exports/rlvr-export.md` had already pinned
(evidenced against ``main`` @ ``20f8b8bb``, read 2026-07-16): a
dataset-root ``environment.yaml`` with ``spec_version`` + a non-empty
``environments`` list, each entry's ``frameworks.openenv`` carrying **exactly
one** of ``space_id`` / ``image`` / ``package``, plus optional ``resources`` /
``secrets``. RFC 006 remains unmerged and contested (the "006" slot has
drifted between briefings) — a future re-pin may need to revisit this shape.

**One manifest per taskset, not per split.** RFC 006's own model is one
``environment.yaml`` at the dataset root describing a ``splits: [train, ...]``
list *inside* one environment entry — not one manifest per split file. Sediment
follows that: a single ``environment.yaml`` is written beside ``tasks.jsonl``
(or ``tasks.train.jsonl`` / ``tasks.eval.jsonl`` when the split is enabled),
covering whichever splits actually landed rows.

**No fabrication.** Two distinct runtime concepts are kept apart
rather than conflated:

* ``runtime_reference`` — a Sediment-specific, **informational** field naming
  what CI check backed the emitted tasks (the mirror's ``workflow_name`` /
  ``workflow_path``) when every task row agrees on a single one. This is a
  reference into the verifier-result repo's CI definition, not a runnable image — RFC
  002's own reasoning is that a workflow "shells out, provisions services,
  matrixes over versions," which is exactly why it is never written into
  ``frameworks.openenv``. Divergent or absent workflow paths across the
  taskset leave this field **absent**, never guessed at.
* ``frameworks.openenv`` — the RFC-actionable slot — is populated **only**
  from an operator-supplied ``image`` or ``package``
  (``SEDIMENT_OPENENV_IMAGE`` / ``SEDIMENT_OPENENV_PACKAGE``, following
  ``VerifierCommandsSettings``'s ``SEDIMENT_``-prefixed pattern). Nothing is
  inferred into it. When neither is configured, ``frameworks`` is omitted
  entirely — RFC 006 lists it as a required per-environment field, but
  emitting a fabricated ``frameworks.openenv`` block to satisfy that
  requirement would be exactly the kind of guess this codebase refuses to
  make elsewhere (``verification_command``, ``base_commit``, reference-patch
  resolution).
  The manifest is honest about being spec-adjacent rather than
  spec-conformant when no real runtime is known.
* ``frameworks.nemo_gym`` — same posture, second framework key. RFC
  006's ``frameworks`` mapping is keyed by framework name and ``openenv`` is
  the only RFC-defined key; ``nemo_gym`` is Sediment's extension under that
  extensible mapping, naming the NeMo Gym resources server (the environment
  implementation ``gym env start --resources-server <name>`` launches —
  NVIDIA-NeMo/Gym, pinned in ``docs/exports/rlvr-export.md``) that serves these
  tasks. Populated **only** from ``SEDIMENT_NEMO_GYM_RESOURCES_SERVER``
  (plus optional ``SEDIMENT_NEMO_GYM_CONFIG``, the server's config YAML
  path); never inferred, omitted when unconfigured, and both framework keys
  may coexist — they describe alternative runtimes for the same taskset.

**Reward convention.** The taskset-level ``reward`` declaration records the
documented verifier-result mapping: resolved CI ``passed`` maps to ``1.0`` and
``failed`` maps to ``0.0``. Sediment task rows retain the exact verifier
results and CI resolution instead of replacing them with this numeric reward.

**No execution, ever.** This module reads task rows and settings; it never
runs ``verification_command``, never touches a container runtime, and never resolves
an image. The consumer owns environment provisioning, verifier execution,
and isolation.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

from .jsonl import ExportRow

logger = logging.getLogger("sediment.export.environment_manifest")

# RFC 006's declared spec_version token for this manifest shape (pinned above).
_SPEC_VERSION = "hf-rl-env-0.1"

# Stable id for the single environment entry Sediment emits per taskset. There
# is exactly one entry per manifest today (one taskset per export run); a
# fixed id keeps repeated exports of the same taskset diffable.
_ENVIRONMENT_ID = "sediment-tasks"

_MANIFEST_FILENAME = "environment.yaml"


class OpenEnvRuntimeSettings(BaseSettings):
    """Operator-supplied OpenEnv runtime reference for the taskset manifest.

    Follows ``VerifierCommandsSettings``'s pattern exactly: an unset value means
    **unconfigured**, and the manifest's ``frameworks.openenv`` block is
    omitted rather than fabricated. At most one of the two should be set —
    per RFC 006, ``frameworks.openenv`` takes exactly one of
    ``space_id`` / ``image`` / ``package``; Sediment exposes the two an
    operator can hand-configure without a Hub Space. If both are set,
    ``openenv_image`` wins (arbitrary but deterministic) and the conflict is
    logged, mirroring how a malformed verifier-commands entry degrades loudly
    rather than silently.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="SEDIMENT_", extra="ignore"
    )

    openenv_image: str | None = None
    openenv_package: str | None = None

    def resolve(self) -> dict[str, str] | None:
        """The ``frameworks.openenv`` runtime entry (``{"image": ...}`` or
        ``{"package": ...}``), or ``None`` when unconfigured. Values are
        stripped and a blank counts as unset — the ``VerifierCommands.load``
        posture: a whitespace-only env var must not become a garbage
        "runnable" target in the RFC-actionable slot."""
        image = (self.openenv_image or "").strip()
        package = (self.openenv_package or "").strip()
        if image and package:
            logger.warning(
                "openenv_runtime_ambiguous_config",
                extra={
                    "openenv_image": image,
                    "openenv_package": package,
                    "resolved": "openenv_image",
                },
            )
        if image:
            return {"image": image}
        if package:
            return {"package": package}
        return None


class NemoGymRuntimeSettings(BaseSettings):
    """Operator-supplied NeMo Gym runtime reference for the taskset manifest —
    the ``frameworks.nemo_gym`` analogue of
    :class:`OpenEnvRuntimeSettings`, same posture: unset means
    **unconfigured** and the block is omitted rather than fabricated.

    ``nemo_gym_resources_server`` is the primary reference — the resources
    server name NeMo Gym's CLI takes (``gym env start --resources-server
    <name>``): the FastAPI service that holds per-task state, exposes the
    environment's tools, and implements ``verify()``. ``nemo_gym_config`` is
    an optional refinement naming the server's config YAML
    (``resources_servers/<name>/configs/<config>.yaml``); it only rides along
    with a configured server name — a config path alone is not a launchable
    reference, so it is logged and treated as unconfigured rather than
    emitted as a garbage entry in the actionable slot (the
    ``VerifierCommands.load`` posture, same as blank-string handling).
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="SEDIMENT_", extra="ignore"
    )

    nemo_gym_resources_server: str | None = None
    nemo_gym_config: str | None = None

    def resolve(self) -> dict[str, str] | None:
        """The ``frameworks.nemo_gym`` entry
        (``{"resources_server": ...}``, plus ``"config"`` when supplied), or
        ``None`` when unconfigured. Values are stripped; blank counts as
        unset."""
        server = (self.nemo_gym_resources_server or "").strip()
        config = (self.nemo_gym_config or "").strip()
        if not server:
            if config:
                logger.warning(
                    "nemo_gym_config_without_resources_server",
                    extra={"nemo_gym_config": config},
                )
            return None
        entry = {"resources_server": server}
        if config:
            entry["config"] = config
        return entry


def _reward_block() -> dict:
    """The taskset-wide verifier-result-to-reward convention."""
    return {"kind": "ci", "pass_value": 1.0, "fail_value": 0.0}


def _workflow_reference(rows: list[ExportRow]) -> dict | None:
    """The CI workflow that backs every emitted task row, when there is
    exactly one. A taskset-level manifest can only honestly name *one*
    runtime; a taskset spanning multiple repos or workflows has no single
    truthful reference, so this returns ``None`` rather than picking one
    arbitrarily (or worse, the most common) — the same "absent, never
    guessed" discipline as ``verification_command``. A row with *no* workflow
    identity at all (``workflow_path`` is Optional on ``CIOutcome``) is a
    disagreement too, not a bystander: "every emitted task row agrees"
    must mean every row, or a mixed taskset would be silently blanketed by
    whichever repo happens to carry a workflow path."""
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for row in rows:
        verifier_results = row.body.get("verifier_results")
        if not isinstance(verifier_results, list) or not verifier_results:
            return None
        for verifier_result in verifier_results:
            if not isinstance(verifier_result, dict):
                return None
            workflow_path = verifier_result.get("workflow_path")
            if not workflow_path:
                return None  # a workflow-less row: no single truthful reference
            seen.add(
                (
                    row.body.get("repo"),
                    verifier_result.get("workflow_name"),
                    workflow_path,
                )
            )
    if len(seen) != 1:
        return None
    repo, workflow_name, workflow_path = seen.pop()
    return {
        "kind": "ci_workflow",
        "repo": repo,
        "workflow_name": workflow_name,
        "workflow_path": workflow_path,
    }


def build_manifest(
    rows: list[ExportRow],
    runtime_settings: OpenEnvRuntimeSettings,
    *,
    split_enabled: bool,
    nemo_gym_settings: NemoGymRuntimeSettings | None = None,
) -> dict | None:
    """Build the ``environment.yaml`` document for a set of ``tasks.jsonl``
    rows, or ``None`` when there is nothing to describe.

    ``None`` on an empty ``rows`` list — matching ``jsonl.py``'s empty-input
    convention: a taskset with zero task rows gets no manifest, the same way
    it gets no ``tasks.jsonl`` (nothing written, never an empty stub).
    """
    if not rows:
        return None

    environment: dict = {"id": _ENVIRONMENT_ID, "reward": _reward_block()}

    if split_enabled:
        # Fixed train-then-eval order (the RFC's own example order), filtered
        # by presence; ``rows`` is non-empty here so at least one side is.
        present = {row.split for row in rows}
        environment["splits"] = [s for s in ("train", "eval") if s in present]

    workflow_ref = _workflow_reference(rows)
    if workflow_ref is not None:
        environment["runtime_reference"] = workflow_ref

    # Fixed openenv-then-nemo_gym key order keeps repeated exports diffable;
    # either, both, or neither may be configured (they describe alternative
    # runtimes for the same taskset, not alternatives to each other).
    frameworks: dict[str, dict[str, str]] = {}
    openenv_runtime = runtime_settings.resolve()
    if openenv_runtime is not None:
        frameworks["openenv"] = openenv_runtime
    if nemo_gym_settings is not None:
        nemo_gym_runtime = nemo_gym_settings.resolve()
        if nemo_gym_runtime is not None:
            frameworks["nemo_gym"] = nemo_gym_runtime
    if frameworks:
        environment["frameworks"] = frameworks

    return {
        "spec_version": _SPEC_VERSION,
        "experimental": True,
        "environments": [environment],
    }


def write_manifest(manifest: dict | None, out_dir: str | Path) -> Path | None:
    """Write ``manifest`` to ``<out_dir>/environment.yaml`` atomically, or
    skip under the same no-truncate guard ``jsonl.py`` uses: ``manifest is
    None`` (the empty-taskset case) leaves the filesystem exactly as found —
    nothing written, an existing manifest from a prior run untouched, a
    warning logged. Returns the written path, or ``None`` when skipped.
    """
    out = Path(out_dir)
    path = out / _MANIFEST_FILENAME

    if manifest is None:
        logger.warning(
            "environment_manifest_empty_input_no_write", extra={"path": str(path)}
        )
        return None

    out.mkdir(parents=True, exist_ok=True)
    # Atomic replace, same discipline as jsonl.py's writer: temp file in
    # the destination dir (so os.replace is a rename), fsync, then swap.
    fd, tmp_name = tempfile.mkstemp(dir=out, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(manifest, handle, sort_keys=False, default_flow_style=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    logger.info("environment_manifest_written", extra={"path": str(path)})
    return path
