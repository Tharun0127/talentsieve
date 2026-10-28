"""Retry timing: exponential backoff with full jitter.

attempt 1 fails -> wait random(0, 2 s)
attempt 2 fails -> wait random(0, 4 s)
attempt 3 fails -> wait random(0, 8 s)   ... capped at 60 s

Exponential: give a struggling API (429, 503) more room each time.
Jitter: if 50 jobs hit a rate limit together, a fixed delay would make all
50 retry together and hit it again. Random delays spread them out. "Full"
jitter (0..cap rather than cap +/- 10%) spreads them the most.
"""
import random


def backoff_delay(attempt: int, base: float, cap: float, rng: random.Random = random) -> float:
    ceiling = min(cap, base * 2 ** (attempt - 1))
    return rng.uniform(0, ceiling)
