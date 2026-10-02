from pathlib import Path

import asyncpg

from app import config

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_pool: asyncpg.Pool | None = None


async def connect() -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        config.DATABASE_URL,
        min_size=config.DB_POOL_MIN,
        max_size=config.DB_POOL_MAX,
        command_timeout=10,
    )


async def close() -> None:
    if _pool:
        await _pool.close()


def pool() -> asyncpg.Pool:
    assert _pool is not None, "database pool not initialised"
    return _pool


async def run_migrations() -> None:
    """Apply each migrations/*.sql file once, in name order.
    The advisory lock stops two app instances migrating at once."""
    async with pool().acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock(727001)")
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name       TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            applied = {r["name"] for r in
                       await conn.fetch("SELECT name FROM schema_migrations")}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in applied:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text())
                    await conn.execute(
                        "INSERT INTO schema_migrations (name) VALUES ($1)",
                        path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(727001)")

def acquire():
    """Pool connection with a wait limit; raises asyncio.TimeoutError."""
    return pool().acquire(timeout=config.DB_ACQUIRE_TIMEOUT)