import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator

from app import auth, db
from app.reserve import Decline, cancel, reserve, show_state


# ---------- startup / shutdown ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await db.run_migrations()
    yield
    await db.close()


app = FastAPI(title="Seat Reservation", lifespan=lifespan)


# ---------- domain declines -> clean 4xx JSON ----------
@app.exception_handler(Decline)
async def decline_handler(request: Request, exc: Decline):
    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.reason, "message": exc.message, **exc.extra},
    )


# ---------- health ----------
@app.get("/health/live")
async def live():
    return {"status": "ok"}


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


@app.post("/shows", status_code=201)
async def create_show(body: CreateShow,
                      admin: auth.User = Depends(auth.require_admin)):
    async with db.pool().acquire() as conn:
        async with conn.transaction():       # show + seats, all or nothing
            row = await conn.fetchrow(
                """INSERT INTO shows (name, price_paise, per_user_limit)
                   VALUES ($1, $2, $3) RETURNING id""",
                body.name, body.price_paise, body.per_user_limit)
            await conn.execute(
                """INSERT INTO seats (show_id, label)
                   SELECT $1, unnest($2::text[])""",
                row["id"], body.seats)
    return {
        "id": str(row["id"]),
        "name": body.name,
        "price_paise": body.price_paise,
        "per_user_limit": body.per_user_limit,
        "total_seats": len(body.seats),
        "seats": [{"label": s, "status": "available"} for s in body.seats],
    }


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

    result, replayed = await reserve(parse_uuid(show_id, "show"),
                                     user.user_id, body.seats, key)
    headers = {"Idempotent-Replayed": "true"} if replayed else {}
    return JSONResponse(status_code=201, content=result, headers=headers)

@app.get("/shows/{show_id}")
async def get_show(show_id: str):
    return await show_state(parse_uuid(show_id, "show"))


@app.post("/reservations/{reservation_id}/cancel")
async def cancel_reservation(reservation_id: str,
                             user: auth.User = Depends(auth.current_user)):
    return await cancel(parse_uuid(reservation_id, "reservation"),
                        user.user_id)