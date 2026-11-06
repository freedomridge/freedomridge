-- A re-fitted map may have no model file: new samples on it are placed from their nearest mapped neighbours, and
-- a map fitted on a compute worker never brings a model (a model is a pickle, which runs code when loaded). A model
-- file still needs the model id it belongs to.
SET LOCAL lock_timeout = '2s';

ALTER TABLE embedding_map_state DROP CONSTRAINT embedding_map_state_check;
ALTER TABLE embedding_map_state ADD CONSTRAINT embedding_map_state_artifact_needs_model
    CHECK (artifact_location IS NULL OR model_id IS NOT NULL);
