"""Comprehensive Freedom Ridge SDK integration test.

Runs against a live Freedom Ridge backend, creating and destroying a 'sdk-test' dataset.
Tests every SDK method that doesn't require external resources (S3 images, embedding models).

Usage (requires FRIDGE_TEST_URL pointing to a disposable backend):
    python -m fridge.test_sdk
"""

import contextlib
import importlib.util
import io
import math
import os
import re
import sys
import tempfile
import traceback
import warnings
from collections.abc import Mapping
from types import ModuleType
from urllib.parse import unquote, urlsplit

import requests
from PIL import Image

import fridge
from fridge import (
    ClassProbabilities,
    Dataset,
    DatasetView,
    Sample,
    compute_image_metrics,
    connect,
    geo_within,
    threshold_rule,
)
from fridge._test_safety import require_test_url
from fridge.core import client as client_module
from fridge.core.client import Client, get_client

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

    @test("add_samples update-mode replace_tags makes the tags exactly the ones sent")
    def _():
        fp = 's3://test/dup_replace_tags.jpg'
        ds.add_samples([Sample(filepath=fp, tags=['stream'])])
        s = ds[fp]
        s.tags = s.tags + ['user-curated']
        s.save()
        ds.add_samples([Sample(filepath=fp, tags=['stream', 'v2'])], on_duplicate='update', replace_tags=True)
        s = ds[fp]
        assert sorted(s.tags) == ['stream', 'v2'], f"tags not replaced: {s.tags}"
        ds.delete_sample(s.id)

    _()

    @test("dataset count() and len() count every sample")
    def _():
        assert ds.count() == len(ds) == ds.all().count() > 0

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

    def refused_with_the_servers_sentence(key, write, undo):
        """`write` raises the server's refusal of a metadata key named like a sample's own column. A server that
        takes it gets `undo` first, so the samples the later tests count stay as they were."""
        sentence = f"metadata key(s) ['{key}'] name sample columns — set them on the sample itself"
        try:
            write()
        except requests.HTTPError as error:
            assert error.response is not None and error.response.status_code == 422, error
            assert sentence in str(error), error
        else:
            undo()
            raise AssertionError(f"a metadata key named {key!r} was accepted")

    def delete_if_stored(fp):
        try:
            ds.delete_sample(ds[fp].id)
        except KeyError:
            pass

    @test("add_samples refuses a metadata key named created_at with the server's sentence, storing nothing")
    def _():
        fp = 's3://test/reserved_created_at.jpg'
        refused_with_the_servers_sentence(
            'created_at',
            lambda: ds.add_samples([Sample(filepath=fp, metadata={'created_at': 'x', 'kept': 1})]),
            lambda: delete_if_stored(fp),
        )
        try:
            ds[fp]
            assert False, "the refused sample was stored"
        except KeyError:
            pass

    _()

    @test("a metadata key named tags or media_type is refused with the server's sentence, on upload and on save()")
    def _():
        for key in ('tags', 'media_type'):
            fp = f's3://test/reserved_{key}.jpg'
            refused_with_the_servers_sentence(
                key,
                lambda: ds.add_samples([Sample(filepath=fp, metadata={key: 'x', 'kept': 1})]),
                lambda: delete_if_stored(fp),
            )
            try:
                ds[fp]
                assert False, f"the sample with a metadata key named {key} was stored"
            except KeyError:
                pass
            s = ds['s3://test/img_6.jpg']
            before = dict(s.metadata)
            s.metadata[key] = 'x'
            refused_with_the_servers_sentence(
                key,
                s.save,
                lambda: ds.client.update_sample(ds._id, s.id, remove_metadata_keys=[key]),
            )
            assert ds['s3://test/img_6.jpg'].metadata == before, "a refused save changed the sample"

    _()

    @test("save() and set_values with a metadata key named like one of the five older sample columns are refused")
    def _():
        for key in ('id', 'dataset_id', 'filepath', 'created_at', 'last_modified_at'):
            s = ds['s3://test/img_6.jpg']
            before = dict(s.metadata)

            def undo():
                ds.client.update_sample(ds._id, s.id, remove_metadata_keys=[key])

            s.metadata[key] = 'x'
            refused_with_the_servers_sentence(key, s.save, undo)
            assert ds['s3://test/img_6.jpg'].metadata == before, "a refused save changed the sample"
            refused_with_the_servers_sentence(key, lambda: ds.set_values([s.id], key, 'x'), undo)
            assert ds['s3://test/img_6.jpg'].metadata == before, "a refused set_values changed the sample"

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

    _run_access_tests(client)
    _run_computed_description_tests(client)
    _run_path_tests(client)

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


# =========================================================================
# ACCESS LEVELS
# A dataset is shared with users and groups at a level, view < tag < edit < manage; a global dataset gives every
# signed-in user its everyone level; Dataset.access_level is the caller's own level. These run on their own private
# dataset, with power users and a group the admin makes over HTTP (the SDK has no user or group management), and
# check what the server stores over HTTP too, so a check doesn't lean on the SDK method it tests.
# =========================================================================
ACCESS_DATASET = 'sdk-test-access'
# Datasets made global by the SDK without a level, to see where they start (view); deleted with the rest.
ACCESS_OPEN_DATASETS = ('sdk-test-access-open', 'sdk-test-access-opened')
# Datasets made with an everyone level, and patch datasets with their sources; deleted with the rest.
ACCESS_LEVEL_DATASETS = {
    'edit': 'sdk-test-access-at-edit',
    'held': 'sdk-test-access-held-at-tag',
    'made': 'sdk-test-access-made-at-tag',
    'refused': 'sdk-test-access-refused-level',
    **{f'source-{kind}': f'sdk-test-access-patch-source-{kind}' for kind in ('edit', 'view', 'private')},
    **{f'patches-{kind}': f'sdk-test-access-patches-{kind}' for kind in ('edit', 'view', 'private')},
}
ACCESS_GROUP = 'sdk-acc-team'
ACCESS_USERS = {'alice': 'sdk_acc_alice', 'bob': 'sdk_acc_bob', 'carol': 'sdk_acc_carol', 'dave': 'sdk_acc_dave'}
ACCESS_PASSWORD = 'sdk-access-pass-1'
LEVELS = ('view', 'tag', 'edit', 'manage')
MANAGE_REFUSAL = 'This needs manage access to the dataset'


def _raises(error, call, *args, **kwargs):
    """The `error` call(*args, **kwargs) raised. Raising nothing fails; any other exception propagates as it is."""
    try:
        call(*args, **kwargs)
    except error as raised:
        return raised
    raise AssertionError(f"expected {error.__name__} from {getattr(call, '__name__', call)}{args}{kwargs}")


def _http_error(call, *args, **kwargs):
    """The status and message of the SDK's error (requests.HTTPError) that call(*args, **kwargs) raised."""
    raised = _raises(requests.HTTPError, call, *args, **kwargs)
    assert raised.response is not None, raised
    return raised.response.status_code, str(raised)


def _dataset_as(user_client, dataset_id):
    """Dataset `dataset_id` as `user_client` opens it: a new object each time, so what it says is read now."""
    saved = client_module._client
    client_module._client = user_client
    try:
        return Dataset.from_id(dataset_id)
    finally:
        client_module._client = saved


