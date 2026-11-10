#!/usr/bin/env python3
"""
SDK Unit Tests (No Backend Required)

Tests the SDK query building logic without needing a running backend.
These tests validate that filters are constructed correctly.

Run with: python3 test_sdk_unit.py
"""

import json
import sys
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))


# Add SDK to path


def test_field_expression():
    """Test FieldExpression comparison operators."""
    from fridge.core.view import FieldExpression

    print("Testing FieldExpression...")

    f = FieldExpression("brightness")

    # Test >
    cond = f > 0.5
    expected = {"field": "brightness", "operator": ">", "value": 0.5}
    assert cond.to_dict() == expected, f"Failed: > operator. Got {cond.to_dict()}"
    print("  ✓ > operator")

    # Test >=
    cond = f >= 0.5
    expected = {"field": "brightness", "operator": ">=", "value": 0.5}
    assert cond.to_dict() == expected, f"Failed: >= operator. Got {cond.to_dict()}"
    print("  ✓ >= operator")

    # Test <
    cond = f < 0.5
    expected = {"field": "brightness", "operator": "<", "value": 0.5}
    assert cond.to_dict() == expected, f"Failed: < operator. Got {cond.to_dict()}"
    print("  ✓ < operator")

    # Test <=
    cond = f <= 0.5
    expected = {"field": "brightness", "operator": "<=", "value": 0.5}
    assert cond.to_dict() == expected, f"Failed: <= operator. Got {cond.to_dict()}"
    print("  ✓ <= operator")

    # Test ==
    cond = f == 0.5
    expected = {"field": "brightness", "operator": "=", "value": 0.5}
    assert cond.to_dict() == expected, f"Failed: == operator. Got {cond.to_dict()}"
    print("  ✓ == operator")

    # Test !=
    cond = f != 0.5
    expected = {"field": "brightness", "operator": "!=", "value": 0.5}
    assert cond.to_dict() == expected, f"Failed: != operator. Got {cond.to_dict()}"
    print("  ✓ != operator")

    # Test is_in
    cond = f.is_in([0.3, 0.5, 0.7])
    expected = {"field": "brightness", "operator": "in", "value": [0.3, 0.5, 0.7]}
    assert cond.to_dict() == expected, f"Failed: is_in. Got {cond.to_dict()}"
    print("  ✓ is_in() method")

    # Test contains
    f = FieldExpression("filename")
    cond = f.contains("night")
    expected = {"field": "filename", "operator": "contains", "value": "night"}
    assert cond.to_dict() == expected, f"Failed: contains. Got {cond.to_dict()}"
    print("  ✓ contains() method")

    print("  All FieldExpression tests passed!\n")


def test_condition_and():
    """Test AND combination of conditions."""
    from fridge.core.view import FieldExpression

    print("Testing AND conditions...")

    a = FieldExpression("brightness")
    b = FieldExpression("scene")

    # Simple AND
    cond = (a > 0.5) & (b == "highway")
    result = cond.to_dict()

    assert result["logic"] == "AND", f"Expected AND logic, got {result.get('logic')}"
    assert len(result["filters"]) == 2, f"Expected 2 filters, got {len(result['filters'])}"
    assert result["filters"][0] == {"field": "brightness", "operator": ">", "value": 0.5}
    assert result["filters"][1] == {"field": "scene", "operator": "=", "value": "highway"}
    print("  ✓ Simple AND: (a > 0.5) & (b == 'highway')")

    # Triple AND (should flatten)
    c = FieldExpression("weather")
    cond = (a > 0.5) & (b == "highway") & (c == "clear")
    result = cond.to_dict()

    assert result["logic"] == "AND"
    assert len(result["filters"]) == 3, f"Expected 3 filters (flattened), got {len(result['filters'])}"
    print("  ✓ Triple AND flattens correctly")

    print("  All AND tests passed!\n")


