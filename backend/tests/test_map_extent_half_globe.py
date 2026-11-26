"""map_extent's box across the antimeridian (docs/MAP_REQUIREMENTS.md #7): the implementer's own checks of what it
reads. Every box comes from one read: today's smallest-and-largest statement, which also takes the largest longitude
below 0 and the smallest from 0 on, to find whether the samples fit in half the globe across the antimeridian."""

import asyncio
import os

import pytest

from backend.routers import datasets
from backend.tests.test_field_registry_postgres import registry_database

__all__ = ['registry_database']
postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

ONE_READ = (
    'SELECT min(lon) AS w, max(lon) AS e, min(lat) AS s, max(lat) AS n, count(*) AS c, '
    'max(lon) FILTER (WHERE lon < 0) AS gap_w, min(lon) FILTER (WHERE lon >= 0) AS gap_e '
    'FROM samples WHERE dataset_id = $1 AND location IS NOT NULL'
)


class Recorded:
    """The database, answering each read with the next of `rows`."""

    def __init__(self, rows):
        self.rows, self.statements = list(rows), []

    async def fetchrow(self, query, *args):
        self.statements.append(query)
        return self.rows.pop(0)


def _extent(monkeypatch, *rows):
    database = Recorded(rows)
    monkeypatch.setattr(datasets, 'db', database)
    return asyncio.run(datasets.get_dataset_map_extent(1, datasets.MapExtentRequest())), database.statements


def _row(w, e, gap_w=None, gap_e=None, s=-1.0, n=2.0, c=7):
    return {'w': w, 'e': e, 's': s, 'n': n, 'c': c, 'gap_w': gap_w, 'gap_e': gap_e}


def test_one_read_in_every_case(monkeypatch):
    # No wider than 180 degrees: the box as before, whatever lies around 0 (here -90 and 90, 180 apart).
    for w, e, gap_w, gap_e in ((-97.3, -90.2, -90.2, None), (170.0, 179.9, None, 170.0), (-90.0, 90.0, -90.0, 90.0)):
        answer, statements = _extent(monkeypatch, _row(w, e, gap_w, gap_e))
        assert (answer, statements) == ({'w': w, 's': -1.0, 'e': e, 'n': 2.0, 'count': 7}, [ONE_READ])
    # Wider, with the stretch around 0 at least 180 degrees wide: the box across the antimeridian.
    answer, statements = _extent(monkeypatch, _row(-179.0, 179.9, -178.2, 177.4))
    assert (answer, statements) == ({'w': 177.4, 's': -1.0, 'e': -178.2, 'n': 2.0, 'count': 7}, [ONE_READ])
    # Wider, with the stretch around 0 narrower (the United States and China): the box as before.
    answer, statements = _extent(monkeypatch, _row(-122.0, 122.0, -74.0, 100.0))
    assert (answer, statements) == ({'w': -122.0, 's': -1.0, 'e': 122.0, 'n': 2.0, 'count': 7}, [ONE_READ])
    answer, statements = _extent(monkeypatch, {'w': None, 'e': None, 's': None, 'n': None, 'c': 0})
    assert (answer, statements) == ({'count': 0}, [ONE_READ])


# Longitudes, and the box map_extent answers for them.
CASES = [
    ('Fiji', [177.4, 179.0, -179.0, -178.2], (177.4, -178.2)),
    ('Chukotka and Alaska', [175.0, 179.5, -179.5, -150.0], (175.0, -150.0)),
    ('Fiji and Europe', [10.0, 177.4, -178.2], (10.0, -178.2)),
    ('the United States and Australia', [-122.0, -74.0, 115.0, 153.0], (115.0, -74.0)),
    ('the United States and China', [-122.0, -74.0, 100.0, 122.0], (-122.0, 122.0)),
    ('the United States, Europe and Australia', [-122.0, -74.0, -9.0, 30.0, 115.0, 153.0], (-122.0, 153.0)),
    ('round the globe', [-170.0, -90.0, -10.0, 70.0, 150.0], (-170.0, 150.0)),
    ('one side only', [170.0, 179.9], (170.0, 179.9)),
    ('one sample', [-178.2], (-178.2, -178.2)),
]


@postgres
def test_the_box_on_postgres(registry_database):
    async def scenario(database_):
        for name, lons, (w, e) in CASES:
            await database_.execute('DELETE FROM samples WHERE dataset_id = 1')
            await database_.execute(
                "INSERT INTO samples (dataset_id, filepath, media_type, location) "
                "SELECT 1, 'case/' || i, 'image', ST_SetSRID(ST_MakePoint(lon, -17 - i % 3), 4326) "
                "FROM unnest($1::float8[]) WITH ORDINALITY AS u(lon, i)",
                lons,
            )
            answer = await datasets.get_dataset_map_extent(1, datasets.MapExtentRequest())
            lats = [-17.0 - i % 3 for i in range(1, len(lons) + 1)]
            assert answer == {'w': w, 's': min(lats), 'e': e, 'n': max(lats), 'count': len(lons)}, name
        await database_.execute('DELETE FROM samples WHERE dataset_id = 1')
        assert await datasets.get_dataset_map_extent(1, datasets.MapExtentRequest()) == {'count': 0}

    registry_database(scenario)
