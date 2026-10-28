"""FastAPI application factory (the API role; the worker is app/worker.py)."""
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.db import build_engine, build_session_factory
from app.routers import auth, batches

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = build_engine(settings)
        app.state.settings = settings
        app.state.session_factory = build_session_factory(engine)
        app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)

        async def enqueue_batch(batch_id: uuid.UUID, resume_ids: list[uuid.UUID]) -> None:
            """Phase 2 wires this to the job queue."""

        app.state.enqueue_batch = enqueue_batch
        yield
        await app.state.redis.aclose()
        await engine.dispose()

    app = FastAPI(title="TalentSieve", lifespan=lifespan)
    app.include_router(auth.router)
    app.include_router(batches.router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
