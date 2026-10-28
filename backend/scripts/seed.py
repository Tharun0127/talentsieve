"""Seed a demo recruiter (idempotent): recruiter@talentsieve.dev / recruit123."""
import asyncio
import os

import asyncpg
import bcrypt

EMAIL = "recruiter@talentsieve.dev"
PASSWORD = "recruit123"


async def seed(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        password_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(12)).decode()
        await conn.execute(
            "INSERT INTO users (email, password_hash) VALUES ($1, $2) ON CONFLICT (email) DO NOTHING",
            EMAIL, password_hash,
        )
        print(f"[seed] demo recruiter {EMAIL} / {PASSWORD}")
    finally:
        await conn.close()


def main() -> None:
    url = os.getenv("DATABASE_URL", "postgresql+asyncpg://sieve:sieve@localhost:5434/talentsieve")
    asyncio.run(seed(url.replace("postgresql+asyncpg://", "postgresql://")))


if __name__ == "__main__":
    main()
