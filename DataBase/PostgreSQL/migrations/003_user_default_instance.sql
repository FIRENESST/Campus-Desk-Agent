-- Backfill the default instance before accepting any new accounts.
LOCK TABLE public.users IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE public.agent_instances ADD COLUMN is_default BOOLEAN NOT NULL DEFAULT FALSE;

-- Keep existing instances and their conversation ownership. Use the oldest one.
WITH oldest AS (
    SELECT DISTINCT ON (user_id) id
    FROM public.agent_instances
    ORDER BY user_id, created_at, id
)
UPDATE public.agent_instances AS instance
SET is_default = TRUE
FROM oldest
WHERE instance.id = oldest.id;

INSERT INTO public.agent_instances (user_id, name, is_default)
SELECT account.id, '默认实例', TRUE
FROM public.users AS account
WHERE NOT EXISTS (
    SELECT 1 FROM public.agent_instances AS instance WHERE instance.user_id = account.id
);

CREATE UNIQUE INDEX agent_instances_one_default_idx ON public.agent_instances (user_id)
    WHERE is_default;

CREATE FUNCTION public.create_user_default_instance() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    INSERT INTO public.agent_instances (user_id, name, is_default)
    VALUES (NEW.id, '默认实例', TRUE);
    RETURN NEW;
END;
$$;

CREATE TRIGGER users_create_default_instance
AFTER INSERT ON public.users
FOR EACH ROW EXECUTE FUNCTION public.create_user_default_instance();
