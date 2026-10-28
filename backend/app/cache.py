"""LLM result cache: never pay twice for the same resume + job description.

Key = sha256(PROMPT_VERSION + provider + resume text + job description).
Recruiters often re-upload the same CVs (a new batch for the same role, a
retry from the DLQ), and each LLM call costs money and rate-limit budget.

- Text is normalised (whitespace collapsed) before hashing, so the same CV
  exported twice still matches.
- The prompt version and provider are in the key: change the prompt or the
  model and old answers are no longer reused.
- Stored in Postgres (llm_cache), not Redis: it saves real money, so it
  should survive a Redis restart.
"""
import hashlib

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.llm.base import ScoreResult
from app.models import LLMCache
from app.pdf import normalize

PROMPT_VERSION = "v1"


def cache_key(resume_text: str, job_description: str, provider: str) -> str:
    parts = [PROMPT_VERSION, provider, normalize(resume_text), normalize(job_description)]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


async def get(session_factory: async_sessionmaker, key: str) -> ScoreResult | None:
    async with session_factory() as session, session.begin():
        response = await session.scalar(select(LLMCache.response).where(LLMCache.key == key))
        if response is None:
            return None
        await session.execute(update(LLMCache).where(LLMCache.key == key).values(hits=LLMCache.hits + 1))
    return ScoreResult.model_validate(response)


async def put(session_factory: async_sessionmaker, key: str, result: ScoreResult, provider: str) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            insert(LLMCache).values(key=key, response=result.model_dump(), provider=provider)
            .on_conflict_do_nothing(index_elements=["key"])
        )
