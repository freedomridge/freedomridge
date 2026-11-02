"""Offline fixtures: pytest -q examples/tusimple/test_tusimple.py."""

import getpass
import io
import json
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from PIL import Image
from requests import Response

from examples._shared import importer as toolkit
from examples.tusimple import ingest_lanes as lanes
from examples.tusimple import ingest_velocity as velocity
from examples.tusimple import mapping as common


def jpeg():
    stream = io.BytesIO()
    Image.new("RGB", (100, 80), "green").save(stream, format="JPEG")
    return stream.getvalue()


def lane_record(clip="one"):
    return {"raw_file": f"clips/{clip}/20.jpg", "lanes": [[10, 20]], "h_samples": [20, 40]}


def vehicle_record():
    return {
        "bbox": {"left": 10, "top": 20, "right": 50, "bottom": 60},
        "velocity": [3, -4],
        "position": [12, -5],
    }


def write_lanes(source, split="train", count=1):
    records = [lane_record(str(i)) for i in range(count)]
    labels = "\n".join(json.dumps(record) for record in records)
    with ZipFile(source / f"{split}_set.zip", "w") as archive:
        for record in records:
            archive.writestr(record["raw_file"], jpeg())
            archive.writestr(record["raw_file"].replace("20.jpg", "19.jpg"), b"unlabelled")
        if split == "train":
            archive.writestr("label_data_fixture.json", labels)
    if split == "test":
        (source / "test_label.json").write_text(labels)


def write_velocity(source, split, clip_ids=(1,)):
    with ZipFile(source / f"benchmark_velocity_{split}.zip", "w") as archive:
        if split == "supp":
            archive.writestr(
                "annotation.json",
                json.dumps(
                    [
                        {"file_name": "supp_img/0001.jpg", "bbox": [vehicle_record()["bbox"]]},
                    ],
                ),
            )
            archive.writestr("supp_img/0001.jpg", jpeg())
            return
        for clip_id in clip_ids:
            record = vehicle_record() if split == "train" else {"bbox": vehicle_record()["bbox"]}
            archive.writestr(f"clips/{clip_id}/annotation.json", json.dumps([record]))
            archive.writestr(f"clips/{clip_id}/imgs/040.jpg", jpeg())
            archive.writestr(f"clips/{clip_id}/imgs/039.jpg", b"unlabelled")
    if split == "test":
        (source / "gt.json").write_text(json.dumps([[vehicle_record()] for _ in clip_ids]))


@pytest.mark.parametrize(
    "module,name,splits",
    [
        (lanes, "tusimple-lanes", ["train", "test"]),
        (velocity, "tusimple-velocity", ["train", "test", "supp"]),
    ],
)
def test_cli_defaults_and_overrides(monkeypatch, tmp_path, module, name, splits):
    monkeypatch.setenv("FRIDGE_API_URL", "http://fixture.invalid/api")
    monkeypatch.delenv("FRIDGE_USERNAME", raising=False)
    monkeypatch.delenv("FRIDGE_PASSWORD", raising=False)
    args = module.parser().parse_args(["--data-root", str(tmp_path)])
    assert (args.dataset_name, args.splits) == (name, splits)
    assert (args.limit, args.batch_size, args.field_name) == (100, 32, "clip")
    assert (args.username, args.password) == ("admin", None)
    assert args.model == "openai/clip-vit-base-patch32"
    assert args.api_url == "http://fixture.invalid/api"
    assert args.on_duplicate == "skip"
    args = module.parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "--limit",
            "0",
            "--device",
            "cpu",
            "--batch-size",
            "2",
            "--field-name",
            "features",
            "--no-reduce",
            "--on-duplicate",
            "update",
            "--splits",
            "train",
        ],
    )
    assert (args.limit, args.batch_size, args.device, args.field_name) == (0, 2, "cpu", "features")
    assert args.no_reduce and args.on_duplicate == "update" and args.splits == ["train"]


