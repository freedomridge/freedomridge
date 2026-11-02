#!/usr/bin/env python3
"""Download public demo releases with parallel resumable ranges and safe extraction."""

import argparse
import base64
import errno
import fcntl
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

from examples._shared.catalog import CATALOG, MANUAL, SOURCES, selection  # noqa: E402

CHUNK = 1024 * 1024


def confined(path, root):
    # Root is resolved once by the caller. Only aliases BELOW it are unsafe.
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.is_relative_to(root) or '..' in path.parts:
        raise ValueError(f'Path outside data root: {path}')
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError(f'Symlink path refused: {path}; choose an ordinary destination below the data root')
        if item == root:
            break
    return path


@contextmanager
def directory_lock(directory):
    """Lock the directory inode: no leftover lock files or unlink/waiter races."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def write_json(path, value):
    temporary = confined(path.with_name(path.name + '.tmp'), path.parent)
    with temporary.open('w') as output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def require_space(root, required, operation):
    free = shutil.disk_usage(root).free
    print(f'Required free disk for {operation}: {required:,} bytes; available: {free:,} bytes', flush=True)
    if required > free:
        raise ValueError(
            f'Insufficient disk space for {operation}: free {free:,}, need {required:,} bytes. Free space first',
        )


def publish(temporary, target):
    """Called under the destination directory lock; preserve existing files."""
    try:
        os.link(temporary, target)
    except OSError as error:
        if error.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV):
            raise
        if target.exists() or target.is_symlink():
            raise FileExistsError(f'Destination already exists: {target}') from error
        os.rename(temporary, target)


def checksum_from_headers(headers):
    if headers.get('Content-MD5'):
        return 'md5:' + base64.b64decode(headers['Content-MD5'], validate=True).hex()
    for item in headers.get('x-goog-hash', '').split(','):
        if item.strip().startswith('md5='):
            return 'md5:' + base64.b64decode(item.strip()[4:], validate=True).hex()
    # Hugging Face LFS object SHA-256, when supplied by the source response.
    etag = headers.get('x-linked-etag', '').strip('"')
    if re.fullmatch('[a-fA-F0-9]{64}', etag):
        return 'sha256:' + etag.lower()
    # Single-part S3 object ETags publish MD5; multipart ETags are not digests.
    etag = headers.get('ETag', '').strip('"')
    if 'x-amz-request-id' in headers and re.fullmatch('[a-fA-F0-9]{32}', etag):
        return 'md5:' + etag.lower()
    return None


def verify_file(path, size, checksum):
    if path.stat().st_size != size:
        raise ValueError(f'Size mismatch: {path}; expected {size} bytes. Inspect/remove this file before retrying')
    if checksum:
        algorithm, expected = checksum.split(':', 1)
        digest = hashlib.new(algorithm)
        with path.open('rb') as stream:
            while block := stream.read(CHUNK):
                digest.update(block)
        if digest.hexdigest().lower() != expected.lower():
            raise ValueError(
                f'{algorithm} checksum mismatch: {path}. Inspect/remove this file and its .parts directory',
            )


def download(url, destination, connections=8, attempts=4, checksum=None):
    """Resume ranges, append/delete parts during assembly, and publish atomically.

    The first part becomes the archive. At most one additional range is copied
    at a time; a durable assembly checkpoint makes interruption resumable too.
    Completed receipts allow offline reruns of these frozen releases.
    """
    destination = Path(destination)
    destination = destination.parent.resolve() / destination.name
    if connections < 1 or attempts < 1:
        raise ValueError('connections and attempts must be positive')
    confined(destination, destination.parent)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with directory_lock(destination.parent):
        receipt = confined(destination.with_name(destination.name + '.download.json'), destination.parent)
        if destination.exists() and receipt.exists():
            saved = json.loads(receipt.read_text())
            if saved['url'] == url and (not checksum or saved['checksum'] == checksum):
                stat = destination.stat()
                if saved['stat'] != [stat.st_size, stat.st_mtime_ns]:
                    verify_file(destination, saved['size'], saved['checksum'])
                    saved['stat'] = [stat.st_size, stat.st_mtime_ns]
                    write_json(receipt, saved)
                print(f'Complete (verified receipt): {destination.name}', flush=True)
                return destination
        published = []

        class RedirectHeaders(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                digest = checksum_from_headers(headers)
                if digest:
                    published.append(digest)
                redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
                if redirected is not None and req.get_method() == 'HEAD':
                    redirected.method = 'HEAD'
                return redirected

        request = urllib.request.Request(url, method='HEAD', headers={'Accept-Encoding': 'identity'})
        opener = urllib.request.build_opener(RedirectHeaders())
        with opener.open(request, timeout=10) as response:
            length = response.headers.get('Content-Length')
            if length is None or not length.isdecimal():
                raise ValueError(f'Source has no valid Content-Length: {url}; use a source with a known size')
            size = int(length)
            validator = response.headers.get('ETag') or response.headers.get('Last-Modified')
            checksum = checksum or (published[0] if published else checksum_from_headers(response.headers))
        identity = {'url': url, 'size': size, 'validator': validator, 'checksum': checksum}

        def complete():
            stat = destination.stat()
            write_json(receipt, {**identity, 'stat': [stat.st_size, stat.st_mtime_ns]})
            print(f'Complete: {destination.name} ({size} bytes)', flush=True)
            return destination

        if destination.exists():
            verify_file(destination, size, checksum)
            return complete()
        parts = confined(destination.with_name(destination.name + '.parts'), destination.parent)
        parts.mkdir(exist_ok=True)
        manifest = confined(parts / 'manifest.json', parts)
        if manifest.exists():
            saved = json.loads(manifest.read_text())
            if saved['source'] != identity:
                raise ValueError(f'Remote file changed; inspect/remove old partial download: {parts}')
        else:
            step = max(1, -(-size // connections))
            saved = {
                'source': identity,
                'ranges': [[start, min(start + step, size) - 1] for start in range(0, size, step)],
            }
            write_json(manifest, saved)
        ranges = saved['ranges']
        assembled = saved.get('assembled', 0)
        part_paths = [confined(parts / f'{index:04}.part', parts) for index in range(len(ranges))]
        # Ignore parts already incorporated in part 0. A crash may leave one
        # copied part, or an uncommitted tail: both are reconciled below.
        held = sum(path.stat().st_size for path in part_paths if path.exists())
        overhead = max((end - start + 1 for start, end in ranges[1:]), default=0)
        require_space(destination.parent, max(0, size - held) + overhead, destination.name)
        stop = threading.Event()

        class RangeUnsupported(ValueError):
            pass

        def fetch(item):
            index, (start, end) = item
            if index < assembled:
                return
            part = part_paths[index]
            wanted = end - start + 1
            for attempt in range(attempts):
                if stop.is_set():
                    return
                have = part.stat().st_size if part.exists() else 0
                if have == wanted:
                    return
                if have > wanted:
                    raise ValueError(f'Oversized partial file: {part}; inspect/remove {parts}')
                headers = {'Range': f'bytes={start + have}-{end}', 'Accept-Encoding': 'identity'}
                if validator:
                    headers['If-Range'] = validator
                req = urllib.request.Request(url, headers=headers)
                try:
                    # Bound a stalled read/connect so Ctrl-C never waits for the
                    # whole archive. read1 returns available bytes promptly.
                    with urllib.request.urlopen(req, timeout=2) as response:
                        expected = f'bytes {start + have}-{end}/{size}'
                        full = response.status == 200 and start == have == 0 and wanted == size
                        if not full and (response.status != 206 or response.headers.get('Content-Range') != expected):
                            raise RangeUnsupported(
                                f'Server did not honor range {expected}; inspect/remove {parts} to restart',
                            )
                        if int(response.headers.get('Content-Length', wanted - have)) != wanted - have:
                            raise ValueError(f'Incorrect range Content-Length; inspect/remove {parts}')
                        with part.open('ab') as out:
                            while have < wanted:
                                if stop.is_set():
                                    return
                                block = response.read1(min(CHUNK, wanted - have))
                                if not block:
                                    raise EOFError('Interrupted range download')
                                out.write(block)
                                have += len(block)
                        return
                except (OSError, EOFError, http.client.HTTPException, urllib.error.URLError):
                    if stop.is_set():
                        return
                    if attempt + 1 == attempts:
                        raise
                    stop.wait(min(4, 2**attempt))

        pool = ThreadPoolExecutor(max_workers=connections)
        fallback = False
        try:
            futures = [pool.submit(fetch, item) for item in enumerate(ranges)]
            for future in as_completed(futures):
                future.result()
        except RangeUnsupported:
            stop.set()
            pool.shutdown(wait=True, cancel_futures=True)
            if assembled or any(path.exists() and path.stat().st_size for path in part_paths):
                raise
            fallback = True
        finally:
            # This also runs on KeyboardInterrupt, BEFORE shutdown waits.
            stop.set()
            pool.shutdown(wait=True, cancel_futures=True)
        if fallback:
            saved['ranges'] = ranges = [[0, size - 1]] if size else []
            part_paths = [parts / '0000.part'] if size else []
            write_json(manifest, saved)
            stop.clear()
            if ranges:
                fetch((0, ranges[0]))

        first = confined(parts / '0000.part', parts)
        if not ranges:
            first.touch()
        if not assembled and ranges:
            assembled = saved['assembled'] = 1
            write_json(manifest, saved)
        committed = ranges[assembled - 1][1] + 1 if assembled else 0
        with first.open('r+b') as output:
            output.truncate(committed)
            output.seek(committed)
            for index in range(1, assembled):
                part_paths[index].unlink(missing_ok=True)
            for index in range(assembled, len(ranges)):
                with part_paths[index].open('rb') as source:
                    shutil.copyfileobj(source, output, CHUNK)
                output.flush()
                os.fsync(output.fileno())
                saved['assembled'] = index + 1
                write_json(manifest, saved)
                part_paths[index].unlink()
        verify_file(first, size, checksum)
        os.chmod(first, 0o644)
        publish(first, destination)
        first.unlink(missing_ok=True)
        manifest.unlink()
        parts.rmdir()
        return complete()


def member_path(name, root):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
        raise ValueError(f'Unsafe archive member: {name!r}')
    return confined(root / path, root)


def extract(archive, destination):
    """Preflight safety and missing-file disk space; never overwrite existing files."""
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    archive = Path(archive)
    receipt = confined(archive.with_name(archive.name + '.extract.jsonl'), archive.parent)
    stamp = [str(root), archive.stat().st_size, archive.stat().st_mtime_ns]
    with directory_lock(root):
        if receipt.exists():
            with receipt.open() as records:
                if json.loads(next(records)) == stamp:
                    count = 0
                    for line in records:
                        name = json.loads(line)
                        if not member_path(name, root).exists():
                            break
                        count += 1
                    else:
                        print(f'Extraction complete: {count} existing entries in {root}', flush=True)
                        return
        required = 0

        def preflight(name, size, is_dir):
            nonlocal required
            target = member_path(name, root)
            if target.exists():
                if is_dir != target.is_dir():
                    raise ValueError(f'Archive file/directory conflict: {target}')
            else:
                required += 4096 if is_dir else ((size + 4095) // 4096) * 4096

        zipped = zipfile.is_zipfile(archive)
        if zipped:
            with zipfile.ZipFile(archive) as source:
                for entry in source.infolist():
                    mode = stat.S_IFMT(entry.external_attr >> 16)
                    if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise ValueError(f'Archive link/special file refused: {entry.filename}')
                    preflight(entry.filename, entry.file_size, entry.is_dir())
        else:
            with tarfile.open(archive, 'r|*') as tar_source:
                for tar_entry in tar_source:
                    if not (tar_entry.isfile() or tar_entry.isdir()):
                        raise ValueError(f'Archive link/special file refused: {tar_entry.name}')
                    preflight(tar_entry.name, tar_entry.size, tar_entry.isdir())
        require_space(root, required, f'extracting {archive.name}')
        extracted = skipped = 0
        fd, filename = tempfile.mkstemp(prefix='.extraction-record-', dir=receipt.parent)
        record_path = Path(filename)
        try:
            with os.fdopen(fd, 'w') as record:
                record.write(json.dumps(stamp) + '\n')

                def write(name, is_dir, open_source):
                    nonlocal extracted, skipped
                    target = member_path(name, root)
                    record.write(json.dumps(name) + '\n')
                    if is_dir:
                        target.mkdir(parents=True, exist_ok=True)
                        return
                    if target.exists():
                        skipped += 1
                        return
                    target.parent.mkdir(parents=True, exist_ok=True)
                    fd, filename = tempfile.mkstemp(prefix='.extract-', dir=target.parent)
                    temporary = Path(filename)
                    try:
                        with os.fdopen(fd, 'wb') as output, open_source() as source:
                            shutil.copyfileobj(source, output, CHUNK)
                        os.chmod(temporary, 0o644)
                        try:
                            publish(temporary, target)
                            extracted += 1
                        except FileExistsError:
                            skipped += 1
                    finally:
                        temporary.unlink(missing_ok=True)

                if zipped:
                    with zipfile.ZipFile(archive) as source:
                        for entry in source.infolist():
                            write(entry.filename, entry.is_dir(), lambda: source.open(entry))
                else:
                    with tarfile.open(archive, 'r|*') as tar_source:
                        for tar_entry in tar_source:
                            write(tar_entry.name, tar_entry.isdir(), lambda: tar_source.extractfile(tar_entry))
            record_path.replace(receipt)
        finally:
            record_path.unlink(missing_ok=True)
        print(f'Extracted {extracted} files ({skipped} skipped) into {root}', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--datasets', default='coco')
    parser.add_argument('--data-root', type=Path, default=os.getenv('FRIDGE_DATA_ROOT') or None)
    parser.add_argument('--connections', type=int, default=8)
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args(argv)
    if args.list:
        for name, (size, license_text) in CATALOG.items():
            print(f'{name:14} {size:28} {license_text}')
        return 0
    if not args.data_root:
        parser.error('Set FRIDGE_DATA_ROOT or supply --data-root')
    if args.connections < 1:
        parser.error('--connections must be positive')
    try:
        names = selection(args.datasets, CATALOG)
        root = args.data_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        for name in names:
            if name in MANUAL:
                print(f'{name}: {MANUAL[name]}')
                continue
            for source in SOURCES[name]:
                path = confined(root / source.path, root)
                download(source.url, path, args.connections, checksum=source.checksum)
                if source.extract_to:
                    extract(path, confined(root / source.extract_to, root))
    except KeyboardInterrupt:
        print('Download interrupted. Partial ranges kept; rerun the same command to resume.', file=sys.stderr)
        return 130
    except (ValueError, urllib.error.HTTPError, zipfile.BadZipFile, tarfile.TarError) as error:
        print(f'Download stopped: {error}', file=sys.stderr)
        return 1
    except (OSError, EOFError, http.client.HTTPException) as error:
        retry = isinstance(error, (EOFError, TimeoutError, ConnectionError, urllib.error.URLError))
        advice = ' Rerun the same command to resume.' if retry else ' Correct the reported error before retrying.'
        print(f'Download stopped: {error}.{advice}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
