ALTER TABLE campus_minta_operations
    ADD COLUMN gym_slot_id VARCHAR(40) NOT NULL DEFAULT '',
    ADD COLUMN gym_slot_details JSONB,
    ADD CONSTRAINT campus_minta_gym_slot CHECK (
        (service = 'gym' OR (gym_slot_id = '' AND gym_slot_details IS NULL))
        AND (gym_slot_details IS NULL OR (gym_slot_id <> '' AND jsonb_typeof(gym_slot_details) = 'object'))
    );

DROP INDEX campus_minta_one_open_resource;
CREATE UNIQUE INDEX campus_minta_one_open_resource
    ON campus_minta_operations(user_id, service, resource_id, gym_slot_id)
    WHERE state IN ('pending', 'active', 'uncertain', 'closing', 'close_uncertain');
