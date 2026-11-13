"""POST /embeddings/reduce fits UMAP in the API process. Importing umap takes about 5 s the first time in a process
(numba, pynndescent): it ran on the event loop, and the API answered no other request until it finished. The import
now runs in the thread that fits."""

import importlib.abc
import importlib.util
import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from backend.routers import embeddings
from backend.tests import test_field_registry_postgres
from backend.tests.test_layout_coordinates import seed

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN') or not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
    reason='Disposable PostgreSQL required',
)
USER = {'id': 1, 'role': 'admin'}


class FirstImport(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """`import umap` as the first one in a process runs it: the module's code runs, on the importing thread."""

    def __init__(self):
        self.threads = []

    def find_spec(self, name, path, target=None):
        return importlib.util.spec_from_loader(name, self) if name == 'umap' else None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        self.threads.append(threading.get_ident())
        module.UMAP = lambda **kwargs: SimpleNamespace(fit_transform=lambda vectors: np.zeros((len(vectors), 2)))


def test_a_reduce_imports_umap_off_the_event_loop(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())
    first_import = FirstImport()
    monkeypatch.setattr(sys, 'meta_path', [first_import, *sys.meta_path])
    loaded = sys.modules.pop('umap', None)

    async def scenario(db):
        await seed(db)
        await embeddings.reduce_embeddings(1, embeddings.ReduceRequest(), USER)
        assert len(first_import.threads) == 1
        assert first_import.threads[0] != threading.get_ident()  # not the event loop's thread
        assert await db.fetchval('SELECT count(*) FROM embedding_coordinates WHERE dataset_id = 1') == 8

    try:
        registry_database(scenario)
    finally:
        sys.modules.pop('umap', None)
        if loaded is not None:
            sys.modules['umap'] = loaded
