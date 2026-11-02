"""Data-integrity integration test: versioning/history + saved views.

Complements test_sdk.py (API surface coverage) with deep guarantees:

  1. Versioning/history — a full-featured dataset is snapshotted, mutated in
     every versioned facet (metadata, tags, location, heading, array fields,
     hashes, sample add/delete), and we assert that NOTHING is ever lost:
     snapshots stay immutable, diffs partition exactly, revert restores every
     facet, the pre-revert auto-snapshot makes revert itself undoable, and
     history for DELETED samples remains reachable.
  2. Saved views — views round-trip byte-identical content, are recovered by
     slug/list, resolve to exactly the right samples (and re-resolve correctly
     as the dataset changes), and enforce private/shared/public visibility.

Runs against a live Freedom Ridge backend, creating and destroying an
'integrity-test' dataset and an 'integrity-viewer' user.

Usage (requires FRIDGE_TEST_URL pointing to a disposable backend):
    python -m fridge.test_integrity
"""

import sys
import traceback

import requests

from fridge import Dataset, Sample, connect
from fridge._test_safety import require_test_url
from fridge.core.client import get_client

PASS = 0
FAIL = 0
ERRORS: list[tuple[str, Exception]] = []

DATASET_NAME = 'integrity-test'
VIEWER_NAME = 'integrity-viewer'
VIEWER_PASSWORD = 'integrity-viewer-pw-1'
N = 12  # fixture samples img_00 .. img_11


def test(name):
    """Decorator to run and report a test."""

    def decorator(fn):
        def wrapper(*args, **kwargs):
            global PASS, FAIL
            try:
                fn(*args, **kwargs)
                PASS += 1
                print(f"  PASS  {name}")
            except Exception as e:
                FAIL += 1
                ERRORS.append((name, e))
                print(f"  FAIL  {name}: {e}")
                traceback.print_exc()

        return wrapper

    return decorator


def _fp(i):
    return f's3://integrity/img_{i:02d}.jpg'


def _make_metadata(i):
    md = {
        'idx': i,
        'split': 'train' if i < 6 else 'val',
        'score': i * 0.5,  # binary-exact floats — safe to deep-compare
        'caption': f'integrity sample {i}',
        'captured_at': f'2026-01-{i + 1:02d}T12:00:00Z',
        'nested': {'level': i % 3, 'ok': True},
    }
    if i == 7:
        md['prob_map'] = {
            'type': 'heatmap',
            'map_path': 's3://integrity/hm_7.png',
            'range': [0.0, 1.0],
            'colormap': 'viridis',
        }
    return md


def _make_sample(i):
    return Sample(
        filepath=_fp(i),
        tags=['seed', f'mod3_{i % 3}'],
        metadata=_make_metadata(i),
        location=(-100.0 + i * 0.01, 40.0 + i * 0.01),
        heading=float(i * 30 % 360),
        embeddings={'clip': [float(i) / N] * 8},
        hashes={'phash_8': f'{i + 1:08x}', 'dhash_8': f'{(i + 1) * 17:08x}'},
        array_fields={'integrity_labels': [f'lab_{i % 2}', 'common']},
    )


def run_tests():
    connect(require_test_url())
    client = get_client()
    client.login('admin', 'admin123')
    base = client.base_url

    # --- Clean up leftovers from a previous (crashed) run ---
    for d in client.get_datasets():
        if d['name'] == DATASET_NAME:
            client.delete_dataset(d['id'])
    for u in client.session.get(f"{base}/users").json():
        if u['username'] == VIEWER_NAME:
            client.session.delete(f"{base}/users/{u['id']}")

    # --- Full-featured fixture: every versioned facet populated ---
    ds = Dataset(DATASET_NAME, is_global=True)  # open: the guest-viewer checks below read it
    print(f"\nDataset: {ds.name} (id={ds._id})")
    print("=" * 60)
    ds.create_array_field('integrity_labels')
    ids = ds.add_samples([_make_sample(i) for i in range(N)])['ids']
    orig = {i: _make_sample(i) for i in range(N)}  # pristine copies for comparison

    def fetch_row(sample_id):
        """Raw API row for one sample (bypasses SDK caches)."""
        r = client.query_samples(ds._id, limit=1, filters={'field': 'id', 'operator': '=', 'value': sample_id})
        assert r['items'], f"sample {sample_id} not found"
        return r['items'][0]

    try:
        _run_view_tests(client, ds, ids, base)
        _run_versioning_tests(client, ds, ids, orig, fetch_row)
    finally:
        # --- CLEANUP ---
        print("\n--- Cleanup ---")
        client.delete_dataset(ds._id)
        print(f"Deleted {DATASET_NAME} dataset")
        for u in client.session.get(f"{base}/users").json():
            if u['username'] == VIEWER_NAME:
                client.session.delete(f"{base}/users/{u['id']}")
                print(f"Deleted {VIEWER_NAME} user")

    # --- REPORT ---
    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for name, err in ERRORS:
            print(f"  {name}: {err}")
    print("=" * 60)
    return FAIL == 0


