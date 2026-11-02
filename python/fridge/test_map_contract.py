"""Map data-path contract tests — the most-regressed server-side map rules.

Validates docs/MAP_REQUIREMENTS.md against the live map_points / map_extent endpoints
using dense synthetic LOCATION data (no images or browser needed). Locks the rules that
have regressed repeatedly:
  #1 auto-show ALL individual points at zoom >= _MAP_POINT_ZOOM (12)
  #2 per-cell hybrid below that zoom: dense cell -> one bubble, sparse cell -> points
  #3 a cluster bubble ALWAYS represents >= _MAP_CLUSTER_MIN_POINTS (100)
  #6 force-points (de-cluster) returns individual points at any zoom
  #7 map_extent returns the true min/max lon/lat for the filtered set

Usage: FRIDGE_TEST_URL=http://127.0.0.1:8001/api python -m fridge.test_map_contract
"""

import random
import sys
import traceback

from fridge import Dataset, Sample, connect
from fridge._test_safety import require_test_url
from fridge.core.client import get_client

# Mirror of backend constants (docs/MAP_REQUIREMENTS.md tunables).
MAP_POINT_ZOOM = 12
CLUSTER_MIN = 100

PASS = 0
FAIL = 0
ERRORS: list[tuple[str, Exception]] = []


def test(name):
    def decorator(fn):
        def wrapper():
            global PASS, FAIL
            try:
                fn()
                PASS += 1
                print(f"  PASS  {name}")
            except Exception as e:
                FAIL += 1
                ERRORS.append((name, e))
                print(f"  FAIL  {name}: {e}")
                traceback.print_exc()

        return wrapper

    return decorator


def run_tests():
    connect(require_test_url())
    client = get_client()
    client.login('admin', 'admin123')

    for d in client.get_datasets():
        if d['name'] == 'synth-map-test':
            client.delete_dataset(d['id'])

    # Dense cluster (>= the 100 floor) at site A + a sparse group (< floor) at site B,
    # each within a tiny area so they land in one grid cell at low zoom. Location-only —
    # no image files needed (the map only reads lon/lat).
    DENSE, SPARSE = 120, 20
    TOTAL = DENSE + SPARSE
    site_a, site_b = (-100.0, 40.0), (10.0, 10.0)
    rng = random.Random(0)

    def _jit(site):
        return (round(site[0] + rng.uniform(-0.001, 0.001), 6), round(site[1] + rng.uniform(-0.001, 0.001), 6))

    samples = [Sample(filepath=f's3://map/a_{i}.jpg', location=_jit(site_a)) for i in range(DENSE)]
    samples += [Sample(filepath=f's3://map/b_{i}.jpg', location=_jit(site_b)) for i in range(SPARSE)]

    ds = Dataset('synth-map-test')
    ds.add_samples(samples)
    print(f"\nMap contract fixture: {DENSE} dense @ {site_a} + {SPARSE} sparse @ {site_b} (location-only)")
    print("=" * 60)

    def map_points(**body):
        r = client.session.post(f"{client.base_url}/datasets/{ds._id}/map_points", json=body)
        r.raise_for_status()
        return r.json()

    def map_extent():
        r = client.session.post(f"{client.base_url}/datasets/{ds._id}/map_extent", json={})
        r.raise_for_status()
        return r.json()

    @test("#2/#3 below zoom: dense cell -> one >=100 bubble, sparse cell -> individual points")
    def _():
        resp = map_points(zoom=4)
        items = resp['items']
        bubbles = [it for it in items if it.get('id') is None and 'count' in it]
        points = [it for it in items if it.get('id') is not None]
        assert resp['mode'] == 'hybrid', f"mode {resp['mode']}"
        assert len(bubbles) == 1, f"expected 1 bubble, got {len(bubbles)}"
        assert bubbles[0]['count'] == DENSE, f"bubble count {bubbles[0]['count']} != {DENSE}"
        assert all(b['count'] >= CLUSTER_MIN for b in bubbles), "a bubble fell below the 100 floor"
        assert len(points) == SPARSE, f"sparse individual points {len(points)} != {SPARSE}"

    _()

    @test("#1 zoom >= 12: ALL individual points auto-shown, no clusters")
    def _():
        resp = map_points(zoom=MAP_POINT_ZOOM)
        assert resp['mode'] == 'points', f"mode {resp['mode']} (clusters leaked at point zoom)"
        assert all(it.get('id') is not None for it in resp['items']), "a cluster bubble leaked at zoom>=12"
        assert len(resp['items']) == TOTAL, f"{len(resp['items'])} points != {TOTAL}"

    _()

    @test("#6 force-points de-clusters at low zoom")
    def _():
        resp = map_points(zoom=4, points=True)
        assert resp['mode'] == 'points', f"mode {resp['mode']}"
        assert all(it.get('id') is not None for it in resp['items'])
        assert len(resp['items']) == TOTAL, f"{len(resp['items'])} != {TOTAL}"

    _()

    @test("#7 map_extent returns the true min/max lon/lat")
    def _():
        ext = map_extent()
        assert ext['count'] == TOTAL, f"extent count {ext['count']} != {TOTAL}"
        assert abs(ext['w'] - site_a[0]) < 0.1 and abs(ext['e'] - site_b[0]) < 0.1, f"lon extent {ext}"
        assert abs(ext['s'] - site_b[1]) < 0.1 and abs(ext['n'] - site_a[1]) < 0.1, f"lat extent {ext}"

    _()

    print("\n--- Cleanup ---")
    client.delete_dataset(ds._id)
    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed")
    for name, err in ERRORS:
        print(f"  {name}: {err}")
    print("=" * 60)
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if run_tests() else 1)
