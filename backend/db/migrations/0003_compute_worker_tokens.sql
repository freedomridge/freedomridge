-- Compute workers: an API token can be a worker token, which only the worker endpoints accept
-- (`python -m fridge.worker`), and a registered worker records its owner and its SDK version.
SET LOCAL lock_timeout = '2s';

ALTER TABLE api_tokens ADD COLUMN scope TEXT NOT NULL DEFAULT 'full'
    CONSTRAINT api_tokens_scope_check CHECK (scope IN ('full', 'worker'));

ALTER TABLE workers
    ADD COLUMN user_id INTEGER CONSTRAINT workers_user_id_fkey REFERENCES users(id) ON DELETE CASCADE,
    ADD COLUMN sdk_version TEXT;