def _run_access_tests(client):
    print("\n--- Access levels ---")
    base = client.base_url
    admin = client.session
    ctx: dict = {}

    def ok(response):
        assert response.ok, f"{response.request.method} {response.url}: {response.status_code} {response.text[:300]}"
        return response

    def remove_leftovers():
        # The dataset first: its shares go with it, and a user who granted one can't be deleted while it stands.
        for d in client.get_datasets():
            if d['name'] in (ACCESS_DATASET, *ACCESS_OPEN_DATASETS, *ACCESS_LEVEL_DATASETS.values()):
                client.delete_dataset(d['id'])
        for u in ok(admin.get(f"{base}/users")).json():
            if u['username'] in ACCESS_USERS.values():
                ok(admin.delete(f"{base}/users/{u['id']}"))
        for g in ok(admin.get(f"{base}/groups")).json():
            if g['name'] == ACCESS_GROUP:
                ok(admin.delete(f"{base}/groups/{g['id']}"))

    @test("access setup (HTTP): a private dataset, four signed-in power users, a group with one of them")
    def _():
        remove_leftovers()
        ds = Dataset(ACCESS_DATASET)
        samples = [
            Sample(filepath=f's3://test/access_{i}.jpg', tags=['seed'], metadata={'note': 'orig'}) for i in range(3)
        ]
        ids = ds.add_samples(samples)['ids']
        users = {}
        for short, name in ACCESS_USERS.items():
            created = ok(
                admin.post(f"{base}/users", json={'username': name, 'password': ACCESS_PASSWORD, 'role': 'power_user'}),
            )
            user_client = Client(base)
            user_client.login(name, ACCESS_PASSWORD)
            # A new user changes their password before anything else; the same one is allowed.
            ok(
                user_client.session.post(
                    f"{base}/auth/change-password",
                    json={'current_password': ACCESS_PASSWORD, 'new_password': ACCESS_PASSWORD},
                ),
            )
            users[short] = (created.json()['id'], user_client)
        group_id = ok(admin.post(f"{base}/groups", json={'name': ACCESS_GROUP})).json()['id']
        ok(admin.put(f"{base}/groups/{group_id}/members/{users['bob'][0]}"))
        ctx.update(ds=ds, ids=ids, users=users, group_id=group_id, admin_id=client.whoami()['id'])
        for _uid, user_client in users.values():  # private and shared with none of them
            status, text = _http_error(_dataset_as, user_client, ds.id)
            assert status == 403, text

    _()
    if 'ds' not in ctx:
        print("  SKIP  the other access tests: their setup failed")
        return

    ds, ids, group_id, admin_id = ctx['ds'], ctx['ids'], ctx['group_id'], ctx['admin_id']
    users = ctx['users']
    alice, alice_client = users['alice']
    bob, bob_client = users['bob']
    carol, carol_client = users['carol']
    dave, dave_client = users['dave']
    first = 's3://test/access_0.jpg'

    # What the server holds, read and set over HTTP as the admin.
    def shares():
        return ok(admin.get(f"{base}/datasets/{ds.id}/access")).json()

    def user_level(user_id):
        return next((g['level'] for g in shares()['grants'] if g['user_id'] == user_id), None)

    def group_level():
        return next((g['level'] for g in shares()['group_grants'] if g['group_id'] == group_id), None)

    def dataset_row():
        return ok(admin.get(f"{base}/datasets/{ds.id}")).json()

    def share(user_id, level):
        ok(admin.post(f"{base}/datasets/{ds.id}/access", json={'user_id': user_id, 'level': level}))

    def unshare(user_id):
        ok(admin.delete(f"{base}/datasets/{ds.id}/access/{user_id}"))

    def share_group(level):
        ok(admin.post(f"{base}/datasets/{ds.id}/access", json={'group': ACCESS_GROUP, 'level': level}))

    def unshare_group():
        ok(admin.delete(f"{base}/datasets/{ds.id}/access/groups/{group_id}"))

    def make_private():
        ok(admin.patch(f"{base}/datasets/{ds.id}", json={'is_global': False}))

    def level_of(user_client):
        """The caller's level as the SDK says it, on a dataset object opened now."""
        return _dataset_as(user_client, ds.id).access_level

    try:

        @test("access_level: an admin's own level is manage, on the dataset object it created and on one loaded")
        def _():
            assert ds.access_level == 'manage', ds.access_level
            assert level_of(client) == 'manage'

        _()

        @test("list_access on a new private dataset: not global, its everyone level, the creator's edit share")
        def _():
            answer = ds.list_access()
            assert {'is_global', 'everyone_level', 'grants', 'group_grants'} <= set(answer), answer
            assert answer['is_global'] is False, answer
            assert answer['everyone_level'] == 'view', answer  # what it would give everyone, were it made global
            assert [(g['user_id'], g['username'], g['level']) for g in answer['grants']] == [
                (admin_id, 'admin', 'edit'),
            ], answer
            assert answer['group_grants'] == [], answer

        _()

        @test("grant_access(user_id), positional, no level (an old script's call): a new share is edit")
        def _():
            unshare(alice)
            assert isinstance(ds.grant_access(alice), dict)
            assert user_level(alice) == 'edit', shares()

        _()

        @test("grant_access(user_id) a second time changes nothing: one share, still edit")
        def _():
            share(alice, 'edit')
            ds.grant_access(alice)
            assert [g['level'] for g in shares()['grants'] if g['user_id'] == alice] == ['edit'], shares()

        _()

        @test("grant_access(user_id, level=...) sets each level, raising it and lowering it")
        def _():
            for level in ('view', 'tag', 'edit', 'manage', 'edit', 'tag', 'view'):
                assert isinstance(ds.grant_access(alice, level=level), dict)
                assert user_level(alice) == level, (level, shares())

        _()

        @test("grant_access(user_id) with no level leaves a share lowered to view at view (old scripts)")
        def _():
            share(alice, 'view')
            ds.grant_access(alice)
            assert user_level(alice) == 'view', shares()
            ds.grant_access(user_id=alice)
            assert user_level(alice) == 'view', shares()

        _()

        @test("access_level: a non-admin's own level is the level of their share, at each level")
        def _():
            make_private()
            for level in LEVELS:
                share(alice, level)
                assert level_of(alice_client) == level, level

        _()

        @test("granted view: the user reads the dataset, and is refused tagging and editing")
        def _():
            make_private()
            ds.grant_access(alice, level='view')
            mine = _dataset_as(alice_client, ds.id)
            assert mine.count() == 3
            assert mine[first].metadata['note'] == 'orig'
            status, text = _http_error(mine.tag_samples, [ids[0]], ['acc-view'])
            assert status == 403 and 'This needs tag access to the dataset' in text, text
            status, text = _http_error(mine.set_values, [ids[0]], 'note', 'changed')
            assert status == 403 and 'This needs edit access to the dataset' in text, text
            sample = ds[first]
            assert 'acc-view' not in sample.tags and sample.metadata['note'] == 'orig', (sample.tags, sample.metadata)

        _()

        @test("granted tag: the user adds and removes `tags`, and is refused changing other metadata")
        def _():
            make_private()
            ds.grant_access(alice, level='tag')
            mine = _dataset_as(alice_client, ds.id)
            mine.tag_samples([ids[0]], ['acc-tag'])  # across a selection
            assert 'acc-tag' in ds[first].tags
            mine.untag_samples([ids[0]], ['acc-tag'])
            assert 'acc-tag' not in ds[first].tags
            mine.client.update_sample(ds.id, ids[0], tags=['seed', 'acc-one'])  # one sample, tags only
            assert sorted(ds[first].tags) == ['acc-one', 'seed']
            status, text = _http_error(mine.set_values, [ids[0]], 'note', 'changed')
            assert status == 403 and 'This needs edit access to the dataset' in text, text
            status, text = _http_error(mine.client.update_sample, ds.id, ids[0], metadata={'note': 'changed'})
            assert status == 403 and 'This needs edit access to the dataset' in text, text
            assert ds[first].metadata['note'] == 'orig'
            ds.client.update_sample(ds.id, ids[0], tags=['seed'])

        _()

        @test("granted edit: the user changes metadata")
        def _():
            make_private()
            ds.grant_access(alice, level='edit')
            mine = _dataset_as(alice_client, ds.id)
            mine.set_values([ids[0]], 'note', 'edited')
            assert ds[first].metadata['note'] == 'edited'
            ds.set_values([ids[0]], 'note', 'orig')

        _()

        @test("an edit-level user is refused every sharing call with the server's text, and nothing changes")
        def _():
            make_private()
            share(alice, 'edit')
            share_group('view')
            before, row = shares(), dataset_row()
            mine = _dataset_as(alice_client, ds.id)
            calls = [
                (mine.list_access, (), {}),
                (mine.grant_access, (dave,), {}),
                (mine.grant_access, (dave,), {'level': 'view'}),
                (mine.grant_access, (), {'group': ACCESS_GROUP, 'level': 'manage'}),
                (mine.revoke_access, (admin_id,), {}),
                (mine.revoke_access, (), {'group': ACCESS_GROUP}),
                (mine.set_everyone_level, ('view',), {}),
                (mine.set_everyone_level, (None,), {}),
            ]
            for call, args, kwargs in calls:
                status, text = _http_error(call, *args, **kwargs)
                assert status == 403 and MANAGE_REFUSAL in text, (call.__name__, args, kwargs, text)
            assert shares() == before, shares()
            after = dataset_row()
            assert (after['is_global'], after['everyone_level']) == (row['is_global'], row['everyone_level'])

        _()

        @test("grant_access to a user who doesn't exist raises the SDK's error with the server's 404")
        def _():
            before = shares()
            for kwargs in ({}, {'level': 'view'}):
                status, text = _http_error(ds.grant_access, 999999999, **kwargs)
                assert status == 404 and 'User not found' in text, (kwargs, text)
            assert shares() == before, shares()

        _()

        @test("grant_access(group=name), no level: a new group share is edit, and its member gets edit")
        def _():
            make_private()
            unshare_group()
            assert isinstance(ds.grant_access(group=ACCESS_GROUP), dict)
            listed = [g for g in shares()['group_grants'] if g['group_id'] == group_id]
            assert [(g['name'], g['level']) for g in listed] == [(ACCESS_GROUP, 'edit')], shares()
            assert level_of(bob_client) == 'edit'

        _()

        @test("grant_access(group=name, level=...) changes the group's level; with no level it keeps it")
        def _():
            make_private()
            share_group('edit')
            ds.grant_access(group=ACCESS_GROUP, level='view')
            assert group_level() == 'view' and level_of(bob_client) == 'view', shares()
            ds.grant_access(group=ACCESS_GROUP)
            assert group_level() == 'view', shares()
            ds.grant_access(group=ACCESS_GROUP, level='manage')
            assert group_level() == 'manage' and level_of(bob_client) == 'manage', shares()
            ds.grant_access(group=ACCESS_GROUP, level='tag')
            assert group_level() == 'tag' and level_of(bob_client) == 'tag', shares()

        _()

        @test("grant_access to a group that doesn't exist raises the SDK's error with the server's 404")
        def _():
            before = shares()
            for kwargs in ({}, {'level': 'view'}):
                status, text = _http_error(ds.grant_access, group='sdk-acc-no-such-group', **kwargs)
                assert status == 404 and 'Group not found' in text, (kwargs, text)
            assert shares() == before, shares()

        _()

        @test("revoke_access(group=name) removes the group's share, and its member can't open the dataset")
        def _():
            make_private()
            share_group('view')
            assert level_of(bob_client) == 'view'
            assert ds.revoke_access(group=ACCESS_GROUP) is None
            assert group_level() is None, shares()
            status, text = _http_error(_dataset_as, bob_client, ds.id)
            assert status == 403, text
            assert ACCESS_DATASET not in [d['name'] for d in bob_client.get_datasets()]

        _()

        @test("revoke_access(group=name) of a group the dataset isn't shared with is a no-op")
        def _():
            unshare_group()
            before = shares()
            assert ds.revoke_access(group=ACCESS_GROUP) is None
            assert shares() == before, shares()

        _()

        @test("revoke_access(user_id) removes the share; again, or of a user never shared with, is a no-op")
        def _():
            make_private()
            share(alice, 'view')
            assert ds.revoke_access(alice) is None
            assert user_level(alice) is None, shares()
            status, text = _http_error(_dataset_as, alice_client, ds.id)
            assert status == 403, text
            before = shares()
            assert ds.revoke_access(user_id=alice) is None
            assert ds.revoke_access(dave) is None
            assert shares() == before, shares()

        _()

        @test("list_access after user and group shares lists each, with its level and name")
        def _():
            make_private()
            unshare(dave)
            share(alice, 'tag')
            share(carol, 'manage')
            share_group('view')
            answer = ds.list_access()
            assert {'is_global', 'everyone_level', 'grants', 'group_grants'} <= set(answer), answer
            assert answer['is_global'] is False, answer
            got = sorted((g['user_id'], g['username'], g['level']) for g in answer['grants'])
            assert got == sorted(
                [
                    (admin_id, 'admin', 'edit'),
                    (alice, ACCESS_USERS['alice'], 'tag'),
                    (carol, ACCESS_USERS['carol'], 'manage'),
                ],
            ), answer
            assert [(g['group_id'], g['name'], g['level']) for g in answer['group_grants']] == [
                (group_id, ACCESS_GROUP, 'view'),
            ], answer

        _()

        @test("a manager who isn't an administrator can call every sharing method")
        def _():
            make_private()
            share(carol, 'manage')
            unshare(dave)
            unshare_group()
            mine = _dataset_as(carol_client, ds.id)
            assert mine.access_level == 'manage'
            assert carol in [g['user_id'] for g in mine.list_access()['grants']]
            assert isinstance(mine.grant_access(dave, level='view'), dict)
            assert user_level(dave) == 'view', shares()
            mine.grant_access(dave)
            assert user_level(dave) == 'view', shares()
            mine.grant_access(group=ACCESS_GROUP, level='tag')
            assert group_level() == 'tag', shares()
            assert mine.revoke_access(group=ACCESS_GROUP) is None
            assert group_level() is None, shares()
            assert mine.revoke_access(dave) is None
            assert user_level(dave) is None, shares()
            assert mine.set_everyone_level('view') is None
            row = dataset_row()
            assert (row['is_global'], row['everyone_level']) == (True, 'view'), row
            assert mine.set_everyone_level(None) is None
            assert dataset_row()['is_global'] is False

        _()

        @test("set_everyone_level(view | tag | edit) makes the dataset global at it; a user with no share gets it")
        def _():
            make_private()
            unshare(dave)
            unshare_group()
            share(alice, 'view')
            for level in ('view', 'tag', 'edit'):
                assert ds.set_everyone_level(level) is None
                answer = ds.list_access()
                assert (answer['is_global'], answer['everyone_level']) == (True, level), answer
                assert level_of(dave_client) == level, level
            assert level_of(alice_client) == 'edit'  # the higher of her view share and the everyone level
            ds.set_everyone_level('view')
            assert level_of(alice_client) == 'view'
            share(alice, 'manage')
            assert level_of(alice_client) == 'manage'  # a share above the everyone level still counts

        _()

        @test("Dataset(name, is_global=True) makes a dataset global at view: a user with no share views it")
        def _():
            opened = Dataset(ACCESS_OPEN_DATASETS[0], is_global=True)
            row = ok(admin.get(f"{base}/datasets/{opened.id}")).json()
            assert (row['is_global'], row['everyone_level']) == (True, 'view'), row
            assert _dataset_as(dave_client, opened.id).access_level == 'view'

        _()

        @test("update_dataset(id, is_global=True) on a private dataset makes it global at view")
        def _():
            created = client.create_dataset(ACCESS_OPEN_DATASETS[1])
            assert (created['is_global'], created['everyone_level']) == (False, 'view'), created
            answer = client.update_dataset(created['id'], is_global=True)
            assert (answer['is_global'], answer['everyone_level']) == (True, 'view'), answer
            assert _dataset_as(dave_client, created['id']).access_level == 'view'

        _()

        @test("Dataset(name, is_global=True, everyone_level='edit') makes a dataset every power user can edit")
        def _():
            made = Dataset(ACCESS_LEVEL_DATASETS['edit'], is_global=True, everyone_level='edit')
            row = ok(admin.get(f"{base}/datasets/{made.id}")).json()
            assert (row['is_global'], row['everyone_level']) == (True, 'edit'), row
            theirs = _dataset_as(dave_client, made.id)  # dave has no share in it
            assert theirs.access_level == 'edit'
            theirs.add_samples([Sample(filepath='s3://test/access_at_edit_0.jpg', metadata={'by': 'dave'})])
            assert ok(admin.get(f"{base}/datasets/{made.id}/samples", params={'limit': 5})).json()['total'] == 1

        _()

        @test("Dataset(name, everyone_level='tag') without is_global: private, holding tag for when it is made global")
        def _():
            held = Dataset(ACCESS_LEVEL_DATASETS['held'], everyone_level='tag')
            row = ok(admin.get(f"{base}/datasets/{held.id}")).json()
            assert (row['is_global'], row['everyone_level']) == (False, 'tag'), row
            status, text = _http_error(_dataset_as, dave_client, held.id)
            assert status == 403, text
            ok(admin.patch(f"{base}/datasets/{held.id}", json={'is_global': True}))
            assert _dataset_as(dave_client, held.id).access_level == 'tag'

        _()

        @test("client.create_dataset(name, is_global=True, everyone_level='tag') creates it global at tag")
        def _():
            created = client.create_dataset(ACCESS_LEVEL_DATASETS['made'], is_global=True, everyone_level='tag')
            assert (created['is_global'], created['everyone_level']) == (True, 'tag'), created
            assert _dataset_as(dave_client, created['id']).access_level == 'tag'

        _()

        @test("an everyone level other than view, tag or edit raises ValueError and creates nothing")
        def _():
            name = ACCESS_LEVEL_DATASETS['refused']
            for level in ('manage', 'owner', 'Edit', ''):
                _raises(ValueError, client.create_dataset, name, is_global=True, everyone_level=level)
                _raises(ValueError, Dataset, name, is_global=True, everyone_level=level)
            assert name not in [d['name'] for d in client.get_datasets()]

        _()

        @test("to_patches gives the patch dataset its source's is_global and everyone level")
        def _():
            for kind, made, wanted in (
                ('edit', {'is_global': True, 'everyone_level': 'edit'}, (True, 'edit')),
                ('view', {'is_global': True, 'everyone_level': 'view'}, (True, 'view')),
                ('private', {}, (False, None)),
            ):
                body = {'name': ACCESS_LEVEL_DATASETS[f'source-{kind}'], **made}
                source = Dataset.from_id(ok(admin.post(f"{base}/datasets", json=body)).json()['id'])
                with tempfile.TemporaryDirectory() as patches_dir:
                    patches = source.to_patches('detections', ACCESS_LEVEL_DATASETS[f'patches-{kind}'], patches_dir)
                row = ok(admin.get(f"{base}/datasets/{patches.id}")).json()
                if wanted[0]:
                    assert (row['is_global'], row['everyone_level']) == wanted, (kind, row)
                else:
                    assert row['is_global'] is False, (kind, row)  # a private source's patches are private

        _()

        @test("made global at edit, then private, then global again with update_dataset: still at edit")
        def _():
            make_private()
            unshare(dave)
            unshare_group()
            assert ds.set_everyone_level('edit') is None
            assert level_of(dave_client) == 'edit'
            client.update_dataset(ds.id, is_global=False)
            status, text = _http_error(_dataset_as, dave_client, ds.id)
            assert status == 403, text
            answer = client.update_dataset(ds.id, is_global=True)
            assert (answer['is_global'], answer['everyone_level']) == (True, 'edit'), answer
            assert level_of(dave_client) == 'edit'
            make_private()

        _()

        @test("set_everyone_level(None) makes the dataset private: a user with no share loses it, shares stay")
        def _():
            ok(admin.patch(f"{base}/datasets/{ds.id}", json={'is_global': True, 'everyone_level': 'tag'}))
            unshare(dave)
            unshare_group()
            share(alice, 'view')
            assert ok(dave_client.session.get(f"{base}/datasets/{ds.id}")).json()['access_level'] == 'tag'
            assert ds.set_everyone_level(None) is None
            assert ds.list_access()['is_global'] is False
            assert dataset_row()['is_global'] is False
            status, text = _http_error(_dataset_as, dave_client, ds.id)
            assert status == 403, text
            assert user_level(alice) == 'view' and level_of(alice_client) == 'view', shares()

        _()

        @test("grant_access with both targets, neither, or another level raises ValueError, and changes nothing")
        def _():
            share(alice, 'view')
            share_group('view')
            before = shares()
            calls: list[tuple[tuple, dict]] = [
                ((), {}),
                ((), {'level': 'view'}),
                ((None,), {'group': None}),
                ((alice,), {'group': ACCESS_GROUP}),
                ((alice,), {'group': ACCESS_GROUP, 'level': 'edit'}),
                ((alice,), {'level': 'owner'}),
                ((alice,), {'level': 'admin'}),
                ((alice,), {'level': ''}),
                ((), {'group': ACCESS_GROUP, 'level': 'write'}),
            ]
            for args, kwargs in calls:
                _raises(ValueError, ds.grant_access, *args, **kwargs)
            assert shares() == before, shares()

        _()

        @test("revoke_access with both targets or neither raises ValueError, and changes nothing")
        def _():
            share(alice, 'view')
            share_group('view')
            before = shares()
            for args, kwargs in (((), {}), ((None,), {'group': None}), ((alice,), {'group': ACCESS_GROUP})):
                _raises(ValueError, ds.revoke_access, *args, **kwargs)
            assert shares() == before, shares()

        _()

        @test("set_everyone_level('manage') or another level raises ValueError, and changes nothing")
        def _():
            make_private()
            before, row = shares(), dataset_row()
            for level in ('manage', 'admin', 'private', ''):
                _raises(ValueError, ds.set_everyone_level, level)
            after = dataset_row()
            assert (after['is_global'], after['everyone_level']) == (row['is_global'], row['everyone_level'])
            assert shares() == before, shares()

        _()

    finally:
        try:
            remove_leftovers()
            print(f"Deleted {ACCESS_DATASET}, its users and its group")
        except Exception as error:  # a cleanup failure must not hide the results above
            print(f"Access cleanup failed: {error}")


