# SPDX-License-Identifier: AGPL-3.0-or-later
"""simcorp-billing generator — the deterministic sim repository.

Builds a small fictional Python billing service with ~165 commits of seeded
backstory, then the content the pipeline's features are designed backward
from: near-duplicate CRUD modules (jaccard false-positive bait), a vendored
directory, a rename-heavy subtree, a unicode-identifier file, mixed line
endings, one binary asset, three CI workflow files, and five SWE-bench-style
executable commit pairs (base commit: ``pytest`` red → gold patch: green).

Determinism is the contract: fixed seed, injected timestamps (never
wall-clock), fixed author/committer identity, per-repo git config pinned
(``commit.gpgsign=false``, ``core.autocrlf=false``) so host config cannot
leak in. Regenerating with the same seed yields byte-identical history —
same commit SHAs — which the sim harness and its tests rely on.

Usage:
    python sim/gen_repo.py --out /tmp/sim [--seed 20260105]

Emits ``<out>/simcorp-billing`` (the repo) and ``<out>/sim_repo_manifest.json``
(head SHA, commit count, executable pairs with base/gold SHAs and their
``verification_command``, notable paths). The manifest is generator output, not
pipeline ground truth — scenario ground truth lives in the Tier A manifest.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

SEED_DEFAULT = 20260105
REPO_NAME = "simcorp-billing"
AUTHOR = "Sim Developer <dev@simcorp.example>"
AUTHOR_NAME, AUTHOR_EMAIL = AUTHOR.removesuffix(">").split(" <")
_TS0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)

_ENTITIES = ("customer", "invoice", "subscription", "payment")

# Near-identical per entity by construction: this is the jaccard
# false-positive cases — similarity must discriminate
# between four token streams that differ only in the entity name.
_CRUD_TEMPLATE = '''"""{title} CRUD routes for the simcorp billing API."""

from app.models import NotFoundError, {title}

_DB: dict[int, {title}] = {{}}
_NEXT_ID = 1


def create_{name}(payload: dict) -> {title}:
    global _NEXT_ID
    record = {title}(id=_NEXT_ID, **payload)
    _DB[_NEXT_ID] = record
    _NEXT_ID += 1
    return record


def get_{name}({name}_id: int) -> {title}:
    if {name}_id not in _DB:
        raise NotFoundError("{name} not found")
    return _DB[{name}_id]


def list_{name}s(limit: int = 50, offset: int = 0) -> list[{title}]:
    records = sorted(_DB.values(), key=lambda r: r.id)
    return records[offset : offset + limit]


def update_{name}({name}_id: int, payload: dict) -> {title}:
    record = get_{name}({name}_id)
    for key, value in payload.items():
        setattr(record, key, value)
    return record


def delete_{name}({name}_id: int) -> None:
    get_{name}({name}_id)
    del _DB[{name}_id]
'''

_MODELS = '''"""Domain records for the simcorp billing API."""

from dataclasses import dataclass, field


class NotFoundError(Exception):
    pass


@dataclass
class Customer:
    id: int
    name: str = ""
    email: str = ""
    country: str = "DE"


@dataclass
class Invoice:
    id: int
    customer_id: int = 0
    lines: list = field(default_factory=list)
    status: str = "draft"


@dataclass
class Subscription:
    id: int
    customer_id: int = 0
    plan: str = "starter"
    seats: int = 1


@dataclass
class Payment:
    id: int
    invoice_id: int = 0
    amount_cents: int = 0
    method: str = "sepa"
'''

_APP_MAIN = '''"""API wiring. Route modules are imported lazily by the framework
layer in production; nothing under tests/ imports this module."""

ROUTES = {
    "/customers": "app.routes.customers",
    "/invoices": "app.routes.invoices",
    "/subscriptions": "app.routes.subscriptions",
    "/payments": "app.routes.payments",
}
'''

# Unicode identifiers (NFKC-valid Python); ASCII filename on purpose so
# macOS NFD filename normalization cannot break byte-identical trees.
_RATES = '''"""VAT rates. Identifiers exercise unicode handling downstream."""

MWST_SÄTZE = {"DE": 1900, "AT": 2000, "FR": 2000}
π_TOLERANZ = 0.005


def satz_für(land: str) -> int:
    return MWST_SÄTZE.get(land, 1900)
'''

_VENDOR_NOTE = "Vendored microjson 0.3 — DO NOT EDIT. Upstream: example.org/microjson\n"
_VENDOR_FILES = {
    "vendor/microjson/__init__.py": '"""Vendored microjson (see NOTE)."""\n\nfrom .decoder import loads\nfrom .encoder import dumps\n\n__all__ = ["loads", "dumps"]\n',
    "vendor/microjson/decoder.py": '"""Tiny strict JSON subset decoder (vendored)."""\n\nimport json\n\n\ndef loads(text: str):\n    return json.loads(text)\n',
    "vendor/microjson/encoder.py": '"""Tiny strict JSON subset encoder (vendored)."""\n\nimport json\n\n\ndef dumps(value) -> str:\n    return json.dumps(value, sort_keys=True)\n',
    "vendor/microjson/NOTE": _VENDOR_NOTE,
}

_LEDGER_FILES = {
    "ledger/journal.py": '"""Double-entry journal lines."""\n\n\ndef post(entries: list[tuple[str, int]]) -> int:\n    return sum(cents for _, cents in entries)\n',
    "ledger/accounts.py": '"""Chart of accounts."""\n\nACCOUNTS = {"revenue": 4000, "vat_payable": 3806, "receivables": 1200}\n',
    "ledger/export.py": '"""Ledger CSV export."""\n\n\ndef header() -> str:\n    return "account;debit;credit"\n',
}

# A minimal, valid, fixed-byte 1x1 PNG (binary asset).
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049"
    "454e44ae426082"
)

_WORKFLOWS = {
    ".github/workflows/test.yml": (
        "name: test\non:\n  push:\n  pull_request:\njobs:\n  test:\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
        "      - run: python -m pip install pytest\n      - run: python -m pytest -q\n"
    ),
    ".github/workflows/lint.yml": (
        "name: lint\non:\n  push:\njobs:\n  lint:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@v4\n"
        "      - run: python -m compileall -q app ledger tests\n"
    ),
    ".github/workflows/integration.yml": (
        "name: integration\non:\n  push:\njobs:\n  integration:\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
        "      - run: python -m pip install pytest\n"
        "      - run: python -m pytest -q tests -k integration\n"
    ),
}

# ── executable subset: five buggy→gold pairs, tests pin the gold behavior ──

_PAIRS: list[dict[str, str]] = [
    {
        "module": "app/billing/proration.py",
        "test": "tests/test_proration.py",
        "buggy": '''"""Prorated charge for a partial billing period."""


def prorate(amount_cents: int, days_used: int, days_in_period: int) -> int:
    if days_in_period <= 0:
        raise ValueError("days_in_period must be positive")
    return days_used // days_in_period * amount_cents
''',
        "gold": '''"""Prorated charge for a partial billing period."""


def prorate(amount_cents: int, days_used: int, days_in_period: int) -> int:
    # fixed: multiply before integer division so partial periods prorate
    if days_in_period <= 0:
        raise ValueError("days_in_period must be positive")
    return amount_cents * days_used // days_in_period
''',
        "test_body": """from app.billing.proration import prorate


