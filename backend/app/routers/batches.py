"""Batches: upload resumes (returns immediately), list, view ranked results.

The API never parses PDFs or calls the LLM. Upload = save files, insert
rows, enqueue one job per resume, return 202 with the batch id. Everything
slow happens in the workers.
"""
import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import func, select

from app.auth import current_user_id
from app.models import QUEUED, Batch, Resume, Result

router = APIRouter(prefix="/batches", tags=["batches"])


async def read_upload(upload: UploadFile, max_bytes: int) -> bytes:
    data = await upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(413, f"{upload.filename}: larger than {max_bytes // (1024 * 1024)} MB")
    return data


def validate_files(files: list[UploadFile], max_files: int) -> None:
    if not files:
        raise HTTPException(422, "upload at least one PDF")
    if len(files) > max_files:
        raise HTTPException(422, f"at most {max_files} resumes per batch")
    for upload in files:
        if not (upload.filename or "").lower().endswith(".pdf"):
            raise HTTPException(422, f"{upload.filename}: only .pdf files are accepted")


def save_file(upload_dir: str, batch_id: uuid.UUID, resume_id: uuid.UUID, data: bytes) -> str:
    """Write to a shared volume the workers can read (api and worker mount it)."""
    folder = os.path.join(upload_dir, str(batch_id))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{resume_id}.pdf")
    with open(path, "wb") as handle:
        handle.write(data)
    return path


@router.post("", status_code=202)
async def create_batch(
    request: Request,
    title: str = Form(..., min_length=1, max_length=200),
    job_description: str = Form(..., min_length=20),
    files: list[UploadFile] = File(...),
    user_id: int = Depends(current_user_id),
):
    settings = request.app.state.settings
    if len(job_description) > settings.max_job_description_chars:
        raise HTTPException(422, "job description is too long")
    validate_files(files, settings.max_files_per_batch)

    batch = Batch(id=uuid.uuid4(), user_id=user_id, title=title.strip(),
                  job_description=job_description.strip(), total=len(files))
    resumes = []
    for upload in files:
        data = await read_upload(upload, settings.max_file_bytes)
        if not data:
            raise HTTPException(422, f"{upload.filename}: empty file")
        resume_id = uuid.uuid4()
        # Content is NOT validated here: a corrupt PDF is the worker's job to
        # detect, and goes to the dead-letter queue as a permanent failure.
        path = save_file(settings.upload_dir, batch.id, resume_id, data)
        resumes.append(Resume(id=resume_id, batch_id=batch.id, filename=upload.filename[:255],
                              file_path=path, status=QUEUED))

    async with request.app.state.session_factory() as session:
        session.add(batch)
        await session.flush()  # the batch row must exist before its resumes (FK)
        session.add_all(resumes)
        await session.commit()
    await request.app.state.enqueue_batch(batch.id, [r.id for r in resumes])
    return {"batch_id": str(batch.id), "total": batch.total}


@router.get("")
async def list_batches(request: Request, user_id: int = Depends(current_user_id)):
    async with request.app.state.session_factory() as session:
        batches = (await session.scalars(
            select(Batch).where(Batch.user_id == user_id).order_by(Batch.created_at.desc())
        )).all()
    return [{"id": str(b.id), "title": b.title, "total": b.total, "created_at": b.created_at.isoformat()}
            for b in batches]


async def owned_batch(request: Request, batch_id: uuid.UUID, user_id: int) -> Batch:
    async with request.app.state.session_factory() as session:
        batch = await session.get(Batch, batch_id)
    if batch is None or batch.user_id != user_id:
        raise HTTPException(404, "batch not found")
    return batch


@router.get("/{batch_id}")
async def get_batch(batch_id: uuid.UUID, request: Request, user_id: int = Depends(current_user_id)):
    """The batch with every resume, ranked by score (unscored ones last)."""
    batch = await owned_batch(request, batch_id, user_id)
    query = (
        select(Resume, Result)
        .outerjoin(Result, Result.resume_id == Resume.id)
        .where(Resume.batch_id == batch_id)
        .order_by(Result.score.desc().nulls_last(), Resume.filename)
    )
    async with request.app.state.session_factory() as session:
        rows = (await session.execute(query)).all()
        counts = dict((await session.execute(
            select(Resume.status, func.count()).where(Resume.batch_id == batch_id).group_by(Resume.status)
        )).all())
    return {
        "id": str(batch.id), "title": batch.title, "job_description": batch.job_description,
        "total": batch.total, "created_at": batch.created_at.isoformat(), "counts": counts,
        "resumes": [resume_out(resume, result) for resume, result in rows],
    }


def resume_out(resume: Resume, result: Result | None) -> dict:
    out = {"id": str(resume.id), "filename": resume.filename, "status": resume.status,
           "attempts": resume.attempts, "error": resume.error, "result": None}
    if result is not None:
        out["result"] = {"score": result.score, "strengths": result.strengths, "gaps": result.gaps,
                         "verdict": result.verdict, "candidate_name": result.candidate_name,
                         "provider": result.provider, "cached": result.cached}
    return out