# =========================================================================
# COMPUTED FIELD DESCRIPTIONS
# compute_metadata() and compute_metrics() give each field they wrote in a run its description and unit, where the
# field has none of its own, and fridge.COMPUTED_FIELD_DESCRIPTIONS holds them (m1-contract-computed-field-descriptions,
# section 2). Written from the contract, not from the code, and read back through the schema as the SDK returns it.
# Where the contract leaves a shape open, these tests read it this way:
# - The images are small real PNGs made here at local paths, read with an image_loader of the test's, so
#   compute_metadata() writes file_size_bytes too. One more image, in memory, stands at an s3:// path, for which
#   compute_metadata() writes no file_size_bytes: there, that field is one "the run wrote no value for". An image that
#   can't be read is a file of bytes that aren't an image.
# - "Skipped because it already had its values" is what the summary line counts as skipped; "describes nothing" is no
#   description request sent and every field reading none (or, after an earlier run, what it read before).
# - A field the run can't vouch for (a sample it wrote no value of it for already holds one: an unreadable image
#   with the user's own brightness, an s3:// sample with the user's own file_size_bytes) reads none, while the
#   other fields the run wrote read the SDK's.
# - The backend-tests job installs no OpenCV, which compute_metrics() needs. Where it is missing, a stand-in for the two
#   OpenCV calls compute_image_metrics() makes is put in sys.modules['cv2'] while the metrics run: its values are not
#   OpenCV's, but which fields a run writes and describes is the same. Where OpenCV is installed, it is used.
# - No level lets a user write values but not describe a field (both need edit), so the server's refusal is made on the
#   session the dataset's client uses: its description requests (PATCH .../fields/<name>/description) are sent to a
#   route the server doesn't have (its 404, as an older server without the route answers), or to the same route as a
#   user with a view share in the dataset (its 403, as the user's level refuses). "Can't be reached" is the same
#   requests raising requests.ConnectionError, as a connection the server refuses does.
# - "Warns once" is one Python warning (warnings.warn) that names every field the run wrote; "why" is the refusal's
#   status code or the server's words, or for the unreachable server the connection error's words or that it could not
#   connect or reach the server, in it.
# - A pair is a tuple. What the two methods return today is None; their progress output is checked by the summary line
#   each prints.
# =========================================================================
CFD_DATASETS = {
    key: f'sdk-test-cfd-{key}'
    for key in (
        'fresh',
        'own',
        'skip',
        'some',
        'overwrite',
        'broken',
        'unvouched-metric',
        'unvouched-metadata',
        'old-server',
        'unreachable',
        'level',
    )
}
CFD_VIEWER = 'sdk_cfd_viewer'
CFD_PASSWORD = 'sdk-cfd-pass-1'
METADATA_FIELDS = ('width', 'height', 'aspect_ratio', 'megapixels', 'file_size_bytes')  # compute_metadata()
METRIC_FIELDS = (  # compute_metrics()
    'brightness',
    'blurriness',
    'contrast',
    'entropy',
    'salt_and_pepper',
    'saturation',
    'pct_underexposed',
    'pct_overexposed',
)
COMPUTED_FIELDS = METADATA_FIELDS + METRIC_FIELDS
# What the server gave each of them by name before this change, word for word, with its unit: the words and units move
# to the SDK unchanged.
SERVER_TEXTS_BEFORE = {
    'width': ("The image's width in pixels, as stored in the file (a camera's rotation tag isn't applied).", 'px'),
    'height': ("The image's height in pixels, as stored in the file (a camera's rotation tag isn't applied).", 'px'),
    'aspect_ratio': (
        'Width divided by height, rounded to 4 decimal places: above 1 for a landscape image, below 1 for portrait.',
        None,
    ),
    'megapixels': ('Width times height in millions of pixels, rounded to 3 decimal places.', 'MP'),
    'file_size_bytes': (
        "The image file's size, recorded only when the sample's path named a file on the disk of the machine that "
        'ran compute_metadata() (never for s3:// files).',
        'bytes',
    ),
    'brightness': (
        "Perceived brightness of the image's average colour, the square root of 0.241·R² + 0.691·G² + 0.068·B² of "
        'its mean red, green and blue values, from 0 (black) to 255 (white).',
        None,
    ),
    'blurriness': (
        'Variance of the Laplacian of the grey-level image: low values suggest a blurry image and high values a '
        'sharp one, despite the name.',
        None,
    ),
    'contrast': ('The highest minus the lowest value over all pixels and all three colour channels, 0 to 255.', None),
    'entropy': (
        'Shannon entropy of the red, green and blue values taken as one 768-bin histogram, from about 1.6 for a single '
        'flat colour to about 9.6 when every value is equally common.',
        'bits',
    ),
    'salt_and_pepper': (
        'Share of pixels that are lone near-white (grey level 244 or more) or near-black (10 or less) specks, the '
        'ones a 2×2 morphological opening removes: a sign of salt-and-pepper noise.',
        '%',
    ),
    'saturation': (
        'Mean colour saturation of the pixels on the HSV scale, from 0 (grey) to 255 (fully saturated colour).',
        None,
    ),
    'pct_underexposed': ('Share of pixels whose grey level is 5 or less (of 255): crushed shadows.', '%'),
    'pct_overexposed': ('Share of pixels whose grey level is 250 or more (of 255): blown highlights.', '%'),
}
# The units among them, as they are today.
UNITS_BEFORE = {
    'width': 'px',
    'height': 'px',
    'megapixels': 'MP',
    'file_size_bytes': 'bytes',
    'entropy': 'bits',
    'salt_and_pepper': '%',
    'pct_underexposed': '%',
    'pct_overexposed': '%',
}
CFD_IMAGES = ((8, 6, (250, 250, 250)), (12, 10, (20, 40, 200)), (16, 16, (128, 128, 128)))  # width, height, colour
IN_MEMORY = 's3://sdk-test-cfd/in-memory.png'
IN_MEMORY_SIZE = (10, 4)
NOTHING_WRITTEN = (None, None, None)  # (description, unit, description_source) of a field nobody described


