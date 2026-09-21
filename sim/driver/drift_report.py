# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wire-drift report: what live agents send now vs. what the fixtures froze.

This is the one thing the scenarios structurally cannot do. Its fixtures are
frozen captures, so if today's agent builds change their telemetry the whole
sim stays green while reality breaks. The driver runs *current* agent builds,
so their payloads are available to compare.

Method: both sides are reduced to a set of **key paths** (``a.b[].c``) and
diffed per source. OTLP attribute lists are flattened to real key paths
first (see ``_flatten_otlp_attrs``), and facts the dry-run stub posted
are excluded (``STUB_SESSION_PREFIX``) — the stub replays the fixtures,
so counting its facts as observed would blind ``missing`` forever after
one dry run.

- Baseline comes from running the shipped translators over the frozen
  fixtures — ``parse_otlp_decisions`` and ``LiteLLMAdapter.normalize`` — and
  reading the ``raw`` each produces. Not a hand-written schema: reusing the
  real translators is what stops this report from drifting away from the code
  it is meant to guard.
- Observed comes from the ``raw`` on facts the driver actually captured, read
  back through ``FactStore``.

Two finding kinds, and the asymmetry matters:

- ``missing`` — a path the fixtures have and live traffic no longer sends.
  This is the dangerous one. Translators read these paths; when one vanishes
  the translator degrades fail-soft and the facts get quietly thinner.
- ``added`` — a path live traffic sends that the fixtures never captured.
  Usually benign, occasionally the first sight of a field worth translating.

Usage:
    python sim/driver/drift_report.py --database-url "$SEDIMENT_DATABASE_URL"
    python sim/driver/drift_report.py --database-url ... --json

Exits 1 when there are ``missing`` findings, 0 otherwise: a new upstream field
should not wake anyone, a disappeared one should.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "packages/capture/tests/fixtures"
ORG = "simcorp"

# Fixture → the source label its facts carry. The gateway fixture is the
# LiteLLM standard logging object; the OTLP ones are per agent and are
# discovered from their directory name, which is already the source.
GATEWAY_FIXTURE = "litellm_standard_logging_object.json"

# Facts the dry-run stub posts carry this session prefix; the report must
# skip them or one dry run permanently seeds every baseline path and
# ``missing`` can never fire again. Kept in sync with
# ``stub_agent.SESSION_PREFIX`` — test_driver.py pins the two together.
STUB_SESSION_PREFIX = "sess-driver-"


def key_paths(value: object, prefix: str = "") -> set[str]:
    """Every key path in a JSON-ish value.

    Lists collapse to a single ``[]`` segment and their elements are unioned:
    a payload with three tool calls and one with a single tool call describe
    the same shape, and treating them as different would make every report
    noise.
    """
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            here = f"{prefix}.{key}" if prefix else str(key)
            paths.add(here)
            paths |= key_paths(child, here)
    elif isinstance(value, list):
        here = f"{prefix}[]"
        for child in value:
            paths |= key_paths(child, here)
    return paths


def _flatten_otlp_attrs(value: object) -> object:
    """Rewrite OTLP attribute lists (``[{"key": k, "value": v}, ...]``) as
    ``{k: v}`` dicts, recursively.

    The OTLP envelope stores attribute *names* as list-element values, so
    ``key_paths`` on the raw envelope reduces every claude-code fact to the
    same ~22 structural paths and no attribute rename can ever surface as
    drift. Flattening restores the semantic paths
    (``decision.attributes.tool_name`` ...) on **both** sides of the diff —
    the baseline runs through the same rewrite, so want and got compare in
    the same space."""
    if isinstance(value, list):
        if value and all(
            isinstance(el, dict) and "key" in el and "value" in el for el in value
        ):
            return {str(el["key"]): _flatten_otlp_attrs(el["value"]) for el in value}
        return [_flatten_otlp_attrs(el) for el in value]
    if isinstance(value, dict):
        return {k: _flatten_otlp_attrs(v) for k, v in value.items()}
    return value


def _shape(raw: dict) -> set[str]:
    return key_paths(_flatten_otlp_attrs(raw))


