import asyncio
import logging
import random

import asyncpg

from app import config
from app.reserve import Decline

log = logging.getLogger("seat.resilience")

# Errors where the transaction rolled back and a retry is safe
TRANSIENT = (
    asyncpg.exceptions.DeadlockDetectedError,
    asyncpg.exceptions.SerializationError,
    asyncpg.exceptions.LockNotAvailableError,
    asyncpg.exceptions.TooManyConnectionsError,
    asyncpg.exceptions.ConnectionDoesNotExistError,  # pooled conn died (DB restart)
    asyncpg.exceptions.CannotConnectNowError,        # DB still starting up
    ConnectionRefusedError,                          # DB briefly unreachable
    ConnectionResetError,                            # connection dropped mid-query
    asyncio.TimeoutError,                            # pool wait or statement timeout
)


async def with_retry(fn, *args):
    """Run fn(*args); retry transient DB errors with backoff + jitter.
    Domain declines (Decline) are never retried."""
    attempts = config.DB_RETRY_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            return await fn(*args)
        except TRANSIENT as e:
            if attempt == attempts:
                log.warning("giving up after %d attempts: %r", attempts, e)
                raise Decline(429, "busy", "server busy, please retry",
                              retry_after_seconds=1) from e
            delay = 0.02 * (2 ** attempt) + random.uniform(0, 0.02)
            log.info("transient %s, retry %d in %.3fs",
                     type(e).__name__, attempt, delay)
            await asyncio.sleep(delay)