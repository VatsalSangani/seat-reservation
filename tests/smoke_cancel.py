"""Run: uv run python tests/smoke_cancel.py"""
import asyncio
import os
import uuid

import httpx

BASE = os.getenv("BASE_URL", "http://localhost:8000")
ADMIN_KEY = os.getenv("ADMIN_KEY", "local-admin-key")


async def token(c, user_id, admin=False):
    headers = {"X-Admin-Key": ADMIN_KEY} if admin else {}
    r = await c.post(f"{BASE}/auth/token", json={"user_id": user_id},
                     headers=headers)
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def main():
    async with httpx.AsyncClient(timeout=30) as c:
        admin = await token(c, "admin", admin=True)
        show = (await c.post(f"{BASE}/shows", headers=admin, json={
            "name": "cancel-test", "price_paise": 25000,
            "seats": ["A1", "A2", "A3"]})).json()["id"]
        alice, bob = await token(c, "alice"), await token(c, "bob")

        r = await c.post(f"{BASE}/shows/{show}/reserve", headers=alice,
                         json={"seats": ["A1"], "idempotency_key": str(uuid.uuid4())})
        res_a = r.json()["reservation_id"]
        print("alice reserves A1:", r.status_code, "(want 201)")

        r = await c.post(f"{BASE}/reservations/{res_a}/cancel", headers=bob)
        print("bob cancels alice's:", r.status_code, "(want 404)")

        r = await c.post(f"{BASE}/reservations/{res_a}/cancel", headers=alice)
        print("alice cancels:", r.status_code, r.json()["released_seats"],
              "(want 200 ['A1'])")

        r = await c.post(f"{BASE}/shows/{show}/reserve", headers=bob,
                         json={"seats": ["A1"], "idempotency_key": str(uuid.uuid4())})
        print("bob rebooks A1:", r.status_code, "(want 201)")

        r = await c.post(f"{BASE}/reservations/{res_a}/cancel", headers=alice)
        print("alice cancels again:", r.status_code, r.json()["released_seats"],
              "(want 200 [] - must NOT free bob's A1)")

        s = (await c.get(f"{BASE}/shows/{show}")).json()
        print("show state:", s["counts"], "invariant_ok:", s["invariant_ok"],
              "(want available 2, confirmed 1, True)")


asyncio.run(main())