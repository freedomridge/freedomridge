"""CSV parsing and country sampling tests with tiny synthetic local images."""

import csv
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from examples.osv5m import ingest as osv  # noqa: E402


def write_csv(root, rows):
    with (root / "test.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def csv_data(tmp_path):
    rows = []
    for index, country in enumerate(["US"] * 7 + ["FR"] * 3 + ["NA"] * 2):
        rows.append(
            dict(
                id=str(10000000000000000 + index),
                latitude="-16.3",
                longitude="45.6",
                country=country,
                captured_at="1660909818797",
                sequence="seq-a",
                **{"sub-region": "", "unique_sub-region": ""},
                land_cover="5",
                climate="3",
                soil="3",
                road_index="2.167",
                drive_side="0",
                dist_sea="0.295",
                creator_username="photographer",
                creator_id="107837578118568.0",
                cell="(61, 28)",
                thumb_original_url="https://example.invalid/image",
                extra_column="additional attribute",
            ),
        )
        folder = tmp_path / "images" / "test" / ("00" if index % 2 else "")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{rows[-1]['id']}.jpg").touch()
    rows.append({**rows[0], "id": "999", "latitude": "nan"})
    rows.append({**rows[0], "id": "998"})
    write_csv(tmp_path, rows)
    return tmp_path, rows


def test_parser(monkeypatch):
    monkeypatch.delenv("FRIDGE_API_URL", raising=False)
    args = osv.build_parser().parse_args(["--data-root", "/data", "--device", "cpu"])
    assert (args.limit, args.dataset_name, args.device) == (100, "osv5m-test", "cpu")
    assert (args.field_name, args.on_duplicate, args.api_url) == ("clip", "skip", "http://localhost:8000/api")
    assert osv.build_parser().parse_args(["--data-root", "/data", "--limit", "0"]).limit == 0


def test_all_columns_and_types(csv_data):
    root, rows = csv_data
    sample = osv.map_row(rows[0], root)
    assert set(sample.metadata) == {osv.field_name(k) for k in rows[0]} | {"license", "license_url", "attribution"}
    assert sample.metadata["image_id"] == "10000000000000000"
    assert sample.metadata["creator_id"] == "107837578118568.0"
    assert sample.metadata["sub_region"] is None
    assert sample.metadata["captured_at"] == datetime(2022, 8, 19, 11, 50, 18, 797000, tzinfo=timezone.utc)
    assert sample.metadata["road_index"] == 2.167
    assert sample.metadata["drive_side"] == "0"
    assert sample.metadata["land_cover"] == "5"
    assert sample.location == (45.6, -16.3)
    assert "Mapillary" in sample.metadata["attribution"]
    assert "OSV-5M authors" in sample.metadata["attribution"]
    assert osv.map_row(rows[-2], root) is None
    assert osv.typed_value("country", "NA") == "NA"  # Namibia is not a missing-value sentinel.
    assert osv.typed_value("dist_sea", "inf") is None
    assert osv.column_type("captured_at") == "datetime"


def test_country_subset_and_order_independence(csv_data):
    root, rows = csv_data
    counts: Counter[str] = Counter()
    selected = osv.select_ids(root, 6, counts)
    countries = Counter(r["country"] for r in rows if r["id"] in selected)
    assert countries == {"US": 2, "FR": 2, "NA": 2}
    assert counts["missing_coordinates"] == counts["missing_image"] == 1
    assert counts["available_countries"] == 3
    write_csv(root, list(reversed(rows)))
    assert osv.select_ids(root, 6, Counter()) == selected
    assert len(osv.select_ids(root, 0, Counter())) == 12
    assert len(osv.select_ids(root, 100, Counter())) == 12
    available = osv.available_images(root)
    samples = list(osv.iter_samples(root, selected, available))
    assert len(samples) == 6
    assert all(Path(sample.filepath).is_file() for sample in samples)


def test_missing_images_arriving(csv_data):
    root, rows = csv_data
    before = osv.select_ids(root, 0, Counter())
    osv.image_path(root, "998").touch()
    after = osv.select_ids(root, 0, Counter())
    assert after - before == {"998"}


def test_schema_and_path_validation(tmp_path):
    (tmp_path / "test.csv").write_text("id,country\n1,US\n")
    with pytest.raises(ValueError, match="missing required"):
        list(osv.csv_rows(tmp_path))
    (tmp_path / "test.csv").write_text("id,latitude,longitude,country,captured_at,sub-region,sub_region\n")
    with pytest.raises(ValueError, match="collide"):
        list(osv.csv_rows(tmp_path))
    with pytest.raises(ValueError, match="Invalid Mapillary"):
        osv.image_path(tmp_path, "../../outside")
