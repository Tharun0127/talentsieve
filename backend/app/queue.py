"""Enqueueing jobs. Used by the API (uploads) and the DLQ retry endpoint."""
import uuid

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app import progress
from app.config import Settings
from app.models import QUEUED

JOB_FUNCTION = "score_resume_job"


def job_id(resume_id: uuid.UUID | str, generation: int) -> str:
    """Deterministic job id. ARQ refuses to enqueue a job id that already
    exists, so a double-submitted upload cannot create two jobs for one
    resume. `generation` changes only when the DLQ retry endpoint
    deliberately re-runs a dead job."""
    return f"score:{resume_id}:{generation}"


async def connect(settings: Settings) -> ArqRedis:
    return await create_pool(RedisSettings.from_dsn(settings.redis_url), default_queue_name=settings.queue_name)


async def enqueue_resume(pool: ArqRedis, settings: Settings, batch_id, resume_id, generation: int = 0) -> None:
    await progress.move(pool, batch_id, resume_id, QUEUED)
    await pool.enqueue_job(JOB_FUNCTION, str(resume_id), _job_id=job_id(resume_id, generation),
                           _queue_name=settings.queue_name)
