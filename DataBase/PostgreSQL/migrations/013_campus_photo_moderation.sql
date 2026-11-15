CREATE INDEX campus_photos_review_place ON public.campus_photos (place, status, created_at DESC, id DESC);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_app') THEN
        GRANT DELETE ON public.campus_photos TO agent_app;
    END IF;
END $$;
