"""Every advisory-lock first key used in the schema is claimed in the registry comment.

Postgres advisory locks share one (classid, objid) space per database, so two features using the
same first key silently serialize or deadlock with each other (restore recovery once took
(20261020, 62), which is the map rank fence of dataset 62).
"""

import re
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[1] / 'db' / 'schema.sql'


def test_every_advisory_first_key_is_registered():
    text = SCHEMA.read_text()
    registry = set(re.findall(r'^--\s+(\d{8}) = ', text, flags=re.M))
    used = set(re.findall(r'pg_(?:try_)?advisory_(?:xact_)?lock(?:_shared)?\(\s*(\d{8})\s*,', text))
    assert used, 'expected advisory locks in schema.sql'
    assert used <= registry, f'unregistered advisory keys: {sorted(used - registry)}'


def test_backend_advisory_first_keys_are_registered():
    """The backend's own advisory locks (Python) claim registered first keys too."""
    from backend.routers import datasets

    registry = set(re.findall(r'^--\s+(\d{8}) = ', SCHEMA.read_text(), flags=re.M))
    backend = SCHEMA.parents[1]
    used = set()
    for path in backend.rglob('*.py'):
        used |= set(
            re.findall(r'pg_(?:try_)?advisory_(?:xact_)?(?:un)?lock(?:_shared)?\(\s*(\d{8})\s*,', path.read_text()),
        )
    used.add(str(datasets._FACET_SLOT_LOCK))  # the facet slots pass their key as a parameter
    assert used <= registry, f'unregistered advisory keys: {sorted(used - registry)}'


def test_each_advisory_first_key_is_claimed_once():
    # Two branches once both registered 20261065 (map-fit resources and ANN builds, both keyed by
    # hashtext of a name), which would have made a map re-fit and an index build block each other.
    keys = re.findall(r'^--\s+(\d{8}) = ', SCHEMA.read_text(), flags=re.M)
    assert len(keys) == len(set(keys)), sorted(k for k in set(keys) if keys.count(k) > 1)
