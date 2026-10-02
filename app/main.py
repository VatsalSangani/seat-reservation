import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, StrictInt, field_validator

from app import auth, db
from app.observability import (
    HTTP_LATENCY,
    HTTP_REQUESTS,
    RESERVATIONS_CONFIRMED,
    RESERVATIONS_DECLINED,
    refresh_seat_gauges,
    request_id_var,
    setup_logging,
)
from app.reserve import Decline, cancel, reserve, show_state
from app.resilience import with_retry

setup_logging()
log = logging.getLogger("seat.app")


# ---------- startup / shutdown ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await db.run_migrations()
    log.info("startup_complete")
    yield
    await db.close()


app = FastAPI(title="Seat Reservation", lifespan=lifespan)


# ---------- request id, timing, access log, HTTP metrics ----------
@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    request_id_var.set(rid)
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        route = request.scope.get("route")
        path = route.path if route else "unmatched"
        duration = time.perf_counter() - start
        HTTP_REQUESTS.labels(request.method, path, str(status)).inc()
        HTTP_LATENCY.labels(path).observe(duration)
        log.info("request", extra={"fields": {
            "method": request.method, "route": path, "status": status,
            "duration_ms": round(duration * 1000, 1)}})


# ---------- domain declines -> clean 4xx JSON ----------
@app.exception_handler(Decline)
async def decline_handler(request: Request, exc: Decline):
    if request.url.path.endswith("/reserve"):
        RESERVATIONS_DECLINED.labels(exc.reason).inc()
        log.debug("reserve_declined", extra={"fields": {
            "reason": exc.reason, "status": exc.status}})
    headers = {"Retry-After": "1"} if exc.status == 429 else None
    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.reason, "message": exc.message, **exc.extra},
        headers=headers,
    )


# ---------- last-resort safety net (should never fire) ----------
@app.exception_handler(Exception)
async def unexpected_handler(request: Request, exc: Exception):
    log.exception("unhandled_error", extra={"fields": {
        "method": request.method, "path": request.url.path}})
    return JSONResponse(status_code=500, content={
        "error": "internal_error", "message": "unexpected server error",
        "request_id": request_id_var.get()})


# ---------- health ----------
@app.get("/health/live")
async def live():
    return {"status": "ok"}


@app.get("/health/ready")
async def ready():
    """Fails closed: 503 unless the database answers within 2 s."""
    try:
        async with db.pool().acquire(timeout=2) as conn:
            await conn.fetchval("SELECT 1", timeout=2)
    except Exception as e:  # noqa: BLE001 - any DB failure means not ready
        log.warning("not_ready", extra={"fields": {"error": type(e).__name__}})
        return JSONResponse(status_code=503,
                            content={"status": "unavailable"})
    return {"status": "ready"}


# ---------- metrics ----------
@app.get("/metrics")
async def metrics():
    try:
        async with db.acquire() as conn:
            await refresh_seat_gauges(conn)
    except Exception as e:  # noqa: BLE001 - metrics must never fail
        log.warning("metrics_db_refresh_failed",
                    extra={"fields": {"error": type(e).__name__}})
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------- auth ----------
class TokenRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)


@app.post("/auth/token")
async def issue_token(req: TokenRequest,
                      x_admin_key: str | None = Header(default=None)):
    """Test helper: anyone can get a user token; an admin token
    needs the secret admin key, so the role can't be self-assigned."""
    if x_admin_key is not None and not auth.is_admin_key(x_admin_key):
        raise HTTPException(403, "invalid admin key")
    role = "admin" if x_admin_key else "user"
    return {"token": auth.create_token(req.user_id, role),
            "user_id": req.user_id, "role": role}


