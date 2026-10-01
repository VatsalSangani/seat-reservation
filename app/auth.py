import hmac
import time
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import config

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class User:
    user_id: str
    role: str


def create_token(user_id: str, role: str = "user") -> str:
    now = int(time.time())
    payload = {"sub": user_id, "role": role, "iat": now,
               "exp": now + config.TOKEN_TTL_SECONDS}
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def is_admin_key(key: str | None) -> bool:
    # constant-time comparison, so timing can't leak the key
    return bool(key) and hmac.compare_digest(key, config.ADMIN_KEY)


async def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    """Identity comes ONLY from the verified token, never the body."""
    if creds is None:
        raise HTTPException(401, "missing bearer token")
    try:
        payload = jwt.decode(creds.credentials, config.JWT_SECRET,
                             algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid or expired token")
    return User(user_id=payload["sub"], role=payload.get("role", "user"))


async def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    return user