"""Storage configs limited to chosen datasets, and config changes reaching every worker at once."""

import os

import asyncpg
import pytest
from fastapi import HTTPException

from backend import database, storage
from backend.routers import datasets
from backend.routers import storage as storage_router
from backend.tests.test_field_registry_postgres import USER, registry_database

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

TRAINING, OTHER = 1, 2  # the datasets registry_database seeds


@pytest.fixture
def scope_database(registry_database, monkeypatch):
    """registry_database with the storage modules pointed at it and no env-var bucket."""

    def run(scenario):
        async def wrapped(database_):
            monkeypatch.setattr(database, 'db', database_)
            monkeypatch.setattr(storage_router, 'db', database_)
            monkeypatch.setattr(storage.storage.config, 's3_bucket', None)
            monkeypatch.setattr(storage, '_backend_cache', {})
            await scenario(database_)

        registry_database(wrapped)

    return run


async def _config(database_, name, bucket, *, patterns=(), restricted=False, default=False, datasets_=()):
    """Insert a config straight into the table, as another worker (or a direct DB edit) would."""
    config_id = await database_.fetchval(
        """
        INSERT INTO storage_configs (name, bucket_name, bucket_patterns, restricted, is_default)
        VALUES ($1, $2, $3, $4, $5) RETURNING id
        """,
        name,
        bucket,
        list(patterns),
        restricted,
        default,
    )
    for dataset_id in datasets_:
        await database_.execute('INSERT INTO storage_config_datasets VALUES ($1, $2)', config_id, dataset_id)
    return config_id


async def _routed(dataset_id, bucket):
    route = await storage.route_s3_bucket(dataset_id, bucket)
    return None if route is None else route.config_id


def test_an_unrestricted_config_serves_every_dataset(scope_database):
    async def scenario(database_):
        shared = await _config(database_, 'shared', 'images', patterns=['masks-*'])
        for dataset_id in (TRAINING, OTHER):
            assert await _routed(dataset_id, 'images') == shared
            assert await _routed(dataset_id, 'masks-2026') == shared
            assert await _routed(dataset_id, 'unconfigured') is None

    scope_database(scenario)


def test_a_restricted_config_serves_only_the_datasets_it_lists(scope_database):
    async def scenario(database_):
        private = await _config(database_, 'private', 'secret', patterns=['secret-*'], restricted=True, datasets_=[1])
        assert await _routed(TRAINING, 'secret') == private
        assert await _routed(TRAINING, 'secret-masks') == private
        # Neither the exact bucket name nor the pattern lets an unlisted dataset through.
        assert await _routed(OTHER, 'secret') is None
        assert await _routed(OTHER, 'secret-masks') is None

        # An empty list serves nothing; it never falls back to "every dataset".
        await database_.execute('DELETE FROM storage_config_datasets')
        assert await _routed(TRAINING, 'secret') is None

    scope_database(scenario)


def test_the_most_specific_usable_config_wins(scope_database):
    async def scenario(database_):
        pattern = await _config(database_, 'pattern', 'unrelated', patterns=['shar*'])  # the oldest
        everyone = await _config(database_, 'everyone', 'shared')
        listed = await _config(database_, 'listed', 'shared', restricted=True, datasets_=[TRAINING])
        # An exact bucket name beats an older pattern match.
        assert await _routed(OTHER, 'shared') == everyone
        # A config restricted to the dataset beats one serving every dataset.
        assert await _routed(TRAINING, 'shared') == listed
        # The dataset's own config beats both, even when it only matches through a pattern.
        await database_.execute('UPDATE datasets SET storage_config_id = $1 WHERE id = $2', pattern, TRAINING)
        assert await _routed(TRAINING, 'shared') == pattern

    scope_database(scenario)


def test_the_env_var_backend_serves_only_datasets_with_no_config_to_fall_back_to(scope_database, monkeypatch):
    async def scenario(database_):
        monkeypatch.setattr(storage.storage.config, 's3_bucket', 'env-bucket')
        assert await storage.route_s3_bucket(TRAINING, 'env-bucket') == storage.ENV_ROUTE
        assert await storage.route_s3_bucket(TRAINING, 'not-env') is None

        own = await _config(database_, 'own', 'elsewhere')
        await database_.execute('UPDATE datasets SET storage_config_id = $1 WHERE id = $2', own, TRAINING)
        assert await storage.route_s3_bucket(TRAINING, 'env-bucket') is None  # its own config serves it
        assert await storage.route_s3_bucket(OTHER, 'env-bucket') == storage.ENV_ROUTE

        await _config(database_, 'default', 'default-bucket', default=True)
        assert await storage.route_s3_bucket(OTHER, 'env-bucket') is None  # the default serves it

    scope_database(scenario)


