"""A POST handler named `<a GET handler>_post` is the POST form of that read (a filter too big for a URL), so
a dataset being deleted must still answer it like the GET: it has to be in access._READ_ONLY_DATASET_POSTS,
or require_dataset_access refuses it as a write with a 409."""

from fastapi.routing import APIRoute

from backend import access
from backend.routers import concepts, datasets, embeddings, hashes, quality, versions

# The routers whose routes take require_dataset_access, as test_dataset_deletion walks them.
ROUTERS = (datasets.router, concepts.router, embeddings.router, hashes.router, versions.router, quality.router)


def test_every_post_twin_of_a_read_is_a_read():
    routes = [route for router in ROUTERS for route in router.routes if isinstance(route, APIRoute)]
    reads = {route.endpoint.__name__ for route in routes if 'GET' in (route.methods or set())}
    twins = {
        route.endpoint.__name__
        for route in routes
        if 'POST' in (route.methods or set())
        and route.endpoint.__name__.endswith('_post')
        and route.endpoint.__name__.removesuffix('_post') in reads
        and any(dependency.call is access.require_dataset_access for dependency in route.dependant.dependencies)
    }
    assert {'get_histogram_post', 'get_histogram_preview_post'} <= twins
    assert twins <= access._READ_ONLY_DATASET_POSTS, sorted(twins - access._READ_ONLY_DATASET_POSTS)
