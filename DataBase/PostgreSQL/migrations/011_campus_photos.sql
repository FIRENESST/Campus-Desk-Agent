CREATE TABLE public.campus_photos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uploader_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    place VARCHAR(14) NOT NULL CHECK (place ~ '^[0-9]{7}_[0-9]{6}$'),
    request_key VARCHAR(128) NOT NULL,
    request_digest CHAR(64) NOT NULL,
    image BYTEA NOT NULL CHECK (octet_length(image) BETWEEN 1 AND 2097152),
    status VARCHAR(8) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    reviewed_at TIMESTAMPTZ,
    reviewed_by UUID REFERENCES public.users(id) ON DELETE SET NULL,
    UNIQUE (uploader_id, request_key)
);

CREATE INDEX campus_photos_gallery ON public.campus_photos (place, created_at DESC, id DESC)
    WHERE status = 'approved';
CREATE INDEX campus_photos_review ON public.campus_photos (status, created_at DESC, id DESC);

REVOKE ALL ON public.campus_photos FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_app') THEN
        GRANT SELECT, INSERT, UPDATE ON public.campus_photos TO agent_app;
    END IF;
END $$;