def test_partial_period_is_proportional():
    assert prorate(3000, 10, 30) == 1000


def test_full_period_charges_everything():
    assert prorate(3000, 30, 30) == 3000
""",
    },
    {
        "module": "app/billing/tax.py",
        "test": "tests/test_tax.py",
        "buggy": '''"""VAT application in basis points."""


def add_vat(amount_cents: int, rate_bp: int) -> int:
    return amount_cents + int(amount_cents * rate_bp / 10_000)
''',
        "gold": '''"""VAT application in basis points."""


def add_vat(amount_cents: int, rate_bp: int) -> int:
    vat, remainder = divmod(amount_cents * rate_bp, 10_000)
    return amount_cents + vat + (1 if remainder * 2 >= 10_000 else 0)
''',
        "test_body": """from app.billing.tax import add_vat


def test_vat_rounds_half_up_not_truncates():
    assert add_vat(999, 1900) == 1189  # 189.81 rounds to 190, not 189


def test_vat_exact():
    assert add_vat(1000, 1900) == 1190
""",
    },
    {
        "module": "app/billing/totals.py",
        "test": "tests/test_totals.py",
        "buggy": '''"""Invoice totals over line amounts in cents."""


def invoice_total(line_amounts_cents: list[int]) -> int:
    return sum(cents for cents in line_amounts_cents if cents > 0)
''',
        "gold": '''"""Invoice totals over line amounts in cents."""


def invoice_total(line_amounts_cents: list[int]) -> int:
    return sum(line_amounts_cents)
''',
        "test_body": """from app.billing.totals import invoice_total


def test_credit_lines_reduce_the_total():
    assert invoice_total([5000, -1500, 300]) == 3800


def test_all_positive():
    assert invoice_total([100, 200]) == 300
