"""Shared fixtures.

Real Postgres (`talentsieve_test`) and real Redis (DB 15), reset per test.
The API runs in-process via httpx's ASGI transport; workers are exercised by
calling the job function directly or by running a real ARQ worker in-process.
"""
import asyncio
import os
import tempfile
from contextlib import AsyncExitStack

import asyncpg
import bcrypt
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from app.auth import create_access_token
from app.config import Settings
from app.main import create_app
from app.pdfgen import text_pdf

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+asyncpg://sieve:sieve@localhost:5434/talentsieve_test"
)
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6374/15")
PLAIN_DSN = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
PASSWORD_HASH = bcrypt.hashpw(b"password123", bcrypt.gensalt(4)).decode()

JOB_DESCRIPTION = "Senior Python backend engineer: FastAPI, PostgreSQL, Redis, Docker, AWS. 5+ years."


def make_settings(**overrides) -> Settings:
    values = {
        "database_url": TEST_DATABASE_URL, "redis_url": TEST_REDIS_URL, "bcrypt_rounds": 4,
        "upload_dir": tempfile.mkdtemp(prefix="sieve-test-"), "llm_provider": "mock",
        "queue_name": "talentsieve:test-queue",
        # Fast, deterministic defaults for tests; individual tests override.
        "mock_llm_min_delay": 0.0, "mock_llm_max_delay": 0.0, "mock_llm_failure_rate": 0.0,
        "retry_base_delay_seconds": 0.05, "retry_max_delay_seconds": 0.2,
        "llm_rate_limit_per_minute": 100_000,
    }
    values.update(overrides)
    return Settings(**values)


def resume_pdf(name: str, skills: str) -> bytes:
    return text_pdf([name, "Software Engineer", f"Skills: {skills}", "Experience: 6 years building APIs."])


async def _create_test_database() -> None:
    admin_dsn = PLAIN_DSN.rsplit("/", 1)[0] + "/postgres"
    db_name = PLAIN_DSN.rsplit("/", 1)[1]
    conn = await asyncpg.connect(admin_dsn)
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name):
            await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def migrated_db() -> str:
    asyncio.run(_create_test_database())
    config = Config("alembic.ini")
    config.attributes["url"] = TEST_DATABASE_URL
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    return TEST_DATABASE_URL


@pytest.fixture
async def db(migrated_db):
    conn = await asyncpg.connect(PLAIN_DSN)
    await conn.execute("TRUNCATE results, resumes, batches, llm_cache, users RESTART IDENTITY CASCADE")
    yield conn
    await conn.close()


@pytest.fixture
async def redis_client():
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
async def recruiter(db) -> dict:
    """A recruiter row plus auth headers."""
    user_id = await db.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ('r@test.dev', $1) RETURNING id", PASSWORD_HASH
    )
    token = create_access_token(make_settings(), user_id)
    return {"id": user_id, "token": token, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture
async def api_factory(db, redis_client):
    """Build an API app + client with settings overrides. Returns (client, app)."""
    async with AsyncExitStack() as stack:
        async def make(**overrides):
            from asgi_lifespan import LifespanManager

            app = create_app(make_settings(**overrides))
            await stack.enter_async_context(LifespanManager(app))
            client = await stack.enter_async_context(
                AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30)
            )
            return client, app

        yield make


async def upload(client, headers, files: list[tuple[str, bytes]], title="Backend hire", jd=JOB_DESCRIPTION):
    return await client.post(
        "/batches", headers=headers, data={"title": title, "job_description": jd},
        files=[("files", (name, data, "application/pdf")) for name, data in files],
    )