def _cfd_images(folder):
    """The images of CFD_IMAGES as PNG files in `folder`; their paths."""
    paths = []
    for index, (width, height, colour) in enumerate(CFD_IMAGES):
        image = Image.new('RGB', (width, height), colour)
        image.putpixel((0, 0), (0, 0, 0))
        image.putpixel((width - 1, height - 1), (255, 255, 255))
        path = os.path.join(folder, f'cfd-{index}.png')
        image.save(path)
        paths.append(path)
    return paths


def _cfd_open(path):
    """The tests' image_loader: a file at a local path, or the image in memory at IN_MEMORY."""
    if path == IN_MEMORY:
        return Image.new('RGB', IN_MEMORY_SIZE, (200, 30, 30))
    with Image.open(path) as image:
        return image.convert('RGB')


def _own_values(index):
    """A sample's values of the user's own for the thirteen fields, each on a scale of its own."""
    return {
        'width': 12.5 + index,  # centimetres on the print
        'height': 9.0,
        'aspect_ratio': 1.39,
        'megapixels': 0.5,
        'file_size_bytes': 2.5 + index,  # kilobytes
        'brightness': 0.25 * (index + 1),  # from 0 to 1
        'blurriness': 0.1 * index,
        'contrast': 0.8,
        'entropy': 0.3,
        'salt_and_pepper': 0.01,
        'saturation': 0.4,
        'pct_underexposed': 0.02,
        'pct_overexposed': 0.03,
    }


