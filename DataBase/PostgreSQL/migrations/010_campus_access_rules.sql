CREATE TABLE public.campus_access_rules (
    service VARCHAR(24) NOT NULL,
    resource_id VARCHAR(100) NOT NULL DEFAULT '',
    audience JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(audience) = 'object'),
    blocked BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (service, resource_id),
    CHECK ((service = 'classroom' AND resource_id <> '') OR (resource_id = '' AND NOT blocked))
);

REVOKE ALL ON public.campus_access_rules FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.campus_access_rules TO agent_app;
    END IF;
END $$;