@pytest.mark.parametrize("option,value", [("--limit", "-1"), ("--batch-size", "0"), ("--batch-size", "-1")])
def test_cli_rejects_invalid_sizes(option, value):
    with pytest.raises(SystemExit):
        lanes.parser().parse_args(["--data-root", "/tmp/fixture", option, value])


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 65536])
def test_json_array_across_chunk_boundaries(chunk_size):
    expected = [{"text": 'brace } comma , quote " and é'}, [], [{"n": 2}]]
    assert list(common.json_array(io.StringIO(" \n" + json.dumps(expected) + "\n"), chunk_size)) == expected
    assert list(common.json_array(io.StringIO(" [ ] \n"), chunk_size)) == []


@pytest.mark.parametrize("payload", ["", "{}", "[", "[{}", "[{},]", "[{} {}]", "[{}] garbage", "[1]"])
def test_json_array_rejects_malformed_input(payload):
    with pytest.raises(ValueError):
        list(common.json_array(io.StringIO(payload), chunk_size=1))


def test_json_lines_blank_lines_and_error_location():
    assert list(common.json_lines(io.BytesIO(b'\n{"a": 1}\n\n'))) == [{"a": 1}]
    with pytest.raises(ValueError, match="line 2"):
        list(common.json_lines(io.StringIO('{}\ninvalid')))


def test_lane_gaps_normalization_empty_lane_and_stable_ids():
    record = {
        "h_samples": [0, 10, 20, 30, 40, 50, 60],
        "lanes": [
            [-2, 10, 20, -2, 40, 50, -2],
            [-2] * 7,
            [-2, -2, 30, -2, -2, -2, -2],
        ],
    }
    frame = common.Frame(None, "clips/one/20.jpg", "train", "one", record)
    metadata = lanes.map_labels(frame, 100, 100)
    assert metadata["lane_count"] == 3
    first, second, isolated = metadata["lanes"]
    assert first["paths"] == [[[0.1, 0.1], [0.2, 0.2]]]
    assert second["paths"] == [[[0.4, 0.4], [0.5, 0.5]]]
    assert isolated["paths"] == [[[0.3, 0.2]]]
    assert first["label"] == "lane" and not first["closed"] and not first["filled"]
    assert [label["attributes"]["lane_index"] for label in metadata["lanes"]] == [0, 0, 2]
    assert len({label["id"] for label in metadata["lanes"]}) == 3
    assert first["id"] == common.stable_id("lanes", "train", "one", 0)
    assert metadata == lanes.map_labels(frame, 100, 100)
    frame.split = "test"
    assert lanes.map_labels(frame, 100, 100)["lanes"][0]["id"] != first["id"]


def test_single_point_lane_is_retained_and_filterable():
    record = {"h_samples": [10, 20, 30], "lanes": [[-2, 40, -2], [10, 20, 30]]}
    frame = common.Frame(None, "20.jpg", "train", "one", record)
    labels = lanes.map_labels(frame, 100, 100)["lanes"]
    assert [label["num_points"] for label in labels] == [1, 3]
    assert [label["attributes"]["num_points"] for label in labels] == [1, 3]
    assert labels[0]["paths"] == [[[0.4, 0.2]]]


@pytest.mark.parametrize("xs,ys", [([10], [10, 20]), ([-1], [10]), ([100], [10]), ([10], [100])])
def test_invalid_lane_coordinates(xs, ys):
    frame = common.Frame(None, "20.jpg", "train", "one", {"lanes": [xs], "h_samples": ys})
    with pytest.raises(ValueError):
        lanes.map_labels(frame, 100, 100)


def test_velocity_axes_units_and_per_vehicle_attributes():
    first = vehicle_record()
    second = {**first, "velocity": [0, 2], "position": [8, 0]}
    frame = common.Frame(None, "040.jpg", "train", "1", [first, second])
    metadata = velocity.map_labels(frame, 100, 80)
    assert metadata["vehicle_count"] == 2 and metadata["has_motion"]
    a, b = metadata["vehicles"]
    assert a["bbox"] == [0.1, 0.25, 0.4, 0.5]
    assert a["attributes"] == {
        "vehicle_index": 0,
        "velocity_forward": 3.0,
        "velocity_right": -4.0,
        "position_forward": 12.0,
        "position_right": -5.0,
        "speed": 5.0,
        "distance": 13.0,
    }
    assert b["attributes"]["speed"] == 2 and b["attributes"]["distance"] == 8
    assert (a["speed"], a["distance"], b["speed"], b["distance"]) == (5, 13, 2, 8)
    for detection in (a, b):
        assert not {"velocity", "position"} & detection.keys()
        for key, value in detection["attributes"].items():
            if key != "vehicle_index":
                assert detection[key] == value
    assert a["id"] != b["id"]
    assert metadata == velocity.map_labels(frame, 100, 80)


