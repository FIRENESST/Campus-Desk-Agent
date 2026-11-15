-- Applied by the migration command, inside a versioned transaction.
-- Supports both a new database and the existing four-table installation.
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(320) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name VARCHAR(50),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT users_email_lowercase_check CHECK (email = lower(email))
);
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(200) NOT NULL DEFAULT '新会话',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CONSTRAINT runs_id_conversation_unique UNIQUE (id, conversation_id),
    CONSTRAINT runs_status_check CHECK (status IN ('queued','running','succeeded','failed','cancelled'))
);
CREATE TABLE IF NOT EXISTS messages (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    run_id UUID,
    role VARCHAR(20) NOT NULL CHECK (role IN ('system','user','assistant','tool')),
    content TEXT NOT NULL DEFAULT '',
    tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(tool_calls) = 'array'),
    tool_call_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, conversation_id) REFERENCES runs(id, conversation_id) ON DELETE SET NULL (run_id),
    CHECK (role <> 'tool' OR tool_call_id IS NOT NULL)
);
ALTER TABLE users ADD COLUMN verified_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE agent_instances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL CHECK (length(trim(name)) > 0),
    profile VARCHAR(40) NOT NULL DEFAULT 'campus' CHECK (profile = 'campus'),
    status VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, user_id)
);
-- Existing conversations are attached to a private default instance per user.
INSERT INTO agent_instances (user_id, name)
SELECT DISTINCT user_id, '默认实例' FROM conversations;
ALTER TABLE conversations ADD COLUMN instance_id UUID;
UPDATE conversations c SET instance_id = i.id FROM agent_instances i WHERE i.user_id = c.user_id;
ALTER TABLE conversations ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE conversations ADD CONSTRAINT conversations_instance_owner_fk
    FOREIGN KEY (instance_id, user_id) REFERENCES agent_instances(id, user_id) ON DELETE CASCADE;

ALTER TABLE runs ADD COLUMN request_key VARCHAR(128);
ALTER TABLE runs ADD COLUMN input_hash TEXT;
ALTER TABLE runs ADD COLUMN input JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE runs ADD COLUMN result_status VARCHAR(40);
ALTER TABLE runs ADD COLUMN worker_id UUID;
ALTER TABLE runs ADD COLUMN lease_expires_at TIMESTAMPTZ;
ALTER TABLE runs ADD COLUMN cancel_requested_at TIMESTAMPTZ;

CREATE TABLE auth_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label VARCHAR(200) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);
CREATE TABLE auth_tokens (
    token_hash TEXT PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES auth_sessions(id) ON DELETE CASCADE,
    kind VARCHAR(8) NOT NULL CHECK (kind IN ('access','refresh')),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE account_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose VARCHAR(8) NOT NULL CHECK (purpose IN ('verify','reset')),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE auth_rate_limits (
    key_hash TEXT PRIMARY KEY,
    window_start TIMESTAMPTZ NOT NULL DEFAULT now(),
    hits INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE audit_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    action VARCHAR(50) NOT NULL,
    outcome VARCHAR(30) NOT NULL,
    subject_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE run_steps (
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step_id VARCHAR(64) NOT NULL,
    agent_id VARCHAR(40) NOT NULL,
    status VARCHAR(40) NOT NULL,
    result JSONB NOT NULL,
    PRIMARY KEY (run_id, step_id)
);
CREATE TABLE run_artifacts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind VARCHAR(40) NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX agent_instances_owner_idx ON agent_instances(user_id, created_at DESC, id DESC);
CREATE INDEX conversations_instance_order_idx ON conversations(instance_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS conversations_user_activity_idx ON conversations(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS messages_conversation_order_idx ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS messages_run_order_idx ON messages(run_id, id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS runs_conversation_history_idx ON runs(conversation_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active_per_conversation_idx
    ON runs(conversation_id) WHERE status IN ('queued','running');
CREATE UNIQUE INDEX runs_idempotency_idx ON runs(conversation_id, request_key) WHERE request_key IS NOT NULL;
CREATE INDEX runs_dispatch_idx ON runs(created_at, id) WHERE status = 'queued';
CREATE INDEX runs_expired_lease_idx ON runs(lease_expires_at) WHERE status = 'running';
CREATE INDEX auth_sessions_owner_idx ON auth_sessions(user_id, created_at DESC);
CREATE INDEX auth_tokens_session_idx ON auth_tokens(session_id);
CREATE INDEX account_tokens_owner_idx ON account_tokens(user_id, purpose);
CREATE INDEX run_artifacts_run_idx ON run_artifacts(run_id, id);
CREATE INDEX audit_events_owner_idx ON audit_events(user_id, created_at DESC);

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_app') THEN
        GRANT USAGE ON SCHEMA public TO agent_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON users, conversations, messages, runs,
            agent_instances, auth_sessions, auth_tokens, account_tokens, auth_rate_limits,
            audit_events, run_steps, run_artifacts TO agent_app;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO agent_app;
    END IF;
END $$;
