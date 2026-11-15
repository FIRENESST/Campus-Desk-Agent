-- External identity is (issuer, subject), never email or display name.
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;
ALTER TABLE auth_sessions ADD COLUMN auth_method VARCHAR(12) NOT NULL DEFAULT 'password'
    CHECK (auth_method IN ('password', 'campus_sso'));

CREATE TABLE external_identities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    protocol VARCHAR(8) NOT NULL CHECK (protocol IN ('oidc','cas','saml2')),
    issuer TEXT NOT NULL,
    subject VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (protocol, issuer, subject)
);
CREATE INDEX external_identities_owner_idx ON external_identities(user_id);

CREATE TABLE sso_transactions (
    state_hash TEXT PRIMARY KEY,
    issuer TEXT NOT NULL,
    nonce_hash TEXT NOT NULL,
    browser_hash TEXT NOT NULL,
    client_challenge VARCHAR(43) NOT NULL,
    protocol VARCHAR(8) NOT NULL CHECK (protocol IN ('oidc','cas','saml2')),
    request_id VARCHAR(255),
    mode VARCHAR(8) NOT NULL CHECK (mode IN ('login','link')),
    link_user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    link_session_id UUID REFERENCES auth_sessions(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '10 minutes',
    consumed_at TIMESTAMPTZ,
    CHECK ((mode='login' AND link_user_id IS NULL AND link_session_id IS NULL)
        OR (mode='link' AND link_user_id IS NOT NULL AND link_session_id IS NOT NULL))
);
CREATE TABLE sso_completions (
    code_hash TEXT PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    protocol VARCHAR(8) NOT NULL CHECK (protocol IN ('oidc','cas','saml2')),
    identity JSONB NOT NULL,
    client_challenge VARCHAR(43) NOT NULL,
    mode VARCHAR(8) NOT NULL CHECK (mode IN ('login','link')),
    link_session_id UUID REFERENCES auth_sessions(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '1 minute',
    consumed_at TIMESTAMPTZ
);
CREATE INDEX sso_transactions_expiry_idx ON sso_transactions(expires_at);
CREATE INDEX sso_completions_expiry_idx ON sso_completions(expires_at);
CREATE TABLE sso_assertion_replays (
    issuer TEXT NOT NULL,
    assertion_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '1 day',
    PRIMARY KEY (issuer, assertion_hash)
);
REVOKE ALL ON external_identities,sso_transactions,sso_completions,sso_assertion_replays FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agent_app') THEN
        GRANT SELECT,INSERT,UPDATE,DELETE ON external_identities,sso_transactions,sso_completions,sso_assertion_replays TO agent_app;
    END IF;
END $$;
