"""FastAPI application factory (the API role; the worker is app/worker.py)."""
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app import queue
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
        app.state.arq = await queue.connect(settings)

        async def enqueue_batch(batch_id: uuid.UUID, resume_ids: list[uuid.UUID]) -> None:
            # One job per resume: a slow or failing CV never holds up the others,
            # and N workers can work on one batch in parallel.
            for resume_id in resume_ids:
                await queue.enqueue_resume(app.state.arq, settings, batch_id, resume_id)

        app.state.enqueue_batch = enqueue_batch
        yield
        await app.state.arq.aclose()
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
