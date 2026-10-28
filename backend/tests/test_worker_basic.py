"""Queue + worker happy path with MockLLM, and the strict output validation."""
import json

import pytest

from app import progress
from app.errors import LLMBadOutput
from app.llm.base import parse_score
from app.llm.mock import MockLLM
from tests.conftest import resume_pdf, run_worker_until_idle, upload


async def test_upload_enqueues_one_job_per_resume_and_worker_scores_them(api_factory, recruiter, db, redis_client):
    client, app = await api_factory()
    files = [
        ("strong.pdf", resume_pdf("Asha Rao", "Python, FastAPI, PostgreSQL, Redis, Docker, AWS")),
        ("medium.pdf", resume_pdf("Ben Ito", "Python, Django, MySQL")),
        ("weak.pdf", resume_pdf("Cleo Park", "Photoshop, Illustrator, branding")),
    ]
    batch_id = (await upload(client, recruiter["headers"], files)).json()["batch_id"]
    assert await redis_client.zcard(app.state.settings.queue_name) == 3
    assert (await progress.counts(redis_client, batch_id))["queued"] == 3

    await run_worker_until_idle(app.state.settings)

    batch = (await client.get(f"/batches/{batch_id}", headers=recruiter["headers"])).json()
    assert batch["counts"] == {"done": 3}
    assert [r["filename"] for r in batch["resumes"]] == ["strong.pdf", "medium.pdf", "weak.pdf"]  # ranked
    top = batch["resumes"][0]["result"]
    assert 0 <= top["score"] <= 100 and len(top["strengths"]) == 3 and len(top["gaps"]) == 3
    assert (await progress.counts(redis_client, batch_id)) == {"queued": 0, "processing": 0, "done": 3, "dead": 0}


async def test_api_never_calls_the_llm(api_factory, recruiter, db):
    """Upload returns before any scoring happens: no results exist yet."""
    client, _ = await api_factory()
    await upload(client, recruiter["headers"], [("a.pdf", resume_pdf("A", "Python"))])
    assert await db.fetchval("SELECT count(*) FROM results") == 0


def test_valid_llm_json_is_accepted_even_inside_a_code_fence():
    raw = "```json\n" + json.dumps({"candidate_name": "Asha", "score": 88, "strengths": ["a", "b", "c"],
                                    "gaps": ["x", "y", "z"], "verdict": "Interview."}) + "\n```"
    assert parse_score(raw).score == 88


@pytest.mark.parametrize("raw", [
    "Sure! The candidate looks great.",                                              # prose, no JSON
    json.dumps({"score": 140, "strengths": ["a", "b", "c"], "gaps": ["x", "y", "z"], "verdict": "v"}),
    json.dumps({"score": 80, "strengths": ["a", "b"], "gaps": ["x", "y", "z"], "verdict": "v"}),
    json.dumps({"score": 80, "strengths": ["a", "b", "c"], "gaps": ["x", "y", "z"]}),  # no verdict
])
def test_invalid_llm_output_is_rejected(raw):
    with pytest.raises(LLMBadOutput):
        parse_score(raw)


async def test_mock_llm_ranks_by_keyword_overlap():
    llm = MockLLM(0, 0, 0)
    jd = "<job_description>python fastapi postgresql redis</job_description>"
    strong = json.loads(await llm.complete("", jd + "<resume>python fastapi postgresql redis</resume>"))
    weak = json.loads(await llm.complete("", jd + "<resume>photoshop branding</resume>"))
    assert strong["score"] > weak["score"]
