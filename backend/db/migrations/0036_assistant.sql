-- The assistant (docs/ASSISTANT.md): who may use it, and the datasets it may not read. All of it belongs to this
-- installation: followers never read it, nothing of it is synced, and the datasets trigger logs no change for
-- assistant_closed (log_dataset_settings names the columns it logs).
--
-- assistant_settings has one row. The MCP door starts off, and of the roles only administrators are let in: an
-- administrator switches it on in Settings → Assistant. seal_key is a random key the server makes for itself here,
-- which seals a private field's values for the assistant (backend/assistant/private.py): never derived from a
-- configured secret, which has a public default. A group listed in assistant_groups lets its members in; a
-- user's own answer in assistant_users decides for that user before their groups and role. Deleting a group or a user
-- removes its entry.
--
-- datasets.assistant_closed is FALSE on every existing row: no row is rewritten and no trigger fires (a constant
-- default). A dataset created from now on starts closed while new_datasets_closed is on (the code that creates
-- datasets reads it).
SET LOCAL lock_timeout = '2s';

CREATE TABLE assistant_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    mcp_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    admin_allowed BOOLEAN NOT NULL DEFAULT TRUE,
    power_user_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    guest_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    new_datasets_closed BOOLEAN NOT NULL DEFAULT FALSE,
    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    seal_key BYTEA NOT NULL DEFAULT sha512((gen_random_uuid()::text || gen_random_uuid()::text || gen_random_uuid()::text
                                    || gen_random_uuid()::text)::bytea)
);
INSERT INTO assistant_settings (singleton) VALUES (TRUE);

CREATE TABLE assistant_groups (
    group_id INTEGER PRIMARY KEY REFERENCES groups(id) ON DELETE CASCADE,
    added_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    added_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE assistant_users (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    allowed BOOLEAN NOT NULL,
    set_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    set_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

ALTER TABLE datasets ADD COLUMN assistant_closed BOOLEAN NOT NULL DEFAULT FALSE;
