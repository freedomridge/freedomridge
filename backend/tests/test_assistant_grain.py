"""The working-view tools' image grain is the app's (backend/assistant/grain.py): every case of the fixture the
frontend's own test also reads (frontend/tests/image-grain-cases.test.cjs, running the real withGrain) comes out
exactly as written there."""

import json
import pathlib

import pytest

from backend.assistant.grain import image_grain

CASES = json.loads(
    (pathlib.Path(__file__).resolve().parents[2] / 'frontend/tests/fixtures/image-grain-cases.json').read_text(),
)


def test_the_fixture_covers_each_kind_of_stage():
    operators = {json.dumps(case['stages']) for case in CASES}
    for needed in ('geo_region"', 'geo_region_exclude', 'concept_select', 'concept_exclude', '"in"', '"NOT"'):
        assert any(needed in text for text in operators), needed
    assert any(not case['stages'] for case in CASES) and any(not case['has_objects'] for case in CASES)


@pytest.mark.parametrize('case', CASES, ids=[case['name'] for case in CASES])
def test_a_views_stages_take_the_apps_image_grain(case):
    assert image_grain(case['stages'], case['has_objects']) == case['filter']
