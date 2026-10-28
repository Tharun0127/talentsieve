"""Batch progress in Redis: counts per status, safe against repeats.

Two hashes per batch:
    progress:<batch>:status   resume_id -> current status
    progress:<batch>:counts   status    -> number of resumes in it

Every status change goes through one Lua script that MOVES a resume from its
old status to the new one. Running the same change twice (a job that runs
twice after a worker crash, a duplicate message) is a no-op, so the counts
can never drift. Plain HINCRBY counters would double-count in exactly the
situations this project is about.
"""
import uuid

from redis.asyncio import Redis

from app.models import STATUSES

TTL_SECONDS = 7 * 24 * 3600

MOVE_STATUS = """
local old = redis.call('HGET', KEYS[1], ARGV[1])
if old == ARGV[2] then
    return 0
end
if old then
    redis.call('HINCRBY', KEYS[2], old, -1)
end
redis.call('HINCRBY', KEYS[2], ARGV[2], 1)
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[3])
return 1
"""


def status_key(batch_id: uuid.UUID | str) -> str:
    return f"progress:{batch_id}:status"


def counts_key(batch_id: uuid.UUID | str) -> str:
    return f"progress:{batch_id}:counts"


async def move(redis: Redis, batch_id, resume_id, status: str) -> bool:
    keys = [status_key(batch_id), counts_key(batch_id)]
    return bool(await redis.eval(MOVE_STATUS, 2, *keys, str(resume_id), status, TTL_SECONDS))


async def counts(redis: Redis, batch_id) -> dict[str, int]:
    raw = await redis.hgetall(counts_key(batch_id))
    return {status: int(raw.get(status, 0)) for status in STATUSES}
