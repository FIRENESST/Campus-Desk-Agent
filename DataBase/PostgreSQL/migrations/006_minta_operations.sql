ALTER TABLE runs ADD COLUMN initiator_session_id UUID REFERENCES auth_sessions(id) ON DELETE SET NULL;

CREATE TABLE campus_minta_operations (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    request_key VARCHAR(128) NOT NULL,
    request_digest CHAR(64) NOT NULL,
    service VARCHAR(12) NOT NULL CHECK (service IN ('classroom', 'gym', 'club', 'library')),
    resource_id VARCHAR(96) NOT NULL,
    resource_name VARCHAR(180),
    teacher_credential_id UUID,
    upstream_id VARCHAR(128),
    state VARCHAR(16) NOT NULL CHECK (state IN ('pending', 'active', 'failed', 'uncertain', 'closing', 'close_uncertain', 'closed')),
    error_code VARCHAR(40),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (user_id, request_key)
);

CREATE UNIQUE INDEX campus_minta_one_open_resource ON campus_minta_operations(user_id, service, resource_id)
    WHERE state IN ('pending', 'active', 'uncertain', 'closing', 'close_uncertain');
CREATE INDEX campus_minta_history ON campus_minta_operations(user_id, service, created_at DESC);

REVOKE ALL ON campus_minta_operations FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agent_app') THEN
        GRANT SELECT,INSERT,UPDATE ON campus_minta_operations TO agent_app;
    END IF;
END $$;
