import os

DATABASE_URL = os.environ["DATABASE_URL"]
JWT_SECRET = os.environ["JWT_SECRET"]
ADMIN_KEY = os.environ["ADMIN_KEY"]

DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "2"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", "86400"))

DB_ACQUIRE_TIMEOUT = float(os.getenv("DB_ACQUIRE_TIMEOUT", "30"))
DB_RETRY_ATTEMPTS = int(os.getenv("DB_RETRY_ATTEMPTS", "4"))