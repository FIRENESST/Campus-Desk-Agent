CREATE TABLE IF NOT EXISTS teacher_credentials (
    user_id TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    contact_digest TEXT NOT NULL,
    phone_tail TEXT NOT NULL,
    identity_digest TEXT NOT NULL,
    campus_linked INTEGER NOT NULL CHECK (campus_linked IN (0, 1)),
    issued_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL CHECK (expires_at > issued_at),
    revoked_at INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS teacher_active_contact
    ON teacher_credentials(contact_digest) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS credential_events (
    id INTEGER PRIMARY KEY,
    credential_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
