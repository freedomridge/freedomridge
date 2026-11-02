"""The filter sidebar's sections open expanded on a dataset small enough to filter every field at once:
GET /datasets/{id} says so in `sidebar_expand_all`, from the dataset's
rows, objects included (the facet scans read them all), against
_LIGHTNING_FACET_MAX_ROWS.
"""

from unittest.mock import AsyncMock

from backend.routers import datasets
from backend.tests.test_field_registry_postgres import USER, registry_database  # noqa: F401


def images(count: int, start: int = 0) -> list:
    return [{'filepath': f'image-{n}.jpg', 'metadata': {'level': n}} for n in range(start, start + count)]


def objects(count: int) -> list:
    return [{'filepath': f'object-{n}.jpg', 'media_type': 'object', 'metadata': {'level': n}} for n in range(count)]


async def opened(dataset_id: int) -> bool:
    answer = await datasets.get_dataset(dataset_id, request=None)  # type: ignore[arg-type]
    return answer['sidebar_expand_all']


def small_line(monkeypatch) -> None:
    monkeypatch.setattr(datasets, '_LIGHTNING_FACET_MAX_ROWS', 5)
    monkeypatch.setattr(datasets, 'get_optional_user', AsyncMock(return_value=None))
    monkeypatch.setattr(datasets, 'check_dataset_access', AsyncMock())


def test_a_dataset_up_to_the_line_opens_expanded_and_one_past_it_does_not(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        small_line(monkeypatch)
        assert await opened(1) is True  # empty
        await datasets.add_samples(1, {'samples': images(5)}, user=USER)
        assert await opened(1) is True  # at the line
        # Grown past the line by an ingest: the next load opens the large-dataset way.
        await datasets.add_samples(1, {'samples': images(1, start=5)}, user=USER)
        assert await opened(1) is False

    registry_database(scenario)


def test_objects_count_too_few_images_over_many_objects_are_not_small(registry_database, monkeypatch):  # noqa: F811
    """Facet scans read objects too: a small image count must not expand a large mixed dataset."""

    async def scenario(database):
        small_line(monkeypatch)
        # 3 images and 10 objects: the sidebar shows 3 rows, but its scans read 13.
        await datasets.add_samples(1, {'samples': images(3) + objects(10)}, user=USER)
        assert await opened(1) is False
        await datasets.add_samples(2, {'samples': images(2) + objects(3)}, user=USER)
        assert await opened(2) is True

    registry_database(scenario)
