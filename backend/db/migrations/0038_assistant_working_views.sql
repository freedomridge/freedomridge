-- The assistant's working views and value lists (docs/ASSISTANT.md, "Working views"): the assistant's own rows, kept for
-- the user who made them and removed with their dataset, their user, or 30 days after they were last used (the job
-- service's housekeeping). Nothing here is a dataset's content: no saved view, no tag, nothing anyone else sees, and
-- followers never read them.
--
-- A working view's stages are the app's filter nodes, in order, except a stage taken from a value list, which names
-- the list ({"list": "vl_...", "field": ..., "exclude": ...}) and is expanded to an `in` filter when the view is used:
-- refining a view copies the list's handle, not its values. `lists` holds the handles its stages name, so the cleanup
-- keeps a list while a view that uses it is kept.
--
-- A value list keeps every value it found with its count (`entries`) when it has at most 10,000; a longer one keeps the
-- filter it was counted over (`source`), from which its download counts the values again.
--
-- assistant_settings gains sample_fields: whether the assistant may read samples' own fields and file paths. Off.
SET LOCAL lock_timeout = '2s';

ALTER TABLE assistant_settings ADD COLUMN sample_fields BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE assistant_views (
    handle TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    stages JSONB NOT NULL,
    lists TEXT[] NOT NULL DEFAULT '{}',
    from_handle TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    used_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
CREATE INDEX idx_assistant_views_dataset ON assistant_views (dataset_id, handle);
CREATE INDEX idx_assistant_views_user ON assistant_views (user_id);
CREATE INDEX idx_assistant_views_used ON assistant_views (used_at);
CREATE INDEX idx_assistant_views_lists ON assistant_views USING gin (lists);

CREATE TABLE assistant_lists (
    handle TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    value_kind TEXT NOT NULL CHECK (value_kind IN ('text', 'number', 'boolean')),
    distinct_count BIGINT NOT NULL,
    entries JSONB,
    source JSONB,
    search TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    used_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
CREATE INDEX idx_assistant_lists_dataset ON assistant_lists (dataset_id, handle);
CREATE INDEX idx_assistant_lists_user ON assistant_lists (user_id);
CREATE INDEX idx_assistant_lists_used ON assistant_lists (used_at);
