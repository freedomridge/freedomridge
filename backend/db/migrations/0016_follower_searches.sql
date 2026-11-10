-- Patch searches relayed to a follower (docs/FOLLOWERS.md): a search on a patch scale only a follower holds waits here
-- until the follower, polling over its own connection, claims and answers it. Rows go after an hour.
SET LOCAL lock_timeout = '2s';

CREATE TABLE follower_searches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    follower_id INTEGER NOT NULL REFERENCES followers(id) ON DELETE CASCADE,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('patch', 'object_to_patch')),
    request JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'claimed', 'answered', 'failed', 'cancelled')),
    result JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    answered_at TIMESTAMPTZ
);
CREATE INDEX follower_searches_waiting ON follower_searches (follower_id, created_at) WHERE state = 'pending';
CREATE INDEX follower_searches_created ON follower_searches (created_at);
