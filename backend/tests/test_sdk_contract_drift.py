"""SDK contract drift.

The theme across all of these: the SDK quietly did something OTHER than what the caller asked for.
A typo became an empty result, a field-selection call shipped the whole payload anyway, a delete
didn't delete, an iterator handed back unsaveable samples, a transfer skipped whole fields — each
with no error. So the assertions here are mostly "does it refuse / does it actually do it", not
"does it run".

Stubbed and self-contained (no DB, no TestClient), matching the rest of the suite.
"""

import datetime

import pytest
from fridge.core.dataset import _FILTER_OPS, Dataset
from fridge.core.sample import Sample
from fridge.core.view import DatasetView

# --------------------------------------------------------------------------------------------
# Dataset.filter() must never silently downgrade an unknown operator to equality
# --------------------------------------------------------------------------------------------


def _filter_view(**kwargs):
    ds = Dataset.__new__(Dataset)  # no network: filter() only builds a dict
    return Dataset.filter(ds, **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"brightness__between": [0.3, 0.8]},  # not an operator we implement
        {"scene__startswith": "high"},
        {"count__gtt": 5},  # typo'd gt
        {"x__NIN": [1]},  # case matters
        {"y__": 1},  # empty suffix
    ],
)
def test_unknown_operator_suffix_raises(kwargs):
    """It used to become `field = value` and match 0 rows — a typo returning a plausible answer."""
    with pytest.raises(ValueError) as e:
        _filter_view(**kwargs)
    assert "unknown filter operator" in str(e.value)
    # The error must be actionable: name the supported set.
    assert "gte" in str(e.value) and "not_in" in str(e.value)


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"scene": "highway"}, ("scene", "=", "highway")),
        ({"brightness__gt": 0.5}, ("brightness", ">", 0.5)),
        ({"brightness__gte": 0.5}, ("brightness", ">=", 0.5)),
        ({"brightness__lt": 0.5}, ("brightness", "<", 0.5)),
        ({"brightness__lte": 0.5}, ("brightness", "<=", 0.5)),
        ({"scene__ne": "x"}, ("scene", "!=", "x")),
        ({"scene__in": ["a", "b"]}, ("scene", "in", ["a", "b"])),
        ({"scene__nin": ["a"]}, ("scene", "not in", ["a"])),
        ({"scene__not_in": ["a"]}, ("scene", "not in", ["a"])),
        ({"note__contains": "x"}, ("note", "contains", "x")),
    ],
)
def test_supported_operators_map_to_the_wire_form(kwargs, expected):
    f = _filter_view(**kwargs)._filters
    assert (f["field"], f["operator"], f["value"]) == expected


@pytest.mark.parametrize(
    "kwargs,operator",
    [
        ({"reviewed__exists": True}, "exists"),
        ({"reviewed__exists": False}, "not_exists"),
        ({"reviewed__is_null": True}, "is_null"),
        ({"reviewed__is_null": False}, "is_not_null"),
        ({"reviewed__is_not_null": True}, "is_not_null"),
        ({"reviewed__not_exists": True}, "not_exists"),
    ],
)
def test_unary_operators_use_the_bool_as_polarity_not_as_a_value(kwargs, operator):
    """`reviewed__exists=False` means "key absent", NOT `reviewed == False`."""
    f = _filter_view(**kwargs)._filters
    assert (f["field"], f["operator"], f["value"]) == ("reviewed", operator, None)


def test_unary_operator_rejects_a_non_bool():
    with pytest.raises(ValueError, match="True or False"):
        _filter_view(reviewed__exists="yes")


def test_every_advertised_operator_is_one_the_backend_implements():
    """A DSL operator the server doesn't know would 400 at query time instead of here."""
    from backend.routers.datasets import parse_filter_node

    for suffix, wire in _FILTER_OPS.items():
        args: list = []
        value = (
            ["a"]
            if wire in ("in", "not in")
            else ("x" if wire not in ("exists", "not_exists", "is_null", "is_not_null") else None)
        )
        sql, _ = parse_filter_node({"field": "some_field", "operator": wire, "value": value}, args, 1)
        assert sql and sql != "TRUE", f"{suffix} -> {wire!r} produced no predicate"


# --------------------------------------------------------------------------------------------
# one row->Sample hydration, and it must carry the dataset
# --------------------------------------------------------------------------------------------

ROW = {
    "id": 7,
    "filepath": "s3://x/a.jpg",
    "media_type": "image",
    "tags": ["t"],
    "metadata": {"k": 1},
    "created_at": "2026-01-01T00:00:00",
    "last_modified_at": "2026-01-02T00:00:00",
    "location": {"type": "Point", "coordinates": [1.0, 2.0]},
    "heading": 90.0,
}


def test_from_row_maps_every_wire_field():
    s = Sample.from_row(ROW, dataset="DS")
    assert (s.id, s.filepath, s.media_type, s.tags, s.metadata) == (7, "s3://x/a.jpg", "image", ["t"], {"k": 1})
    assert s.created_at == "2026-01-01T00:00:00" and s.last_modified_at == "2026-01-02T00:00:00"
    assert s.location == ROW["location"] and s.heading == 90.0
    assert s._dataset == "DS"


