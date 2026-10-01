import hashlib
import json
import uuid

import asyncpg

from app import db


class Decline(Exception):
    """A clean domain outcome (4xx), never a server error."""

    def __init__(self, status: int, reason: str, message: str, **extra):
        self.status, self.reason, self.message, self.extra = (
            status, reason, message, extra)


def request_hash(show_id: uuid.UUID, seats: list[str]) -> str:
    """Same show + same set of seats = same hash (order doesn't matter)."""
    canonical = json.dumps({"show_id": str(show_id), "seats": sorted(seats)},
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def reserve(show_id: uuid.UUID, user_id: str, seats: list[str],
                  key: str) -> tuple[dict, bool]:
    """Returns (reservation, replayed). Raises Decline for 4xx outcomes."""
    seats = sorted(seats)
    req_hash = request_hash(show_id, seats)

    async with db.pool().acquire() as conn:
        try:
            async with conn.transaction():
                show = await conn.fetchrow(
                    "SELECT price_paise, per_user_limit FROM shows WHERE id = $1",
                    show_id)
                if show is None:
                    raise Decline(404, "show_not_found", "show not found")

                # ① serialise THIS user's requests for THIS show
                await conn.execute(
                    """INSERT INTO user_show_quota (show_id, user_id)
                       VALUES ($1, $2) ON CONFLICT DO NOTHING""",
                    show_id, user_id)
                held = await conn.fetchval(
                    """SELECT held_count FROM user_show_quota
                       WHERE show_id = $1 AND user_id = $2 FOR UPDATE""",
                    show_id, user_id)

                # ② idempotency (checked while holding the user lock)
                prev = await conn.fetchrow(
                    """SELECT request_hash, response FROM idempotency_keys
                       WHERE user_id = $1 AND key = $2""",
                    user_id, key)
                if prev:
                    if prev["request_hash"] != req_hash:
                        raise Decline(409, "idempotency_key_reused",
                                      "key already used with a different request")
                    return json.loads(prev["response"]), True

                # ③ per-user limit
                limit = show["per_user_limit"]
                if held + len(seats) > limit:
                    raise Decline(409, "per_user_limit",
                                  f"limit is {limit} seats per user",
                                  limit=limit, held=held)

                # ④ lock requested seats in sorted order (no deadlocks)
                rows = await conn.fetch(
                    """SELECT label, status FROM seats
                       WHERE show_id = $1 AND label = ANY($2::text[])
                       ORDER BY label FOR UPDATE""",
                    show_id, seats)
                found = {r["label"] for r in rows}
                if len(found) != len(seats):
                    raise Decline(400, "unknown_seat", "seat does not exist",
                                  seats=sorted(set(seats) - found))
                taken = [r["label"] for r in rows if r["status"] != "available"]
                if taken:
                    raise Decline(409, "seat_taken", "seat already taken",
                                  seats=taken)

                # ⑤ write everything in the same transaction
                amount = show["price_paise"] * len(seats)
                res_id = await conn.fetchval(
                    """INSERT INTO reservations
                         (show_id, user_id, seats, amount_paise, status)
                       VALUES ($1, $2, $3, $4, 'confirmed') RETURNING id""",
                    show_id, user_id, seats, amount)
                await conn.execute(
                    """UPDATE seats SET status = 'confirmed', user_id = $3,
                              reservation_id = $4, updated_at = now()
                       WHERE show_id = $1 AND label = ANY($2::text[])""",
                    show_id, seats, user_id, res_id)
                await conn.execute(
                    """UPDATE user_show_quota SET held_count = held_count + $3
                       WHERE show_id = $1 AND user_id = $2""",
                    show_id, user_id, len(seats))

                response = {
                    "reservation_id": str(res_id), "show_id": str(show_id),
                    "user_id": user_id, "seats": seats,
                    "amount_paise": amount, "status": "confirmed",
                }
                await conn.execute(
                    """INSERT INTO idempotency_keys
                         (user_id, key, show_id, request_hash,
                          reservation_id, response)
                       VALUES ($1, $2, $3, $4, $5, $6::jsonb)""",
                    user_id, key, show_id, req_hash, res_id,
                    json.dumps(response))
                return response, False
        except asyncpg.UniqueViolationError:
            # safety net: same key raced in from another show
            raise Decline(409, "idempotency_key_reused",
                          "key already used with a different request")