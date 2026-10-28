"""The ARQ worker: `arq app.worker.WorkerSettings`.

ARQ in one paragraph: a job is a Redis entry in a sorted set (the queue).
A worker polls the queue, claims a job by setting an "in-progress" key, runs
the async function, and only REMOVES the job from the queue when the
function has finished. That last detail is what makes crashes safe (see
docs/DECISIONS.md, "Worker crash safety").

This module decides what happens when a job fails:

    PermanentError             -> dead-letter queue now (retrying cannot help)
    TransientError / unknown   -> retry later with backoff, until MAX_ATTEMPTS,
                                  then dead-letter queue
"""
import logging
import uuid

from arq import Retry
from arq.connections import RedisSettings

from app import dlq
from app.config import Settings, get_settings
from app.db import build_engine, build_session_factory
from app.errors import PermanentError, TransientError
from app.jobs import JobDeps, load, process_resume, set_status
from app.llm import build_provider
from app.models import DEAD, QUEUED
from app.queue import JOB_FUNCTION
from app.retry import backoff_delay

logger = logging.getLogger("talentsieve.worker")


async def score_resume_job(ctx: dict, resume_id: str) -> str:
    deps: JobDeps = ctx["deps"]
    settings = deps.settings
    attempt = ctx["job_try"]  # 1 on the first run; ARQ bumps it on every start, crashes included
    rid = uuid.UUID(resume_id)

    if attempt > settings.max_attempts:
        # Only reachable if a worker died during the last allowed attempt.
        return await dead_letter(deps, rid, settings.max_attempts,
                                 f"gave up after {settings.max_attempts} attempts (worker crashed on the last one)")
    try:
        return await process_resume(deps, rid, attempt)
    except PermanentError as exc:
        return await dead_letter(deps, rid, attempt, f"permanent error, not retried: {exc}")
    except Exception as exc:
        # TransientError is the expected case. An UNKNOWN exception (a bug,
        # a library error we did not map) is also retried: a retry costs a
        # little; wrongly dead-lettering a recoverable job loses a candidate.
        kind = "transient" if isinstance(exc, TransientError) else "unexpected"
        if attempt >= settings.max_attempts:
            return await dead_letter(deps, rid, attempt,
                                     f"{kind} error, gave up after {attempt} attempts: {exc}")
        delay = backoff_delay(attempt, settings.retry_base_delay_seconds, settings.retry_max_delay_seconds)
        await mark_for_retry(deps, rid, attempt, f"{kind} error on attempt {attempt}, retrying: {exc}")
        logger.info("resume %s attempt %s failed (%s); retry in %.2fs", rid, attempt, exc, delay)
        # Retry tells ARQ to put the SAME job back in the queue, deferred.
        raise Retry(defer=delay) from exc


async def mark_for_retry(deps: JobDeps, resume_id: uuid.UUID, attempt: int, error: str) -> None:
    resume, _ = await load(deps, resume_id)
    await set_status(deps, resume, QUEUED, error=error, attempts=attempt)


async def dead_letter(deps: JobDeps, resume_id: uuid.UUID, attempt: int, reason: str) -> str:
    logger.warning("resume %s -> DLQ: %s", resume_id, reason)
    try:
        resume, _ = await load(deps, resume_id)
        await set_status(deps, resume, DEAD, error=reason, attempts=attempt)
    except PermanentError:
        pass  # the resume itself is gone; still record the id in the DLQ
    await dlq.push(deps.redis, deps.settings, resume_id)
    # Returning normally (not raising) tells ARQ the job is finished, so it
    # is NOT retried by ARQ as well.
    return "dead"


assert score_resume_job.__name__ == JOB_FUNCTION  # the name the API enqueues


def make_startup(settings: Settings):
    async def startup(ctx: dict) -> None:
        engine = build_engine(settings)
        ctx["engine"] = engine
        # ctx["redis"] is ARQ's own connection; reuse it for progress, DLQ etc.
        ctx["deps"] = JobDeps(settings=settings, session_factory=build_session_factory(engine),
                              redis=ctx["redis"], provider=build_provider(settings))
        logger.info("worker ready: provider=%s concurrency=%s", ctx["deps"].provider.name,
                    settings.worker_concurrency)

    return startup


async def shutdown(ctx: dict) -> None:
    await ctx["engine"].dispose()


def worker_options(settings: Settings) -> dict:
    """Everything ARQ needs, built from our Settings (tests pass their own)."""
    return {
        "functions": [score_resume_job],
        "queue_name": settings.queue_name,
        "redis_settings": RedisSettings.from_dsn(settings.redis_url),
        "on_startup": make_startup(settings),
        "on_shutdown": shutdown,
        "max_jobs": settings.worker_concurrency,
        "job_timeout": settings.job_timeout_seconds,
        # One more than our own limit: ARQ fails a job WITHOUT calling it once
        # job_try > max_tries, so our code must see the last try to DLQ it.
        "max_tries": settings.max_attempts + 1,
        "keep_result": 3600,
    }


class WorkerSettings:
    """Read by the `arq` command line."""


for _name, _value in worker_options(get_settings()).items():
    setattr(WorkerSettings, _name, _value)
