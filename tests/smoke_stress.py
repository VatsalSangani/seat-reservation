"""Run: N=2000 uv run python tests/smoke_stress.py"""
import asyncio
import os
import random
import uuid
from collections import Counter

import httpx

BASE = os.getenv("BASE_URL", "http://localhost:8000")
ADMIN_KEY = os.getenv("ADMIN_KEY", "local-admin-key")
N = int(os.getenv("N", "2000"))


async def token(c, user_id, admin=False):
    headers = {"X-Admin-Key": ADMIN_KEY} if admin else {}
    r = await c.post(f"{BASE}/auth/token", json={"user_id": user_id},
                     headers=headers)
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def reserve(c, hdr, show, seats):
    try:
        r = await c.post(f"{BASE}/shows/{show}/reserve", headers=hdr,
                         json={"seats": seats,
                               "idempotency_key": str(uuid.uuid4())})
    except httpx.TransportError as e:
        return 0, f"network:{type(e).__name__}"
    reason = r.json().get("error", "") if r.status_code != 201 else ""
    return r.status_code, reason


async def main():
    limits = httpx.Limits(max_connections=500)
    async with httpx.AsyncClient(timeout=120, limits=limits) as c:
        admin = await token(c, "admin", admin=True)
        seats = [f"S{i}" for i in range(1, 51)]
        show = (await c.post(f"{BASE}/shows", headers=admin, json={
            "name": "stress", "price_paise": 25000, "seats": seats})).json()["id"]

        users = await asyncio.gather(*(token(c, f"s{i}") for i in range(N)))
        hot = ["S1", "S2", "S3"]
        jobs = []
        for i, hdr in enumerate(users):
            if i % 2:   # hot-seat storm
                jobs.append(reserve(c, hdr, show, [random.choice(hot)]))
            else:       # overlapping pairs, random order (deadlock bait)
                pair = random.sample(seats[3:13], 2)
                jobs.append(reserve(c, hdr, show, pair))
        results = await asyncio.gather(*jobs)

        print("status:", Counter(s for s, _ in results))
        print("reasons:", Counter(r for _, r in results if r))
        print("5xx:", sum(1 for s, _ in results if s >= 500), "(want 0)")
        st = (await c.get(f"{BASE}/shows/{show}")).json()
        print("counts:", st["counts"], "invariant_ok:", st["invariant_ok"])


asyncio.run(main())