# =========================================================================
# SAVED VIEWS — recovery fidelity, resolution, visibility
# (Runs before versioning so resolution expectations see pristine data.)
# =========================================================================
def _run_view_tests(client, ds, ids, base):
    print("\n--- Views: round-trip fidelity ---")

    stages = [
        {'type': 'filter', 'description': 'val split', 'filter': {'field': 'split', 'operator': '=', 'value': 'val'}},
        {'type': 'filter', 'description': 'idx >= 9', 'filter': {'field': 'idx', 'operator': '>=', 'value': 9}},
    ]
    ctx = {}

    @test("create_view returns content byte-identical to what was sent")
    def _():
        v = ds.create_view('integrity-view', stages, description='deep view', is_public=False)
        assert isinstance(v['content'], dict), f"content not decoded to dict: {type(v['content'])}"
        assert v['content'] == {'viewStages': stages}, f"content mangled: {v['content']}"
        ctx['private'] = v

    _()

    @test("view recovered by slug with identical content")
    def _():
        v = client.get_view_by_slug(ctx['private']['slug'])
        assert v['name'] == 'integrity-view'
        assert v['description'] == 'deep view'
        assert v['content'] == {'viewStages': stages}, f"slug recovery mangled: {v['content']}"

    _()

    @test("view recovered via list with identical content")
    def _():
        match = [v for v in ds.list_views() if v['name'] == 'integrity-view']
        assert len(match) == 1, f"expected exactly 1, got {len(match)}"
        assert match[0]['content'] == {'viewStages': stages}

    _()

    print("\n--- Views: resolution ---")

    def resolve(view):
        """Apply a recovered view's filter stages the way the frontend does (AND)."""
        filters = {'logic': 'AND', 'filters': [st['filter'] for st in view['content']['viewStages']]}
        r = client.query_samples(ds._id, limit=1000, filters=filters)
        return {item['id'] for item in r['items']}

    @test("recovered view resolves to exactly the expected samples")
    def _():
        v = client.get_view_by_slug(ctx['private']['slug'])
        expected = {ids[9], ids[10], ids[11]}  # split=val AND idx>=9
        assert resolve(v) == expected, f"resolved {resolve(v)}, expected {expected}"

    _()

    @test("view re-resolves live as the dataset changes (definition, not id list)")
    def _():
        tmp_id = ds.add_samples([Sample(filepath='s3://integrity/img_tmp.jpg', metadata={'idx': 250, 'split': 'val'})])[
            'ids'
        ][0]
        v = client.get_view_by_slug(ctx['private']['slug'])
        got = resolve(v)
        assert tmp_id in got and len(got) == 4, f"new matching sample missing: {got}"
        ds.delete_sample(tmp_id)
        got = resolve(v)
        assert got == {ids[9], ids[10], ids[11]}, f"stale resolution after delete: {got}"

    _()

    @test("duplicate view name rejected; original intact")
    def _():
        try:
            ds.create_view('integrity-view', stages, is_public=False)
            assert False, "duplicate view name should be rejected"
        except requests.HTTPError as e:
            resp = e.response
            assert resp is not None and resp.status_code == 400, (
                f"expected 400, got {resp.status_code if resp else None}"
            )
        match = [v for v in ds.list_views() if v['name'] == 'integrity-view']
        assert len(match) == 1 and match[0]['content'] == {'viewStages': stages}

    _()

    print("\n--- Views: visibility (private / shared / public) ---")

    resp = client.session.post(
        f"{base}/users",
        json={'username': VIEWER_NAME, 'password': VIEWER_PASSWORD, 'role': 'guest'},
    )
    resp.raise_for_status()
    viewer_id = resp.json()['id']
    viewer = requests.Session()
    viewer.post(f"{base}/auth/login", json={'username': VIEWER_NAME, 'password': VIEWER_PASSWORD}).raise_for_status()
    # New users are created with must_change_password=true (a server-side gate), which 403s every
    # non-exempt route until cleared. Complete the first-login password change (same
    # password) so the viewer can actually use the API — exactly what a real admin-created
    # user does before the app becomes usable.
    viewer.post(
        f"{base}/auth/change-password",
        json={'current_password': VIEWER_PASSWORD, 'new_password': VIEWER_PASSWORD},
    ).raise_for_status()
    anon = requests.Session()

    public = ds.create_view('integrity-public', [stages[0]], is_public=True)
    private = ctx['private']

    @test("other user's list shows public views but not private ones")
    def _():
        names = {v['name'] for v in viewer.get(f"{base}/datasets/{ds._id}/views").json()}
        assert 'integrity-public' in names, f"public view missing: {names}"
        assert 'integrity-view' not in names, f"private view leaked: {names}"

    _()

    @test("private slug hidden (404) from other users; anonymous gets 401")
    def _():
        assert viewer.get(f"{base}/views/{private['slug']}").status_code == 404
        assert anon.get(f"{base}/views/{private['slug']}").status_code == 401

    _()

    @test("view slugs require login; public views open to any logged-in user")
    def _():
        # Login required for ALL access — slugs included; 401 even for slugs
        # that don't exist, so anonymous callers can't probe for them.
        assert anon.get(f"{base}/views/{public['slug']}").status_code == 401
        assert anon.get(f"{base}/views/no-such-slug").status_code == 401
        r = viewer.get(f"{base}/views/{public['slug']}")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        assert r.json()['content'] == {'viewStages': [stages[0]]}

    _()

    @test("sharing grants slug access + list visibility; unsharing revokes")
    def _():
        client.session.patch(
            f"{base}/datasets/{ds._id}/views/{private['id']}/share",
            json={'is_public': False, 'shared_with_user_ids': [viewer_id]},
        ).raise_for_status()
        assert viewer.get(f"{base}/views/{private['slug']}").status_code == 200
        names = {v['name'] for v in viewer.get(f"{base}/datasets/{ds._id}/views").json()}
        assert 'integrity-view' in names, f"shared view not listed: {names}"
        client.session.patch(
            f"{base}/datasets/{ds._id}/views/{private['id']}/share",
            json={'is_public': False, 'shared_with_user_ids': []},
        ).raise_for_status()
        assert viewer.get(f"{base}/views/{private['slug']}").status_code == 404

    _()

    @test("non-owner cannot delete a view")
    def _():
        assert viewer.delete(f"{base}/views/{private['id']}").status_code == 403
        assert client.get_view_by_slug(private['slug'])['name'] == 'integrity-view'

    _()

    @test("owner sees own private view by slug")
    def _():
        assert client.get_view_by_slug(private['slug'])['content'] == {'viewStages': stages}

    _()


