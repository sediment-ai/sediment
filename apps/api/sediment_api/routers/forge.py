# SPDX-License-Identifier: AGPL-3.0-or-later
"""
GitHub webhook ingest routes, mounted under /ingest/github in main.py.

One module for both routes: they share the HMAC intake and differ only in
which parser/store pair the payload walks through. The signature is
verified BEFORE the event type is examined: a wrong-secret delivery must
401 even for event types a route skips, or GitHub's setup ping would
green-light a misconfigured webhook. The push route stores the Push fact,
then — when SEDIMENT_MIRROR_PATH is set — schedules an (org, repo) mirror
refresh as a background task that runs AFTER the response. Best-effort
substrate: the fetch is off both the event loop and the request path, and
no failure ever becomes a 4xx/5xx because the stored fact is the contract
(ADR 0001).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request
from sediment_capture import (
    parse_pull_request_merge,
    parse_pull_request_revision,
    parse_push,
    parse_repository_rename,
    parse_workflow_run,
)
from sediment_core import (
    FactStore,
    REPOSITORY_IDENTITY_LIMIT,
    OperationalReportLimitExceeded,
    Push,
    SessionCommitObservation,
)
from sediment_derive import (
    AttributionSource,
    AttributionPolicy,
    MirrorError,
    MirrorManager,
    MirrorPolicy,
    MirrorPolicyError,
    derive_attribution_result,
    read_repository_context,
    build_repository_context,
)
from sediment_derive.notes import read_commit_note
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from ..config import settings
from ..deps import get_store, read_verified_webhook

logger = logging.getLogger("sediment.api.forge")
router = APIRouter(tags=["forge"])


def _refresh_mirror(
    push: Push,
    store: FactStore,
    *,
    fetch_repo: str | None = None,
    fetch_clone_url: str | None = None,
) -> None:
    """Fetch the retained Push's repository mirror, then re-derive the repo's
    attributions. The background supervisor runs it in a disposable process,
    keeping ``git fetch`` off the event loop and the request-serving pool.
    Clone-URL confinement is enforced only in production, so the
    dev/test file:// bare-remote fixtures keep working; injected here rather
    than read from settings inside the derive package.

    Swallows every failure with a severity-appropriate log: the Push fact is
    already stored and is the contract (ADR 0001), so a refresh problem must
    never escape (a raising background task would surface after the response).
    Invalid or unavailable repository context stops before Git work. After a
    valid context, a failed fetch still permits a pure Derivation over retained
    mirror state.

    The process supervisor bounds this complete chain and terminates its Git
    descendants at the deadline. A child owns its database engine.
    """
    # Context reads finish before Git locks. The full population prevents a
    # selected Push from hiding another lifetime that claims its fetch location.
    try:
        boundary = datetime.now(UTC)
        with store.read_snapshot() as snapshot:
            context = build_repository_context(
                snapshot.read_repository_identities(
                    push.org_id,
                    captured_through=boundary,
                    limit=REPOSITORY_IDENTITY_LIMIT,
                ),
                snapshot.read_repository_renames(
                    push.org_id,
                    captured_through=boundary,
                    limit=REPOSITORY_IDENTITY_LIMIT,
                ),
                push.org_id,
                as_of=boundary,
            )
    except SQLAlchemyError:
        logger.error(
            "repository_context_database_unavailable repo=%s count=1", push.repo
        )
        return
    except OperationalReportLimitExceeded:
        logger.warning("repository_context_declined reason=population_limit count=1")
        return
    except Exception:
        logger.warning("repository_context_declined reason=invalid_population count=1")
        return
    policy = MirrorPolicy(
        enforce=not settings.dev_mode,
        allowed_hosts=frozenset(settings.allowed_clone_hosts),
    )
    mirrors = MirrorManager(settings.mirror_path, policy=policy)
    location = dict(
        repository_context=context,
        fetch_repo=fetch_repo,
        fetch_clone_url=fetch_clone_url,
    )
    try:
        with mirrors.observation_capture(push, **location):
            with mirrors.refresh_snapshot(push, **location) as mirror:
                try:
                    observations = _capture_session_commit_observations(
                        push,
                        mirror,
                        push.push_id,
                        max_commits=AttributionPolicy().max_commits_per_push,
                    )
                except Exception:
                    logger.exception(
                        "session_commit_observation_error repo=%s", push.repo
                    )
                    observations = []
            _store_session_commit_observations(push, observations, store)
    except MirrorPolicyError as exc:
        # A source/target identity or clone policy decline leaves refs intact.
        logger.warning(
            "mirror_refresh_skipped repo=%s reason=%s count=1", push.repo, exc
        )
    except MirrorError as exc:
        # Transient git/network failure: the next push retries the fetch.
        logger.warning(
            "mirror_refresh_failed repo=%s ref=%s after_sha=%s error=%s",
            push.repo,
            push.ref,
            push.after_sha,
            exc,
        )
    except Exception:
        # A genuine bug in the mirror path — keep the traceback, but still
        # never let it escape into (or after) the webhook response.
        logger.exception(
            "mirror_refresh_error repo=%s ref=%s after_sha=%s",
            push.repo,
            push.ref,
            push.after_sha,
        )
    _derive_push_attributions(push, mirrors, store)


def _capture_session_commit_observations(
    push: Push,
    mirror,
    source_push_id: str,
    *,
    max_commits: int,
) -> list[SessionCommitObservation]:
    """Collect Git-note edges exposed by one stable bounded refresh."""
    captured_at = datetime.now(UTC)
    observations: list[SessionCommitObservation] = []
    for commit_sha in mirror.list_push_commits(push, max_commits):
        note = read_commit_note(mirror.path, commit_sha)
        if note is None:
            continue
        for session_id in sorted({session.session_id for session in note.sessions}):
            try:
                observation = SessionCommitObservation(
                    org_id=push.org_id,
                    repo=push.repo,
                    repository_provider=push.repository_provider,
                    repository_host=push.repository_host,
                    repository_id=push.repository_id,
                    commit_sha=commit_sha,
                    session_id=session_id,
                    source_push_id=source_push_id,
                    captured_at=captured_at,
                )
            except ValueError:
                logger.warning(
                    "session_commit_observation_invalid repo=%s commit_sha=%s",
                    push.repo,
                    commit_sha,
                )
                continue
            observations.append(observation)
    return observations


def _store_session_commit_observations(
    push: Push,
    observations: list[SessionCommitObservation],
    store: FactStore,
) -> None:
    """Persist collected observations after releasing the mirror lock."""
    stored_count = 0
    duplicate_count = 0
    for observation in observations:
        try:
            stored = store.store_session_commit_observation(observation)
        except SQLAlchemyError:
            logger.error(
                "session_commit_observation_database_unavailable repo=%s",
                push.repo,
            )
            return
        except Exception:
            logger.exception(
                "session_commit_observation_error repo=%s commit_sha=%s",
                push.repo,
                observation.commit_sha,
            )
            continue
        stored_count += int(stored)
        duplicate_count += int(not stored)
    logger.info(
        "session_commit_observations_captured repo=%s stored=%d duplicates=%d",
        push.repo,
        stored_count,
        duplicate_count,
    )


def _derive_push_attributions(
    push: Push, mirrors: MirrorManager, store: FactStore
) -> None:
    """Run the attribution derivation for THIS push's commits and log
    structured counts. A trigger and a log line, NOT a cache: the result is
    discarded — it is recomputable on demand (ADR 0001), and caching is
    deferred until a consumer needs it. Scoped to the arriving push so the
    per-webhook cost is bounded by one push's commits, not the repo's whole
    history, and the logged counts describe this push. The PostgreSQL store
    borrows connections from the lifespan-owned engine across worker threads.
    Same fail-soft contract as the refresh: no failure ever escapes."""
    try:
        # Observation capture has finished. Use a fresh snapshot so context,
        # quarantine visibility, and the Derivation include those stored Facts.
        with store.read_snapshot() as snapshot:
            context = read_repository_context(snapshot, push.org_id)
            result = derive_attribution_result(
                snapshot,
                mirrors,
                push.org_id,
                pushes=[push],
                repository_context=context,
                as_of=context.as_of,
            )
        attributions = result.attributions
        by_source = Counter(c.attribution_source for c in attributions)
        logger.info(
            "attributions_derived repo=%s attributed=%d notes=%d jaccard=%d",
            push.repo,
            len(attributions),
            by_source[AttributionSource.GIT_NOTES],
            by_source[AttributionSource.JACCARD],
        )
    except SQLAlchemyError:
        # SQLAlchemy exception text can contain statements, parameters, and
        # driver diagnostics. Keep the fact-first background path fail-soft
        # without putting any of that secret-bearing context in the log.
        logger.error("attribution_derivation_database_unavailable repo=%s", push.repo)
    except Exception:
        logger.exception("attribution_derivation_error repo=%s", push.repo)


@router.post(
    "/push",
    responses={
        503: {
            "description": "Database unavailable, or stored Push awaits retry after mirror admission failure."
        }
    },
)
async def ingest_push(
    request: Request,
    background: BackgroundTasks,
    # None (not required): a missing signature must 401 as an auth failure
    # inside read_verified_webhook, not 422 as a schema error. The event
    # header stays required — absent only on non-GitHub malformed requests.
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str = Header(...),
    store: FactStore = Depends(get_store),
) -> dict[str, Any]:
    payload = await read_verified_webhook(request, x_hub_signature_256)
    # GitHub delivers every subscribed event (incl. ping) to the one URL;
    # the X-GitHub-Event header is the discriminator.
    if x_github_event != "push":
        return {"skipped": True, "reason": "not a push event"}
    push = parse_push(payload, org_id=settings.org_id, github_host=settings.github_host)
    if push is None:
        return {"skipped": True, "reason": "not a storable push"}
    receipt = await run_in_threadpool(store.store_push_receipt, push)
    fetch_repo, fetch_clone_url = push.repo, push.clone_url
    push = receipt.fact
    stored = receipt.stored
    logger.info(
        "push_stored fact_id=%s stored=%s repo=%s ref=%s",
        push.push_id,
        stored,
        push.repo,
        push.ref,
    )
    # Schedule the refresh on redeliveries too (stored=False): the fetch is
    # idempotent and a redelivery may be the retry after a failed mirror.
    # Skip repo-less pushes: parse_push stores them (facts first) but the
    # mirror has no repo to fetch, and repo="" would collapse distinct repos
    # into one shared mirror dir where fetch --prune thrashes their refs.
    if settings.mirror_path and push.repo:
        task = request.app.state.workers.submit_push(
            push, fetch_repo=fetch_repo, fetch_clone_url=fetch_clone_url
        )
        background.add_task(request.app.state.workers.wait_background, task)
    return {"fact_id": push.push_id, "stored": stored}


@router.post(
    "/repository",
    response_model=dict[str, Any],
    responses={
        503: {
            "description": "Mirror work capacity, storage, or execution budget exceeded."
        }
    },
)
async def ingest_repository(
    request: Request,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str = Header(...),
    x_github_delivery: str | None = Header(None),
    store: FactStore = Depends(get_store),
) -> dict[str, Any]:
    """Store an immutable rename receipt independently of mirror availability."""
    payload = await read_verified_webhook(request, x_hub_signature_256)
    if x_github_event != "repository":
        return {"skipped": True, "reason": "not a repository event"}
    rename, reason = parse_repository_rename(
        payload,
        org_id=settings.org_id,
        github_host=settings.github_host,
        source_event_id=x_github_delivery,
    )
    if rename is None:
        assert reason is not None
        return {"skipped": True, "reason": reason.value}
    receipt = await run_in_threadpool(store.store_repository_rename_receipt, rename)
    logger.info(
        "repository_rename_stored fact_id=%s stored=%s", receipt.fact_id, receipt.stored
    )
    return {"fact_id": receipt.fact_id, "stored": receipt.stored}


@router.post("/ci")
async def ingest_ci(
    request: Request,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str = Header(...),
    x_github_delivery: str | None = Header(None),
    store: FactStore = Depends(get_store),
) -> dict[str, Any]:
    payload = await read_verified_webhook(request, x_hub_signature_256)
    if x_github_event != "workflow_run":
        return {"skipped": True, "reason": "not a workflow_run event"}
    outcome = parse_workflow_run(
        payload,
        org_id=settings.org_id,
        source_event_id=x_github_delivery,
        github_host=settings.github_host,
    )
    if outcome is None:
        return {"skipped": True, "reason": "not a completed workflow_run"}
    receipt = await run_in_threadpool(store.store_ci_outcome_receipt, outcome)
    outcome = receipt.fact
    stored = receipt.stored
    logger.info(
        "ci_outcome_stored fact_id=%s stored=%s repo=%s result=%s",
        outcome.outcome_id,
        stored,
        outcome.repo,
        outcome.result,
    )
    return {"fact_id": outcome.outcome_id, "stored": stored}


@router.post("/pull-request")
async def ingest_pull_request(
    request: Request,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str = Header(...),
    x_github_delivery: str | None = Header(None),
    store: FactStore = Depends(get_store),
) -> dict[str, Any]:
    payload = await read_verified_webhook(request, x_hub_signature_256)
    if x_github_event != "pull_request":
        return {"skipped": True, "reason": "not a pull_request event"}
    if payload.get("action") == "closed":
        merge = parse_pull_request_merge(
            payload,
            org_id=settings.org_id,
            source_event_id=x_github_delivery,
            github_host=settings.github_host,
        )
        if merge is None:
            return {"skipped": True, "reason": "not a merged pull_request"}
        receipt = await run_in_threadpool(store.store_pull_request_merge_receipt, merge)
        merge = receipt.fact
        stored = receipt.stored
        logger.info(
            "pull_request_merge_stored fact_id=%s stored=%s repo=%s pr_number=%s",
            merge.merge_id,
            stored,
            merge.repo,
            merge.pr_number,
        )
        return {"fact_id": merge.merge_id, "stored": stored}

    revision, skip_reason = parse_pull_request_revision(
        payload,
        org_id=settings.org_id,
        source_event_id=x_github_delivery,
        github_host=settings.github_host,
    )
    if revision is None:
        assert skip_reason is not None
        return {"skipped": True, "reason": skip_reason.value}
    receipt = await run_in_threadpool(
        store.store_pull_request_revision_receipt, revision
    )
    revision = receipt.fact
    stored = receipt.stored
    logger.info(
        "pull_request_revision_stored fact_id=%s stored=%s repo=%s pr_number=%s",
        revision.revision_id,
        stored,
        revision.repo,
        revision.pr_number,
    )
    return {"fact_id": revision.revision_id, "stored": stored}
