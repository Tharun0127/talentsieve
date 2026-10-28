"""Retries (transient), no retries (permanent), and the dead-letter queue."""
import json
import random

from app import dlq
from app.errors import LLMRateLimited
from app.llm.mock import MockLLM
from app.pdfgen import corrupt_pdf, text_pdf
from app.retry import backoff_delay
from tests.conftest import resume_pdf, run_worker_until_idle, upload


class FlakyLLM(MockLLM):
    """Fails with a 429 the first `failures` calls, then answers normally."""

    def __init__(self, failures: int):
        super().__init__(0, 0, 0)
        self.failures = failures

    async def complete(self, system, user):
        self.calls += 1
        if self.calls <= self.failures:
            raise LLMRateLimited("429 from flaky test provider")
        return json.dumps(self._answer(*self._split(user)))


def test_backoff_grows_exponentially_with_full_jitter():
    rng = random.Random(1)
    for attempt, ceiling in [(1, 2), (2, 4), (3, 8), (4, 16), (5, 32), (9, 60)]:
        delays = [backoff_delay(attempt, base=2, cap=60, rng=rng) for _ in range(500)]
        assert 0 <= min(delays) and max(delays) <= ceiling
        assert max(delays) > ceiling * 0.9  # really uses the whole range (jitter)


async def test_transient_failure_is_retried_until_it_succeeds(api_factory, recruiter, db):
    client, app = await api_factory()
    batch_id = (await upload(client, recruiter["headers"], [("a.pdf", resume_pdf("Asha", "Python"))])).json()["batch_id"]
    flaky = FlakyLLM(failures=2)

    await run_worker_until_idle(app.state.settings, provider=flaky)

    row = await db.fetchrow("SELECT status, attempts, error FROM resumes WHERE batch_id = $1", batch_id)
    assert (row["status"], row["attempts"]) == ("done", 3)
    assert row["error"] is None
    assert flaky.calls == 3


async def test_transient_failure_goes_to_dlq_after_five_attempts(api_factory, recruiter, db, redis_client):
    client, app = await api_factory()
    batch_id = (await upload(client, recruiter["headers"], [("a.pdf", resume_pdf("Asha", "Python"))])).json()["batch_id"]
    always_down = MockLLM(0, 0, failure_rate=1.0)

    await run_worker_until_idle(app.state.settings, provider=always_down)

    row = await db.fetchrow("SELECT id, status, attempts, error FROM resumes WHERE batch_id = $1", batch_id)
    assert (row["status"], row["attempts"]) == ("dead", 5)
    assert "gave up after 5 attempts" in row["error"]
    assert always_down.calls == 5
    assert await dlq.members(redis_client, app.state.settings) == [str(row["id"])]


async def test_corrupt_pdf_is_permanent_and_never_retried(api_factory, recruiter, db, redis_client):
    client, app = await api_factory()
    await upload(client, recruiter["headers"], [("broken.pdf", corrupt_pdf())])
    llm = MockLLM(0, 0, 0)

    await run_worker_until_idle(app.state.settings, provider=llm)

    row = await db.fetchrow("SELECT status, attempts, error FROM resumes")
    assert (row["status"], row["attempts"]) == ("dead", 1)
    assert row["error"].startswith("permanent error, not retried: could not read PDF")
    assert llm.calls == 0  # never paid for an LLM call on a file that cannot work
    assert len(await dlq.members(redis_client, app.state.settings)) == 1


async def test_pdf_without_text_is_permanent(api_factory, recruiter, db):
    client, app = await api_factory()
    await upload(client, recruiter["headers"], [("scan.pdf", text_pdf([]))])
    await run_worker_until_idle(app.state.settings)
    row = await db.fetchrow("SELECT status, error FROM resumes")
    assert row["status"] == "dead" and "no extractable text" in row["error"]


async def test_one_bad_resume_does_not_affect_the_others(api_factory, recruiter, db):
    client, app = await api_factory()
    files = [("good1.pdf", resume_pdf("A", "Python")), ("broken.pdf", corrupt_pdf()),
             ("good2.pdf", resume_pdf("B", "FastAPI"))]
    batch_id = (await upload(client, recruiter["headers"], files)).json()["batch_id"]
    await run_worker_until_idle(app.state.settings)
    batch = (await client.get(f"/batches/{batch_id}", headers=recruiter["headers"])).json()
    assert batch["counts"] == {"done": 2, "dead": 1}


async def test_dlq_endpoint_lists_and_retries_dead_jobs(api_factory, recruiter, db, redis_client):
    client, app = await api_factory()
    headers = recruiter["headers"]
    batch_id = (await upload(client, headers, [("a.pdf", resume_pdf("Asha", "Python"))])).json()["batch_id"]
    await run_worker_until_idle(app.state.settings, provider=MockLLM(0, 0, failure_rate=1.0))

    dead = (await client.get("/admin/dlq", headers=headers)).json()
    assert len(dead) == 1 and dead[0]["attempts"] == 5 and "gave up" in dead[0]["error"]

    # The LLM is healthy again: retry from the DLQ.
    assert (await client.post(f"/admin/dlq/{dead[0]['resume_id']}/retry", headers=headers)).json() == {"requeued": 1}
    # A double click does nothing.
    assert (await client.post(f"/admin/dlq/{dead[0]['resume_id']}/retry", headers=headers)).status_code == 404
    await run_worker_until_idle(app.state.settings, provider=MockLLM(0, 0, 0))

    row = await db.fetchrow("SELECT status, attempts, generation FROM resumes")
    assert (row["status"], row["attempts"], row["generation"]) == ("done", 1, 1)
    assert await dlq.members(redis_client, app.state.settings) == []
    batch = (await client.get(f"/batches/{batch_id}", headers=headers)).json()
    assert batch["counts"] == {"done": 1}


async def test_retry_all_requeues_every_dead_job(api_factory, recruiter, db):
    client, app = await api_factory()
    headers = recruiter["headers"]
    await upload(client, headers, [(f"{i}.pdf", resume_pdf(f"C{i}", "Python")) for i in range(3)])
    await run_worker_until_idle(app.state.settings, provider=MockLLM(0, 0, failure_rate=1.0))
    assert (await client.post("/admin/dlq/retry-all", headers=headers)).json() == {"requeued": 3}
    await run_worker_until_idle(app.state.settings, provider=MockLLM(0, 0, 0))
    assert await db.fetchval("SELECT count(*) FROM resumes WHERE status = 'done'") == 3


async def test_dlq_is_private_to_the_owner(api_factory, recruiter, db):
    client, app = await api_factory()
    await upload(client, recruiter["headers"], [("a.pdf", corrupt_pdf())])
    await run_worker_until_idle(app.state.settings)
    other = await client.post("/auth/signup", json={"email": "x@corp.com", "password": "password123"})
    headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert (await client.get("/admin/dlq", headers=headers)).json() == []
    assert (await client.post("/admin/dlq/retry-all", headers=headers)).json() == {"requeued": 0}