def test_supp_boxes_have_no_fabricated_motion():
    frame = common.Frame(None, "supp_img/0001.jpg", "supp", "0001", [{"bbox": vehicle_record()["bbox"]}])
    metadata = velocity.map_labels(frame, 100, 80)
    assert not metadata["has_motion"] and metadata["vehicle_count"] == 1
    assert metadata["vehicles"][0]["attributes"] == {"vehicle_index": 0}
    frame.annotation = []
    assert velocity.map_labels(frame, 100, 80)["vehicle_count"] == 0


@pytest.mark.parametrize("pair", [[1], [1, 2, 3], [1, float("nan")], [float("inf"), 0]])
def test_invalid_motion(pair):
    with pytest.raises(ValueError, match="two finite"):
        frame = common.Frame(None, "040.jpg", "train", "1", [{**vehicle_record(), "velocity": pair}])
        velocity.map_labels(frame, 100, 80)


@pytest.mark.parametrize(
    "box",
    [
        {"left": 20, "right": 10, "top": 0, "bottom": 10},
        {"left": 0, "right": 10, "top": 20, "bottom": 10},
        {"left": float("nan"), "right": 10, "top": 0, "bottom": 10},
    ],
)
def test_invalid_boxes(box):
    with pytest.raises(ValueError, match="Invalid vehicle box"):
        velocity.map_labels(common.Frame(None, "040.jpg", "train", "1", [{"bbox": box}]), 100, 80)


def test_degenerate_supp_box_preserved_and_flagged():
    box = {"top": 40, "right": 49, "bottom": 40, "left": 48}
    frame = common.Frame(None, "supp_img/0013.jpg", "supp", "0013", [{"bbox": box}])
    metadata = velocity.map_labels(frame, 100, 80)
    assert metadata["vehicle_count"] == 1
    detection = metadata["vehicles"][0]
    assert detection["bbox"] == [0.48, 0.5, 0.01, 0.0]
    assert detection["attributes"]["degenerate_bbox"] is True
    assert "speed" not in detection and "distance" not in detection


@pytest.mark.parametrize(
    "module,split,member",
    [
        (lanes, "train", "clips/0/20.jpg"),
        (lanes, "test", "clips/0/20.jpg"),
        (velocity, "train", "clips/1/imgs/040.jpg"),
        (velocity, "test", "clips/1/imgs/040.jpg"),
        (velocity, "supp", "supp_img/0001.jpg"),
    ],
)
def test_selective_extraction_and_repair(tmp_path, module, split, member):
    (write_lanes if module is lanes else write_velocity)(tmp_path, split)
    root = tmp_path / "output"
    with closing(module.iter_frames(tmp_path, [split])) as frames:
        frame = next(frames)
        assert frame.member == member
        target = frame.destination(root, "lanes" if module is lanes else "velocity")
        common.extract_frame(frame, target)
        assert target.read_bytes() == jpeg()
        target.write_bytes(b"interrupted")
        common.extract_frame(frame, target)
        assert target.read_bytes() == jpeg()
        assert list(root.rglob("*.jpg")) == [target]
    assert frame.archive.fp is None


@pytest.mark.parametrize("failure", ["boxes", "length", "clip_ids"])
def test_velocity_test_ground_truth_alignment(tmp_path, failure):
    write_velocity(tmp_path, "test", clip_ids=(2,) if failure == "clip_ids" else (1,))
    if failure == "boxes":
        (tmp_path / "gt.json").write_text(json.dumps([[{**vehicle_record(), "bbox": {}}]]))
    elif failure == "length":
        (tmp_path / "gt.json").write_text("[]")
    with pytest.raises(ValueError):
        list(velocity.iter_frames(tmp_path, ["test"]))


