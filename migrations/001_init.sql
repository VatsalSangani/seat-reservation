-- Shows: price in integer paise, never floats
CREATE TABLE shows (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name           TEXT NOT NULL,
    price_paise    BIGINT NOT NULL CHECK (price_paise >= 0),
    per_user_limit INT NOT NULL DEFAULT 4 CHECK (per_user_limit > 0),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per seat. The single source of truth for seat state.
CREATE TABLE seats (
    show_id        UUID NOT NULL REFERENCES shows(id),
    label          TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'available'
                   CHECK (status IN ('available', 'held', 'confirmed')),
    user_id        TEXT,
    reservation_id UUID,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (show_id, label),          -- a seat exists once per show
    -- safety net: an available seat has no owner; a taken seat must have one
    CHECK ((status = 'available') = (user_id IS NULL AND reservation_id IS NULL))
);
CREATE INDEX seats_show_status ON seats (show_id, status);

CREATE TABLE reservations (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    show_id      UUID NOT NULL REFERENCES shows(id),
    user_id      TEXT NOT NULL,
    seats        TEXT[] NOT NULL,
    amount_paise BIGINT NOT NULL CHECK (amount_paise >= 0),
    status       TEXT NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX reservations_user ON reservations (user_id);

-- One row per (show, user): we lock it so one user's requests take turns
CREATE TABLE user_show_quota (
    show_id    UUID NOT NULL REFERENCES shows(id),
    user_id    TEXT NOT NULL,
    held_count INT NOT NULL DEFAULT 0 CHECK (held_count >= 0),
    PRIMARY KEY (show_id, user_id)
);

-- Idempotency: the same key for the same user can only ever exist once
CREATE TABLE idempotency_keys (
    user_id        TEXT NOT NULL,
    key            TEXT NOT NULL,
    show_id        UUID NOT NULL,
    request_hash   TEXT NOT NULL,        -- detects same key + different seats
    reservation_id UUID NOT NULL,
    response       JSONB NOT NULL,       -- replayed on retries
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)           -- scoped per user
);