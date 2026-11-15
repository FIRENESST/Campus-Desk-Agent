CREATE TABLE public.campus_profiles (
    identity_id UUID PRIMARY KEY REFERENCES public.external_identities(id) ON DELETE CASCADE,
    role VARCHAR(16) NOT NULL CHECK (role IN ('student', 'teacher')),
    full_name VARCHAR(80) NOT NULL CHECK (btrim(full_name) <> ''),
    campus_number VARCHAR(32) NOT NULL CHECK (campus_number ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$'),
    college VARCHAR(100) NOT NULL CHECK (btrim(college) <> ''),
    grade VARCHAR(24),
    revision UUID NOT NULL DEFAULT gen_random_uuid(),
    reviewed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((role = 'teacher' AND grade IS NULL) OR (role = 'student' AND grade IS NOT NULL))
);

REVOKE ALL ON public.campus_profiles FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_app') THEN
        GRANT SELECT, INSERT, UPDATE ON public.campus_profiles TO agent_app;
    END IF;
END $$;