def test_from_row_accepts_the_versions_row_shape():
    """version_snapshots rows name the column sample_id, not id."""
    assert Sample.from_row({"sample_id": 42, "filepath": "f"}, dataset="DS").id == 42


def test_no_hand_rolled_hydration_remains():
    """Four divergent copies is how two of them lost `_dataset` and one lost `location`."""
    import inspect

    from fridge.core import dataset as dmod
    from fridge.core import view as vmod

    for mod in (dmod, vmod):
        src = inspect.getsource(mod)
        # `Sample(filepath=...)` is legitimate only for constructing a NEW sample (add_sample).
        hydrations = [ln for ln in src.splitlines() if "Sample(filepath=item" in ln or "Sample(filepath=r[" in ln]
        assert not hydrations, f"{mod.__name__} still hydrates by hand: {hydrations}"


# --------------------------------------------------------------------------------------------
# select_fields() must send `slim`, or the endpoint ignores `fields`
# --------------------------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.sent = []

    def post(self, url, json=None, **kw):
        self.sent.append(json)

        class R:
            ok = True
            status_code = 200

            @staticmethod
            def json():
                return {"items": [], "total": 0}

        return R()


class _Ds:
    _id = 1

    def __init__(self, session):
        self.client = type("C", (), {"base_url": "http://t/api", "session": session})()


def test_select_fields_sends_slim_for_native_fields():
    rec = _Recorder()
    view = DatasetView(_Ds(rec))
    view._selected_fields = ["id", "filepath"]
    view._execute_query()
    assert rec.sent[-1]["fields"] == "id,filepath"
    assert rec.sent[-1].get("slim") is True, "without slim the server IGNORES `fields` and ships everything"


@pytest.mark.parametrize("field", ["grp", "brightness", "location", "heading"])
def test_select_fields_falls_back_rather_than_corrupting(field):
    """Correctness beats the optimisation.

    slim projects metadata through `metadata->>'x'` — TEXT — so a number would arrive as a
    string and `sum(view.values('score'))` would raise; `location`/`heading` aren't projectable
    at all and would come back as None. Both must fall back to a full row (what it did before),
    not silently change the data. Caught by the live SDK suite, which CI runs and unit tests don't.
    """
    rec = _Recorder()
    view = DatasetView(_Ds(rec))
    view._selected_fields = [field]
    view._execute_query()
    assert "slim" not in rec.sent[-1], f"{field} must not be requested in slim mode"
    assert rec.sent[-1]["fields"] == field


def test_no_slim_when_no_fields_selected():
    rec = _Recorder()
    DatasetView(_Ds(rec))._execute_query()
    assert "slim" not in rec.sent[-1] and "fields" not in rec.sent[-1]


# --------------------------------------------------------------------------------------------
# Sample.save() must delete removed keys and tolerate datetimes
# --------------------------------------------------------------------------------------------


class _SaveClient:
    def __init__(self):
        self.calls = []

    def update_sample(self, dataset_id, sample_id, **kw):
        self.calls.append(kw)
        return {}


def _saveable(metadata):
    client = _SaveClient()
    ds = type("D", (), {"_id": 1, "client": client})()
    return Sample.from_row({"id": 1, "filepath": "f", "metadata": dict(metadata)}, dataset=ds), client


def test_deleting_a_metadata_key_is_sent_as_a_removal():
    """A merge can only add/overwrite — the delete used to be a silent no-op."""
    s, client = _saveable({"keep": 1, "drop": 2})
    del s.metadata["drop"]
    s.save()
    assert client.calls[-1]["remove_metadata_keys"] == ["drop"]
    assert client.calls[-1]["metadata"] == {"keep": 1}


def test_no_removals_when_nothing_was_deleted():
    s, client = _saveable({"a": 1})
    s.metadata["b"] = 2
    s.save()
    assert client.calls[-1]["remove_metadata_keys"] is None


def test_second_save_does_not_re_remove():
    s, client = _saveable({"a": 1, "b": 2})
    del s.metadata["b"]
    s.save()
    s.save()
    assert client.calls[0]["remove_metadata_keys"] == ["b"]
    assert client.calls[1]["remove_metadata_keys"] is None, "baseline must re-sync after a save"


def test_datetime_metadata_is_serialised_not_crashed():
    s, client = _saveable({})
    s.metadata["captured_at"] = datetime.datetime(2026, 1, 2, 3, 4, 5)
    s.save()
    # With the machine's offset: test_sdk_naive_datetimes.py checks it in a fixed time zone.
    sent = client.calls[-1]["metadata"]["captured_at"]
    assert sent == datetime.datetime(2026, 1, 2, 3, 4, 5).astimezone().isoformat()


