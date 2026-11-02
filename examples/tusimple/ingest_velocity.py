#!/usr/bin/env python3
"""Import TuSimple velocity last frames and supplementary vehicle detections."""

import io
import json
import math
import sys
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import Detection, Detections  # noqa: E402

from examples.tusimple.mapping import (  # noqa: E402
    Frame,
    build_parser,
    ingest,
    json_array,
    round_robin,
    safe_member,
    stable_id,
)


def parser():
    return build_parser("velocity", "TusimpleVelocity", ("train", "test", "supp"))


def test_records(source, archive, clip_ids):
    """gt.json is ordered by numeric clip ID, starting at 1; verify its boxes."""
    if clip_ids != list(range(1, len(clip_ids) + 1)):
        raise ValueError("Test clip IDs must be consecutive from 1 to align gt.json")
    with (source / "gt.json").open() as stream:
        for clip_id, record in zip(clip_ids, json_array(stream), strict=True):
            boxes = json.loads(archive.read(f"clips/{clip_id}/annotation.json"))
            if [vehicle["bbox"] for vehicle in record] != [vehicle["bbox"] for vehicle in boxes]:
                raise ValueError(f"gt.json bounding boxes do not match test clip {clip_id}")
            yield clip_id, record


def iter_split(source, split):
    with ZipFile(source / f"benchmark_velocity_{split}.zip") as archive:
        if split == "supp":
            with archive.open("annotation.json") as raw, io.TextIOWrapper(raw) as stream:
                for record in json_array(stream):
                    member = str(safe_member(record["file_name"]))
                    yield Frame(
                        archive,
                        member,
                        split,
                        PurePosixPath(member).stem,
                        [{"bbox": b} for b in record["bbox"]],
                    )
            return
        clip_ids = sorted(
            int(PurePosixPath(n).parent.name) for n in archive.namelist() if n.endswith("/annotation.json")
        )
        records = (
            test_records(source, archive, clip_ids)
            if split == "test"
            else ((i, json.loads(archive.read(f"clips/{i}/annotation.json"))) for i in clip_ids)
        )
        try:
            for clip_id, record in records:
                yield Frame(archive, f"clips/{clip_id}/imgs/040.jpg", split, str(clip_id), record)
        finally:
            records.close()


def iter_frames(source, splits=("train", "test", "supp")):
    return round_robin([iter_split(source, split) for split in dict.fromkeys(splits)])


def source_components(pair):
    """Validate TuSimple's [forward, right] components without swapping axes."""
    if len(pair) != 2 or not all(math.isfinite(float(value)) for value in pair):
        raise ValueError(f"Expected two finite motion components: {pair}")
    return tuple(map(float, pair))


def map_labels(frame, width, height):
    detections = Detections()
    for index, vehicle in enumerate(frame.annotation):
        box = vehicle["bbox"]
        left, top, right, bottom = (float(box[k]) for k in ("left", "top", "right", "bottom"))
        if not all(math.isfinite(v) for v in (left, top, right, bottom)) or right < left or bottom < top:
            raise ValueError(f"Invalid vehicle box: {box}")
        attributes: dict = {"vehicle_index": index}
        # The supplement contains 49 zero-area boxes. Preserve and flag the
        # source annotations instead of dropping vehicles or inventing extent.
        if right == left or bottom == top:
            attributes["degenerate_bbox"] = True
        if frame.split != "supp":
            velocity_forward, velocity_right = source_components(vehicle["velocity"])
            position_forward, position_right = source_components(vehicle["position"])
            attributes.update(
                velocity_forward=velocity_forward,
                velocity_right=velocity_right,
                position_forward=position_forward,
                position_right=position_right,
                speed=math.hypot(velocity_forward, velocity_right),
                distance=math.hypot(position_forward, position_right),
            )
        detection = Detection(
            "vehicle",
            bbox=[left / width, top / height, (right - left) / width, (bottom - top) / height],
            id=stable_id("velocity", frame.split, frame.clip_id, index),
            **attributes,
        )
        # match_elements filters direct element keys, not nested attributes.
        # Keep scalar mirrors on each detection for same-vehicle predicates.
        if frame.split != "supp":
            detection.update({key: value for key, value in attributes.items() if key != "vehicle_index"})
        detections.append(detection)
    return {
        "vehicles": detections,
        "vehicle_count": len(detections),
        "has_motion": frame.split != "supp",
        "width": width,
        "height": height,
    }


def main():
    args = parser().parse_args()
    ingest(args, "velocity", iter_frames(args.source_root, args.splits), map_labels)


if __name__ == "__main__":
    main()