def test_config_changes_apply_without_any_cache_flush(scope_database):
    """A change made through one backend worker applies on every other worker's next request: here the
    rows change underneath the module (no router call, so nothing could flush a cache)."""

    async def scenario(database_):
        # A "no config" answer is not remembered: the config added next is used at once.
        assert await _routed(TRAINING, 'images') is None
        config_id = await _config(database_, 'images', 'images')
        route = await storage.route_s3_bucket(TRAINING, 'images')
        assert route is not None and route.config_id == config_id

        backend = await storage.backend_for_route(route)
        assert backend is not None
        assert await storage.backend_for_route(route) is backend  # the S3 client is cached
        await database_.execute("UPDATE storage_configs SET access_key_id = 'rotated' WHERE id = $1", config_id)
        rotated = await storage.route_s3_bucket(TRAINING, 'images')
        assert rotated is not None and rotated.updated_at != route.updated_at  # the trigger bumped it
        rebuilt = await storage.backend_for_route(rotated)
        assert rebuilt is not None and rebuilt is not backend
        assert rebuilt.config.s3_access_key == 'rotated'

        # Restricting it, or deleting it, takes effect at once too.
        await database_.execute('UPDATE storage_configs SET restricted = TRUE WHERE id = $1', config_id)
        assert await _routed(TRAINING, 'images') is None
        await database_.execute('UPDATE storage_configs SET restricted = FALSE WHERE id = $1', config_id)
        assert await _routed(TRAINING, 'images') == config_id
        await database_.execute('DELETE FROM storage_configs WHERE id = $1', config_id)
        assert await _routed(TRAINING, 'images') is None

    scope_database(scenario)


def test_the_api_refuses_scopes_it_could_not_honour(scope_database):
    create = storage_router.create_storage_config
    update = storage_router.update_storage_config
    Create, Update = storage_router.StorageConfigCreate, storage_router.StorageConfigUpdate

    async def refused(call, status_code):
        with pytest.raises(HTTPException) as caught:
            await call
        assert caught.value.status_code == status_code
        return caught.value.detail

    async def scenario(database_):
        assert 'default' in await refused(
            create(Create(name='a', bucket_name='b', is_default=True, restricted=True), USER),
            400,
        )
        assert '[99]' in await refused(
            create(Create(name='a', bucket_name='b', restricted=True, dataset_ids=[1, 99]), USER),
            400,
        )
        assert 'restricted' in await refused(create(Create(name='a', bucket_name='b', dataset_ids=[1]), USER), 400)

        made = await create(Create(name='private', bucket_name='b', restricted=True, dataset_ids=[TRAINING]), USER)
        assert (made.restricted, made.dataset_ids) == (True, [TRAINING])
        listed = {config.id: config for config in await storage_router.list_storage_configs(USER)}
        assert listed[made.id].dataset_ids == [TRAINING]
        assert (await storage_router.get_storage_config(made.id, USER)).dataset_ids == [TRAINING]

        # A dataset can only be given a restricted config that lists it.
        assigned = await datasets.update_dataset(TRAINING, datasets.DatasetUpdate(storage_config_id=made.id), USER)
        assert assigned['storage_config_id'] == made.id
        assert 'Settings' in await refused(
            datasets.update_dataset(OTHER, datasets.DatasetUpdate(storage_config_id=made.id), USER),
            400,
        )
        await refused(datasets.create_dataset(datasets.DatasetCreate(name='new', storage_config_id=made.id), USER), 400)

        # ... and its list can't drop a dataset that uses it as its own config.
        assert 'training' in await refused(update(made.id, Update(dataset_ids=[OTHER]), USER), 409)
        assert 'default' in await refused(update(made.id, Update(is_default=True), USER), 400)
        widened = await update(made.id, Update(dataset_ids=[TRAINING, OTHER]), USER)
        assert widened.dataset_ids == [TRAINING, OTHER]
        renamed = await update(made.id, Update(name='renamed'), USER)  # the list is kept when omitted
        assert renamed.dataset_ids == [TRAINING, OTHER]

        # Serving every dataset again clears the list, so it can't silently come back later.
        opened = await update(made.id, Update(restricted=False), USER)
        assert (opened.restricted, opened.dataset_ids) == (False, [])
        assert await database_.fetchval('SELECT count(*) FROM storage_config_datasets') == 0

        # Deleting a config deletes its list with it.
        closed = await update(made.id, Update(restricted=True, dataset_ids=[TRAINING]), USER)
        assert closed.dataset_ids == [TRAINING]
        await datasets.update_dataset(TRAINING, datasets.DatasetUpdate(storage_config_id=None), USER)
        await storage_router.delete_storage_config(made.id, USER)
        assert await database_.fetchval('SELECT count(*) FROM storage_config_datasets') == 0

        # The database refuses a restricted default outright, whatever writes it.
        with pytest.raises(asyncpg.CheckViolationError):
            await _config(database_, 'bad', 'b', restricted=True, default=True)

    scope_database(scenario)
