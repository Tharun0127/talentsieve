"""SSE progress, the global rate limiter, and the LLM result cache."""
import asyncio
import json
import time

from redis.asyncio import Redis

from app import cache, ratelimit
from app.llm.mock import MockLLM
from tests.conftest import TEST_REDIS_URL, resume_pdf, run_worker_until_idle, upload


# --- rate limiter -------------------------------------------------------------
async def test_global_limit_holds_across_many_workers(redis_client):
    """4 separate Redis connections stand in for 4 worker processes, each
    with 5 jobs. Limit: 5 calls per 1 s window across ALL of them."""
    limit, window = 5, 1.0
    clients = [Redis.from_url(TEST_REDIS_URL) for _ in range(4)]
    granted: list[float] = []

    async def job(client):
        await ratelimit.acquire(client, limit, window)
        granted.append(time.monotonic())

    started = time.monotonic()
    try:
        await asyncio.gather(*(job(c) for c in clients for _ in range(5)))
    finally:
        for c in clients:
            await c.aclose()

    granted.sort()
    # In any sliding 1 s window, at most 5 grants (small slack for timer jitter).
    for i, t in enumerate(granted):
        in_window = [g for g in granted[i:] if g - t < window - 0.02]
        assert len(in_window) <= limit
    # 20 calls at 5 per second needs at least 3 full windows.
    assert time.monotonic() - started >= 3 * window - 0.1


async def test_limiter_reports_how_long_to_wait(redis_client):
    for _ in range(3):
        assert await ratelimit.try_acquire(redis_client, limit=3, window_seconds=2) == 0
    wait_ms = await ratelimit.try_acquire(redis_client, limit=3, window_seconds=2)
    assert 1500 < wait_ms <= 2000


# --- cache ------------------------------------------------------------------
def test_cache_key_ignores_whitespace_but_not_content():
    jd = "Python backend engineer"
    assert cache.cache_key("Asha  Rao\nPython", jd, "mock") == cache.cache_key("Asha Rao Python", jd, "mock")
    assert cache.cache_key("Asha Rao Python", jd, "mock") != cache.cache_key("Asha Rao Go", jd, "mock")
    assert cache.cache_key("Asha", jd, "mock") != cache.cache_key("Asha", jd + " AWS", "mock")
    assert cache.cache_key("Asha", jd, "mock") != cache.cache_key("Asha", jd, "anthropic")


async def test_reuploading_the_same_resumes_does_not_call_the_llm_again(api_factory, recruiter, db):
    client, app = await api_factory()
    files = [(f"c{i}.pdf", resume_pdf(f"Candidate {i}", "Python, FastAPI")) for i in range(3)]
    llm = MockLLM(0, 0, 0)

    first = (await upload(client, recruiter["headers"], files)).json()["batch_id"]
    await run_worker_until_idle(app.state.settings, provider=llm)
    second = (await upload(client, recruiter["headers"], files)).json()["batch_id"]
    await run_worker_until_idle(app.state.settings, provider=llm)

    assert llm.calls == 3  # not 6
    rows = (await client.get(f"/batches/{second}", headers=recruiter["headers"])).json()["resumes"]
    assert all(r["result"]["cached"] for r in rows)
    first_scores = sorted(r["result"]["score"] for r in
                          (await client.get(f"/batches/{first}", headers=recruiter["headers"])).json()["resumes"])
    assert sorted(r["result"]["score"] for r in rows) == first_scores
    assert await db.fetchval("SELECT sum(hits) FROM llm_cache") == 3


async def test_cache_hit_does_not_use_a_rate_limit_slot(api_factory, recruiter, redis_client):
    client, app = await api_factory()
    files = [("c.pdf", resume_pdf("Asha", "Python"))]
    await upload(client, recruiter["headers"], files)
    await run_worker_until_idle(app.state.settings)
    used = await redis_client.zcard(ratelimit.KEY)
    await upload(client, recruiter["headers"], files)
    await run_worker_until_idle(app.state.settings)
    assert await redis_client.zcard(ratelimit.KEY) == used


# --- SSE ----------------------------------------------------------------------
def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = [l for l in block.splitlines() if not l.startswith(":")]
        if not lines:
            continue
        name = next((l[7:] for l in lines if l.startswith("event: ")), "message")
        data = next(l[6:] for l in lines if l.startswith("data: "))
        events.append((name, json.loads(data)))
    return events


async def test_progress_streams_over_sse_until_complete(api_factory, recruiter):
    client, app = await api_factory()
    files = [(f"c{i}.pdf", resume_pdf(f"C{i}", "Python")) for i in range(4)]
    batch_id = (await upload(client, recruiter["headers"], files)).json()["batch_id"]
    url = f"/batches/{batch_id}/events?token={recruiter['token']}"

    async def slow_worker():
        await asyncio.sleep(0.6)  # let the stream send the "all queued" state first
        await run_worker_until_idle(app.state.settings, provider=MockLLM(0.3, 0.3, 0))

    response, _ = await asyncio.gather(client.get(url), slow_worker())

    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert events[0] == ("message", {"queued": 4, "processing": 0, "done": 0, "dead": 0, "total": 4})
    assert events[-1][0] == "complete" and events[-1][1]["done"] == 4
    done_counts = [data["done"] for _, data in events]
    assert done_counts == sorted(done_counts)  # never goes backwards


async def test_sse_requires_a_valid_token(api_factory, recruiter):
    client, _ = await api_factory()
    batch_id = (await upload(client, recruiter["headers"], [("a.pdf", resume_pdf("A", "Go"))])).json()["batch_id"]
    assert (await client.get(f"/batches/{batch_id}/events?token=nope")).status_code == 401
