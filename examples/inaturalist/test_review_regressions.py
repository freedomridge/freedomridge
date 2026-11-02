"""Regressions: local fixtures only, including documented CLI contracts."""

import re
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from examples._shared import importer as common  # noqa: E402
from examples._shared import verify as verify_smoke  # noqa: E402
from examples.inaturalist import ingest as inat  # noqa: E402
from examples.osv5m import ingest as osv  # noqa: E402


@pytest.fixture
def source_samples(tmp_path):
    samples = []
    for index in range(2):
        image = dict(
            id=index,
            file_name=f"{index}.jpg",
            latitude=0,
            longitude=0,
            license=0,
            rights_holder=f"Photographer {index}",
        )
        Image.new("RGB", (8, 8), "green").save(tmp_path / image["file_name"])
        samples.append(
            inat.map_image(
                image,
                dict(id=index),
                dict(id=42, name="Lumbricus terrestris"),
                dict(name="Attribution License", url="https://example.invalid/license"),
                tmp_path,
            ),
        )
    return samples


@pytest.fixture
def csv_row():
    return dict(
        id="123456789",
        latitude="18.7",
        longitude="100.7",
        captured_at="1660909818797",
        country="TH",
        region="Nan",
        city="Nan",
        creator_username="Photographer A",
    )


@pytest.fixture
def backend(monkeypatch, tmp_path):
    response = requests.Response()
    response.status_code = 200
    response._content = b"image bytes"
    setattr(response, '_content_consumed', True)
    client = Mock(base_url="http://127.0.0.1:8072/api")
    client.lookup_samples.return_value = {}
    client.session.get.return_value = response
    dataset = Mock(_id=7)
    dataset.compute_visualization.return_value = {'fit_sample_size': 2, 'reduced': 2}
    dataset.add_samples.side_effect = [{"ids": [101]}, {"ids": [102]}]
    dataset.get_schema.return_value = []
    dataset.lookup_samples.side_effect = lambda field, paths: client.lookup_samples(7, field, paths)
    import fridge

    monkeypatch.setattr(fridge, "connect", Mock())
    monkeypatch.setattr(fridge, "get_client", lambda: client)
    monkeypatch.setattr(fridge, "Dataset", lambda *a, **kw: dataset)
    monkeypatch.setenv("FRIDGE_PASSWORD", "fixture-password")
    args = inat.build_parser().parse_args(["--data-root", str(tmp_path), "--batch-size", "1"])
    return SimpleNamespace(
        args=args,
        client=client,
        dataset=dataset,
        response=response,
        encoder=Mock(return_value=lambda images: [[1.0, 2.0] for _ in images]),
    )


def run_import(backend, samples):
    return common.ingest(backend.args, samples, {}, "fixture", encoder_factory=backend.encoder)


def test_species_classification(source_samples):
    for sample in source_samples:
        assert sample.metadata["species"]["label"] == "Lumbricus terrestris"
        assert "ground_truth" not in sample.metadata
    assert inat.FIELD_TYPES["species"] == "object"
    assert "ground_truth" not in inat.FIELD_TYPES


