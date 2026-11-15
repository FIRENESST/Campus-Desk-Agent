ALTER TABLE runs
    ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
    ADD COLUMN retry_key VARCHAR(128),
    ADD COLUMN withdrawn_at TIMESTAMPTZ,
    ADD COLUMN withdrawal_started_at TIMESTAMPTZ,
    ADD COLUMN withdrawal_claim UUID,
    ADD COLUMN withdrawal_expires_at TIMESTAMPTZ,
    ADD COLUMN chat_effects_tracked BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE runs ALTER COLUMN chat_effects_tracked SET DEFAULT TRUE;

CREATE INDEX runs_active_withdrawal_idx ON runs(conversation_id, withdrawal_expires_at)
    WHERE withdrawal_expires_at IS NOT NULL;

ALTER TABLE campus_minta_operations ADD COLUMN restores_operation_id UUID
    REFERENCES campus_minta_operations(id) ON DELETE SET NULL;
CREATE INDEX campus_minta_restores_idx ON campus_minta_operations(restores_operation_id)
    WHERE restores_operation_id IS NOT NULL;

CREATE TABLE chat_turn_effects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind VARCHAR(12) NOT NULL CHECK (kind IN ('start', 'close', 'club')),
    label TEXT NOT NULL,
    operation_id UUID REFERENCES campus_minta_operations(id) ON DELETE SET NULL,
    state VARCHAR(12) NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'applied', 'no_effect', 'undone')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX chat_turn_effects_run_idx ON chat_turn_effects(run_id, created_at DESC, id DESC);
CREATE INDEX chat_turn_effects_operation_idx ON chat_turn_effects(operation_id) WHERE operation_id IS NOT NULL;

-- Older starts can be linked exactly by the chat's existing idempotency key and owner.
INSERT INTO chat_turn_effects(run_id,kind,label,operation_id,state)
SELECT r.id, 'start',
    (CASE o.service WHEN 'library' THEN '归还' WHEN 'club' THEN '取消报名' ELSE '取消预约' END)
      || '「' || COALESCE(o.resource_name,o.resource_id) || '」', o.id,
    CASE WHEN o.state='failed' THEN 'no_effect'
         WHEN o.state IN ('pending','uncertain') THEN 'pending' ELSE 'applied' END
FROM campus_minta_operations o
JOIN runs r ON r.id=(CASE WHEN o.request_key ~ '^chat-[0-9a-f]{32}$' THEN substring(o.request_key FROM 6)::uuid END)
JOIN conversations c ON c.id=r.conversation_id AND c.user_id=o.user_id
WHERE o.request_key ~ '^chat-[0-9a-f]{32}$';

REVOKE ALL ON chat_turn_effects FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agent_app') THEN
        GRANT SELECT,INSERT,UPDATE ON chat_turn_effects TO agent_app;
    END IF;
END $$;
