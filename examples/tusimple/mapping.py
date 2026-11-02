"""Shared streaming, selective extraction and inline CLIP ingestion helpers."""

import hashlib
import json
import shutil
import sys
from contextlib import closing
from dataclasses import dataclass
from itertools import islice
from pathlib import Path, PurePosixPath
from weakref import WeakKeyDictionary
from zipfile import ZipFile

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import Sample  # noqa: E402

from examples._shared import importer  # noqa: E402

DATA_LICENSE = (
    "Dataset terms: supplement permits training use; supplied lane and velocity train/test terms do not "
    "expressly grant public display or redistribution. Apache-2.0 covers benchmark code, not established "
    "for dataset archives. Project policy: local evaluation only; not for the public demo."
)
ATTRIBUTION = "TuSimple benchmark"


def build_parser(benchmark, source_root, splits):
    result = importer.parser(
        f"Import TuSimple {benchmark} (offline)",
        f"tusimple-{benchmark}",
        batch_size=32,
        limit_help='First N frames total, interleaved across splits; 0 = all',
        media_uri_option=False,  # its terms keep the data local: never upload it
    )
    result.add_argument(
        '--source-root',
        type=Path,
        default=Path(source_root),
        help=f'Archives (default: {source_root})',
    )
    result.add_argument('--splits', nargs='+', choices=splits, default=list(splits))
    return result


def safe_member(name):
    """Reject archive paths that could escape the destination."""
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe archive member: {name!r}")
    return path


@dataclass
class Frame:
    archive: ZipFile | None
    member: str
    split: str
    clip_id: str
    annotation: object

    def destination(self, root, benchmark):
        base = (root / benchmark / self.split).resolve()
        target = (base / safe_member(self.member)).resolve()
        if not base.is_relative_to(root.resolve()) or not target.is_relative_to(base):
            raise ValueError(f"Destination escapes data root: {target}")
        return target


def stable_id(*parts):
    return hashlib.sha256("/".join(map(str, parts)).encode()).hexdigest()[:24]


def json_lines(stream):
    for number, line in enumerate(stream, 1):
        if line.strip():
            try:
                yield json.loads(line)
            except ValueError as exc:
                raise ValueError(f"Invalid JSON on line {number}") from exc


def json_array(stream, chunk_size=65536):
    """Stream a top-level array of objects/arrays without loading the entire file.

    The benchmark's supp annotations and test ground truth are JSON arrays,
    unlike the lane JSONL files. Memory is bounded by one record plus a chunk.
    """
    decoder = json.JSONDecoder()
    buffer = ""
    eof = False

    def fill():
        nonlocal buffer, eof
        chunk = stream.read(chunk_size)
        eof = not chunk
        buffer += chunk

    def whitespace():
        nonlocal buffer
        buffer = buffer.lstrip()
        while not buffer and not eof:
            fill()
            buffer = buffer.lstrip()

    whitespace()
    if not buffer.startswith("["):
        raise ValueError("Expected a JSON array")
    buffer = buffer[1:]
    whitespace()
    if not buffer.startswith("]"):
        while True:
            whitespace()
            if not buffer.startswith(("{", "[")):
                raise ValueError("Expected an object or array record")
            while True:
                try:
                    item, end = decoder.raw_decode(buffer)
                    break
                except json.JSONDecodeError:
                    if eof:
                        raise ValueError("Invalid or truncated JSON array") from None
                    fill()
            buffer = buffer[end:]
            yield item
            whitespace()
            if buffer.startswith("]"):
                break
            if not buffer.startswith(","):
                raise ValueError("Expected comma or closing bracket")
            buffer = buffer[1:]
    buffer = buffer[1:]
    whitespace()
    if buffer:
        raise ValueError("Trailing data after JSON array")


def round_robin(iterators):
    """Represent every split in limited runs; close open zips even on early exit."""
    active = list(iterators)
    all_iterators = active.copy()
    try:
        while active:
            for iterator in active.copy():
                try:
                    yield next(iterator)
                except StopIteration:
                    active.remove(iterator)
    finally:
        for iterator in all_iterators:
            iterator.close()


def extract_frame(frame, destination):
    """Extract one member atomically; repair interrupted/truncated extractions."""
    assert frame.archive is not None
    info = frame.archive.getinfo(frame.member)
    if destination.is_file() and destination.stat().st_size == info.file_size:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    if temporary.is_symlink():
        raise ValueError(f"Refusing symlink: {temporary}")
    try:
        with frame.archive.open(info) as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def ingest(args, benchmark, frames, map_labels):
    root = args.data_root.resolve()
    sample_frames: WeakKeyDictionary[Sample, Frame] = WeakKeyDictionary()

    def samples():
        with closing(frames):
            records = islice(frames, args.limit) if args.limit else frames
            for frame in records:
                path = frame.destination(root, benchmark)
                # Must extract before advancing a generator that owns an open ZIP.
                extract_frame(frame, path)
                sample = Sample(str(path), tags=[frame.split])
                sample_frames[sample] = frame
                yield sample

    def prepare(sample):
        frame = sample_frames[sample]
        with Image.open(sample.filepath) as image:
            metadata = map_labels(frame, *image.size)
        metadata.update(
            split=frame.split,
            clip_id=frame.clip_id,
            source_file=frame.member,
            license=DATA_LICENSE,
            attribution=ATTRIBUTION,
        )
        sample.metadata = metadata

    return importer.ingest(
        args,
        samples(),
        {},
        f"TuSimple {benchmark}; LOCAL-ONLY, not for the public demo; terms: examples/tusimple/README.md",
        prepare=prepare,
    )