@contextlib.contextmanager
def _metric_dependencies():
    """OpenCV for compute_metrics(), or where it isn't installed a stand-in for its two calls (see above)."""
    if importlib.util.find_spec('cv2') is not None:
        yield
        return
    import numpy as np

    def laplacian(source, depth):
        grey = np.asarray(source, dtype=np.float64)
        around = np.pad(grey, 1, mode='reflect')
        return around[:-2, 1:-1] + around[2:, 1:-1] + around[1:-1, :-2] + around[1:-1, 2:] - 4 * grey

    def opening(source, operation, kernel):
        return np.array(source)

    stand_in = ModuleType('cv2')
    vars(stand_in).update(CV_64F=6, MORPH_OPEN=2, Laplacian=laplacian, morphologyEx=opening)
    sys.modules['cv2'] = stand_in
    try:
        yield
    finally:
        sys.modules.pop('cv2', None)


@contextlib.contextmanager
def _description_requests(session, divert=None):
    """The description requests (PATCH .../fields/<name>/description) sent on `session`, as (field name, body); with
    `divert`, each is answered by divert(method, url, ...) in place of the server's route."""
    sent: list = []
    original = session.request

    def request(method, url, *args, **kwargs):
        found = re.search(r'/fields/([^/]+)/description$', urlsplit(url).path)
        if str(method).upper() == 'PATCH' and found:
            sent.append((unquote(found.group(1)), kwargs.get('json')))
            if divert is not None:
                return divert(method, url, *args, **kwargs)
        return original(method, url, *args, **kwargs)

    vars(session)['request'] = request
    try:
        yield sent
    finally:
        vars(session).pop('request', None)


def _schema_descriptions(ds):
    """{field name: (description, unit, description_source)}, from the schema as the SDK returns it."""
    return {
        field['name']: (field['description'], field['unit'], field['description_source']) for field in ds.get_schema()
    }


def _sdk_described(name):
    """What a field the SDK described reads: its words and unit, description_source "set"."""
    description, unit = SERVER_TEXTS_BEFORE[name]
    return description, unit, 'set'


def _differences(seen, expected):
    """{field name: (what it reads, what it should)} for each field of `expected` that reads otherwise."""
    return {name: (seen.get(name), want) for name, want in expected.items() if seen.get(name) != want}


def _stored_metadata(ds):
    """Each sample's metadata, by filepath."""
    return {sample.filepath: dict(sample.metadata) for sample in ds.iter_samples()}


def _quietly(call, *args, **kwargs):
    """call(*args, **kwargs), keeping what it prints: (its answer, the text printed)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        answer = call(*args, **kwargs)
    return answer, out.getvalue()


def _warnings_naming(caught):
    """The texts of the caught warnings that name any of the computed fields."""
    texts = [str(warning.message) for warning in caught]
    return [text for text in texts if any(re.search(rf'\b{name}\b', text) for name in COMPUTED_FIELDS)]


def _names_each(text, fields):
    """The fields `text` doesn't name."""
    return [name for name in fields if not re.search(rf'\b{name}\b', text)]


