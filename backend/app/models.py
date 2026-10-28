"""ORM models: recruiters, batches, resumes (one job each), results, LLM cache."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# A resume's life: queued -> processing -> done
#                              |-> queued again (transient error, retry later)
#                              |-> dead (permanent error or retries used up -> DLQ)
QUEUED, PROCESSING, DONE, DEAD = "queued", "processing", "done", "dead"
STATUSES = (QUEUED, PROCESSING, DONE, DEAD)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    job_description: Mapped[str] = mapped_column(Text)
    total: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Resume(Base):
    """One uploaded PDF = one background job."""

    __tablename__ = "resumes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("batches.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default=QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Why it failed (last error) - shown in the failed-jobs panel.
    error: Mapped[str | None] = mapped_column(Text)
    # Bumped by a DLQ retry, so the new ARQ job gets a fresh job id.
    generation: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Result(Base):
    __tablename__ = "results"

    id: Mapped[int] = mapped_column(primary_key=True)
    # WHY unique: idempotency. A job that runs twice (worker crashed after
    # writing but before acknowledging) UPSERTs the same row instead of
    # inserting a duplicate.
    resume_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resumes.id"), unique=True)
    score: Mapped[int]
    strengths: Mapped[list[str]] = mapped_column(JSONB)
    gaps: Mapped[list[str]] = mapped_column(JSONB)
    verdict: Mapped[str] = mapped_column(Text)
    candidate_name: Mapped[str | None] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(32))
    cached: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LLMCache(Base):
    """LLM answers keyed by sha256(resume text + job description).

    In Postgres rather than Redis because it saves real money: it should
    survive a Redis restart and last as long as the data it describes.
    """

    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    response: Mapped[dict] = mapped_column(JSONB)
    provider: Mapped[str] = mapped_column(String(32))
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