def baseline_shapes() -> dict[str, set[str]]:
    """Key paths per source, from the frozen fixtures through the real
    translators."""
    from sediment_capture import ADAPTERS, parse_otlp_decisions
    from sediment_core import GatewayProvider

    shapes: defaultdict[str, set[str]] = defaultdict(set)

    payload = json.loads((FIXTURES / GATEWAY_FIXTURE).read_text())
    # ADAPTERS holds instances, not classes, and is keyed by the provider enum.
    completion = ADAPTERS[GatewayProvider.LITELLM].normalize(
        payload, org_id=ORG, session_id="sess-baseline", user_id="baseline"
    )
    shapes[str(GatewayProvider.LITELLM)] = _shape(completion.raw)

    for path in sorted(FIXTURES.glob("otlp/*/*.json")):
        decisions = parse_otlp_decisions(json.loads(path.read_text()), org_id=ORG)
        for decision in decisions:
            shapes[str(decision.agent_harness)] |= _shape(decision.raw)
    return dict(shapes)


def observed_shapes(database_url: str, org: str = ORG) -> dict[str, set[str]]:
    """Key paths per source, from facts the driver captured.

    Reads through ``FactStore`` (ADR 0001), never a direct SELECT.
    """
    from sediment_core import FactStore
    from sediment_core.postgres_engine import create_postgres_engine

    engine = create_postgres_engine(database_url)
    try:
        store = FactStore(engine)
        shapes: defaultdict[str, set[str]] = defaultdict(set)
        facts = chain(
            (
                (str(call.gateway_provider), call)
                for call in store.read_inference_calls(org)
            ),
            ((str(d.agent_harness), d) for d in store.read_decisions(org)),
        )
        for source, fact in facts:
            if fact.session_id.startswith(STUB_SESSION_PREFIX):
                continue  # dry-run stub traffic: fixtures replayed, not drift
            shapes[source] |= _shape(fact.raw)
        return dict(shapes)
    finally:
        engine.dispose()


@dataclass
class Finding:
    source: str
    kind: str  # "missing" | "added" | "unobserved"
    paths: list[str] = field(default_factory=list)

    def line(self) -> str:
        if self.kind == "unobserved":
            return (
                f"unobserved {self.source}: the fixtures describe this source "
                "but the run captured none of it — no drift claim possible"
            )
        verb = (
            "gone from live traffic"
            if self.kind == "missing"
            else "new in live traffic"
        )
        shown = ", ".join(self.paths[:8])
        more = f" (+{len(self.paths) - 8} more)" if len(self.paths) > 8 else ""
        return f"{self.kind} {self.source}: {len(self.paths)} path(s) {verb} — {shown}{more}"


def compare(
    baseline: dict[str, set[str]], observed: dict[str, set[str]]
) -> list[Finding]:
    """Diff the two shape maps. Sources present in the baseline but absent
    from the run are reported as ``unobserved`` rather than as wholesale
    drift: no traffic is not the same as changed traffic, and calling it
    drift would fire on every run that simply did not use that agent."""
    findings: list[Finding] = []
    for source in sorted(baseline):
        want = baseline[source]
        if source not in observed:
            findings.append(Finding(source, "unobserved"))
            continue
        got = observed[source]
        if missing := sorted(want - got):
            findings.append(Finding(source, "missing", missing))
        if added := sorted(got - want):
            findings.append(Finding(source, "added", added))
    for source in sorted(set(observed) - set(baseline)):
        findings.append(Finding(source, "added", sorted(observed[source])))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="driver wire-drift report")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("SEDIMENT_DATABASE_URL"),
        help="PostgreSQL URL (default: SEDIMENT_DATABASE_URL)",
    )
    parser.add_argument("--org", default=ORG)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if not args.database_url:
        parser.error("set SEDIMENT_DATABASE_URL or pass --database-url")
    findings = compare(baseline_shapes(), observed_shapes(args.database_url, args.org))
    if args.json:
        print(
            json.dumps(
                [
                    {"source": f.source, "kind": f.kind, "paths": f.paths}
                    for f in findings
                ],
                indent=2,
            )
        )
    else:
        for finding in findings:
            print(finding.line())
        if not findings:
            print("no drift: every fixture path is still on the wire")

    missing = [f for f in findings if f.kind == "missing"]
    if missing:
        sys.stdout.flush()
        print(
            f"\n{len(missing)} source(s) lost wire paths the translators read. "
            "Re-capture fixtures only after deciding whether the translator "
            "should follow (packages/capture/tests/fixtures are frozen — "
            "docs/agents/capture-translators.md).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
