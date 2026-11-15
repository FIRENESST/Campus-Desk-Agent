ALTER TABLE campus_minta_operations
    ADD COLUMN classroom_starts_at TIMESTAMPTZ,
    ADD COLUMN classroom_ends_at TIMESTAMPTZ,
    ADD CONSTRAINT campus_minta_classroom_times CHECK (
        (classroom_starts_at IS NULL AND classroom_ends_at IS NULL)
        OR (service = 'classroom' AND classroom_starts_at IS NOT NULL
            AND classroom_ends_at IS NOT NULL AND classroom_ends_at > classroom_starts_at)
    );

DROP INDEX campus_minta_one_open_resource;
CREATE UNIQUE INDEX campus_minta_one_open_resource ON campus_minta_operations
    (user_id, service, resource_id, gym_slot_id, COALESCE(classroom_starts_at, '-infinity'::timestamptz))
    WHERE state IN ('pending', 'active', 'uncertain', 'closing', 'close_uncertain');
CREATE INDEX campus_minta_classroom_calendar ON campus_minta_operations (user_id, classroom_starts_at)
    WHERE service='classroom' AND classroom_starts_at IS NOT NULL;

CREATE TABLE personal_calendar_events (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    id UUID NOT NULL,
    title VARCHAR(120) NOT NULL CHECK (length(btrim(title)) > 0),
    location VARCHAR(180) NOT NULL DEFAULT '',
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL CHECK (ends_at > starts_at),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    PRIMARY KEY (user_id, id)
);
CREATE INDEX personal_calendar_events_time ON personal_calendar_events (user_id, starts_at, id);

REVOKE ALL ON personal_calendar_events FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON personal_calendar_events TO agent_app;
    END IF;
END $$;