@pytest.mark.parametrize("member", ["../escape.jpg", "/absolute.jpg", "clips/../../escape", "a\\b", "C:/a", ""])
def test_unsafe_archive_members(member):
    with pytest.raises(ValueError, match="Unsafe archive member"):
        common.safe_member(member)


def test_destination_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "lanes").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes data root"):
        common.Frame(None, "20.jpg", "train", "1", {}).destination(root, "lanes")


@pytest.fixture
def importer(tmp_path, monkeypatch):
    """Real archive/mapper/image handling with an in-memory API and encoder."""
    root = tmp_path / "output"
    monkeypatch.delenv("FRIDGE_DATA_ROOT", raising=False)
    monkeypatch.setenv("FRIDGE_PASSWORD", "fixture-password")
    samples = {}
    encoded = []
    refreshed = []
    reductions = []
    events = []
    media = Response()
    media.status_code = 200
    media._content = jpeg()
    setattr(media, '_content_consumed', True)
    args = lanes.parser().parse_args(["--data-root", str(root), "--limit", "0", "--batch-size", "8"])

    class FakeDataset:
        id = _id = 1

        def lookup_samples(self, field, paths):
            return {p: 1 for p in paths if p in samples}

        def get_schema(self):
            return []

        def add_samples(self, batch, on_duplicate):
            events.append("write")
            assert on_duplicate in ("skip", "update")
            for sample in batch:
                assert sample.embeddings == {"clip": [0.25, 0.75]}
                samples[sample.filepath] = sample
            return {"skipped": 0, "ids": [list(samples).index(s.filepath) + 1 for s in batch]}

        def refresh_stats(self):
            refreshed.append(True)

        def compute_visualization(self, *, field_name):
            assert field_name == 'clip'
            reductions.append(True)
            return {'fit_sample_size': len(samples), 'reduced': len(samples)}

    class FakeEncoder:
        def __init__(self, *_):
            pass

        def __call__(self, images):
            encoded.append(len(images))
            return [[0.25, 0.75]] * len(images)

    def get_media(url, timeout, stream):
        events.append("media")
        assert stream is True
        assert url == "http://fixture.invalid/api/samples/1/media" and timeout > 0
        return media

    client = SimpleNamespace(
        login=lambda *_: None,
        lookup_samples=lambda _, field, paths: {p: 1 for p in paths if p in samples},
        base_url="http://fixture.invalid/api",
        session=SimpleNamespace(get=get_media),
    )
    import fridge

    monkeypatch.setattr(fridge, "connect", lambda _: None)
    monkeypatch.setattr(fridge, "get_client", lambda: client)
    monkeypatch.setattr(fridge, "Dataset", lambda *a, **kw: FakeDataset())
    monkeypatch.setattr(toolkit, "ClipEncoder", FakeEncoder)

    def run(module=lanes, splits=("train", "test")):
        benchmark = "lanes" if module is lanes else "velocity"
        common.ingest(args, benchmark, module.iter_frames(tmp_path, splits), module.map_labels)

    return SimpleNamespace(
        args=args,
        run=run,
        samples=samples,
        encoded=encoded,
        refreshed=refreshed,
        reductions=reductions,
        events=events,
        media=media,
        client=client,
    )


def test_ingest_final_batch_resume_and_update(tmp_path, importer):
    """Exhausting short splits within a batch must not invalidate queued frames."""
    write_lanes(tmp_path, "train", count=2)
    write_lanes(tmp_path, "test", count=1)
    args, run, samples, encoded = importer.args, importer.run, importer.samples, importer.encoded

    run()
    assert len(samples) == 3 and encoded == [3]
    assert [s.metadata["split"] for s in samples.values()] == ["train", "test", "train"]
    lost = Path(next(iter(samples)))
    lost.unlink()
    run()
    assert lost.exists() and len(samples) == 3 and encoded == [3]
    args.on_duplicate = "error"
    with pytest.raises(RuntimeError, match="duplicate filepaths"):
        run()
    args.on_duplicate = "update"
    args.no_reduce = True
    run()
    assert len(samples) == 3 and encoded == [3, 3]
    assert len(importer.refreshed) == 3 and len(importer.reductions) == 1


