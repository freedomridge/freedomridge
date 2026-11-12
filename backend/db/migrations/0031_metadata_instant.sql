-- Metadata datetimes as instants, part one: the rule that turns a datetime field's stored text into the instant
-- it names, as an IMMUTABLE function an index can be built on. Nothing uses it in an index yet; no row changes.
SET LOCAL lock_timeout = '2s';

-- The instant a metadata datetime's text names, or NULL when the text isn't one: the rule in backend/datetimes.py,
-- whose parse_instant is its Python twin. Indexes on datetime fields are built on this function and store its
-- answers, so it must NEVER change in place: a new rule is a new function (metadata_instant_v2) and every index on
-- this one rebuilt. backend/tests/test_datetime_instants.py pins it. IMMUTABLE honestly: it reads no setting
-- (TimeZone, DateStyle, timezone_abbreviations) and no time zone database, it calls only IMMUTABLE built-ins, each
-- named with its schema so that no search_path changes what it calls, and it never raises: every number is checked
-- before make_timestamp sees it. No offset is UTC, a date alone is midnight UTC, and the first six fraction digits
-- count, added as whole microseconds.
CREATE OR REPLACE FUNCTION metadata_instant_v1(value TEXT) RETURNS TIMESTAMPTZ
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
DECLARE
    zone_at INT;                    -- the offset's sign, counted from the 17th character, or 0
    clock pg_catalog.text;          -- hh:mm[:ss[.fraction]], or '' for a date alone
    zone pg_catalog.text := '';     -- the offset without its colon (+hh or +hhmm, or -), or ''
    y INT;
    mo INT;
    d INT;
    last_day INT;
    oh INT;
    om INT;
BEGIN
    -- YYYY-MM-DD, then optionally T or a space, hh:mm, :ss, .fraction, and Z, +hh, +hhmm or +hh:mm (or -), with
    -- month 01-12, day 01-31, hour 00-23, minute and second 00-59 and an offset up to 15:59.
    IF value OPERATOR(pg_catalog.!~) '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])([T ]([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9](\.[0-9]+)?)?(Z|[+-](0[0-9]|1[0-5])(:?[0-5][0-9])?)?)?$' THEN
        RETURN NULL;
    END IF;
    y := pg_catalog.substr(value, 1, 4)::INT;
    mo := pg_catalog.substr(value, 6, 2)::INT;
    d := pg_catalog.substr(value, 9, 2)::INT;
    -- No year 0000, and no day past the end of its month (Gregorian leap years).
    last_day := CASE
        WHEN mo OPERATOR(pg_catalog.=) 2 THEN
            CASE WHEN (y OPERATOR(pg_catalog.%) 4) OPERATOR(pg_catalog.=) 0
                      AND ((y OPERATOR(pg_catalog.%) 100) OPERATOR(pg_catalog.<>) 0
                           OR (y OPERATOR(pg_catalog.%) 400) OPERATOR(pg_catalog.=) 0)
                 THEN 29 ELSE 28 END
        WHEN mo OPERATOR(pg_catalog.=) ANY (ARRAY[4, 6, 9, 11]) THEN 30
        ELSE 31
    END;
    IF y OPERATOR(pg_catalog.=) 0 OR d OPERATOR(pg_catalog.>) last_day THEN
        RETURN NULL;
    END IF;
    -- The grammar allows one sign after the minutes: the offset's.
    zone_at := pg_catalog.strpos(pg_catalog.translate(pg_catalog.substr(value, 17), '-', '+'), '+');
    IF zone_at OPERATOR(pg_catalog.>) 0 THEN
        clock := pg_catalog.substr(value, 12, zone_at OPERATOR(pg_catalog.+) 4);
        zone := pg_catalog.translate(pg_catalog.substr(value, zone_at OPERATOR(pg_catalog.+) 16), ':', '');
    ELSE
        clock := pg_catalog.substr(pg_catalog.rtrim(value, 'Z'), 12);
    END IF;
    -- A part that isn't written is zero.
    oh := pg_catalog.rpad(pg_catalog.substr(zone, 2, 2), 2, '0')::INT;
    om := pg_catalog.rpad(pg_catalog.substr(zone, 4, 2), 2, '0')::INT;
    IF pg_catalog.substr(zone, 1, 1) OPERATOR(pg_catalog.=) '-' THEN
        oh := OPERATOR(pg_catalog.-) oh;
        om := OPERATOR(pg_catalog.-) om;
    END IF;
    -- The wall-clock time, plus the fraction as whole microseconds, read at the offset (ISO sign: +02:00 is east).
    RETURN pg_catalog.timezone(
        pg_catalog.make_interval(hours => oh, mins => om),
        pg_catalog.make_timestamp(
            y,
            mo,
            d,
            pg_catalog.rpad(pg_catalog.substr(clock, 1, 2), 2, '0')::INT,
            pg_catalog.rpad(pg_catalog.substr(clock, 4, 2), 2, '0')::INT,
            pg_catalog.rpad(pg_catalog.substr(clock, 7, 2), 2, '0')::INT
        ) OPERATOR(pg_catalog.+) (
            pg_catalog.rpad(pg_catalog.substr(clock, 10, 6), 6, '0')::INT OPERATOR(pg_catalog.*) INTERVAL '1 microsecond'
        )
    );
END;
$$;
