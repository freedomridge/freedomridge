"""The backend shards (scripts/backend_test_shards.py) run every test file exactly once, in balanced shards."""

from pathlib import Path

import pytest

from scripts.backend_test_shards import load_timings, parse_shard, split, timings_from_junit

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / 'backend' / 'tests').glob('test_*.py'))


@pytest.mark.parametrize('count', [1, 2, 3, 4, 6])
def test_every_file_is_in_exactly_one_shard(count):
    shards = split(FILES, count, load_timings())
    assert len(shards) == count
    assert sorted(name for shard in shards for name in shard) == FILES


def test_the_split_ignores_the_collection_order():
    timings = load_timings()
    assert split(FILES, 4, timings) == split(list(reversed(FILES)), 4, timings)


def test_the_shards_are_balanced():
    timings = load_timings()
    total = sum(timings.get(name, 0) for name in FILES)
    for shard in split(FILES, 4, timings):
        # Largest first into the lightest shard: no shard exceeds the average by more than one file.
        assert sum(timings.get(name, 0) for name in shard) <= total / 4 + max(timings.values())


def test_a_file_missing_from_the_timings_still_runs():
    files = ['backend/tests/test_a.py', 'backend/tests/test_new.py']
    shards = split(files, 2, {'backend/tests/test_a.py': 5.0})
    assert sorted(name for shard in shards for name in shard) == files


@pytest.mark.parametrize('spec', ['0/4', '5/4', '1/0', '2', 'a/b', ''])
def test_a_malformed_shard_is_refused(spec):
    with pytest.raises(ValueError):
        parse_shard(spec)


def test_a_shard_is_read_as_index_and_count():
    assert parse_shard('3/4') == (3, 4)


def test_the_timings_add_up_the_reports_of_every_shard(tmp_path):
    """A sharded run leaves one JUnit report for each shard: a file's seconds are its tests' in whichever report they
    are, and a module skipped as a whole (no classname) still counts as its file."""

    def report(name, cases):
        rows = ''.join(
            f'<testcase classname="{classname}" name="{test}" time="{seconds}"/>' for classname, test, seconds in cases
        )
        path = tmp_path / name
        path.write_text(f'<testsuites><testsuite>{rows}</testsuite></testsuites>')
        return str(path)

    first = report(
        's1.xml',
        [('backend.tests.test_a', 'test_one', '1.25'), ('backend.tests.test_a', 'test_two', '0.5')],
    )
    second = report(
        's2.xml',
        [('backend.tests.test_b.TestGroup', 'test_three', '2'), ('', 'backend.tests.test_c', '0')],
    )
    assert timings_from_junit(first) == {'backend/tests/test_a.py': 1.8}
    assert timings_from_junit(first, second) == {
        'backend/tests/test_a.py': 1.8,
        'backend/tests/test_b.py': 2.0,
        'backend/tests/test_c.py': 0.0,
    }
    with pytest.raises(ValueError, match='not a backend test'):
        timings_from_junit(report('other.xml', [('examples.test_x', 'test_y', '1')]))
