-- Follower instances, added on the primary (Settings → Followers): each has an account (follower:<name>,
-- no password) and a 'follower' token that only the follower endpoints accept, and follows chosen datasets
-- with an option each for tags and views. A dataset is followed (datasets.followed, read by the change log of
-- 0007) while any follower follows it. Small tables only.
SET LOCAL lock_timeout = '2s';

ALTER TABLE api_tokens DROP CONSTRAINT api_tokens_scope_check;
ALTER TABLE api_tokens ADD CONSTRAINT api_tokens_scope_check CHECK (scope IN ('full', 'worker', 'follower'));

CREATE TABLE followers (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_id INTEGER REFERENCES api_tokens(id) ON DELETE SET NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ
);

CREATE TABLE follower_datasets (
    follower_id INTEGER NOT NULL REFERENCES followers(id) ON DELETE CASCADE,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    tags TEXT NOT NULL DEFAULT 'primary' CHECK (tags IN ('primary', 'both')),
    views TEXT NOT NULL DEFAULT 'primary' CHECK (views IN ('primary', 'both')),
    -- Set just after the subscription commits: changes from transactions at or after it are in the log, and the
    -- follower's full copy starts once every earlier one has ended (a write in flight when the dataset became
    -- followed may not have seen the flag). NULL until then.
    start_txid BIGINT,
    acked_txid BIGINT,
    subscribed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (follower_id, dataset_id)
);
CREATE INDEX follower_datasets_dataset ON follower_datasets (dataset_id);

CREATE OR REPLACE FUNCTION sync_dataset_followed() RETURNS trigger AS $$
BEGIN
    UPDATE datasets d SET followed = EXISTS (SELECT 1 FROM follower_datasets f WHERE f.dataset_id = d.id)
    WHERE d.id IN (SELECT dataset_id FROM changed)
      AND d.followed IS DISTINCT FROM EXISTS (SELECT 1 FROM follower_datasets f WHERE f.dataset_id = d.id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER follower_datasets_followed_insert AFTER INSERT ON follower_datasets REFERENCING NEW TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION sync_dataset_followed();
CREATE TRIGGER follower_datasets_followed_delete AFTER DELETE ON follower_datasets REFERENCING OLD TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION sync_dataset_followed();
