# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tier A scenario harness: Group 1 attribution ground truth.

Runs synthetic scenarios against the real pipeline in-process: the
generated simcorp-billing repo (``gen_repo.py``) plus scripted capture
traffic through a FastAPI ``TestClient`` — gateway inference calls via
``/ingest/gateway``, OTLP decisions and edit observations via ``/v1/logs``,
HMAC-signed pushes via ``/ingest/github/push`` — into PostgreSQL under
``SEDIMENT_ORG_ID=simcorp``. Facts enter ONLY through the ingest doors and
are read back ONLY through ``FactStore`` reads and the ``sediment_derive``
public API (ADR 0001: no direct database writes, no derived-state persistence).

Every wire shape here is parameterized from the wire-verified fixtures in
``packages/capture/tests/fixtures`` — session ids, content, and timestamps
substituted per scenario, structure never invented. Notes stamps are written
in the stamper's exact format (``scripts/sediment_attribution.py``:
``{"v": 1, "sessions": [{tool, session_id, stamped_at}, …]}`` on
``refs/notes/sediment``), and the sim clone carries the installer's
``notes.rewriteRef`` config, so rebase note-copying behaves as it does in an
installed clone.

Each scenario appends rows to the ground-truth manifest (JSONL, one row per
completion). Two labeled axes per row:

- ``expected_attribution`` — what the pipeline is expected to produce at the
  DEFAULT policy ("git_notes" / "jaccard" / null). The per-scenario asserts pin
  this; ``precision_report.py`` scores the default-threshold derivation
  against it.
- ``true_link`` — whether the completion GENUINELY belongs to
  (expected_commit, expected_file), independent of threshold. This is the
  ground truth the sweep scores against: a hand-edited completion the
  0.7 threshold misses is still a true link at lower thresholds, and the
  retyped-after-reject bait is a false positive at every threshold.

Timing: ``captured_at`` is stamped at ingest (wall clock), exactly as in
production — the run takes seconds, so every completion sits inside every
push's attribution window and the derivation's outcome depends only on
content, notes, and fact identity, never on the wall-clock values
themselves. Git history and OTLP ``timeUnixNano`` values are fixed/injected.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import random
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sediment_capture import sign_payload
from sediment_core import CIResult, FactStore, FactTable, Push
from sediment_derive import (
    EVAL,
    AttributionSource,
    Attribution,
    AttributionPolicy,
    MirrorManager,
    RepositoryContext,
    RepositoryIdentity,
    attach_edit_retention,
    derive_attributions,
    derive_recovery_result,
    four_gram_containment,
    inference_fact_id,
    inference_input_messages,
    is_eval,
    join_decisions_by_call_id,
    render_scoring_text,
    read_repository_context,
    split_of,
)
from sediment_export import (
    AttributedCompletionPolicy,
    AttributedCompletion,
    DPOPolicy,
    assemble_attributed_completions_result,
    build_dataset_diagnostics,
    build_dpo_bucket_sparsity,
    export_rlvr,
    project_dpo,
    project_sft,
    resolve_confidence_breakdown,
)

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "packages/capture/tests/fixtures"

ORG = "simcorp"
REPO_FULL = "simcorp/simcorp-billing"
REPOSITORY_ID = 90_000_001
TOOL = "claude-code"
# Stamper payload constants — must match scripts/sediment_attribution.py.
NOTES_REF = "refs/notes/sediment"
SCHEMA_VERSION = 1
STAMPED_AT = "2026-03-02T09:00:00+00:00"
# Scenario commits continue the generated history on a fixed later clock.
_SCEN_TS0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
# Fixed OTLP record time base (ns since epoch) — injected, never wall-clock.
_TIME_NS0 = 1_783_460_000_000_000_000


def _load_sibling(name: str):
    if name in sys.modules:  # one shared instance across harness modules
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: dataclasses resolves string annotations via
    # sys.modules[cls.__module__] and crashes on an unregistered module.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gen_repo = _load_sibling("gen_repo")


# ── scenario content ───────────────────────────────────────────────────────
# Vocabularies are deliberately distinct ACROSS scenarios (so completions
# cannot cross-match another scenario's diffs inside the shared attribution
# window) and deliberately similar WITHIN the cross-prompt pair (so the
# control is meaningful: similarity alone could confuse A and B).

_XPC_REMINDERS = '''"""Dunning reminder scheduling."""

REMINDER_STAGES = ("gentle", "firm", "final")


def schedule_reminder(invoice_id: int, stage: str, overdue_days: int) -> dict:
    if stage not in REMINDER_STAGES:
        raise ValueError("unknown reminder stage")
    return {
        "invoice_id": invoice_id,
        "stage": stage,
        "send_after_days": overdue_days + REMINDER_STAGES.index(stage) * 7,
    }
'''

_XPC_NOTICES = '''"""Dunning notice scheduling."""

NOTICE_STAGES = ("gentle", "firm", "final")


def schedule_notice(invoice_id: int, stage: str, overdue_days: int) -> dict:
    if stage not in NOTICE_STAGES:
        raise ValueError("unknown notice stage")
    return {
        "invoice_id": invoice_id,
        "stage": stage,
        "send_after_days": overdue_days + NOTICE_STAGES.index(stage) * 5,
    }
'''

# Hand-edit gradient: the completion the "agent" produced, and the content
# the "developer" actually committed after editing ~10% / ~30% / ~60% of it.
_GRAD_10_COMPLETION = '''"""Late fee assessment for overdue invoices."""

GRACE_DAYS = 5
LATE_FEE_BP = 250
WEEKLY_TRANCHE = 7


def assess_late_fee(balance_cents: int, overdue_days: int) -> int:
    if overdue_days <= GRACE_DAYS:
        return 0
    tranches = (overdue_days - GRACE_DAYS + WEEKLY_TRANCHE - 1) // WEEKLY_TRANCHE
    accrued = balance_cents * LATE_FEE_BP * tranches // 10_000
    return min(accrued, balance_cents // 4)
'''

_GRAD_10_COMMITTED = _GRAD_10_COMPLETION.replace(
    "    return min(accrued, balance_cents // 4)\n",
    "    capped = balance_cents // 4\n    return min(accrued, capped)\n",
)

_GRAD_30_COMPLETION = '''"""Refund issuance for returned subscriptions."""

REFUND_WINDOW_DAYS = 30
RESTOCK_FEE_BP = 500


def issue_refund(payment_cents: int, days_since_charge: int, plan: str) -> int:
    if days_since_charge > REFUND_WINDOW_DAYS:
        return 0
    restock = payment_cents * RESTOCK_FEE_BP // 10_000
    refundable = payment_cents - restock
    return refundable if plan != "enterprise" else payment_cents
'''

_GRAD_30_COMMITTED = '''"""Refund issuance for returned subscriptions."""

REFUND_WINDOW_DAYS = 30
PARTNER_TIERS = ("enterprise", "reseller", "agency_partner")


def issue_refund(amount_minor: int, days_since_charge: int, plan: str) -> int:
    if days_since_charge > REFUND_WINDOW_DAYS:
        return 0
    holdback = amount_minor * 500 // 10_000
    deduction = holdback if plan not in PARTNER_TIERS else 0
    return amount_minor - deduction
'''

_GRAD_60_COMPLETION = '''"""Write-off provisioning for delinquent balances."""

DELINQUENT_AFTER_DAYS = 120
PROVISION_RATE_BP = 7500


def provision_writeoff(balance_cents: int, delinquent_days: int) -> int:
    if delinquent_days < DELINQUENT_AFTER_DAYS:
        return 0
    provision = balance_cents * PROVISION_RATE_BP // 10_000
    return provision
'''

_GRAD_60_COMMITTED = '''"""Write-off provisioning for delinquent balances."""

AGING_BUCKETS = ((120, 7500), (240, 9000), (365, 10_000))


def provision_writeoff(outstanding_minor: int, aging_days: int) -> int:
    reserve_bp = 0
    for threshold_days, bucket_bp in AGING_BUCKETS:
        if aging_days >= threshold_days:
            reserve_bp = bucket_bp
    return outstanding_minor * reserve_bp // 10_000
'''

_BAIT_CREDIT_MEMO = '''"""Credit memo issuance against posted invoices."""

MAX_MEMO_CENTS = 50_000


def issue_credit_memo(invoice_id: int, amount_cents: int, reason_code: str) -> dict:
    if amount_cents <= 0 or amount_cents > MAX_MEMO_CENTS:
        raise ValueError("credit memo amount out of bounds")
    return {
        "invoice_id": invoice_id,
        "amount_cents": amount_cents,
        "reason_code": reason_code,
    }
'''

# "Retyped by hand" after rejecting: near-identical, as retyped code is.
_BAIT_RETYPED = _BAIT_CREDIT_MEMO.replace(
    '"""Credit memo issuance against posted invoices."""',
    '"""Credit memo issuance against posted invoices (manual)."""',
)

_TN_ABANDONED_1 = '''"""Chargeback arbitration intake (never shipped)."""


def open_arbitration_case(dispute_ref: str, network: str) -> dict:
    evidence_deadline_days = 14 if network == "visa_scheme" else 18
    return {"dispute_ref": dispute_ref, "deadline": evidence_deadline_days}
'''

_TN_ABANDONED_2 = '''"""Payout IBAN validation sketch (never shipped)."""


def validate_iban_checksum(iban_candidate: str) -> bool:
    rearranged = iban_candidate[4:] + iban_candidate[:4]
    numeric_form = int("".join(str(int(ch, 36)) for ch in rearranged))
    return numeric_form % 97 == 1
'''

_REBASE_COLLECTIONS = '''"""Collections escalation ladder."""

ESCALATION_LADDER = ("email", "phone", "agency")


def next_escalation(current: str) -> str | None:
    if current not in ESCALATION_LADDER:
        raise ValueError("unknown escalation step")
    position = ESCALATION_LADDER.index(current)
    if position + 1 < len(ESCALATION_LADDER):
        return ESCALATION_LADDER[position + 1]
    return None
'''

_SQUASH_SETTLEMENTS_PART = '''"""Settlement batching for payment providers."""

SETTLEMENT_CUTOFF_HOUR = 17


def settlement_batch_id(provider: str, day_ordinal: int) -> str:
    return f"{provider}-batch-{day_ordinal:05d}"
'''

_SQUASH_SETTLEMENTS_FULL = (
    _SQUASH_SETTLEMENTS_PART
    + """

def in_todays_batch(hour: int) -> bool:
    return hour < SETTLEMENT_CUTOFF_HOUR
"""
)

# Edit-observation wire pairs: applied_text is the Edit tool's new_string
# snippet, and observed_file_text is the whole file at session end.
# attach_edit_retention scorer contract. Pair 1 remains verbatim; pair 2 is
# half-rewritten.
_EO_SNIPPET = (
    "def satz_grenze(land: str) -> int:\n"
    "    grenzwert = MWST_SÄTZE.get(land, 1900)\n"
    "    return grenzwert\n"
)
_EO_FINAL_SURVIVED = (
    '"""VAT rates (session-end state)."""\n\n'
    'MWST_SÄTZE = {"DE": 1900, "AT": 2000, "FR": 2000}\n\n\n' + _EO_SNIPPET
)
_EO_FINAL_PARTIAL = (
    '"""VAT rates (session-end state)."""\n\n'
    'MWST_SÄTZE = {"DE": 1900, "AT": 2000, "FR": 2000}\n\n\n'
    "def satz_grenze(land: str) -> int:\n"
    "    return MWST_SÄTZE.get(land, 1900) + aufschlag_für(land)\n"
)
# Pinned honest output of four_gram_containment over the pair above (67/96
# of the snippet's 4-grams survive). Recompute if the pair changes; a scorer
# swap that moves this number is a deliberate decision, not drift.
_EO_PARTIAL_PINNED = 0.6979166666666666

# ── Group 2/3 content (again: one vocabulary family per scenario) ──────────

_RECOV_STATEMENTS = '''"""Monthly statement run assembly."""

STATEMENT_CYCLE_DAY = 28


def close_statement_run(period_ordinal: int, open_balances: list[int]) -> dict:
    carried_forward = sum(entry for entry in open_balances if entry > 0)
    return {
        "period_ordinal": period_ordinal,
        "carried_forward": carried_forward,
        "cycle_day": STATEMENT_CYCLE_DAY,
    }
'''

