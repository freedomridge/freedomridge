"""Read KITTI raw-drive OXTS with bounded HTTP ranges and an atomic, offline cache."""

from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from examples._shared.importer import contained_path

RAW_BASE_URL = 'https://s3.eu-central-1.amazonaws.com/avg-kitti/raw_data'
MAX_RANGE = 32 * 1024 * 1024
CACHE_VERSION = 1


def validate_member(member):
    """Lexical ZIP checks independent of the working directory."""
    path = PurePosixPath(member)
    if not member or path.is_absolute() or '..' in path.parts or '\\' in member or ':' in member:
        raise ValueError(f'Unsafe archive member: {member!r}')
    return path


def safe_path(root, member):
    """Reject zip-slip and resolved symlink escapes, including on resume."""
    path = validate_member(member)
    root = Path(root).resolve()
    return contained_path(root.joinpath(*path.parts), root)


def atomic_write(path, writer):
    """Publish only a complete file; temporary names cannot follow existing links."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.kitti-', delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            writer(temporary)
            temporary.flush()
            os.chmod(temporary_path, 0o644)
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def extract_member(archive, member, root):
    target = safe_path(root, member)
    info = archive.getinfo(member)
    if stat.S_ISLNK(info.external_attr >> 16) or info.is_dir():
        raise ValueError(f'Expected a regular archive member: {member}')
    if target.is_file() and target.stat().st_size == info.file_size:
        target.chmod(0o644)
        return target
    with archive.open(info) as source:
        atomic_write(target, lambda output: shutil.copyfileobj(source, output, length=1024 * 1024))
    return target


@dataclass(frozen=True)
class RawFrame:
    date: str
    drive: str
    frame: int


def parse_mapping(mapping_text, random_text):
    rows = []
    for line in mapping_text.splitlines():
        date, drive, frame = line.split()
        if not re.fullmatch(r'\d{4}_\d{2}_\d{2}', date):
            raise ValueError('Invalid KITTI date')
        datetime.strptime(date, '%Y_%m_%d')
        if not re.fullmatch(re.escape(date) + r'_drive_\d{4}_sync', drive) or not frame.isdecimal():
            raise ValueError('Invalid KITTI raw drive/frame')
        rows.append(RawFrame(date, drive, int(frame)))
    order = [int(value) for value in re.split(r'[,\s]+', random_text.strip()) if value]
    if len(set(order)) != len(order) or len(order) != len(rows) or any(i < 1 or i > len(rows) for i in order):
        raise ValueError('train_rand must be a 1-based permutation of train_mapping')
    return {f'{i:06d}': rows[index - 1] for i, index in enumerate(order)}


def read_mapping(source_root):
    with ZipFile(Path(source_root) / 'devkit_object.zip') as archive:
        return parse_mapping(
            archive.read('mapping/train_mapping.txt').decode('utf-8'),
            archive.read('mapping/train_rand.txt').decode('utf-8'),
        )


def parse_oxts(text, timestamp):
    values = [float(value) for value in text.split()]
    if len(values) != 30 or not all(math.isfinite(value) for value in values):
        raise ValueError('OXTS packets must contain 30 finite numbers')
    lat, lon, altitude, _, _, yaw, north, east = values[:8]
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('Invalid OXTS coordinates')
    raw_timestamp = timestamp.strip()
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?', raw_timestamp)
    if match is None:
        raise ValueError('Expected a naive raw KITTI timestamp with up to 9 fractional digits')
    captured = datetime.strptime(match[1], '%Y-%m-%d %H:%M:%S').replace(
        microsecond=int((match[2] or '').ljust(6, '0')[:6]),
    )
    # The raw text has no zone. Keep it losslessly and expose this interpretation.
    captured = captured.replace(tzinfo=ZoneInfo('Europe/Berlin'))
    return {
        'location': (lon, lat),
        'heading': (90 - math.degrees(yaw)) % 360,
        'altitude_m': altitude,
        'speed_kmh': math.hypot(north, east) * 3.6,
        'captured_at': captured.isoformat(),
        'oxts_timestamp_raw': raw_timestamp + ' (Europe/Berlin local)',
        'timestamp_timezone': 'Europe/Berlin (CEST; corroborated by sun position)',
    }


class HTTPRangeFile(io.RawIOBase):
    """Seekable ZIP reader: tail, central directory, then explicitly prefetched OXTS.

    Refuses servers that ignore Range, mismatched responses, and large reads. A
    small tail request obtains both the archive size and its end-of-directory.
    zipfile handles ZIP64, compression and CRC validation without a full download.
    """

    def __init__(self, url):
        super().__init__()
        self.url = url
        self.position = 0
        self.size = 0
        self.blocks: list[tuple[int, bytes]] = []
        self._request('bytes=-65558')

    def _request(self, byte_range):
        for attempt in range(3):
            try:
                return self._request_once(byte_range)
            except HTTPError as error:
                if error.code not in (408, 429) and error.code < 500:
                    raise
                if attempt == 2:
                    raise
            except (OSError, HTTPException):
                if attempt == 2:
                    raise
            time.sleep(2**attempt)
        raise AssertionError('Unreachable retry state')

    def _request_once(self, byte_range):
        request = Request(self.url, headers={'Range': byte_range, 'Accept-Encoding': 'identity'})
        with urlopen(request, timeout=60) as response:
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
            if (
                response.status != 206
                or match is None
                or response.headers.get('Content-Encoding', 'identity') != 'identity'
            ):
                raise ValueError('Server must honor HTTP Range with an unencoded 206 response')
            start, end, total = map(int, match.groups())
            if not 0 <= start <= end < total or end - start + 1 > MAX_RANGE:
                raise ValueError('Invalid or oversized Content-Range')
            if self.size and total != self.size:
                raise ValueError('Remote ZIP size changed during fetch')
            if byte_range.startswith('bytes=-'):
                expected = (max(0, total - int(byte_range[7:])), total - 1)
            else:
                left, right = map(int, byte_range[6:].split('-'))
                expected = (left, right)
            if (start, end) != expected:
                raise ValueError('Server returned a different byte range')
            data = response.read(end - start + 2)
            if len(data) < end - start + 1:
                raise OSError('Truncated range response')
            if len(data) > end - start + 1:
                raise ValueError('Oversized range response')
        self.size = total
        self.blocks.append((start, data))
        return data

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        if whence not in (io.SEEK_SET, io.SEEK_CUR, io.SEEK_END):
            raise ValueError('Invalid seek mode')
        position = offset + (self.position if whence == io.SEEK_CUR else self.size if whence == io.SEEK_END else 0)
        if position < 0:
            raise ValueError('Negative seek')
        self.position = position
        return position

    def read(self, size=-1):
        size = min(self.size - self.position, size) if size >= 0 else self.size - self.position
        if size <= 0:
            return b''
        if size > MAX_RANGE:
            raise ValueError('Refusing a large remote ZIP read')
        for start, data in reversed(self.blocks):
            if start <= self.position and self.position + size <= start + len(data):
                result = data[self.position - start : self.position - start + size]
                break
        else:
            result = self._request(f'bytes={self.position}-{self.position + size - 1}')
        self.position += size
        return result

    def prefetch(self, start, end):
        self.seek(start)
        self.read(end - start)


def cache_path(cache_root, drive):
    if not re.fullmatch(r'\d{4}_\d{2}_\d{2}_drive_\d{4}_sync', drive):
        raise ValueError('Invalid drive cache key')
    return safe_path(cache_root, f'{drive}.json')


def load_drive(cache_root, drive):
    path = cache_path(cache_root, drive)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or data.get('version') != CACHE_VERSION
        or data.get('drive') != drive
        or not isinstance(data.get('members'), dict)
    ):
        raise ValueError(f'Invalid OXTS cache: {path.name}')
    validate_members(data['members'])
    return data


def validate_members(members):
    if 'timestamps.txt' not in members or not isinstance(members['timestamps.txt'], str):
        raise ValueError('OXTS cache has no timestamps')
    timestamps = members['timestamps.txt'].splitlines()
    packets = 0
    for name, content in members.items():
        if name == 'timestamps.txt':
            continue
        if not re.fullmatch(r'data/\d{10}\.txt', name) or not isinstance(content, str):
            raise ValueError('Unexpected OXTS member')
        frame = int(PurePosixPath(name).stem)
        if frame >= len(timestamps):
            raise ValueError('Missing timestamp for OXTS packet')
        parse_oxts(content, timestamps[frame])
        packets += 1
    if not packets:
        raise ValueError('OXTS cache has no packets')


def fetch_drive(cache_root, raw, base_url=RAW_BASE_URL):
    """Fetch ONLY a drive's OXTS span; complete cached drives make zero requests."""
    try:
        cached = load_drive(cache_root, raw.drive)
    except ValueError:
        cached = None  # Refetch corrupt JSON, invalid packets and old cache versions.
    if cached is not None:
        return cached
    path = cache_path(cache_root, raw.drive)
    url = f'{base_url.rstrip("/")}/{raw.drive.removesuffix("_sync")}/{raw.drive}.zip'
    prefix = f'{raw.date}/{raw.drive}/oxts/'
    with HTTPRangeFile(url) as remote, ZipFile(remote) as archive:
        entries = sorted(archive.infolist(), key=lambda info: info.header_offset)
        selected = [
            info
            for info in entries
            if info.filename.startswith(prefix)
            and re.fullmatch(r'timestamps\.txt|data/\d{10}\.txt', info.filename.removeprefix(prefix))
        ]
        if not selected:
            raise ValueError('Raw ZIP has no OXTS text entries')
        if sum(info.file_size for info in selected) > MAX_RANGE:
            raise ValueError('Oversized OXTS contents')
        first, last = selected[0].header_offset, selected[-1].header_offset
        for info in entries:
            if first <= info.header_offset <= last and not info.filename.startswith(prefix):
                raise ValueError('OXTS ZIP entries are not contiguous; refusing unrelated data')
        following = [info.header_offset for info in entries if info.header_offset > last]
        end = following[0] if following else archive.start_dir
        remote.prefetch(first, end)
        members = {}
        for info in selected:
            name = info.filename.removeprefix(prefix)
            if name in members:
                raise ValueError('Duplicate OXTS member')
            members[name] = archive.read(info).decode('utf-8')
    validate_members(members)
    data = {'version': CACHE_VERSION, 'drive': raw.drive, 'source_url': url, 'members': members}
    atomic_write(path, lambda output: output.write(json.dumps(data, sort_keys=True).encode()))
    return data


def frame_gps(cache, raw):
    if cache is None:
        return {}
    packet = cache['members'].get(f'data/{raw.frame:010d}.txt')
    if packet is None:
        return {}
    return parse_oxts(packet, cache['members']['timestamps.txt'].splitlines()[raw.frame])