# ---------- shows ----------
class CreateShow(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    seats: list[str] = Field(min_length=1, max_length=10_000)
    price_paise: StrictInt = Field(ge=0)          # StrictInt rejects floats
    per_user_limit: StrictInt = Field(default=4, ge=1)

    @field_validator("seats")
    @classmethod
    def seats_valid(cls, seats: list[str]) -> list[str]:
        cleaned = [s.strip() for s in seats]
        if any(not s or len(s) > 16 for s in cleaned):
            raise ValueError("seat labels must be 1-16 characters")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("duplicate seat labels")
        return cleaned


async def _insert_show(body: CreateShow):
    async with db.acquire() as conn:
        async with conn.transaction():       # show + seats, all or nothing
            row = await conn.fetchrow(
                """INSERT INTO shows (name, price_paise, per_user_limit)
                   VALUES ($1, $2, $3) RETURNING id""",
                body.name, body.price_paise, body.per_user_limit)
            await conn.execute(
                """INSERT INTO seats (show_id, label)
                   SELECT $1, unnest($2::text[])""",
                row["id"], body.seats)
    return row["id"]


@app.post("/shows", status_code=201)
async def create_show(body: CreateShow,
                      admin: auth.User = Depends(auth.require_admin)):
    show_id = await with_retry(_insert_show, body)
    log.info("show_created", extra={"fields": {
        "show_id": str(show_id), "seats": len(body.seats)}})
    return {
        "id": str(show_id),
        "name": body.name,
        "price_paise": body.price_paise,
        "per_user_limit": body.per_user_limit,
        "total_seats": len(body.seats),
        "seats": [{"label": s, "status": "available"} for s in body.seats],
    }


@app.get("/shows/{show_id}")
async def get_show(show_id: str):
    return await with_retry(show_state, parse_uuid(show_id, "show"))


# ---------- reserve ----------
class ReserveRequest(BaseModel):
    seats: list[str] = Field(min_length=1, max_length=10)
    idempotency_key: str | None = Field(default=None, min_length=1,
                                        max_length=128)
    # any other field (e.g. a spoofed "user_id") is ignored by Pydantic

    @field_validator("seats")
    @classmethod
    def seats_valid(cls, seats: list[str]) -> list[str]:
        cleaned = [s.strip() for s in seats]
        if any(not s for s in cleaned):
            raise ValueError("empty seat label")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("duplicate seats in request")
        return cleaned


def parse_uuid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise Decline(404, f"{what}_not_found", f"{what} not found") from None


@app.post("/shows/{show_id}/reserve", status_code=201)
async def reserve_seats(
    show_id: str,
    body: ReserveRequest,
    user: auth.User = Depends(auth.current_user),
    idempotency_key: str | None = Header(default=None),
):
    # key may come from the header or the body; if both, they must match
    if (body.idempotency_key and idempotency_key
            and body.idempotency_key != idempotency_key):
        raise Decline(400, "idempotency_key_mismatch",
                      "header and body keys differ")
    key = body.idempotency_key or idempotency_key
    if not key:
        raise Decline(400, "idempotency_key_required",
                      "idempotency key is required")

    result, replayed = await with_retry(
        reserve, parse_uuid(show_id, "show"), user.user_id, body.seats, key)

    if replayed:
        RESERVATIONS_DECLINED.labels("idempotent_replay").inc()
    else:
        RESERVATIONS_CONFIRMED.inc()
    log.debug("reserve_ok", extra={"fields": {
        "show_id": result["show_id"], "user_id": user.user_id,
        "seats": result["seats"], "replayed": replayed}})

    headers = {"Idempotent-Replayed": "true"} if replayed else {}
    return JSONResponse(status_code=201, content=result, headers=headers)


# ---------- cancel ----------
@app.post("/reservations/{reservation_id}/cancel")
async def cancel_reservation(reservation_id: str,
                             user: auth.User = Depends(auth.current_user)):
    result = await with_retry(cancel, parse_uuid(reservation_id, "reservation"),
                              user.user_id)
    log.info("cancel_ok", extra={"fields": {
        "reservation_id": result["reservation_id"],
        "released": result["released_seats"]}})
    return result