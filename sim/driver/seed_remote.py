# SPDX-License-Identifier: AGPL-3.0-or-later
"""Driver seeder: put simcorp-billing on a remote and hook a working clone.

The scenarios fire wire-shaped traffic at an in-process ``TestClient``. The driver runs
**live agents** against a real remote and a real deployment, so the corpus has
to exist somewhere both can reach. This script builds it:

1. ``gen_repo.generate()`` into the workdir — byte-identical every run, same
   SHAs, so the remote is recreatable rather than precious.
2. Push it to ``--remote``.
3. Clone the remote back into ``<workdir>/clone`` — the tree the agents edit.
   Agents must work in a *clone*, not the generated original: the push the
   pipeline observes has to travel the same path a developer's would.
4. Install the attribution stamper's git hooks in that clone, so agent commits
   carry notes attribution instead of falling back to jaccard.

Usage:
    python sim/driver/seed_remote.py --workdir /tmp/sim-driver \\
        --remote git@github.com:simcorp/simcorp-billing-sim.git

Refuses a remote that already has commits unless ``--force-push`` is given:
recreating the corpus is routine, and doing it to the wrong URL by a typo is
not recoverable from here.

Machine-level agent hook entries (``~/.claude/settings.json``,
``~/.codex/hooks.json``) are deliberately NOT written by default — those are
the operator's files, shared with every other repo on the host. Pass
``--with-agent-hooks`` to opt in, or verify an existing install with
``sediment doctor <clone>``.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIM = HERE.parent
REPO_ROOT = HERE.parents[1]
STAMPER = REPO_ROOT / "scripts" / "sediment_attribution.py"
CLONE_DIRNAME = "clone"


def _load_sibling(name: str, directory: Path = SIM):
    """Import a path-only sim module (``sim/`` is not a package).

    Same mechanism as ``scenarios.py::_load_sibling``, including the
    register-before-exec ordering that dataclass annotation resolution needs.
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gen_repo = _load_sibling("gen_repo")


def git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` against the host's real config.

    ``cwd`` is positional and required because this module can run
    ``push --force``. Every call must explicitly target the generated
    repository, independent of the operator's working directory.

    Unlike ``gen_repo._Repo.git`` this deliberately does NOT scrub the
    environment: pushing to a private remote needs the operator's ssh agent,
    credential helper and known_hosts. Generation stays hermetic; transport
    cannot be.
    """
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def remote_is_empty(remote: str) -> bool:
    """True when the remote has no refs. A remote we cannot reach is not
    'empty' — raise rather than let --force-push decide against a typo."""
    result = subprocess.run(
        ["git", "ls-remote", "--heads", remote],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot reach {remote}: {result.stderr.strip()}")
    return not result.stdout.strip()


def install_stamper(clone: Path, *, agent_hooks: bool) -> str:
    """Install the stamper into ``clone``; returns the installer's report.

    Runs the real installer rather than writing hooks here — a second
    implementation of the hook block is exactly how a fleet drifts.
    """
    argv = [sys.executable, str(STAMPER), "install", str(clone)]
    if not agent_hooks:
        argv.append("--no-agents")
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"stamper install failed: {result.stderr.strip()}")
    return result.stdout.strip()


def seed(
    workdir: Path,
    remote: str,
    *,
    seed_value: int = gen_repo.SEED_DEFAULT,
    force_push: bool = False,
    agent_hooks: bool = False,
) -> dict:
    """Generate, push, clone, hook. Returns the generator manifest plus the
    clone path — the handle every later driver step needs."""
    if not remote_is_empty(remote) and not force_push:
        raise SystemExit(
            f"refusing to seed {remote}: it already has branches. Recreating "
            "the corpus is fine (generation is byte-identical) — re-run with "
            "--force-push once you have checked the URL."
        )
    manifest = gen_repo.generate(workdir, seed_value)
    source = workdir / gen_repo.REPO_NAME

    branch = manifest["default_branch"]
    git(source, "remote", "add", "origin", remote)
    # --force even on a fresh remote: with --force-push the operator has
    # already accepted the overwrite, and a partially-seeded remote (an
    # interrupted earlier run) would otherwise reject non-fast-forward. The
    # explicit source ref, not HEAD, for the same reason cwd is required —
    # this line must be unambiguous about what it sends where.
    git(source, "push", "--force", "origin", f"refs/heads/{branch}:refs/heads/{branch}")

    clone = workdir / CLONE_DIRNAME
    # --branch explicitly, never the remote's default HEAD. A bare remote
    # created with `git init --bare` still has HEAD on `master` after we push
    # `main`, so a plain clone lands on a dangling HEAD and every later
    # `rev-parse HEAD` fails — which is exactly how the first real run of
    # this script died.
    git(workdir, "clone", "--quiet", "--branch", branch, remote, str(clone))
    if not git(clone, "rev-parse", "--verify", "HEAD"):
        raise SystemExit(f"clone of {remote} has no HEAD — the push did not land")
    report = install_stamper(clone, agent_hooks=agent_hooks)
    return {**manifest, "clone": str(clone), "remote": remote, "stamper": report}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the driver's sim remote")
    parser.add_argument("--workdir", required=True, help="empty directory to build in")
    parser.add_argument("--remote", required=True, help="git URL of the sim remote")
    parser.add_argument("--seed", type=int, default=gen_repo.SEED_DEFAULT)
    parser.add_argument(
        "--force-push",
        action="store_true",
        help="overwrite a remote that already has branches",
    )
    parser.add_argument(
        "--with-agent-hooks",
        action="store_true",
        help="also write the user-level Claude Code / Codex hook entries "
        "(touches the operator's ~/.claude and ~/.codex)",
    )
    args = parser.parse_args(argv)

    result = seed(
        Path(args.workdir),
        args.remote,
        seed_value=args.seed,
        force_push=args.force_push,
        agent_hooks=args.with_agent_hooks,
    )
    print(
        f"seeded {args.remote} at {result['head'][:12]} ({result['commits']} commits)"
    )
    print(f"working clone: {result['clone']}")
    print(f"stamper: {result['stamper']}")
    print(f"verify the clone before the first run: sediment doctor {result['clone']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
