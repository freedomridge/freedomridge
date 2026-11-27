"""Region boundaries come with the install: the boundary file's place, the load the job runner starts by itself, the
button, the fetch command, migration 0039, the worker image and the install steps.

Written from the contract, not from the code, and tested where a user or the job runner meets it: the job runner's
start-up call (backend.regions.queue_bundled_loads), a queued region load run as the runner runs it
(backend.regions.handler), the command line (backend.regions.main, `fetch`), the HTTP API signed in with real API
tokens, and the files Dockerfile.worker, THIRD_PARTY_NOTICES.md, .gitignore, docs/QUICK_START.md, docs/UPGRADING.md
and .github/workflows/images.yml. The rule, in short:

- A set's file is looked for in one directory, FRIDGE_REGIONS_DIR (read at each call) or else backend/boundaries/
  beside the backend's code, which git ignores, under the name the set's URL gives it. Only the set's pinned file
  (within its size limit, with its SHA-256) counts as present; anything else there is no file, and the runner's log
  says so once, naming it.
- When the job runner starts, the pinned file is present, the set has never been loaded (no region_sets row) and no
  region load is queued or running, it queues one region load (kind region_load), however often and however many
  runners at once ask; kept from the queue's lock by another runner, it gives up within about two seconds with the
  queue's contention error. A set that was loaded, from anything and in any version, is never loaded again by
  itself; a load that failed for good is queued again at the next start. The server downloads nothing by itself: an
  automatic load whose file has gone loads nothing.
- That load records loaded_from "bundled" and no user. The button (POST /api/admin/regions/load) reads the pinned
  file when it is there ("bundled", by the administrator) and downloads otherwise ("download", by the administrator).
- Every region load first asks the database volume's free-space gate: on a nearly full disk it writes nothing and the
  job fails with the gate's words (an automatic one is queued again at the next start); with room, or with the free
  space unknown, it loads.
- `python -m backend.regions fetch` writes each set's pinned file (`--set NAME`: that one) into the directory and
  says where; with the pinned file already there it downloads nothing; it replaces anything else; a download that
  isn't the pinned file, or that fails, leaves the directory as it was (a file there before untouched), prints the
  error and exits 1. It needs no database. The Quick Start and a development checkout's upgrade steps run it, saying
  it is a 41 MB download.
- Under this suite the directory is an empty one of its own, so nothing loads by itself.
- Migration 0039 lets loaded_from be "bundled" and changes nothing else; an upgraded database equals schema.sql's.
- Dockerfile.worker fetches the file while building, checked with the URL and SHA-256 of backend.regions.SETS (or
  with the module's own fetch), into the place above, and its comments say building it needs BuildKit. The image
  workflow's try-it smoke test checks region_sets for the bundled load. THIRD_PARTY_NOTICES.md names the data, its
  source, version and licence.

The boundary file is test_regions_spec's synthetic FEATURES. A test pins it by replacing SETS["natural-earth-admin1"]
with a copy whose sha256 is the file's own (contract 7.2), and replaces `download` wherever it could be reached: to
supply bytes, to fail, or to fail the test if it is called. Nothing here reaches the network. Each database test has a
database of its own made from this checkout's schema.sql (test_regions_spec's world), and its own regions directory.

Where the contract leaves something open, these tests read it this way:
- queue_bundled_loads's answer is checked by its length only (one job, or an empty list); the job itself is read from
  the jobs table. "Queued or running" covers a load the button queued, one the call itself queued, and one a runner
  has claimed. A queued load is run by claiming it with job_queue.claim_job, as a runner does, and calling
  handler(pool)(job) with the claimed row, publishing each step with job_queue.publish_step until the job ends. Where
  how a job ended matters (the free-space tests), a job runner's worker (job_runner.ServerWorker) runs it instead.
- The queue's lock is the advisory lock start_load already schedules region loads under ('schedule:region_load').
  Held by another connection, queue_bundled_loads must raise job_queue.QueueContended in well under ten seconds and
  queue nothing. Among runners starting at once, one that gives up that way is allowed: exactly one load is queued.
- The free-space gate is backend.diskspace's, reading db_free_bytes (7.7): a few MB free refuses a load, 1 TB or None
  (unknown) lets it through. A refused job is "failed", and its error's message contains the gate's opening words "Not
  enough free disk on the database volume". For a load the administrator starts with no file there, whether it
  downloads before it is refused isn't checked. Every other test reports 1 TB free, so the test database's real disk
  never decides a test.
- The log "says so once, naming the file": one start (one call) gives exactly one log record at INFO or above that
  contains the file's name; with no such record, exactly one printed line that does.
- "The right size limit": a file with the pinned SHA-256 but over the set's max_bytes (lowered for the test) is not
  the pinned file, for the runner and for fetch alike.
- An automatic load whose file has gone, or has been replaced by another file, may fail or end without loading;
  either way it downloads nothing and loads nothing.
- loaded_by keeps its shape ({"id", "username"}, docs/REGIONS.md); "the administrator" is checked by its id.
- A failed download is `download` raising the module's RegionLoadError, which the existing download raises for every
  network failure (section 7 names no exception). "Prints where each file is": the file's path appears in what fetch
  prints, on either stream. "Says so" (already there) and "prints the error" are checked as output, not by wording,
  except that a failed download's output mentions Settings.
- fetch makes the directory when it isn't there: backend/boundaries/ isn't in a fresh checkout.
- With one known set, `--set natural-earth-admin1` can't be told apart from fetching every set; `--set` with a name
  that isn't a set downloads nothing and doesn't exit 0.
- The default place is checked by fetching with FRIDGE_REGIONS_DIR unset and the working directory elsewhere. A file
  already there (a developer's fetched one) is never replaced: the test then pins that file and checks fetch finds it
  without downloading. A file the test writes there is removed afterwards.
- 3.6 is checked as: FRIDGE_REGIONS_DIR is set, names an existing empty directory, and that is not backend/boundaries/.
- Migration 0039 is applied on its own to a database made from the last release before it (v2.13.2) with the
  migrations before it applied and a region_sets row: every table keeps its columns and rows, and the only schema
  lines it changes mention loaded_from. loaded_from then accepts download, file and bundled and refuses any other text
  (case included); the rest of the upgrade gives schema.sql's schema, which accepts the same.
- Dockerfile.worker is read as text: either an ADD or RUN instruction names SETS's URL and SHA-256 (as sha256:<hex>
  or the bare hex a check reads) after ARG and ENV values are put in, and names the place (as an absolute path or
  relative to its WORKDIR); or a RUN runs `-m backend.regions fetch` after the backend's code is copied in. The place
  is the image's ENV FRIDGE_REGIONS_DIR, else boundaries/ in the directory the backend is copied to. Neither
  instruction swallows a failure with `||`, so a download that isn't the pinned file fails the build.
- THIRD_PARTY_NOTICES.md: one section names Natural Earth, the set's version, the public domain, and a source (the
  set's URL, naturalearthdata.com, or Natural Earth's vector repository).
- 2.5 and 2.6 are read as text only: a comment line of Dockerfile.worker names BuildKit; the try-it job of
  .github/workflows/images.yml mentions region_sets and bundled.

Not tested here: that the job runner calls queue_bundled_loads each time it starts (that would mean running the
runner's loop), that the load is retried by its policy, that the image builds (that downloads the real file), and
what the docs say about building images (2.5), restarting the backend after a fetch (5.2) and the development
Compose file's jobs profile (8.4), and the smoke test's ten-minute wait (2.6).
"""

