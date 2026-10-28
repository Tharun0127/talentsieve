"""Dead-letter queue admin: list failed jobs and send them back to the queue.

Scoped to the caller's own batches: a recruiter administers their jobs only.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select, update

from app import dlq, queue
from app.auth import current_user_id
from app.models import DEAD, QUEUED, Batch, Resume

router = APIRouter(prefix="/admin/dlq", tags=["admin"])


@router.get("")
async def list_dead_letters(request: Request, user_id: int = Depends(current_user_id)):
    state = request.app.state
    ids = [uuid.UUID(i) for i in await dlq.members(state.redis, state.settings)]
    if not ids:
        return []
    query = (
        select(Resume, Batch.title)
        .join(Batch, Batch.id == Resume.batch_id)
        .where(Resume.id.in_(ids), Batch.user_id == user_id)
    )
    async with state.session_factory() as session:
        rows = {resume.id: (resume, title) for resume, title in (await session.execute(query)).all()}
    # Keep the DLQ's own order (newest first).
    return [
        {"resume_id": str(rid), "batch_id": str(rows[rid][0].batch_id), "batch_title": rows[rid][1],
         "filename": rows[rid][0].filename, "attempts": rows[rid][0].attempts, "error": rows[rid][0].error}
        for rid in ids if rid in rows
    ]


async def requeue(request: Request, resume: Resume) -> None:
    """Back to the queue with a NEW generation (so a fresh ARQ job id) and a
    fresh attempt budget."""
    state = request.app.state
    async with state.session_factory() as session, session.begin():
        generation = await session.scalar(
            update(Resume).where(Resume.id == resume.id, Resume.status == DEAD)
            .values(status=QUEUED, attempts=0, error=None, generation=Resume.generation + 1,
                    updated_at=func.now())
            .returning(Resume.generation)
        )
    if generation is None:
        return  # someone else already re-queued it (double click): do nothing
    await dlq.remove(state.redis, state.settings, resume.id)
    await queue.enqueue_resume(state.arq, state.settings, resume.batch_id, resume.id, generation)


async def owned_dead_resumes(request: Request, user_id: int, resume_id: uuid.UUID | None = None) -> list[Resume]:
    query = select(Resume).join(Batch, Batch.id == Resume.batch_id).where(
        Batch.user_id == user_id, Resume.status == DEAD
    )
    if resume_id is not None:
        query = query.where(Resume.id == resume_id)
    async with request.app.state.session_factory() as session:
        return list((await session.scalars(query)).all())


@router.post("/{resume_id}/retry")
async def retry_one(resume_id: uuid.UUID, request: Request, user_id: int = Depends(current_user_id)):
    resumes = await owned_dead_resumes(request, user_id, resume_id)
    if not resumes:
        raise HTTPException(404, "no dead job with that id")
    await requeue(request, resumes[0])
    return {"requeued": 1}


@router.post("/retry-all")
async def retry_all(request: Request, user_id: int = Depends(current_user_id)):
    resumes = await owned_dead_resumes(request, user_id)
    for resume in resumes:
        await requeue(request, resume)
    return {"requeued": len(resumes)}
