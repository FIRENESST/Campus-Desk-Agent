ALTER TABLE public.account_tokens
    DROP CONSTRAINT account_tokens_purpose_check,
    ADD CONSTRAINT account_tokens_purpose_check CHECK (purpose IN ('verify','reset','change')),
    ADD COLUMN session_id UUID REFERENCES public.auth_sessions(id) ON DELETE CASCADE,
    ADD CONSTRAINT account_tokens_change_session_check CHECK ((purpose = 'change') = (session_id IS NOT NULL));

UPDATE public.account_tokens
SET expires_at = created_at + INTERVAL '10 minutes'
WHERE purpose = 'verify' AND expires_at > created_at + INTERVAL '10 minutes';

ALTER TABLE public.auth_tokens
    DROP CONSTRAINT auth_tokens_kind_check,
    ADD CONSTRAINT auth_tokens_kind_check CHECK (kind IN ('access','refresh','remember'));
