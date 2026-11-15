CREATE TABLE public.user_wechat_following (
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    account_id VARCHAR(64) NOT NULL CHECK (account_id ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (user_id, account_id)
);

REVOKE ALL ON public.user_wechat_following FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_app') THEN
        GRANT SELECT, INSERT, DELETE ON public.user_wechat_following TO agent_app;
    END IF;
END $$;
