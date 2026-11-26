-- The MCP server at /mcp (backend/mcp_door.py, docs/ASSISTANT.md): its kind of API token, and the assistant's call log.
--
-- An 'mcp' token opens /mcp and nothing else: a token pasted into an AI client must not open the rest of the API
-- (routers/auth.py refuses it everywhere else as a token that doesn't exist).
--
-- assistant_calls holds one row per tool call made through a door ('mcp' now, 'chat' later): who, which tool, which
-- dataset when the arguments named one, when, how long, and whether it succeeded. Never the arguments or the
-- result. The job service removes rows older than 30 days (backend/assistant_calls.py). A deleted user leaves its
-- rows with no user; dataset_id is the number the arguments named and has no key, so deleting a dataset never
-- waits on, or fails because of, the log.
--
-- Small tables only: api_tokens is checked against the new list, and assistant_calls is new and empty.
SET LOCAL lock_timeout = '2s';

ALTER TABLE api_tokens DROP CONSTRAINT api_tokens_scope_check;
ALTER TABLE api_tokens ADD CONSTRAINT api_tokens_scope_check CHECK (scope IN ('full', 'worker', 'follower', 'mcp'));

CREATE TABLE assistant_calls (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    door TEXT NOT NULL CHECK (door IN ('mcp', 'chat')),
    tool TEXT NOT NULL,
    dataset_id INTEGER,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    ok BOOLEAN NOT NULL,
    error_code TEXT,
    CHECK (ok = (error_code IS NULL))
);

CREATE INDEX idx_assistant_calls_started_at ON assistant_calls(started_at);
CREATE INDEX idx_assistant_calls_user_id ON assistant_calls(user_id);
