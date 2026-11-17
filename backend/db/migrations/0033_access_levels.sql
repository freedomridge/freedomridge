-- Access levels, part one: groups of users, and a level on every share of a dataset, a user's own (dataset_access)
-- or a group's (dataset_group_access): 1 view, 2 tag, 3 edit, 4 manage, each including the ones before it. Numbers,
-- so SQL takes the highest; the API and the SDK speak the names (backend/access.py). A global dataset gives every
-- signed-in user its everyone_level (view, tag or edit), read only while it is global. It starts at view, so a dataset
-- made global from now on gives view until a manager raises it.
--
-- Nobody's access changes. Every share is edit, which is what a share gave until now, and every dataset already global
-- gives edit. A dataset's owner_id stops giving access by itself, so each owner without a share is given an edit share
-- here; owner_id stays, as who created the dataset. Shares, groups and everyone_level belong to this installation:
-- followers never read them, and the datasets trigger logs no change for everyone_level.
--
-- Only small tables: users, datasets and their shares. Nothing reads or rewrites samples.
SET LOCAL lock_timeout = '2s';

CREATE TABLE groups (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE group_members (
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX idx_group_members_user ON group_members(user_id);

-- Existing shares take the default: edit.
ALTER TABLE dataset_access ADD COLUMN level SMALLINT NOT NULL DEFAULT 3
    CONSTRAINT dataset_access_level_check CHECK (level BETWEEN 1 AND 4);

CREATE TABLE dataset_group_access (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    level SMALLINT NOT NULL CONSTRAINT dataset_group_access_level_check CHECK (level BETWEEN 1 AND 4),
    granted_by_user_id INTEGER REFERENCES users(id),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dataset_id, group_id)
);
CREATE INDEX idx_dataset_group_access_group ON dataset_group_access(group_id);
-- Guarded like dataset_access: no share is written to a dataset being deleted (schema.sql, the deletion guards).
CREATE TRIGGER deletion_guard_insert AFTER INSERT ON dataset_group_access REFERENCING NEW TABLE AS deletion_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION guard_dataset_rows('dataset');
CREATE TRIGGER deletion_guard_update BEFORE UPDATE ON dataset_group_access FOR EACH ROW
    WHEN (OLD.dataset_id IS DISTINCT FROM NEW.dataset_id
          OR NEW.dataset_id::text IS DISTINCT FROM current_setting('fridge.writable_dataset', TRUE))
    EXECUTE FUNCTION guard_dataset_update('dataset');
CREATE TRIGGER deletion_guard_delete AFTER DELETE ON dataset_group_access REFERENCING OLD TABLE AS deletion_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION guard_dataset_rows('dataset');

-- View for every dataset, then edit for the ones already global: they give edit, as they did.
ALTER TABLE datasets ADD COLUMN everyone_level SMALLINT NOT NULL DEFAULT 1
    CONSTRAINT datasets_everyone_level_check CHECK (everyone_level BETWEEN 1 AND 3);
-- The guard refuses any write to a dataset being deleted, so it is off for this statement, on purpose: the column is
-- filled in, nothing else changes. The settings trigger logs nothing for it.
ALTER TABLE datasets DISABLE TRIGGER dataset_write_guard;
UPDATE datasets SET everyone_level = 3 WHERE is_global;
ALTER TABLE datasets ENABLE TRIGGER dataset_write_guard;

-- Each owner without a share gets an edit share. A dataset being deleted too: its owner still sees it in the list until
-- the deletion finishes, as before. The guard refuses any share written to such a dataset, so it is off for this
-- statement, on purpose; the deletion removes the share with the dataset's others.
ALTER TABLE dataset_access DISABLE TRIGGER deletion_guard_insert;
INSERT INTO dataset_access (dataset_id, user_id, level)
SELECT id, owner_id, 3 FROM datasets WHERE owner_id IS NOT NULL
ON CONFLICT (dataset_id, user_id) DO NOTHING;
ALTER TABLE dataset_access ENABLE TRIGGER deletion_guard_insert;
