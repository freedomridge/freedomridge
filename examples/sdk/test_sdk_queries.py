#!/usr/bin/env python3
"""
Freedom Ridge SDK Implementation Tests

This script tests the Freedom Ridge SDK query functionality by:
1. Running SDK queries
2. Running equivalent raw API calls
3. Comparing results to ensure the SDK produces correct output

Tests cover:
- Metadata field filtering (scene, weather, etc.)
- Tags array filtering
- Boolean operators (AND, OR, NOT)
- Comparison operators (>, >=, <, <=, ==, !=)
- is_in(), contains() methods
- Django-style filtering
- SQL-like query strings
- View chaining (.filter().order_by().limit())
- View methods (.first(), .ids(), .count())

It saves and tags samples (and reverts them), so point it at a prepared dataset you
may edit; there is no default.

Run with:
    FRIDGE_API_URL="http://localhost:8000/api" FRIDGE_TEST_DATASET=my-test-dataset \
        python3 examples/sdk/test_sdk_queries.py
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from examples._shared.importer import existing_dataset, login  # noqa: E402

# Add SDK to path


# Configuration
API_URL = os.getenv("FRIDGE_API_URL", "http://localhost:8000/api")
DATASET_NAME = ""  # set from --dataset / FRIDGE_TEST_DATASET


class TestResult:
    __test__ = False

    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.error: Optional[str] = None
        self.sdk_count: int = 0
        self.api_count: int = 0
        self.details: str = ""

    def __repr__(self):
        status = "✓ PASS" if self.passed else "✗ FAIL"
        return f"{status}: {self.name}"


def raw_api_query(session: requests.Session, dataset_id: int, filters: Dict[str, Any]) -> Dict:
    """Execute a raw API query (equivalent to what SDK generates)."""
    response = session.post(
        f"{API_URL}/datasets/{dataset_id}/samples/query",
        json={"filters": filters, "limit": 1000},
    )
    response.raise_for_status()
    return response.json()


def run_tests():
    print("=" * 70)
    print("Freedom Ridge SDK Implementation Tests")
    print("=" * 70)
    print()

    # Import the Freedom Ridge SDK
    from fridge import connect, get_client

    # Connect and authenticate
    print(f"Connecting to {API_URL}...")
    connect(API_URL)
    client = get_client()

    try:
        login(client)
        print("✓ Authenticated\n")
    except Exception as e:
        print(f"✗ Login failed: {e}")
        return 1

    # Load dataset
    print(f"Loading dataset '{DATASET_NAME}'...")
    try:
        dataset = existing_dataset(client, DATASET_NAME)
        print(f"✓ Dataset loaded (ID: {dataset._id})\n")
    except (Exception, SystemExit) as e:
        print(f"✗ Failed to load dataset: {e}")
        return 1

    # Get session for raw API calls
    session = client.session
    dataset_id = dataset._id
    s = dataset.samples

    results: List[TestResult] = []

    # =========================================================================
    # METADATA FIELD TESTS
    # =========================================================================
    print("Testing metadata field filters...")

    # TEST: Metadata equality
    test = TestResult("Metadata equality: s[s.scene == 'highway']")
    try:
        sdk_view = s[s.scene == "highway"]
        sdk_count = len(sdk_view)

        api_result = raw_api_query(
            session,
            dataset_id,
            {
                "field": "scene",
                "operator": "=",
                "value": "highway",
            },
        )
        api_count = api_result["total"]

        test.sdk_count = sdk_count
        test.api_count = api_count
        test.passed = sdk_count == api_count and sdk_count > 0
        test.details = f"SDK: {sdk_count}, API: {api_count}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: Another metadata field
    test = TestResult("Metadata: s[s.weather == 'clear']")
    try:
        sdk_view = s[s.weather == "clear"]
        sdk_count = len(sdk_view)

        api_result = raw_api_query(
            session,
            dataset_id,
            {
                "field": "weather",
                "operator": "=",
                "value": "clear",
            },
        )
        api_count = api_result["total"]

        test.sdk_count = sdk_count
        test.api_count = api_count
        test.passed = sdk_count == api_count and sdk_count > 0
        test.details = f"SDK: {sdk_count}, API: {api_count}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: Numeric greater than
    test = TestResult("Numeric >: s[s.speed > 20]")
    try:
        sdk_view = s[s.speed > 20]
        sdk_count = len(sdk_view)

        api_result = raw_api_query(
            session,
            dataset_id,
            {
                "field": "speed",
                "operator": ">",
                "value": 20,
            },
        )
        api_count = api_result["total"]

        # Verify actual values are correct
        samples = list(sdk_view.limit(3))
        speeds = [sample.metadata.get("speed") for sample in samples]
        all_above = all(s > 20 for s in speeds if s is not None)

        test.sdk_count = sdk_count
        test.api_count = api_count
        test.passed = sdk_count == api_count and sdk_count > 0 and all_above
        test.details = f"SDK: {sdk_count}, API: {api_count}, samples: {[f'{s:.1f}' for s in speeds]}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: Numeric less than
    test = TestResult("Numeric <: s[s.speed < 5]")
    try:
        sdk_view = s[s.speed < 5]
        sdk_count = len(sdk_view)

        api_result = raw_api_query(
            session,
            dataset_id,
            {
                "field": "speed",
                "operator": "<",
                "value": 5,
            },
        )
        api_count = api_result["total"]

        test.sdk_count = sdk_count
        test.api_count = api_count
        test.passed = sdk_count == api_count and sdk_count > 0
        test.details = f"SDK: {sdk_count}, API: {api_count}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: Metadata is_in (IN operator)
    test = TestResult("Metadata IN: s[s.scene.is_in(['highway', 'city street'])]")
    try:
        sdk_view = s[s.scene.is_in(["highway", "city street"])]
        sdk_count = len(sdk_view)

        api_result = raw_api_query(
            session,
            dataset_id,
            {
                "logic": "OR",
                "filters": [
                    {"field": "scene", "operator": "=", "value": "highway"},
                    {"field": "scene", "operator": "=", "value": "city street"},
                ],
            },
        )
        api_count = api_result["total"]

        test.sdk_count = sdk_count
        test.api_count = api_count
        test.passed = sdk_count == api_count and sdk_count > 0
        test.details = f"SDK: {sdk_count}, API: {api_count}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # =========================================================================
    # TAGS ARRAY TESTS
    # =========================================================================
    print("Testing tags array filters...")

    # TEST: Tags contains
    test = TestResult("Tags contains: s[s.tags.contains('highway')]")
    try:
        sdk_view = s[s.tags.contains("highway")]
        sdk_count = len(sdk_view)

        api_result = raw_api_query(
            session,
            dataset_id,
            {
                "field": "tags",
                "operator": "contains",
                "value": "highway",
            },
        )
        api_count = api_result["total"]

        test.sdk_count = sdk_count
        test.api_count = api_count
        test.passed = sdk_count == api_count and sdk_count > 0
        test.details = f"SDK: {sdk_count}, API: {api_count}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # =========================================================================
    # BOOLEAN OPERATOR TESTS
    # =========================================================================
    print("Testing boolean operators...")

    # TEST: AND with metadata
    test = TestResult("AND: (scene == 'highway') & (weather == 'clear')")
    try:
        sdk_view = s[(s.scene == "highway") & (s.weather == "clear")]
        sdk_count = len(sdk_view)

        api_result = raw_api_query(
            session,
            dataset_id,
            {
                "logic": "AND",
                "filters": [
                    {"field": "scene", "operator": "=", "value": "highway"},
                    {"field": "weather", "operator": "=", "value": "clear"},
                ],
            },
        )
        api_count = api_result["total"]

        test.sdk_count = sdk_count
        test.api_count = api_count
        test.passed = sdk_count == api_count and sdk_count > 0
        test.details = f"SDK: {sdk_count}, API: {api_count}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: OR with metadata
    test = TestResult("OR: (weather == 'rainy') | (weather == 'foggy')")
    try:
        sdk_view = s[(s.weather == "rainy") | (s.weather == "foggy")]
        sdk_count = len(sdk_view)

        api_result = raw_api_query(
            session,
            dataset_id,
            {
                "logic": "OR",
                "filters": [
                    {"field": "weather", "operator": "=", "value": "rainy"},
                    {"field": "weather", "operator": "=", "value": "foggy"},
                ],
            },
        )
        api_count = api_result["total"]

        test.sdk_count = sdk_count
        test.api_count = api_count
        test.passed = sdk_count == api_count
        test.details = f"SDK: {sdk_count}, API: {api_count}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: NOT
    test = TestResult("NOT: ~(s.scene == 'highway')")
    try:
        sdk_view = s[~(s.scene == "highway")]
        sdk_count = len(sdk_view)

        api_result = raw_api_query(
            session,
            dataset_id,
            {
                "logic": "NOT",
                "filters": [
                    {"field": "scene", "operator": "=", "value": "highway"},
                ],
            },
        )
        api_count = api_result["total"]

        test.sdk_count = sdk_count
        test.api_count = api_count
        test.passed = sdk_count == api_count
        test.details = f"SDK: {sdk_count}, API: {api_count}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: Complex nested
    test = TestResult("Complex: ((highway | city) & clear & ~tags.train)")
    try:
        sdk_view = s[
            ((s.scene == "highway") | (s.scene == "city street")) & (s.weather == "clear") & ~(s.tags.contains("train"))
        ]
        sdk_count = len(sdk_view)

        # Build equivalent API query
        api_result = raw_api_query(
            session,
            dataset_id,
            {
                "logic": "AND",
                "filters": [
                    {
                        "logic": "OR",
                        "filters": [
                            {"field": "scene", "operator": "=", "value": "highway"},
                            {"field": "scene", "operator": "=", "value": "city street"},
                        ],
                    },
                    {"field": "weather", "operator": "=", "value": "clear"},
                    {
                        "logic": "NOT",
                        "filters": [
                            {"field": "tags", "operator": "contains", "value": "train"},
                        ],
                    },
                ],
            },
        )
        api_count = api_result["total"]

        test.sdk_count = sdk_count
        test.api_count = api_count
        test.passed = sdk_count == api_count
        test.details = f"SDK: {sdk_count}, API: {api_count}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # =========================================================================
    # QUERY SYNTAX TESTS
    # =========================================================================
    print("Testing query syntaxes...")

    # TEST: SQL-like query
    test = TestResult("SQL: dataset.query(\"scene = 'highway'\")")
    try:
        sdk_view = dataset.query("scene = 'highway'")
        sdk_count = len(sdk_view)

        attr_view = s[s.scene == "highway"]
        attr_count = len(attr_view)

        test.sdk_count = sdk_count
        test.api_count = attr_count
        test.passed = sdk_count == attr_count and sdk_count > 0
        test.details = f"SQL: {sdk_count}, Attribute: {attr_count}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: Django-style
    test = TestResult("Django: dataset.filter(scene='highway')")
    try:
        sdk_view = dataset.filter(scene="highway")
        sdk_count = len(sdk_view)

        attr_view = s[s.scene == "highway"]
        attr_count = len(attr_view)

        test.sdk_count = sdk_count
        test.api_count = attr_count
        test.passed = sdk_count == attr_count and sdk_count > 0
        test.details = f"Django: {sdk_count}, Attribute: {attr_count}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # =========================================================================
    # CHAINING TESTS
    # =========================================================================
    print("Testing view chaining...")

    # TEST: Chaining with limit
    test = TestResult("Chaining: filter(scene='highway').limit(5)")
    try:
        sdk_view = dataset.filter(scene="highway").limit(5)
        sdk_samples = list(sdk_view)

        test.passed = len(sdk_samples) == 5
        test.details = f"Got {len(sdk_samples)} samples"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: Chaining with order_by
    test = TestResult("Chaining: filter().order_by('id').limit(3)")
    try:
        sdk_view = dataset.filter(scene="highway").order_by("id").limit(3)
        sdk_samples = list(sdk_view)

        ids = [sample.id for sample in sdk_samples]
        is_sorted = ids == sorted(ids)

        test.passed = is_sorted and len(sdk_samples) == 3
        test.details = f"IDs: {ids} (sorted: {is_sorted})"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # =========================================================================
    # VIEW METHOD TESTS
    # =========================================================================
    print("Testing view methods...")

    # TEST: view.first()
    test = TestResult("View.first(): returns single sample")
    try:
        sdk_view = dataset.filter(scene="highway")
        first = sdk_view.first()

        test.passed = first is not None and hasattr(first, 'id') and hasattr(first, 'filepath')
        test.details = f"First ID: {first.id}, scene: {first.metadata.get('scene')}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: view.ids()
    test = TestResult("View.ids(): returns list of IDs")
    try:
        sdk_view = dataset.filter(scene="highway").limit(3)
        ids = sdk_view.ids()

        test.passed = isinstance(ids, list) and len(ids) == 3 and all(isinstance(i, int) for i in ids)
        test.details = f"IDs: {ids}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: dataset.all()
    test = TestResult("dataset.all().limit(3)")
    try:
        sdk_view = dataset.all().limit(3)
        sdk_samples = list(sdk_view)

        test.passed = len(sdk_samples) == 3
        test.details = f"Got {len(sdk_samples)} samples"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: Sample object has correct attributes
    test = TestResult("Sample objects have correct attributes")
    try:
        sdk_view = dataset.filter(scene="highway").limit(1)
        samples = list(sdk_view)

        if samples:
            sample = samples[0]
            has_id = hasattr(sample, 'id') and sample.id is not None
            has_filepath = hasattr(sample, 'filepath') and sample.filepath
            has_tags = hasattr(sample, 'tags') and isinstance(sample.tags, list)
            has_metadata = hasattr(sample, 'metadata') and isinstance(sample.metadata, dict)
            has_scene = sample.metadata.get('scene') == 'highway'

            test.passed = all([has_id, has_filepath, has_tags, has_metadata, has_scene])
            test.details = f"ID: {sample.id}, scene: {sample.metadata.get('scene')}, tags: {sample.tags}"
        else:
            test.error = "No samples returned"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # =========================================================================
    # SAMPLE MODIFICATION TESTS
    # =========================================================================
    print("Testing sample modification...")

    # TEST: sample.save()
    test = TestResult("sample.save() persists changes")
    try:
        # Get a sample
        sdk_view = dataset.filter(scene="highway").limit(1)
        sample = sdk_view.first()
        original_tags = sample.tags.copy()

        # Modify and save
        sample.tags.append("_sdk_test_save")
        sample.metadata["_sdk_test_field"] = "test_value"
        sample.save()

        # Verify by re-fetching the same sample through SDK
        # Use tags.contains filter to find our test tag
        verify_view = s[s.tags.contains("_sdk_test_save")]
        verify_count = len(verify_view)
        has_tag = verify_count > 0

        # Verify metadata via re-query
        reload_sample = dataset.filter(tags__contains="_sdk_test_save").first()
        has_field = reload_sample and reload_sample.metadata.get("_sdk_test_field") == "test_value"

        # Clean up
        sample.tags = original_tags
        sample.save()

        test.passed = has_tag and has_field
        test.details = f"Tag saved: {has_tag}, Field saved: {has_field}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: dataset.tag_samples()
    test = TestResult("dataset.tag_samples() adds tags")
    try:
        sdk_view = dataset.filter(scene="highway").limit(2)
        ids = sdk_view.ids()

        result = dataset.tag_samples(ids, ["_sdk_test_bulk"])

        test.passed = result.get("updated_count") == 2
        test.details = f"Updated {result.get('updated_count')} samples"

        # Clean up
        dataset.untag_samples(ids, ["_sdk_test_bulk"])
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: dataset.untag_samples()
    test = TestResult("dataset.untag_samples() removes tags")
    try:
        sdk_view = dataset.filter(scene="highway").limit(2)
        ids = sdk_view.ids()

        # Add then remove
        dataset.tag_samples(ids, ["_sdk_test_untag"])
        result = dataset.untag_samples(ids, ["_sdk_test_untag"])

        test.passed = result.get("updated_count") == 2
        test.details = f"Removed from {result.get('updated_count')} samples"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # =========================================================================
    # AGGREGATION TESTS
    # =========================================================================
    print("Testing aggregations...")

    # TEST: count_values
    test = TestResult("view.count_values('scene')")
    try:
        scene_counts = dataset.all().count_values("scene")

        has_highway = "highway" in scene_counts
        highway_count = scene_counts.get("highway", 0)

        test.passed = has_highway and highway_count > 0
        test.details = f"highway: {highway_count}, total scenes: {len(scene_counts)}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: distinct
    test = TestResult("view.distinct('weather')")
    try:
        weathers = dataset.all().distinct("weather")

        has_clear = "clear" in weathers

        test.passed = has_clear and len(weathers) > 0
        test.details = f"Values: {weathers}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: bounds
    test = TestResult("view.bounds('speed')")
    try:
        min_speed, max_speed = dataset.all().bounds("speed")

        test.passed = min_speed is not None and max_speed is not None
        test.details = f"Range: {min_speed:.1f} to {max_speed:.1f}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: histogram_values
    test = TestResult("view.histogram_values('speed', bins=5)")
    try:
        hist = dataset.all().histogram_values("speed", bins=5)

        test.passed = len(hist) == 5
        test.details = f"Got {len(hist)} buckets"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: Aggregations on filtered view
    test = TestResult("Filtered aggregation: highway weather counts")
    try:
        highway_view = dataset.filter(scene="highway")
        weather_counts = highway_view.count_values("weather")

        test.passed = len(weather_counts) > 0
        test.details = f"Weather types in highway: {list(weather_counts.keys())}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # =========================================================================
    # SIMILARITY SEARCH TESTS
    # =========================================================================
    print("Testing similarity search...")

    # TEST: sort_by_similarity with sample
    test = TestResult("sort_by_similarity(sample, limit=5)")
    try:
        ref_sample = dataset.filter(scene="highway").first()
        similar = dataset.all().sort_by_similarity(ref_sample, limit=5)

        # Should have 5 results, first one should be the reference itself (distance=0)
        has_results = len(similar) == 5
        first_is_ref = similar.first().id == ref_sample.id
        has_distance = similar.first().distance is not None

        test.passed = has_results and first_is_ref and has_distance
        test.details = f"Got {len(similar)} results, first distance: {similar.first().distance:.4f}"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: sort_by_similarity with filtered view
    test = TestResult("Filtered similarity: city_view.sort_by_similarity(highway_sample)")
    try:
        ref_sample = dataset.filter(scene="highway").first()
        city_view = dataset.filter(scene="city street")

        similar_in_city = city_view.sort_by_similarity(ref_sample, limit=5)

        # All results should be city street scenes
        all_city = all(s.metadata.get("scene") == "city street" for s in similar_in_city)

        test.passed = len(similar_in_city) == 5 and all_city
        test.details = f"Got {len(similar_in_city)} city street samples"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # =========================================================================
    # ITER_SAMPLES TESTS
    # =========================================================================
    print("Testing iter_samples and select_fields...")

    # TEST: iter_samples basic
    test = TestResult("iter_samples(progress=False)")
    try:
        count = 0
        for sample in dataset.filter(scene="highway").limit(5).iter_samples(progress=False):
            count += 1

        test.passed = count == 5
        test.details = f"Iterated over {count} samples"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # TEST: select_fields reduces data
    test = TestResult("select_fields(['id', 'filepath'])")
    try:
        view = dataset.filter(scene="highway").select_fields(["id", "filepath"]).limit(3)
        samples = list(view)

        # All samples should have id and filepath
        all_have_id = all(s.id is not None for s in samples)
        all_have_filepath = all(s.filepath for s in samples)

        test.passed = len(samples) == 3 and all_have_id and all_have_filepath
        test.details = f"Got {len(samples)} samples with id and filepath"
    except Exception as e:
        test.error = str(e)
    results.append(test)

    # =========================================================================
    # Print Results
    # =========================================================================
    print("\n" + "=" * 70)
    print("TEST RESULTS")
    print("=" * 70)

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)

    for result in results:
        if result.passed:
            print(f"\n✓ {result.name}")
            print(f"  {result.details}")
        else:
            print(f"\n✗ {result.name}")
            if result.error:
                print(f"  ERROR: {result.error}")
            else:
                print(f"  MISMATCH: {result.details}")

    print("\n" + "=" * 70)
    print(f"SUMMARY: {passed}/{len(results)} tests passed")
    if failed > 0:
        print(f"         {failed} tests failed")
    print("=" * 70)

    # Return exit code
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url', default=os.getenv('FRIDGE_API_URL', 'http://localhost:8000/api'))
    parser.add_argument(
        '--dataset',
        default=os.getenv('FRIDGE_TEST_DATASET'),
        help='Prepared dataset to test against; it is edited (default: $FRIDGE_TEST_DATASET)',
    )
    args = parser.parse_args()
    if not args.dataset:
        parser.error('set FRIDGE_TEST_DATASET or --dataset to a prepared dataset the script may edit')
    API_URL, DATASET_NAME = args.api_url, args.dataset
    sys.exit(run_tests())
