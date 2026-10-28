"""At-least-once delivery means a job can run twice. Prove that is harmless,
and prove that a worker killed mid-job does not lose the job."""
import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest

from app import jobs, progress
from app.jobs import JobDeps, process_resume
from app.llm.mock import MockLLM
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, resume_pdf, run_worker_until_idle, upload


async def deps_for(app, provider) -> JobDeps:
    state = app.state
    return JobDeps(settings=state.settings, session_factory=state.session_factory, redis=state.redis,
                   provider=provider)


async def test_job_that_runs_twice_after_crash_before_ack_has_one_result(api_factory, recruiter, db, redis_client,
                                                                         monkeypatch):
    """Worker writes the result, then dies before marking the job done.
    ARQ runs the job again. Still exactly one result row, counts correct."""
    client, app = await api_factory()
    batch_id = (await upload(client, recruiter["headers"], [("a.pdf", resume_pdf("Asha", "Python"))])).json()["batch_id"]
    resume_id = await db.fetchval("SELECT id FROM resumes")
    llm = MockLLM(0, 0, 0)
    deps = await deps_for(app, llm)

    real_set_status = jobs.set_status

    async def crash_when_marking_done(deps, resume, status, **kwargs):
        if status == "done":
            raise RuntimeError("worker killed right after saving the result")
        return await real_set_status(deps, resume, status, **kwargs)

    monkeypatch.setattr(jobs, "set_status", crash_when_marking_done)
    with pytest.raises(RuntimeError):
        await process_resume(deps, resume_id, attempt=1)
    assert await db.fetchval("SELECT count(*) FROM results") == 1          # result written...
    assert await db.fetchval("SELECT status FROM resumes") == "processing"  # ...but never acknowledged

    monkeypatch.setattr(jobs, "set_status", real_set_status)
    assert await process_resume(deps, resume_id, attempt=2) == "done"       # the redelivery

    assert await db.fetchval("SELECT count(*) FROM results") == 1          # UPSERT, not a duplicate
    assert await db.fetchval("SELECT status FROM resumes") == "done"
    assert await progress.counts(redis_client, batch_id) == {"queued": 0, "processing": 0, "done": 1, "dead": 0}


async def test_duplicate_delivery_of_a_finished_job_does_no_work(api_factory, recruiter, db):
    client, app = await api_factory()
    await upload(client, recruiter["headers"], [("a.pdf", resume_pdf("Asha", "Python"))])
    resume_id = await db.fetchval("SELECT id FROM resumes")
    llm = MockLLM(0, 0, 0)
    deps = await deps_for(app, llm)

    assert await process_resume(deps, resume_id, attempt=1) == "done"
    assert await process_resume(deps, resume_id, attempt=1) == "already_done"
    assert llm.calls == 1  # the duplicate did not pay for a second LLM call


async def test_two_concurrent_runs_of_the_same_job_store_one_result(api_factory, recruiter, db, redis_client):
    client, app = await api_factory()
    batch_id = (await upload(client, recruiter["headers"], [("a.pdf", resume_pdf("Asha", "Python"))])).json()["batch_id"]
    resume_id = await db.fetchval("SELECT id FROM resumes")
    deps = await deps_for(app, MockLLM(0.05, 0.05, 0))

    await asyncio.gather(*(process_resume(deps, resume_id, attempt=1) for _ in range(5)))

    assert await db.fetchval("SELECT count(*) FROM results") == 1
    assert await progress.counts(redis_client, batch_id) == {"queued": 0, "processing": 0, "done": 1, "dead": 0}


async def test_duplicate_enqueue_of_the_same_job_id_is_ignored(api_factory, recruiter, db, redis_client):
    client, app = await api_factory()
    await upload(client, recruiter["headers"], [("a.pdf", resume_pdf("Asha", "Python"))])
    resume_id = await db.fetchval("SELECT id FROM resumes")
    batch_id = await db.fetchval("SELECT batch_id FROM resumes")
    from app import queue
    await queue.enqueue_resume(app.state.arq, app.state.settings, batch_id, resume_id)  # same job id again
    assert await redis_client.zcard(app.state.settings.queue_name) == 1
    await run_worker_until_idle(app.state.settings)
    assert await db.fetchval("SELECT count(*) FROM results") == 1


# --- a REAL crash: SIGKILL a worker process in the middle of a job -------------
def start_worker_process(settings) -> subprocess.Popen:
    env = {
        **os.environ,
        "DATABASE_URL": TEST_DATABASE_URL, "REDIS_URL": TEST_REDIS_URL,
        "QUEUE_NAME": settings.queue_name, "UPLOAD_DIR": settings.upload_dir,
        "LLM_PROVIDER": "mock", "MOCK_LLM_MIN_DELAY": "3", "MOCK_LLM_MAX_DELAY": "3",
        "MOCK_LLM_FAILURE_RATE": "0", "LLM_RATE_LIMIT_PER_MINUTE": "100000",
        # Small timeout so ARQ's in-progress lock (timeout + 10 s) expires quickly.
        "JOB_TIMEOUT_SECONDS": "5", "WORKER_CONCURRENCY": "1",
    }
    return subprocess.Popen([sys.executable, "-m", "arq", "app.worker.WorkerSettings"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def wait_for_status(db, status: str, timeout: float) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if await db.fetchval("SELECT status FROM resumes") == status:
            return time.monotonic() - started
        await asyncio.sleep(0.1)
    raise AssertionError(f"resume never reached {status!r} within {timeout}s")


async def test_killed_worker_mid_job_is_picked_up_by_another_worker(api_factory, recruiter, db, redis_client):
    client, app = await api_factory()
    settings = app.state.settings
    await upload(client, recruiter["headers"], [("a.pdf", resume_pdf("Asha", "Python, FastAPI"))])
    job_ids = await redis_client.zrange(settings.queue_name, 0, -1)

    worker_a = start_worker_process(settings)
    await wait_for_status(db, "processing", timeout=20)
    worker_a.send_signal(signal.SIGKILL)  # no cleanup, no goodbye: like an OOM kill
    worker_a.wait()
    killed_at = time.monotonic()

    # The job was NOT lost: it is still in the queue, and the dead worker's
    # in-progress lock is still there (it expires after job_timeout + 10 s).
    assert await redis_client.zrange(settings.queue_name, 0, -1) == job_ids
    assert await redis_client.exists(f"arq:in-progress:{job_ids[0]}")
    assert await db.fetchval("SELECT count(*) FROM results") == 0

    worker_b = start_worker_process(settings)
    try:
        await wait_for_status(db, "done", timeout=40)
    finally:
        worker_b.send_signal(signal.SIGTERM)
        worker_b.wait(timeout=15)
    recovered_after = time.monotonic() - killed_at

    row = await db.fetchrow("SELECT attempts, status FROM resumes")
    assert row["status"] == "done"
    assert row["attempts"] == 2  # the killed run counted as attempt 1
    assert await db.fetchval("SELECT count(*) FROM results") == 1
    assert await redis_client.zcard(settings.queue_name) == 0
    print(f"\nrecovered {recovered_after:.1f}s after the kill (lock = job_timeout 5 s + 10 s)")
    assert 10 < recovered_after < 30
