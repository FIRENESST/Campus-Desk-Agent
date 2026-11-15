ALTER TABLE public.users
    ADD COLUMN role VARCHAR(16) NOT NULL DEFAULT 'student',
    ADD COLUMN academic_title VARCHAR(50),
    ADD CONSTRAINT users_role_check CHECK (role IN ('student', 'teacher')),
    ADD CONSTRAINT users_academic_title_check CHECK (
        academic_title IS NULL OR (
            role = 'teacher' AND btrim(academic_title) <> '' AND academic_title !~ '[[:cntrl:]]'
        )
    );

-- Keep the application role's existing write access to account fields.
-- Assigning a campus role or title requires a privileged database connection.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_app') THEN
        REVOKE INSERT, UPDATE ON TABLE public.users FROM agent_app;
        GRANT INSERT (id, email, password_hash, display_name, created_at, updated_at, verified_at, is_active),
              UPDATE (id, email, password_hash, display_name, created_at, updated_at, verified_at, is_active)
            ON TABLE public.users TO agent_app;
    END IF;
END $$;
