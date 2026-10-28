"""The ARQ worker: `arq app.worker.WorkerSettings`.

ARQ in one paragraph: a job is a Redis entry in a sorted set (the queue).
A worker polls the queue, claims a job by setting an "in-progress" key, runs
the async function, and only REMOVES the job from the queue when the
function has finished. That last detail is what makes crashes safe (see
docs/DECISIONS.md, "Worker crash safety").
"""
import logging
import uuid

from arq.connections import RedisSettings

from app.config import Settings, get_settings
from app.db import build_engine, build_session_factory
from app.jobs import JobDeps, process_resume
from app.llm import build_provider
from app.queue import JOB_FUNCTION

logger = logging.getLogger("talentsieve.worker")


async def score_resume_job(ctx: dict, resume_id: str) -> str:
    deps: JobDeps = ctx["deps"]
    return await process_resume(deps, uuid.UUID(resume_id), attempt=ctx["job_try"])


assert score_resume_job.__name__ == JOB_FUNCTION  # the name the API enqueues


def make_startup(settings: Settings):
    async def startup(ctx: dict) -> None:
        engine = build_engine(settings)
        ctx["engine"] = engine
        # ctx["redis"] is ARQ's own connection; reuse it for progress etc.
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