_CRA_APPROVALS = '''"""Manual approval queue for high-value invoices."""

APPROVAL_THRESHOLD_CENTS = 250_000


def needs_manual_approval(amount_cents: int, requester_role: str) -> bool:
    if requester_role == "billing_admin":
        return False
    return amount_cents >= APPROVAL_THRESHOLD_CENTS
'''

_CRB_FX = '''"""Foreign-exchange conversion for invoicing."""

FX_SPREAD_BP = 85


def convert_minor_units(amount_minor: int, mid_rate_ppm: int) -> int:
    spread_adjusted = mid_rate_ppm * (10_000 - FX_SPREAD_BP) // 10_000
    return amount_minor * spread_adjusted // 1_000_000
'''

_CRC_OUTBOUND = '''"""Outbound webhook notifications for paid invoices."""

OUTBOUND_RETRY_SCHEDULE_S = (30, 300, 3600)


def next_retry_delay(attempt_index: int) -> int | None:
    if attempt_index >= len(OUTBOUND_RETRY_SCHEDULE_S):
        return None
    return OUTBOUND_RETRY_SCHEDULE_S[attempt_index]
'''

# Same-prompt retry loop (the exact-count DPO box): four regenerations of
# "sequential invoice numbering", divergent enough that each attributes only
# to its own committed file.
_DPO_NUMBERING_A = '''"""Sequential invoice numbering."""

NUMBERING_PREFIX = "INV"


def next_invoice_number(counter: int) -> str:
    padded = str(counter).zfill(6)
    return NUMBERING_PREFIX + "-" + padded
'''

_DPO_NUMBERING_B = '''"""Sequential gapless invoice numbering."""

NUMBERING_PREFIX = "INV"
NUMBERING_WIDTH = 8


def next_invoice_number(counter: int, year: int) -> str:
    padded = str(counter).zfill(NUMBERING_WIDTH)
    return f"{NUMBERING_PREFIX}-{year}-{padded}"
'''

_DPO_NUMBERING_C = '''"""Sequential invoice numbering with a check digit."""

NUMBERING_PREFIX = "INV"


def next_invoice_number(counter: int) -> str:
    check_digit = counter * 7 % 10
    return f"{NUMBERING_PREFIX}-{counter:06d}-{check_digit}"
'''

_DPO_NUMBERING_D = '''"""Invoice numbering from an opaque draw (rejected approach)."""

import secrets

NUMBERING_PREFIX = "INV"


def next_invoice_number(counter: int) -> str:
    opaque_draw = secrets.token_hex(4)
    return f"{NUMBERING_PREFIX}-{opaque_draw}"
'''

_DPO_PORTAL = '''"""Customer portal access tokens."""

PORTAL_TOKEN_TTL_S = 900


def portal_token_expiry(issued_epoch_s: int) -> int:
    return issued_epoch_s + PORTAL_TOKEN_TTL_S
'''

_XSPLIT_SYNC_A = '''"""Ledger replication cursor tracking."""

REPLICATION_BATCH_ROWS = 500


def advance_replication_cursor(watermark: int, applied_rows: int) -> int:
    if applied_rows > REPLICATION_BATCH_ROWS:
        raise ValueError("applied more rows than the batch allows")
    return watermark + applied_rows
'''

_XSPLIT_SYNC_B = '''"""Ledger replication cursor tracking (checkpointed)."""

REPLICATION_BATCH_ROWS = 500
CHECKPOINT_EVERY_BATCHES = 10


def advance_replication_cursor(watermark: int, applied_rows: int) -> int:
    bounded_rows = min(applied_rows, REPLICATION_BATCH_ROWS)
    return watermark + bounded_rows
'''


def _cap_module(total_lines: int) -> str:
    """A module of exactly ``total_lines`` lines (trailing newline, so a
    new-file diff shows exactly that many added lines) whose tokens are
    unique to its own size — never a jaccard candidate for anything."""
    return (
        "\n".join(f"CAP{total_lines}_LINE_{i:03d} = {i}" for i in range(total_lines))
        + "\n"
    )


# The eval holdout over the sim's fixed session-id population at fraction
# 0.1 is a pure function of sha256, so the count is a constant —
# 10 of the 61 distinct completion-bearing sessions at the point the split
# scenario runs (Group 4/5 sessions arrive later and don't retro-shift it).
# Recompute (split_of over the session list) only when an earlier scenario
# adds or renames sessions.
_PINNED_EVAL_SESSIONS = 10

# ── Group 4/5 content ──────────────────────────────────────────────────────

# The wire shape an API-key-authed Claude Code session sends: Anthropic's
# metadata.user_id is a JSON blob. session_identity.py condenses it to the
# label below, so the envelope never carries raw JSON.
_P63_USER_BLOB = json.dumps(
    {
        "device_id": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "account_uuid": "",
        "session_id": "107e8b22-0000-4000-8000-000000000063",
    }
)
# What ``_identity_label`` (sediment_capture/session_identity.py) makes of
# that blob: the "device_" prefix plus the first 12 chars of device_id.
# Spelled out here rather than imported — it is a private helper of the
# resolver, and the pin is the wire value, not the implementation.
_P63_USER_LABEL = "device_" + json.loads(_P63_USER_BLOB)["device_id"][:12]
_P63_SESSION = "107e8b22-0000-4000-8000-000000000063"

_P63_ANOMALY = '''"""Spend-spike anomaly detector sketch (never shipped)."""


def is_spend_spike(window_totals_cents: list[int]) -> bool:
    trailing_median = sorted(window_totals_cents[:-1])[len(window_totals_cents) // 2]
    return window_totals_cents[-1] > trailing_median * 3
'''

_P61_NORMALIZER = '''"""Billing address normalizer sketch (never shipped)."""


def normalize_postcode(raw_postcode: str, country: str) -> str:
    compact = raw_postcode.replace(" ", "").upper()
    return compact if country != "NL" else compact[:4] + " " + compact[4:]
'''

_P34_CSV_MAPPER = '''"""CSV import column mapper sketch (never shipped)."""


def map_import_columns(header_row: list[str]) -> dict[str, int]:
    return {column.strip().lower(): idx for idx, column in enumerate(header_row)}
'''

# A two-file V4A patch so the codex fan-out is a real fan.
_P34_ARGUMENTS = (
    "*** Begin Patch\n"
    "*** Update File: calc.py\n"
    "@@\n def add(a, b):\n     return a + b\n+\n+\n+def multiply(a, b):\n"
    "+    return a * b\n"
    "*** Update File: util.py\n"
    "@@\n-PRECISION = 2\n+PRECISION = 4\n"
    "*** End Patch\n"
)

_OOO_ARCHIVAL = '''"""Cold-storage archival for settled invoices."""

ARCHIVE_AFTER_DAYS = 730


def should_archive(settled_days_ago: int, legal_hold: bool) -> bool:
    if legal_hold:
        return False
    return settled_days_ago >= ARCHIVE_AFTER_DAYS
'''

_OOO_RETENTION = '''"""Retention purge schedule for archived records."""

PURGE_AFTER_YEARS = 10


def purge_due(archived_year: int, current_year: int) -> bool:
    return current_year - archived_year >= PURGE_AFTER_YEARS
'''

_QUAR_DISPUTES = '''"""Dispute case tracker."""

DISPUTE_STAGES = ("opened", "evidence", "arbitration", "closed")


def advance_dispute(stage: str) -> str:
    position = DISPUTE_STAGES.index(stage)
    return DISPUTE_STAGES[min(position + 1, len(DISPUTE_STAGES) - 1)]
'''

_SKEW_CALENDAR = '''"""Billing calendar period boundaries."""

FISCAL_START_MONTH = 4


def period_for(month: int) -> int:
    return (month - FISCAL_START_MONTH) % 12 + 1
'''

_SKEW_TERMS = '''"""Payment terms in net days."""

TERMS_BY_TIER = {"starter": 14, "growth": 30, "enterprise": 45}


def due_in_days(tier: str) -> int:
    return TERMS_BY_TIER.get(tier, 30)
'''

# Group 6: a red→green pair created by the scenario itself. The buggy
# version deliberately shares almost no tokens with the gold fix (different
# docstring, different identifiers), so the fix inference call attributes ONLY
# to the fix commit — keeping the exported task's base_commit at the red
# buggy commit, exactly the SWE-bench shape.
_RLVR_ROUNDING_BUGGY = '''"""Floor cash amounts to a five-cent step."""


def round_to_nickel(amount_cents: int) -> int:
    truncated = amount_cents - amount_cents % 5
    return truncated
'''

_RLVR_ROUNDING_GOLD = '''"""Swedish rounding for invoice cash totals."""


def round_to_nickel(amount_cents: int) -> int:
    remainder = amount_cents % 5
    if remainder >= 3:
        return amount_cents + 5 - remainder
    return amount_cents - remainder
'''

_RLVR_ROUNDING_TEST = """from app.billing.rounding import round_to_nickel


def test_rounds_half_up_to_nearest_five():
    assert round_to_nickel(123) == 125


def test_exact_multiples_unchanged():
    assert round_to_nickel(120) == 120
"""


