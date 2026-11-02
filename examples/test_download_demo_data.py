"""Local-only range server, interrupted transfer, checksums and hostile archives."""

import hashlib
import io
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from examples._shared.catalog import SOURCES, Source
from examples.download_demo_data import download, extract, main


@pytest.fixture
def server():
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('images/one.txt', b'example' * 30000)
    payload = data.getvalue()
    state: dict[str, Any] = {
        'interrupt': False,
        'requests': [],
        'ignore_range': False,
        'etag': 'fixture-v1',
        'slow': False,
        'no_length': False,
        'offline': False,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            if state['offline']:
                self.send_error(503)
                return
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/data.zip')
                self.send_header('x-linked-etag', '0' * 64)
                self.end_headers()
                return
            self.send_response(200)
            if not state['no_length']:
                self.send_header('Content-Length', str(len(payload)))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('ETag', state['etag'])
            self.send_header('x-linked-etag', hashlib.sha256(payload).hexdigest())
            self.end_headers()

        def do_GET(self):
            if self.headers.get('Range') and not state['ignore_range']:
                start, end = map(int, self.headers['Range'].removeprefix('bytes=').split('-'))
            else:
                start, end = 0, len(payload) - 1
            state['requests'].append((start, end))
            self.send_response(200 if state['ignore_range'] else 206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{len(payload)}')
            self.send_header('Content-Length', str(end - start + 1))
            self.end_headers()
            if state['interrupt']:
                self.wfile.write(payload[start : start + 101])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
            elif state['slow']:
                try:
                    for offset in range(start, end + 1, 1024):
                        self.wfile.write(payload[offset : min(offset + 1024, end + 1)])
                        self.wfile.flush()
                        time.sleep(0.1)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.wfile.write(payload[start : end + 1])

        def log_message(self, *_):
            pass

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}/data.zip', state, payload
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_parallel_interrupted_resume_and_idempotent_extraction(server, tmp_path):
    url, state, payload = server
    target = tmp_path / 'data.zip'
    state['interrupt'] = True
    with pytest.raises((OSError, EOFError)):
        download(url, target, connections=3, attempts=1)
    assert not target.exists()
    parts = sorted((tmp_path / 'data.zip.parts').glob('*.part'))
    assert parts and any(p.stat().st_size == 101 for p in parts)
    progress = {int(p.stem): p.stat().st_size for p in parts}
    state['interrupt'] = False
    state['requests'].clear()
    download(url, target, connections=2)  # original boundaries survive changed concurrency
    assert target.read_bytes() == payload
    step = -(-len(payload) // 3)
    assert all(start % step == progress.get(start // step, 0) for start, _ in state['requests'])
    state['requests'].clear()
    download(url, target)
    assert not state['requests']
    extract(target, tmp_path / 'out')
    image = tmp_path / 'out/images/one.txt'
    image.write_text('keep existing file')
    extract(target, tmp_path / 'out')
    assert image.read_text() == 'keep existing file'


def test_range_ignored_falls_back_to_single_stream(server, tmp_path):
    url, state, payload = server
    state['ignore_range'] = True
    assert download(url, tmp_path / 'data.zip').read_bytes() == payload


def test_checksum_and_size_failures(server, tmp_path):
    url, _, _ = server
    with pytest.raises(ValueError, match='checksum mismatch'):
        download(url, tmp_path / 'bad.zip', checksum='sha256:' + '0' * 64)
    assert not (tmp_path / 'bad.zip').exists()
    (tmp_path / 'existing.zip').write_bytes(b'wrong')
    with pytest.raises(ValueError, match='Size mismatch'):
        download(url, tmp_path / 'existing.zip')
    assert (tmp_path / 'existing.zip').read_bytes() == b'wrong'


def test_changed_remote_refuses_resume(server, tmp_path):
    url, state, _ = server
    state['interrupt'] = True
    with pytest.raises(EOFError):
        download(url, tmp_path / 'data.zip', attempts=1)
    state['etag'] = 'fixture-v2'
    with pytest.raises(ValueError, match='changed'):
        download(url, tmp_path / 'data.zip')


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'a/../../escape', 'a\\b', 'C:/escape'])
@pytest.mark.parametrize('kind', ['zip', 'tar'])
def test_archive_traversal_is_preflighted(tmp_path, name, kind):
    path = tmp_path / ('unsafe.' + kind)
    if kind == 'zip':
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('innocent', b'ok')
            archive.writestr(name, b'bad')
    else:
        with tarfile.open(path, 'w') as archive:
            for entry in ('innocent', name):
                info = tarfile.TarInfo(entry)
                info.size = 2
                archive.addfile(info, io.BytesIO(b'ok'))
    with pytest.raises(ValueError, match='Unsafe'):
        extract(path, tmp_path / 'out')
    assert not (tmp_path / 'out/innocent').exists()


@pytest.mark.parametrize('kind', ['zip', 'tar'])
def test_archive_links_rejected(tmp_path, kind):
    path = tmp_path / ('unsafe.' + kind)
    if kind == 'zip':
        with zipfile.ZipFile(path, 'w') as archive:
            info = zipfile.ZipInfo('link')
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            archive.writestr(info, '../outside')
    else:
        with tarfile.open(path, 'w') as archive:
            tar_info = tarfile.TarInfo('link')
            tar_info.type = tarfile.SYMTYPE
            tar_info.linkname = '../outside'
            archive.addfile(tar_info)
    with pytest.raises(ValueError, match='link/special'):
        extract(path, tmp_path / 'out')


def test_tar_rerun_and_existing_symlink(tmp_path):
    path = tmp_path / 'good.tar.gz'
    with tarfile.open(path, 'w:gz') as archive:
        info = tarfile.TarInfo('nested/file')
        info.size = 3
        archive.addfile(info, io.BytesIO(b'yes'))
    extract(path, tmp_path / 'out')
    extract(path, tmp_path / 'out')
    assert (tmp_path / 'out/nested/file').read_bytes() == b'yes'
    (tmp_path / 'out/nested/file').unlink()
    (tmp_path / 'out/nested/file').symlink_to(tmp_path / 'outside')
    with pytest.raises(ValueError, match='Symlink'):
        extract(path, tmp_path / 'out')
    assert not (tmp_path / 'outside').exists()


def test_list_and_manual_datasets_need_no_network(tmp_path, capsys):
    assert main(['--list']) == 0
    assert 'CC BY' in capsys.readouterr().out
    assert main(['--datasets', 'bdd100k,tusimple', '--data-root', str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert 'free account' in output and 'Local evaluation only' in output


def test_published_checksum_on_redirect_is_verified(server, tmp_path):
    url, _, _ = server
    redirected = url.rsplit('/', 1)[0] + '/redirect'
    with pytest.raises(ValueError, match='checksum mismatch'):
        download(redirected, tmp_path / 'data.zip')
    assert not (tmp_path / 'data.zip').exists()


def test_symlink_root_and_lock_cleanup(server, tmp_path, monkeypatch):
    url, _, payload = server
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setitem(SOURCES, 'coco', [Source(url, 'data.zip')])
    assert main(['--datasets', 'coco', '--data-root', str(link / 'data')]) == 0
    assert (real / 'data/data.zip').read_bytes() == payload
    assert not list(real.rglob('*.lock'))


def test_disk_refusal_before_get(server, tmp_path, monkeypatch, capsys):
    url, state, _ = server
    monkeypatch.setitem(SOURCES, 'coco', [Source(url, 'data.zip')])
    monkeypatch.setattr(shutil, 'disk_usage', lambda _: type('Usage', (), {'free': 1})())
    assert main(['--data-root', str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert not state['requests']
    assert 'free disk' in output.out.lower()
    assert 'space' in output.err.lower()
    assert 'rerun' not in output.err.lower()


def test_ctrl_c_is_prompt_and_resumable(server, tmp_path):
    url, state, payload = server
    state['slow'] = True
    target = tmp_path / 'data.zip'
    code = (
        'from pathlib import Path; '
        'from examples.download_demo_data import main, SOURCES; '
        'from examples._shared.catalog import Source; '
        f'SOURCES["coco"] = [Source({url!r}, "data.zip")]; '
        f'raise SystemExit(main(["--data-root", {str(tmp_path)!r}, "--connections", "2"]))'
    )
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    process = subprocess.Popen(
        [sys.executable, '-u', '-c', code],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while len(state['requests']) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(state['requests']) == 2
        time.sleep(0.2)
        start = time.monotonic()
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=4)
        assert time.monotonic() - start < 4
        assert process.returncode == 130
        assert 'resume' in stderr.lower() and 'Traceback' not in stderr
        assert not target.exists()
        assert list(tmp_path.rglob('*.part'))
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    state['slow'] = False
    state['requests'].clear()
    download(url, target, connections=3)
    assert target.read_bytes() == payload
    assert all(start > 0 for start, _ in state['requests'])


def test_complete_download_works_offline(server, tmp_path):
    url, state, payload = server
    target = download(url, tmp_path / 'data.zip')
    state['offline'] = True
    assert download(url, target).read_bytes() == payload


def test_missing_length_has_actionable_error(server, tmp_path, monkeypatch, capsys):
    url, state, _ = server
    state['no_length'] = True
    monkeypatch.setitem(SOURCES, 'coco', [Source(url, 'data.zip')])
    assert main(['--data-root', str(tmp_path)]) == 1
    message = capsys.readouterr().err
    assert 'Content-Length' in message
    assert 'rerun' not in message.lower()


def test_kitti_download_only():
    assert all(source.extract_to is None for source in SOURCES['kitti'])


def test_assembly_peak_and_resume(server, tmp_path, monkeypatch):
    url, _, payload = server
    original = shutil.copyfileobj
    calls = 0

    def copy(source, output, length):
        nonlocal calls
        original(source, output, length)
        output.flush()
        calls += 1
        held = sum(
            p.stat().st_size
            for p in tmp_path.rglob('*')
            if p.is_file() and (p.suffix == '.part' or p.name.startswith('.assemble-'))
        )
        assert held <= len(payload) + -(-len(payload) // 4)
        if calls == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(shutil, 'copyfileobj', copy)
    with pytest.raises(KeyboardInterrupt):
        download(url, tmp_path / 'data.zip', connections=4)
    assert not (tmp_path / 'data.zip').exists()
    assert not list(tmp_path.glob('.assemble-*'))
    monkeypatch.setattr(shutil, 'copyfileobj', original)
    assert download(url, tmp_path / 'data.zip').read_bytes() == payload


def test_extraction_disk_check_before_writes(server, tmp_path, monkeypatch):
    url, _, _ = server
    target = download(url, tmp_path / 'data.zip')
    monkeypatch.setattr(shutil, 'disk_usage', lambda _: type('Usage', (), {'free': 1})())
    with pytest.raises(ValueError, match='disk space'):
        extract(target, tmp_path / 'out')
    assert not (tmp_path / 'out/images/one.txt').exists()


def test_no_hardlink_filesystem(server, tmp_path, monkeypatch):
    import errno

    def unsupported(*_):
        raise OSError(errno.EOPNOTSUPP, 'fixture filesystem')

    monkeypatch.setattr(os, 'link', unsupported)
    url, _, payload = server
    target = download(url, tmp_path / 'data.zip')
    assert target.read_bytes() == payload
    extract(target, tmp_path / 'out')
    assert (tmp_path / 'out/images/one.txt').read_bytes() == b'example' * 30000


def test_one_connection_has_no_assembly_copy(server, tmp_path, monkeypatch):
    def copy(*_):
        raise AssertionError('A single range should become the final file without copying')

    monkeypatch.setattr(shutil, 'copyfileobj', copy)
    url, _, payload = server
    assert download(url, tmp_path / 'data.zip', connections=1).read_bytes() == payload