import asyncio
import contextlib
import dataclasses
import hashlib
import importlib
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import asyncpg
import pytest

from backend import database as database_module
from backend import diskspace, job_queue
from backend.database import init_connection
from backend.job_heartbeat import heartbeat_connector
from backend.job_runner import ServerWorker
from backend.tests import test_regions_spec as spec
from backend.tests import test_upgrade_paths
from backend.tests.test_access_rule_spec import ADMIN
from backend.tests.test_upgrade_paths import ROOT

database = test_upgrade_paths.database
regions_world = spec.regions_world
# The module under test, read as Any: these tests stand on the names of the contract's section 7, some of which a
# checkout without the feature doesn't have.
regions: Any = importlib.import_module('backend.regions')

needs_database = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN') or not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
    reason='Requires disposable PostgreSQL',
)

SET = spec.SET
KIND = 'region_load'
FILE_NAME = 'ne_10m_admin_1_states_provinces.geojson'
DEFAULT_PLACE = ROOT / 'backend' / 'boundaries'
OLDER = ('XA-BK',)  # left out of FEATURES: another boundary file, as an older version would be
# Where the backend's database would be: a local port nothing listens on (as conftest's unit-only URL).
NO_DATABASE = 'postgres://fridge-regions-tests:unused@127.0.0.1:1/no_database'
RUNNER = 'regions-bundled-spec'
NEARLY_FULL = 5 * 1024**2  # bytes free on the database volume: a few MB
PLENTY = 1024**4
GATE = 'Not enough free disk on the database volume'  # how the free-space gate's refusal starts


def free_disk(monkeypatch: Any, free: Optional[int]) -> None:
    """backend.diskspace.db_free_bytes replaced to report `free` bytes on the database volume, None: unknown (7.7)."""

    async def db_free_bytes(connection: Any) -> Optional[int]:
        return free

    monkeypatch.setattr(diskspace, 'db_free_bytes', db_free_bytes)


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch):
    """Every test's database volume has room for a load unless the test says otherwise: the gate reads the test
    database's real disk, which no test here is about (other writers' tests stub the gate for the same reason)."""
    free_disk(monkeypatch, PLENTY)


# --- The boundary file, its place and `download` ---


def synthetic(folder: Path, leave_out: tuple[str, ...] = ()) -> bytes:
    """test_regions_spec's boundary file (FEATURES less `leave_out`), written outside any regions directory."""
    sources = folder / 'sources'
    sources.mkdir(exist_ok=True)
    path = sources / f'boundaries-{len(list(sources.iterdir()))}.geojson'
    return spec.boundary_file(path, leave_out).read_bytes()


def pin(monkeypatch: Any, data: bytes, **changes: Any) -> Any:
    """SETS' entry for the set replaced by a copy whose pinned file is `data` (contract 7.2), with any other changes."""
    pinned = dataclasses.replace(regions.SETS[SET], sha256=hashlib.sha256(data).hexdigest(), **changes)
    monkeypatch.setitem(regions.SETS, SET, pinned)
    return pinned


def regions_dir(monkeypatch: Any, folder: Path, make: bool = True) -> Path:
    """FRIDGE_REGIONS_DIR set to a directory of the test's own, made unless `make` is false."""
    directory = folder / 'regions-dir'
    if make:
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('FRIDGE_REGIONS_DIR', str(directory))
    return directory


def put(directory: Path, data: bytes) -> Path:
    """`data` as the set's file in `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / FILE_NAME
    path.write_bytes(data)
    return path


def supply(monkeypatch: Any, data: bytes) -> list:
    """`download` replaced by one that gives `data`; the list holds the set it was called for, each time."""
    calls: list = []

    async def download(boundary_set: Any) -> bytes:
        calls.append(boundary_set)
        return data

    monkeypatch.setattr(regions, 'download', download)
    return calls


def forbid_download(monkeypatch: Any) -> list:
    """`download` replaced by one that fails the test when it is called."""
    calls: list = []

    async def download(boundary_set: Any) -> bytes:
        calls.append(boundary_set)
        pytest.fail(f'download was called for {boundary_set.name}: nothing may be downloaded here')

    monkeypatch.setattr(regions, 'download', download)
    return calls


def fail_download(monkeypatch: Any) -> list:
    """`download` replaced by one that fails as the network does."""
    calls: list = []

    async def download(boundary_set: Any) -> bytes:
        calls.append(boundary_set)
        raise regions.RegionLoadError(f'Could not download {boundary_set.url}: All connection attempts failed')

    monkeypatch.setattr(regions, 'download', download)
    return calls


def exit_code(argv: list[str]) -> int:
    """The exit code of `python -m backend.regions <argv>`: main's answer, or the one argparse exits with."""
    try:
        return regions.main(argv)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1


