-- The assistant's administration tools (docs/ASSISTANT.md, "Administration tools"): four tools that read this server's
-- status, background jobs, backups and index advice for an administrator's AI client. assistant_settings gains their
-- switch, admin_tools, off as shipped. Nothing else stored changes.
SET LOCAL lock_timeout = '2s';

ALTER TABLE assistant_settings ADD COLUMN admin_tools BOOLEAN NOT NULL DEFAULT FALSE;
