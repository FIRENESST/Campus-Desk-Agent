CREATE TABLE public.calendar_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    label VARCHAR(60) NOT NULL CHECK (length(btrim(label)) > 0),
    token_hash CHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX calendar_subscriptions_owner ON public.calendar_subscriptions (user_id, created_at DESC, id);

REVOKE ALL ON public.calendar_subscriptions FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_app') THEN
        GRANT SELECT, INSERT, DELETE ON public.calendar_subscriptions TO agent_app;
    END IF;
END $$;
