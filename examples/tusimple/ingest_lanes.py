#!/usr/bin/env python3
"""Import the annotated 20th frame from TuSimple lane train/test clips."""

import sys
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import Polyline, Polylines  # noqa: E402

from examples.tusimple.mapping import (  # noqa: E402
    Frame,
    build_parser,
    ingest,
    json_lines,
    round_robin,
    safe_member,
    stable_id,
)


def parser():
    return build_parser("lanes", "TusimpleLaneDetection", ("train", "test"))


def iter_split(source, split):
    with ZipFile(source / f"{split}_set.zip") as archive:
        if split == "train":
            labels = sorted(
                name for name in archive.namelist() if name.startswith("label_data_") and name.endswith(".json")
            )
            if not labels:
                raise ValueError("No label_data_*.json in train_set.zip")
        else:
            labels = ["test_label.json"]
        for name in labels:
            stream = archive.open(name) if split == "train" else (source / name).open("rb")
            with stream:
                for record in json_lines(stream):
                    member = str(safe_member(record["raw_file"]))
                    if PurePosixPath(member).name != "20.jpg":
                        raise ValueError(f"Expected annotated 20.jpg, got {member}")
                    yield Frame(archive, member, split, str(PurePosixPath(member).parent), record)


def iter_frames(source, splits=("train", "test")):
    return round_robin([iter_split(source, split) for split in dict.fromkeys(splits)])


def map_labels(frame, width, height):
    record = frame.annotation
    labels = Polylines()
    for lane_index, xs in enumerate(record["lanes"]):
        if len(xs) != len(record["h_samples"]):
            raise ValueError("Lane x coordinates and h_samples must have equal lengths")
        paths: list = []
        run: list = []
        for x, y in zip(xs, record["h_samples"], strict=True):
            if x == -2:
                if run:
                    paths.append(run)
                    run = []
            else:
                if not 0 <= x < width or not 0 <= y < height:
                    raise ValueError(f"Lane point outside image: {(x, y)}")
                run.append([x / width, y / height])
        if run:
            paths.append(run)
        for segment_index, path in enumerate(paths):
            # Preserve existing IDs for ordinary lanes; extra runs need their
            # own labels because the renderer draws only paths[0].
            identity = ("lanes", frame.split, frame.clip_id, lane_index)
            label = Polyline(
                "lane",
                paths=[path],
                closed=False,
                filled=False,
                id=stable_id(*identity) if segment_index == 0 else stable_id(*identity, segment_index),
                lane_index=lane_index,
                segment_index=segment_index,
                num_points=len(path),
            )
            # match_elements evaluates direct label keys.
            label["num_points"] = len(path)
            labels.append(label)
    return {"lanes": labels, "lane_count": len(record["lanes"]), "width": width, "height": height}


def main():
    args = parser().parse_args()
    ingest(args, "lanes", iter_frames(args.source_root, args.splits), map_labels)


if __name__ == "__main__":
    main()
