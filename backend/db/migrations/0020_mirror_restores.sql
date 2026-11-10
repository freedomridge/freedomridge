-- After a primary is restored into a new database (a 410), a follower holds each copy of a dataset the primary still
-- shares and asks its admin whether to resume into it (docs/FOLLOWERS.md): sample ids can't be trusted across a
-- restore, so the copy's samples are matched to the primary's by file path, and by image hash where both have one.
SET LOCAL lock_timeout = '2s';

CREATE TABLE IF NOT EXISTS mirror_restores (
    dataset_id INTEGER PRIMARY KEY REFERENCES mirrors(dataset_id) ON DELETE CASCADE,  -- the stopped copy held
    link_id INTEGER NOT NULL REFERENCES primary_links(id) ON DELETE CASCADE,
    origin_dataset_id INTEGER NOT NULL,
    origin_name TEXT,  -- the dataset's name on the restored primary, shown beside the copy's
    primary_identity TEXT NOT NULL,  -- the restored primary database's
    hash_field TEXT,  -- the image hash compared: the lowest field name both sides have, or none
    compare_after BIGINT NOT NULL DEFAULT 0,  -- the comparison's keyset cursor (the primary's sample id)
    total BIGINT NOT NULL DEFAULT 0,  -- the primary's samples compared
    matched BIGINT NOT NULL DEFAULT 0,  -- matched by file path, and by image hash where both have one
    matched_hash BIGINT NOT NULL DEFAULT 0,  -- of those, matched by image hash too
    only_here BIGINT,  -- the copy's samples the primary no longer has
    computed_at TIMESTAMPTZ,  -- the comparison is done, and the question shows
    -- resume or new: its admin's choice; drop: no longer shared; redo: the primary was restored again, compare again.
    decision TEXT CHECK (decision IN ('resume', 'new', 'drop', 'redo')),
    decided_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    decided_at TIMESTAMPTZ,
    phase TEXT NOT NULL DEFAULT 'compare'
        CHECK (phase IN ('compare', 'ask', 'unmatched', 'negate', 'move', 'views', 'map', 'resume')),
    -- A step that failed, as for a mirror: shown on the copy's row, tried again later, the other mirrors carry on.
    last_error TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    retry_after TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mirror_restores_link ON mirror_restores (link_id);

-- The copy's samples matched to the restored primary's, while it's held: applied, in bounded steps, if its admin
-- chooses to resume.
CREATE TABLE IF NOT EXISTS mirror_restore_map (
    dataset_id INTEGER NOT NULL REFERENCES mirror_restores(dataset_id) ON DELETE CASCADE,
    sample_id INTEGER NOT NULL,
    new_origin_id INTEGER NOT NULL,
    PRIMARY KEY (dataset_id, sample_id),
    UNIQUE (dataset_id, new_origin_id)
);
