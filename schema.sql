-- Kudos portal schema. The application executes this file on an empty database.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('employee', 'admin')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kudos (
    id INTEGER PRIMARY KEY,
    sender_id INTEGER NOT NULL REFERENCES users (id),
    recipient_id INTEGER NOT NULL REFERENCES users (id),
    message TEXT NOT NULL CHECK (length(message) BETWEEN 1 AND 500),
    created_at TEXT NOT NULL,
    is_visible INTEGER NOT NULL DEFAULT 1 CHECK (is_visible IN (0, 1)),
    moderated_by INTEGER REFERENCES users (id),
    moderated_at TEXT,
    reason_for_moderation TEXT,
    CHECK (sender_id <> recipient_id)
);

-- kudos_id is intentionally not a foreign key. A delete must keep the audit row.
CREATE TABLE IF NOT EXISTS moderation_events (
    id INTEGER PRIMARY KEY,
    kudos_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('hide', 'restore', 'delete')),
    actor_id INTEGER NOT NULL REFERENCES users (id),
    reason TEXT,
    message_snapshot TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kudos_feed ON kudos (is_visible, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_kudos_sender_created ON kudos (sender_id, created_at);
CREATE INDEX IF NOT EXISTS idx_kudos_pair_created ON kudos (sender_id, recipient_id, created_at);