""",
    },
    {
        "module": "app/billing/currency.py",
        "test": "tests/test_currency.py",
        "buggy": '''"""Decimal-string amounts to integer minor units."""


def to_minor_units(amount: str) -> int:
    return int(float(amount) * 100)
''',
        "gold": '''"""Decimal-string amounts to integer minor units."""

from decimal import Decimal


def to_minor_units(amount: str) -> int:
    return int(Decimal(amount) * 100)
''',
        "test_body": """from app.billing.currency import to_minor_units


def test_float_hostile_amount():
    assert to_minor_units("19.99") == 1999


def test_whole_amount():
    assert to_minor_units("20") == 2000
""",
    },
    {
        "module": "app/billing/discounts.py",
        "test": "tests/test_discounts.py",
        "buggy": '''"""Percentage discounts on amounts in cents."""


def apply_discount(amount_cents: int, percent: int) -> int:
    return amount_cents - percent
''',
        "gold": '''"""Percentage discounts on amounts in cents."""


def apply_discount(amount_cents: int, percent: int) -> int:
    if not 0 <= percent <= 100:
        raise ValueError("percent out of range")
    return amount_cents - amount_cents * percent // 100
''',
        "test_body": """from app.billing.discounts import apply_discount


def test_percentage_not_absolute():
    assert apply_discount(2000, 10) == 1800


def test_zero_discount():
    assert apply_discount(2000, 0) == 2000
