#!/usr/bin/env python3
"""Stream iNaturalist 2021 validation into Freedom Ridge with GPS and offline CLIP."""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import Classification, Sample  # noqa: E402

from examples._shared.credits import DATASET_CREDITS  # noqa: E402
from examples._shared.importer import coordinates, ingest, utc_datetime  # noqa: E402
from examples._shared.importer import parser as common_parser  # noqa: E402

TAXONOMY = ("kingdom", "phylum", "class", "order", "family", "genus")
FIELD_TYPES = {
    **dict.fromkeys(
        (*TAXONOMY, "common_name", "specific_epithet", "supercategory", "image_dir_name"),
        "string",
    ),
    **dict.fromkeys(("image_id", "annotation_id", "category_id", "width", "height", "license_id"), "number"),
    **dict.fromkeys(("file_name", "license", "license_url", "rights_holder", "attribution"), "string"),
    "date": "datetime",
    "latitude": "number",
    "longitude": "number",
    "location_uncertainty": "number",
    "species": "object",
}


class JSONStream:
    """Small stdlib incremental decoder; retains only one COCO item plus a read buffer."""

    def __init__(self, handle, chunk_size):
        self.handle, self.chunk_size = handle, chunk_size
        self.buffer = ""
        self.decoder = json.JSONDecoder()

    def read_more(self):
        chunk = self.handle.read(self.chunk_size)
        if not chunk:
            raise ValueError("Unexpected end of COCO JSON")
        self.buffer += chunk

    def peek(self):
        self.buffer = self.buffer.lstrip()
        while not self.buffer:
            self.read_more()
            self.buffer = self.buffer.lstrip()
        return self.buffer[0]

    def take(self, token):
        if self.peek() != token:
            raise ValueError(f"Expected {token!r} in COCO JSON")
        self.buffer = self.buffer[1:]

    def value(self):
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer)
                # A number can end at a chunk boundary before its remaining digits arrive.
                if end == len(self.buffer):
                    self.read_more()
                    continue
                self.buffer = self.buffer[end:]
                return value
            except json.JSONDecodeError:
                self.read_more()


def iter_coco_array(path, name, chunk_size=65536):
    """Read a named top-level array without materializing the document or other arrays."""
    with Path(path).open(encoding="utf-8") as handle:
        stream = JSONStream(handle, chunk_size)
        stream.take("{")
        while stream.peek() != "}":
            key = stream.value()
            stream.take(":")
            if stream.peek() == "[":
                stream.take("[")
                while stream.peek() != "]":
                    item = stream.value()
                    if key == name:
                        yield item
                    if stream.peek() == "]":
                        break
                    stream.take(",")
                stream.take("]")
                if key == name:
                    return
            else:
                stream.value()
            if stream.peek() == "}":
                break
            stream.take(",")
    raise ValueError(f"Missing top-level array {name!r} in {path}")


def build_parser():
    parser = common_parser(
        __doc__,
        "inaturalist-2021-val",
        batch_size=32,
        limit_help="First N eligible images in source JSON order after license/GPS/file checks; 0 = all",
    )
    parser.add_argument(
        "--licenses",
        default="no-derivatives",
        help="'no-derivatives' (default: every license except NoDerivatives), 'all', "
        "or comma-separated license IDs from val.json",
    )
    parser.set_defaults(reduce_errors='warn')
    return parser


def select_licenses(value, licenses):
    preset = value.strip().lower()
    if preset == "all":
        return set(licenses)
    if preset == "no-derivatives":
        # The app serves resized thumbnails and crops, so ND images are left out.
        return {key for key, item in licenses.items() if "NoDerivatives" not in item["name"]}
    try:
        selected = {int(token.strip()) for token in value.split(",")}
    except ValueError as exc:
        raise ValueError("--licenses must be 'all', 'no-derivatives' or comma-separated integer license IDs") from exc
    if not selected <= licenses.keys():
        raise ValueError(f"Unknown license IDs: {selected - licenses.keys()}")
    return selected


def map_image(image, annotation, category, license_info, root):
    location = coordinates(image.get("latitude"), image.get("longitude"))
    if location is None:
        return None
    path = (root / image["file_name"]).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Image path escapes data root: {image['file_name']}")
    metadata = {k: v for k, v in image.items() if k not in ("id", "license")}
    metadata.update({k: v for k, v in category.items() if k not in ("id", "name")})
    metadata.update(
        image_id=image["id"],
        annotation_id=annotation["id"],
        category_id=category["id"],
        species=Classification(category["name"], id=f"inat-{image['id']}"),
        scientific_name=category["name"],  # the plain string: exact filter + typeahead in the sidebar
        date=utc_datetime(image.get("date")),
        latitude=location[1],
        longitude=location[0],
        license_id=image["license"],
        license=license_info["name"],
        license_url=license_info["url"],
        attribution=f"{image.get('rights_holder') or 'Unknown rights holder'}; iNaturalist 2021; {license_info['url']}",
    )
    return Sample(filepath=str(path), location=location, metadata=metadata)


def iter_samples(root, limit, license_selection, counts):
    root = root.resolve()
    metadata = root / "val.json"
    categories = {c["id"]: c for c in iter_coco_array(metadata, "categories")}
    licenses = {item["id"]: item for item in iter_coco_array(metadata, "licenses")}
    allowed = select_licenses(license_selection, licenses)
    annotations = {}
    for annotation in iter_coco_array(metadata, "annotations"):
        if annotation["image_id"] in annotations:
            raise ValueError(f"Multiple species annotations for image {annotation['image_id']}")
        annotations[annotation["image_id"]] = annotation
    selected = 0
    for image in iter_coco_array(metadata, "images"):
        counts["rows"] += 1
        if coordinates(image.get("latitude"), image.get("longitude")) is None:
            counts["missing_coordinates"] += 1
            continue
        if image["license"] not in allowed:
            counts["license_excluded"] += 1
            continue
        annotation = annotations[image["id"]]
        sample = map_image(image, annotation, categories[annotation["category_id"]], licenses[image["license"]], root)
        if not Path(sample.filepath).is_file():
            counts["missing_image"] += 1
            continue
        # Keep scanning after the limit for complete missing-coordinate/image counts.
        if limit and selected >= limit:
            continue
        selected += 1
        counts["selected"] += 1
        yield sample


def main():
    args = build_parser().parse_args()
    root = args.data_root.resolve()
    print(
        f"Available JPEGs: {sum(1 for _ in (root / 'val').rglob('*.jpg'))}; metadata: {root / 'val.json'}",
        flush=True,
    )
    counts: Counter[str] = Counter()
    ingest(
        args,
        iter_samples(root, args.limit, args.licenses, counts),
        FIELD_TYPES,
        DATASET_CREDITS['inaturalist-2021-val'],
        counts,
        refresh_description=True,
    )


if __name__ == "__main__":
    main()
