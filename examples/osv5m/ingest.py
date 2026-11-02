#!/usr/bin/env python3
"""Import a deterministic country-spread subset of OSV-5M test with GPS and offline CLIP."""

import csv
import hashlib
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import Sample  # noqa: E402

from examples._shared.credits import DATASET_CREDITS, OSV5M_ATTRIBUTION  # noqa: E402
from examples._shared.importer import coordinates, ingest  # noqa: E402
from examples._shared.importer import parser as common_parser  # noqa: E402

ATTRIBUTION = OSV5M_ATTRIBUTION
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
NUMBER_COLUMNS = {"latitude", "longitude", "road_index", "dist_sea"}
# Numeric codes identify categories, not continuous measurements. Preserve them as strings.
CATEGORY_COLUMNS = {
    "id",
    "country",
    "sequence",
    "lon_bin",
    "lat_bin",
    "cell",
    "land_cover",
    "drive_side",
    "climate",
    "soil",
    "region",
    "sub-region",
    "city",
    "unique_city",
    "unique_sub-region",
    "unique_region",
    "unique_country",
    "quadtree_10_1000",
    "creator_username",
    "creator_id",
    "thumb_original_url",
}
NULLS = {"", "nan", "null", "none"}


def build_parser():
    result = common_parser(
        __doc__,
        'osv5m-test',
        batch_size=32,
        limit_help='N images in country round-robin order; 0 = all',
    )
    result.set_defaults(reduce_errors='warn')
    return result


def field_name(column):
    # Native sample 'id' is reserved; hyphens cannot be used by the field/filter API.
    return "image_id" if column == "id" else column.replace("-", "_")


def column_type(column):
    if column == "captured_at":
        return "datetime"
    return "number" if column in NUMBER_COLUMNS else "string"


def typed_value(column, value):
    if value is None or not value.strip():
        return None
    value = value.strip()
    if (column == "captured_at" or column in NUMBER_COLUMNS) and value.lower() in NULLS:
        return None
    if column == "captured_at":
        # Mapillary CSV timestamps are milliseconds since the Unix epoch, not seconds.
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    if column in NUMBER_COLUMNS:
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def csv_rows(root):
    with (root / "test.csv").open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        if not {"id", "latitude", "longitude", "country", "captured_at"} <= set(columns):
            raise ValueError("test.csv is missing required OSV-5M columns")
        if len({field_name(k) for k in columns}) != len(columns):
            raise ValueError("CSV columns collide after field-name normalization")
        yield from reader


def image_path(root, image_id):
    if not image_id or not image_id.isdecimal():
        raise ValueError(f"Invalid Mapillary image ID: {image_id!r}")
    return root / "images" / "test" / f"{image_id}.jpg"


def stable_key(value):
    return hashlib.sha256(value.encode()).digest(), value


def available_images(root):
    """Archives extract into 00/, 01/, etc.; also accept a flattened images/test/ layout."""
    paths = {}
    for path in sorted((root / "images" / "test").rglob("*.jpg")):
        if path.stem in paths:
            raise ValueError(f"Duplicate extracted image ID: {path.stem}")
        paths[path.stem] = path
    return paths


def select_ids(root, limit, counts, available=None):
    """One small ID index, no retained metadata/images; round-robin hashed countries and IDs."""
    countries = defaultdict(list)
    available = available_images(root) if available is None else available
    for row in csv_rows(root):
        counts["rows"] += 1
        if coordinates(row["latitude"], row["longitude"]) is None:
            counts["missing_coordinates"] += 1
            continue
        if row["id"] not in available:
            counts["missing_image"] += 1
            continue
        countries[row["country"] or "(unknown)"].append(row["id"])
    groups = [iter(sorted(countries[country], key=stable_key)) for country in sorted(countries, key=stable_key)]
    selected: set[str] = set()
    while groups and (not limit or len(selected) < limit):
        remaining = []
        for group in groups:
            image_id = next(group, None)
            if image_id is not None:
                selected.add(image_id)
                remaining.append(group)
            if limit and len(selected) >= limit:
                break
        groups = remaining
    counts["available_countries"] = len(countries)
    counts["selected"] = len(selected)
    return selected


def map_row(row, root, available=None):
    location = coordinates(row.get("latitude"), row.get("longitude"))
    if location is None:
        return None
    metadata = {field_name(column): typed_value(column, value) for column, value in row.items()}
    creator = metadata.get("creator_username") or "Unknown contributor"
    source = f"https://www.mapillary.com/app/?pKey={row['id']}"
    metadata.update(
        license="CC-BY-SA 4.0",
        license_url=LICENSE_URL,
        attribution=f"{creator}; Mapillary ({source}); OSV-5M authors (Astruc et al., CVPR 2024); "
        f"CC-BY-SA 4.0 ({LICENSE_URL})",
    )
    path = image_path(root, row["id"]) if available is None else available[row["id"]]
    return Sample(filepath=str(path), location=location, metadata=metadata)


def iter_samples(root, selected, available=None):
    for row in csv_rows(root):
        if row["id"] in selected:
            yield map_row(row, root, available)


def main():
    args = build_parser().parse_args()
    root = args.data_root.resolve()
    available = available_images(root)
    print(f"Available JPEGs: {len(available)}", flush=True)
    counts: Counter[str] = Counter()
    selected = select_ids(root, args.limit, counts, available)
    print(f"Selection: {dict(counts)}", flush=True)
    first_row: dict[str, str] = next(csv_rows(root), {})
    field_types = {field_name(column): column_type(column) for column in first_row}
    field_types.update(dict.fromkeys(("license", "license_url", "attribution"), "string"))
    ingest(args, iter_samples(root, selected, available), field_types, DATASET_CREDITS['osv5m-test'], counts)


if __name__ == "__main__":
    main()
