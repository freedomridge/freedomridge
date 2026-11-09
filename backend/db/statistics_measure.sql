WITH definitions AS MATERIALIZED (
    SELECT * FROM jsonb_to_recordset($4::jsonb)
        AS definition(field_name TEXT, field_type TEXT, origin TEXT, token TEXT, label_list BOOLEAN)
), source AS MATERIALIZED (
    SELECT id, metadata, tags, media_type, __NATIVE__ AS native
    FROM samples WHERE dataset_id=$1 AND id >= $2 AND id < $3
), metadata_entries AS MATERIALIZED (
    SELECT source.id, definition.field_name, definition.field_type, definition.token, definition.label_list, entry.value
    FROM source CROSS JOIN LATERAL jsonb_each(metadata) entry
    JOIN definitions definition ON definition.field_name=entry.key AND definition.origin='metadata'
), overlay_entries AS MATERIALIZED (
    SELECT id, field_name, token, value, CASE
        WHEN jsonb_typeof(value)='object' THEN CASE
            WHEN value->>'type'='heatmap' THEN 'heatmap'
            WHEN value->>'type'='class_probabilities' THEN 'probability'
            WHEN value->>'_type'='cuboid' THEN NULL
            WHEN value ? 'maskPath' AND field_name<>'segmentation' THEN 'mask_overlay'
            WHEN value ? 'label' AND NOT value ? 'maskPath'
                 AND field_name NOT IN ('detections','polygons') THEN 'classification'
        END
        WHEN value->0->>'_type'='cuboid' THEN 'cuboid'
        WHEN jsonb_typeof(value->0->'bbox') IN ('object','array') THEN 'detection'
        WHEN value->0->>'_type'='keypoints' THEN 'keypoint'
        WHEN jsonb_typeof(value->0->'points')='array' OR jsonb_typeof(value->0->'paths')='array' THEN 'polyline'
        WHEN value->0 ? 'label' AND field_name NOT IN ('detections','polygons')
            THEN 'classification'
    END AS kind
    FROM metadata_entries
    WHERE jsonb_typeof(value)='object' OR (jsonb_typeof(value)='array' AND jsonb_typeof(value->0)='object')
), label_parents AS MATERIALIZED (
    SELECT field_name FROM definitions WHERE label_list
    UNION SELECT field_name FROM overlay_entries WHERE kind IN ('detection','polyline','keypoint','classification') AND jsonb_typeof(value)='array'
), entries AS MATERIALIZED (
    SELECT id, field_name, field_type, token, value FROM metadata_entries
    UNION ALL
    SELECT id, 'tags', 'enum', NULL, to_jsonb(tags) FROM source
    UNION ALL
    SELECT id, 'media_type', 'enum', NULL, jsonb_build_array(media_type) FROM source
    UNION ALL
    SELECT source.id, definition.field_name, 'enum', definition.token,
           COALESCE(native->definition.field_name, '[]'::jsonb)
    FROM source CROSS JOIN definitions definition
    WHERE definition.origin='native_array' AND definition.field_name NOT IN ('tags', 'media_type')
    UNION ALL
    SELECT id, field_name || '.label', 'enum', token, jsonb_path_query_array(value, '$[*].label')
    FROM metadata_entries
    WHERE field_name IN (SELECT field_name FROM label_parents) AND jsonb_typeof(value)='array'
      AND (field_name NOT IN ('ground_truth','predictions') OR value='[]'::jsonb OR jsonb_path_exists(value, '$[*].label'))
      AND NOT EXISTS (SELECT 1 FROM definitions own WHERE own.field_name=metadata_entries.field_name || '.label')
    UNION ALL
    SELECT id, 'segmentation.labels', 'enum', token, value->'labels'
    FROM metadata_entries WHERE field_name='segmentation' AND jsonb_typeof(value->'labels')='array'
    UNION ALL
    -- One index entry per sample, even when several fields have the same shape.
    SELECT id, '__' || kind || '_fields__', kind || '_index', NULL, jsonb_agg(field_name)
    FROM overlay_entries WHERE kind IS NOT NULL GROUP BY id, kind
), bounds AS (
    SELECT field_name, field_type, token AS definition_token,
        -- Every branch now emits at most one row per (sample, field). Avoid sorting
        -- the full JSONB values for count(DISTINCT id); a bounded hash aggregate suffices.
        count(*) FILTER (WHERE value <> 'null'::jsonb) AS sample_count,
        min(CASE WHEN field_type='number' AND jsonb_typeof(value)='number' THEN (value #>> '{}')::float8 END) AS min_value,
        max(CASE WHEN field_type='number' AND jsonb_typeof(value)='number' THEN (value #>> '{}')::float8 END) AS max_value,
        to_char(min(CASE WHEN field_type='datetime' THEN stats_datetime(value #>> '{}') END)
            AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS text_min,
        to_char(max(CASE WHEN field_type='datetime' THEN stats_datetime(value #>> '{}') END)
            AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS text_max
    FROM entries GROUP BY field_name, field_type, token
), distinct_values AS MATERIALIZED (
    SELECT DISTINCT field_name, field_type, item
    FROM entries CROSS JOIN LATERAL jsonb_array_elements_text(
        CASE WHEN field_type='enum' OR right(field_type, 6)='_index'
             THEN CASE WHEN jsonb_typeof(value)='array' THEN value ELSE '[]'::jsonb END
             WHEN field_type IN ('string', 'number', 'boolean')
                  AND jsonb_typeof(value) IN ('string', 'number', 'boolean') THEN jsonb_build_array(value)
             ELSE '[]'::jsonb END) expanded(item)
    WHERE item IS NOT NULL
), ranked AS (
    SELECT field_name, field_type, item, row_number() OVER (PARTITION BY field_name ORDER BY item) AS position
    FROM distinct_values
), lists AS (
    SELECT field_name, array_agg(item ORDER BY item) AS items FROM ranked
    WHERE right(field_type,6)='_index' OR position<=CASE WHEN field_type='enum' THEN 201 ELSE 101 END
    GROUP BY field_name
)
SELECT bounds.*, COALESCE(items[1:CASE WHEN right(field_type,6)='_index' THEN cardinality(items)
                                      WHEN field_type='enum' THEN 200 ELSE 100 END], '{}'::text[]) AS unique_values,
       COALESCE(right(field_type,6)='_index'
                OR cardinality(items)<=CASE WHEN field_type='enum' THEN 200 ELSE 100 END, TRUE) AS values_complete
FROM bounds LEFT JOIN lists USING (field_name)
ORDER BY field_name;
