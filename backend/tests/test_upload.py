"""Auth and the non-blocking upload endpoint."""
import os

from app.pdfgen import corrupt_pdf
from tests.conftest import resume_pdf, upload


async def test_signup_and_login(api_factory):
    client, _ = await api_factory()
    body = {"email": "Hire@Corp.com", "password": "password123"}
    assert (await client.post("/auth/signup", json=body)).status_code == 201
    login = await client.post("/auth/login", json={"email": "hire@corp.com", "password": "password123"})
    assert login.status_code == 200 and login.json()["access_token"]


async def test_upload_returns_immediately_with_a_batch_id(api_factory, recruiter, db):
    client, app = await api_factory()
    files = [(f"cand{i}.pdf", resume_pdf(f"Candidate {i}", "Python, FastAPI")) for i in range(5)]

    response = await upload(client, recruiter["headers"], files)

    assert response.status_code == 202
    batch_id = response.json()["batch_id"]
    assert response.json()["total"] == 5
    rows = await db.fetch("SELECT status, file_path FROM resumes WHERE batch_id = $1", batch_id)
    assert len(rows) == 5 and {r["status"] for r in rows} == {"queued"}
    assert all(os.path.exists(r["file_path"]) for r in rows)


async def test_corrupt_pdf_is_accepted_at_upload(api_factory, recruiter):
    """The API does no content validation; the worker decides (and DLQs it)."""
    client, _ = await api_factory()
    response = await upload(client, recruiter["headers"], [("broken.pdf", corrupt_pdf())])
    assert response.status_code == 202


async def test_upload_limits(api_factory, recruiter):
    client, _ = await api_factory(max_files_per_batch=3, max_file_bytes=1000)
    headers = recruiter["headers"]
    too_many = [(f"c{i}.pdf", b"%PDF small") for i in range(4)]
    assert (await upload(client, headers, too_many)).status_code == 422
    assert (await upload(client, headers, [("cv.docx", b"word")])).status_code == 422
    assert (await upload(client, headers, [("big.pdf", b"x" * 2000)])).status_code == 413
    assert (await upload(client, headers, [("ok.pdf", b"%PDF")], jd="too short")).status_code == 422


async def test_batches_are_private_to_their_recruiter(api_factory, recruiter, db):
    client, _ = await api_factory()
    batch_id = (await upload(client, recruiter["headers"], [("a.pdf", resume_pdf("A", "Go"))])).json()["batch_id"]
    other = await client.post("/auth/signup", json={"email": "other@corp.com", "password": "password123"})
    headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert (await client.get(f"/batches/{batch_id}", headers=headers)).status_code == 404
    assert (await client.get("/batches", headers=headers)).json() == []


async def test_requires_auth(api_factory):
    client, _ = await api_factory()
    assert (await client.get("/batches")).status_code == 401