def _run_computed_description_tests(client):
    print("\n--- Computed field descriptions ---")
    base = client.base_url
    admin = client.session
    ctx: dict = {}

    def ok(response):
        assert response.ok, f"{response.request.method} {response.url}: {response.status_code} {response.text[:300]}"
        return response

    def remove_leftovers():
        for d in client.get_datasets():
            if d['name'] in CFD_DATASETS.values():
                client.delete_dataset(d['id'])
        for u in ok(admin.get(f"{base}/users")).json():
            if u['username'] == CFD_VIEWER:
                ok(admin.delete(f"{base}/users/{u['id']}"))

    try:
        remove_leftovers()
    except Exception as error:
        print(f"  Computed descriptions: leftovers not removed: {error}")

    with tempfile.TemporaryDirectory(prefix='sdk-test-cfd-') as folder:
        paths = _cfd_images(folder)
        broken = os.path.join(folder, 'cfd-broken.png')  # not an image: it can't be read
        with open(broken, 'wb') as out:
            out.write(b'not an image')

        def images_dataset(key, values=None):
            """A new dataset of the local images, each sample with values(index) as its metadata when given."""
            ds = Dataset(CFD_DATASETS[key])
            ds.add_samples(
                [Sample(filepath=path, metadata=values(index) if values else {}) for index, path in enumerate(paths)],
            )
            return ds

        try:

            @test(
                "COMPUTED_FIELD_DESCRIPTIONS: the thirteen fields compute_metadata() and compute_metrics() write, each "
                "(description, unit), in the words and units the server gave them by name",
            )
            def _():
                table = getattr(fridge, 'COMPUTED_FIELD_DESCRIPTIONS', None)
                assert table is not None, "fridge has no COMPUTED_FIELD_DESCRIPTIONS"
                assert isinstance(table, Mapping), type(table)
                assert sorted(table) == sorted(COMPUTED_FIELDS), sorted(table)
                for name, pair in table.items():
                    assert isinstance(pair, tuple) and len(pair) == 2, (name, pair)
                    description, unit = pair
                    assert isinstance(description, str) and description.strip(), (name, pair)
                    assert unit is None or isinstance(unit, str), (name, pair)
                    assert pair == SERVER_TEXTS_BEFORE[name], (name, pair, SERVER_TEXTS_BEFORE[name])
                assert {name: unit for name, (_, unit) in table.items() if unit is not None} == UNITS_BEFORE

            _()

            @test(
                "compute_metadata() and compute_metrics() write the values they wrote before, return None and print "
                "their summary",
            )
            def _():
                ds = images_dataset('fresh')
                ctx['fresh'] = ds
                with _metric_dependencies():
                    from_metadata, printed = _quietly(ds.compute_metadata, image_loader=_cfd_open)
                    from_metrics, printed_metrics = _quietly(ds.compute_metrics, image_loader=_cfd_open)
                    expected = {path: compute_image_metrics(_cfd_open(path)) for path in paths}
                assert (from_metadata, from_metrics) == (None, None)
                assert 'Metadata: 3 computed, 0 skipped, 0 errors' in printed, printed
                assert 'Metrics: 3 computed, 0 skipped, 0 errors' in printed_metrics, printed_metrics
                stored = _stored_metadata(ds)
                for path, (width, height, _colour) in zip(paths, CFD_IMAGES):
                    meta = stored[path]
                    assert {name: meta.get(name) for name in METADATA_FIELDS} == {
                        'width': width,
                        'height': height,
                        'aspect_ratio': round(width / height, 4),
                        'megapixels': round(width * height / 1e6, 3),
                        'file_size_bytes': os.path.getsize(path),
                    }, (path, meta)
                    for name, value in expected[path].items():
                        assert math.isclose(meta[name], value, rel_tol=1e-9, abs_tol=1e-9), (path, name, meta, value)

            _()

            @test("compute_metadata() gives each field it wrote the SDK's description and unit, description_source set")
            def _():
                seen = _schema_descriptions(ctx['fresh'])
                differ = _differences(seen, {name: _sdk_described(name) for name in METADATA_FIELDS})
                assert not differ, differ

            _()

            @test("compute_metrics() gives each field it wrote the SDK's description and unit, description_source set")
            def _():
                seen = _schema_descriptions(ctx['fresh'])
                differ = _differences(seen, {name: _sdk_described(name) for name in METRIC_FIELDS})
                assert not differ, differ

            _()

            @test(
                "what someone wrote is kept: a field with only a description gains the unit, one with only a unit "
                "gains the description, one with both is untouched",
            )
            def _():
                ds = images_dataset('own', _own_values)
                written = {
                    'width': ('Print width, ours.', None),  # a description only: gains the unit
                    'height': (None, 'cm'),  # a unit only: gains the description
                    'megapixels': ('Million pixels, ours.', 'Mpx'),  # both: untouched
                    'entropy': ('Our entropy.', None),
                    'salt_and_pepper': (None, 'ppm'),
                    'pct_overexposed': ('Blown pixels, ours.', 'share'),
                }
                for name, (description, unit) in written.items():
                    ds.set_field_description(name, description, unit=unit)
                with _metric_dependencies():
                    _quietly(ds.compute_metadata, image_loader=_cfd_open, skip_existing=False)
                    _quietly(ds.compute_metrics, image_loader=_cfd_open, skip_existing=False)
                expected = {name: _sdk_described(name) for name in COMPUTED_FIELDS}
                for name, (description, unit) in written.items():
                    words, sdk_unit = SERVER_TEXTS_BEFORE[name]
                    expected[name] = (description or words, unit or sdk_unit, 'set')
                differ = _differences(_schema_descriptions(ds), expected)
                assert not differ, differ

            _()

            @test(
                "skip_existing=True where every sample already has the values (the user's own) writes no value and "
                "describes nothing",
            )
            def _():
                ds = images_dataset('skip', _own_values)
                before = _stored_metadata(ds)
                with _metric_dependencies(), _description_requests(ds.client.session) as sent:
                    from_metadata, printed = _quietly(ds.compute_metadata, image_loader=_cfd_open, skip_existing=True)
                    from_metrics, printed_metrics = _quietly(
                        ds.compute_metrics,
                        image_loader=_cfd_open,
                        skip_existing=True,
                    )
                assert (from_metadata, from_metrics) == (None, None)
                assert 'Metadata: 0 computed, 3 skipped, 0 errors' in printed, printed
                assert 'Metrics: 0 computed, 3 skipped, 0 errors' in printed_metrics, printed_metrics
                assert _stored_metadata(ds) == before
                assert sent == [], sent
                differ = _differences(_schema_descriptions(ds), dict.fromkeys(COMPUTED_FIELDS, NOTHING_WRITTEN))
                assert not differ, differ

            _()

            def mixed_dataset(key):
                """Two local images holding the user's own values (brightness from 0 to 1), and one in memory with
                none."""
                ds = Dataset(CFD_DATASETS[key])
                ds.add_samples(
                    [Sample(filepath=path, metadata=_own_values(index)) for index, path in enumerate(paths[:2])]
                    + [Sample(filepath=IN_MEMORY)],
                )
                return ds

            @test(
                "skip_existing=True where some samples already have the values (the user's own): the run writes the "
                "others and describes nothing",
            )
            def _():
                ds = mixed_dataset('some')
                before = _stored_metadata(ds)
                with _metric_dependencies(), _description_requests(ds.client.session) as sent:
                    _, printed = _quietly(ds.compute_metadata, image_loader=_cfd_open, skip_existing=True)
                    _, printed_metrics = _quietly(ds.compute_metrics, image_loader=_cfd_open, skip_existing=True)
                assert 'Metadata: 1 computed, 2 skipped, 0 errors' in printed, printed
                assert 'Metrics: 1 computed, 2 skipped, 0 errors' in printed_metrics, printed_metrics
                stored = _stored_metadata(ds)
                for path in paths[:2]:
                    assert stored[path] == before[path], (path, stored[path])  # the user's own values stay
                made = stored[IN_MEMORY]
                assert made.get('width') == IN_MEMORY_SIZE[0] and isinstance(made.get('brightness'), float), made
                assert sent == [], sent
                differ = _differences(_schema_descriptions(ds), dict.fromkeys(COMPUTED_FIELDS, NOTHING_WRITTEN))
                assert not differ, differ

            _()

            @test(
                "skip_existing=False on the same kind of dataset writes every sample and describes the fields it wrote",
            )
            def _():
                ds = mixed_dataset('overwrite')
                with _metric_dependencies():
                    _, printed = _quietly(ds.compute_metadata, image_loader=_cfd_open, skip_existing=False)
                    _, printed_metrics = _quietly(ds.compute_metrics, image_loader=_cfd_open, skip_existing=False)
                assert 'Metadata: 3 computed, 0 skipped, 0 errors' in printed, printed
                assert 'Metrics: 3 computed, 0 skipped, 0 errors' in printed_metrics, printed_metrics
                stored = _stored_metadata(ds)
                for path, (width, height, _colour) in zip(paths[:2], CFD_IMAGES):
                    meta = stored[path]
                    assert (meta.get('width'), meta.get('height')) == (width, height), meta  # the SDK's values now
                    assert meta.get('file_size_bytes') == os.path.getsize(path), meta
                differ = _differences(
                    _schema_descriptions(ds),
                    {name: _sdk_described(name) for name in COMPUTED_FIELDS},
                )
                assert not differ, differ

            _()

            @test(
                "a second run with skip_existing=True on a dataset the SDK computed skips every sample, changes no "
                "description and sends no description request",
            )
            def _():
                ds = ctx['fresh']
                values, described = _stored_metadata(ds), _schema_descriptions(ds)
                with _metric_dependencies(), _description_requests(ds.client.session) as sent:
                    _, printed = _quietly(ds.compute_metadata, image_loader=_cfd_open, skip_existing=True)
                    _, printed_metrics = _quietly(ds.compute_metrics, image_loader=_cfd_open, skip_existing=True)
                assert 'Metadata: 0 computed, 3 skipped, 0 errors' in printed, printed
                assert 'Metrics: 0 computed, 3 skipped, 0 errors' in printed_metrics, printed_metrics
                assert sent == [], sent
                assert _stored_metadata(ds) == values
                differ = _differences(_schema_descriptions(ds), {name: described.get(name) for name in COMPUTED_FIELDS})
                assert not differ, differ

            _()

            @test(
                "a sample whose image can't be read is not a skip: the run describes the fields it wrote, and leaves "
                "alone one it wrote no value for",
            )
            def _():
                ds = Dataset(CFD_DATASETS['broken'])
                ds.add_samples(
                    [
                        Sample(filepath=IN_MEMORY, metadata={'file_size_bytes': 2.5}),  # the user's own, in kilobytes
                        Sample(filepath=broken),
                    ],
                )
                with _metric_dependencies():
                    _, printed = _quietly(ds.compute_metadata, image_loader=_cfd_open)
                    _, printed_metrics = _quietly(ds.compute_metrics, image_loader=_cfd_open)
                assert 'Metadata: 1 computed, 0 skipped, 1 errors' in printed, printed
                assert 'Metrics: 1 computed, 0 skipped, 1 errors' in printed_metrics, printed_metrics
                stored = _stored_metadata(ds)
                assert stored[IN_MEMORY].get('width') == IN_MEMORY_SIZE[0], stored[IN_MEMORY]
                assert stored[IN_MEMORY].get('file_size_bytes') == 2.5, stored[IN_MEMORY]
                expected = {name: _sdk_described(name) for name in COMPUTED_FIELDS}
                expected['file_size_bytes'] = NOTHING_WRITTEN  # the user's own: the run wrote none
                differ = _differences(_schema_descriptions(ds), expected)
                assert not differ, differ

            _()

            @test(
                "compute_metrics(skip_existing=False) with an unreadable image that holds the user's own brightness: "
                "brightness is not described, the other metrics the run wrote are",
            )
            def _():
                ds = Dataset(CFD_DATASETS['unvouched-metric'])
                ds.add_samples(
                    [Sample(filepath=path) for path in paths[:2]]
                    + [Sample(filepath=broken, metadata={'brightness': 0.5})],  # the user's own, from 0 to 1
                )
                with _metric_dependencies():
                    _, printed = _quietly(ds.compute_metrics, image_loader=_cfd_open, skip_existing=False)
                assert 'Metrics: 2 computed, 0 skipped, 1 errors' in printed, printed
                stored = _stored_metadata(ds)
                for path in paths[:2]:
                    assert all(isinstance(stored[path].get(name), float) for name in METRIC_FIELDS), stored[path]
                assert stored[broken] == {'brightness': 0.5}, stored[broken]
                expected = {name: _sdk_described(name) for name in METRIC_FIELDS}
                expected['brightness'] = NOTHING_WRITTEN  # one of its values is the user's own
                differ = _differences(_schema_descriptions(ds), expected)
                assert not differ, differ

            _()

            @test(
                "compute_metadata() with an s3:// sample that holds the user's own file_size_bytes beside local "
                "files: file_size_bytes is not described, the other fields the run wrote are",
            )
            def _():
                ds = Dataset(CFD_DATASETS['unvouched-metadata'])
                ds.add_samples(
                    [Sample(filepath=path) for path in paths]
                    + [Sample(filepath=IN_MEMORY, metadata={'file_size_bytes': 2.5})],  # the user's own, in kilobytes
                )
                _, printed = _quietly(ds.compute_metadata, image_loader=_cfd_open)
                assert 'Metadata: 4 computed, 0 skipped, 0 errors' in printed, printed
                stored = _stored_metadata(ds)
                for path in paths:
                    assert stored[path].get('file_size_bytes') == os.path.getsize(path), stored[path]
                assert stored[IN_MEMORY].get('file_size_bytes') == 2.5, stored[IN_MEMORY]
                assert stored[IN_MEMORY].get('width') == IN_MEMORY_SIZE[0], stored[IN_MEMORY]
                expected = {name: _sdk_described(name) for name in METADATA_FIELDS}
                expected['file_size_bytes'] = NOTHING_WRITTEN  # one of its values is the user's own
                differ = _differences(_schema_descriptions(ds), expected)
                assert not differ, differ

            _()

            @test(
                "a description the server refuses (an older server without the route: 404): compute_metadata() still "
                "writes the values and returns None, and warns once, naming the fields and why",
            )
            def _():
                ds = images_dataset('old-server')
                session = ds.client.session

                def to_a_missing_route(method, url, *args, **kwargs):
                    return session.request(method, f'{url}-on-a-newer-server', *args, **kwargs)

                with (
                    warnings.catch_warnings(record=True) as caught,
                    _description_requests(session, to_a_missing_route) as sent,
                ):
                    warnings.simplefilter('always')
                    returned, printed = _quietly(ds.compute_metadata, image_loader=_cfd_open)
                assert returned is None, returned
                assert 'Metadata: 3 computed, 0 skipped, 0 errors' in printed, printed
                stored = _stored_metadata(ds)
                for path, (width, height, _colour) in zip(paths, CFD_IMAGES):
                    assert (stored[path].get('width'), stored[path].get('height')) == (width, height), stored[path]
                asked = {name for name, _body in sent}
                assert asked and asked <= set(METADATA_FIELDS), sent
                named = _warnings_naming(caught)
                assert len(named) == 1, named
                assert not _names_each(named[0], METADATA_FIELDS), (_names_each(named[0], METADATA_FIELDS), named[0])
                assert '404' in named[0] or 'not found' in named[0].lower(), named[0]
                differ = _differences(_schema_descriptions(ds), dict.fromkeys(METADATA_FIELDS, NOTHING_WRITTEN))
                assert not differ, differ

            _()

            @test(
                "the server can't be reached for the description: compute_metadata() still writes the values and "
                "returns None, and warns once, naming the fields and why",
            )
            def _():
                ds = images_dataset('unreachable')

                def unreachable(method, url, *args, **kwargs):
                    raise requests.ConnectionError('Connection refused (the test cut the line)')

                with (
                    warnings.catch_warnings(record=True) as caught,
                    _description_requests(ds.client.session, unreachable) as sent,
                ):
                    warnings.simplefilter('always')
                    returned, printed = _quietly(ds.compute_metadata, image_loader=_cfd_open)
                assert returned is None, returned
                assert 'Metadata: 3 computed, 0 skipped, 0 errors' in printed, printed
                stored = _stored_metadata(ds)
                for path, (width, height, _colour) in zip(paths, CFD_IMAGES):
                    assert (stored[path].get('width'), stored[path].get('height')) == (width, height), stored[path]
                asked = {name for name, _body in sent}
                assert asked and asked <= set(METADATA_FIELDS), sent
                named = _warnings_naming(caught)
                assert len(named) == 1, named
                assert not _names_each(named[0], METADATA_FIELDS), (_names_each(named[0], METADATA_FIELDS), named[0])
                assert re.search(r'connect|reach|cut the line', named[0], re.I), named[0]
                differ = _differences(_schema_descriptions(ds), dict.fromkeys(METADATA_FIELDS, NOTHING_WRITTEN))
                assert not differ, differ

            _()

            @test(
                "a description the server refuses (the user's level: 403): compute_metrics() still writes the values "
                "and returns None, and warns once, naming the fields and why",
            )
            def _():
                ds = images_dataset('level')
                created = ok(
                    admin.post(
                        f"{base}/users",
                        json={'username': CFD_VIEWER, 'password': CFD_PASSWORD, 'role': 'power_user'},
                    ),
                )
                viewer = Client(base)
                viewer.login(CFD_VIEWER, CFD_PASSWORD)
                ok(
                    viewer.session.post(
                        f"{base}/auth/change-password",
                        json={'current_password': CFD_PASSWORD, 'new_password': CFD_PASSWORD},
                    ),
                )
                ok(
                    admin.post(
                        f"{base}/datasets/{ds.id}/access",
                        json={'user_id': created.json()['id'], 'level': 'view'},
                    ),
                )

                def as_the_viewer(method, url, *args, **kwargs):
                    return viewer.session.request(method, url, *args, **kwargs)

                with (
                    warnings.catch_warnings(record=True) as caught,
                    _metric_dependencies(),
                    _description_requests(ds.client.session, as_the_viewer) as sent,
                ):
                    warnings.simplefilter('always')
                    returned, printed = _quietly(ds.compute_metrics, image_loader=_cfd_open)
                assert returned is None, returned
                assert 'Metrics: 3 computed, 0 skipped, 0 errors' in printed, printed
                stored = _stored_metadata(ds)
                for path in paths:
                    assert all(isinstance(stored[path].get(name), (int, float)) for name in METRIC_FIELDS), stored[path]
                asked = {name for name, _body in sent}
                assert asked and asked <= set(METRIC_FIELDS), sent
                named = _warnings_naming(caught)
                assert len(named) == 1, named
                assert not _names_each(named[0], METRIC_FIELDS), (_names_each(named[0], METRIC_FIELDS), named[0])
                assert '403' in named[0] or 'needs edit access' in named[0], named[0]
                differ = _differences(_schema_descriptions(ds), dict.fromkeys(METRIC_FIELDS, NOTHING_WRITTEN))
                assert not differ, differ

            _()

        finally:
            try:
                remove_leftovers()
                print("Deleted the computed-description datasets and their user")
            except Exception as error:  # a cleanup failure must not hide the results above
                print(f"Computed descriptions cleanup failed: {error}")