def test_condition_or():
    """Test OR combination of conditions."""
    from fridge.core.view import FieldExpression

    print("Testing OR conditions...")

    a = FieldExpression("scene")

    # Simple OR
    cond = (a == "highway") | (a == "city")
    result = cond.to_dict()

    assert result["logic"] == "OR", f"Expected OR logic, got {result.get('logic')}"
    assert len(result["filters"]) == 2
    print("  ✓ Simple OR: (a == 'highway') | (a == 'city')")

    # Triple OR (should flatten)
    cond = (a == "highway") | (a == "city") | (a == "residential")
    result = cond.to_dict()

    assert result["logic"] == "OR"
    assert len(result["filters"]) == 3, f"Expected 3 filters (flattened), got {len(result['filters'])}"
    print("  ✓ Triple OR flattens correctly")

    print("  All OR tests passed!\n")


def test_condition_not():
    """Test NOT negation of conditions."""
    from fridge.core.view import FieldExpression

    print("Testing NOT conditions...")

    a = FieldExpression("scene")

    cond = ~(a == "parking")
    result = cond.to_dict()

    assert result["logic"] == "NOT", f"Expected NOT logic, got {result.get('logic')}"
    assert len(result["filters"]) == 1
    assert result["filters"][0] == {"field": "scene", "operator": "=", "value": "parking"}
    print("  ✓ NOT: ~(a == 'parking')")

    print("  All NOT tests passed!\n")


def test_a_condition_has_no_truth_value():
    """`and`, `or`, `not` and a chained comparison would each drop a clause without a word, so they raise."""
    from fridge.core.view import FieldExpression

    a = FieldExpression("brightness")
    b = FieldExpression("scene")

    with pytest.raises(TypeError, match="&"):
        (a > 0.5) and (b == "city")
    with pytest.raises(TypeError, match="&"):
        (a > 0.5) or (b == "city")
    with pytest.raises(TypeError, match="&"):
        not (a > 0.5)
    with pytest.raises(TypeError, match="&"):
        0.2 < a < 0.8


def test_complex_nested():
    """Test complex nested conditions."""
    from fridge.core.view import FieldExpression

    print("Testing complex nested conditions...")

    scene = FieldExpression("scene")
    brightness = FieldExpression("brightness")
    weather = FieldExpression("weather")

    # ((highway OR city) AND brightness > 0.5 AND NOT sunny)
    cond = ((scene == "highway") | (scene == "city")) & (brightness > 0.5) & ~(weather == "sunny")
    result = cond.to_dict()

    # Should be AND at top level with 3 children
    assert result["logic"] == "AND", f"Expected AND at top, got {result.get('logic')}"
    assert len(result["filters"]) == 3, f"Expected 3 top-level filters, got {len(result['filters'])}"

    # First child should be OR
    assert result["filters"][0]["logic"] == "OR"
    assert len(result["filters"][0]["filters"]) == 2

    # Second child should be simple comparison
    assert result["filters"][1]["field"] == "brightness"
    assert result["filters"][1]["operator"] == ">"

    # Third child should be NOT
    assert result["filters"][2]["logic"] == "NOT"

    print("  ✓ Complex: ((highway OR city) AND brightness > 0.5 AND NOT sunny)")
    print(f"    Result structure: {json.dumps(result, indent=2)[:200]}...")

    print("  All complex nested tests passed!\n")


def test_sample_query_proxy():
    """Test SampleQuery proxy for attribute access."""
    from fridge.core.view import FieldExpression, SampleQuery

    print("Testing SampleQuery proxy...")

    # Create a mock dataset
    class MockDataset:
        pass

    proxy = SampleQuery(MockDataset())

    # Access attribute should return FieldExpression
    expr = proxy.brightness
    assert isinstance(expr, FieldExpression)
    assert expr.field_name == "brightness"
    print("  ✓ proxy.brightness returns FieldExpression('brightness')")

    expr = proxy.some_field
    assert isinstance(expr, FieldExpression)
    assert expr.field_name == "some_field"
    print("  ✓ proxy.some_field returns FieldExpression('some_field')")

    print("  All SampleQuery tests passed!\n")


