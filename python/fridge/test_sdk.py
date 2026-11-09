"""Comprehensive Freedom Ridge SDK integration test.

Runs against a live Freedom Ridge backend, creating and destroying a 'sdk-test' dataset.
Tests every SDK method that doesn't require external resources (S3 images, embedding models).

Usage (requires FRIDGE_TEST_URL pointing to a disposable backend):
    python -m fridge.test_sdk
"""

import sys
import traceback

from fridge import ClassProbabilities, Dataset, DatasetView, Sample, connect, geo_within, threshold_rule
from fridge._test_safety import require_test_url
from fridge.core.client import get_client

PASS = 0
FAIL = 0
ERRORS: list[tuple[str, Exception]] = []


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


def run_tests():
    connect(require_test_url())
    client = get_client()
    client.login('admin', 'admin123')

    # Clean up any leftover test datasets
    for d in client.get_datasets():
        if d['name'] in ('sdk-test', 'sdk-test-sort', 'sdk-test-ann', 'sdk-test-ver', 'sdk-test-delemb'):
            client.delete_dataset(d['id'])

    ds = Dataset('sdk-test')
    print(f"\nDataset: {ds.name} (id={ds._id})")
    print("=" * 60)

    # --- SAMPLE CRUD ---
    print("\n--- Sample CRUD ---")

    @test("add_samples")
    def _():
        result = ds.add_samples(
            [
                Sample(
                    filepath=f's3://test/img_{i}.jpg',
                    tags=['batch1'],
                    metadata={'index': i, 'category': 'a' if i < 5 else 'b', 'score': i * 0.1},
                    location=(i * 0.01 - 90, i * 0.01 + 40),
                    heading=i * 10.0,
                )
                for i in range(10)
            ],
        )
        assert len(result['ids']) == 10, f"Expected 10 ids, got {len(result['ids'])}"
        # Store for later tests
        _.__ids = result['ids']

    _()
    sample_ids = getattr(_, '_Test__ids', None) or _.__ids

    @test("dataset[filepath] lookup")
    def _():
        s = ds['s3://test/img_0.jpg']
        assert s.id == sample_ids[0], f"Expected id {sample_ids[0]}, got {s.id}"
        assert s.tags == ['batch1']
        assert s.metadata['index'] == 0

    _()

    @test("get_or_create existing")
    def _():
        s, created = ds.get_or_create('s3://test/img_1.jpg')
        assert not created, "Should not be created"
        assert s.id == sample_ids[1]

    _()

    @test("get_or_create new")
    def _():
        s, created = ds.get_or_create('s3://test/img_new.jpg', tags=['new'], metadata={'index': 99})
        assert created, "Should be created"
        assert s.id is not None
        ds.delete_sample(s.id)  # Clean up

    _()

    @test("add_samples update-mode merges tags (never erases UI curation)")
    def _():
        # Guard against the data-loss regression where ON CONFLICT DO UPDATE
        # replaced tags wholesale: a streaming re-ingest in 'update' mode must
        # keep tags users added in the app, while metadata stays uploader-owned.
        fp = 's3://test/dup_update_tags.jpg'
        ds.add_samples([Sample(filepath=fp, tags=['stream'], metadata={'rev': 1})])
        s = ds[fp]
        s.tags = s.tags + ['user-curated']  # simulate in-app curation after ingest
        s.save()
        ds.add_samples(
            [Sample(filepath=fp, tags=['stream', 'v2'], metadata={'rev': 2})],
            on_duplicate='update',
        )
        s = ds[fp]
        assert 'user-curated' in s.tags, f"user tag erased by update-mode ingest: {s.tags}"
        assert 'stream' in s.tags and 'v2' in s.tags, f"uploader tags missing: {s.tags}"
        assert s.metadata['rev'] == 2, "metadata should be uploader-owned (replaced on update)"
        ds.delete_sample(s.id)  # keep the suite idempotent

    _()

    @test("update_sample via save()")
    def _():
        s = ds['s3://test/img_2.jpg']
        s.tags = ['updated']
        s.metadata['score'] = 9.99
        s.save()
        s2 = ds['s3://test/img_2.jpg']
        assert 'updated' in s2.tags, f"Tags not updated: {s2.tags}"

    _()

    @test("add_heatmap + save round-trip")
    def _():
        s = ds['s3://test/img_3.jpg']
        s.add_heatmap(
            name='my_heatmap',
            map_path='s3://test/img_3_heatmap.png',
            range=(0, 100),
            colormap='viridis',
        )
        s.save()
        s2 = ds['s3://test/img_3.jpg']
        hm = s2.metadata.get('my_heatmap')
        assert isinstance(hm, dict), f"Heatmap metadata missing: {s2.metadata}"
        assert hm.get('type') == 'heatmap', f"type marker missing: {hm}"
        assert hm.get('map_path') == 's3://test/img_3_heatmap.png'
        assert hm.get('range') == [0.0, 100.0]
        assert hm.get('colormap') == 'viridis'

    _()

    @test("class probabilities: the label, its classes and colours, and its default rule")
    def _():
        s = ds['s3://test/img_5.jpg']
        s.metadata['weeds'] = ClassProbabilities('s3://test/img_5_probs.png', classes=3)
        s.save()
        assert ds['s3://test/img_5.jpg'].metadata['weeds'] == {
            'type': 'class_probabilities',
            'probPath': 's3://test/img_5_probs.png',
            'classes': 3,
        }
        ds.set_field_classes('weeds', ['soil', 'crop', 'weed'], colors={1: '#00ff00', 2: '#ff0000'})
        rule = threshold_rule([(2, 0.3), (1, 0.6)], fallback=None)
        ds.set_default_rule('weeds', rule)
        config = ds.client.get_dataset_by_id(ds._id)['segmentation_config']
        assert config['mask_targets']['weeds'] == {'0': 'soil', '1': 'crop', '2': 'weed'}, config
        assert config['color_scheme']['fields']['weeds']['idColors'] == {'1': '#00ff00', '2': '#ff0000'}, config
        assert config['class_rules']['weeds'] == rule, config
        try:
            ds.set_default_rule('weeds', {'type': 'rules', 'rules': []})
            assert False, "A rule with no thresholds should be refused"
        except Exception as error:
            assert '1 to 64' in str(error), error
        ds.set_default_rule('weeds', None)
        assert 'weeds' not in ds.client.get_dataset_by_id(ds._id)['segmentation_config']['class_rules']

    _()

    @test("update_sample with remove_metadata_keys")
    def _():
        # Seed a sample with two heatmap keys, remove one authoritatively.
        s = ds['s3://test/img_4.jpg']
        s.add_heatmap('hm_a', 's3://test/a.png', (0, 1))
        s.add_heatmap('hm_b', 's3://test/b.png', (0, 1))
        s.metadata['unrelated'] = 'keep_me'
        s.save()
        # Authoritative removal: drop hm_a, leave hm_b and unrelated alone.
        ds.client.update_sample(
            ds._id,
            s.id,
            remove_metadata_keys=['hm_a'],
        )
        s2 = ds['s3://test/img_4.jpg']
        assert 'hm_a' not in s2.metadata, f"hm_a not removed: {s2.metadata.keys()}"
        assert 'hm_b' in s2.metadata, f"hm_b should survive: {s2.metadata.keys()}"
        assert s2.metadata.get('unrelated') == 'keep_me', "Unrelated metadata was touched"

    _()

    @test("delete_sample")
    def _():
        ds.add_samples([Sample(filepath='s3://test/to_delete.jpg', metadata={'temp': True})])
        s = ds['s3://test/to_delete.jpg']
        ds.delete_sample(s.id)
        try:
            ds['s3://test/to_delete.jpg']
            assert False, "Should have raised KeyError"
        except KeyError:
            pass

    _()

    # --- TAGS ---
    print("\n--- Health ---")

    @test("GET /api/health is 200 and reports no pending migrations")
    def _():
        # A backend serving an un-migrated database must say so (503 + the list) instead of
        # 500ing one feature at a time; a current database (schema.sql seeds the ledger with
        # every migration file) must be a plain 200.
        r = client.session.get(f"{client.base_url}/health")
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
        body = r.json()
        assert body.get('status') == 'ok' and body.get('pending_migrations') == [], body

    _()

    print("\n--- Tags ---")

    @test("tag_samples")
    def _():
        ds.tag_samples(sample_ids[:3], ['tagged'])
        s = ds['s3://test/img_0.jpg']
        assert 'tagged' in s.tags, f"Tag not added: {s.tags}"

    _()

    @test("tagging stamps who/when on the sample and registers the tag (created by, created at)")
    def _():
        # last_modified_at must move when a tag is applied (it never did), and the tag registry
        # must know who created the tag and when; a tag that arrived with the
        # sample (ingest) is listed with unknown dates rather than missing.
        def _stamp():
            # The grid query is the API surface that returns last_modified_at (ds[filepath] doesn't).
            r = client.session.post(
                f"{client.base_url}/datasets/{ds._id}/samples/query",
                json={
                    'filters': {
                        'logic': 'AND',
                        'filters': [{'field': 'id', 'operator': 'in', 'value': [sample_ids[1]]}],
                    },
                    'limit': 1,
                },
            )
            r.raise_for_status()
            rows = r.json().get('items') or r.json().get('samples') or []
            assert len(rows) == 1, rows
            return rows[0].get('last_modified_at')

        before = _stamp()
        ds.tag_samples([sample_ids[1]], ['dated-by-sdk'])
        after = _stamp()
        assert after and after != before, f"last_modified_at not stamped by tagging: {before!r} -> {after!r}"
        resp = client.session.post(
            f"{client.base_url}/datasets/{ds._id}/samples/tag-counts",
            json={'sample_ids': sample_ids[:3], 'field_name': 'tags'},
        )
        resp.raise_for_status()
        info = resp.json().get('tag_info') or {}
        assert 'dated-by-sdk' in info, f"tag not registered: {sorted(info)}"
        assert info['dated-by-sdk']['created_by'] == 'admin', info['dated-by-sdk']
        assert info['dated-by-sdk']['created_at'], info['dated-by-sdk']
        assert info['dated-by-sdk']['last_used_at'], info['dated-by-sdk']
        assert 'tagged' in info and info['tagged']['created_by'] == 'admin', info.get('tagged')
        ds.untag_samples([sample_ids[1]], ['dated-by-sdk'])

    _()

    @test("untag_samples")
    def _():
        ds.untag_samples(sample_ids[:3], ['tagged'])
        s = ds['s3://test/img_0.jpg']
        assert 'tagged' not in s.tags, f"Tag not removed: {s.tags}"

    _()

    # --- SET VALUES ---
    print("\n--- Set Values ---")

    @test("set_values")
    def _():
        count = ds.set_values(sample_ids[:5], 'reviewed', True)
        assert count == 5, f"Expected 5 updated, got {count}"

    _()

    # --- LOOKUP ---
    print("\n--- Lookup ---")

    @test("lookup_samples by metadata field")
    def _():
        result = ds.lookup_samples('index', [0, 1, 2])
        assert len(result) == 3, f"Expected 3 matches, got {len(result)}"

    _()

    # --- SCHEMA & FIELDS ---
    print("\n--- Schema & Fields ---")

    @test("get_schema")
    def _():
        schema = ds.get_schema()
        names = [f['name'] for f in schema]
        assert 'tags' in names, f"Missing tags in schema: {names}"
        assert 'index' in names, f"Missing index in schema: {names}"

    _()

    @test("datetime_field_detection")
    def _():
        # Regression guard (this has broken repeatedly): ISO-datetime metadata must
        # auto-detect as 'datetime' so the Filters sidebar renders a date slider, not
        # a free-text/enum box. Covers all three stored shapes the detector accepts:
        # 'T'-separated, space-separated, and date-only, each in
        # its own field, so reverting to the old 'T'-only rule fails this test. The
        # plain-string control must NOT be promoted to datetime.
        added = ds.add_samples(
            [
                Sample(
                    filepath='s3://test/dt_a.jpg',
                    metadata={
                        'datetime': '2021-06-01T22:52:12',
                        'local_datetime': '2021-06-01T17:52:12',
                        'cap_space': '2020-04-23 05:15:30',
                        'cap_dateonly': '2024-01-11',
                        'note_str': 'alpha',
                    },
                ),
                Sample(
                    filepath='s3://test/dt_b.jpg',
                    metadata={
                        'datetime': '2022-07-21T19:34:50',
                        'local_datetime': '2022-07-21T14:34:50',
                        'cap_space': '2021-08-15 12:00:00',
                        'cap_dateonly': '2025-03-02',
                        'note_str': 'beta',
                    },
                ),
            ],
        )
        try:
            ds.refresh_stats()
            dt_fields = ('datetime', 'local_datetime', 'cap_space', 'cap_dateonly')

            # Both schema surfaces must agree. get_schema() hits GET /schema; the Filters
            # sidebar hits /field-stats — separate code paths that have diverged before,
            # so assert datetime on both.
            schema_types = {f['name']: f['type'] for f in ds.get_schema()}
            fs_resp = ds.client.session.get(f"{ds.client.base_url}/datasets/{ds._id}/field-stats")
            fs_resp.raise_for_status()
            fieldstats_types = {f['name']: f['type'] for f in fs_resp.json()}

            for surface, types in (('schema', schema_types), ('field-stats', fieldstats_types)):
                for fld in dt_fields:
                    assert types.get(fld) == 'datetime', (
                        f"[{surface}] {fld} should be 'datetime', got {types.get(fld)!r}"
                    )
                assert types.get('note_str') != 'datetime', f"[{surface}] note_str wrongly promoted to datetime"

        finally:
            # Self-cleaning, even when an assertion above fails: drop the probe samples so the
            # persistent sample count is unchanged for downstream tests (versioning asserts an exact
            # count of 10 — one failure here used to cascade into the versioning tests).
            for sid in added['ids']:
                ds.delete_sample(sid)

    _()

    @test("promote_field / demote_field")
    def _():
        ds.promote_field('index')
        ds.demote_field('index')

    _()

    @test("create_array_field / list_array_fields")
    def _():
        ds.create_array_field('test_labels')
        fields = ds.list_array_fields()
        assert 'test_labels' in fields, f"test_labels not in {fields}"

    _()

    # --- DATASET METADATA ---
    print("\n--- Dataset Metadata ---")

    @test("get_dataset_by_id")
    def _():
        info = client.get_dataset_by_id(ds._id)
        assert info['name'] == 'sdk-test'

    _()

    @test("update_dataset")
    def _():
        client.update_dataset(ds._id, description='Integration test dataset')
        info = client.get_dataset_by_id(ds._id)
        assert info['description'] == 'Integration test dataset'

    _()

    @test("refresh_stats")
    def _():
        ds.refresh_stats()

    _()

    # --- VIEWS ---
    print("\n--- Views ---")

    @test("create_view")
    def _():
        v = ds.create_view(
            'Test View',
            [
                {
                    'type': 'filter',
                    'description': 'cat=a',
                    'filter': {'field': 'category', 'operator': '=', 'value': 'a'},
                },
            ],
            description='Category A only',
        )
        assert v['name'] == 'Test View'
        _.__view_id = v['id']
        _.__view_slug = v['slug']

    _()
    view_id = _.__view_id
    view_slug = _.__view_slug

    @test("list_views")
    def _():
        views = ds.list_views()
        assert any(v['name'] == 'Test View' for v in views), f"View not found in {views}"

    _()

    @test("get_view_by_slug")
    def _():
        v = client.get_view_by_slug(view_slug)
        assert v['name'] == 'Test View'

    _()

    @test("delete_view")
    def _():
        ds.delete_view(view_id)
        views = ds.list_views()
        assert not any(v['name'] == 'Test View' for v in views)

    _()

    # --- VERSIONING ---
    print("\n--- Versioning ---")

    @test("create_version")
    def _():
        v = ds.create_version('v1', 'First version')
        assert v['name'] == 'v1'
        _.__version_id = v['id']

    _()
    version_id = _.__version_id

    @test("list_versions")
    def _():
        versions = ds.list_versions()
        assert any(v['name'] == 'v1' for v in versions)

    _()

    @test("get_version_status")
    def _():
        status = client.get_version_status(ds._id)
        assert status['current_version']['name'] == 'v1'
        assert not status['has_changes']

    _()

    @test("get_version")
    def _():
        v = client.get_version(ds._id, version_id)
        assert v['name'] == 'v1'

    _()

    # Make a change so diff has something to show
    ds.add_samples([Sample(filepath='s3://test/img_after_v1.jpg', metadata={'index': 100})])

    @test("diff (current vs version)")
    def _():
        d = ds.diff(version_name='v1')
        assert d['added'] >= 1, f"Expected at least 1 added, got {d['added']}"
        assert d['unchanged'] >= 10, "Expected at least 10 unchanged"

    _()

    @test("get_current_diff_samples")
    def _():
        samples = client.get_current_diff_samples(ds._id, version_id, 'added')
        assert len(samples) >= 1

    _()

    @test("checkout (iterate version samples)")
    def _():
        count = 0
        for sample in ds.checkout(version_name='v1'):
            count += 1
            assert sample.filepath, "Sample has no filepath"
            assert sample.id is not None
        assert count == 10, f"Expected 10 versioned samples, got {count}"

    _()

    @test("revert")
    def _():
        result = ds.revert(version_name='v1')
        assert result['samples_reverted'] == 10

    _()

    @test("delete_version")
    def _():
        # Create a throwaway version to delete
        v2 = ds.create_version('v-deleteme')
        client.delete_version(ds._id, v2['id'])
        versions = ds.list_versions()
        assert not any(v['name'] == 'v-deleteme' for v in versions)

    _()

    # --- VERSIONING HONESTY ---
    print("\n--- Versioning honesty ---")

    def _ver_api(method, path, **kw):
        r = client.session.request(method, f"{client.base_url}{path}", **kw)
        r.raise_for_status()
        return r.json()

    def _ver_rows(dataset_id, sample_ids):
        rows = _ver_api(
            'POST',
            f"/datasets/{dataset_id}/samples/query",
            json={'filters': {'field': 'id', 'operator': 'in', 'value': sample_ids}, 'limit': len(sample_ids)},
        )
        return {r['id']: r for r in (rows.get('items') or rows.get('samples') or [])}

    ver_ds = _ver_api('POST', '/datasets', json={'name': 'sdk-test-ver'})['id']
    ver_ids = _ver_api(
        'POST',
        f"/datasets/{ver_ds}/samples",
        json={
            'samples': [{'filepath': f's3://test/ver_{i}.jpg', 'metadata': {'i': i}} for i in range(4)]
            + [
                {'filepath': 's3://test/ver_0.jpg#o0', 'media_type': 'object', 'metadata': {'parent': 0}},
                {'filepath': 's3://test/ver_noext', 'media_type': 'image'},
            ],
        },
    )['ids']
    ver_v1 = _ver_api('POST', f"/datasets/{ver_ds}/versions", json={'name': 'v1'})['id']
    _ver_api('POST', f"/datasets/{ver_ds}/versions", json={'name': 'hashonly', 'include_metadata': False})

    @test("version status: a hash-only version is not 'current' after a tag change")
    def _():
        _ver_api(
            'POST',
            f"/datasets/{ver_ds}/samples/bulk-tag",
            json={'sample_ids': [ver_ids[0]], 'add_tags': ['vtag']},
        )
        status = _ver_api('GET', f"/datasets/{ver_ds}/versions/status")
        assert status['has_changes'] is True, status

    _()

    @test("current-diff counts a heading added after the version")
    def _():
        _ver_api('PATCH', f"/datasets/{ver_ds}/samples/{ver_ids[1]}", json={'heading': 123.0})
        diff = _ver_api('GET', f"/datasets/{ver_ds}/versions/current-diff/{ver_v1}")
        assert diff['modified'] == 2, diff  # the tagged sample + the re-headed one

    _()

    @test("revert restores media_type, clears the added heading, stamps only changed rows")
    def _():
        untouched_before = _ver_rows(ver_ds, [ver_ids[2]])[ver_ids[2]]['last_modified_at']
        for sid in ver_ids[4:6]:
            _ver_api('DELETE', f"/datasets/{ver_ds}/samples/{sid}")
        result = _ver_api('POST', f"/datasets/{ver_ds}/versions/{ver_v1}/revert", params={'restore_deleted': 'true'})
        assert result['samples_restored'] == 2, result
        rows = _ver_rows(ver_ds, ver_ids)
        assert rows[ver_ids[4]]['media_type'] == 'object', rows[ver_ids[4]]
        assert rows[ver_ids[5]]['media_type'] == 'image', rows[ver_ids[5]]
        assert rows[ver_ids[1]].get('heading') is None, rows[ver_ids[1]]
        assert rows[ver_ids[2]]['last_modified_at'] == untouched_before, (untouched_before, rows[ver_ids[2]])

    _()
    client.delete_dataset(ver_ds)

    # --- QUERY API ---
    print("\n--- Query API ---")

    @test("dataset.all()")
    def _():
        view = ds.all()
        assert isinstance(view, DatasetView)
        count = len(view)
        assert count >= 10, f"Expected at least 10, got {count}"

    _()

    @test("dataset.filter()")
    def _():
        view = ds.filter(category='a')
        count = len(view)
        assert count == 5, f"Expected 5, got {count}"

    _()

    @test("view.first()")
    def _():
        s = ds.all().first()
        assert s is not None
        assert s.id is not None

    _()

    @test("view.ids()")
    def _():
        ids = ds.all().ids()
        assert len(ids) >= 10

    _()

    @test("view.values(field)")
    def _():
        vals = ds.filter(category='a').values('index')
        assert len(vals) == 5

    _()

    @test("view.count_values(field)")
    def _():
        counts = ds.all().count_values('category')
        assert 'a' in counts, f"'a' not in {counts}"
        assert 'b' in counts, f"'b' not in {counts}"

    _()

    @test("view.distinct(field)")
    def _():
        cats = ds.all().distinct('category')
        assert set(cats) == {'a', 'b'}, f"Expected {{a, b}}, got {cats}"

    _()

    @test("view.bounds(field)")
    def _():
        low, high = ds.all().bounds('score')
        assert low is not None and high is not None

    _()

    @test("iter_samples with selected_fields")
    def _():
        count = 0
        for s in ds.iter_samples(selected_fields=['filepath', 'index']):
            count += 1
            assert s.filepath is not None
        assert count >= 10

    _()

    @test("iter_samples with filters")
    def _():
        f = {"logic": "AND", "filters": [{"field": "category", "operator": "=", "value": "b"}]}
        count = sum(1 for _ in ds.iter_samples(filters=f))
        assert count == 5, f"Expected 5, got {count}"

    _()

    # --- HASHES ---
    print("\n--- Hashes ---")

    @test("set_hashes")
    def _():
        ds.set_hashes(sample_ids[:3], {'dhash_8': 'aabbccdd', 'phash_8': '11223344'})

    _()

    @test("get_hash_fields")
    def _():
        fields = ds.get_hash_fields()
        assert 'dhash_8' in fields, f"dhash_8 not in {fields}"

    _()

    @test("get_hash_stats")
    def _():
        stats = ds.get_hash_stats()
        assert stats['total_samples'] >= 10
        assert 'dhash_8' in stats['fields']

    _()

    @test("find_hash_duplicates")
    def _():
        result = client.find_hash_duplicates(ds._id, 'dhash_8')
        # All 3 samples have the same hash, so there should be 1 group
        assert result['total_groups'] == 1
        assert result['groups'][0]['size'] == 3

    _()

    @test("sample_duplicates")
    def _():
        # All 3 samples share the same dhash_8, so the seed's duplicates are all 3.
        result = client.sample_duplicates(ds._id, sample_ids[0], field_name='dhash_8')
        assert sample_ids[0] in result['sample_ids']
        assert len(result['sample_ids']) == 3

    _()

    # --- EMBEDDINGS ---
    print("\n--- Embeddings ---")

    @test("upload_embeddings")
    def _():
        embs = [
            {'sample_id': sid, 'vector': [float(i) / 10 for i in range(8)], 'field_name': 'test_emb'}
            for sid in sample_ids[:5]
        ]
        client.upload_embeddings(ds._id, embs)

    _()

    @test("get_embedding_fields")
    def _():
        fields = ds.get_embedding_fields()
        assert 'test_emb' in fields, f"test_emb not in {fields}"

    _()

    @test("get_vectors")
    def _():
        result = client.get_vectors(ds._id, field_name='test_emb', limit=5)
        assert result['total'] == 5, f"Expected total=5, got {result['total']}"
        assert len(result['items']) == 5

    _()

    # --- SIMILARITY SEARCH ---
    print("\n--- Similarity Search ---")

    @test("similarity_search by sample_id")
    def _():
        results = client.similarity_search(ds._id, sample_id=sample_ids[0], field_name='test_emb', limit=3)
        assert len(results) >= 1

    _()

    # --- EMBEDDING CONCEPTS ---
    print("\n--- Embedding Concepts ---")

    # Separable embeddings on a dedicated field so a seed concept can discriminate:
    # samples[0:3] point along axis 0 (group A), samples[3:5] along axis 1 (group B).
    @test("concept: upload separable embeddings")
    def _():
        embs = []
        for j, sid in enumerate(sample_ids[:5]):
            v = [0.0] * 8
            v[0 if j < 3 else 1] = 1.0
            embs.append({'sample_id': sid, 'vector': v, 'field_name': 'concept_emb'})
        client.upload_embeddings(ds._id, embs)

    _()

    concept_state = {}

    @test("create_concept (seed from group A)")
    def _():
        c = ds.create_concept('catlike', seed_ids=sample_ids[:3], field='concept_emb', threshold=0.75)
        assert c['id'] is not None
        assert c['kind'] == 'seed'
        assert c['scored_count'] == 5, f"expected 5 scored, got {c['scored_count']}"
        concept_state['id'] = c['id']

    _()

    @test("concepts() lists the new concept with a parsed spec (not double-encoded)")
    def _():
        match = [c for c in ds.concepts() if c['id'] == concept_state['id']]
        assert match, 'created concept not in list'
        spec = match[0].get('spec')
        assert isinstance(spec, dict), f'spec should be an object, got {type(spec).__name__}'
        assert spec.get('seed_ids') == sample_ids[:3], f"spec.seed_ids round-trip: {spec.get('seed_ids')}"

    _()

    @test("concept_preview discriminates A (>0.75) from B (<0.75)")
    def _():
        hi = ds.concept_preview(concept_state['id'], op='>', threshold=0.75)
        assert hi['matched'] == 3, f"expected 3 above 0.75, got {hi['matched']}"
        lo = ds.concept_preview(concept_state['id'], op='<', threshold=0.75)
        assert lo['matched'] == 2, f"expected 2 below 0.75, got {lo['matched']}"

    _()

    @test("concept_exclude filter drops group A")
    def _():
        flt = {
            'logic': 'AND',
            'filters': [
                {'field': 'id', 'operator': 'in', 'value': sample_ids[:5]},
                {
                    'field': '_concept',
                    'operator': 'concept_exclude',
                    'value': {'concept_id': concept_state['id'], 'op': '>', 'threshold': 0.75},
                },
            ],
        }
        res = client.query_samples(ds._id, filters=flt, limit=50)
        ids = {it['id'] for it in res['items']}
        assert ids == set(sample_ids[3:5]), f"expected group B only, got {sorted(ids)}"

    _()

    @test("concept_select filter keeps group A")
    def _():
        flt = {
            'logic': 'AND',
            'filters': [
                {'field': 'id', 'operator': 'in', 'value': sample_ids[:5]},
                {
                    'field': '_concept',
                    'operator': 'concept_select',
                    'value': {'concept_id': concept_state['id'], 'op': '>', 'threshold': 0.75},
                },
            ],
        }
        res = client.query_samples(ds._id, filters=flt, limit=50)
        ids = {it['id'] for it in res['items']}
        assert ids == set(sample_ids[:3]), f"expected group A only, got {sorted(ids)}"

    _()

    @test("create_concept kind=probe (logistic fit) discriminates groups")
    def _():
        c = ds.create_concept(
            'catprobe',
            seed_ids=sample_ids[:3],  # positives = group A
            neg_ids=sample_ids[3:5],  # negatives = group B
            field='concept_emb',
            kind='probe',
            threshold=0.5,
        )
        assert c['kind'] == 'probe'
        assert c['scored_count'] == 5, f"expected 5 scored, got {c['scored_count']}"
        hi = ds.concept_preview(c['id'], op='>', threshold=0.5)
        assert hi['matched'] == 3, f"probe: expected 3 above 0.5, got {hi['matched']}"
        ds.delete_concept(c['id'])

    _()

    @test("rebuild_concept retunes threshold only")
    def _():
        c = ds.rebuild_concept(concept_state['id'], threshold=0.9)
        assert abs(c['threshold'] - 0.9) < 1e-9

    _()

    @test("rescore_concept incremental is a no-op when nothing new")
    def _():
        r = ds.rescore_concept(concept_state['id'], full=False)
        assert r['scored'] == 0, f"expected 0 new, got {r['scored']}"

    _()

    @test("refine_concept: keep/exclude marks move the boundary")
    def _():
        c = ds.create_concept('refineflip', seed_ids=sample_ids[:3], field='concept_emb', threshold=0.5)
        cid = c['id']

        def selected(thr=0.5):
            res = client.query_samples(
                ds._id,
                filters={
                    'logic': 'AND',
                    'filters': [
                        {'field': 'id', 'operator': 'in', 'value': sample_ids[:5]},
                        {
                            'field': '_concept',
                            'operator': 'concept_select',
                            'value': {'concept_id': cid, 'op': '>', 'threshold': thr},
                        },
                    ],
                },
                limit=50,
            )
            return {it['id'] for it in res['items']}

        assert selected() == set(sample_ids[:3]), 'seed concept should select group A'
        # keep a group-A example (negative), exclude a group-B example (positive)
        out = ds.refine_concept(cid, keep_ids=[sample_ids[0]], exclude_ids=[sample_ids[3]])
        assert out['kind'] == 'probe', f"refine should make it a probe, got {out['kind']}"
        after = selected()  # sigmoid midpoint 0.5 = the probe's decision boundary
        assert sample_ids[3] in after, f'excluded mark should now be selected: {sorted(after)}'
        assert sample_ids[0] not in after, f'kept mark should be dropped: {sorted(after)}'
        ds.delete_concept(cid)

    _()

    @test("refine_concept persists corrections in spec and preserves the cut level")
    def _():
        c = ds.create_concept('refinepersist', seed_ids=sample_ids[:3], field='concept_emb')
        cid = c['id']
        before = ds.concept_preview(cid, op='>', threshold=0.75)['matched']
        out = ds.refine_concept(cid, keep_ids=[sample_ids[0], sample_ids[1]], threshold=0.75, self_train=True)
        # corrections are saved + retrievable via the concept's spec (the user's "how many / what")
        match = [x for x in ds.concepts() if x['id'] == cid][0]
        assert sorted((match.get('spec') or {}).get('neg_ids', [])) == sorted([sample_ids[0], sample_ids[1]]), (
            f"kept marks not persisted: {(match.get('spec') or {}).get('neg_ids')}"
        )
        # cut level preserved: ~same number hidden at the new saved threshold as before
        after = ds.concept_preview(cid, op='>', threshold=out['threshold'])['matched']
        assert abs(after - before) <= 1, f'cut level not preserved: before={before} after={after}'
        ds.delete_concept(cid)

    _()

    @test("delete_concept removes it")
    def _():
        ds.delete_concept(concept_state['id'])
        assert not any(c['id'] == concept_state['id'] for c in ds.concepts())

    _()

    # --- DELETING A USER WHO IS REFERENCED BY DATA ---
    # dataset_access.granted_by_user_id (and the samples/storage_configs *_by_user_id columns)
    # are NO ACTION, so DELETE /users/{id} for an admin who ever granted dataset access raised
    # an unhandled FK violation -> 500. The grant path is the one
    # the app actually writes today (samples.created_by/modified_by are never populated).
    print("\n--- User delete ---")

    import requests as _requests

    def _remove_user_if_present(name):
        for u in client.session.get(f"{client.base_url}/users").json():
            if u['username'] == name:
                client.session.delete(f"{client.base_url}/users/{u['id']}")

    @test("deleting an admin who granted dataset access is a 409 with guidance, and works once the grant is gone")
    def _():
        for name in ('sdk_fk_admin', 'sdk_fk_target'):
            _remove_user_if_present(name)
        r = client.session.post(
            f"{client.base_url}/users",
            json={'username': 'sdk_fk_target', 'password': 'target-pass-1', 'role': 'guest'},
        )
        assert r.status_code == 200, r.text
        target_id = r.json()['id']
        r = client.session.post(
            f"{client.base_url}/users",
            json={'username': 'sdk_fk_admin', 'password': 'fkadmin-pass-1', 'role': 'admin'},
        )
        assert r.status_code == 200, r.text
        admin_id = r.json()['id']
        other = _requests.Session()
        r = other.post(f"{client.base_url}/auth/login", json={'username': 'sdk_fk_admin', 'password': 'fkadmin-pass-1'})
        assert r.status_code == 200, r.text
        r = other.post(
            f"{client.base_url}/auth/change-password",
            json={'current_password': 'fkadmin-pass-1', 'new_password': 'fkadmin-pass-2'},
        )
        assert r.status_code == 200, r.text  # clears must_change_password so the grant route opens
        r = other.post(f"{client.base_url}/datasets/{ds._id}/access", json={'user_id': target_id})
        assert r.status_code in (200, 201), r.text  # granted_by_user_id = sdk_fk_admin
        r = client.session.delete(f"{client.base_url}/users/{admin_id}")
        assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text[:200]}"
        assert 'deactivate' in r.json()['detail'].lower()
        r = client.session.delete(f"{client.base_url}/datasets/{ds._id}/access/{target_id}")
        assert r.status_code in (200, 204), r.text
        r = client.session.delete(f"{client.base_url}/users/{admin_id}")
        assert r.status_code == 200, r.text
        r = client.session.delete(f"{client.base_url}/users/{target_id}")
        assert r.status_code == 200, r.text

    _()

    # --- CLIENT METHODS ---
    print("\n--- Client Metadata ---")

    @test("get_datasets")
    def _():
        datasets = client.get_datasets()
        assert any(d['name'] == 'sdk-test' for d in datasets)

    _()

    @test("query_samples")
    def _():
        result = client.query_samples(ds._id, limit=5)
        assert len(result['items']) == 5

    _()

    @test("query_samples with filters")
    def _():
        result = client.query_samples(ds._id, limit=100, filters={"field": "category", "operator": "=", "value": "a"})
        assert result['total'] == 5

    _()

    @test("query_samples slim mode")
    def _():
        result = client.query_samples(ds._id, limit=5, fields='filepath,id', slim=True, skip_count=True)
        assert len(result['items']) == 5

    _()

    # --- NAMED EMBEDDINGS AT CREATION ---
    print("\n--- Named Embeddings at Creation ---")

    @test("add_samples with named embeddings")
    def _():
        s = Sample(
            filepath='s3://test/emb_test.jpg',
            metadata={'emb_test': True},
            # Suite-specific names: embedding dimension is fixed per field NAME across every dataset
            # on the server, so an 8-d 'clip' is refused wherever a real 512-d 'clip' exists.
            embeddings={'sdk_test_clip': [0.1] * 8, 'sdk_test_dinov2': [0.2] * 16},
        )
        result = ds.add_samples([s])
        assert len(result['ids']) == 1
        try:
            # Verify both fields exist
            fields = ds.get_embedding_fields()
            assert 'sdk_test_clip' in fields, f"sdk_test_clip not in {fields}"
            assert 'sdk_test_dinov2' in fields, f"sdk_test_dinov2 not in {fields}"
        finally:
            ds.delete_sample(result['ids'][0])

    _()

    @test("Sample takes named embeddings only (the single embedding= is gone in 2.0)")
    def _():
        s = Sample(filepath='s3://test/emb_named.jpg', embeddings={'clip': [0.5] * 8})
        assert s.to_dict()['embeddings'] == {'clip': [0.5] * 8}
        try:
            Sample(filepath='s3://test/emb_single.jpg', embedding=[0.5] * 8)  # type: ignore[call-arg]
        except TypeError:
            pass
        else:
            raise AssertionError("Sample(embedding=...) was accepted")

    _()

    # --- SCATTER / LASSO COORDINATE INVARIANT ---
    # The scatter draws embedding_viz_rank.x/y; lookup, select-region and the
    # embedding_polygon filter read embeddings.x/y. The database keeps
    # the two identical (trigger on embeddings), so re-projecting coordinates WITHOUT a
    # re-rank must never let a lasso select points the plot did not draw (without that trigger the two
    # could disagree completely, with no error anywhere).
    print("\n--- Scatter coordinate invariant ---")

    viz_ids = sample_ids[:5]
    layout_a = {sid: (float(i), 0.0) for i, sid in enumerate(viz_ids)}
    layout_b = {sid: (float(i) + 100.0, 100.0) for i, sid in enumerate(viz_ids)}
    box_a = [[-1.0, -1.0], [5.0, -1.0], [5.0, 1.0], [-1.0, 1.0]]
    box_b = [[99.0, 99.0], [105.0, 99.0], [105.0, 101.0], [99.0, 101.0]]

    def _by_id(points):
        return {p['sample_id']: (round(p['x'], 3), round(p['y'], 3)) for p in points}

    def _scatter():
        return client.query_embeddings(ds._id, field='viz_sync_emb', sample_pct=100)

    def _lasso_total(polygon):
        resp = client.session.post(
            f"{client.base_url}/datasets/{ds._id}/embeddings/select-region",
            json={'field': 'viz_sync_emb', 'polygon': polygon},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()['total']

    def _polygon_filter_ids(polygon):
        resp = client.session.post(
            f"{client.base_url}/datasets/{ds._id}/samples/query",
            json={
                'filters': {'field': 'viz_sync_emb', 'operator': 'embedding_polygon', 'value': polygon},
                'limit': 100,
            },
        )
        assert resp.status_code == 200, resp.text
        return sorted(item['id'] for item in resp.json()['items'])

    @test("scatter: coordinates + full re-rank populate the rank table")
    def _():
        client.upload_embeddings(
            ds._id,
            [
                {'sample_id': sid, 'vector': [float(i) / 10 for i in range(8)], 'field_name': 'viz_sync_emb'}
                for sid in viz_ids
            ],
        )
        client.upload_coordinates(
            ds._id,
            'viz_sync_emb',
            [{'sample_id': sid, 'x': x, 'y': y} for sid, (x, y) in layout_a.items()],
        )
        ranked = client.recompute_viz_rank(ds._id, 'viz_sync_emb', full=True)['ranked']
        assert ranked == 5, f"expected 5 ranked points, got {ranked}"
        q = _scatter()
        assert _by_id(q['points']) == layout_a, f"scatter != layout A: {q['points']}"
        assert q.get('rank_stale') is False, q

    _()

    @test("scatter, lookup, lasso and embedding_polygon agree after coords are re-uploaded WITHOUT a re-rank")
    def _():
        client.upload_coordinates(
            ds._id,
            'viz_sync_emb',
            [{'sample_id': sid, 'x': x, 'y': y} for sid, (x, y) in layout_b.items()],
        )
        q = _scatter()
        scatter = _by_id(q['points'])
        lookup = _by_id(client.lookup_embedding_coordinates(ds._id, viz_ids, field='viz_sync_emb')['points'])
        assert lookup == layout_b, f"lookup != layout B: {lookup}"
        assert scatter == lookup, f"scatter draws {scatter} but lookup/lasso use {lookup}"
        assert q.get('rank_stale') is True, "coverage ranks must be flagged stale after a re-projection"
        assert _lasso_total(box_b) == 5, "lasso around the drawn points must select all of them"
        assert _lasso_total(box_a) == 0, "lasso around the OLD layout must select nothing"
        assert _polygon_filter_ids(box_b) == sorted(viz_ids)
        assert _polygon_filter_ids(box_a) == []

    _()

    @test("scatter: a full re-rank clears rank_stale and keeps the current layout")
    def _():
        client.recompute_viz_rank(ds._id, 'viz_sync_emb', full=True)
        q = _scatter()
        assert q.get('rank_stale') is False, q
        assert _by_id(q['points']) == layout_b

    _()

    # --- SAMPLE INTERNALS ---
    print("\n--- Sample Internals ---")

    @test("Sample to_dict location tuple → GeoJSON")
    def _():
        s = Sample(filepath='test.jpg', location=(-122.42, 37.77))
        d = s.to_dict()
        assert d['location']['type'] == 'Point'
        assert d['location']['coordinates'] == [-122.42, 37.77]

    _()

    @test("Sample to_dict with array_fields")
    def _():
        s = Sample(filepath='test.jpg', array_fields={'labels': ['a', 'b']})
        d = s.to_dict()
        assert d['array_fields'] == {'labels': ['a', 'b']}

    _()

    @test("Sample __setattr__ routes core fields correctly")
    def _():
        s = Sample(filepath='test.jpg')
        s.tags = ['x']
        s.metadata = {'k': 'v'}
        s.embeddings = {'clip': [1.0]}
        assert s.tags == ['x']
        assert s.metadata == {'k': 'v'}
        assert s.embeddings == {'clip': [1.0]}
        # metadata should NOT contain embeddings
        assert 'embeddings' not in s.metadata

    _()

    @test("Sample __setattr__ routes unknown fields to metadata")
    def _():
        s = Sample(filepath='test.jpg')
        s.brightness = 0.75
        assert s.metadata['brightness'] == 0.75

    _()

    @test("dataset[filepath] KeyError for missing")
    def _():
        try:
            ds['s3://test/nonexistent.jpg']
            assert False, "Should have raised KeyError"
        except KeyError:
            pass

    _()

    # --- QUERY CHAINING ---
    print("\n--- Query Chaining ---")

    @test("view chaining: filter().order_by().limit()")
    def _():
        view = ds.filter(category='a').order_by('index').limit(3)
        items = view.to_list()
        assert len(items) == 3
        # Should be sorted by index
        indices = [s.metadata.get('index') for s in items]
        assert indices == sorted(indices), f"Not sorted: {indices}"

    _()

    @test("view chaining: filter().count()")
    def _():
        count = ds.filter(category='b').count()
        assert count == 5, f"Expected 5, got {count}"

    _()

    @test("view count/len apply skip() and limit()")
    def _():
        view = ds.filter(category='b')
        for windowed, expected in ((view.limit(3), 3), (view.skip(4), 1), (view.skip(2).limit(10), 3)):
            assert windowed.count() == len(windowed) == len(windowed.to_list()) == expected, (
                f"count {windowed.count()}, rows {len(windowed.to_list())}, expected {expected}"
            )

    _()

    @test("view.histogram_values()")
    def _():
        hist = ds.all().histogram_values('score', bins=5)
        assert len(hist) > 0, "Empty histogram"

    _()

    @test("view.histogram_values() with a filter too large for a URL")
    def _():
        # A saved selection of 20,000 ids (here 5 samples and ids no sample has) is ~200 KB of JSON, past
        # the server's 64 KB request line: a histogram sent with the filter in its URL got HTTP 400.
        ids = ds.filter(category='a').ids()
        s = ds.samples
        view = s[s.id.is_in(ids + list(range(10_000_000, 10_020_000 - len(ids))))]
        hist = view.histogram_values('score', bins=5)
        assert sum(bucket['count'] for bucket in hist) == len(ids) == 5, hist

    _()

    @test("dataset.query() string syntax")
    def _():
        view = ds.query("category = 'a'")
        count = len(view)
        assert count == 5, f"Expected 5, got {count}"

    _()

    @test("dataset.query() checks fields: an unknown one is an error naming it, with a suggestion")
    def _():
        try:
            ds.query("categroy = 'a'")
        except ValueError as error:
            assert "line 1, column 1: unknown field 'categroy'" in str(error) and "'category'" in str(error), error
        else:
            raise AssertionError("a misspelt field was accepted")

    _()

    @test("dataset.query() lists with any separators, and create_view(query=)")
    def _():
        values = sorted({s.metadata.get("category") for s in ds.all() if s.metadata.get("category")})
        pasted = "category in (" + "  ".join(f'"{v}"' for v in values[:2]) + ")"
        assert ds.query(pasted).count() == ds.filter(category__in=values[:2]).count() > 0
        view = ds.create_view("query-language view", query=pasted + "\nAND category != 'zzz'")
        try:
            stages = view["content"]["viewStages"]
            assert [stage["filter"]["operator"] for stage in stages] == ["in", "!="], stages
            assert ds.load_view("query-language view").count() == ds.query(pasted).count()
        finally:
            ds.delete_view(view["id"])

    _()

    @test("dataset.query() IS NULL / IS NOT NULL treat a missing key as null")
    def _():
        # Some samples added earlier have no category: both spellings must agree with filter().
        for query, is_null in (("category IS NULL", True), ("category IS NOT NULL", False)):
            got, expected = ds.query(query).count(), ds.filter(category__is_null=is_null).count()
            assert got == expected, f"{query}: {got}, filter(category__is_null={is_null}): {expected}"
        assert ds.query("category IS NOT NULL").count() > 0

    _()

    # --- VERSION DIFF BETWEEN TWO VERSIONS ---
    print("\n--- Version-to-Version Diff ---")

    @test("diff between two versions")
    def _():
        v1 = ds.create_version('v-diff-a')
        ds.add_samples([Sample(filepath='s3://test/diff_between.jpg', metadata={'diff': True})])
        v2 = ds.create_version('v-diff-b')
        diff = client.get_version_diff(ds._id, v1['id'], v2['id'])
        assert diff['added'] >= 1, f"Expected added >= 1, got {diff}"
        # Clean up versions
        client.delete_version(ds._id, v2['id'])
        client.delete_version(ds._id, v1['id'])

    _()

    # --- SIMILARITY SEARCH WITH VECTOR ---
    print("\n--- Similarity Search (vector) ---")

    @test("similarity_search by raw vector")
    def _():
        results = client.similarity_search(ds._id, vector=[0.1] * 8, field_name='test_emb', limit=3)
        assert len(results) >= 1

    _()

    # --- PASSWORD POLICY (server-side; the modal's minLength alone was bypassable) ---
    print("\n--- Password policy ---")

    def _delete_user_if_present(name):
        for u in client.session.get(f"{client.base_url}/users").json():
            if u['username'] == name:
                client.session.delete(f"{client.base_url}/users/{u['id']}")

    @test("change-password rejects a short password and the username itself; admin's password is untouched")
    def _():
        for bad in ('short', 'admin', 'ADMIN', ''):
            resp = client.session.post(
                f"{client.base_url}/auth/change-password",
                json={'current_password': 'admin123', 'new_password': bad},
            )
            assert resp.status_code == 400, f"{bad!r}: {resp.status_code} {resp.text[:120]}"
        assert client.whoami()['username'] == 'admin'  # the session is intact, nothing was changed

    _()

    @test("create-user and admin reset-password enforce the same policy")
    def _():
        _delete_user_if_present('sdk_policy_user')
        r = client.session.post(
            f"{client.base_url}/users",
            json={'username': 'sdk_policy_user', 'password': 'short', 'role': 'guest'},
        )
        assert r.status_code == 400, r.text
        r = client.session.post(
            f"{client.base_url}/users",
            json={'username': 'sdk_policy_user', 'password': 'longenough1', 'role': 'guest'},
        )
        assert r.status_code == 200, r.text
        uid = r.json()['id']
        r = client.session.post(f"{client.base_url}/users/{uid}/reset-password", json={'password': 'sdk_policy_user'})
        assert r.status_code == 400, r.text  # equals the username
        r = client.session.delete(f"{client.base_url}/users/{uid}")
        assert r.status_code == 200, r.text

    _()

    # --- ERROR HANDLING ---
    print("\n--- Error Handling ---")

    @test("delete nonexistent sample raises")
    def _():
        try:
            ds.delete_sample(999999999)
            assert False, "Should have raised"
        except Exception:
            pass

    _()

    @test("get nonexistent dataset by id raises")
    def _():
        try:
            client.get_dataset_by_id(999999999)
            assert False, "Should have raised"
        except Exception:
            pass

    _()

    @test("similarity_search without sample_id or vector raises")
    def _():
        try:
            client.similarity_search(ds._id)
            assert False, "Should have raised"
        except ValueError:
            pass

    _()

    # =====================================================================
    # EXTENDED COVERAGE
    # End-to-end tests for recently-regressed filter operators, view
    # aggregations, field ops, and bulk per-sample writes. These assert
    # observable behavior (round-trips, partitions, ground-truth math), not
    # implementation details. A dedicated freshly-seeded fixture (ext_*) is
    # used so earlier tests' mutations (e.g. img_2's score -> 9.99) can't skew
    # results, and its locations sit far from the original seed to avoid
    # geo-query collisions.
    # =====================================================================
    print("\n--- Extended: Fixture ---")

    def _ext_meta(i):
        m = {'grp': 'x' if i < 5 else 'y', 'val': float(i)}
        if i == 0:
            m['sparse_key'] = 'only here'  # present on EXACTLY one sample
        return m

    ext_ids = ds.add_samples(
        [
            Sample(
                filepath=f's3://test/ext_{i}.jpg',
                metadata=_ext_meta(i),
                location=(10.0 + i * 0.01, 10.0 + i * 0.01),
            )
            for i in range(10)
        ],
    )['ids']
    # A null-location sample — geo filters must never return it.
    ext_null_id = ds.add_samples([Sample(filepath='s3://test/ext_null.jpg', metadata={'grp_null': True})])['ids'][0]
    total = ds.all().count()  # stable for the rest of the run (no test adds/removes samples)

    print("\n--- Extended: Filter Operators ---")

    @test("filter: id != excludes exactly one (IS DISTINCT FROM, native column)")
    def _():
        view = ds.samples[ds.samples.id != ext_ids[3]]
        assert view.count() == total - 1, f"Expected {total - 1}, got {view.count()}"
        assert ext_ids[3] not in view.ids()

    _()

    @test("filter: id not_in excludes listed ids; is_in is the complement")
    def _():
        excl = ext_ids[:3]
        view = ds.samples[ds.samples.id.not_in(excl)]
        assert view.count() == total - 3, f"Expected {total - 3}, got {view.count()}"
        assert not (set(excl) & set(view.ids())), "Excluded ids leaked into not_in result"
        incl = ds.samples[ds.samples.id.is_in(excl)]
        assert set(incl.ids()) == set(excl), f"is_in mismatch: {incl.ids()}"

    _()

    @test("filter: created_at range + != (ISO->datetime coercion, tz 'Z')")
    def _():
        past = '2000-01-01T00:00:00Z'
        assert ds.samples[ds.samples.created_at > past].count() == total
        assert ds.samples[ds.samples.created_at < past].count() == 0
        # != a never-matching timestamp keeps every row (IS DISTINCT FROM)
        assert ds.samples[ds.samples.created_at != past].count() == total

    _()

    @test("filter: malformed created_at raises (no silent match-all)")
    def _():
        try:
            ds.samples[ds.samples.created_at > 'not-a-real-date'].count()
            assert False, "Malformed datetime should raise, not match all"
        except Exception:
            pass

    _()

    @test("filter: exists matches only the sparse-key sample (not all rows)")
    def _():
        view = ds.samples[ds.samples.sparse_key.exists()]
        assert view.count() == 1, f"exists matched {view.count()} (regression: matched all?)"
        assert view.ids() == [ext_ids[0]]
        assert ds.samples[ds.samples.grp.exists()].count() == 10  # present on all ext samples

    _()

    @test("filter: not_exists is the complement of exists (partition)")
    def _():
        present = client.query_samples(ds._id, limit=1, filters={'field': 'sparse_key', 'operator': 'exists'})['total']
        absent = client.query_samples(ds._id, limit=1, filters={'field': 'sparse_key', 'operator': 'not_exists'})[
            'total'
        ]
        assert present == 1, f"exists total {present}"
        assert absent == total - 1, f"not_exists total {absent}"
        assert present + absent == total

    _()

    @test("filter: geo_within selects enclosed points; excludes null + outside")
    def _():
        # Box around ext_0..ext_4 only; ring left unclosed (backend must auto-close).
        poly = [[9.995, 9.995], [10.045, 9.995], [10.045, 10.045], [9.995, 10.045]]
        inside = ds.samples[geo_within(poly)]
        assert inside.count() == 5, f"Expected 5 enclosed, got {inside.count()}"
        assert set(inside.ids()) == set(ext_ids[:5])
        assert ext_null_id not in inside.ids()
        ocean = [[0.0, 0.0], [0.0, 0.001], [0.001, 0.001], [0.001, 0.0]]
        assert ds.samples[geo_within(ocean)].count() == 0

    _()

    @test("filter: in_selection token round-trips and ANDs correctly")
    def _():
        def make_token(filt):
            r = client.session.post(f"{client.base_url}/datasets/{ds._id}/selection_token", json={'filters': filt})
            r.raise_for_status()
            return r.json()

        tok = make_token({'field': 'grp', 'operator': '=', 'value': 'x'})
        assert tok['count'] == 5, f"token count {tok['count']}"
        sel = {'field': '_sel', 'operator': 'in_selection', 'value': tok['token']}
        assert client.query_samples(ds._id, limit=1, filters=sel)['total'] == 5
        # token (grp x) AND grp == y -> 0 (AND across a token and a normal filter)
        both = {'logic': 'AND', 'filters': [sel, {'field': 'grp', 'operator': '=', 'value': 'y'}]}
        assert client.query_samples(ds._id, limit=1, filters=both)['total'] == 0
        # an empty source filter yields a token whose in_selection count is 0
        empty = make_token({'field': 'grp', 'operator': '=', 'value': 'nope'})
        assert empty['count'] == 0
        sel_e = {'field': '_sel', 'operator': 'in_selection', 'value': empty['token']}
        assert client.query_samples(ds._id, limit=1, filters=sel_e)['total'] == 0

    _()

    print("\n--- Extended: Aggregations ---")

    @test("aggregate: sum/mean/std match Python over the same values")
    def _():
        import statistics

        gx = ds.samples[ds.samples.grp == 'x']
        vals = [v for v in gx.values('val') if v is not None]
        assert len(vals) == 5, f"fixture mismatch: {vals}"
        assert abs(gx.sum('val') - sum(vals)) < 1e-6
        assert abs(gx.mean('val') - statistics.mean(vals)) < 1e-6
        assert abs(gx.std('val') - statistics.stdev(vals)) < 1e-6  # sample stddev

    _()

    @test("aggregate: quantiles span min..max and are monotonic")
    def _():
        gx = ds.samples[ds.samples.grp == 'x']
        q = gx.quantiles('val', [0.0, 0.5, 1.0])
        assert q[0.0] == 0.0 and q[1.0] == 4.0, f"q={q}"
        assert q[0.0] <= q[0.5] <= q[1.0]
        assert set(gx.quantiles('val').keys()) == {0.25, 0.5, 0.75}  # default quartiles

    _()

    @test("aggregate: multi-op in one request, scoped by the view filter")
    def _():
        gx = ds.samples[ds.samples.grp == 'x']
        res = gx.aggregate(
            [
                {'field': 'val', 'op': 'mean'},
                {'field': 'grp', 'op': 'count_values'},
            ],
        )
        assert len(res) == 2
        assert abs(res[0]['value'] - 2.0) < 1e-6
        counts = {r['value']: r['count'] for r in res[1]['value']}
        assert counts == {'x': 5}, f"count_values not view-scoped: {counts}"

    _()

    @test("view.schema() reflects the fixture fields")
    def _():
        names = [f['name'] for f in ds.all().schema()]
        assert 'grp' in names and 'val' in names, f"schema missing fields: {names}"

    _()

    print("\n--- Extended: View Set/Shape Ops ---")

    @test("view.exclude is the complement of filter (NOT on a present key)")
    def _():
        ext_all = ds.samples[(ds.samples.grp == 'x') | (ds.samples.grp == 'y')]
        assert ext_all.count() == 10
        only_y = ext_all.exclude(ds.samples.grp == 'x')
        assert only_y.count() == 5
        assert all(v == 'y' for v in only_y.values('grp'))
        assert ext_all.filter(ds.samples.grp == 'x').count() + only_y.count() == 10

    _()

    @test("view.skip/limit paginate into disjoint, contiguous pages")
    def _():
        ext_all = ds.samples[(ds.samples.grp == 'x') | (ds.samples.grp == 'y')].order_by('val')
        page1 = ext_all.limit(3).ids()
        page2 = ext_all.skip(3).limit(3).ids()
        assert len(page1) == 3 and len(page2) == 3
        assert not (set(page1) & set(page2)), "Pages overlap"
        assert ext_all.offset(3).limit(3).ids() == page2  # offset aliases skip

    _()

    @test("view.select_fields returns valid projected samples")
    def _():
        rows = ds.samples[ds.samples.grp == 'x'].select_fields(['id', 'filepath']).to_list()
        assert len(rows) == 5
        assert all(r.id is not None and r.filepath for r in rows)

    _()

    print("\n--- Extended: Field Ops (round-trip) ---")

    @test("clone_field -> rename_field -> clear_field round-trip")
    def _():
        ds.clone_field('val', 'val_copy')
        s0 = ds['s3://test/ext_0.jpg']
        assert s0.metadata.get('val_copy') == 0.0, f"clone failed: {s0.metadata}"
        assert s0.metadata.get('val') == 0.0, "clone changed the original field"
        ds.rename_field('val_copy', 'val_renamed')
        s0 = ds['s3://test/ext_0.jpg']
        assert 'val_copy' not in s0.metadata, "old name survived rename"
        assert s0.metadata.get('val_renamed') == 0.0, "rename lost the value"
        ds.clear_field('val_renamed')
        s0 = ds['s3://test/ext_0.jpg']
        assert 'val_renamed' not in s0.metadata, "clear left the key"
        assert s0.metadata.get('val') == 0.0, "clear touched the original field"

    _()

    @test("explicit field type correction preserves stored values")
    def _():
        field_name = 'type_correction_probe'
        try:
            ds.set_values([ext_ids[0]], field_name, 'placeholder')
            ds.set_values([ext_ids[0]], field_name, 3)
            before = next(field for field in ds.get_schema() if field['name'] == field_name)
            assert before['declared_type'] == 'string'
            result = ds.set_field_type(field_name, 'number')
            assert result['field_type'] == 'number'
            after = next(field for field in ds.get_schema() if field['name'] == field_name)
            assert after['declared_type'] == 'number'
            assert ds['s3://test/ext_0.jpg'].metadata[field_name] == 3
        finally:
            ds.clear_field(field_name)

    _()

    print("\n--- Extended: Bulk Per-Sample Writes ---")

    @test("set_values_per_sample writes a distinct value per id")
    def _():
        n = ds.set_values_per_sample('rank', {ext_ids[0]: 10, ext_ids[1]: 20, ext_ids[2]: 30})
        assert n == 3, f"Expected 3 updated, got {n}"
        assert ds['s3://test/ext_0.jpg'].metadata.get('rank') == 10
        assert ds['s3://test/ext_1.jpg'].metadata.get('rank') == 20
        assert ds['s3://test/ext_2.jpg'].metadata.get('rank') == 30

    _()

    @test("set_values_per_sample_multi merges keys, preserves existing metadata")
    def _():
        n = ds.set_values_per_sample_multi({ext_ids[0]: {'aa': 1, 'bb': 2}})
        assert n == 1, f"Expected 1 updated, got {n}"
        s0 = ds['s3://test/ext_0.jpg']
        assert s0.metadata.get('aa') == 1 and s0.metadata.get('bb') == 2
        assert s0.metadata.get('val') == 0.0, "existing metadata was clobbered"

    _()

    @test("merge_samples merges by filepath; skip_existing protects existing")
    def _():
        src = [
            Sample(filepath='s3://test/ext_0.jpg', metadata={'merged_tag': 'yes'}),
            Sample(filepath='s3://test/ext_1.jpg', metadata={'merged_tag': 'yes'}),
        ]
        assert ds.merge_samples(src) == 2
        assert ds['s3://test/ext_0.jpg'].metadata.get('merged_tag') == 'yes'
        assert ds['s3://test/ext_0.jpg'].metadata.get('val') == 0.0  # preserved
        # skip_existing must NOT overwrite the present key
        src2 = [Sample(filepath='s3://test/ext_0.jpg', metadata={'merged_tag': 'no'})]
        assert ds.merge_samples(src2, skip_existing=True) == 0
        assert ds['s3://test/ext_0.jpg'].metadata.get('merged_tag') == 'yes'

    _()

    @test("client.bulk_tag_samples adds then removes tags")
    def _():
        ids4 = ext_ids[:4]
        client.bulk_tag_samples(ds._id, ids4, add_tags=['bulk'])
        assert 'bulk' in ds['s3://test/ext_0.jpg'].tags
        client.bulk_tag_samples(ds._id, ids4, remove_tags=['bulk'])
        assert 'bulk' not in ds['s3://test/ext_0.jpg'].tags

    _()

    print("\n--- Extended: Quality + Lifecycle ---")

    @test("compute_hardness: uniform logits -> 1.0, confident -> ~0")
    def _():
        ds.set_values_per_sample_multi(
            {
                ext_ids[6]: {'logits': [1.0, 1.0, 1.0, 1.0]},  # maximally uncertain
                ext_ids[7]: {'logits': [10.0, 0.0, 0.0, 0.0]},  # confident
            },
        )
        res = ds.compute_hardness('logits')
        assert res['count'] == 2, f"Expected 2 scored, got {res['count']}"
        assert abs(ds['s3://test/ext_6.jpg'].metadata['hardness'] - 1.0) < 1e-6
        assert ds['s3://test/ext_7.jpg'].metadata['hardness'] < 0.2
        assert 'hardness' not in ds['s3://test/ext_8.jpg'].metadata  # no logits -> no key

    _()

    @test("Dataset.from_id loads the existing dataset without creating one")
    def _():
        before = len(client.get_datasets())
        d2 = Dataset.from_id(ds._id)
        assert d2._id == ds._id and d2.name == 'sdk-test'
        assert len(client.get_datasets()) == before, "from_id created a dataset"

    _()

    @test("Dataset.from_id raises for a missing id")
    def _():
        try:
            Dataset.from_id(999999999)
            assert False, "from_id should raise for a missing id"
        except Exception:
            pass

    _()

    # --- SORTED PAGING IS A TOTAL ORDER ---
    # OFFSET paging over `ORDER BY <field>` is only stable when the order is total: among
    # tied rows a bounded sort returns a different order per LIMIT/OFFSET, so the same
    # sample lands on two pages while another is never returned (a dataset sorted by a
    # 10-value field had 22% duplicated and 22% missing, with `total` correct).
    # Ties are the normal case: every categorical field, and created_at (one NOW() per
    # ingest batch). Own dataset so the counts elsewhere in this suite are untouched.
    print("\n--- Sorted paging ---")

    ds_sort = Dataset('sdk-test-sort')
    n_sort = 1500
    sort_ids: set = set()

    @test("sorted paging: seed 1500 rows with heavily tied sort keys (two ingest batches)")
    def _():
        for lo in (0, n_sort // 2):
            result = ds_sort.add_samples(
                [
                    Sample(
                        filepath=f's3://test/sort_{i}.jpg',
                        metadata={'bucket': 'abc'[i % 3], 'score': float(i % 7)},
                    )
                    for i in range(lo, lo + n_sort // 2)
                ],
            )
            sort_ids.update(result['ids'])
        assert len(sort_ids) == n_sort, f"expected {n_sort} ids, got {len(sort_ids)}"

    _()

    def _walk_sorted(sort_by, sort_order, key, limit=100):
        """Page the whole dataset by OFFSET the way the grid does; return (ids, keys)."""
        ids: list = []
        keys: list = []
        offset = 0
        while True:
            resp = client.session.post(
                f"{client.base_url}/datasets/{ds_sort._id}/samples/query",
                json={
                    'sort_by': sort_by,
                    'sort_order': sort_order,
                    'limit': limit,
                    'offset': offset,
                    'skip_count': True,
                },
            )
            assert resp.status_code == 200, resp.text
            items = resp.json()['items']
            ids.extend(s['id'] for s in items)
            keys.extend(key(s) for s in items)
            if len(items) < limit:
                return ids, keys
            offset += limit

    def _assert_total_order(label, ids, keys, descending=False):
        # The sort really applied (keys monotonic) AND every row appeared exactly once.
        pairs = zip(keys, keys[1:])
        assert all((a >= b) if descending else (a <= b) for a, b in pairs), f"{label}: not sorted"
        dups = len(ids) - len(set(ids))
        missing = len(sort_ids - set(ids))
        assert dups == 0 and missing == 0, f"{label}: fetched={len(ids)} duplicates={dups} missing={missing}"

    @test("sorted paging by a string field with 500-way ties: every row exactly once")
    def _():
        ids, keys = _walk_sorted('bucket', 'asc', lambda s: s['metadata']['bucket'])
        _assert_total_order('bucket asc', ids, keys)

    _()

    @test("sorted paging by a numeric field, descending: every row exactly once")
    def _():
        ids, keys = _walk_sorted('score', 'desc', lambda s: float(s['metadata']['score']))
        _assert_total_order('score desc', ids, keys, descending=True)

    _()

    @test("sorted paging by created_at (one timestamp per ingest batch): every row exactly once")
    def _():
        ids, keys = _walk_sorted('created_at', 'asc', lambda s: s['created_at'])
        _assert_total_order('created_at asc', ids, keys)

    _()

    @test("DatasetView.sort_by().iter_samples() (OFFSET-paged): every row exactly once")
    def _():
        samples = list(ds_sort.all().sort_by('bucket').iter_samples(batch_size=100))
        ids = [s.id for s in samples]
        _assert_total_order('view sort_by bucket', ids, [s.metadata['bucket'] for s in samples])

    _()

    @test("sorted paging: cleanup")
    def _():
        client.delete_dataset(ds_sort._id)

    _()

    # --- RANGE FILTER WITH A NUMERIC STRING (the Add-Filter editor sends strings) ---
    print("\n--- Numeric-string range filter ---")

    @test("range filter with a numeric STRING value compares numerically (no 500) and equals the numeric form")
    def _():
        def _ids(value):
            resp = client.session.post(
                f"{client.base_url}/datasets/{ds._id}/samples/query",
                json={'filters': {'field': 'score', 'operator': '>', 'value': value}, 'limit': 1000},
            )
            assert resp.status_code == 200, f"value={value!r}: {resp.status_code} {resp.text[:200]}"
            return sorted(s['id'] for s in resp.json()['items'])

        as_number = _ids(0.5)
        as_string = _ids('0.5')
        assert as_number, "fixture has samples with score > 0.5"
        assert as_string == as_number, f"string form {as_string} != numeric form {as_number}"

    _()

    # --- FILTERED SIMILARITY SEARCH IS COMPLETE ---
    # pgvector post-filters HNSW candidates: a plain scan hands back at most ef_search rows
    # and the WHERE is applied afterwards, so "find similar" + a selective filter silently
    # can return fewer matching rows than LIMIT. Use an isolated fixture big enough for
    # the planner to consider HNSW; a sequential scan would hide candidate exhaustion.
    # This checks result counts. The unit test separately pins iterative-scan settings
    # because candidate exhaustion depends on pgvector version and dataset size.
    print("\n--- Filtered similarity search ---")

    ds_ann = Dataset('sdk-test-ann')
    n_ann = 4000
    ann_ids: list = []

    @test("filtered search: seed 4000 vectors in 100 groups and build the HNSW index")
    def _():
        import random

        rnd = random.Random(7)
        for lo in range(0, n_ann, 1000):
            result = ds_ann.add_samples(
                [Sample(filepath=f's3://test/ann_{i}.jpg', metadata={'grp': i % 100}) for i in range(lo, lo + 1000)],
            )
            ann_ids.extend(result['ids'])
        client.upload_embeddings(
            ds_ann._id,
            [
                {'sample_id': sid, 'field_name': 'ann_emb', 'vector': [rnd.random() for _ in range(8)]}
                for sid in ann_ids
            ],
        )
        ds_ann.build_ann_index('ann_emb')
        assert len(ann_ids) == n_ann

    _()

    @test("filtered similarity search returns min(limit, matching) rows, not the ef_search leftovers")
    def _():
        # A BROAD filter (30%): the planner drives from the HNSW index and post-filters,
        # which is where the truncation lives (a selective filter is planned filter-first
        # and never truncates). ef_search=100 * 30% ~ 30 survivors for LIMIT 50 without
        # the iterative scan.
        flt = {'field': 'grp', 'operator': '<', 'value': 30}
        resp = client.session.post(
            f"{client.base_url}/datasets/{ds_ann._id}/samples/query",
            json={'filters': flt, 'limit': 1},
        )
        assert resp.status_code == 200, resp.text
        matching = resp.json()['total']
        assert matching == n_ann * 30 // 100, f"filter should match 30%: {matching}"
        results = client.similarity_search(
            ds_ann._id,
            sample_id=ann_ids[7],
            field_name='ann_emb',
            limit=50,
            filters=flt,
        )
        expected = min(50, matching)
        assert len(results) == expected, f"got {len(results)} results, expected {expected} (matching={matching})"
        grp = lambda r: (r['metadata'] if isinstance(r, dict) else r.metadata)['grp']  # noqa: E731
        assert all(grp(r) < 30 for r in results), "every result must satisfy the filter"

    _()

    @test("filtered search: cleanup")
    def _():
        client.delete_dataset(ds_ann._id)

    _()

    # --- DELETE EMBEDDINGS ---
    # One field's vectors are deleted for chosen samples (in several requests), the cleared samples are listed
    # as missing, new vectors are accepted, queued for projection, found by search and scored by uniqueness.
    print("\n--- Delete embeddings ---")

    ds_del = Dataset('sdk-test-delemb')
    n_del = 300
    del_field = 'delemb'
    del_ids: list = []
    del_chosen: list = []

    def _del_vectors(ids, sign):
        import random

        rnd = random.Random(11)
        return [
            {
                'sample_id': sid,
                'field_name': del_field,
                'vector': [sign * (1.0 + rnd.random() * 0.1)] + [rnd.random() * 0.1 for _ in range(7)],
            }
            for sid in ids
        ]

    def _del_missing_ids():
        ids: list = []
        after = None
        while True:
            page = ds_del.missing('embedding', field=del_field, limit=50, after_id=after)
            if not page['items']:
                return ids
            ids.extend(item['id'] for item in page['items'])
            after = ids[-1]

    @test("delete embeddings: seed vectors, map coordinates and the ANN index")
    def _():
        result = ds_del.add_samples(
            [
                Sample(filepath=f's3://test/del_{i}.jpg', metadata={'grp': i % 3, 'uniqueness_delemb': 0.5})
                for i in range(n_del)
            ],
        )
        del_ids.extend(result['ids'])
        del_chosen.extend(del_ids[:120])
        client.upload_embeddings(ds_del._id, _del_vectors(del_ids, 1.0))
        client.upload_coordinates(
            ds_del._id,
            del_field,
            [{'sample_id': sid, 'x': float(i), 'y': float(i % 7)} for i, sid in enumerate(del_ids)],
        )
        client.recompute_viz_rank(ds_del._id, del_field, full=True)
        ds_del.build_ann_index(del_field)

    _()

    @test("delete_embeddings(ids) in several requests reports the count and lists exactly those samples as missing")
    def _():
        from fridge.core import client as client_module

        per_request = client_module.DELETE_EMBEDDINGS_IDS_PER_REQUEST
        client_module.DELETE_EMBEDDINGS_IDS_PER_REQUEST = 50
        try:
            result = ds_del.delete_embeddings(del_field, del_chosen)
        finally:
            client_module.DELETE_EMBEDDINGS_IDS_PER_REQUEST = per_request
        assert result == {'deleted': 120}, result
        assert ds_del.missing('embedding', field=del_field)['total'] == 120
        assert _del_missing_ids() == sorted(del_chosen)
        again = ds_del.delete_embeddings(del_field, del_chosen)
        assert again == {'deleted': 0}, f"deleting again is a no-op: {again}"
        # Sample metadata is untouched, and only the chosen samples lost their map position.
        assert len(ds_del.filter(uniqueness_delemb=0.5)) == n_del, "metadata of every sample is untouched"
        located = client.lookup_embedding_coordinates(ds_del._id, del_ids, field=del_field)['points']
        assert len(located) == n_del - 120, f"{len(located)} positions remain"

    _()

    @test("re-uploaded vectors are queued for projection, found by search, and scored by uniqueness")
    def _():
        client.upload_embeddings(ds_del._id, _del_vectors(del_chosen, -1.0))
        assert ds_del.missing('embedding', field=del_field)['total'] == 0
        pending = client.get_vectors(ds_del._id, field_name=del_field, limit=1000, pending_only=True)['items']
        assert sorted(item['sample_id'] for item in pending) == sorted(del_chosen), "only the new vectors are pending"
        assert not client.lookup_embedding_coordinates(ds_del._id, del_chosen, field=del_field)['points']
        probe = _del_vectors(del_chosen[:1], -1.0)[0]['vector']
        new_side = client.similarity_search(ds_del._id, vector=probe, field_name=del_field, limit=120)
        assert sorted(r['id'] if isinstance(r, dict) else r.id for r in new_side) == sorted(del_chosen)
        old_probe = _del_vectors(del_ids[-1:], 1.0)[0]['vector']
        old_side = client.similarity_search(ds_del._id, vector=old_probe, field_name=del_field, limit=180)
        assert sorted(r['id'] if isinstance(r, dict) else r.id for r in old_side) == sorted(del_ids[120:]), (
            "the deleted vectors must not be found"
        )
        scored = ds_del.compute_uniqueness(del_field, k=3, metadata_field='uniq_delemb')
        assert scored['count'] == n_del, scored

    _()

    @test("DatasetView.delete_embeddings deletes the vectors of the view's samples")
    def _():
        group = ds_del.filter(grp=1)
        expected = len(group)
        assert expected > 0
        assert group.delete_embeddings(del_field) == {'deleted': expected}
        assert ds_del.missing('embedding', field=del_field)['total'] == expected
        job = ds_del.filter(grp=2).delete_embeddings(del_field, wait=False)
        assert job.wait()['deleted'] == len(ds_del.filter(grp=2))

    _()

    @test("whole-field delete clears every vector; the field's dimension stays tied to its ANN index")
    def _():
        result = ds_del.delete_embedding_field(del_field)
        assert result == {'deleted': 100}, f"groups 1 and 2 were already deleted: {result}"
        assert ds_del.missing('embedding', field=del_field)['total'] == n_del
        try:
            client.upload_embeddings(
                ds_del._id,
                [{'sample_id': del_ids[0], 'field_name': del_field, 'vector': [1.0] * 4}],
            )
        except Exception as error:
            assert 'ANN index' in str(error), error
        else:
            raise AssertionError("a vector of another dimension must be refused while the ANN index exists")
        client.upload_embeddings(ds_del._id, _del_vectors(del_ids, 1.0))
        assert ds_del.missing('embedding', field=del_field)['total'] == 0

    _()

    @test("delete embeddings: cleanup")
    def _():
        client.delete_dataset(ds_del._id)

    _()

    # --- DUPLICATE BATCH IN THE DEFAULT on_duplicate='error' MODE ---
    # The backend commits the NEW rows and answers 409 with {ids, existing}; the SDK must
    # surface both (a regression hid the ids — this is the lock).
    print("\n--- Duplicate batch ---")

    dup_fps = ['s3://test/dup_a.jpg', 's3://test/dup_b.jpg', 's3://test/dup_c.jpg']

    @test("add_samples default mode: a batch with a duplicate commits the new row and reports ids + existing")
    def _():
        first = ds.add_samples([Sample(filepath=dup_fps[0]), Sample(filepath=dup_fps[1])])
        assert len(first['ids']) == 2 and not first.get('existing'), first
        second = ds.add_samples([Sample(filepath=dup_fps[0]), Sample(filepath=dup_fps[2])])
        assert second.get('existing'), f"409 payload must carry `existing`: {second}"
        assert second['existing'].get(dup_fps[0]) == first['ids'][0], second
        assert len(second.get('ids', [])) == 1, f"the non-duplicate row's id must be reported: {second}"
        assert ds[dup_fps[2]].id == second['ids'][0], "the non-duplicate row was committed"

    _()

    @test("get_or_create after a lost race (add_samples reports existing) returns the existing sample")
    def _():
        s, created = ds.get_or_create(dup_fps[0])
        assert created is False and s.id == ds[dup_fps[0]].id

    _()

    @test("duplicate batch: cleanup")
    def _():
        for fp in dup_fps:
            ds.delete_sample(ds[fp].id)

    _()

    # --- CLEANUP ---
    print("\n--- Cleanup ---")
    client.delete_dataset(ds._id)
    print("Deleted sdk-test dataset")

    # --- REPORT ---
    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for name, err in ERRORS:
            print(f"  {name}: {err}")
    print("=" * 60)
    return FAIL == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