# =========================================================================
# PATHS DO NOT CHANGE; A COPY CAN GIVE NEW ONES
# add_samples_from(..., path_prefixes={old: new}) gives the copies new paths, in filepath and in the texts inside
# metadata; a rule that would give two samples one path is refused before anything is written; sample.save() after a
# changed filepath raises and sends nothing; a PATCH carrying another filepath is a 422; a copy keeps objects with
# their images. The full set of cases is
# backend/tests/test_paths_do_not_change_spec.py (the app in-process); these check the same rules on a running server.
# =========================================================================
PATH_DATASETS = (
    'sdk-test-paths-source',
    'sdk-test-paths-target',
    'sdk-test-paths-collide',
    'sdk-test-paths-refused',
    'sdk-test-paths-objects',
    'sdk-test-paths-objects-copy',
)
PATH_PHRASE = "filepath can't be changed"


def _run_path_tests(client):
    print("\n--- Paths do not change ---")
    base = client.base_url

    def remove_leftovers():
        for d in client.get_datasets():
            if d['name'] in PATH_DATASETS:
                client.delete_dataset(d['id'])

    remove_leftovers()
    try:
        source = Dataset('sdk-test-paths-source')
        source.add_samples(
            [
                Sample(
                    filepath='/media/old/a.jpg',
                    tags=['t', '/media/old/tag'],
                    metadata={
                        'mask': {'maskPath': '/media/old/masks/a.png'},
                        'lidar': [{'path': '/media/old/lidar/a.bin'}, 3],
                        'note': 'see /media/old/a.jpg',
                        'older': '/media/older/a.jpg',
                        'by_path': {'/media/old/k.jpg': 'v'},
                    },
                    location=(-93.5, 41.2),
                    heading=90.0,
                    embeddings={'sdk_test_paths_vec': [1.0, 2.0, 3.0]},
                    hashes={'phash': '00ff00ff00ff00ff'},
                ),
                Sample(filepath='/media/older/b.jpg', metadata={'p': '/media/old/b.png'}),
                Sample(filepath='s3://bucket/c.jpg', metadata={'n': 1}),
            ],
        )
        rule = {'/media/old': '/media/new'}

        @test("add_samples_from with path_prefixes gives the copies new paths in filepath and metadata, the rest kept")
        def _():
            target = Dataset('sdk-test-paths-target')
            result = target.add_samples_from(source, path_prefixes=rule)
            counted = {key: result.get(key) for key in ('seen', 'added', 'skipped', 'rewritten')}
            assert counted == {'seen': 3, 'added': 3, 'skipped': 0, 'rewritten': 1}, result
            a = target['/media/new/a.jpg']
            assert a.metadata == {
                'mask': {'maskPath': '/media/new/masks/a.png'},
                'lidar': [{'path': '/media/new/lidar/a.bin'}, 3],
                'note': 'see /media/old/a.jpg',
                'older': '/media/older/a.jpg',
                'by_path': {'/media/old/k.jpg': 'v'},
            }, a.metadata
            assert a.tags == ['t', '/media/old/tag'], a.tags
            assert target['/media/older/b.jpg'].metadata == {'p': '/media/new/b.png'}
            assert target['s3://bucket/c.jpg'].metadata == {'n': 1}
            assert client.get_vectors_by_ids(target.id, [a.id], ['sdk_test_paths_vec']) == {
                a.id: {'sdk_test_paths_vec': [1.0, 2.0, 3.0]},
            }
            assert client.get_hashes_by_ids(target.id, [a.id], ['phash']) == {a.id: {'phash': '00ff00ff00ff00ff'}}
            again = target.add_samples_from(source, path_prefixes=rule)
            counted = {key: again.get(key) for key in ('seen', 'added', 'skipped', 'rewritten')}
            assert counted == {'seen': 3, 'added': 0, 'skipped': 3, 'rewritten': 0}, again
            assert len(target) == 3
            assert source['/media/old/a.jpg'].metadata['mask'] == {'maskPath': '/media/old/masks/a.png'}

        _()

        @test("add_samples_from refuses a rule that would give two samples one path, before writing anything")
        def _():
            collide = Dataset('sdk-test-paths-collide')
            paths = ['/media/old/a.jpg', 'fill/0.jpg', 'fill/1.jpg', '/media/new/a.jpg']
            collide.add_samples([Sample(filepath=path) for path in paths])
            refused = Dataset('sdk-test-paths-refused')
            raised = _raises(ValueError, refused.add_samples_from, collide, path_prefixes=rule, batch_size=1)
            assert '/media/new/a.jpg' in str(raised) or '/media/old/a.jpg' in str(raised), raised
            assert len(refused) == 0, f"{len(refused)} samples were written before the refusal"

        _()

        @test("add_samples_from refuses a bad rule (not a dict, an empty old, a non-text new)")
        def _():
            refused = Dataset('sdk-test-paths-refused')
            for bad in ([('/media/old', '/media/new')], {'': '/media/new'}, {'/media/old': 1}):
                _raises(ValueError, refused.add_samples_from, source, path_prefixes=bad)
            assert len(refused) == 0

        _()

        @test("sample.save() after a changed filepath raises ValueError and writes nothing; the path put back saves")
        def _():
            s = source['/media/old/a.jpg']
            s.filepath = '/media/new/a.jpg'
            s.metadata['changed'] = True
            raised = _raises(ValueError, s.save)
            assert PATH_PHRASE in str(raised), raised
            assert 'changed' not in source['/media/old/a.jpg'].metadata
            s.filepath = '/media/old/a.jpg'
            s.save()
            assert source['/media/old/a.jpg'].metadata['changed'] is True

        _()

        @test("PATCH with another filepath is a 422 that writes nothing; with the sample's own path it is accepted")
        def _():
            sample_id = source['/media/old/a.jpg'].id
            url = f"{base}/datasets/{source.id}/samples/{sample_id}"
            moved = client.session.patch(url, json={'filepath': '/media/new/a.jpg', 'metadata': {'patched': 1}})
            assert moved.status_code == 422 and PATH_PHRASE in moved.text, (moved.status_code, moved.text[:300])
            assert 'patched' not in source['/media/old/a.jpg'].metadata
            same = client.session.patch(url, json={'filepath': '/media/old/a.jpg', 'metadata': {'patched': 1}})
            assert same.status_code == 200, (same.status_code, same.text[:300])
            assert source['/media/old/a.jpg'].metadata['patched'] == 1

        _()

        @test("add_samples_from links each copied object to the copy of its image")
        def _():
            with_objects = Dataset('sdk-test-paths-objects')
            image = with_objects.add_samples([Sample(filepath='s3://test/paths/img.jpg', metadata={'img': 'I'})])
            objects = []
            for name in ('o0', 'o1'):
                crop = Sample(
                    filepath=f's3://test/paths/crops/{name}.png',
                    metadata={'parent_id': image['ids'][0], 'obj': name},
                )
                crop.media_type = 'object'
                objects.append(crop)
            with_objects.add_samples(objects)
            copy = Dataset('sdk-test-paths-objects-copy')
            result = copy.add_samples_from(with_objects, batch_size=1)
            assert result == {'seen': 3, 'added': 3, 'skipped': 0, 'objects_without_image': 0}, result
            copied_image = copy['s3://test/paths/img.jpg'].id
            pivot = {
                'logic': 'AND',
                'filters': [
                    {'field': 'media_type', 'operator': '=', 'value': 'object'},
                    {'field': 'parent_id', 'operator': 'in', 'value': [copied_image]},
                ],
            }
            found = client.query_samples(copy.id, filters=pivot)['items']
            assert sorted(item['metadata']['obj'] for item in found) == ['o0', 'o1'], found
            finder = client.session.post(f"{base}/datasets/{copy.id}/objects/unlinked")
            assert finder.status_code == 200 and finder.json()['total'] == 0, finder.text[:300]

        _()
    finally:
        try:
            remove_leftovers()
            print("Deleted the path datasets")
        except Exception as error:  # a cleanup failure must not hide the results above
            print(f"Path datasets cleanup failed: {error}")


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
