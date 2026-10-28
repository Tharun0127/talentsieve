"""What one job does: score one resume. Kept free of ARQ so it is easy to
test; app/worker.py wraps it with retries and the dead-letter queue.

Steps:
  1. Load the resume and its batch.        (gone?          -> permanent)
  2. Mark it "processing".
  3. Extract text from the PDF.            (corrupt/empty? -> permanent)
  4. Ask the LLM, validate the JSON.       (timeout/429/5xx/bad JSON -> transient)
  5. UPSERT the result, mark "done".       (idempotent: safe to run twice)
"""
import logging
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import cache, pdf, progress, ratelimit
from app.config import Settings
from app.errors import MissingRecord
from app.llm.base import LLMProvider, ScoreResult, score_resume
from app.models import DONE, PROCESSING, Batch, Resume, Result

logger = logging.getLogger("talentsieve.jobs")


@dataclass
class JobDeps:
    settings: Settings
    session_factory: async_sessionmaker
    redis: Redis
    provider: LLMProvider


async def load(deps: JobDeps, resume_id: uuid.UUID) -> tuple[Resume, Batch]:
    async with deps.session_factory() as session:
        resume = await session.get(Resume, resume_id)
        batch = await session.get(Batch, resume.batch_id) if resume else None
    if resume is None or batch is None:
        raise MissingRecord(f"resume {resume_id} no longer exists")
    return resume, batch


async def set_status(deps: JobDeps, resume: Resume, status: str, *, error: str | None = None,
                     attempts: int | None = None) -> None:
    """Update the source of truth (Postgres), then the live progress (Redis)."""
    values = {"status": status, "error": error, "updated_at": func.now()}
    if attempts is not None:
        values["attempts"] = attempts
    async with deps.session_factory() as session, session.begin():
        await session.execute(update(Resume).where(Resume.id == resume.id).values(**values))
    await progress.move(deps.redis, resume.batch_id, resume.id, status)


async def save_result(deps: JobDeps, resume_id: uuid.UUID, result: ScoreResult, provider: str,
                      cached: bool) -> None:
    """INSERT ... ON CONFLICT (resume_id) DO UPDATE.

    If this job already ran once (the worker died after this line but before
    ARQ recorded the job as finished), the second run overwrites the same
    row instead of adding a duplicate.
    """
    values = {"resume_id": resume_id, "score": result.score, "strengths": result.strengths,
              "gaps": result.gaps, "verdict": result.verdict, "candidate_name": result.candidate_name,
              "provider": provider, "cached": cached}
    statement = insert(Result).values(**values)
    statement = statement.on_conflict_do_update(
        constraint="uq_results_resume_id",
        set_={**{k: statement.excluded[k] for k in values if k != "resume_id"}, "updated_at": func.now()},
    )
    async with deps.session_factory() as session, session.begin():
        await session.execute(statement)


async def already_done(deps: JobDeps, resume: Resume) -> bool:
    if resume.status != DONE:
        return False
    async with deps.session_factory() as session:
        return await session.scalar(select(Result.id).where(Result.resume_id == resume.id)) is not None


async def get_score(deps: JobDeps, resume_text: str, job_description: str) -> tuple[ScoreResult, bool]:
    """Cache first (free), otherwise wait for a global rate-limit slot and
    ask the LLM. Returns (result, came_from_cache)."""
    key = cache.cache_key(resume_text, job_description, deps.provider.name)
    cached = await cache.get(deps.session_factory, key)
    if cached is not None:
        return cached, True

    settings = deps.settings
    waited = await ratelimit.acquire(deps.redis, settings.llm_rate_limit_per_minute, settings.llm_rate_window_seconds)
    if waited > 1:
        logger.info("waited %.1fs for a global LLM rate-limit slot", waited)
    result = await score_resume(deps.provider, resume_text, job_description)
    await cache.put(deps.session_factory, key, result, deps.provider.name)
    return result, False


async def process_resume(deps: JobDeps, resume_id: uuid.UUID, attempt: int) -> str:
    resume, batch = await load(deps, resume_id)
    if await already_done(deps, resume):
        # A duplicate delivery of a job that already finished: nothing to do.
        logger.info("resume %s already done; skipping duplicate run", resume_id)
        return "already_done"

    await set_status(deps, resume, PROCESSING, attempts=attempt)
    text = pdf.extract_text(resume.file_path)
    result, cached = await get_score(deps, text, batch.job_description)
    await save_result(deps, resume.id, result, deps.provider.name, cached)
    await set_status(deps, resume, DONE, attempts=attempt)
    return "done"
