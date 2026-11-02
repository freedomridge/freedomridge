"""Tiny local fixtures only; no model, service, network, or real dataset required."""

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from examples._shared import importer as common  # noqa: E402
from examples.inaturalist import ingest as inat  # noqa: E402


@pytest.fixture
def coco(tmp_path):
    category = dict(
        id=42,
        name="Lumbricus terrestris",
        common_name="Common Earthworm",
        kingdom="Animalia",
        phylum="Annelida",
        **{"class": "Clitellata"},
        order="Haplotaxida",
        family="Lumbricidae",
        genus="Lumbricus",
        specific_epithet="terrestris",
        supercategory="Animalia",
        image_dir_name="00042_worm",
    )
    images = []
    for index in range(5):
        images.append(
            dict(
                id=index,
                width=8,
                height=8,
                file_name=f"val/00042_worm/{index}.jpg",
                license=index % 2,
                rights_holder="A photographer",
                date="2020-06-03 01:02:03+02:00",
                latitude=0,
                longitude=0,
                location_uncertainty=None,
            ),
        )
    images[1]["latitude"] = None
    images[2]["longitude"] = 181
    annotations = [dict(id=100 + i, image_id=i, category_id=42) for i in range(5)]
    data = dict(
        info={"description": "An array-looking string: [ } ], café"},
        images=images,
        categories=[category],
        annotations=list(reversed(annotations)),
        licenses=[
            dict(id=0, name="Attribution License", url="http://creativecommons.org/licenses/by/4.0/"),
            dict(id=1, name="Attribution-NonCommercial License", url="http://creativecommons.org/licenses/by-nc/4.0/"),
        ],
    )
    (tmp_path / "val.json").write_text(json.dumps(data, ensure_ascii=False))
    for image in images[:4]:
        path = tmp_path / image["file_name"]
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), "green").save(path)
    return tmp_path, data


def test_parser_defaults_and_validation(monkeypatch):
    monkeypatch.setenv("FRIDGE_API_URL", "http://127.0.0.1:8072/api")
    monkeypatch.delenv("FRIDGE_USERNAME", raising=False)
    monkeypatch.delenv("FRIDGE_PASSWORD", raising=False)
    parser = inat.build_parser()
    args = parser.parse_args(["--data-root", "/data"])
    assert (args.dataset_name, args.limit, args.field_name, args.licenses) == (
        "inaturalist-2021-val",
        100,
        "clip",
        "no-derivatives",
    )
    assert args.model == "openai/clip-vit-base-patch32"
    assert (args.username, args.password, args.on_duplicate) == ("admin", None, "skip")
    assert args.api_url.endswith(":8072/api")
    for option, value in [("--limit", "-1"), ("--batch-size", "0"), ("--on-duplicate", "replace")]:
        with pytest.raises(SystemExit):
            parser.parse_args(["--data-root", "/data", option, value])


def test_stream_join_mapping_and_counts(coco):
    root, data = coco
    assert list(inat.iter_coco_array(root / "val.json", "categories", chunk_size=1)) == data["categories"]
    counts: Counter[str] = Counter()
    samples = list(inat.iter_samples(root, 0, "all", counts))
    assert len(samples) == 2
    assert counts == {"rows": 5, "missing_coordinates": 2, "missing_image": 1, "selected": 2}
    sample = samples[0]
    assert sample.location == (0, 0)
    assert sample.metadata["annotation_id"] == 100  # Join by ID, not array position.
    assert sample.metadata["category_id"] == 42
    assert sample.metadata["species"]["label"] == "Lumbricus terrestris"
    # R-1: the plain name stays filterable alongside the overlay classification
    assert sample.metadata["scientific_name"] == "Lumbricus terrestris"
    assert all(sample.metadata[key] for key in (*inat.TAXONOMY, "species", "common_name"))
    assert sample.metadata["date"] == datetime(2020, 6, 2, 23, 2, 3, tzinfo=timezone.utc)
    assert sample.to_dict()["metadata"]["date"] == "2020-06-02T23:02:03+00:00"
    assert sample.metadata["license_id"] == 0
    assert sample.metadata["rights_holder"] in sample.metadata["attribution"]
    assert sample.metadata["location_uncertainty"] is None


def test_license_selection_and_limit(coco):
    root, _ = coco
    counts: Counter[str] = Counter()
    assert len(list(inat.iter_samples(root, 0, "0", counts))) == 1
    assert counts["license_excluded"] == 1
    assert len(list(inat.iter_samples(root, 1, "all", Counter()))) == 1
    for value in ["999", "by-nc", ""]:
        with pytest.raises(ValueError):
            list(inat.iter_samples(root, 0, value, Counter()))


