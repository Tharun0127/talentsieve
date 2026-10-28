"""Dead-letter queue: jobs that will not succeed by themselves.

A job lands here when:
- it hit a PERMANENT error (corrupt PDF, no text, rejected request), or
- it hit TRANSIENT errors MAX_ATTEMPTS times in a row.

Two records, on purpose:
- Postgres: resume.status = 'dead' and resume.error = the reason. Durable,
  queryable, shown in the UI.
- Redis list `<queue>:dlq` of resume ids, newest first. The operational view
  ("what is dead right now?") that tools and the retry endpoint use.

Writing it twice for the same resume (a duplicate run) does not duplicate
the entry: LREM first, then LPUSH.
"""
import uuid

from redis.asyncio import Redis

from app.config import Settings


def dlq_key(settings: Settings) -> str:
    return f"{settings.queue_name}:dlq"


async def push(redis: Redis, settings: Settings, resume_id: uuid.UUID) -> None:
    async with redis.pipeline(transaction=True) as pipe:
        pipe.lrem(dlq_key(settings), 0, str(resume_id))
        pipe.lpush(dlq_key(settings), str(resume_id))
        await pipe.execute()


async def remove(redis: Redis, settings: Settings, resume_id: uuid.UUID) -> int:
    return await redis.lrem(dlq_key(settings), 0, str(resume_id))


async def members(redis: Redis, settings: Settings) -> list[str]:
    raw = await redis.lrange(dlq_key(settings), 0, -1)
    return [r.decode() if isinstance(r, bytes) else r for r in raw]