def output(capfd: Any) -> str:
    captured = capfd.readouterr()
    return captured.out + captured.err


def says_where(text: str, path: Path) -> bool:
    return str(path) in text or str(path.resolve()) in text


def files_in(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob('*') if path.is_file())


# --- fetch (no database) ---


def test_fetch_writes_the_pinned_file_into_the_directory_and_says_where(monkeypatch, tmp_path, capfd):
    assert Path(urlparse(regions.SETS[SET].url).path).name == FILE_NAME  # the name the set's URL gives it
    data = synthetic(tmp_path)
    pinned = pin(monkeypatch, data)
    directory = regions_dir(monkeypatch, tmp_path, make=False)  # not there yet, as in a fresh checkout
    calls = supply(monkeypatch, data)
    assert exit_code(['fetch']) == 0
    target = directory / FILE_NAME
    assert target.read_bytes() == data
    assert files_in(directory) == [target]
    assert calls == [pinned]
    assert says_where(output(capfd), target)


def test_a_second_fetch_finds_the_pinned_file_and_downloads_nothing(monkeypatch, tmp_path, capfd):
    data = synthetic(tmp_path)
    pin(monkeypatch, data)
    directory = regions_dir(monkeypatch, tmp_path)
    supply(monkeypatch, data)
    assert exit_code(['fetch']) == 0
    capfd.readouterr()
    calls = forbid_download(monkeypatch)
    assert exit_code(['fetch']) == 0
    assert calls == []
    target = directory / FILE_NAME
    assert target.read_bytes() == data
    assert says_where(output(capfd), target)


def test_fetch_replaces_a_file_that_is_not_the_pinned_one(monkeypatch, tmp_path):
    data = synthetic(tmp_path)
    pinned = pin(monkeypatch, data)
    directory = regions_dir(monkeypatch, tmp_path)
    put(directory, synthetic(tmp_path, OLDER))
    calls = supply(monkeypatch, data)
    assert exit_code(['fetch']) == 0
    assert calls == [pinned]
    assert (directory / FILE_NAME).read_bytes() == data


def contents(folder: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in files_in(folder)}


def file_there_before(directory: Path, folder: Path, there: str) -> None:
    """With `there` saying so, a file that is neither the pinned one nor the download, already in `directory`."""
    if there == 'another file there':
        put(directory, synthetic(folder, ('XB-NO',)))


@pytest.mark.parametrize('there', ['nothing there', 'another file there'])
@pytest.mark.parametrize('wrong', ['another file', 'over the size limit'])
def test_a_download_that_is_not_the_pinned_file_leaves_the_directory_as_it_was_and_exits_1(
    monkeypatch,
    tmp_path,
    capfd,
    wrong,
    there,
):
    data = synthetic(tmp_path)
    if wrong == 'another file':
        pin(monkeypatch, data)
        downloaded = synthetic(tmp_path, OLDER)
    else:  # the pinned SHA-256, over the set's size limit
        pin(monkeypatch, data, max_bytes=len(data) - 1)
        downloaded = data
    file_there_before(regions_dir(monkeypatch, tmp_path), tmp_path, there)
    calls = supply(monkeypatch, downloaded)
    before = contents(tmp_path)
    assert exit_code(['fetch']) == 1
    assert len(calls) == 1
    assert contents(tmp_path) == before  # nothing partial, nothing unverified, a file that was there untouched
    assert output(capfd).strip(), 'fetch prints the error'


@pytest.mark.parametrize('there', ['nothing there', 'another file there'])
def test_a_failed_download_leaves_the_directory_as_it_was_says_settings_can_load_it_later_and_exits_1(
    monkeypatch,
    tmp_path,
    capfd,
    there,
):
    pin(monkeypatch, synthetic(tmp_path))
    file_there_before(regions_dir(monkeypatch, tmp_path), tmp_path, there)
    calls = fail_download(monkeypatch)
    before = contents(tmp_path)
    assert exit_code(['fetch']) == 1
    assert len(calls) == 1
    assert contents(tmp_path) == before
    printed = output(capfd)
    assert 'Settings' in printed, printed


def test_fetch_set_fetches_the_set_it_names(monkeypatch, tmp_path):
    data = synthetic(tmp_path)
    pinned = pin(monkeypatch, data)
    directory = regions_dir(monkeypatch, tmp_path)
    calls = supply(monkeypatch, data)
    assert exit_code(['fetch', '--set', SET]) == 0
    assert calls == [pinned]
    assert (directory / FILE_NAME).read_bytes() == data
    (directory / FILE_NAME).unlink()
    calls.clear()
    assert exit_code(['fetch', '--set', 'world-rivers']) != 0
    assert calls == []
    assert files_in(directory) == []