def test_assigning_a_whole_metadata_dict_is_a_MERGE_not_a_purge():
    """The critical regression: `s.metadata = {...}` must not delete the keys it omits.

    An importer may get_or_create, then replace metadata from an external source and save.
    Treating the omitted keys as deletions wipes independently computed server-side fields
    nothing upstream produces (scene_category, saturation, uniqueness_*). In-place `del` is the
    case that genuinely means "remove"; wholesale assignment is not.
    """
    s, client = _saveable({"scene_category": "OUTDOOR", "saturation": 0.5, "source_id": "m1"})
    s.metadata = {"source_id": "m1", "new_key": 1}
    s.save()
    assert client.calls[-1]["remove_metadata_keys"] is None, (
        f"wholesale assignment must merge; it deleted {client.calls[-1]['remove_metadata_keys']}"
    )
    assert client.calls[-1]["metadata"] == {"source_id": "m1", "new_key": 1}


def test_in_place_delete_still_removes_after_a_reassignment():
    """Re-baselining on assignment must not disable the delete case it was added for."""
    s, client = _saveable({"a": 1, "b": 2})
    s.metadata = {"a": 1, "b": 2, "c": 3}  # re-baseline
    del s.metadata["b"]  # in-place -> a real removal
    s.save()
    assert client.calls[-1]["remove_metadata_keys"] == ["b"]


def _row_saveable(item):
    client = _SaveClient()
    ds = type("D", (), {"_id": 1, "client": client})()
    return Sample.from_row(item, dataset=ds), client


def test_a_slim_row_never_writes_tags_back():
    """The high-severity regression: slim omits `tags`, and PATCH treats tags as a full REPLACE.

    Making slim-projected samples saveable (they used to raise ValueError) turned a loud
    failure into a silent wipe of the sample's real tags.
    """
    import warnings as W

    s, client = _row_saveable({"id": 1, "filepath": "f"})  # slim: no tags key
    s.tags.append("processed")
    with W.catch_warnings(record=True) as caught:
        W.simplefilter("always")
        s.save()
    assert client.calls[-1]["tags"] is None, "tags must not be written back when they weren't loaded"
    assert any("tags were modified but not saved" in str(c.message) for c in caught), "the caller must be told"


def test_a_full_row_does_write_tags_back():
    s, client = _row_saveable({"id": 1, "filepath": "f", "tags": ["gold"]})
    s.tags.append("processed")
    s.save()
    assert client.calls[-1]["tags"] == ["gold", "processed"]


def test_an_explicit_tag_assignment_is_an_intentional_replace():
    """Assigning tags is unambiguous, so honour it even on a slim row."""
    s, client = _row_saveable({"id": 1, "filepath": "f"})
    s.tags = ["only-this"]
    s.save()
    assert client.calls[-1]["tags"] == ["only-this"]


def test_a_sample_built_by_hand_still_writes_its_tags():
    """Only HYDRATED samples can be missing a field; a constructed one carries what it was given."""
    client = _SaveClient()
    ds = type("D", (), {"_id": 1, "client": client})()
    s = Sample(filepath="f", tags=["a"], _dataset=ds)
    s.id = 1
    s.save()
    assert client.calls[-1]["tags"] == ["a"]


def test_original_key_snapshot_is_not_itself_metadata():
    """Sample.__setattr__ routes unknown attributes INTO metadata — the tracker must be core."""
    s = Sample.from_row({"id": 1, "filepath": "f", "metadata": {"a": 1}}, dataset=None)
    assert "_original_metadata_keys" not in s.metadata
    assert s._original_metadata_keys == {"a"}


# --------------------------------------------------------------------------------------------
# importers batch; the UI URL is derived
# --------------------------------------------------------------------------------------------


def test_importers_batch_instead_of_one_giant_post():
    import inspect

    from fridge.core import io as iomod

    src = inspect.getsource(iomod)
    assert "dataset.add_samples(samples, on_duplicate=on_duplicate)" not in src, (
        "an importer still POSTs the whole import in one request — a real COCO export 413s"
    )
    assert src.count("_add_batched(dataset, samples, on_duplicate)") == 4


def test_add_batched_chunks_and_accumulates():
    from fridge.core.io import _add_batched

    seen = []

    class FakeDataset:
        def add_samples(self, batch, on_duplicate=None):
            seen.append(len(batch))
            return {"ids": list(range(len(batch))), "skipped": 1}

    out = _add_batched(FakeDataset(), [{"filepath": str(i)} for i in range(2500)], "skip", batch_size=1000)
    assert seen == [1000, 1000, 500]
    assert len(out["ids"]) == 2500 and out["skipped"] == 3


def test_view_url_is_derived_from_the_client(capsys):
    ds = Dataset.__new__(Dataset)
    ds._id = 9
    ds.client = type("C", (), {"base_url": "https://freedomridge.example.com/api"})()
    Dataset.view(ds)
    out = capsys.readouterr().out
    assert "https://freedomridge.example.com/datasets/9" in out
    assert "5173" not in out, "the hard-coded Vite port never served this app"
