-- Named regions: countries, states or provinces, and named groups of states, loaded from a public boundary set
-- (backend/regions.py, docs/REGIONS.md), for the geo_region and geo_region_exclude filters. They belong to this
-- installation: a set is loaded by its administrator, followers never read these tables, and loading a set again
-- replaces that set's rows only. New and empty tables, so nothing else is read or rewritten.
SET LOCAL lock_timeout = '2s';

-- One row per loaded set: which version, from which file, when and by whom, and what the load decided (report).
CREATE TABLE region_sets (
    name TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    verified BOOLEAN NOT NULL,  -- the file was the set's pinned one
    loaded_from TEXT NOT NULL CONSTRAINT region_sets_loaded_from_check CHECK (loaded_from IN ('download', 'file')),
    loaded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- NULL from the command line
    loaded_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    report JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Each region whole: its key (US-KS, US, US:midwest), its name and country, its bounding box (west > east across the
-- antimeridian, as in GeoJSON) and its polygons.
CREATE TABLE regions (
    id SERIAL PRIMARY KEY,
    set_name TEXT NOT NULL REFERENCES region_sets(name) ON DELETE CASCADE,
    kind TEXT NOT NULL CONSTRAINT regions_kind_check CHECK (kind IN ('country', 'state', 'group')),
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    country_key TEXT NOT NULL,
    country_name TEXT NOT NULL,
    west DOUBLE PRECISION NOT NULL,
    south DOUBLE PRECISION NOT NULL,
    east DOUBLE PRECISION NOT NULL,
    north DOUBLE PRECISION NOT NULL,
    geom GEOMETRY(MultiPolygon, 4326) NOT NULL
);

-- Each region cut into pieces of at most 256 vertices: a location is tested against the few pieces around it, not
-- against a coastline of thousands of points.
CREATE TABLE region_parts (
    region_id INTEGER NOT NULL REFERENCES regions(id) ON DELETE CASCADE,
    geom GEOMETRY(Polygon, 4326) NOT NULL
);
CREATE INDEX idx_region_parts_geom ON region_parts USING gist (geom);
CREATE INDEX idx_region_parts_region ON region_parts (region_id);

-- The quarter-degree cells each region touches: cell (i, j) holds the longitudes from i/4 to (i + 1)/4 and the
-- latitudes from j/4 to (j + 1)/4. `inside`: one piece of the region covers the whole cell, so every location in it
-- is in the region; otherwise the cell holds some of the region's border, and a location in it is tested exactly. A
-- location in no cell of the region is outside it. Memory for the per-row test follows the region's size, never its
-- samples.
CREATE TABLE region_cells (
    region_id INTEGER NOT NULL REFERENCES regions(id) ON DELETE CASCADE,
    i INTEGER NOT NULL,
    j INTEGER NOT NULL,
    inside BOOLEAN NOT NULL,
    PRIMARY KEY (region_id, i, j)
);

-- Up to `how_many` keys of regions close to what was written: its key or name in another case, then names that start
-- with it, then keys and names sharing most of its letter pairs (Dice similarity of at least 0.6: 'US-KZ' finds US-KS
-- and US-KY, 'Kanass' Kansas).
CREATE OR REPLACE FUNCTION region_suggestions(written TEXT, how_many INTEGER DEFAULT 3)
RETURNS TEXT[] AS $$
    WITH typed AS (
        SELECT lower(written) AS text,
               ARRAY(SELECT DISTINCT substr(lower(written), i, 2) FROM generate_series(1, length(written) - 1) AS i) AS pairs
    ), scored AS (
        SELECT r.key, r.name, array_position(ARRAY['country', 'state', 'group'], r.kind) AS kind_order,
               CASE WHEN lower(r.key) = t.text THEN 0
                    WHEN lower(r.name) = t.text THEN 1
                    WHEN starts_with(lower(r.name), t.text) THEN 2
                    ELSE 3 END AS rank,
               greatest(
                   2.0 * (SELECT count(*) FROM unnest(t.pairs) AS pair WHERE strpos(lower(r.key), pair) > 0)
                       / NULLIF(length(r.key) - 1 + cardinality(t.pairs), 0),
                   2.0 * (SELECT count(*) FROM unnest(t.pairs) AS pair WHERE strpos(lower(r.name), pair) > 0)
                       / NULLIF(length(r.name) - 1 + cardinality(t.pairs), 0)
               ) AS likeness
        FROM regions r, typed t
    )
    SELECT COALESCE(array_agg(key ORDER BY rank, likeness DESC, kind_order, name, key), '{}') FROM (
        SELECT * FROM scored WHERE rank < 3 OR likeness >= 0.6
        ORDER BY rank, likeness DESC, kind_order, name, key
        LIMIT how_many
    ) best
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- What a filter naming a region this server doesn't have is told (a 400 through the app's DataError handler).
CREATE OR REPLACE FUNCTION unknown_region_message(region_key TEXT)
RETURNS TEXT AS $$
DECLARE
    close TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM regions) THEN
        RETURN format('Unknown region %L: no boundary set is loaded on this server', region_key);
    END IF;
    SELECT string_agg(format('%L (%s)', r.key, r.name), ' or ' ORDER BY s.position) INTO close
    FROM unnest(region_suggestions(region_key)) WITH ORDINALITY AS s(key, position) JOIN regions r USING (key);
    RETURN format('Unknown region %L: the boundary set isn''t loaded on this server or has no such region', region_key)
        || CASE WHEN close IS NULL THEN '' ELSE format(' (did you mean %s?)', close) END;
END;
$$ LANGUAGE plpgsql STABLE PARALLEL SAFE;

-- The region a filter names, by its key: a key no loaded set has is refused (SQLSTATE 22023), never matched as
-- nothing. It only reads, and an error raised in a parallel worker reaches the client with its SQLSTATE.
CREATE OR REPLACE FUNCTION region_id(region_key TEXT)
RETURNS INTEGER AS $$
DECLARE
    region INTEGER;
BEGIN
    SELECT id INTO region FROM regions WHERE key = region_key;
    IF region IS NULL THEN
        RAISE EXCEPTION '%', unknown_region_message(region_key) USING ERRCODE = '22023';
    END IF;
    RETURN region;
END;
$$ LANGUAGE plpgsql STABLE PARALLEL SAFE;

-- One call checks every region a filter names: the filter compiler ANDs it in front of the whole filter, where it is
-- a One-Time Filter, so a statement that reads no row is refused too.
CREATE OR REPLACE FUNCTION require_regions(region_keys TEXT[])
RETURNS BOOLEAN AS $$
BEGIN
    PERFORM region_id(region_key) FROM unnest(region_keys) AS region_key;
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