class SimWorld:
    """In-process sim deployment: patched settings singleton + TestClient +
    the generated repo working clone.

    The settings singleton pattern (see ``apps/api/tests/conftest.py``): org
    and secrets must be in the env before ``sediment_api.config`` is
    imported. Under a full pytest run that import already happened with the
    test org, so this patches the singleton's attributes directly — and
    restores them on exit, so the sim never leaks into other tests.
    """

    def __init__(self, workdir: Path, database_url: str) -> None:
        self.workdir = Path(workdir)
        self.database_url = database_url
        self.manifest_rows: list[dict[str, Any]] = []
        self._gateway_seq = 0
        self._ci_seq = 0
        self._saved: dict[str, Any] = {}
        self._client_started = False

    def __enter__(self) -> SimWorld:
        os.environ.setdefault("SEDIMENT_ORG_ID", ORG)
        os.environ.setdefault("SEDIMENT_DATABASE_URL", self.database_url)
        os.environ.setdefault("SEDIMENT_API_BEARER_TOKEN", "sim-token-9c41-ingest-2b7f")
        os.environ.setdefault("SEDIMENT_OPERATOR_TOKEN", "sim-operator-2ab6-token-8c1d")
        os.environ.setdefault(
            "SEDIMENT_GITHUB_WEBHOOK_SECRET", "sim-webhook-secret-d5f2-91a"
        )

        from fastapi.testclient import TestClient
        from pydantic import SecretStr
        from sediment_api.config import settings
        from sediment_api.main import app

        self.settings = settings
        overrides = {
            "org_id": ORG,
            "api_bearer_token": "sim-token-9c41-ingest-2b7f",
            "database_url": SecretStr(self.database_url),
            "mirror_path": str(self.workdir / "mirrors"),
            "github_host": "git.simcorp.example",
            # file:// clone_url for the sim origin — MirrorPolicy enforces
            # scheme confinement only outside dev mode.
            "dev_mode": True,
        }
        self._saved = {k: getattr(settings, k) for k in overrides}
        for key, value in overrides.items():
            setattr(settings, key, value)

        # From here on the shared singleton is patched: a failure below
        # (generation, git config, client construction) skips __exit__, so
        # restore before re-raising — a crashed sim must not poison other
        # test modules' view of the settings (review-closeout finding).
        try:
            self.repo_manifest = gen_repo.generate(self.workdir)
            self.repo = gen_repo._Repo(
                self.workdir / gen_repo.REPO_NAME, random.Random(20260302)
            )
            self.repo.clock = _SCEN_TS0
            # The installed-clone posture: the stamper installer sets
            # notes.rewriteRef so amend/rebase copy the attribution note.
            self.repo.git("config", "notes.rewriteRef", NOTES_REF)
            self.client = TestClient(app)
            self.client.__enter__()
            self._client_started = True
            self.app = app
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc: object) -> bool:
        if self._client_started:
            self.client.__exit__(None, None, None)
            self._client_started = False
        for key, value in self._saved.items():
            setattr(self.settings, key, value)
        return False

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.api_bearer_token}"}

    def fire_gateway(
        self,
        session_id: str,
        completion_text: str,
        *,
        prompt: str,
        call_id: str | None = None,
        user_id: str = "dev@simcorp.example",
    ) -> str:
        """One LiteLLM callback POST → one InferenceCall fact. Returns its id.

        ``call_id`` becomes the model-call dedup/join key: pass it explicitly
        when a decision must join this call (``join_decisions_by_call_id``
        matches the OTLP decision's ``tool_use_id`` against it)."""
        payload = _fixture("litellm_standard_logging_object.json")
        self._gateway_seq += 1
        if call_id is None:
            call_id = f"sim-call-{self._gateway_seq:04d}"
        payload["litellm_call_id"] = call_id
        payload["trace_id"] = session_id
        payload["messages"] = [{"role": "user", "content": prompt}]
        payload["response"]["id"] = f"chatcmpl-{call_id}"
        payload["response"]["choices"][0]["message"]["content"] = completion_text
        meta = payload["metadata"]["requester_metadata"]
        meta["session_id"] = session_id
        meta["org_id"] = ORG
        return self.fire_gateway_payload(session_id, payload, user_id=user_id)

    def fire_gateway_payload(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        user_id: str = "dev@simcorp.example",
    ) -> str:
        """One gateway-callback POST of an already-built payload → one
        InferenceCall fact. Returns its id."""
        body = {
            "provider": "litellm",
            "session_id": session_id,
            "user_id": user_id,
            "payload": payload,
        }
        resp = self.client.post("/ingest/gateway", json=body, headers=self._auth())
        assert resp.status_code == 200 and resp.json()["stored"] is True, resp.text
        return resp.json()["fact_id"]

    def fire_otlp(self, payload: dict[str, Any]) -> None:
        resp = self.client.post("/v1/logs", json=payload, headers=self._auth())
        assert resp.status_code == 200 and resp.json() == {}, resp.text

    def fire_push(
        self,
        before: str,
        after: str,
        *,
        forced: bool = False,
        expect_stored: bool = True,
    ) -> str:
        """One signed push webhook → Push fact + mirror fetch + scoped
        derivation (the background task runs synchronously under TestClient).
        ``expect_stored=False`` asserts the redelivery contract instead
        (``stored: false``, nothing new persisted).
        """
        payload = _fixture("github_push.json")
        payload["ref"] = "refs/heads/main"
        payload["before"] = before
        payload["after"] = after
        payload["forced"] = forced
        repository = self._retarget_repository(payload)
        repository["html_url"] = "https://git.simcorp.example/simcorp-billing"
        repository["ssh_url"] = "git@git.simcorp.example:simcorp-billing.git"
        payload["commits"] = [
            {
                "id": after,
                "message": "sim push",
                "added": [],
                "removed": [],
                "modified": [],
            }
        ]
        payload["head_commit"] = {"id": after, "message": "sim push"}
        return self._fire_webhook("/ingest/github/push", "push", payload, expect_stored)

    def fire_ci(
        self,
        head_sha: str,
        *,
        workflow: str,
        conclusion: str,
        branch: str = "main",
        run_id: int | None = None,
        run_attempt: int | None = None,
        expect_stored: bool = True,
    ) -> str:
        """One signed ``workflow_run`` webhook → CIOutcome fact (or a clean
        skip for a non-completed run). ``workflow`` selects one synthetic
        definition with a stable positive ID, display name, and workflow path.
        ``conclusion`` passes through verbatim so non-verdict states retain
        their capture meaning. Pass the same ``run_id`` and ``run_attempt``
        to redeliver a run (``expect_stored=False``); another attempt retains
        the workflow definition. Returns the fact id."""
        payload = _fixture("github_workflow_run.json")
        self._ci_seq += 1
        run = payload["workflow_run"]
        run["id"] = run_id if run_id is not None else 90_000_000 + self._ci_seq
        # Synthetic provider IDs must not inherit the one frozen fixture's
        # identity. Hash the definition name, not emission order or run attempt.
        workflow_id = (
            int.from_bytes(hashlib.sha256(workflow.encode()).digest()[:7], "big") + 1
        )
        run["workflow_id"] = workflow_id
        run["name"] = workflow
        run["path"] = f".github/workflows/{workflow}.yml"
        run["head_branch"] = branch
        run["head_sha"] = head_sha
        run["run_number"] = self._ci_seq
        if run_attempt is not None:
            run["run_attempt"] = run_attempt
        run["conclusion"] = conclusion
        run["html_url"] = (
            f"https://git.simcorp.example/simcorp-billing/actions/runs/{run['id']}"
        )
        payload["workflow"]["id"] = workflow_id
        payload["workflow"]["name"] = workflow
        payload["workflow"]["path"] = run["path"]
        self._retarget_repository(payload)
        return self._fire_webhook(
            "/ingest/github/ci", "workflow_run", payload, expect_stored
        )

    def _retarget_repository(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Give every forge fixture the sim clone's explicit synthetic identity."""
        repository = payload["repository"]
        repository["id"] = REPOSITORY_ID
        repository["name"] = "simcorp-billing"
        repository["full_name"] = REPO_FULL
        repository["owner"]["login"] = "simcorp"
        repository["clone_url"] = f"file://{self.repo.path}"
        return repository

    def _fire_webhook(
        self, route: str, event: str, payload: dict[str, Any], expect_stored: bool
    ) -> str:
        """One HMAC-signed forge delivery. Returns the fact id."""
        body = json.dumps(payload).encode()
        resp = self.client.post(
            route,
            content=body,
            headers={
                "X-Hub-Signature-256": sign_payload(
                    body, self.settings.github_webhook_secret
                ),
                "X-GitHub-Event": event,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["stored"] is expect_stored, resp.text
        return resp.json()["fact_id"]

    def head(self) -> str:
        return self.repo.git("rev-parse", "HEAD")

    def write_commit(self, rel: str, content: str, message: str) -> str:
        self.repo.write(rel, content)
        return self.repo.commit(message)

    def stamp_notes(self, session_ids: list[str]) -> None:
        """Write the attribution note on HEAD exactly as the stamper does
        (``scripts/sediment_attribution.py`` ``cmd_stamp``): sorted unique
        (tool, session_id) pairs, one shared stamped_at, schema v1,
        ``git notes add -f`` on the fixed ref."""
        unique = sorted({(TOOL, s) for s in session_ids})
        payload = json.dumps(
            {
                "v": SCHEMA_VERSION,
                "sessions": [
                    {"tool": tool, "session_id": session_id, "stamped_at": STAMPED_AT}
                    for tool, session_id in unique
                ],
            }
        )
        self.repo.git(
            "notes",
            f"--ref={NOTES_REF}",
            "add",
            "-f",
            "-F",
            "-",
            "HEAD",
            stdin=payload,
        )

    def store(self) -> nullcontext[FactStore]:
        """Borrow the API process's lifespan-owned PostgreSQL fact store."""
        return nullcontext(self.app.state.fact_store)

    def push_fact(self, after_sha: str) -> Push:
        with self.store() as store:
            [push] = [p for p in store.read_pushes(ORG) if p.after_sha == after_sha]
        return push

    def derive(
        self,
        *,
        pushes: list[Push] | None = None,
        policy: AttributionPolicy | None = None,
    ) -> list[Attribution]:
        with self.store() as store, store.read_snapshot() as snapshot:
            repository_context = read_repository_context(snapshot, ORG)
            boundary = max(
                repository_context.as_of,
                max(
                    (
                        call.observed_at.astimezone(UTC)
                        for call in snapshot.read_inference_call_summaries(ORG)
                    ),
                    default=datetime.min.replace(tzinfo=UTC),
                ),
            )
            if boundary != repository_context.as_of:
                repository_context = read_repository_context(
                    snapshot, ORG, as_of=boundary
                )
            return derive_attributions(
                snapshot,
                MirrorManager(self.settings.mirror_path),
                ORG,
                policy,
                pushes=pushes,
                repository_context=repository_context,
                as_of=repository_context.as_of,
            )

    def derive_for(self, after_sha: str) -> list[Attribution]:
        return self.derive(pushes=[self.push_fact(after_sha)])

    def recovery(self):
        """Full recovery-pair derivation (``derive_recovery_result``) over
        the sim's facts and mirrors, default policy."""
        with self.store() as store:
            return derive_recovery_result(
                store, MirrorManager(self.settings.mirror_path), ORG
            )

    def attributed_completions(self, eval_fraction: float = 0.0):
        """Full attributed-completion assembly over the sim's facts and mirrors."""
        return self.attributed_completion_evidence(eval_fraction)[0]

    def attributed_completion_evidence(
        self, eval_fraction: float = 0.0
    ) -> tuple[list[AttributedCompletion], RepositoryContext]:
        """Assemble rows and their complete repository context in one snapshot."""
        with self.store() as store, store.read_snapshot() as snapshot:
            assembly = assemble_attributed_completions_result(
                snapshot,
                MirrorManager(self.settings.mirror_path),
                ORG,
                AttributedCompletionPolicy(eval_fraction=eval_fraction),
            )
            assert assembly.repository_context is not None
            return assembly.rows, assembly.repository_context

    def inference_calls_by_id(self):
        """Fact id to inference call — the projection lookup shape."""
        with self.store() as store:
            calls = store.read_inference_calls(ORG)
            return {inference_fact_id(call): call for call in calls}

    def expect(
        self,
        scenario: str,
        inference_call_id: str,
        *,
        commit: str | None = None,
        file: str | None = None,
        attribution: str | None,
        true_link: bool,
        reward_min: float | None = None,
        note: str = "",
    ) -> None:
        self.manifest_rows.append(
            {
                "scenario": scenario,
                "inference_call_id": inference_call_id,
                "expected_commit": commit,
                "expected_file": file,
                "expected_attribution": attribution,
                "true_link": true_link,
                "expected_reward_min": reward_min,
                "notes": note,
            }
        )


def _fixture(rel: str) -> dict[str, Any]:
    """A wire-verified fixture, freshly parsed so callers can retarget it in
    place without aliasing another call's payload."""
    return json.loads((FIXTURES / rel).read_text())


def _log_records(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every OTLP log record in an export request, across resource/scope."""
    for resource_logs in payload["resourceLogs"]:
        for scope_logs in resource_logs["scopeLogs"]:
            yield from scope_logs["logRecords"]


def _otlp_stamp(time_ns: int) -> str:
    """``event.timestamp`` as the wire spells it: UTC ISO-8601, milliseconds."""
    return (
        datetime.fromtimestamp(time_ns / 1e9, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[
            :-3
        ]
        + "Z"
    )


def _otlp_decision_payload(
    fixture_name: str,
    session_id: str,
    tool_use_id: str,
    time_ns: int,
    file_path: str | None = None,
) -> dict[str, Any]:
    """A wire-verified Claude Code OTLP batch with the scenario's identifiers
    substituted in place — record structure, resource attributes, and every
    unrelated attribute stay exactly as captured."""
    payload = _fixture(f"otlp/claude_code/{fixture_name}")
    stamp = _otlp_stamp(time_ns)
    for record in _log_records(payload):
        record["timeUnixNano"] = str(time_ns)
        record["observedTimeUnixNano"] = str(time_ns)
        for attr in record["attributes"]:
            value = attr["value"]
            if attr["key"] == "session.id":
                value["stringValue"] = session_id
            elif attr["key"] == "tool_use_id":
                value["stringValue"] = tool_use_id
            elif attr["key"] == "event.timestamp":
                value["stringValue"] = stamp
            elif attr["key"] == "tool_input" and file_path is not None:
                tool_input = json.loads(value["stringValue"])
                tool_input["file_path"] = file_path
                value["stringValue"] = json.dumps(tool_input)
    return payload


def _codex_payload(
    conversation_id: str,
    call_id: str,
    observed_ns: int,
    *,
    arguments: str | None = None,
    decision_only: bool = False,
) -> dict[str, Any]:
    """The wire-verified Codex OTLP batch (``accept_apply_patch.json``) with
    the scenario's identifiers substituted. Codex's shape quirks preserved:
    ``timeUnixNano`` stays ``"0"`` (the real time lives in
    ``observedTimeUnixNano``), and the per-file fan-out comes from the
    result's V4A patch ``arguments``. ``decision_only=True`` drops the
    ``tool_result`` record — the redelivery shape whose missing arguments
    collapse the fan to one ``file_path=""`` row."""
    payload = _fixture("otlp/codex/accept_apply_patch.json")
    stamp = _otlp_stamp(observed_ns)
    for resource_logs in payload["resourceLogs"]:
        for scope_logs in resource_logs["scopeLogs"]:
            kept_records = []
            for record in scope_logs["logRecords"]:
                attrs = {a["key"]: a for a in record["attributes"]}
                event = attrs["event.name"]["value"]["stringValue"]
                if decision_only and event == "codex.tool_result":
                    continue
                record["observedTimeUnixNano"] = str(observed_ns)
                attrs["conversation.id"]["value"]["stringValue"] = conversation_id
                attrs["call_id"]["value"]["stringValue"] = call_id
                attrs["event.timestamp"]["value"]["stringValue"] = stamp
                if arguments is not None and "arguments" in attrs:
                    attrs["arguments"]["value"]["stringValue"] = arguments
                kept_records.append(record)
            scope_logs["logRecords"] = kept_records
    return payload


def _copilot_survival_payload(
    session_id: str,
    request_id: str,
    time_ns: int,
    *,
    survival_rate: float,
    window_ms: int,
) -> dict[str, Any]:
    """The wire-verified Copilot ``edit.survival`` batch with the scenario's
    identity, graded rate, and delay bucket substituted — the resource-scope
    ``session.id`` and the non-decision records (``gen_ai.*``,
    ``agent.turn``) kept exactly as captured."""
    payload = _fixture("otlp/copilot/edit_survival.json")
    for resource_logs in payload["resourceLogs"]:
        for attr in resource_logs["resource"]["attributes"]:
            if attr["key"] == "session.id":
                attr["value"]["stringValue"] = session_id
    for record in _log_records(payload):
        record["timeUnixNano"] = str(time_ns)
        record["observedTimeUnixNano"] = str(time_ns)
        for attr in record["attributes"]:
            key, value = attr["key"], attr["value"]
            if key == "request_id":
                value["stringValue"] = request_id
            elif key == "survival_rate_four_gram":
                # The wire encodes whole rates as intValue; a graded rate
                # rides doubleValue (both valid OTLP AnyValue).
                value.clear()
                value["doubleValue"] = survival_rate
            elif key == "time_delay_ms":
                value.clear()
                value["intValue"] = window_ms  # int64, as captured
    return payload


def _edit_observation_payload(
    session_id: str,
    tool_use_id: str,
    file_path: str,
    applied_text: str,
    observed_file_text: str,
    time_ns: int,
) -> dict[str, Any]:
    """The transcript extractor's ``sediment.edit_observation`` record — the same
    wire shape ``apps/api/tests/test_e2e_smoke.py`` verifies."""
    record = {
        "body": {"stringValue": "sediment.edit_observation"},
        "timeUnixNano": str(time_ns),
        "attributes": [
            {"key": key, "value": {"stringValue": value}}
            for key, value in {
                "session.id": session_id,
                "tool_use_id": tool_use_id,
                "tool_name": "Write",
                "file_path": file_path,
                "applied_text": applied_text,
                "observed_file_text": observed_file_text,
                "agent": "claude-code",
            }.items()
        ],
    }
    return {"resourceLogs": [{"scopeLogs": [{"logRecords": [record]}]}]}


# ── Group 1 scenarios ──────────────────────────────────────────────────────


def scenario_cross_prompt_control(world: SimWorld) -> None:
    """The cross-prompt e2e box: two concurrent sessions with similar
    prompts and heavily overlapping completions; the commit carries session
    A's code and session A's notes stamp while B is active. Notes must
    attribute A — similarity alone could plausibly pick either."""
    a = world.fire_gateway(
        "sess-sim-xpc-a", _XPC_REMINDERS, prompt="add a dunning reminder scheduler"
    )
    b = world.fire_gateway(
        "sess-sim-xpc-b", _XPC_NOTICES, prompt="add a dunning notice scheduler"
    )
    before = world.head()
    after = world.write_commit(
        "app/billing/reminders.py", _XPC_REMINDERS, "Add dunning reminder schedule"
    )
    world.stamp_notes(["sess-sim-xpc-a"])
    world.fire_push(before, after)

    attributions = world.derive_for(after)
    assert len(attributions) == 1, attributions
    match = attributions[0]
    assert match.file_path == "app/billing/reminders.py"
    assert match.inference_call_id == a
    assert match.session_id == "sess-sim-xpc-a"
    assert match.attribution_source == AttributionSource.GIT_NOTES

    world.expect(
        "cross_prompt_control",
        a,
        commit=after,
        file="app/billing/reminders.py",
        attribution="git_notes",
        true_link=True,
        note="session A stamped; B active concurrently with a similar prompt",
    )
    world.expect(
        "cross_prompt_control",
        b,
        attribution=None,
        true_link=False,
        note="concurrent session B: similar completion, never landed",
    )


def scenario_hand_edit_gradient(world: SimWorld) -> None:
    """Completions applied then hand-edited ~10% / ~30% / ~60% before commit
    (graded ground truth). At the default 0.7 threshold only the 10% edit
    attributes; the 30% and 60% edits fall below and surface in the
    threshold sweep instead."""
    session = "sess-sim-grad"
    g10 = world.fire_gateway(session, _GRAD_10_COMPLETION, prompt="add late fees")
    g30 = world.fire_gateway(session, _GRAD_30_COMPLETION, prompt="add refunds")
    g60 = world.fire_gateway(session, _GRAD_60_COMPLETION, prompt="add write-offs")

    before = world.head()
    sha10 = world.write_commit(
        "app/billing/late_fees.py", _GRAD_10_COMMITTED, "Add late fee assessment"
    )
    sha30 = world.write_commit(
        "app/billing/refunds.py", _GRAD_30_COMMITTED, "Add refund issuance"
    )
    sha60 = world.write_commit(
        "app/billing/writeoffs.py", _GRAD_60_COMMITTED, "Add write-off provisioning"
    )
    world.fire_push(before, sha60)

    attributions = world.derive_for(sha60)
    assert len(attributions) == 1, attributions
    match = attributions[0]
    assert (match.inference_call_id, match.file_path, match.commit_sha) == (
        g10,
        "app/billing/late_fees.py",
        sha10,
    )
    assert match.attribution_source == AttributionSource.JACCARD
    assert match.similarity_score >= 0.9  # near-verbatim application

    world.expect(
        "hand_edit_gradient_10",
        g10,
        commit=sha10,
        file="app/billing/late_fees.py",
        attribution="jaccard",
        true_link=True,
        note="~10% hand-edited; attributes at the default 0.7 threshold",
    )
    world.expect(
        "hand_edit_gradient_30",
        g30,
        commit=sha30,
        file="app/billing/refunds.py",
        attribution=None,
        true_link=True,
        note="~30% hand-edited; below 0.7, recovered in the sweep near 0.6",
    )
    world.expect(
        "hand_edit_gradient_60",
        g60,
        commit=sha60,
        file="app/billing/writeoffs.py",
        attribution=None,
        true_link=True,
        note="~60% hand-edited; below 0.7, recovered in the sweep near 0.5",
    )


def scenario_false_positive_bait(world: SimWorld) -> None:
    """A rejected completion whose code the developer later types anyway.

    At the attribution layer an applied completion and a retyped one are
    indistinguishable — the commit content IS the completion content — so
    the pipeline attributes it via jaccard (asserted here, so a behavior
    change is caught). Ground truth still labels it a NON-link: the reject
    decision is the recorded fact reward policy uses, and the manifest row
    charges this match against jaccard precision (the pinned floor accounts
    for it — measuring discrimination, not just recall)."""
    session = "sess-sim-bait"
    bait = world.fire_gateway(
        session, _BAIT_CREDIT_MEMO, prompt="add credit memo issuance"
    )
    world.fire_otlp(
        _otlp_decision_payload(
            "tool_decision_reject_user.json",
            session,
            "toolu-sim-bait-1",
            _TIME_NS0 + 1_000_000_000,
        )
    )
    before = world.head()
    after = world.write_commit(
        "app/billing/credit_memo.py", _BAIT_RETYPED, "Add credit memo issuance"
    )
    world.fire_push(before, after)

    attributions = world.derive_for(after)
    assert len(attributions) == 1, attributions
    match = attributions[0]
    assert match.inference_call_id == bait
    assert match.attribution_source == AttributionSource.JACCARD
    assert match.similarity_score >= 0.9  # retyped code is near-identical

    world.expect(
        "false_positive_bait",
        bait,
        commit=after,
        file="app/billing/credit_memo.py",
        attribution="jaccard",
        true_link=False,
        note=(
            "rejected then retyped by hand: pipeline attributes via jaccard "
            "(asserted), ground truth counts it a false positive"
        ),
    )


def scenario_true_negatives(world: SimWorld) -> None:
    """Abandoned completions that never land anywhere: no attribution may
    claim them, at the default threshold, ever."""
    tn1 = world.fire_gateway(
        "sess-sim-tn-1", _TN_ABANDONED_1, prompt="sketch chargeback arbitration"
    )
    tn2 = world.fire_gateway(
        "sess-sim-tn-2", _TN_ABANDONED_2, prompt="sketch IBAN validation"
    )
    for inference_call_id, note in (
        (tn1, "abandoned chargeback sketch; never committed"),
        (tn2, "abandoned IBAN sketch; never committed"),
    ):
        world.expect(
            "true_negatives",
            inference_call_id,
            attribution=None,
            true_link=False,
            note=note,
        )
    # The no-attribution assert runs in run_all's final full derivation —
    # later scenarios' pushes must not claim these either.


def scenario_rebase_squash_survival(world: SimWorld) -> None:
    """Stamped commits rebased and squash-merged (SHA rewrite survival).

    Rebase: the installer configures ``notes.rewriteRef``, so git copies the
    note onto the rewritten commit — notes attribution survives (asserted).
    Squash-merge: the squash commit is a brand-new commit created after the
    stamper's own markers were already cleared (each squashed branch commit
    was stamped individually), and note-rewrite copying does not apply to
    ``merge --squash``. The stamper's ``prepare-commit-msg`` hook
    (``union-squash-notes``) reads the squashed commits' SHAs from
    ``.git/SQUASH_MSG`` while it still exists and unions their
    already-written notes back into the local markers, so the existing
    post-commit ``stamp`` step writes a correct union note on the squash
    commit too — notes attribution survives here as well (asserted)."""
    repo = world.repo

    # — rebase leg —
    session_rebase = "sess-sim-rebase"
    rebase_completion = world.fire_gateway(
        session_rebase, _REBASE_COLLECTIONS, prompt="add collections escalation"
    )
    fork_point = world.head()
    repo.git("checkout", "-q", "-b", "feature/collections")
    branch_sha = world.write_commit(
        "app/billing/collections.py", _REBASE_COLLECTIONS, "Add collections ladder"
    )
    world.stamp_notes([session_rebase])
    repo.git("checkout", "-q", "main")
    world.write_commit(
        "docs/collections_rollout.md",
        "# Collections rollout\n\nStaged by region.\n",
        "Document collections rollout",
    )
    repo.git("checkout", "-q", "feature/collections")
    repo.git("rebase", "main")
    rebased_sha = world.head()
    assert rebased_sha != branch_sha  # the rewrite actually happened
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--ff-only", "feature/collections")
    world.fire_push(fork_point, rebased_sha)

    attributions = world.derive_for(rebased_sha)
    assert len(attributions) == 1, attributions
    match = attributions[0]
    assert match.commit_sha == rebased_sha
    assert match.inference_call_id == rebase_completion
    assert match.attribution_source == AttributionSource.GIT_NOTES, (
        "notes attribution did not survive the rebase — rewriteRef copy broken?"
    )
    world.expect(
        "rebase_survival",
        rebase_completion,
        commit=rebased_sha,
        file="app/billing/collections.py",
        attribution="git_notes",
        true_link=True,
        note="stamped on the pre-rebase SHA; note copied via notes.rewriteRef",
    )

    # — squash leg —
    session_squash = "sess-sim-squash"
    squash_completion = world.fire_gateway(
        session_squash, _SQUASH_SETTLEMENTS_FULL, prompt="add settlement batching"
    )
    before = world.head()
    repo.git("checkout", "-q", "-b", "feature/settlements")
    world.write_commit(
        "app/billing/settlements.py", _SQUASH_SETTLEMENTS_PART, "Add settlement ids"
    )
    world.stamp_notes([session_squash])
    world.write_commit(
        "app/billing/settlements.py", _SQUASH_SETTLEMENTS_FULL, "Add cutoff check"
    )
    world.stamp_notes([session_squash])
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--squash", "feature/settlements")
    squash_sha = repo.commit("Add settlement batching (squashed)")
    # prepare-commit-msg (union-squash-notes) read the squashed branch
    # commits' already-written notes from .git/SQUASH_MSG before the
    # commit above completed, and post-commit (stamp) wrote the union onto
    # the squash commit — simulated here the same way stamp_notes() stands
    # in for a real stamp everywhere else in this scenario.
    world.stamp_notes([session_squash])
    world.fire_push(before, squash_sha)

    attributions = world.derive_for(squash_sha)
    assert len(attributions) == 1, attributions
    match = attributions[0]
    assert match.commit_sha == squash_sha
    assert match.inference_call_id == squash_completion
    assert match.attribution_source == AttributionSource.GIT_NOTES, (
        "squash-merge notes attribution regressed — union-squash-notes broken?"
    )
    world.expect(
        "squash_survival",
        squash_completion,
        commit=squash_sha,
        file="app/billing/settlements.py",
        attribution="git_notes",
        true_link=True,
        note="squash-merge carries attribution via the union-squash-notes hook",
    )


def scenario_edit_observation(world: SimWorld) -> None:
    """EditObservation ground truth: accept decisions plus
    ``sediment.edit_observation`` pairs through ``/v1/logs``, then
    ``attach_edit_retention`` + ``four_gram_containment`` over the read-back
    facts. Pair 1 survives verbatim (rate 1.0); pair 2 is half-rewritten
    (pinned partial rate)."""
    session = "sess-sim-eo"
    file_path = "/sim/simcorp-billing/app/billing/rates.py"
    for seq, (tool_use_id, applied_text, observed_file_text) in enumerate(
        (
            ("toolu-sim-eo-1", _EO_SNIPPET, _EO_FINAL_SURVIVED),
            ("toolu-sim-eo-2", _EO_SNIPPET, _EO_FINAL_PARTIAL),
        )
    ):
        time_ns = _TIME_NS0 + (10 + seq) * 1_000_000_000
        world.fire_otlp(
            _otlp_decision_payload(
                "tool_decision_accept_user.json",
                session,
                tool_use_id,
                time_ns,
                file_path=file_path,
            )
        )
        world.fire_otlp(
            _edit_observation_payload(
                session,
                tool_use_id,
                file_path,
                applied_text,
                observed_file_text,
                time_ns + 1,
            )
        )

    with world.store() as store:
        decisions = [d for d in store.read_decisions(ORG) if d.session_id == session]
        outcomes = store.read_edit_observations(ORG)
    assert len(decisions) == 2 and len(outcomes) == 2
    assert all(d.accepted and d.edit_retention_score is None for d in decisions)
    assert all(o.file_path == file_path for o in outcomes)

    filled = attach_edit_retention(decisions, outcomes, four_gram_containment)
    by_call = {d.call_id: d for d in filled}
    assert by_call["toolu-sim-eo-1"].edit_retention_score == 1.0
    partial = by_call["toolu-sim-eo-2"].edit_retention_score
    assert partial is not None and abs(partial - _EO_PARTIAL_PINNED) < 1e-9
    assert 0.0 < partial < 1.0, partial


# ── Group 2 scenarios ──────────────────────────────────────────────────────


def scenario_ci_lineage_recovery(world: SimWorld) -> None:
    """Per-lineage CI transitions: red→red→green pairs on the LAST red
    (tightest window), non-verdict runs skipped over as non-boundaries, and
    aggregate workflow ambiguity excluded. The kept pair is enriched with the
    completion attributed to the failed commit."""
    session = "sess-sim-recov"
    recov = world.fire_gateway(
        session, _RECOV_STATEMENTS, prompt="add statement run close"
    )
    runbook = "# Audit runbook\n\nRotate quarterly.\n"
    before = world.head()
    c1 = world.write_commit("docs/runbook_audit.md", runbook, "Document audit runbook")
    c2 = world.write_commit(
        "app/billing/statement_runs.py", _RECOV_STATEMENTS, "Add statement run close"
    )
    runbook += "Escalate misses.\n"
    c3 = world.write_commit("docs/runbook_audit.md", runbook, "Note audit escalation")
    runbook += "Weekly review.\n"
    c4 = world.write_commit("docs/runbook_audit.md", runbook, "Note weekly review")
    runbook += "Archive yearly.\n"
    c5 = world.write_commit("docs/runbook_audit.md", runbook, "Note yearly archive")
    world.fire_push(before, c5)

    # The interleaved verdict timeline (firing order IS captured_at order):
    #   test: fail(c1) fail(c2) PASS(c3) fail(c4) cancelled(c5)
    #   lint: pass(c1) fail(c2) timed_out(c3) PASS(c4)
    world.fire_ci(c1, workflow="test", conclusion="failure")
    world.fire_ci(c1, workflow="lint", conclusion="success")
    world.fire_ci(c2, workflow="test", conclusion="failure")
    world.fire_ci(c2, workflow="lint", conclusion="failure")
    world.fire_ci(c3, workflow="test", conclusion="success")
    lint_c3 = world.fire_ci(c3, workflow="lint", conclusion="timed_out")
    world.fire_ci(c4, workflow="test", conclusion="failure")
    world.fire_ci(c4, workflow="lint", conclusion="success")
    test_c5 = world.fire_ci(c5, workflow="test", conclusion="cancelled")

    with world.store() as store:
        outcomes = {o.outcome_id: o for o in store.read_ci_outcomes(ORG)}
    # Distinct non-verdict states survive capture and remain transition-neutral.
    assert outcomes[lint_c3].result == CIResult.TIMED_OUT
    assert outcomes[test_c5].result == CIResult.CANCELLED

    result = world.recovery()
    pairs = {
        (p.workflow_path, p.failed_commit_sha, p.fixed_commit_sha) for p in result.pairs
    }
    assert pairs == {
        # last red before the green wins (c1's red superseded by c2's)
        (".github/workflows/test.yml", c2, c3),
    }, pairs
    # Enrichment: the failed commit is c2, which attributes to the statement-run
    # completion (jaccard, applied verbatim).
    assert all(p.failed_inference_call_ids == [recov] for p in result.pairs)
    assert dict(result.skipped) == {"ambiguous_workflow_verdicts": 2}, result.skipped
    assert result.kept_diff_line_counts == [1]  # the c3 doc edit
    # c1 and c4 have conflicting workflow verdicts, so neither supplies a
    # recovery boundary. The trailing cancelled run also supplies no boundary.

    world.expect(
        "ci_lineage_recovery",
        recov,
        commit=c2,
        file="app/billing/statement_runs.py",
        attribution="jaccard",
        true_link=True,
        note="applied verbatim at the failed commit; enriches the recovery pair",
    )


def scenario_recovery_cap_boundary(world: SimWorld) -> None:
    """The 200-line recovery-diff cap boundary: pairs whose fixing diff is
    199, exactly 200, and 201 changed lines — kept, kept (the cap is
    inclusive), dropped (logged + tallied, with the actual line count
    recorded on both sides of the gate)."""
    before = world.head()
    e0 = world.write_commit(
        "docs/integration_notes.md", "# Integration notes\n", "Seed integration notes"
    )
    e1 = world.write_commit(
        "app/billing/cap_pad_199.py", _cap_module(199), "Pad rate table (199)"
    )
    e2 = world.write_commit(
        "docs/integration_notes.md",
        "# Integration notes\n\nStart second run.\n",
        "Start second integration run",
    )
    e3 = world.write_commit(
        "app/billing/cap_pad_200.py", _cap_module(200), "Pad rate table (200)"
    )
    e4 = world.write_commit(
        "docs/integration_notes.md",
        "# Integration notes\n\nStart second run.\nStart third run.\n",
        "Start third integration run",
    )
    e5 = world.write_commit(
        "app/billing/cap_pad_201.py", _cap_module(201), "Pad rate table (201)"
    )
    world.fire_push(before, e5)

    world.fire_ci(e0, workflow="integration", conclusion="failure")
    world.fire_ci(e1, workflow="integration", conclusion="success")  # diff = 199
    world.fire_ci(e2, workflow="integration", conclusion="failure")
    world.fire_ci(e3, workflow="integration", conclusion="success")  # diff = 200
    world.fire_ci(e4, workflow="integration", conclusion="failure")
    world.fire_ci(e5, workflow="integration", conclusion="success")  # diff = 201

    result = world.recovery()
    # The clean lineage pair from the previous scenario plus 199 and 200 —
    # exactly at the cap is KEPT (<=); 201 is dropped and tallied.
    assert sorted(result.kept_diff_line_counts) == [1, 199, 200], (
        result.kept_diff_line_counts
    )
    assert result.dropped_diff_line_counts == [201]
    assert result.skipped["diff_oversized"] == 1
    boundary_pairs = {
        (p.failed_commit_sha, p.fixed_commit_sha)
        for p in result.pairs
        if p.workflow_path == ".github/workflows/integration.yml"
    }
    assert boundary_pairs == {(e0, e1), (e2, e3)}, boundary_pairs


def scenario_confidence_resolution(world: SimWorld) -> None:
    """The confidence ladder's tie-breaks (ADR 0004), one sub-case each, with
    exact resolved confidences: an explicit accept stands over a CI failure
    (CI multiplies, never overrides); an explicit reject wins over both a
    concurrent explicit accept and a CI pass; and a numbered fail→pass retry
    keeps a categorical pass with zero reliability and no recovery pair."""
    time_ns = _TIME_NS0 + 20_000_000_000
    before = world.head()

    cra = world.fire_gateway(
        "sess-sim-cra",
        _CRA_APPROVALS,
        prompt="add a manual approval queue",
        call_id="toolu-sim-cra-1",
    )
    world.fire_otlp(
        _otlp_decision_payload(
            "tool_decision_accept_user.json", "sess-sim-cra", "toolu-sim-cra-1", time_ns
        )
    )
    f1 = world.write_commit(
        "app/billing/approvals.py", _CRA_APPROVALS, "Add approval queue"
    )
    world.stamp_notes(["sess-sim-cra"])

    crb = world.fire_gateway(
        "sess-sim-crb",
        _CRB_FX,
        prompt="add fx conversion",
        call_id="toolu-sim-crb-1",
    )
    world.fire_otlp(
        _otlp_decision_payload(
            "tool_decision_accept_user.json",
            "sess-sim-crb",
            "toolu-sim-crb-1",
            time_ns + 1_000_000_000,
        )
    )
    world.fire_otlp(
        _otlp_decision_payload(
            "tool_decision_reject_user.json",
            "sess-sim-crb",
            "toolu-sim-crb-1",
            time_ns + 2_000_000_000,
        )
    )
    f2 = world.write_commit("app/billing/fx_rates.py", _CRB_FX, "Add fx conversion")
    world.stamp_notes(["sess-sim-crb"])

    crc = world.fire_gateway(
        "sess-sim-crc", _CRC_OUTBOUND, prompt="add outbound webhooks"
    )
    f3 = world.write_commit(
        "app/billing/webhooks_out.py", _CRC_OUTBOUND, "Add outbound webhooks"
    )
    world.stamp_notes(["sess-sim-crc"])
    world.fire_push(before, f3)

    # Distinct branch strings keep these lineages away from the recovery
    # scenarios; CI joins by repository lifetime and SHA, independently of branch.
    world.fire_ci(f1, workflow="reward-check", conclusion="failure", branch="reward/a")
    world.fire_ci(f2, workflow="reward-check", conclusion="success", branch="reward/b")
    retry_run = 99_000_543
    world.fire_ci(
        f3,
        workflow="reward-check",
        conclusion="failure",
        branch="reward/c",
        run_id=retry_run,
        run_attempt=1,
    )
    world.fire_ci(
        f3,
        workflow="reward-check",
        conclusion="success",
        branch="reward/c",
        run_id=retry_run,
        run_attempt=2,
    )

    attributed_completions, repository_context = world.attributed_completion_evidence()
    counts = Counter(t.inference_call_id for t in attributed_completions)
    assert all(counts[c] == 1 for c in (cra, crb, crc)), counts
    by_completion = {t.inference_call_id: t for t in attributed_completions}

    # 1) accept-overrides-CI-fail: explicit accept holds the 1.0 decision
    #    factor; the CI failure multiplies (0.7) but never overrides.
    one = resolve_confidence_breakdown(
        by_completion[cra], repository_context=repository_context
    )
    assert (one.decision_factor, one.ci_factor, one.similarity_discount) == (
        1.0,
        0.7,
        1.0,  # notes attribution: no similarity discount
    )
    assert math.isclose(one.final, 0.7)

    # 2) reject-wins: over the concurrent explicit accept AND the CI pass.
    two = resolve_confidence_breakdown(
        by_completion[crb], repository_context=repository_context
    )
    assert (two.decision_factor, two.ci_factor) == (0.0, 1.1)
    assert two.final == 0.0

    # 3) Attempt order resolves fail→pass as pass while the independent
    #    suspected-flake reliability discounts confidence to zero.
    three = resolve_confidence_breakdown(
        by_completion[crc], repository_context=repository_context
    )
    assert (three.decision_factor, three.ci_factor) == (0.6, 1.1)
    assert three.ci_reliability == 0.0
    assert three.final == 0.0

    # The suspected flake never becomes a fabricated recovery pair.
    assert world.recovery().skipped["unreliable_ci_resolution"] == 1

    for inference_call_id, sha, file, reward_min, note in (
        (cra, f1, "app/billing/approvals.py", 0.7, "explicit accept over CI fail"),
        (
            crb,
            f2,
            "app/billing/fx_rates.py",
            0.0,
            "explicit reject wins over accept + CI pass",
        ),
        (
            crc,
            f3,
            "app/billing/webhooks_out.py",
            0.0,
            "numbered retry pass, zero CI reliability",
        ),
    ):
        world.expect(
            "confidence_resolution",
            inference_call_id,
            commit=sha,
            file=file,
            attribution="git_notes",
            true_link=True,
            reward_min=reward_min,
            note=note,
        )


# ── Group 3 scenarios ──────────────────────────────────────────────────────


def scenario_dpo_retry_loop(world: SimWorld) -> None:
    """The exact-count DPO box: one same-prompt/same-model retry loop
    under explicit ``dpo_outcome`` selection yields pools {2 CI passes} ×
    {2 CI failures} = 4 possible pairs, capped at exactly 3 (per-bucket cap,
    tallied), with the self-pair guard holding by construction. A second,
    singleton bucket pins the sparsity report's histogram."""
    session = "sess-sim-dpo-1"
    prompt = "implement sequential invoice numbering"
    time_ns = _TIME_NS0 + 30_000_000_000

    v1 = world.fire_gateway(session, _DPO_NUMBERING_A, prompt=prompt)
    v2 = world.fire_gateway(session, _DPO_NUMBERING_B, prompt=prompt)
    v3 = world.fire_gateway(
        session, _DPO_NUMBERING_C, prompt=prompt, call_id="toolu-sim-dpo-3"
    )
    v4 = world.fire_gateway(
        session, _DPO_NUMBERING_D, prompt=prompt, call_id="toolu-sim-dpo-4"
    )
    world.fire_otlp(
        _otlp_decision_payload(
            "tool_decision_accept_user.json", session, "toolu-sim-dpo-3", time_ns
        )
    )
    world.fire_otlp(
        _otlp_decision_payload(
            "tool_decision_reject_user.json",
            session,
            "toolu-sim-dpo-4",
            time_ns + 1_000_000_000,
        )
    )

    before = world.head()
    s1 = world.write_commit(
        "app/billing/numbering_a.py", _DPO_NUMBERING_A, "Attempt invoice numbering"
    )
    s2 = world.write_commit(
        "app/billing/numbering_b.py", _DPO_NUMBERING_B, "Gapless invoice numbering"
    )
    s3 = world.write_commit(
        "app/billing/numbering_c.py", _DPO_NUMBERING_C, "Check-digit numbering"
    )
    s4 = world.write_commit(
        "app/billing/numbering_d.py", _DPO_NUMBERING_D, "Opaque-draw numbering"
    )
    world.fire_push(before, s4)
    # Distinct branch strings: no cross-commit lineage, so no recovery pairs.
    world.fire_ci(s1, workflow="dpo-check", conclusion="failure", branch="dpo/a")
    world.fire_ci(s2, workflow="dpo-check", conclusion="success", branch="dpo/b")
    world.fire_ci(s3, workflow="dpo-check", conclusion="failure", branch="dpo/c")
    world.fire_ci(s4, workflow="dpo-check", conclusion="success", branch="dpo/d")

    single = world.fire_gateway(
        "sess-sim-dpo-single", _DPO_PORTAL, prompt="add portal token expiry"
    )
    s5 = world.write_commit(
        "app/billing/portal_tokens.py", _DPO_PORTAL, "Add portal token expiry"
    )
    world.fire_push(s4, s5)
    world.fire_ci(s5, workflow="dpo-check", conclusion="success", branch="dpo/e")

    attributed_completions, repository_context = world.attributed_completion_evidence()
    completions = world.inference_calls_by_id()
    dpo_policy = DPOPolicy(recipe_id="dpo_outcome")
    projection = project_dpo(
        attributed_completions,
        completions,
        dpo_policy,
        repository_context=repository_context,
    )

    # THE exact-count assertion: 2 chosen × 2 rejected = 4 possible, cap 3.
    assert len(projection.rows) == 3, [
        (r.metadata.chosen_completion_id, r.metadata.rejected_completion_id)
        for r in projection.rows
    ]
    assert projection.skipped["bucket_capped"] == 1
    for row in projection.rows:
        assert row.metadata.recipe_id == "dpo_outcome"
        assert row.metadata.chosen_completion_id in {v2, v4}
        assert row.metadata.rejected_completion_id in {v1, v3}
        assert (
            row.metadata.chosen_completion_id != row.metadata.rejected_completion_id
        )  # self-pair
        assert row.metadata.source_model == "gpt-4o"
        assert row.prompt == [{"role": "user", "content": prompt}]

    # Sparsity ground truth at this point in the run: the numbering
    # bucket (4 candidates) plus four clean-CI singletons — the portal
    # completion here and three earlier attributed completions.
    sparsity = build_dpo_bucket_sparsity(
        attributed_completions,
        completions,
        dpo_policy,
        repository_context=repository_context,
    )
    assert sparsity.bucket_size_histogram == {
        "1": 4,
        "2": 0,
        "3-5": 1,
        "6-10": 0,
        "10+": 0,
    }, sparsity.bucket_size_histogram
    assert sparsity.singleton_candidates == 4
    assert sparsity.cap_hit_buckets == 1

    for inference_call_id, sha, file, note in (
        (v1, s1, "app/billing/numbering_a.py", "retry 1: CI fail → rejected pool"),
        (v2, s2, "app/billing/numbering_b.py", "retry 2: CI pass → chosen pool"),
        (
            v3,
            s3,
            "app/billing/numbering_c.py",
            "retry 3: CI fail → rejected; human accept stays separate",
        ),
        (
            v4,
            s4,
            "app/billing/numbering_d.py",
            "retry 4: CI pass → chosen; human reject stays separate",
        ),
        (
            single,
            s5,
            "app/billing/portal_tokens.py",
            "singleton bucket (sparsity report)",
        ),
    ):
        world.expect(
            "dpo_retry_loop",
            inference_call_id,
            commit=sha,
            file=file,
            attribution="jaccard",
            true_link=True,
            note=note,
        )


def scenario_split_and_bulk(world: SimWorld) -> None:
    """The split pins (and the ≥50-session acceptance line): 45 bulk abandoned
    sessions push the org past 50 distinct sessions; the deterministic
    session-keyed holdout at fraction 0.1 is pinned to its exact count (a
    pure function of sha256 over the fixed id population); attributed_completions stamp
    the same split as the primitive; a same-prompt cross-session DPO pair
    deliberately straddles the holdout and lands in eval (eval-wins); and
    the projected DPO/SFT datasets carry zero cross-split duplicate
    prompts and zero near-duplicate bucket pairs."""
    for i in range(45):
        inference_call_id = world.fire_gateway(
            f"sess-sim-bulk-{i:03d}",
            f'"""Probe sketch {i:03d} (abandoned)."""\n\nBULK_PROBE_{i:03d} = {i}\n',
            prompt=f"probe sketch {i:03d}",
        )
        world.expect(
            "bulk_sessions",
            inference_call_id,
            attribution=None,
            true_link=False,
            note="bulk abandoned probe; split-population filler",
        )

    # Session ids chosen at authoring time so the pair straddles the 0.1
    # holdout (pure-function fact, pinned):
    assert split_of("sess-sim-xsplit-a", 0.1) == "train"
    assert split_of("sess-sim-xsplit-b", 0.1) == "eval"
    prompt = "add a ledger replication cursor"
    xa = world.fire_gateway("sess-sim-xsplit-a", _XSPLIT_SYNC_A, prompt=prompt)
    xb = world.fire_gateway("sess-sim-xsplit-b", _XSPLIT_SYNC_B, prompt=prompt)
    before = world.head()
    y1 = world.write_commit(
        "app/billing/sync_a.py", _XSPLIT_SYNC_A, "Add replication cursor"
    )
    y2 = world.write_commit(
        "app/billing/sync_b.py", _XSPLIT_SYNC_B, "Checkpoint replication cursor"
    )
    world.fire_push(before, y2)
    world.fire_ci(y1, workflow="sync-check", conclusion="failure", branch="sync/a")
    world.fire_ci(y2, workflow="sync-check", conclusion="success", branch="sync/b")

    with world.store() as store:
        sessions = sorted(
            {call.session_id for call in (*store.read_inference_calls(ORG),)}
        )
    assert len(sessions) >= 50, len(sessions)
    eval_sessions = [s for s in sessions if is_eval(s, 0.1)]
    # The holdout is deterministic — over this fixed population the
    # count is a constant, not a distribution. (The pinned literal IS the
    # re-run check: a nondeterministic is_eval could not keep hitting it.)
    assert len(eval_sessions) == _PINNED_EVAL_SESSIONS, sorted(eval_sessions)

    attributed_completions, repository_context = world.attributed_completion_evidence(
        eval_fraction=0.1
    )
    assert all(t.split == split_of(t.session_id, 0.1) for t in attributed_completions)

    completions = world.inference_calls_by_id()
    projection = project_dpo(
        attributed_completions,
        completions,
        DPOPolicy(recipe_id="dpo_outcome"),
        repository_context=repository_context,
    )
    # The retry-loop bucket's 3 plus the cross-split pair.
    assert len(projection.rows) == 4, len(projection.rows)
    [xrow] = [r for r in projection.rows if r.metadata.chosen_completion_id == xb]
    assert xrow.metadata.rejected_completion_id == xa
    assert xrow.metadata.split == EVAL  # eval-wins on a straddling pair

    # No cross-split duplicate prompts, no near-duplicate buckets, in either
    # projected dataset.
    sft = project_sft(
        attributed_completions, completions, repository_context=repository_context
    )
    diagnostics = build_dataset_diagnostics(
        projection.rows,
        sft.rows,
        attributed_completions=attributed_completions,
        inference_calls=completions,
        repository_context=repository_context,
    )
    assert all(
        report.duplicate_prompt_count == 0
        for report in diagnostics.cross_split_duplicates
    ), diagnostics.cross_split_duplicates
    assert all(
        report.near_miss_pair_count == 0
        for report in diagnostics.dpo_near_duplicate_buckets
    )

    for inference_call_id, sha, file, note in (
        (xa, y1, "app/billing/sync_a.py", "train-session member of the eval-wins pair"),
        (xb, y2, "app/billing/sync_b.py", "eval-session member of the eval-wins pair"),
    ):
        world.expect(
            "split_and_bulk",
            inference_call_id,
            commit=sha,
            file=file,
            attribution="jaccard",
            true_link=True,
            note=note,
        )


# ── Group 4 scenarios: known-bug pins ──────────────────────────────────────


def scenario_gateway_identity_pins(world: SimWorld) -> None:
    """Two fixed identity bugs, each pinned at its honest behavior.

    Blob user_id: an API-key-authed Claude Code session's Anthropic
    ``metadata.user_id`` is a JSON blob. The identity resolver
    (``session_identity.py``) extracts the session uuid and condenses the
    blob to a short readable label (``device_<12 hex>``), so the stored
    InferenceCall never carries raw JSON in ``user_id``.

    Padded session id: ``NonEmptyId`` strips surrounding whitespace at
    construction, so a whitespace-PADDED session id normalizes at every
    door — the padded gateway spelling and the unpadded OTLP spelling of one
    logical session land as ONE session key, and the decision→completion
    join (call_id-keyed) attaches within that single session."""
    blob = world.fire_gateway(
        _P63_SESSION,
        _P63_ANOMALY,
        prompt="sketch a spend-spike detector",
        call_id="sim-call-63-blob",
        user_id=_P63_USER_LABEL,  # what reaches the envelope today
    )
    world.expect(
        "identity_pins_63",
        blob,
        attribution=None,
        true_link=False,
        note="API-key-authed user_id JSON blob condensed to a device label (fixed)",
    )

    padded_session = "sess-sim-pad-61\n"
    padded = world.fire_gateway(
        padded_session,
        _P61_NORMALIZER,
        prompt="sketch a postcode normalizer",
        call_id="toolu-sim-pad-61",
    )
    world.fire_otlp(
        _otlp_decision_payload(
            "tool_decision_accept_user.json",
            "sess-sim-pad-61",  # the unpadded spelling of the same session
            "toolu-sim-pad-61",
            _TIME_NS0 + 40_000_000_000,
        )
    )
    world.expect(
        "identity_pins_61",
        padded,
        attribution=None,
        true_link=False,
        note="whitespace-padded session id normalizes at construction; one session (fixed)",
    )

    with world.store() as store:
        completions = world.inference_calls_by_id()
        pad_decisions = [
            d for d in store.read_decisions(ORG) if d.call_id == "toolu-sim-pad-61"
        ]

    # Session extracted as before, and the identity now stores as a readable
    # label — no raw JSON, and stable across this device's sessions (the
    # blob's per-session uuid is nowhere in it).
    assert completions[blob].session_id == _P63_SESSION
    assert completions[blob].user_id == _P63_USER_LABEL
    assert completions[blob].user_id == "device_e3b0c44298fc"
    assert _P63_SESSION not in completions[blob].user_id

    # NonEmptyId strips at construction, so the padded gateway spelling and
    # the unpadded OTLP spelling land as ONE session key.
    assert padded_session != "sess-sim-pad-61"  # the wire really was padded
    assert completions[padded].session_id == "sess-sim-pad-61"  # stripped
    [pad_decision] = pad_decisions
    assert pad_decision.session_id == "sess-sim-pad-61"
    assert completions[padded].session_id == pad_decision.session_id
    # The call_id join attaches as before — now within one unfragmented session.
    joined = join_decisions_by_call_id(completions.values(), pad_decisions)
    assert joined[padded] == pad_decisions


def scenario_codex_collapse(world: SimWorld) -> None:
    """One Codex apply_patch call fans into per-file decision rows from
    the V4A patch arguments; a decision-only redelivery (result lost across
    a batch boundary) lands a subsumed ``file_path=""`` row as a distinct
    fact; the derivation-layer join drops the subsumed row and keeps the
    per-file pair. A full-batch redelivery collapses entirely (dedup)."""
    conversation = "01900000-0000-7000-8000-000000000034"
    call_id = "call_sim34patch"
    observed_ns = _TIME_NS0 + 41_000_000_000

    join_target = world.fire_gateway(
        conversation,
        _P34_CSV_MAPPER,
        prompt="map csv import columns",
        call_id=call_id,
    )
    world.expect(
        "codex_collapse_34",
        join_target,
        attribution=None,
        true_link=False,
        note="join target for the codex fan-out; content never lands",
    )

    world.fire_otlp(
        _codex_payload(conversation, call_id, observed_ns, arguments=_P34_ARGUMENTS)
    )

    def codex_rows() -> list:
        with world.store() as store:
            return sorted(
                (d for d in store.read_decisions(ORG) if d.session_id == conversation),
                key=lambda d: d.file_path,
            )

    fanned = codex_rows()
    assert [d.file_path for d in fanned] == ["calc.py", "util.py"]
    assert all(d.accepted and not d.explicit and d.call_id == call_id for d in fanned)

    # The redelivery shape: decision without its result → one "" row,
    # stored as a distinct fact (facts first; the collapse is derivation-side).
    world.fire_otlp(
        _codex_payload(conversation, call_id, observed_ns, decision_only=True)
    )
    with_subsumed = codex_rows()
    assert [d.file_path for d in with_subsumed] == ["", "calc.py", "util.py"]

    # A FULL redelivery collapses on the natural key — still three rows.
    world.fire_otlp(
        _codex_payload(conversation, call_id, observed_ns, arguments=_P34_ARGUMENTS)
    )
    assert [d.file_path for d in codex_rows()] == ["", "calc.py", "util.py"]

    # The derivation join keeps the per-file rows and drops the subsumed "".
    with world.store() as store:
        completions = store.read_inference_calls(ORG)
    joined = join_decisions_by_call_id(completions, with_subsumed)
    assert sorted(d.file_path for d in joined[join_target]) == ["calc.py", "util.py"]
    assert {d.session_id for d in joined[join_target]} == {conversation}
    assert (
        next(c for c in completions if c.inference_call_id == join_target).session_id
        == conversation
    )


def scenario_responses_churn(world: SimWorld) -> None:
    """A ``/v1/responses`` SLO with tool-item churn through the gateway door:
    spoken
    ``output_text`` AND function-call arguments both survive into the
    completion text, and the request history's tool items are stored
    faithfully (item-for-item)."""
    fixture = _fixture("litellm_responses_standard_logging_object.json")
    fixture["litellm_call_id"] = "sim-call-resp-79"
    inference_call_id = world.fire_gateway_payload("sess-sim-resp-79", fixture)

    with world.store() as store:
        [c] = [
            call
            for call in store.read_inference_calls(ORG)
            if call.inference_call_id == inference_call_id
        ]
    # Spoken text and tool-call arguments are both the model's output.
    assert "Confirmed. Now I'll fix" in render_scoring_text(c)
    assert "slugify" in render_scoring_text(c)
    # The request history survives item-for-item (tool items included).
    assert len(inference_input_messages(c)) == len(fixture["messages"])
    assert c.model_call_id == "sim-call-resp-79"

    world.expect(
        "responses_churn_79",
        inference_call_id,
        attribution=None,
        true_link=False,
        note="/v1/responses shape pin; content never lands",
    )


def scenario_copilot_survival_buckets(world: SimWorld) -> None:
    """Copilot graded survival at multiple delay buckets: three windows
    (0 / 5 s / 30 s), the first two sharing an identical ``timeUnixNano``
    (the distinct-bucket timestamp tie the ``COALESCE(observation_delay_ms,
    -1)`` dedup key exists for — distinct measurements must not collapse as
    fake redeliveries), then an identical-window redelivery that MUST
    collapse."""
    session = "sess-sim-cop-1"
    request_id = "req-sim-cop-0001"
    tied_ns = _TIME_NS0 + 42_000_000_000

    world.fire_otlp(
        _copilot_survival_payload(
            session, request_id, tied_ns, survival_rate=1.0, window_ms=0
        )
    )
    world.fire_otlp(
        _copilot_survival_payload(
            session, request_id, tied_ns, survival_rate=0.8, window_ms=5000
        )
    )
    world.fire_otlp(
        _copilot_survival_payload(
            session,
            request_id,
            tied_ns + 25_000_000_000,
            survival_rate=0.5,
            window_ms=30_000,
        )
    )
    # Identical-window redelivery: same bucket, same time — collapses.
    world.fire_otlp(
        _copilot_survival_payload(
            session, request_id, tied_ns, survival_rate=0.8, window_ms=5000
        )
    )

    with world.store() as store:
        rows = sorted(
            (d for d in store.read_decisions(ORG) if d.session_id == session),
            key=lambda d: d.observation_delay_ms or 0,
        )
    assert [(d.observation_delay_ms, d.edit_retention_score) for d in rows] == [
        (0, 1.0),
        (5000, 0.8),
        (30_000, 0.5),
    ], rows
    assert all(d.accepted and not d.explicit and d.call_id == request_id for d in rows)
    # The timestamp tie really is a tie — and both measurements survived it.
    assert rows[0].occurred_at == rows[1].occurred_at


# ── Group 5 scenarios: operational ─────────────────────────────────────────


def scenario_out_of_order_webhooks(world: SimWorld) -> None:
    """Out-of-order delivery and redelivery: a CI verdict lands before any
    push names its commit; the later commit range's push arrives before the
    earlier range's; then every webhook is redelivered. Facts dedup, and
    the full derivation is identical before and after the redelivery storm
    (ADR 0001: results are a function of the facts, not arrival order)."""
    o1 = world.fire_gateway(
        "sess-sim-ooo-1", _OOO_ARCHIVAL, prompt="add cold-storage archival"
    )
    o2 = world.fire_gateway(
        "sess-sim-ooo-2", _OOO_RETENTION, prompt="add retention purge schedule"
    )
    before = world.head()
    n1 = world.write_commit(
        "app/billing/archival.py", _OOO_ARCHIVAL, "Add cold-storage archival"
    )
    n2 = world.write_commit(
        "app/billing/retention.py", _OOO_RETENTION, "Add retention purge"
    )

    # CI beats every push; then the later range beats the earlier one.
    ci_run = 95_000_101
    world.fire_ci(
        n1, workflow="ooo-check", conclusion="success", branch="ooo/a", run_id=ci_run
    )
    world.fire_push(n1, n2)  # the later range, delivered first
    world.fire_push(before, n1)  # the earlier range, delivered late

    first = world.derive()
    claimed = {
        c.inference_call_id: (c.commit_sha, c.file_path)
        for c in first
        if c.inference_call_id in (o1, o2)
    }
    assert claimed == {
        o1: (n1, "app/billing/archival.py"),
        o2: (n2, "app/billing/retention.py"),
    }, claimed

    # The redelivery storm: every webhook again, every fact collapses.
    world.fire_ci(
        n1,
        workflow="ooo-check",
        conclusion="success",
        branch="ooo/a",
        run_id=ci_run,
        expect_stored=False,
    )
    world.fire_push(n1, n2, expect_stored=False)
    world.fire_push(before, n1, expect_stored=False)
    assert world.derive() == first  # byte-equal re-derivation

    for inference_call_id, sha, file, note in (
        (o1, n1, "app/billing/archival.py", "push range delivered late; CI first"),
        (o2, n2, "app/billing/retention.py", "push range delivered early"),
    ):
        world.expect(
            "out_of_order_webhooks",
            inference_call_id,
            commit=sha,
            file=file,
            attribution="jaccard",
            true_link=True,
            note=note,
        )


def scenario_quarantine_shift(world: SimWorld) -> None:
    """Mid-run quarantine: excluding a completion removes exactly its
    attribution; excluding the commit's only CI outcome shifts the attributed completion's
    resolved confidence from 0.66 to None (no reward signal at all); each
    release restores the previous derivation exactly. The quarantine token
    in provenance changes with every append."""
    q1 = world.fire_gateway(
        "sess-sim-quar", _QUAR_DISPUTES, prompt="add dispute case tracker"
    )
    before = world.head()
    k1 = world.write_commit(
        "app/billing/disputes.py", _QUAR_DISPUTES, "Add dispute tracker"
    )
    world.fire_push(before, k1)
    ci_fact = world.fire_ci(
        k1, workflow="quar-check", conclusion="success", branch="quar/a"
    )

    def q1_attributed_completions():
        rows, repository_context = world.attributed_completion_evidence()
        return [t for t in rows if t.inference_call_id == q1], repository_context

    [t], repository_context = q1_attributed_completions()
    baseline = resolve_confidence_breakdown(t, repository_context=repository_context)
    assert baseline is not None and math.isclose(baseline.final, 0.66)
    state_before = t.provenance

    with world.store() as store:
        store.quarantine_fact(
            ORG, FactTable.INFERENCE_CALLS, q1, reason="sim: mid-run quarantine"
        )
    assert world.derive_for(k1) == []  # 1 → 0, exactly
    assert q1_attributed_completions()[0] == []

    with world.store() as store:
        store.release_fact(ORG, FactTable.INFERENCE_CALLS, q1, reason="sim: release")
    [t], repository_context = q1_attributed_completions()
    restored = resolve_confidence_breakdown(t, repository_context=repository_context)
    assert restored is not None and math.isclose(restored.final, 0.66)
    assert t.provenance != state_before  # the quarantine token moved

    # Quarantining the commit's only CI outcome: the attributed completion survives but
    # carries no reward signal at all — confidence resolves to None.
    with world.store() as store:
        store.quarantine_fact(
            ORG, FactTable.CI_OUTCOMES, ci_fact, reason="sim: suspect CI run"
        )
    [t], repository_context = q1_attributed_completions()
    assert t.ci_outcomes == []
    assert (
        resolve_confidence_breakdown(t, repository_context=repository_context) is None
    )

    with world.store() as store:
        store.release_fact(ORG, FactTable.CI_OUTCOMES, ci_fact, reason="sim: release")
    [t], repository_context = q1_attributed_completions()
    final = resolve_confidence_breakdown(t, repository_context=repository_context)
    assert final is not None and math.isclose(final.final, 0.66)

    world.expect(
        "quarantine_shift",
        q1,
        commit=k1,
        file="app/billing/disputes.py",
        attribution="jaccard",
        true_link=True,
        reward_min=0.66,
        note="quarantined and released mid-run; ends visible",
    )


def scenario_clock_skew(world: SimWorld) -> None:
    """Clock skew: git commit clocks land six years in the past and a year
    in the future while capture times are ordinary. Attribution windows
    anchor on the push/completion ``captured_at`` FACTS, never on commit
    timestamps — both completions attribute normally."""
    s1 = world.fire_gateway(
        "sess-sim-skew-1", _SKEW_CALENDAR, prompt="add billing calendar periods"
    )
    s2 = world.fire_gateway("sess-sim-skew-2", _SKEW_TERMS, prompt="add payment terms")
    before = world.head()
    saved_clock = world.repo.clock
    world.repo.clock = datetime(2020, 1, 1, 12, 0, tzinfo=UTC)
    k1 = world.write_commit(
        "app/billing/calendar.py", _SKEW_CALENDAR, "Add billing calendar"
    )
    world.repo.clock = datetime(2027, 6, 1, 12, 0, tzinfo=UTC)
    k2 = world.write_commit("app/billing/terms.py", _SKEW_TERMS, "Add payment terms")
    world.repo.clock = saved_clock
    world.fire_push(before, k2)

    matches = {
        c.inference_call_id: (c.commit_sha, c.file_path)
        for c in world.derive_for(k2)
        if c.inference_call_id in (s1, s2)
    }
    assert matches == {
        s1: (k1, "app/billing/calendar.py"),
        s2: (k2, "app/billing/terms.py"),
    }, matches

    for inference_call_id, sha, file, note in (
        (s1, k1, "app/billing/calendar.py", "commit clock six years in the past"),
        (s2, k2, "app/billing/terms.py", "commit clock a year in the future"),
    ):
        world.expect(
            "clock_skew",
            inference_call_id,
            commit=sha,
            file=file,
            attribution="jaccard",
            true_link=True,
            note=note,
        )


# ── Group 6: RLVR round-trip ───────────────────────────────────────────────

# Pinned honest task-row count over the full catalog — every rollout with an
# attributed commit, a pass/fail CI outcome, and a resolvable gold patch
# (recovery/confidence/DPO/xsplit/quarantine/rlvr sessions qualify; sessions
# without CI on their attributed commits are skipped-and-counted). Update
# alongside scenarios that add attributed-and-CI-labeled sessions.
_PINNED_TASK_ROWS = 11


def _round_trip_git(*args: str) -> None:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )
    assert result.returncode == 0, result.stderr


def _pytest_rc(repo: Path) -> int:
    # PYTHONDONTWRITEBYTECODE: stale __pycache__ from a same-size checkout
    # must never mask the patch (the bytecode-cache trap).
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=repo,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    ).returncode


def scenario_rlvr_round_trip(world: SimWorld) -> None:
    """Group 6: ``export_rlvr`` over the sim DB, then the executable
    round-trip — check out the exported ``base_commit`` from the mirror, run
    ``verification_command``, assert red; apply the exported ``reference_patch``, assert
    green. Plain subprocess pytest only (the ``docs/exports/rlvr-export.md``
    posture: no Verifiers / NeMo Gym / OpenEnv imports anywhere).

    The story: a human lands a buggy rounding module with its failing test
    (CI red, unstamped); the agent session ships the fix (stamped, CI
    green). Only the fix commit attributes to the session, so the task's
    ``base_commit`` is the red buggy commit — the SWE-bench shape."""
    session = "sess-sim-rlvr"
    fix = world.fire_gateway(
        session, _RLVR_ROUNDING_GOLD, prompt="fix cash rounding to round half up"
    )
    before = world.head()
    world.repo.write("app/billing/rounding.py", _RLVR_ROUNDING_BUGGY)
    world.repo.write("tests/test_rounding.py", _RLVR_ROUNDING_TEST)
    r1 = world.repo.commit("Add cash rounding with tests")
    r2 = world.write_commit(
        "app/billing/rounding.py", _RLVR_ROUNDING_GOLD, "Fix cash rounding"
    )
    world.stamp_notes([session])
    world.fire_push(before, r2)
    world.fire_ci(r1, workflow="rlvr-check", conclusion="failure", branch="rlvr/a")
    world.fire_ci(r2, workflow="rlvr-check", conclusion="success", branch="rlvr/a")

    # verification_command is operator configuration, never inference.
    commands_file = world.workdir / "verifier_commands.toml"
    commands_file.write_text(
        f'[repos."{REPO_FULL}"]\nverification_command = "python -m pytest -q"\n',
        encoding="utf-8",
    )
    os.environ["SEDIMENT_VERIFIER_COMMANDS_FILE"] = str(commands_file)
    try:
        with world.store() as store:
            summary = export_rlvr(
                store,
                MirrorManager(world.settings.mirror_path),
                ORG,
                world.workdir / "rlvr",
                target="sediment",
            )
    finally:
        os.environ.pop("SEDIMENT_VERIFIER_COMMANDS_FILE", None)

    rows = [
        json.loads(line)
        for path in sorted((world.workdir / "rlvr").glob("tasks.*.jsonl"))
        for line in path.read_text().splitlines()
    ]
    assert summary["task_rows"] == len(rows)
    assert len(rows) == _PINNED_TASK_ROWS, len(rows)
    assert summary["environment_manifest"]  # taskset sidecar written

    [task] = [r for r in rows if r["instance_id"].startswith(f"{ORG}-{session}-")]
    assert task["repo"] == REPO_FULL
    assert task["base_commit"] == r1  # the red commit, not its parent
    assert task["verification"]["verification_command"] == "python -m pytest -q"
    assert task["recipe_id"] == "rlvr_ci"
    assert task["reward_source"] == "resolved_ci_pass"
    assert task["verifier_results"][0]["result"] == "passed"
    assert task["ci_resolution"]["verdict"] == "passed"
    assert task["attribution_source"] == "git_notes"
    assert "round_to_nickel" in task["reference_patch"]

    # THE round-trip, from nothing but the exported row and the mirror.
    with world.store() as store, store.read_snapshot() as snapshot:
        repository_context = read_repository_context(snapshot, ORG)
        commit_key = repository_context.commit_key(
            ORG,
            task["repo"],
            task["base_commit"],
            repository_identity=RepositoryIdentity(**task["repository_identity"]),
        )
    assert commit_key is not None, "exported repository identity must resolve"
    mirror = MirrorManager(world.settings.mirror_path).open_repository(
        commit_key.repository
    )
    assert mirror is not None, "captured repository mirror must exist"
    checkout = world.workdir / "rlvr" / "checkout"
    _round_trip_git("clone", "--quiet", str(mirror.path), str(checkout))
    _round_trip_git("-C", str(checkout), "checkout", "-q", task["base_commit"])
    assert _pytest_rc(checkout) != 0, "base commit must be red"
    patch_file = world.workdir / "rlvr" / "gold.patch"
    patch_file.write_text(task["reference_patch"], encoding="utf-8")
    _round_trip_git("-C", str(checkout), "apply", str(patch_file))
    assert _pytest_rc(checkout) == 0, "gold patch must flip the tests green"

    world.expect(
        "rlvr_round_trip",
        fix,
        commit=r2,
        file="app/billing/rounding.py",
        attribution="git_notes",
        true_link=True,
        note="exported task's gold patch; round-trip verified red→green",
    )


SCENARIOS = (
    scenario_cross_prompt_control,
    scenario_hand_edit_gradient,
    scenario_false_positive_bait,
    scenario_true_negatives,
    scenario_rebase_squash_survival,
    scenario_edit_observation,
    scenario_ci_lineage_recovery,
    scenario_recovery_cap_boundary,
    scenario_confidence_resolution,
    scenario_dpo_retry_loop,
    scenario_split_and_bulk,
    # Group 4/5 run AFTER the split scenario: their sessions must not
    # retro-shift the pinned holdout count.
    scenario_gateway_identity_pins,
    scenario_codex_collapse,
    scenario_responses_churn,
    scenario_copilot_survival_buckets,
    scenario_out_of_order_webhooks,
    scenario_quarantine_shift,
    scenario_clock_skew,
    scenario_rlvr_round_trip,
)

# Exact fact totals over every scenario — update alongside scenarios.
EXPECTED_TOTALS = {
    "inference_calls": 76,
    "decisions": 15,
    "edit_observations": 2,
    "pushes": 16,
    "ci_outcomes": 30,
}


@dataclass(frozen=True)
class SimRun:
    """Everything a consumer (tests, precision_report) needs after the world
    is torn down: plain paths, no live settings patch."""

    workdir: Path
    database_url: str
    mirror_path: str
    manifest_path: Path
    rows: list[dict[str, Any]]
    repo_manifest: dict[str, Any]


def run_all(workdir: Path, database_url: str | None = None) -> SimRun:
    """Generate the repo, run every Group 1 scenario, assert exact fact
    totals and the global negative-row invariant, write the manifest."""
    resolved_database_url = database_url or os.environ.get("SEDIMENT_DATABASE_URL")
    if not resolved_database_url:
        raise ValueError("set SEDIMENT_DATABASE_URL for the simulation")
    with SimWorld(workdir, resolved_database_url) as world:
        for scenario in SCENARIOS:
            scenario(world)

        with world.store() as store:
            observed = {
                "inference_calls": len(store.read_inference_calls(ORG)),
                "decisions": len(store.read_decisions(ORG)),
                "edit_observations": len(store.read_edit_observations(ORG)),
                "pushes": len(store.read_pushes(ORG)),
                "ci_outcomes": len(store.read_ci_outcomes(ORG)),
            }
        assert observed == EXPECTED_TOTALS, observed

        # Global check over ALL pushes: negative rows stay unclaimed even by
        # scenarios that ran later than the row's own.
        attributions = world.derive()
        claimed = {c.inference_call_id for c in attributions}
        for row in world.manifest_rows:
            if row["expected_attribution"] is None:
                assert row["inference_call_id"] not in claimed, row

        manifest_path = world.workdir / "ground_truth_manifest.jsonl"
        manifest_path.write_text(
            "".join(json.dumps(row) + "\n" for row in world.manifest_rows),
            encoding="utf-8",
        )
        return SimRun(
            workdir=world.workdir,
            database_url=resolved_database_url,
            mirror_path=world.settings.mirror_path,
            manifest_path=manifest_path,
            rows=list(world.manifest_rows),
            repo_manifest=world.repo_manifest,
        )


if __name__ == "__main__":
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description="Run the Tier A sim scenarios")
    parser.add_argument(
        "--workdir", help="working directory (default: a fresh temp dir)"
    )
    args = parser.parse_args()
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp("sim"))
    run = run_all(workdir)
    print(f"scenarios green: {len(run.rows)} manifest rows in {run.manifest_path}")