def test_label_classes():
    """Test typed label classes serialize to the frontend conventions."""
    print("Testing label classes...")
    from fridge import Detection, Heatmap, Keypoint, Polygon, Polyline

    d = Detection("car", bbox=[0.1, 0.2, 0.3, 0.4], confidence=0.9, occluded=True)
    assert d["bbox"] == [0.1, 0.2, 0.3, 0.4] and d["label"] == "car"
    assert d.confidence == 0.9 and d["attributes"] == {"occluded": True}
    assert abs(d.area - 0.12) < 1e-9
    # dict bbox accepted; bad length rejected
    assert Detection("c", {"x": 0, "y": 0, "width": 1, "height": 1})["bbox"] == [0, 0, 1, 1]
    try:
        Detection("c", [1, 2, 3])
        assert False
    except ValueError:
        pass
    # Polyline open by default; Polygon closed+filled
    assert Polyline("lane", points=[[0, 0], [1, 1]])["closed"] is False
    assert Polygon("road", points=[[0, 0], [1, 0], [1, 1]])["closed"] is True
    # Keypoints carry the _type discriminator (vs polygons)
    assert Keypoint(points=[[0.1, 0.1]], label="pose")["_type"] == "keypoints"
    # Heatmap shape + range validation
    assert Heatmap("p.png", range=(0, 255))["type"] == "heatmap"
    try:
        Heatmap("p.png", range=("a", "b"))
        assert False
    except ValueError:
        pass
    print("  All label-class tests passed!\n")


def test_evaluation_helpers():
    """Test the pure evaluation helpers (IoU, AP)."""
    print("Testing evaluation helpers...")
    from fridge.core.evaluation import _ap_from_scores, _iou_xywh

    assert abs(_iou_xywh([0, 0, 10, 10], [0, 0, 10, 10]) - 1.0) < 1e-9
    assert _iou_xywh([0, 0, 10, 10], [100, 100, 10, 10]) == 0.0
    assert abs(_iou_xywh([0, 0, 10, 10], [5, 0, 10, 10]) - (50 / 150)) < 1e-9
    assert _ap_from_scores([(0.9, True), (0.8, True)], 2) == 1.0  # perfect
    assert _ap_from_scores([(0.9, False), (0.8, False)], 2) == 0.0  # all FP
    assert _ap_from_scores([], 0) == 0.0  # no GT
    print("  All evaluation-helper tests passed!\n")


def main():
    print("=" * 70)
    print("Freedom Ridge SDK Unit Tests (No Backend Required)")
    print("=" * 70)
    print()

    tests = [
        test_field_expression,
        test_condition_and,
        test_condition_or,
        test_condition_not,
        test_complex_nested,
        test_sample_query_proxy,
        test_label_classes,
        test_evaluation_helpers,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  ✗ FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ ERROR: {e}")
            failed += 1

    print("=" * 70)
    print(f"SUMMARY: {passed}/{len(tests)} test groups passed")
    if failed > 0:
        print(f"         {failed} test groups failed")
    print("=" * 70)

    return 0 if failed == 0 else 1


@pytest.mark.parametrize('requested,expected', [('auto', 5), (-1, 8), (100, 8), (6, 6)])
def test_visualization_returns_resolved_fit_size(monkeypatch, requested, expected):
    from fridge.core import dataset as sdk

    dataset = object.__new__(sdk.Dataset)
    dataset._id = 7
    dataset.client = Mock()
    dataset.client.get_map_state.return_value = {}
    dataset.client.hold_map_placement.return_value = {'held_until': None}  # a field without a map yet
    dataset.client.get_vectors.return_value = {
        'total': 8,
        'items': [{'sample_id': i, 'vector': [i, 1]} for i in range(1, 9)],
    }
    monkeypatch.setattr(sdk, '_auto_fit_sample_size', Mock(return_value=5))
    monkeypatch.setattr(sdk, 'fit_reducer', Mock(return_value=(None, np.ones((8, 2)))))
    result = dataset.compute_visualization(field_name='clip', fit_sample_size=requested)
    assert result == {'total_embeddings': 8, 'reduced': 8, 'fit_sample_size': expected, 'ranked': True}
    dataset.client.upload_coordinates.assert_called_once()
    assert len(dataset.client.upload_coordinates.call_args.args[2]) == 8
    dataset.client.reduce_embeddings.assert_not_called()


if __name__ == "__main__":
    sys.exit(main())
