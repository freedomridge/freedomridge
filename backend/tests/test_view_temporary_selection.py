"""A saved view can't hold a temporary selection.

A Select All over 50,000 samples (and a version comparison's "View samples") is a selection token
that region_selections prunes after 2 hours, so a view saved with one reopened as 0 samples with no
explanation. Saving one is refused; the UI saves the filters instead.
"""

import asyncio

import pytest
from fastapi import HTTPException

from backend.models import ViewCreate
from backend.routers import views

TOKEN_STAGE = {
    "type": "selection",
    "description": "60000 Selected",
    "filter": {"logic": "AND", "filters": [{"field": "id", "operator": "in_selection", "value": "tok"}]},
}
IDX_STAGE = {"type": "filter", "description": "idx ≥ 9", "filter": {"field": "idx", "operator": ">=", "value": 9}}


def test_a_view_with_a_selection_token_is_refused_before_touching_the_db():
    content = {
        "filters": {"logic": "AND", "filters": [IDX_STAGE["filter"], TOKEN_STAGE["filter"]]},
        "viewStages": [IDX_STAGE, TOKEN_STAGE],
    }
    with pytest.raises(HTTPException) as exc:  # no db monkeypatch: refused before any query
        asyncio.run(views.create_view(1, ViewCreate(name="v", content=content), {"id": 1, "role": "admin"}))
    assert exc.value.status_code == 422
    assert "filter" in exc.value.detail and "2 hours" in exc.value.detail


@pytest.mark.parametrize(
    "content, expected",
    [
        ({"viewStages": [IDX_STAGE]}, False),
        ({"viewStages": [IDX_STAGE, TOKEN_STAGE]}, True),
        ({"filters": {"logic": "NOT", "filters": [{"logic": "AND", "filters": [TOKEN_STAGE["filter"]]}]}}, True),
        ({"viewStages": [{"filter": {"field": "id", "operator": "in", "value": [1, 2, 3]}}]}, False),
    ],
)
def test_token_detection_walks_the_whole_tree(content, expected):
    assert views._has_temporary_selection(content) is expected
