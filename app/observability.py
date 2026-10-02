import contextvars
import json
import logging

from prometheus_client import Counter, Gauge, Histogram

request_id_var = contextvars.ContextVar("request_id", default="-")


# ---------- structured JSON logs ----------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            data.update(fields)
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True  # we log our own


# ---------- Prometheus metrics ----------
RESERVATIONS_CONFIRMED = Counter(
    "reservations_confirmed_total", "Reservations confirmed")
RESERVATIONS_DECLINED = Counter(
    "reservations_declined_total", "Reservation attempts declined",
    ["reason"])
for _reason in ("seat_taken", "per_user_limit", "idempotent_replay",
                "idempotency_key_reused", "busy"):
    RESERVATIONS_DECLINED.labels(_reason)        # show as 0 from the start

SEATS_AVAILABLE = Gauge(
    "seats_available", "Available seats per show (read from DB)",
    ["show_id"])
SEATS_BY_STATUS = Gauge(
    "seats", "Seats per show and status (read from DB)",
    ["show_id", "status"])

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "Request latency", ["route"])


async def refresh_seat_gauges(conn) -> None:
    """Set seat gauges from the DB, so metrics always match the API."""
    rows = await conn.fetch(
        "SELECT show_id, status, count(*) AS n FROM seats "
        "GROUP BY show_id, status")
    SEATS_AVAILABLE.clear()
    SEATS_BY_STATUS.clear()
    per_show: dict[str, dict[str, int]] = {}
    for r in rows:
        per_show.setdefault(str(r["show_id"]), {})[r["status"]] = r["n"]
    for show_id, counts in per_show.items():
        SEATS_AVAILABLE.labels(show_id).set(counts.get("available", 0))
        for status in ("available", "held", "confirmed"):
            SEATS_BY_STATUS.labels(show_id, status).set(counts.get(status, 0))