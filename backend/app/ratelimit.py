"""Global LLM rate limit: all workers TOGETHER stay under N requests/minute.

Each worker process has its own memory, so a per-process limiter would let
4 workers make 4x the allowed calls. The limit therefore lives in Redis,
shared by everyone.

Algorithm: sliding-window log in a sorted set.
    member = one request, score = when it was made (ms)
To ask for a slot (atomically, in one Lua script):
    1. drop entries older than the window
    2. fewer than LIMIT left?  -> add ours, go
       otherwise               -> answer "wait until the oldest one leaves"

It uses Redis's own clock (TIME), not each worker's, so workers on machines
whose clocks disagree still share one consistent window.

Compared with a fixed window ("max 120 per calendar minute"), a sliding
window cannot let 240 calls through around a minute boundary.
"""
import asyncio
import random
import uuid

from redis.asyncio import Redis

ACQUIRE = """
local key, window_ms, limit = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2])
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window_ms)
if redis.call('ZCARD', key) < limit then
    redis.call('ZADD', key, now, ARGV[3])
    redis.call('PEXPIRE', key, window_ms)
    return 0
end
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
return math.max(1, tonumber(oldest[2]) + window_ms - now)
"""

KEY = "llm:ratelimit"


async def try_acquire(redis: Redis, limit: int, window_seconds: float) -> int:
    """0 if we got a slot, otherwise milliseconds until one frees up."""
    return int(await redis.eval(ACQUIRE, 1, KEY, int(window_seconds * 1000), limit, uuid.uuid4().hex))


async def acquire(redis: Redis, limit: int, window_seconds: float) -> float:
    """Wait until a slot is free. Returns seconds spent waiting."""
    waited = 0.0
    while (wait_ms := await try_acquire(redis, limit, window_seconds)) > 0:
        # A little jitter so waiting workers do not all retry in the same ms.
        pause = wait_ms / 1000 + random.uniform(0, 0.05)
        await asyncio.sleep(pause)
        waited += pause
    return waited