""",
    },
]

_BACKSTORY_WORDS = (
    "reconcile dunning ledger payout chargeback settlement remittance "
    "proforma quote credit rebate arrears retainer accrual"
).split()


class _Repo:
    """Deterministic commit engine: injected clock, fixed identity."""

    def __init__(self, path: Path, rng: random.Random) -> None:
        self.path = path
        self.rng = rng
        self.clock = _TS0
        self.count = 0

    def git(self, *args: str, stdin: str | None = None) -> str:
        stamp = self.clock.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env = {
            "GIT_AUTHOR_NAME": AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
            # Isolate from host config entirely (gpgsign, hooks, autocrlf).
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(self.path),
            "PATH": "/usr/bin:/bin",
        }
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            input=stdin,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {args[0]}: {result.stderr.strip()}")
        return result.stdout.strip()

    def write(self, rel: str, content: str | bytes) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.clock += timedelta(minutes=self.rng.randint(7, 173))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        self.count += 1
        return self.git("rev-parse", "HEAD")


def _scaffold(repo: _Repo) -> None:
    repo.git("init", "-q", "-b", "main")
    repo.git("config", "commit.gpgsign", "false")
    repo.git("config", "core.autocrlf", "false")
    # No background maintenance, ever: modern git (2.5x) spawns a DETACHED
    # `git maintenance run --auto` after commit/merge/rebase, and a detached
    # repack can race the harness's file:// mirror fetch — upload-pack then
    # dies mid-pack with "unable to read <object>" and the scenario sees an
    # empty mirror (observed as a CI flake on the very first fetch). Local
    # config beats any runner-image global/system config, so this closes
    # every auto-maintenance trigger, current or future. Object hashing is
    # unaffected: generated history stays byte-identical.
    repo.git("config", "gc.auto", "0")
    repo.git("config", "gc.autoDetach", "false")
    repo.git("config", "maintenance.auto", "false")

    repo.write("README.md", "# simcorp-billing\n\nFictional billing service.\n")
    repo.write("pytest.ini", "[pytest]\ntestpaths = tests\n")
    repo.write(".gitignore", "__pycache__/\n*.pyc\n.pytest_cache/\n")
    repo.commit("Bootstrap simcorp-billing")

    repo.write("app/__init__.py", "")
    repo.write("app/models.py", _MODELS)
    repo.commit("Add domain records")

    repo.write("app/main.py", _APP_MAIN)
    repo.write("app/routes/__init__.py", "")
    repo.commit("Wire route table")

    for entity in _ENTITIES:
        repo.write(
            f"app/routes/{entity}s.py",
            _CRUD_TEMPLATE.format(name=entity, title=entity.title()),
        )
        repo.commit(f"Add {entity} CRUD routes")

    for rel, content in _VENDOR_FILES.items():
        repo.write(rel, content)
    repo.commit("Vendor microjson")

    for rel, content in _LEDGER_FILES.items():
        repo.write(rel, content)
    repo.commit("Add ledger subtree")

    repo.write("app/billing/__init__.py", "")
    repo.write("app/billing/rates.py", _RATES)
    repo.commit("Add VAT rate table")

    # Mixed line endings: whole-file CRLF plus a mixed-EOL file.
    repo.write("docs/CHANGES.txt", b"initial import\r\nvendored microjson\r\n")
    repo.write("docs/OPS.txt", b"runbook line one\nwindows note\r\nunix line\n")
    repo.commit("Operational notes")

    repo.write("assets/logo.png", _PNG)
    repo.commit("Add logo asset")

    for rel, content in _WORKFLOWS.items():
        repo.write(rel, content)
    repo.commit("Add CI workflows")

    repo.write("tests/__init__.py", "")
    repo.write(
        "tests/test_models.py",
        "from app.models import Customer\n\n\n"
        "def test_customer_defaults():\n"
        '    assert Customer(id=1).country == "DE"\n',
    )
    repo.commit("Test scaffold")


def _backstory(repo: _Repo, commits: int) -> None:
    """Seeded filler history: small, plausible, deterministic edits."""
    for i in range(commits):
        word = repo.rng.choice(_BACKSTORY_WORDS)
        action = repo.rng.randrange(4)
        if action == 0:
            entity = repo.rng.choice(_ENTITIES)
            rel = f"app/routes/{entity}s.py"
            existing = (repo.path / rel).read_text(encoding="utf-8")
            repo.write(
                rel,
                existing
                + f"\n\ndef audit_{word}_{i}() -> str:\n"
                + f'    return "{entity}:{word}:{i}"\n',
            )
            message = f"Add {word} audit hook to {entity} routes"
        elif action == 1:
            repo.write(
                f"docs/note_{i:03d}.md",
                f"# {word}\n\nOperational note {i} about {word} handling.\n",
            )
            message = f"Document {word} handling"
        elif action == 2:
            repo.write("app/config.py", f'VERSION = "0.{i // 10}.{i % 10}"\n')
            message = f"Bump internal version marker ({i})"
        else:
            changes = (repo.path / "docs/CHANGES.txt").read_bytes()
            repo.write("docs/CHANGES.txt", changes + f"{word} pass {i}\r\n".encode())
            message = f"Record {word} pass in changelog"
        repo.commit(message)


def _rename_subtree(repo: _Repo) -> None:
    repo.git("mv", "ledger", "accounting")
    repo.commit("Rename ledger/ to accounting/")
    repo.write(
        "accounting/__init__.py",
        '"""Accounting package (renamed from ledger/)."""\n',
    )
    repo.commit("Package marker for accounting/")


def _executable_pairs(repo: _Repo) -> list[dict[str, str]]:
    pairs = []
    for spec in _PAIRS:
        repo.write(spec["module"], spec["buggy"])
        repo.write(spec["test"], spec["test_body"])
        base = repo.commit(f"Add {Path(spec['module']).stem} billing rule + tests")
        repo.write(spec["module"], spec["gold"])
        gold = repo.commit(f"Fix {Path(spec['module']).stem} computation")
        pairs.append(
            {
                "module": spec["module"],
                "test": spec["test"],
                "base_sha": base,
                "gold_sha": gold,
                "verification_command": "python -m pytest -q",
            }
        )
    return pairs


def generate(out: Path, seed: int = SEED_DEFAULT) -> dict:
    """Generate the repo and return the manifest (also written to disk)."""
    repo_path = out / REPO_NAME
    if repo_path.exists():
        raise SystemExit(f"refusing to overwrite existing {repo_path}")
    repo_path.mkdir(parents=True)
    repo = _Repo(repo_path, random.Random(seed))

    _scaffold(repo)
    _backstory(repo, commits=70)
    _rename_subtree(repo)
    _backstory(repo, commits=60)
    pairs = _executable_pairs(repo)
    repo.write(
        "README.md", "# simcorp-billing\n\nFictional billing service.\nStable.\n"
    )
    repo.commit("Declare service stable")

    manifest = {
        "repo": REPO_NAME,
        "seed": seed,
        "head": repo.git("rev-parse", "HEAD"),
        "commits": repo.count,
        "default_branch": "main",
        "executable_pairs": pairs,
        "renamed_subtree": {"from": "ledger/", "to": "accounting/"},
        "jaccard_bait": [f"app/routes/{e}s.py" for e in _ENTITIES],
        "vendored": "vendor/microjson/",
        "unicode_identifiers": "app/billing/rates.py",
        "mixed_line_endings": ["docs/CHANGES.txt", "docs/OPS.txt"],
        "binary_asset": "assets/logo.png",
        "workflows": sorted(_WORKFLOWS),
    }
    (out / "sim_repo_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate simcorp-billing")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--seed", type=int, default=SEED_DEFAULT)
    args = parser.parse_args(argv)
    manifest = generate(Path(args.out), args.seed)
    print(
        f"generated {manifest['repo']}: {manifest['commits']} commits, "
        f"head {manifest['head'][:12]}, "
        f"{len(manifest['executable_pairs'])} executable pairs"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
