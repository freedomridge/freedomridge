#!/usr/bin/env python3
"""
Example: Freedom Ridge SDK - DatasetView Query API

Demonstrates the new query API with three query syntaxes:
1. Direct attribute access: s[(s.field > 5) & (s.other == "value")]
2. SQL-like strings: dataset.query("field > 5 AND other = 'value'")
3. Django-style kwargs: dataset.filter(field__gt=5, other="value")

The queries use the fields scene, timeofday and weather; on a dataset without them they
match nothing. The dataset must already exist (--dataset).
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import connect  # noqa: E402

from examples._shared.importer import existing_dataset, login  # noqa: E402

# Add SDK to path


# Configuration
API_URL = os.getenv("FRIDGE_API_URL", "http://localhost:8000/api")


def main():
    global API_URL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url', default=os.getenv('FRIDGE_API_URL', 'http://localhost:8000/api'))
    parser.add_argument('--dataset', required=True, help='Name of an existing dataset')
    args = parser.parse_args()
    API_URL = args.api_url
    print("=" * 70)
    print("Freedom Ridge SDK - DatasetView Query API Demo")
    print("=" * 70)
    print()

    # Connect to API
    print(f"Connecting to Freedom Ridge API at {API_URL}...")
    connect(API_URL)

    # Login
    from fridge import get_client

    client = get_client()
    try:
        login(client)
        print("✓ Authenticated successfully\n")
    except Exception as e:
        print(f"✗ Login failed: {e}")
        print("  Please ensure the backend is running and credentials are correct\n")
        return

    # Load dataset
    print("Loading dataset...")
    dataset = existing_dataset(client, args.dataset)

    # =========================================================================
    # SYNTAX 1: Direct Attribute Access
    # =========================================================================
    print("\n" + "=" * 70)
    print("SYNTAX 1: Direct Attribute Access")
    print("=" * 70)
    print()

    s = dataset.samples

    # Simple comparison
    print("Example 1.1: s[(s.scene == 'highway')]")
    view = s[(s.scene == "highway")]
    print(f"  → {len(view)} highway samples")

    # AND condition
    print("\nExample 1.2: s[(s.scene == 'highway') & (s.timeofday == 'daytime')]")
    view = s[(s.scene == "highway") & (s.timeofday == "daytime")]
    print(f"  → {len(view)} daytime highway samples")

    # OR condition
    print("\nExample 1.3: s[(s.weather == 'rainy') | (s.weather == 'foggy')]")
    view = s[(s.weather == "rainy") | (s.weather == "foggy")]
    print(f"  → {len(view)} rainy or foggy samples")

    # NOT condition
    print("\nExample 1.4: s[~(s.scene == 'parking lot')]")
    view = s[~(s.scene == "parking lot")]
    print(f"  → {len(view)} samples NOT in parking lots")

    # is_in for multiple values
    print("\nExample 1.5: s[s.scene.is_in(['highway', 'city street', 'residential'])]")
    view = s[s.scene.is_in(["highway", "city street", "residential"])]
    print(f"  → {len(view)} samples from selected scenes")

    # Complex query
    print("\nExample 1.6: Complex: ((highway OR city) & daytime & NOT clear weather)")
    view = s[
        ((s.scene == "highway") | (s.scene == "city street")) & (s.timeofday == "daytime") & ~(s.weather == "clear")
    ]
    print(f"  → {len(view)} matching samples")

    # =========================================================================
    # SYNTAX 2: SQL-like Strings
    # =========================================================================
    print("\n" + "=" * 70)
    print("SYNTAX 2: SQL-like Strings")
    print("=" * 70)
    print()

    # Simple
    print("Example 2.1: dataset.query(\"scene = 'highway'\")")
    view = dataset.query("scene = 'highway'")
    print(f"  → {len(view)} highway samples")

    # AND
    print("\nExample 2.2: dataset.query(\"scene = 'highway' AND timeofday = 'daytime'\")")
    view = dataset.query("scene = 'highway' AND timeofday = 'daytime'")
    print(f"  → {len(view)} daytime highway samples")

    # IN clause
    print("\nExample 2.3: dataset.query(\"scene IN ('highway', 'city street')\")")
    view = dataset.query("scene IN ('highway', 'city street')")
    print(f"  → {len(view)} samples from those scenes")

    # =========================================================================
    # SYNTAX 3: Django-style Keyword Arguments
    # =========================================================================
    print("\n" + "=" * 70)
    print("SYNTAX 3: Django-style Keyword Arguments")
    print("=" * 70)
    print()

    # Simple equality
    print("Example 3.1: dataset.filter(scene='highway')")
    view = dataset.filter(scene="highway")
    print(f"  → {len(view)} highway samples")

    # Multiple conditions (AND)
    print("\nExample 3.2: dataset.filter(scene='highway', timeofday='daytime')")
    view = dataset.filter(scene="highway", timeofday="daytime")
    print(f"  → {len(view)} daytime highway samples")

    # Using operators
    print("\nExample 3.3: dataset.filter(scene__in=['highway', 'city street'])")
    view = dataset.filter(scene__in=["highway", "city street"])
    print(f"  → {len(view)} samples from those scenes")

    # =========================================================================
    # CHAINING OPERATIONS
    # =========================================================================
    print("\n" + "=" * 70)
    print("CHAINING: Combine filter, sort, limit")
    print("=" * 70)
    print()

    # Chain filter with sort and limit
    print("Example: Filter, sort by scene, limit to 10")
    view = dataset.filter(timeofday="daytime").order_by("scene").limit(10)

    print(f"  Found {len(view)} total, showing first 10:")
    for i, sample in enumerate(view):
        scene = sample.metadata.get("scene", "unknown")
        print(f"    {i + 1}. {Path(sample.filepath).name} - {scene}")

    # =========================================================================
    # ITERATION
    # =========================================================================
    print("\n" + "=" * 70)
    print("ITERATION: Loop through results")
    print("=" * 70)
    print()

    view = dataset.filter(scene="highway").limit(5)
    print("First 5 highway samples:")
    for sample in view:
        print(f"  - {sample.filepath}")
        print(f"    Scene: {sample.metadata.get('scene')}")
        print(f"    Weather: {sample.metadata.get('weather')}")

    print("\n" + "=" * 70)
    print("✓ Demo complete!")
    print("=" * 70)


if __name__ == "__main__":
    main()
