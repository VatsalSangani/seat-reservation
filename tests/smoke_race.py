"""Quick local race test. Run: uv run python tests/smoke_race.py"""
import asyncio
import os
import uuid
from collections import Counter

import httpx

BASE = os.getenv("BASE_URL", "http://localhost:8000")
ADMIN_KEY = os.getenv("ADMIN_KEY", "local-admin-key")


async def token(c, user_id, admin=False):
    headers = {"X-Admin-Key": ADMIN_KEY} if admin else {}
    r = await c.post(f"{BASE}/auth/token", json={"user_id": user_id},
                     headers=headers)
    return r.json()["token"]


async def reserve(c, tok, show, seats, key=None):
    r = await c.post(f"{BASE}/shows/{show}/reserve",
                     json={"seats": seats,
                           "idempotency_key": key or str(uuid.uuid4())},
                     headers={"Authorization": f"Bearer {tok}"})
    return r.status_code, r.json()


async def main():
    limits = httpx.Limits(max_connections=200)
    async with httpx.AsyncClient(timeout=60, limits=limits) as c:
        admin = await token(c, "admin", admin=True)
        r = await c.post(f"{BASE}/shows",
                         json={"name": "race", "price_paise": 25000,
                               "seats": [f"A{i}" for i in range(1, 21)]},
                         headers={"Authorization": f"Bearer {admin}"})
        show = r.json()["id"]

        # 1. 200 users storm seat A1
        toks = await asyncio.gather(*(token(c, f"u{i}") for i in range(200)))
        res = await asyncio.gather(*(reserve(c, t, show, ["A1"]) for t in toks))
        print("A1 storm:", Counter(s for s, _ in res), "(want 201 x1)")

        # 2. one user, 10 parallel requests, limit 4
        t = await token(c, "greedy")
        res = await asyncio.gather(*(reserve(c, t, show, [f"A{i}"])
                                     for i in range(2, 12)))
        print("limit test:", Counter(s for s, _ in res), "(want 201 x4)")

        # 3. idempotency: same key twice, then different seats
        t = await token(c, "retry-user")
        key = str(uuid.uuid4())
        s1, b1 = await reserve(c, t, show, ["A15"], key)
        s2, b2 = await reserve(c, t, show, ["A15"], key)
        s3, _ = await reserve(c, t, show, ["A16"], key)
        print("idempotency:", s1, s2,
              "same id:", b1["reservation_id"] == b2["reservation_id"],
              "| other seats:", s3, "(want 201 201 True | 409)")


asyncio.run(main())