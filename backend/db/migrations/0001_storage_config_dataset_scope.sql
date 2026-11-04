-- Storage configs can be limited to chosen datasets, and a config change reaches every backend worker
-- at once.
--
-- restricted = FALSE (every existing config, and the default for new ones): the config serves every
-- dataset, as before. restricted = TRUE: it serves only the datasets listed in storage_config_datasets.
-- The default config serves datasets without a config of their own, so it can't be restricted.
--
-- updated_at changes on every UPDATE (trigger below). Workers cache one S3 client per config and
-- rebuild it when the row's updated_at differs, so new credentials apply everywhere on the next request.
SET LOCAL lock_timeout = '2s';

ALTER TABLE storage_configs ADD COLUMN restricted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE storage_configs ADD COLUMN updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now();
ALTER TABLE storage_configs
    ADD CONSTRAINT storage_configs_default_not_restricted CHECK (NOT (is_default AND restricted));

CREATE TABLE storage_config_datasets (
    storage_config_id INTEGER NOT NULL REFERENCES storage_configs(id) ON DELETE CASCADE,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    PRIMARY KEY (storage_config_id, dataset_id)
);

CREATE OR REPLACE FUNCTION storage_configs_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_storage_configs_touch_updated_at
    BEFORE UPDATE ON storage_configs
    FOR EACH ROW
    EXECUTE FUNCTION storage_configs_touch_updated_at();
