BEGIN;

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
    CONSTRAINT runs_status_check CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
    )
);

CREATE TABLE IF NOT EXISTS messages (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    run_id UUID,
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb,
    tool_call_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT messages_run_conversation_fk
        FOREIGN KEY (run_id, conversation_id)
        REFERENCES runs(id, conversation_id)
        ON DELETE SET NULL (run_id),
    CONSTRAINT messages_role_check CHECK (
        role IN ('system', 'user', 'assistant', 'tool')
    ),
    CONSTRAINT messages_tool_call_id_check CHECK (
        role <> 'tool' OR tool_call_id IS NOT NULL
    ),
    CONSTRAINT messages_tool_calls_array_check CHECK (
        jsonb_typeof(tool_calls) = 'array'
    )
);

CREATE INDEX IF NOT EXISTS conversations_user_activity_idx
    ON conversations (user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS messages_conversation_order_idx
    ON messages (conversation_id, id);

CREATE INDEX IF NOT EXISTS messages_run_order_idx
    ON messages (run_id, id)
    WHERE run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS runs_conversation_history_idx
    ON runs (conversation_id, created_at DESC);

-- 同一会话最多只能有一个排队中或运行中的任务。
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active_per_conversation_idx
    ON runs (conversation_id)
    WHERE status IN ('queued', 'running');

REVOKE ALL ON DATABASE cloud_agent FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;

GRANT CONNECT ON DATABASE cloud_agent TO agent_app;
GRANT USAGE ON SCHEMA public TO agent_app;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLE users, conversations, runs, messages TO agent_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO agent_app;

COMMIT;