@pytest.mark.parametrize("value", ["Nan", "nan", "NaN", "null", "None", "NA"])
def test_nonempty_text_survives_csv(tmp_path, csv_row, value):
    import csv

    csv_row.update(region=value, city=value)
    with (tmp_path / "test.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_row)
        writer.writeheader()
        writer.writerow(csv_row)
    sample = osv.map_row(next(osv.csv_rows(tmp_path)), tmp_path)
    assert sample.metadata["region"] == sample.metadata["city"] == value
    assert osv.typed_value("region", "") is None
    assert osv.typed_value("dist_sea", "nan") is None
    assert osv.typed_value("dist_sea", "inf") is None


@pytest.mark.parametrize("importer", [inat, osv])
def test_safe_limit(importer, tmp_path):
    parser = importer.build_parser()
    assert parser.parse_args(["--data-root", str(tmp_path)]).limit == 100
    assert parser.parse_args(["--data-root", str(tmp_path), "--limit", "0"]).limit == 0


@pytest.mark.parametrize("creator", ["Photographer A", "Photographer B", ""])
def test_attribution(csv_row, tmp_path, source_samples, creator):
    csv_row["creator_username"] = creator
    attribution = osv.map_row(csv_row, tmp_path).metadata["attribution"]
    assert creator in attribution
    assert "Mapillary" in attribution and "OSV-5M" in attribution
    assert "https://www.mapillary.com/app/?pKey=123456789" in attribution
    for sample in source_samples:
        assert sample.metadata["rights_holder"] in sample.metadata["attribution"]
        assert sample.metadata["license_url"] in sample.metadata["attribution"]


def test_duplicate_only_run_skips_reduce(backend, source_samples, capsys):
    backend.client.lookup_samples.side_effect = lambda _, __, paths: {p: 101 for p in paths}
    counts = run_import(backend, source_samples)
    assert counts == Counter(duplicate_skipped=2)
    backend.encoder.assert_not_called()
    backend.dataset.add_samples.assert_not_called()
    backend.dataset.compute_visualization.assert_not_called()
    assert "Skipping reduction" in capsys.readouterr().out


def test_new_writes_reduce_and_check_media_once(backend, source_samples):
    assert run_import(backend, source_samples)["written"] == 2
    backend.dataset.compute_visualization.assert_called_once_with(field_name="clip")
    backend.client.reduce_embeddings.assert_not_called()
    backend.client.session.get.assert_called_once_with(
        "http://127.0.0.1:8072/api/samples/101/media",
        timeout=30,
        stream=True,
    )


def test_media_403_stops_after_first_batch(backend, source_samples):
    backend.response.status_code = 403
    backend.response._content = b'{"detail":"Local filepath is outside FRIDGE_DATA_ROOT"}'
    backend.args.no_reduce = True
    with pytest.raises(RuntimeError, match="FRIDGE_DATA_ROOT"):
        run_import(backend, source_samples)
    assert backend.dataset.add_samples.call_count == 1
    backend.dataset.refresh_stats.assert_not_called()
    backend.dataset.compute_visualization.assert_not_called()


@pytest.mark.parametrize("status", [404, 500])
def test_media_check_propagates_other_errors(backend, source_samples, status):
    backend.response.status_code = status
    with pytest.raises(requests.HTTPError):
        run_import(backend, source_samples)
    assert backend.dataset.add_samples.call_count == 1


@pytest.mark.parametrize("module", [inat, osv, verify_smoke])
def test_credentials_environment_and_no_default(module, monkeypatch):
    argv = (
        ["--kind", "osv5m", "--dataset-name", "fixture", "--api-url", "http://127.0.0.1:8072/api"]
        if module is verify_smoke
        else ["--data-root", "/data"]
    )
    monkeypatch.setenv("FRIDGE_USERNAME", "fixture-user")
    monkeypatch.setenv("FRIDGE_PASSWORD", "fixture-password")
    args = module.build_parser().parse_args(argv)
    assert (args.username, args.password) == ("fixture-user", "fixture-password")
    args = module.build_parser().parse_args([*argv, "--username", "explicit", "--password", "explicit-secret"])
    assert (args.username, args.password) == ("explicit", "explicit-secret")
    monkeypatch.delenv("FRIDGE_PASSWORD")
    assert module.build_parser().parse_args(argv).password is None


def test_missing_password_fails_before_login(backend, monkeypatch):
    backend.args.password = None
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(ValueError, match="FRIDGE_PASSWORD"):
        run_import(backend, [])
    backend.client.login.assert_not_called()


def test_interactive_password(backend, monkeypatch):
    import getpass

    backend.args.password = None
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompt = Mock(return_value="typed-secret")
    monkeypatch.setattr(getpass, "getpass", prompt)
    run_import(backend, [])
    prompt.assert_called_once()
    backend.client.login.assert_called_once_with(backend.args.username, "typed-secret")


@pytest.fixture
def examples_dir():
    return Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("kind", ["inaturalist", "osv5m"])
def test_documented_full_import_command(examples_dir, kind):
    readme = (examples_dir / kind / "README.md").read_text()
    commands = re.findall(r"```bash\n(.*?)```", readme, re.S)
    assert any("ingest.py" in command and "--limit 0" in command for command in commands)


def test_documented_geolocated_count(examples_dir):
    readme = (examples_dir / "inaturalist" / "README.md").read_text()
    assert "97,992" in readme and "323" in readme and "1,685" in readme


@pytest.mark.parametrize(
    "artifact",
    ["inaturalist/SMOKE.md", "inaturalist/smoke-results.json", "osv5m/smoke-results.json"],
)
def test_no_session_artifacts(examples_dir, artifact):
    assert not (examples_dir / artifact).exists()


@pytest.mark.parametrize("kind", ["inaturalist", "osv5m"])
def test_documented_measured_throughput(examples_dir, kind):
    readme = (examples_dir / kind / "README.md").read_text()
    assert "22–28 images/s" in readme
    assert "6–10" not in readme and "13–21 CPU" not in readme
    if kind == "inaturalist":
        assert "20–25 minutes" in readme