@pytest.mark.parametrize("module,split", [(lanes, "train"), (velocity, "train"), (velocity, "supp")])
def test_sample_terms_and_attribution(tmp_path, importer, module, split):
    (write_lanes if module is lanes else write_velocity)(tmp_path, split)
    importer.run(module, [split])
    metadata = next(iter(importer.samples.values())).metadata
    assert metadata["attribution"] == "TuSimple benchmark"
    assert "training" in metadata["license"]
    assert "public" in metadata["license"] and "Apache" in metadata["license"]


def test_frame_never_enters_metadata(tmp_path, importer, monkeypatch):
    write_lanes(tmp_path, 'train')
    original = toolkit.ingest

    def inspect(args, samples, *rest, **kwargs):
        def checked():
            for sample in samples:
                json.dumps(sample.to_dict())
                assert not any(key.startswith('_') for key in sample.metadata)
                yield sample

        return original(args, checked(), *rest, **kwargs)

    monkeypatch.setattr(toolkit, 'ingest', inspect)
    importer.run(splits=['train'])


@pytest.mark.parametrize("module", [lanes, velocity])
@pytest.mark.parametrize("forbidden", [False, True])
def test_first_batch_media_check_without_local_env(tmp_path, importer, module, forbidden):
    if module is lanes:
        write_lanes(tmp_path, count=3)
    else:
        write_velocity(tmp_path, "train", clip_ids=(1, 2, 3))
    importer.args.batch_size = 1
    importer.media.status_code = 403 if forbidden else 200
    if forbidden:
        with pytest.raises(RuntimeError, match=r"403.*FRIDGE_DATA_ROOT"):
            importer.run(module, ["train"])
        assert len(importer.samples) == 1 and importer.encoded == [1]
        assert not importer.reductions and not importer.refreshed
        assert importer.events == ["write", "media"]
    else:
        importer.run(module, ["train"])
        assert importer.events == ["write", "media", "write", "write"]


@pytest.mark.parametrize("module", [lanes, velocity])
def test_duplicate_only_rerun_skips_reduce(tmp_path, importer, module, capsys):
    (write_lanes if module is lanes else write_velocity)(tmp_path, "train")
    importer.run(module, ["train"])
    assert importer.reductions == [True]
    importer.reductions.clear()
    importer.events.clear()
    capsys.readouterr()
    importer.run(module, ["train"])
    assert not importer.reductions and importer.encoded == [1]
    assert importer.events == ["media"]
    assert "no samples written" in capsys.readouterr().out


@pytest.mark.parametrize("module", [lanes, velocity])
def test_credentials_from_env_and_explicit_override(tmp_path, monkeypatch, module):
    monkeypatch.setenv("FRIDGE_USERNAME", "fixture-user")
    monkeypatch.setenv("FRIDGE_PASSWORD", "fixture-secret")
    args = module.parser().parse_args(["--data-root", str(tmp_path)])
    assert (args.username, args.password) == ("fixture-user", "fixture-secret")
    args = module.parser().parse_args(
        ["--data-root", str(tmp_path), "--username", "override", "--password", "explicit"],
    )
    assert (args.username, args.password) == ("override", "explicit")


def test_missing_password_noninteractive_fails_before_login(tmp_path, importer, monkeypatch):
    write_lanes(tmp_path)
    importer.args.password = None
    logins = []
    importer.client.login = lambda *args: logins.append(args)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(ValueError, match="FRIDGE_PASSWORD"):
        importer.run(splits=["train"])
    assert not logins and not importer.samples


def test_missing_password_prompts_in_terminal(tmp_path, importer, monkeypatch):
    write_lanes(tmp_path)
    importer.args.password = None
    logins = []
    importer.client.login = lambda *args: logins.append(args)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "prompted-secret")
    importer.run(splits=["train"])
    assert logins == [(importer.args.username, "prompted-secret")]
