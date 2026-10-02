# Write-up

## Results (live Railway deployment)
5,526 concurrent reservations against a fresh 200-seat show:
200 confirmed, every seat exactly once; 201 x242 (incl. idempotent replays),
409 x5,284; **zero 5xx, zero network errors**; invariant
`available + held + confirmed == total` held during and after the burst;
per-user limit held; duplicate keys produced one reservation each; spoofed
identity ignored.

## 1. The atomic decision
Everything happens in one Postgres transaction per request:
1. Lock the user's row in `user_show_quota` (`SELECT ... FOR UPDATE`), so one
   user's parallel requests run one at a time.
2. Re-check the idempotency key under that lock.
3. Enforce the per-user limit against the locked count.
4. Lock the requested seats with `SELECT ... ORDER BY label FOR UPDATE`. If any
   is not `available`, roll back and return 409 (all-or-nothing).
5. Write the reservation, seats, quota and idempotency key, then commit.

**Why it's race-free:** the decision is made on locked rows, never on an
earlier read. 500 requests for A12 queue on A12's row lock; the first commits,
the rest wake up, see `confirmed` and decline.

**Deadlocks:** every transaction locks the quota row first, then seats in
sorted order, so two multi-seat requests can never wait on each other in a
cycle. Cancel takes locks in the same order.

**Not `SKIP LOCKED`:** it would decline a seat just because another request is
checking it. If that request then rolls back (e.g. over its limit), everyone
has already been turned away and the seat goes unsold.

**Fast path:** before locking, a lock-free read declines seats that are
already confirmed. Postgres only shows committed data, so "taken" there is
really taken; "available" still goes through the locked path. Losers stop
holding pool connections while queueing on hot rows.

**Safety nets in the schema:** `(show_id, label)` primary key; a CHECK that
an available seat has no owner and a taken seat does; money as BIGINT paise.

## 2. Idempotency
- Stored in `idempotency_keys` with primary key `(user_id, key)`, in the
  **same transaction** as the booking, so a key exists if and only if its
  booking committed.
- A retry with the same key returns the stored response (201, header
  `Idempotent-Replayed: true`); no second booking.
- Same key with different seats: a hash of (show, sorted seats) differs, 409.
- Concurrent duplicates are serialised by the user lock; the second sees the
  first's key and replays.
- In the fast path the seat is read **before** the key; a booking and its key
  commit together, so a request that sees its own booking also sees its key.
- Only successful bookings store keys: a declined request reserved nothing,
  so a retry gets a fresh decision.
- Known trade-off: a replay returns the original response verbatim, even if
  the reservation was later cancelled.

## 3. Holds and release
Reservations are confirmed immediately; release is an explicit, owner-only
`POST /reservations/{id}/cancel`. Chosen over auto-expiring holds: no timers
or expiry races. Cancel frees only seats whose `reservation_id` still matches,
so a late cancel can never free a seat resold to someone else (tested).
Cancelling twice is a safe no-op. Not-yours and not-found both return 404,
so reservations can't be probed.

## 4. Consistency vs availability under a partition
Correctness wins. If the app can't reach Postgres it can't prove a seat is
free, so it refuses to sell: `/health/ready` returns 503 so the platform
stops routing to it, and in-flight requests retry transient errors with
backoff, then fail with a 4xx 429 rather than guessing. There is no local
cache of seat state that could be sold from. One database is the single
source of truth.

## 5. Observability: what would page me at 2am
- **Any 5xx** (`http_requests_total{status=~"5.."}` > 0): should never happen.
- **Invariant broken:** `seats{status="available"}+held+confirmed != total`.
- **Readiness failing** for more than a minute (DB unreachable).
- **429 `busy` rising:** pool exhausted or DB struggling.
- **p99 reserve latency** above an SLO (e.g. 1 s) from the histogram.
Investigation: every error response carries a `request_id`; the matching JSON
log line has the full context and traceback.

## 6. Battle scars (found by tests, fixed)
- A rename left one caller on an old name; endpoint returned plain-text 500.
  Added Ruff (undefined names) to the workflow.
- Default 5 s uvicorn keep-alive closed idle connections while requests
  queued: client `ReadError`s. Now `--timeout-keep-alive 75`.
- A refactor broke tuple unpacking in the reserve endpoint: 1,997 x 500 in a
  stress run. The JSON safety net caught it and every transaction rolled back
  (invariant intact). Lesson: one request, then quick tests, then stress.
- After a DB restart, pooled connections were dead; one endpoint had no
  retry. Now every DB path uses the same retry policy, including
  "database starting up" errors.
- Python logs went to stderr, so Railway showed every request as an error;
  now stdout.

## 7. Performance note
Local and live bursts plateaued at about 50-90 req/s, but server metrics
showed about 40 ms mean reserve latency. By Little's law only about 4
requests were in flight inside the server, so the single-process Python load
generator (plus network distance to Railway) was the limit, not the service.

## 8. AI usage (directed vs decided)
I built this step by step in a chat with Claude (Anthropic), not with an
autonomous coding agent.
- **Claude proposed:** the schema, the locking design, most of the code, the
  test and burst scripts, and explanations of each decision.
- **I directed and decided:** keeping the design simple (one Postgres, no
  Redis/queues), cancel over auto-expiry, all-or-nothing multi-seat, uv,
  Docker Engine on WSL, a PR per step, Railway as production with EC2 as
  backup/staging, keeping sleep mode off.
- **I verified:** I ran every step locally and on Railway, read the outputs,
  and found the bugs above through tests; fixes were made together.
I can explain and extend every part of the code.

## 9. What I'd do next
- Turn the smoke tests into a pytest suite run by GitHub Actions on every PR.
- Replay returns the reservation's current status after a cancel.
- Explicit tests for 401 / unknown show / unknown seat.
- A faster load generator (e.g. k6) to measure the server's real ceiling.
- Prometheus + Grafana dashboard and alert rules for the section 5 pages.