def test_bad_json_and_path(coco):
    root, data = coco
    with pytest.raises(ValueError, match="Missing top-level"):
        list(inat.iter_coco_array(root / "val.json", "absent"))
    (root / "bad.json").write_text('{"images":[{"id":')
    with pytest.raises(ValueError, match="Unexpected end"):
        list(inat.iter_coco_array(root / "bad.json", "images", chunk_size=2))
    data["images"][0]["file_name"] = "../outside.jpg"
    with pytest.raises(ValueError, match="escapes"):
        inat.map_image(data["images"][0], data["annotations"][-1], data["categories"][0], data["licenses"][0], root)


@pytest.mark.parametrize("lat,lon", [(None, 2), ("NaN", 2), (0, "inf"), (91, 0), (0, -181)])
def test_invalid_coordinates(lat, lon):
    assert common.coordinates(lat, lon) is None


def test_inline_batches_resume_and_duplicate_modes(coco, monkeypatch):
    import fridge

    monkeypatch.setenv("FRIDGE_PASSWORD", "fixture-password")
    monkeypatch.setattr(common, "check_media", lambda client, sample_id, media=None: None)
    root, _ = coco
    samples = list(inat.iter_samples(root, 0, "all", Counter()))
    stored, calls = {}, []

    class Client:
        def login(self, *args):
            pass

        def lookup_samples(self, dataset_id, field, paths):
            return {p: 1 for p in paths if p in stored}

    class Dataset:
        def lookup_samples(self, field, paths):
            return Client().lookup_samples(self._id, field, paths)

        _id = 1

        def __init__(self, *args, **kwargs):
            pass

        def add_samples(self, batch, on_duplicate):
            calls.append(len(batch))
            for sample in batch:
                assert sample.location is not None
                assert sample.embeddings == {"clip": [1.0, 2.0]}
                assert sample.metadata["species"]["label"]
                stored[sample.filepath] = sample.to_dict()
            return {"ids": list(range(len(batch)))}

        def get_schema(self):
            return []

        def refresh_stats(self):
            pass

    monkeypatch.setattr(fridge, "connect", lambda url: None)
    monkeypatch.setattr(fridge, "get_client", Client)
    monkeypatch.setattr(fridge, "Dataset", Dataset)
    args = inat.build_parser().parse_args(["--data-root", str(root), "--no-reduce", "--batch-size", "1"])

    def encoder_factory(args):
        return lambda images: [[1.0, 2.0] for _ in images]

    result = common.ingest(args, samples, {}, "test", encoder_factory=encoder_factory)
    assert result["written"] == 2 and calls == [1, 1]

    def no_encoder(args):
        pytest.fail("A completed rerun must not load CLIP")

    result = common.ingest(args, samples, {}, "test", encoder_factory=no_encoder)
    assert result["duplicate_skipped"] == 2 and len(stored) == 2
    args.on_duplicate = "error"
    with pytest.raises(RuntimeError, match="duplicate filepaths"):
        common.ingest(args, samples, {}, "test", encoder_factory=no_encoder)
    args.on_duplicate = "update"
    assert common.ingest(args, samples, {}, "test", encoder_factory=encoder_factory)["written"] == 2
    # Partial/corrupt images are counted and can be retried after the writer finishes.
    stored.clear()
    path = Path(samples[0].filepath)
    original = path.read_bytes()
    path.write_bytes(b"partial download")
    args.on_duplicate = "skip"
    result = common.ingest(args, samples, {}, "test", encoder_factory=encoder_factory)
    assert result["unreadable_image"] == result["written"] == 1
    path.write_bytes(original)
    result = common.ingest(args, samples, {}, "test", encoder_factory=encoder_factory)
    assert result["written"] == result["duplicate_skipped"] == 1


def test_default_license_preset_excludes_no_derivatives():
    licenses = {
        0: dict(id=0, name="Attribution License"),
        1: dict(id=1, name="Attribution-NonCommercial License"),
        2: dict(id=2, name="Attribution-NonCommercial-NoDerivatives License"),
        5: dict(id=5, name="Attribution-NoDerivatives License"),
    }
    assert inat.select_licenses("no-derivatives", licenses) == {0, 1}
    assert inat.select_licenses("all", licenses) == {0, 1, 2, 5}
