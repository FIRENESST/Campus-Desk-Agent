CREATE TABLE public.microsoft_todo_authorizations (
    user_id UUID PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    session_id UUID NOT NULL REFERENCES public.auth_sessions(id) ON DELETE CASCADE,
    state_hash CHAR(64) NOT NULL,
    verifier VARCHAR(43) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);

CREATE TABLE public.microsoft_todo_connections (
    user_id UUID PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    microsoft_id TEXT NOT NULL UNIQUE,
    account_name TEXT NOT NULL,
    refresh_token BYTEA NOT NULL,
    access_token BYTEA NOT NULL,
    access_expires_at TIMESTAMPTZ NOT NULL,
    list_id TEXT,
    calendar_hash CHAR(64),
    connected_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_synced_at TIMESTAMPTZ,
    next_sync_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    sync_error VARCHAR(32),
    claim_id UUID,
    claimed_until TIMESTAMPTZ
);

CREATE INDEX microsoft_todo_sync_due ON public.microsoft_todo_connections(next_sync_at);

REVOKE ALL ON public.microsoft_todo_authorizations, public.microsoft_todo_connections FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.microsoft_todo_authorizations, public.microsoft_todo_connections TO agent_app;
    END IF;
END $$;
