"""On-sale stampede + reconciliation.
Usage: ./burst.sh <BASE_URL> [--users 5000] [--concurrency 500]
"""
import argparse
import asyncio
import os
import random
import sys
import time
import uuid
from collections import Counter, defaultdict

import httpx

HOT = ["A1", "A2", "A3", "A4", "A5"]


async def main(base: str, users: int, concurrency: int, admin_key: str):
    limits = httpx.Limits(max_connections=concurrency,
                          max_keepalive_connections=concurrency)
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(base_url=base, timeout=60,
                                 limits=limits) as c:

        async def token(uid, admin=False):
            h = {"X-Admin-Key": admin_key} if admin else {}
            r = await c.post("/auth/token", json={"user_id": uid}, headers=h)
            r.raise_for_status()
            return {"Authorization": f"Bearer {r.json()['token']}"}

        async def call(method, url, **kw):
            async with sem:
                try:
                    r = await c.request(method, url, **kw)
                    body = r.json() if r.content else {}
                    return r.status_code, body
                except httpx.TransportError as e:
                    return 0, {"error": f"network:{type(e).__name__}"}

        # ① fresh show
        admin = await token("burst-admin", admin=True)
        seats = [f"{row}{n}" for row in "ABCDEFGHIJ" for n in range(1, 21)]
        r = await c.post("/shows", headers=admin, json={
            "name": f"burst-{int(time.time())}", "price_paise": 25000,
            "seats": seats, "per_user_limit": 4})
        r.raise_for_status()
        show = r.json()["id"]
        print(f"show {show}: {len(seats)} seats, {users} users")

        # tokens up front (not part of the measured stampede),
        # through the same concurrency limit so httpx's pool isn't flooded
        async def limited_token(uid):
            async with sem:
                return await token(uid)

        t_tokens = time.perf_counter()
        tokens = await asyncio.gather(
            *(limited_token(f"burst-u{i}") for i in range(users)))
        greedy = await token("burst-greedy")
        print(f"{len(tokens)} tokens ready in "
              f"{time.perf_counter() - t_tokens:.1f}s, starting stampede")

        jobs, kinds = [], []

        def add(kind, coro):
            kinds.append(kind)
            jobs.append(coro)

        dup_keys = []
        for hdr in tokens:
            roll = random.random()
            if roll < 0.5:
                seat, kind = random.choice(HOT), "hot"
            elif roll < 0.8:
                seat, kind = random.choice(seats[len(HOT):]), "random"
            elif roll < 0.9:          # same key sent twice at once
                key = str(uuid.uuid4())
                seat = random.choice(seats[len(HOT):])
                dup_keys.append(key)
                for _ in range(2):
                    add("dup", call("POST", f"/shows/{show}/reserve", headers=hdr,
                                    json={"seats": [seat], "idempotency_key": key}))
                continue
            else:                     # spoof attempt: body claims another user
                seat, kind = random.choice(seats[len(HOT):]), "spoof"
                add(kind, call("POST", f"/shows/{show}/reserve", headers=hdr,
                               json={"seats": [seat], "user_id": "victim",
                                     "idempotency_key": str(uuid.uuid4())}))
                continue
            add(kind, call("POST", f"/shows/{show}/reserve", headers=hdr,
                           json={"seats": [seat],
                                 "idempotency_key": str(uuid.uuid4())}))
        free = [s for s in seats if s not in HOT]
        for s in random.sample(free, 10):  # greedy user: 10 parallel, limit 4
            add("greedy", call("POST", f"/shows/{show}/reserve", headers=greedy,
                               json={"seats": [s],
                                     "idempotency_key": str(uuid.uuid4())}))

        # monitor: check the invariant DURING the burst
        stop, snapshots = asyncio.Event(), []

        async def monitor():
            while not stop.is_set():
                _, st = await call("GET", f"/shows/{show}")
                if "counts" in st:
                    snapshots.append(st["invariant_ok"])
                await asyncio.sleep(0.2)

        mon = asyncio.create_task(monitor())
        t0 = time.perf_counter()
        results = await asyncio.gather(*jobs)
        elapsed = time.perf_counter() - t0
        stop.set()
        await mon

        # ③ reconcile
        status = Counter(s for s, _ in results)
        reasons = Counter(b.get("error", "") for s, b in results if s != 201)
        wins_per_seat = defaultdict(set)
        res_ids, confirmed_seats = set(), set()
        greedy_wins, spoof_ok = 0, True
        for (s, b), kind in zip(results, kinds, strict=True):
            if s == 201:
                res_ids.add(b["reservation_id"])
                for seat in b["seats"]:
                    wins_per_seat[seat].add(b["reservation_id"])
                    confirmed_seats.add(seat)
                if kind == "greedy":
                    greedy_wins += 1
                if kind == "spoof" and b["user_id"] == "victim":
                    spoof_ok = False
        dup_results = [(s, b) for (s, b), k in zip(results, kinds, strict=True)
                       if k == "dup"]
        dup_ids = Counter(b.get("reservation_id")
                          for s, b in dup_results if s == 201)

        _, final = await call("GET", f"/shows/{show}")
        counts = final["counts"]

        checks = {
            "no seat sold twice":
                all(len(v) == 1 for v in wins_per_seat.values()),
            "each hot seat exactly one winner":
                all(len(wins_per_seat.get(h, ())) == 1 for h in HOT),
            "zero 5xx":
                not any(s >= 500 for s in status),
            "zero network errors":
                status.get(0, 0) == 0,
            "invariant held during burst":
                bool(snapshots) and all(snapshots),
            "invariant holds after burst":
                final["invariant_ok"],
            "API confirmed == seats won in responses":
                counts["confirmed"] == len(confirmed_seats),
            "duplicate keys -> one reservation each":
                all(n == 2 for n in dup_ids.values()),
            "per-user limit held (<= 4)":
                greedy_wins <= 4,
            "identity from token (spoof ignored)":
                spoof_ok,
        }

        print(f"\nfired {len(results)} reservations in {elapsed:.1f}s "
              f"({len(results) / elapsed:.0f} req/s)")
        print("status:", dict(status))
        print("declined by reason:", dict(reasons))
        print("final counts:", counts,
              f"| invariant snapshots during burst: {len(snapshots)}")
        print()
        for name, ok in checks.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        return all(checks.values())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("base_url")
    p.add_argument("--users", type=int, default=5000)
    p.add_argument("--concurrency", type=int, default=500)
    a = p.parse_args()
    ok = asyncio.run(main(a.base_url.rstrip("/"), a.users, a.concurrency,
                          os.getenv("ADMIN_KEY", "local-admin-key")))
    sys.exit(0 if ok else 1)