def test_fetch_needs_no_database(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', NO_DATABASE)
    monkeypatch.setattr(database_module, 'DATABASE_URL', NO_DATABASE)
    if hasattr(regions, 'DATABASE_URL'):
        monkeypatch.setattr(regions, 'DATABASE_URL', NO_DATABASE)
    data = synthetic(tmp_path)
    pin(monkeypatch, data)
    directory = regions_dir(monkeypatch, tmp_path)
    supply(monkeypatch, data)
    assert exit_code(['fetch']) == 0
    assert (directory / FILE_NAME).read_bytes() == data


# --- The place ---


def test_the_directory_is_read_when_it_is_needed_not_when_the_module_is_imported(monkeypatch, tmp_path):
    data = synthetic(tmp_path)
    pin(monkeypatch, data)
    calls = supply(monkeypatch, data)
    first, second = tmp_path / 'first', tmp_path / 'second'
    monkeypatch.setenv('FRIDGE_REGIONS_DIR', str(first))
    assert exit_code(['fetch']) == 0
    monkeypatch.setenv('FRIDGE_REGIONS_DIR', str(second))
    assert exit_code(['fetch']) == 0
    assert (first / FILE_NAME).read_bytes() == data
    assert (second / FILE_NAME).read_bytes() == data
    assert len(calls) == 2


def test_without_the_variable_the_place_is_backend_boundaries_beside_the_code(monkeypatch, tmp_path, capfd):
    monkeypatch.delenv('FRIDGE_REGIONS_DIR', raising=False)
    monkeypatch.chdir(tmp_path)  # beside the code, not in the working directory
    target = DEFAULT_PLACE / FILE_NAME
    if target.exists():
        # A file someone fetched: never replaced. Pinned as it is, fetch finds it there and downloads nothing.
        data = target.read_bytes()
        pin(monkeypatch, data)
        calls = forbid_download(monkeypatch)
        assert exit_code(['fetch']) == 0
        assert calls == []
        assert target.read_bytes() == data
    else:
        data = synthetic(tmp_path)
        pin(monkeypatch, data)
        calls = supply(monkeypatch, data)
        made = not DEFAULT_PLACE.exists()
        try:
            assert exit_code(['fetch']) == 0
            assert len(calls) == 1
            assert target.read_bytes() == data
            assert all(path.parent.name == 'sources' for path in files_in(tmp_path)), files_in(tmp_path)
        finally:
            if target.exists():
                target.unlink()
            if made and DEFAULT_PLACE.is_dir() and not any(DEFAULT_PLACE.iterdir()):
                DEFAULT_PLACE.rmdir()
    assert says_where(output(capfd), target)


def test_git_ignores_backend_boundaries_and_the_repository_holds_no_boundary_file():
    path = f'backend/boundaries/{FILE_NAME}'
    ignored = subprocess.run(['git', '-C', str(ROOT), 'check-ignore', '-q', path], capture_output=True, text=True)
    assert ignored.returncode == 0, f'{path} is not ignored by git: {ignored.stderr}'
    tracked = subprocess.run(
        ['git', '-C', str(ROOT), 'ls-files', 'backend/boundaries'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert tracked.stdout == ''


def test_under_the_test_suite_the_directory_is_an_empty_one_of_its_own():
    """Nothing set here: what every test sees unless it puts a file there itself (contract 3.6)."""
    place = os.environ.get('FRIDGE_REGIONS_DIR')
    assert place, 'the test suite sets FRIDGE_REGIONS_DIR'
    directory = Path(place)
    assert directory.is_dir(), place
    assert list(directory.iterdir()) == [], place
    assert directory.resolve() != DEFAULT_PLACE.resolve()


# --- The job runner's start, and the load it queues ---


async def loads_waiting(r: Any) -> list:
    """The region loads queued or running, oldest first."""
    return await r.database.fetch(
        "SELECT id, state, created_by FROM jobs WHERE kind = $1 AND state IN ('queued', 'running') ORDER BY created_at",
        KIND,
    )


async def claim(r: Any) -> dict:
    job = await job_queue.claim_job(r.database.pool, RUNNER, kinds=[KIND], lease_seconds=600)
    assert job is not None, 'no region load is queued'
    return job


async def run_job(r: Any) -> dict:
    """The queued region load claimed and run as the job runner runs it, each step published, until it ends."""
    pool = r.database.pool
    job = await claim(r)
    for _ in range(100):
        step = await regions.handler(pool)(job)
        if step.committed:
            return dict(await pool.fetchrow('SELECT * FROM jobs WHERE id = $1', job['id']))
        job = await job_queue.publish_step(
            pool,
            job['id'],
            job['lease_token'],
            checkpoint=step.checkpoint,
            done=step.done,
            total=step.total,
            message=step.message,
            result=step.result,
            finished=step.finished,
            publisher=step.publish,
        )
        if job['state'] != 'running':
            return job
    pytest.fail('the region load did not end')


async def run_in_a_worker(r: Any) -> dict:
    """The queued region load run by a job runner's worker (job_runner.ServerWorker, which records how a job ended)
    until it ends, or is put back to be tried again."""
    [waiting] = await loads_waiting(r)
    pool = r.database.pool
    worker = ServerWorker(pool, {KIND: regions.handler(pool)}, heartbeat_connect=heartbeat_connector(r.url))
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        await worker.run_once()
        job = await pool.fetchrow('SELECT state, error, failures, result FROM jobs WHERE id = $1', waiting['id'])
        if job['state'] in ('succeeded', 'failed', 'cancelled') or job['failures']:
            return dict(job)
    pytest.fail('the region load did not end within 120 seconds')


async def nothing_written(r: Any) -> None:
    assert await r.database.fetchval('SELECT count(*) FROM region_sets') == 0
    assert await r.database.fetchval('SELECT count(*) FROM regions') == 0
    assert (await spec.admin_sets(r))['loaded'] is None


def refused_for_the_disk(job: dict) -> None:
    """The job failed for good with the free-space gate's own words."""
    assert job['state'] == 'failed', job
    assert isinstance(job['error'], dict) and GATE in job['error'].get('message', ''), job


async def press(r: Any) -> dict:
    """The button: POST /api/admin/regions/load as the administrator."""
    response = await spec.post(r, '/api/admin/regions/load', {'set': SET})
    assert response.status_code == 202, response.text
    return response.json()


def point_loader(r: Any) -> None:
    """The loader's own DATABASE_URL, if it keeps one, at the test's database (as test_regions_spec does)."""
    if hasattr(regions, 'DATABASE_URL'):
        r.monkeypatch.setattr(regions, 'DATABASE_URL', r.url)


@needs_database
def test_the_runner_start_queues_one_load_when_the_pinned_file_is_there_and_the_set_was_never_loaded(regions_world):
    async def scenario(r):
        data = synthetic(r.folder)
        pin(r.monkeypatch, data)
        put(regions_dir(r.monkeypatch, r.folder), data)
        forbid_download(r.monkeypatch)
        queued = await regions.queue_bundled_loads(r.database.pool)
        assert isinstance(queued, list) and len(queued) == 1, queued
        [job] = await loads_waiting(r)
        assert job['state'] == 'queued'
        # Followed like any other load: the listing names it, Background jobs list it, the button joins it.
        state = await spec.admin_sets(r)
        assert state['loaded'] is None
        assert state['load_job'] is not None, state
        assert (str(state['load_job']['job_id']), state['load_job']['state']) == (str(job['id']), 'queued')
        listed = await r.call(ADMIN, 'GET', '/api/admin/jobs')
        assert listed.status_code == 200, listed.text
        assert [item['kind'] for item in listed.json()['items'] if str(item['id']) == str(job['id'])] == [KIND]
        pressed = await spec.post(r, '/api/admin/regions/load', {'set': SET})
        assert pressed.is_success, pressed.text
        assert (pressed.json()['started'], str(pressed.json()['job_id'])) == (False, str(job['id']))
        assert len(await loads_waiting(r)) == 1

    regions_world(scenario)


@needs_database
def test_the_automatic_load_reads_the_file_and_records_bundled_with_no_user(regions_world):
    async def scenario(r):
        point_loader(r)
        data = synthetic(r.folder)
        pinned = pin(r.monkeypatch, data)
        put(regions_dir(r.monkeypatch, r.folder), data)
        calls = forbid_download(r.monkeypatch)
        assert len(await regions.queue_bundled_loads(r.database.pool)) == 1
        ended = await run_job(r)
        assert ended['state'] == 'succeeded', ended
        assert ended['result'] is not None, ended  # the load's report
        assert calls == []
        state = await spec.admin_sets(r)
        info = state['loaded']
        assert isinstance(info, dict), state
        assert (info['loaded_from'], info['loaded_by']) == ('bundled', None), info
        assert (info['version'], info['verified'], info['sha256']) == (pinned.version, True, pinned.sha256), info
        assert (info['counts'], info['removed_keys']) == (spec.COUNTS, []), info
        assert state['load_job'] is None, state
        assert (await spec.region(r, 'XA-NM')).status_code == 200
        # Loaded: the next start queues nothing.
        assert await regions.queue_bundled_loads(r.database.pool) == []
        assert await loads_waiting(r) == []
        # GET /api/admin/regions keeps its shape: the same keys as after a load from the command line.
        assert await spec.load(r, 'v2') == 0
        again = await spec.admin_sets(r)
        assert again['loaded']['loaded_from'] == 'file'
        assert (set(again), set(again['loaded'])) == (set(state), set(info))

    regions_world(scenario)


@needs_database
@pytest.mark.parametrize('made', [True, False], ids=['an empty directory', 'no directory'])
def test_with_no_file_the_runner_start_queues_nothing(regions_world, made):
    async def scenario(r):
        pin(r.monkeypatch, synthetic(r.folder))
        regions_dir(r.monkeypatch, r.folder, make=made)
        forbid_download(r.monkeypatch)
        assert await regions.queue_bundled_loads(r.database.pool) == []
        assert await loads_waiting(r) == []
        assert (await spec.admin_sets(r))['loaded'] is None

    regions_world(scenario)


@needs_database
@pytest.mark.parametrize('wrong', ['another file', 'over the size limit'])
def test_a_file_that_is_not_the_pinned_one_is_no_file_and_the_log_names_it_once(regions_world, caplog, capfd, wrong):
    async def scenario(r):
        data = synthetic(r.folder)
        directory = regions_dir(r.monkeypatch, r.folder)
        if wrong == 'another file':
            pin(r.monkeypatch, data)
            put(directory, synthetic(r.folder, OLDER))
        else:  # the pinned SHA-256, over the set's size limit
            pin(r.monkeypatch, data, max_bytes=len(data) - 1)
            put(directory, data)
        forbid_download(r.monkeypatch)
        caplog.set_level(logging.DEBUG)
        caplog.clear()
        capfd.readouterr()
        assert await regions.queue_bundled_loads(r.database.pool) == []
        assert await loads_waiting(r) == []
        said = [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.INFO and FILE_NAME in record.getMessage()
        ]
        if not said:
            said = [line for line in output(capfd).splitlines() if FILE_NAME in line]
        assert len(said) == 1, said

    regions_world(scenario)


@needs_database
@pytest.mark.parametrize('source', ['file', 'download', 'bundled'])
def test_a_set_loaded_from_anything_in_any_version_is_never_loaded_again_by_itself(regions_world, source):
    async def scenario(r):
        point_loader(r)
        directory = regions_dir(r.monkeypatch, r.folder)
        older = synthetic(r.folder, OLDER)
        if source == 'file':  # the command line, a file an administrator vouched for
            assert await spec.load(r, 'v0', leave_out=OLDER) == 0
        elif source == 'download':  # the button, with no file there
            pin(r.monkeypatch, older)
            supply(r.monkeypatch, older)
            await press(r)
            assert (await run_job(r))['state'] == 'succeeded'
        else:  # by itself, from an older release's file
            pin(r.monkeypatch, older, version='v0')
            put(directory, older)
            forbid_download(r.monkeypatch)
            assert len(await regions.queue_bundled_loads(r.database.pool)) == 1
            assert (await run_job(r))['state'] == 'succeeded'
        before = (await spec.admin_sets(r))['loaded']
        assert before is not None and before['loaded_from'] == source, before
        # This release's pinned file, now present.
        data = synthetic(r.folder)
        pin(r.monkeypatch, data)
        put(directory, data)
        forbid_download(r.monkeypatch)
        assert await regions.queue_bundled_loads(r.database.pool) == []
        assert await loads_waiting(r) == []
        assert (await spec.admin_sets(r))['loaded'] == before

    regions_world(scenario)


@needs_database
@pytest.mark.parametrize('state', ['queued', 'running'])
def test_with_a_load_queued_or_running_the_runner_start_queues_nothing(regions_world, state):
    async def scenario(r):
        data = synthetic(r.folder)
        pin(r.monkeypatch, data)
        put(regions_dir(r.monkeypatch, r.folder), data)
        forbid_download(r.monkeypatch)
        await press(r)
        if state == 'running':
            await claim(r)
        assert await regions.queue_bundled_loads(r.database.pool) == []
        assert [job['state'] for job in await loads_waiting(r)] == [state]

    regions_world(scenario)


@needs_database
def test_asked_twice_the_runner_start_queues_one_load(regions_world):
    async def scenario(r):
        data = synthetic(r.folder)
        pin(r.monkeypatch, data)
        put(regions_dir(r.monkeypatch, r.folder), data)
        forbid_download(r.monkeypatch)
        first = await regions.queue_bundled_loads(r.database.pool)
        second = await regions.queue_bundled_loads(r.database.pool)
        assert (len(first), second) == (1, [])
        assert len(await loads_waiting(r)) == 1

    regions_world(scenario)


@needs_database
def test_runners_starting_at_once_queue_one_load(regions_world):
    async def scenario(r):
        data = synthetic(r.folder)
        pin(r.monkeypatch, data)
        put(regions_dir(r.monkeypatch, r.folder), data)
        forbid_download(r.monkeypatch)
        other = await asyncpg.create_pool(r.url, min_size=2, max_size=2, init=init_connection)
        try:
            pools = [r.database.pool, other, r.database.pool, other]
            calls = (regions.queue_bundled_loads(pool) for pool in pools)
            together = await asyncio.gather(*calls, return_exceptions=True)
        finally:
            await other.close()
        # A runner that waited too long for the queue's lock gives up with the queue's contention error (3.1).
        for answer in together:
            if isinstance(answer, BaseException) and not isinstance(answer, job_queue.QueueContended):
                raise answer
        answers = [answer for answer in together if isinstance(answer, list)]
        assert sum(len(queued) for queued in answers) == 1, together
        assert len(await loads_waiting(r)) == 1

    regions_world(scenario)


# The lock the queue's region loads are scheduled under (start_load's, in backend/regions.py).
QUEUE_LOCK = f"SELECT pg_advisory_lock(hashtextextended('schedule:{KIND}', 0))"
QUEUE_UNLOCK = f"SELECT pg_advisory_unlock(hashtextextended('schedule:{KIND}', 0))"


@needs_database
def test_a_runner_start_that_cannot_get_the_queues_lock_gives_up_with_the_queues_contention_error(regions_world):
    async def scenario(r):
        data = synthetic(r.folder)
        pin(r.monkeypatch, data)
        put(regions_dir(r.monkeypatch, r.folder), data)
        forbid_download(r.monkeypatch)
        holder = await asyncpg.connect(r.url)  # another runner, holding the lock for longer than the wait
        try:
            await holder.execute(QUEUE_LOCK)
            started = time.monotonic()
            with pytest.raises(job_queue.QueueContended):
                await asyncio.wait_for(regions.queue_bundled_loads(r.database.pool), timeout=30)
            waited = time.monotonic() - started
            assert waited < 10, f'gave up after {waited:.1f} s'
            assert await loads_waiting(r) == []
            await holder.execute(QUEUE_UNLOCK)
        finally:
            await holder.close()
        assert len(await regions.queue_bundled_loads(r.database.pool)) == 1
        assert len(await loads_waiting(r)) == 1

    regions_world(scenario)


@needs_database
def test_a_load_that_failed_for_good_is_queued_again_at_the_next_start(regions_world):
    async def scenario(r):
        data = synthetic(r.folder)
        pin(r.monkeypatch, data)
        put(regions_dir(r.monkeypatch, r.folder), data)
        forbid_download(r.monkeypatch)
        assert len(await regions.queue_bundled_loads(r.database.pool)) == 1
        job = await claim(r)
        error = {'type': 'RegionLoadError', 'message': 'the load failed'}
        failed = await job_queue.fail_job(r.database.pool, job['id'], job['lease_token'], error)
        assert failed['state'] == 'failed', failed
        assert await loads_waiting(r) == []
        assert len(await regions.queue_bundled_loads(r.database.pool)) == 1
        assert [job['state'] for job in await loads_waiting(r)] == ['queued']

    regions_world(scenario)


@needs_database
@pytest.mark.parametrize('change', ['removed', 'replaced by another file'])
def test_an_automatic_load_whose_file_has_gone_downloads_nothing_and_loads_nothing(regions_world, change):
    async def scenario(r):
        point_loader(r)
        data = synthetic(r.folder)
        pin(r.monkeypatch, data)
        path = put(regions_dir(r.monkeypatch, r.folder), data)
        assert len(await regions.queue_bundled_loads(r.database.pool)) == 1
        if change == 'removed':
            path.unlink()
        else:
            path.write_bytes(synthetic(r.folder, OLDER))
        calls = forbid_download(r.monkeypatch)
        with contextlib.suppress(Exception):  # the job may fail
            await run_job(r)
        assert calls == []
        assert (await spec.admin_sets(r))['loaded'] is None

    regions_world(scenario)


# --- The button ---


@needs_database
def test_the_button_reads_the_pinned_file_when_it_is_there_and_records_bundled_by_the_admin(regions_world):
    async def scenario(r):
        point_loader(r)
        data = synthetic(r.folder)
        pinned = pin(r.monkeypatch, data)
        put(regions_dir(r.monkeypatch, r.folder), data)
        calls = forbid_download(r.monkeypatch)
        await press(r)
        assert (await run_job(r))['state'] == 'succeeded'
        assert calls == []
        info = (await spec.admin_sets(r))['loaded']
        assert info['loaded_from'] == 'bundled', info
        assert isinstance(info['loaded_by'], dict) and info['loaded_by']['id'] == ADMIN, info
        assert (info['version'], info['verified'], info['sha256']) == (pinned.version, True, pinned.sha256), info
        assert info['counts'] == spec.COUNTS, info

    regions_world(scenario)


@needs_database
@pytest.mark.parametrize('there', ['no file', 'a file that is not the pinned one'])
def test_the_button_downloads_when_the_pinned_file_is_not_there_and_records_download(regions_world, there):
    async def scenario(r):
        point_loader(r)
        data = synthetic(r.folder)
        pinned = pin(r.monkeypatch, data)
        directory = regions_dir(r.monkeypatch, r.folder)
        if there != 'no file':
            put(directory, synthetic(r.folder, OLDER))
        calls = supply(r.monkeypatch, data)
        await press(r)
        assert (await run_job(r))['state'] == 'succeeded'
        assert calls == [pinned]
        info = (await spec.admin_sets(r))['loaded']
        assert info['loaded_from'] == 'download', info
        assert isinstance(info['loaded_by'], dict) and info['loaded_by']['id'] == ADMIN, info
        assert (info['verified'], info['sha256'], info['counts']) == (True, pinned.sha256, spec.COUNTS), info

    regions_world(scenario)


# --- The free-space check ---


@needs_database
def test_on_a_nearly_full_disk_the_automatic_load_writes_nothing_fails_naming_the_disk_and_is_queued_again(
    regions_world,
):
    async def scenario(r):
        point_loader(r)
        data = synthetic(r.folder)
        pin(r.monkeypatch, data)
        put(regions_dir(r.monkeypatch, r.folder), data)
        forbid_download(r.monkeypatch)
        free_disk(r.monkeypatch, NEARLY_FULL)
        assert len(await regions.queue_bundled_loads(r.database.pool)) == 1
        refused_for_the_disk(await run_in_a_worker(r))
        await nothing_written(r)
        # Failed for good: the next start queues it again, and with room it loads.
        assert len(await regions.queue_bundled_loads(r.database.pool)) == 1
        free_disk(r.monkeypatch, PLENTY)
        assert (await run_in_a_worker(r))['state'] == 'succeeded'
        assert (await spec.admin_sets(r))['loaded']['loaded_from'] == 'bundled'

    regions_world(scenario)


@needs_database
@pytest.mark.parametrize('there', ['the pinned file', 'no file'])
def test_on_a_nearly_full_disk_a_load_the_admin_starts_writes_nothing_and_fails_naming_the_disk(regions_world, there):
    async def scenario(r):
        point_loader(r)
        data = synthetic(r.folder)
        pin(r.monkeypatch, data)
        directory = regions_dir(r.monkeypatch, r.folder)
        if there == 'the pinned file':
            put(directory, data)
            forbid_download(r.monkeypatch)
        else:
            supply(r.monkeypatch, data)
        free_disk(r.monkeypatch, NEARLY_FULL)
        await press(r)
        refused_for_the_disk(await run_in_a_worker(r))
        await nothing_written(r)

    regions_world(scenario)


@needs_database
@pytest.mark.parametrize('started', ['by itself', 'by the admin'])
@pytest.mark.parametrize('free', [PLENTY, None], ids=['plenty of space', 'free space unknown'])
def test_with_room_or_free_space_unknown_the_load_goes_through(regions_world, free, started):
    async def scenario(r):
        point_loader(r)
        data = synthetic(r.folder)
        pin(r.monkeypatch, data)
        put(regions_dir(r.monkeypatch, r.folder), data)
        forbid_download(r.monkeypatch)
        free_disk(r.monkeypatch, free)
        if started == 'by itself':
            assert len(await regions.queue_bundled_loads(r.database.pool)) == 1
        else:
            await press(r)
        assert (await run_in_a_worker(r))['state'] == 'succeeded'
        info = (await spec.admin_sets(r))['loaded']
        assert (info['loaded_from'], info['counts']) == ('bundled', spec.COUNTS), info

    regions_world(scenario)


# --- A source checkout ---


@needs_database
def test_after_a_fetch_the_next_runner_start_loads_the_set(regions_world):
    async def scenario(r):
        point_loader(r)
        data = synthetic(r.folder)
        pin(r.monkeypatch, data)
        regions_dir(r.monkeypatch, r.folder, make=False)
        supply(r.monkeypatch, data)
        assert await asyncio.to_thread(exit_code, ['fetch']) == 0
        calls = forbid_download(r.monkeypatch)
        assert len(await regions.queue_bundled_loads(r.database.pool)) == 1
        assert (await run_job(r))['state'] == 'succeeded'
        assert calls == []
        info = (await spec.admin_sets(r))['loaded']
        assert (info['loaded_from'], info['loaded_by']) == ('bundled', None), info

    regions_world(scenario)


# --- The database ---

# What region_sets.loaded_from accepts in schema.sql.
LOADED_FROM = {'download': True, 'file': True, 'bundled': True, 'Bundled': False, 'elsewhere': False, '': False}


async def loaded_from_accepted(name: str) -> dict[str, bool]:
    """Which of LOADED_FROM's values a row of region_sets in the database `name` may hold."""
    url = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{name}').geturl()
    connection = await asyncpg.connect(url)
    insert = 'INSERT INTO region_sets (name, version, sha256, verified, loaded_from) VALUES ($1, $2, $3, $4, $5)'
    accepted = {}
    try:
        for value in LOADED_FROM:
            try:
                await connection.execute(insert, f'loaded-from-{value or "empty"}', '1', '0' * 64, True, value)
            except asyncpg.CheckViolationError:
                accepted[value] = False
            else:
                accepted[value] = True
    finally:
        await connection.close()
    return accepted


@needs_database
def test_loaded_from_takes_download_file_or_bundled_on_a_fresh_install(database):
    make, _, _, _ = database
    fresh = make((ROOT / 'backend/db/schema.sql').read_text())
    assert asyncio.run(loaded_from_accepted(fresh)) == LOADED_FROM


# --- The worker image, the notices and the install steps ---


def substitute(text: str, values: dict[str, str]) -> str:
    """`text` with each $NAME and ${NAME} of `values` put in."""
    return re.sub(
        r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)',
        lambda match: values.get(match.group(1) or match.group(2), match.group(0)),
        text,
    )


def dockerfile(path: Path) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
    """A Dockerfile's instructions as (keyword, the rest with ARG and ENV values put in, the WORKDIR it runs in),
    continuation lines joined and comments left out; and the ENV values the image runs with."""
    lines: list[str] = []
    current = ''
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.endswith('\\'):
            current += line[:-1].strip() + ' '
            continue
        lines.append(current + line)
        current = ''
    if current.strip():
        lines.append(current.strip())
    values: dict[str, str] = {}
    environment: dict[str, str] = {}
    workdir = '/'
    steps = []
    for line in lines:
        keyword, _, rest = line.partition(' ')
        keyword, rest = keyword.upper(), substitute(rest.strip(), values)
        if keyword in ('ARG', 'ENV'):
            pairs = re.findall(r'([A-Za-z_][A-Za-z0-9_]*)=("[^"]*"|\'[^\']*\'|\S*)', rest)
            if not pairs and keyword == 'ENV':  # the old form, ENV NAME value
                variable, _, value = rest.partition(' ')
                pairs = [(variable, value.strip())]
            for variable, value in pairs:
                values[variable] = value.strip('"\'')
                if keyword == 'ENV':
                    environment[variable] = values[variable]
        if keyword == 'WORKDIR':
            workdir = rest if rest.startswith('/') else f'{workdir.rstrip("/")}/{rest}'
        steps.append((keyword, rest, workdir))
    return steps, environment


def test_the_worker_image_fetches_the_pinned_file_into_its_place_and_checks_it_while_building():
    boundary_set = regions.SETS[SET]  # the real entry: nothing is replaced here
    steps, environment = dockerfile(ROOT / 'Dockerfile.worker')
    copies = [
        (index, rest, workdir)
        for index, (keyword, rest, workdir) in enumerate(steps)
        if keyword == 'COPY' and re.match(r'(?:--\S+\s+)*backend/?\s', rest)
    ]
    assert copies, 'Dockerfile.worker copies the backend in'
    copied, rest, workdir = copies[0]
    code = rest.split()[-1].rstrip('/')
    code = code if code.startswith('/') else f'{workdir.rstrip("/")}/{code}'
    place = environment.get('FRIDGE_REGIONS_DIR', f'{code}/boundaries').rstrip('/')
    fetches = [
        (index, rest)
        for index, (keyword, rest, _) in enumerate(steps)
        if keyword == 'RUN' and re.search(r'-m\s+backend\.regions\s+fetch\b', rest)
    ]
    downloads = [
        (rest, workdir) for keyword, rest, workdir in steps if keyword in ('ADD', 'RUN') and boundary_set.url in rest
    ]
    assert fetches or downloads, (
        f"Dockerfile.worker neither runs python -m backend.regions fetch nor names the set's URL {boundary_set.url}"
    )
    for index, rest in fetches:
        assert index > copied, 'the fetch runs after the backend code is copied in'
        inline = re.search(r'FRIDGE_REGIONS_DIR=(\S+)', rest)
        assert inline is None or inline.group(1).strip('"\'').rstrip('/') == place, (rest, place)
        assert '||' not in rest, f'a failed fetch must fail the build: {rest}'
    for rest, workdir in downloads:
        assert boundary_set.sha256 in rest, f'the download is checked against sha256:{boundary_set.sha256}: {rest}'
        base = workdir.rstrip('/') + '/'
        relative = place[len(base) :] if place.startswith(base) else None
        assert place in rest or (relative is not None and relative in rest), f'the file goes to {place}: {rest}'
        assert '||' not in rest, f'a download that is not the pinned file must fail the build: {rest}'


def test_third_party_notices_name_the_boundary_data_its_source_version_and_licence():
    boundary_set = regions.SETS[SET]
    text = (ROOT / 'THIRD_PARTY_NOTICES.md').read_text()
    named = [section for section in re.split(r'(?m)^#+ ', text) if 'Natural Earth' in section]
    assert named, 'THIRD_PARTY_NOTICES.md names Natural Earth'
    sources = (boundary_set.url, 'naturalearthdata.com', 'nvkelso/natural-earth-vector')
    assert any(
        boundary_set.version in section
        and 'public domain' in section.lower()
        and any(source in section for source in sources)
        for section in named
    ), named


def test_the_quick_start_and_the_development_checkout_steps_include_the_fetch():
    upgrading = (ROOT / 'docs/UPGRADING.md').read_text().split('## A development checkout', 1)
    assert len(upgrading) == 2, "docs/UPGRADING.md has a development checkout's steps"
    for name, text in (
        ('docs/QUICK_START.md', (ROOT / 'docs/QUICK_START.md').read_text()),
        ("docs/UPGRADING.md's development checkout", re.split(r'(?m)^## ', upgrading[1])[0]),
    ):
        assert re.search(r'-m backend\.regions fetch\b', text), f'{name} has no python -m backend.regions fetch'
        assert '41 MB' in text, f'{name} does not say the fetch is a 41 MB download'


def test_the_worker_dockerfile_says_building_it_needs_buildkit():
    comments = [line for line in (ROOT / 'Dockerfile.worker').read_text().splitlines() if line.lstrip().startswith('#')]
    assert any('BuildKit' in line for line in comments), 'no comment in Dockerfile.worker names BuildKit'


def test_the_image_workflows_try_it_smoke_test_waits_for_the_bundled_load():
    workflow = (ROOT / '.github/workflows/images.yml').read_text()
    jobs = re.split(r'(?m)^  (?=[A-Za-z0-9_-]+:\s*$)', workflow)
    [try_it] = [job for job in jobs if job.startswith('try-it:')]
    assert 'region_sets' in try_it and 'bundled' in try_it, 'the try-it job does not check region_sets for bundled'