# =========================================================================
# VERSIONING / HISTORY — snapshot fidelity, change detection, revert, undo
# =========================================================================
def _run_versioning_tests(client, ds, ids, orig, fetch_row):
    ctx = {}

    print("\n--- Versioning: snapshot fidelity ---")

    @test("create full version captures exact sample count")
    def _():
        v = ds.create_version('v1', 'full snapshot of pristine fixture')
        assert v['sample_count'] == N, f"expected {N}, got {v['sample_count']}"
        assert v['include_metadata'] is True
        ctx['v1'] = v

    _()

    @test("checkout(v1) round-trips every sample's metadata and tags")
    def _():
        got = {s.filepath: s for s in ds.checkout('v1')}
        assert len(got) == N, f"expected {N} samples, got {len(got)}"
        for i in range(N):
            s = got[_fp(i)]
            assert s.metadata == orig[i].metadata, f"img_{i:02d} metadata mismatch: {s.metadata} != {orig[i].metadata}"
            assert set(s.tags) == set(orig[i].tags), f"img_{i:02d} tags mismatch: {s.tags}"

    _()

    print("\n--- Versioning: every facet's change is detected ---")

    # Mutate ONE facet per sample so diffs attribute changes precisely.
    mutations = [
        ("metadata", lambda: client.update_sample(ds._id, ids[0], metadata={'score': 999.9})),
        ("tags", lambda: client.update_sample(ds._id, ids[1], tags=['mutated'])),
        ("location", lambda: client.update_sample(ds._id, ids[2], location=(-50.0, 10.0))),
        ("heading", lambda: client.update_sample(ds._id, ids[3], heading=359.0)),
        ("array field", lambda: client.update_sample(ds._id, ids[4], array_fields={'integrity_labels': ['changed']})),
        ("hashes", lambda: ds.set_hashes([ids[5]], {'phash_8': 'deadbeef'})),
    ]
    for n_mutated, (facet, mutate) in enumerate(mutations, start=1):

        @test(f"{facet} change detected as exactly {n_mutated} modified")
        def _(mutate=mutate, n_mutated=n_mutated, facet=facet):
            mutate()
            d = ds.diff(version_name='v1')
            assert d['modified'] == n_mutated, f"after {facet} change: modified={d['modified']}, expected {n_mutated}"

        _()

    @test("sample deletion detected as removed; addition as added; exact partition")
    def _():
        ds.delete_sample(ids[6])
        ctx['new_id'] = ds.add_samples(
            [Sample(filepath='s3://integrity/img_new.jpg', metadata={'idx': 100, 'split': 'val', 'score': 50.0})],
        )['ids'][0]
        d = ds.diff(version_name='v1')
        assert (d['added'], d['removed'], d['modified'], d['unchanged']) == (1, 1, 6, 5), f"diff partition wrong: {d}"
        assert d['removed'] + d['modified'] + d['unchanged'] == N

    _()

    @test("diff sample listings name exactly the right samples")
    def _():
        v1 = ctx['v1']['id']
        added = client.get_current_diff_samples(ds._id, v1, 'added')['sample_ids']
        removed = client.get_current_diff_samples(ds._id, v1, 'removed')['sample_ids']
        modified = client.get_current_diff_samples(ds._id, v1, 'modified')['sample_ids']
        assert added == [ctx['new_id']], f"added: {added}"
        assert removed == [ids[6]], f"removed: {removed}"
        assert sorted(modified) == sorted(ids[:6]), f"modified: {modified}"

    _()

    @test("status reports unsaved changes and no matching version")
    def _():
        st = ds.version_status()
        assert st['has_changes'] is True
        assert st.get('current_version') is None

    _()

    print("\n--- Versioning: history is never lost ---")

    @test("snapshot is immutable: checkout(v1) still shows pre-mutation values")
    def _():
        got = {s.filepath: s for s in ds.checkout('v1')}
        assert got[_fp(0)].metadata == orig[0].metadata, "v1 metadata polluted by live edit"
        assert set(got[_fp(1)].tags) == set(orig[1].tags), "v1 tags polluted by live edit"

    _()

    @test("DELETED sample's history still reachable via checkout(v1)")
    def _():
        got = {s.filepath: s for s in ds.checkout('v1')}
        assert len(got) == N, f"checkout dropped deleted sample: {len(got)}/{N}"
        assert got[_fp(6)].metadata == orig[6].metadata, "deleted sample's snapshot lost"

    _()

    @test("version samples show snapshot location/heading, not live values")
    def _():
        items = client.get_version_samples(ds._id, ctx['v1']['id'], limit=100, full=True)['items']
        by_fp = {it['filepath']: it for it in items}
        loc = by_fp[_fp(2)]['location']
        assert loc is not None, "snapshot location missing"
        lon, lat = loc['coordinates']
        elon, elat = orig[2].location['coordinates'] if isinstance(orig[2].location, dict) else orig[2].location
        assert abs(lon - elon) < 1e-6 and abs(lat - elat) < 1e-6, (
            f"location shows live value: ({lon}, {lat}), expected ({elon}, {elat})"
        )
        assert abs(by_fp[_fp(3)]['location']['heading'] - float(3 * 30 % 360)) < 1e-6, (
            f"heading shows live value: {by_fp[_fp(3)]['location']}"
        )

    _()

    print("\n--- Versioning: revert restores every facet ---")

    @test("revert(v1) reports true restore count + creates an undo auto-snapshot")
    def _():
        r = ds.revert(version_name='v1')
        # One snapshotted sample was deleted, so only the N - 1 survivors are
        # restored — the response reports actual rows, not the version's count.
        assert r['samples_reverted'] == N - 1, f"samples_reverted={r['samples_reverted']}"
        assert r['auto_snapshot_id'] is not None and r['auto_snapshot_name'].startswith('auto-'), (
            f"no undo snapshot: {r}"
        )
        ctx['auto_id'] = r['auto_snapshot_id']

    _()

    @test("revert restored metadata exactly")
    def _():
        row = fetch_row(ids[0])
        assert row['metadata'] == orig[0].metadata, f"metadata not restored: {row['metadata']}"

    _()

    @test("revert restored tags exactly")
    def _():
        assert set(fetch_row(ids[1])['tags']) == set(orig[1].tags)

    _()

    @test("revert restored location and heading exactly")
    def _():
        loc = fetch_row(ids[2])['location']
        lon, lat = loc['coordinates']
        assert abs(lon - (-100.0 + 0.02)) < 1e-6 and abs(lat - 40.02) < 1e-6, f"location: {loc}"
        loc3 = fetch_row(ids[3])['location']
        assert abs(loc3['heading'] - float(3 * 30 % 360)) < 1e-6, f"heading: {loc3}"

    _()

    @test("revert restored array fields exactly")
    def _():
        row = fetch_row(ids[4])
        assert row.get('integrity_labels') == ['lab_0', 'common'], (
            f"array field not restored: {row.get('integrity_labels')}"
        )

    _()

    @test("by default, revert never deletes or resurrects samples")
    def _():
        assert ds['s3://integrity/img_new.jpg'].id == ctx['new_id'], "added sample lost by revert"
        try:
            ds[_fp(6)]
            assert False, "default revert resurrected a deleted sample"
        except KeyError:
            pass

    _()

    @test("post-revert diff vs v1 = only the hash change (hashes are NOT reverted)")
    def _():
        d = ds.diff(version_name='v1')
        assert (d['added'], d['removed'], d['modified'], d['unchanged']) == (1, 1, 1, 10), f"post-revert diff: {d}"
        modified = client.get_current_diff_samples(ds._id, ctx['v1']['id'], 'modified')['sample_ids']
        assert modified == [ids[5]], f"expected only the hash-mutated sample: {modified}"

    _()

    print("\n--- Versioning: revert is undoable ---")

    @test("reverting to the auto-snapshot restores the pre-revert state")
    def _():
        ds.revert(version_id=ctx['auto_id'])
        assert fetch_row(ids[0])['metadata'].get('score') == 999.9, "undo lost mutated metadata"
        assert fetch_row(ids[1])['tags'] == ['mutated'], "undo lost mutated tags"
        lon, lat = fetch_row(ids[2])['location']['coordinates']
        assert abs(lon - (-50.0)) < 1e-6 and abs(lat - 10.0) < 1e-6, "undo lost mutated location"
        assert fetch_row(ids[4]).get('integrity_labels') == ['changed'], "undo lost array field"

    _()

    @test("after undo, status matches the auto-snapshot exactly")
    def _():
        st = ds.version_status()
        assert st['has_changes'] is False, f"unexpected changes: {st}"
        assert st['current_version']['id'] == ctx['auto_id'], f"wrong version: {st}"

    _()

    print("\n--- Versioning: hash-only versions + lifecycle safety ---")

    @test("hash-only version diffs clean (no tag/array false positives)")
    def _():
        v = ds.create_version('v-lite', include_metadata=False)
        assert v['include_metadata'] is False
        ctx['vlite'] = v
        d = ds.diff(version_name='v-lite')
        assert (d['added'], d['removed'], d['modified']) == (0, 0, 0), f"false positives: {d}"
        assert d['unchanged'] == N

    _()

    @test("revert to hash-only version is refused (400, nothing changed)")
    def _():
        before = fetch_row(ids[0])['metadata']
        try:
            ds.revert(version_name='v-lite')
            assert False, "revert to hash-only version should be rejected"
        except requests.HTTPError as e:
            resp = e.response
            assert resp is not None and resp.status_code == 400, (
                f"expected 400, got {resp.status_code if resp else None}"
            )
        assert fetch_row(ids[0])['metadata'] == before, "refused revert still mutated data"

    _()

    @test("duplicate version name rejected without corrupting history")
    def _():
        try:
            ds.create_version('v1')
            assert False, "duplicate version name should be rejected"
        except requests.HTTPError as e:
            # A 409 specifically: accepting any HTTPError let an unhandled 500 pass for months.
            status = e.response.status_code if e.response is not None else None
            assert status == 409, f"expected 409 for a duplicate version name, got {status}"
        v1s = [v for v in ds.list_versions() if v['name'] == 'v1']
        assert len(v1s) == 1, f"duplicate v1 rows: {len(v1s)}"
        assert v1s[0]['sample_count'] == N

    _()

    @test("deleting one version leaves other versions and live data intact")
    def _():
        client.delete_version(ds._id, ctx['vlite']['id'])
        names = {v['name'] for v in ds.list_versions()}
        assert 'v-lite' not in names and 'v1' in names, f"versions: {names}"
        got = {s.filepath: s for s in ds.checkout('v1')}
        assert len(got) == N and got[_fp(6)].metadata == orig[6].metadata, (
            "v1 history damaged by deleting an unrelated version"
        )
        assert ds.all().count() == N, "live data changed by version delete"

    _()

    @test("version-to-version diff partitions exactly")
    def _():
        d = client.get_version_diff(ds._id, ctx['v1']['id'], ctx['auto_id'])
        assert (d['added'], d['removed'], d['modified'], d['unchanged']) == (1, 1, 6, 5), (
            f"v1 vs auto-snapshot diff: {d}"
        )

    _()

    print("\n--- Versioning: restore deleted samples ---")

    @test("revert(restore_deleted=True) re-creates the deleted sample, original id")
    def _():
        r = ds.revert(version_name='v1', restore_deleted=True)
        assert r['samples_restored'] == 1, f"samples_restored={r['samples_restored']}"
        assert r['samples_reverted'] == N - 1, f"samples_reverted={r['samples_reverted']}"
        s6 = ds[_fp(6)]
        assert s6.id == ids[6], f"restored with new id {s6.id} != {ids[6]} (history broken)"
        assert s6.metadata == orig[6].metadata, f"restored metadata: {s6.metadata}"
        assert set(s6.tags) == set(orig[6].tags), f"restored tags: {s6.tags}"
        row = fetch_row(ids[6])
        lon, lat = row['location']['coordinates']
        assert abs(lon - (-100.0 + 0.06)) < 1e-6 and abs(lat - 40.06) < 1e-6, f"location: {row['location']}"
        assert abs(row['location']['heading'] - float(6 * 30 % 360)) < 1e-6, f"heading: {row['location']}"
        assert row.get('integrity_labels') == ['lab_0', 'common'], f"array field: {row.get('integrity_labels')}"
        assert row.get('media_type') == 'image', f"media_type: {row.get('media_type')}"

    _()

    @test("restored sample is flagged for derived-data backfill (missing hashes)")
    def _():
        m = client.get_missing(ds._id, 'hash', field='phash_8')
        missing_ids = {it['id'] for it in m['items']}
        assert ids[6] in missing_ids, f"restored sample not flagged for backfill: {missing_ids}"

    _()

    @test("restored sample reconnects to history; hash backfill completes it")
    def _():
        # No longer 'removed' — but it diffs as modified (hashes were destroyed
        # with the sample and are never restored) until the uploader backfills.
        d = ds.diff(version_name='v1')
        assert (d['added'], d['removed'], d['modified'], d['unchanged']) == (1, 0, 2, 10), f"post-restore diff: {d}"
        modified = client.get_current_diff_samples(ds._id, ctx['v1']['id'], 'modified')['sample_ids']
        assert sorted(modified) == sorted([ids[5], ids[6]]), f"modified: {modified}"
        # Backfill the original hash values -> bit-identical to v1 again.
        ds.set_hashes([ids[6]], {'phash_8': f'{7:08x}', 'dhash_8': f'{7 * 17:08x}'})
        d = ds.diff(version_name='v1')
        assert (d['added'], d['removed'], d['modified'], d['unchanged']) == (1, 0, 1, 11), f"after backfill: {d}"

    _()

    @test("restore is idempotent: second call restores nothing, no duplicates")
    def _():
        r = ds.revert(version_name='v1', restore_deleted=True)
        assert r['samples_restored'] == 0, f"second restore not a no-op: {r}"
        assert ds.all().count() == N + 1, f"sample count drifted: {ds.all().count()}"

    _()


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
