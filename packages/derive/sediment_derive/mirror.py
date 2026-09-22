# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Git-protocol diff extraction via a local bare mirror per (org, repo).

One bare repository per pushed repo lives under a base directory, created on
first sight and refreshed with ``git fetch`` on every push. Diffs are
extracted locally with ``git diff-tree``, so the diff path speaks plain git
protocol — any forge, no REST rate limits — and the mirror carries
``refs/notes/sediment`` (notes attribution) and ``refs/pull/*/head`` (squash
aliasing) for the attribution derivation.

Auth is deployment config, not code: an SSH deploy key or a git credential
helper for the mirror's remote. Subprocess ``git`` only — no cloud SDKs
outside ``packages/export``.

Upgrade paths deliberately not built yet:
  - Partial clone (``--filter=blob:none``) if first-clone cost on very large
    repos hurts.
  - The per-repo lock is a same-host file lock — single-node posture by
    design, matching the fact-store contract.
"""

from __future__ import annotations

import fcntl
import ipaddress
import logging
import os
import shutil
import socket
import subprocess
import threading
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from sediment_core import CommitSha, FactTable, ForgeProvider, Push, normalize_org_id

from .repository_identity import (
    IdentifiedRepositoryKey,
    LegacyRepositoryKey,
    RepositoryContext,
    RepositoryIdentity,
    RepositoryKey,
    repository_sort_key,
)

logger = logging.getLogger("sediment.derive.mirror")

# What every mirror fetches, unconditionally — the notes and PR-head refs are
# what the attribution derivation consumes; fetching them is cheap and keeps
# the flag surface there. The notes refspec is a glob on purpose: an exact
# refspec for a ref the remote doesn't have (any repo before the attribution
# stamper's first push) makes the ENTIRE fetch fail with "couldn't find
# remote ref", while a no-match glob is silently skipped.
FETCH_REFSPECS = (
    "+refs/heads/*:refs/heads/*",
    "+refs/notes/sediment*:refs/notes/sediment*",
    "+refs/pull/*/head:refs/pull/*/head",
)

_GIT_TIMEOUT_SECONDS = 600  # bounds a hung remote; local ops finish instantly
_IDENTIFIED_MIRROR_NAMESPACE = "repositories-v1"

# ponytail: fixed cross-host bound; promote it to policy only if real repositories
# need a different ceiling. The command-line value overrides ambient
# diff.renameLimit so the same mirror produces the same rename result everywhere.
_RENAME_DETECTION_LIMIT = 1_000

# Restrict git's transports to ordinary ones. Blocks the ``ext::`` transport
# (arbitrary command execution) and other exotic schemes: ``clone_url`` is
# webhook-controlled, so a valid-signature payload must not be able to pick a
# transport that runs a command. ``file`` stays enabled for local/bare remotes
# and the test fixtures. Non-network git ops (init/config/rev-list) ignore it.
_ALLOWED_GIT_PROTOCOLS = "file:git:http:https:ssh"

# Schemes a webhook-supplied clone_url may use when MirrorPolicy enforces.
# ``file`` is deliberately absent — it reads a server-local repo into the
# sender's org. Scheme-less values (bare paths, scp-style ``host:path``) are
# also refused: the forge webhooks this URL comes from always carry a real URL.
_ENFORCED_CLONE_SCHEMES = frozenset({"http", "https", "git", "ssh"})


def _ip_is_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # is_private covers the classic SSRF targets (127/8, ::1, 169.254/16,
    # 10/8, 172.16/12, 192.168/16) plus the rest of the IANA special
    # registries; the extra flags are belt-and-suspenders for
    # loopback/link-local/reserved/0.0.0.0.
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_internal_host(host: str) -> bool:
    """Loopback / link-local / private-range hosts — the SSRF targets.

    Resolves the host the way git's transport will (``getaddrinfo``), so it
    catches not just canonical IP literals but the non-canonical encodings
    ``ipaddress.ip_address`` rejects yet the resolver accepts — decimal
    (``http://2130706433/``), hex (``0x7f000001``), octal, short-form
    (``127.1``) — plus a DNS name that resolves to an internal address. DNS
    rebinding (a name that resolves differently for us than for git) stays
    uncaught; the allowlist and deployment-level egress policy are the
    controls there.
    """
    if host == "localhost" or host.endswith(".localhost"):
        return True
    # Canonical IP literal → classify directly, no lookup.
    try:
        return _ip_is_internal(ipaddress.ip_address(host))
    except ValueError:
        pass
    # Non-canonical literal or DNS name: resolve as git will, reject if ANY
    # returned address is internal. Unresolvable → git can't connect either, so
    # no SSRF; let the (real) fetch failure be the mirror's own concern.
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            if _ip_is_internal(ipaddress.ip_address(info[4][0])):
                return True
        except ValueError:
            continue
    return False


@dataclass(frozen=True)
class MirrorPolicy:
    """Confinement for the webhook-supplied ``clone_url``.

    The default (``enforce=False``) is the dev/test posture: anything git can
    fetch, including the ``file://`` bare-remote fixtures. The API layer
    injects ``enforce=not dev_mode`` and the operator's host allowlist — this
    package never imports API settings.
    """

    enforce: bool = False
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)

    def rejection_reason(self, clone_url: str) -> str | None:
        """Why this clone_url must not be fetched, or None if acceptable."""
        if not self.enforce:
            return None
        parsed = urlparse(clone_url)
        if parsed.scheme not in _ENFORCED_CLONE_SCHEMES:
            return f"scheme {parsed.scheme or '(none)'!r} is not allowed"
        host = parsed.hostname  # already lowercased, port/userinfo stripped
        if not host:
            return "clone_url has no host"
        if self.allowed_hosts:
            if any(host == allowed.lower() for allowed in self.allowed_hosts):
                return None
            return "host is not in the clone-host allowlist"
        if _is_internal_host(host):
            return "loopback/link-local/private host"
        return None


class MirrorError(RuntimeError):
    """A git subprocess failed, timed out, or the process could not start."""


class MirrorPolicyError(MirrorError):
    """The webhook-supplied clone_url was refused by the confinement policy.
    A subclass so callers can tell a *deterministic* rejection from a
    *transient* git failure: retrying a rejected URL never succeeds, so the
    webhook should skip cleanly rather than 500 and be redelivered forever."""


class FileReadStatus(StrEnum):
    """Closed outcomes for a bounded commit-file read."""

    READABLE = "readable"
    ABSENT = "absent"
    BINARY = "binary"
    OVERSIZED = "oversized"


@dataclass(frozen=True)
class FileRead:
    """The outcome and optional text from one bounded commit-file read."""

    status: FileReadStatus
    text: str | None = None


def _run_git(cwd: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git and return the completed process — exit code NOT checked, for
    the callers whose answer *is* the exit code (``is_ancestor``).

    ``TimeoutExpired`` and ``OSError`` (git missing/unexecutable, fork
    failure) are not ``MirrorError`` subclasses, so a caller's
    ``except MirrorError`` would miss them and they would surface as an
    uncaught 500 — map them in. Fail-soft paths (the notes reader) then
    degrade to the jaccard fallback, while paths with no handler still
    surface the failure, just as a ``MirrorError``.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            # Git output is bytes; repos legally hold non-UTF-8 content (a
            # latin-1 source file in a diff, an arbitrary note blob). The
            # default strict decode would raise UnicodeDecodeError — which no
            # caller's ``except MirrorError`` catches — killing an entire
            # derivation on one legacy-encoded object. Replace: the bad bytes
            # degrade to U+FFFD (never a similarity token, never valid JSON)
            # and every fail-soft path keeps its contract.
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_ALLOW_PROTOCOL": _ALLOWED_GIT_PROTOCOLS},
        )
    except subprocess.TimeoutExpired as exc:
        raise MirrorError(
            f"git {args[0]} timed out after {_GIT_TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise MirrorError(f"git {args[0]} could not run: {exc}") from exc


def _git(cwd: Path | None, *args: str) -> str:
    result = _run_git(cwd, *args)
    if result.returncode != 0:
        raise MirrorError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass
class RepoMirror:
    """A local bare mirror, ready for read-only git queries. The diff source
    the attribution derivation reads from."""

    path: Path

    def _regular_blob(self, commit_sha: str, file_path: str) -> str | None:
        row = _git(
            self.path,
            "ls-tree",
            "--full-tree",
            "-z",
            "--end-of-options",
            commit_sha,
            "--",
            file_path,
        )
        if not row:
            return None
        metadata, separator, resolved_path = row.rstrip("\0").partition("\t")
        parts = metadata.split()
        if separator != "\t" or len(parts) != 3 or resolved_path != file_path:
            return None
        mode, object_type, object_id = parts
        if not mode.startswith("100") or object_type != "blob":
            return None
        return object_id

    def read_file(self, commit_sha: str, file_path: str, *, max_bytes: int) -> FileRead:
        """Read a regular file at a commit without exceeding ``max_bytes``."""
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        object_id = self._regular_blob(commit_sha, file_path)
        if object_id is None:
            return FileRead(FileReadStatus.ABSENT)
        size_text = _git(
            self.path, "cat-file", "-s", "--end-of-options", object_id
        ).strip()
        try:
            size = int(size_text)
        except ValueError as exc:  # pragma: no cover - git contract guard
            raise MirrorError("git cat-file returned a non-integer size") from exc
        if size > max_bytes:
            return FileRead(FileReadStatus.OVERSIZED)
        text = _git(self.path, "cat-file", "blob", "--end-of-options", object_id)
        if "\0" in text:
            return FileRead(FileReadStatus.BINARY)
        return FileRead(FileReadStatus.READABLE, text)

    def resolve_path(
        self, source_sha: str, boundary_sha: str, source_path: str
    ) -> str | None:
        """Resolve a same path or one unambiguous rename at ``boundary_sha``."""
        if self._regular_blob(boundary_sha, source_path) is not None:
            return source_path
        output = _git(
            self.path,
            "-c",
            "core.quotePath=false",
            "diff",
            "--name-status",
            "-z",
            "-M",
            f"-l{_RENAME_DETECTION_LIMIT}",
            "--end-of-options",
            source_sha,
            boundary_sha,
            "--",
        )
        fields = output.split("\0")
        if fields and fields[-1] == "":
            fields.pop()
        destinations: list[str] = []
        index = 0
        while index < len(fields):
            status = fields[index]
            index += 1
            if status.startswith(("R", "C")):
                if index + 1 >= len(fields):
                    break
                old_path, new_path = fields[index : index + 2]
                index += 2
                if status.startswith("R") and old_path == source_path:
                    destinations.append(new_path)
            elif index < len(fields):
                index += 1
        if len(destinations) != 1:
            return None
        destination = destinations[0]
        if self._regular_blob(boundary_sha, destination) is None:
            return None
        return destination

    def refs(self) -> dict[str, str]:
        """Return the sorted ref-to-object snapshot used by a derivation."""

        rows = _git(
            self.path,
            "for-each-ref",
            "--format=%(refname)%00%(objectname)",
        ).splitlines()
        return dict(sorted(row.split("\0", 1) for row in rows if "\0" in row))

    def fetch_commit_diff(self, repo: str, commit_sha: str) -> str:
        """Unified diff for one commit, matching GitHub's ``.diff`` semantics:
        against the first parent (merges included), the empty tree for a root
        commit, with rename detection."""
        # --end-of-options: commit_sha is webhook-controlled and unvalidated, so
        # a "--output=<path>"-style value must be read as a (bad) revision, not
        # an option — otherwise it's a git-option-injection / file-write vector.
        parents = _git(
            self.path,
            "rev-list",
            "--parents",
            "-n",
            "1",
            "--end-of-options",
            commit_sha,
        ).split()[1:]
        # Keep non-ASCII names readable in exported patch text. Git still
        # C-quotes controls, quotes, and backslashes; the shared parser handles
        # both quoted and unquoted paths.
        base = (
            "-c",
            "core.quotePath=false",
            "diff-tree",
            "-p",
            "-M",
            f"-l{_RENAME_DETECTION_LIMIT}",
            "--no-commit-id",
        )
        if parents:
            return _git(self.path, *base, "--end-of-options", parents[0], commit_sha)
        return _git(self.path, *base, "--root", "--end-of-options", commit_sha)

    def list_push_commits(self, push: Push, max_commits: int) -> list[str]:
        """The commits of a push, oldest first, capped at ``max_commits``
        (newest kept — the head is what attribution targets first).
        Force-pushes, branch creations (zero ``before``), and an unknown
        ``before`` all degrade to head-only."""
        if push.forced or not push.before_sha.strip("0"):
            return [push.after_sha]
        try:
            revs = _git(
                self.path,
                "rev-list",
                f"--max-count={max(1, max_commits)}",
                "--end-of-options",
                f"{push.before_sha}..{push.after_sha}",
            ).split()
        except MirrorError:
            logger.warning(
                "push_range_unenumerable",
                extra={"before": push.before_sha, "after": push.after_sha},
            )
            return [push.after_sha]
        # Clamp: a 0/negative cap (config typo) must still keep the head —
        # never silently attribute a stray subset or nothing.
        return list(reversed(revs)) or [push.after_sha]

    def heads_precede_commit(
        self, commit_sha: CommitSha, heads: Iterable[CommitSha]
    ) -> bool:
        """Prove every head in a caller-bounded batch strictly precedes a commit.

        Unknown ancestry returns False so callers retain exact range checks.
        """
        heads = tuple(dict.fromkeys(heads))
        if not heads:
            return True
        try:
            remaining = _git(
                self.path,
                "rev-list",
                "--max-count=1",
                "--end-of-options",
                *heads,
                # Exclude all target parents and their ancestors, not the target.
                # Any output disproves the batch proof; empty output proves it.
                f"^{commit_sha}^@",
            )
        except MirrorError:
            logger.warning(
                "commit_owner_prefilter_unavailable",
                extra={"commit_sha": commit_sha, "head_count": len(heads)},
            )
            return False
        return not remaining.strip()

    def commit_exists(self, commit_sha: str) -> bool:
        """Whether ``commit_sha`` resolves to a commit object in this mirror.

        Read-only, never fetches. The gold-patch projection uses this to
        keep only a session's commits that live in the reward repo's mirror, so
        a cross-repo session degrades to the repo it can actually diff rather
        than emitting a nonsense range. ``--end-of-options``: ``commit_sha`` is
        webhook-derived, so it must be read as a revision, never a git option.
        The ``^{commit}`` peel makes a tag or tree name (which ``--verify``
        alone would accept) fail — only a real commit counts."""
        try:
            _git(
                self.path,
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{commit_sha}^{{commit}}",
            )
        except MirrorError:
            return False
        return True

    def parent_commit(self, commit_sha: str) -> str | None:
        """The first parent of ``commit_sha`` (``sha^``), or None for a root
        commit (no parent). Raises ``MirrorError`` when the commit is absent.

        Read-only, never fetches. The task projection uses this for
        ``base_commit`` — the checkout point the gold patch applies onto.
        ``--end-of-options``: ``commit_sha`` is webhook-derived, so it must be
        read as a revision, never a git option."""
        parents = _git(
            self.path,
            "rev-list",
            "--parents",
            "-n",
            "1",
            "--end-of-options",
            commit_sha,
        ).split()[1:]
        return parents[0] if parents else None

    def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool:
        """Whether ``ancestor_sha`` is an ancestor of — or equal to —
        ``descendant_sha``. Read-only, never fetches.

        The task projection uses this to reject a degenerate
        ``base..last`` range before emitting a nonsense gold patch: a session's
        attributed commits are ordered by committer time with an sha tie-break,
        so two commits sharing a timestamp can land child-before-parent, which
        would otherwise diff a commit against its own descendant. Runs through
        ``_run_git`` rather than ``_git`` because the answer is the *exit
        code* — 0 is ancestor, 1 is not, anything else is a real failure — and
        ``_git`` collapses 1 into ``MirrorError``. ``--end-of-options``: both
        revisions are webhook-derived."""
        result = _run_git(
            self.path,
            "merge-base",
            "--is-ancestor",
            "--end-of-options",
            ancestor_sha,
            descendant_sha,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise MirrorError(
            f"git merge-base --is-ancestor failed: {result.stderr.strip()}"
        )

    def diff_range(self, base_sha: str, head_sha: str) -> str:
        """Unified diff ``base_sha..head_sha`` — the gold patch — matching
        the mirror's single-commit diff semantics: rename detection on, path
        quoting off so accented/CJK filenames stay parseable.

        Read-only, never fetches. Both revisions are webhook-derived, so
        ``--end-of-options`` fences them off from git's option parser; ``--``
        after them keeps a ref that happens to look like a path from being
        read as one.

        Unlike ``fetch_commit_diff`` (plumbing ``diff-tree``, config-immune),
        this is porcelain ``git diff`` — a developer's global config
        (``diff.noprefix``, ``diff.external``, ``diff.srcPrefix``/
        ``dstPrefix``) would otherwise reshape or replace the diff body. Pin
        every knob that matters and disable external diff drivers so the
        output shape is identical on every machine."""
        return _git(
            self.path,
            "-c",
            "core.quotePath=false",
            "-c",
            "diff.noprefix=false",
            "-c",
            "diff.srcPrefix=a/",
            "-c",
            "diff.dstPrefix=b/",
            "diff",
            "--no-ext-diff",
            "-M",
            f"-l{_RENAME_DETECTION_LIMIT}",
            "--end-of-options",
            base_sha,
            head_sha,
            "--",
        )


class MirrorManager:
    """Creates and refreshes the bare mirrors under one base directory."""

    def __init__(self, base_path: str, policy: MirrorPolicy | None = None) -> None:
        self.base = Path(base_path)
        self.policy = policy or MirrorPolicy()
        self._read_snapshot_state = threading.local()

    def _mirror_path(self, org_id: str, repo: str) -> Path:
        # One flat directory per (org, repo): percent-quoting makes "/" (and
        # anything else path-hostile in webhook-controlled names) inert, so a
        # crafted repo name cannot traverse outside the base dir.
        return self.base / quote(f"{org_id}/{repo}", safe="")

    def _lock_file(self, mirror_dir_name: str) -> Path:
        # Locks live in a subdir so a lock file can never collide with a mirror
        # directory: a dirname always contains "%2F" (the quoted "/"), so it can
        # never equal ".locks" — whereas "base/{dirname}.lock" collides with the
        # mirror dir of a repo literally named "….lock".
        locks = self.base / ".locks"
        path = locks / mirror_dir_name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _observation_lock_file(self, mirror_dir_name: str) -> Path:
        locks = self.base / ".observation-locks"
        path = locks / mirror_dir_name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _repository_path(self, key: RepositoryKey) -> Path:
        if isinstance(key, LegacyRepositoryKey):
            return self._mirror_path(key.org_id, key.repo)
        if not isinstance(key, IdentifiedRepositoryKey):
            raise ValueError("mirror requires a validated repository key")
        identity = key.identity
        # Validated components each fit one filesystem leaf. Combining them
        # would exceed NAME_MAX for a valid 253-character forge hostname.
        return (self.base / _IDENTIFIED_MIRROR_NAMESPACE).joinpath(
            *(
                quote(part, safe="")
                for part in (
                    key.org_id,
                    identity.provider,
                    identity.host,
                    identity.repository_id,
                )
            )
        )

    def _refresh_target(
        self,
        push: Push,
        repository_context: RepositoryContext | None,
        fetch_repo: str | None,
        fetch_clone_url: str | None,
    ) -> tuple[RepositoryKey, str, str]:
        repo = push.repo if fetch_repo is None else fetch_repo
        clone_url = push.clone_url if fetch_clone_url is None else fetch_clone_url
        if repository_context is None:
            if push.repository_id is not None:
                raise MirrorPolicyError("repository_source_absent")
            if repo != push.repo:
                raise MirrorPolicyError("repository_mirror_identity_unresolved")
            key = LegacyRepositoryKey(push.org_id, push.repo)
        else:
            source = repository_context.resolve_fact(push)
            if source.key is None:
                raise MirrorPolicyError(source.reason)
            target = repository_context.mirror_refresh_resolution(
                FactTable.PUSHES, push.push_id, repo=repo
            )
            if target.key is None:
                raise MirrorPolicyError(target.reason)
            key = target.key
        self._validate_clone_url(repo, clone_url)
        return key, repo, clone_url

    @contextmanager
    def observation_capture(
        self,
        push: Push,
        *,
        repository_context: RepositoryContext | None = None,
        fetch_repo: str | None = None,
        fetch_clone_url: str | None = None,
    ) -> Iterator[None]:
        """Serialize refresh-to-persistence capture for one repository.

        This lock is separate from the mirror lock. A capture can release the
        mirror for Derivations before a database write completes without
        allowing a later Push to persist the same Git-note edge first.
        """
        key, _, _ = self._refresh_target(
            push, repository_context, fetch_repo, fetch_clone_url
        )
        name = str(self._repository_path(key).relative_to(self.base))
        with _locked(self._observation_lock_file(name)):
            yield

    def _validate_clone_url(self, repo: str, clone_url: str) -> None:
        reason = self.policy.rejection_reason(clone_url)
        if reason is None:
            return
        parsed = urlparse(clone_url)
        # Log scheme+host only: clone URLs can embed userinfo credentials.
        logger.warning(
            "clone_url_rejected",
            extra={
                "repo": repo,
                "scheme": parsed.scheme,
                "clone_host": parsed.hostname,
                "reason": reason,
            },
        )
        raise MirrorPolicyError(f"clone_url rejected: {reason}")

    @contextmanager
    def read_snapshot(
        self, org_id: str, repos: list[str] | tuple[str, ...]
    ) -> Iterator[MirrorManager]:
        """Hold every requested mirror stable for a derivation run.

        Locks use the same files as create, fetch, remove, and rename. Sorted
        acquisition gives multi-repository runs the same deadlock-free order.
        """

        with self.read_repository_snapshot(
            LegacyRepositoryKey(org_id, repo) for repo in repos
        ):
            yield self

    @contextmanager
    def read_repository_snapshot(
        self, keys: Iterable[RepositoryKey]
    ) -> Iterator[MirrorManager]:
        """Hold stable repository locks without reading or changing origin."""
        names = sorted(
            {str(self._repository_path(key).relative_to(self.base)) for key in keys}
        )
        held = getattr(self._read_snapshot_state, "held", None)
        if held is not None and held[0] == os.getpid():
            if not set(names).issubset(held[1]):
                raise ValueError("nested mirror snapshot cannot add repository locks")
            yield self
            return
        with ExitStack() as stack:
            for name in names:
                stack.enter_context(_locked(self._lock_file(name)))
            self._read_snapshot_state.held = (os.getpid(), frozenset(names))
            try:
                yield self
            finally:
                del self._read_snapshot_state.held

    def open(self, org_id: str, repo: str) -> RepoMirror | None:
        """Legacy-only accessor: the existing mirror for (org, repo), or None
        when the repo was never mirrored. Never fetches — derivations must
        not touch the network."""
        path = self._mirror_path(org_id, repo)
        if not (path / "HEAD").exists():
            return None
        return RepoMirror(path)

    def open_repository(self, key: RepositoryKey) -> RepoMirror | None:
        """Open one exact repository lifetime without fetching or inferring it."""
        path = self._repository_path(key)
        if not (path / "HEAD").exists():
            return None
        return RepoMirror(path)

    def ensure(
        self,
        push: Push,
        *,
        repository_context: RepositoryContext | None = None,
        fetch_repo: str | None = None,
        fetch_clone_url: str | None = None,
    ) -> RepoMirror:
        """Create the mirror on first sight, then fetch. Returns the
        refreshed mirror, ready for diff extraction."""
        with self.refresh_snapshot(
            push,
            repository_context=repository_context,
            fetch_repo=fetch_repo,
            fetch_clone_url=fetch_clone_url,
        ) as mirror:
            return mirror

    @contextmanager
    def refresh_snapshot(
        self,
        push: Push,
        *,
        repository_context: RepositoryContext | None = None,
        fetch_repo: str | None = None,
        fetch_clone_url: str | None = None,
    ) -> Iterator[RepoMirror]:
        """Refresh one mirror and hold it stable while the caller observes it."""
        # Confine the webhook-controlled clone_url BEFORE any git op or
        # directory creation — a rejected URL must leave no trace and never
        # reach `git fetch`.
        key, repo, clone_url = self._refresh_target(
            push, repository_context, fetch_repo, fetch_clone_url
        )
        path = self._repository_path(key)
        name = str(path.relative_to(self.base))
        with _locked(self._lock_file(name)):
            if not (path / "HEAD").exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                _git(None, "init", "--bare", "--quiet", str(path))
                logger.info(
                    "mirror_created",
                    extra={"repo": repo, "path": str(path)},
                )
            # Reconcile the remote every time via `git config` (create-or-update),
            # NOT `remote add`/`set-url`. This is idempotent and self-healing: a
            # mirror half-created by an earlier crash (HEAD present, origin never
            # configured) would otherwise take a `remote set-url` path that fails
            # with "No such remote" on every future push and wedge permanently.
            # It also picks up a rotated clone_url for free.
            _git(path, "config", "remote.origin.url", clone_url)
            first, *rest = FETCH_REFSPECS
            _git(path, "config", "--replace-all", "remote.origin.fetch", first)
            for refspec in rest:
                _git(path, "config", "--add", "remote.origin.fetch", refspec)
            # --prune: drop mirror refs the remote deleted (closed PRs, merged
            # branches) so refs/pull/* and refs/heads/* stay a faithful mirror
            # — stale refs would mislead squash aliasing.
            # -c http.followRedirects=false: the clone_url host is confined by
            # MirrorPolicy, but git's default (follow the initial redirect)
            # would let an allowed public host 302 the info/refs request to an
            # internal address (169.254.169.254, …) — SSRF past the host check.
            # Deny redirects so the confined host is the only host contacted.
            _git(
                path,
                "-c",
                "http.followRedirects=false",
                "fetch",
                "--prune",
                "--quiet",
                "origin",
            )
            yield RepoMirror(path)

    def list_mirrored_repos(self, org_id: str) -> list[str]:
        """Legacy repos mirrored under ``org_id``, recovered from the
        directory names this manager itself created
        (``quote(f"{org_id}/{repo}", safe="")``) — never a fact-store read.
        GC uses this to enumerate what exists on disk before deciding
        what the fact store says should survive. Skips ``.locks`` and any
        entry without a ``HEAD`` file (not a mirror — a stray or
        half-created directory)."""
        if not self.base.exists():
            return []
        prefix = f"{org_id}/"
        repos: list[str] = []
        for entry in self.base.iterdir():
            if entry.name == ".locks" or not (entry / "HEAD").exists():
                continue
            decoded = unquote(entry.name)
            if decoded.startswith(prefix):
                repos.append(decoded[len(prefix) :])
        return repos

    def list_mirrored_repositories(self, org_id: str) -> list[RepositoryKey]:
        """Enumerate stable keys and separate legacy mirrors; never infer names."""
        org_id = normalize_org_id(org_id)
        keys: set[RepositoryKey] = set()
        for repo in self.list_mirrored_repos(org_id):
            try:
                keys.add(LegacyRepositoryKey(org_id, repo))
            except ValueError:
                logger.warning("mirror_directory_invalid", extra={"org_id": org_id})
        org_root = self.base / _IDENTIFIED_MIRROR_NAMESPACE / quote(org_id, safe="")
        if org_root.exists():
            for entry in org_root.glob("*/*/*"):
                if not (entry / "HEAD").exists():
                    continue
                try:
                    provider, host, repository_id = (
                        unquote(part) for part in entry.relative_to(org_root).parts
                    )
                    key = IdentifiedRepositoryKey(
                        org_id,
                        RepositoryIdentity(
                            ForgeProvider(provider), host, repository_id
                        ),
                    )
                    if self._repository_path(key) != entry:
                        raise ValueError("noncanonical mirror path")
                except ValueError:
                    logger.warning("mirror_directory_invalid", extra={"org_id": org_id})
                    continue
                keys.add(key)
        return sorted(keys, key=repository_sort_key)

    def remove_repository(self, key: RepositoryKey) -> bool:
        """Remove one exact mirror under its stable lock; legacy stays separate."""
        path = self._repository_path(key)
        if isinstance(key, LegacyRepositoryKey):
            return self.remove(key.org_id, key.repo)
        name = str(path.relative_to(self.base))
        with _locked(self._lock_file(name)):
            if not path.exists():
                return False
            shutil.rmtree(path)
        return True

    def remove(self, org_id: str, repo: str) -> bool:
        """Delete a mirror's entire directory tree. Destructive and, short of
        a fresh clone, irreversible — callers (``gc_mirrors``) own the
        dry-run/``--apply`` posture; this method has none of its own. Returns
        ``False`` when there was nothing to remove."""
        path = self._mirror_path(org_id, repo)
        with _locked(self._lock_file(path.name)):
            if not path.exists():
                return False
            shutil.rmtree(path)
        return True

    def rename(self, org_id: str, old_repo: str, new_repo: str) -> bool:
        """Move a legacy mirror between literal paths. Identified capture
        never calls this method; names cannot establish repository identity.

        Returns ``False`` (no-op) when no mirror exists under ``old_repo``
        (the eventual first push under the new name just clones fresh via
        ``ensure()``) or when a mirror already exists under ``new_repo``
        (never clobber an existing mirror — a double-rename or a race against
        a concurrent ``ensure()`` under the new name). Does not touch
        ``remote.origin.url``: ``ensure()`` already reconciles that
        idempotently via ``git config`` on the next push under the new name.

        Locks both the old and new mirror's lock files, in sorted order, so a
        concurrent ``ensure()``/``remove()`` on either name cannot deadlock
        against this."""
        if old_repo == new_repo:
            return False
        old_path = self._mirror_path(org_id, old_repo)
        new_path = self._mirror_path(org_id, new_repo)
        first, second = sorted((old_path.name, new_path.name))
        with _locked(self._lock_file(first)), _locked(self._lock_file(second)):
            if not old_path.exists() or new_path.exists():
                return False
            new_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.rename(new_path)
        return True
