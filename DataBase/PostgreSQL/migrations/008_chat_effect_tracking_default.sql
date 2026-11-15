-- Older application processes may still accept turns during a rolling update.
ALTER TABLE runs ALTER COLUMN chat_effects_tracked SET DEFAULT FALSE;
