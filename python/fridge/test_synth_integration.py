"""Synthetic-data integration tests for Freedom Ridge.

Generates a fully-annotated synthetic dataset (real images + every typed-label
field) and exercises annotation round-trips, filtering-by-annotation, aggregations,
media serving, to_patches, evaluation, and (when torch+transformers are present)
patch embeddings + patch search — end to end against a LIVE backend.

Usage (requires FRIDGE_TEST_URL pointing to a disposable backend):
    python -m fridge.test_synth_integration

Mirrors python/fridge/test_sdk.py: explicit backend at FRIDGE_TEST_URL,
admin/admin123, self-creating + self-cleaning 'synth-test' dataset.
"""

import os
import shutil
import sys
import traceback

from PIL import Image

from fridge import Dataset, connect, geo_within
from fridge._test_safety import require_test_url
from fridge.core.client import get_client
from fridge.synth import SyntheticSpec, generate

PASS = 0
FAIL = 0
SKIP = 0
ERRORS: list[tuple[str, Exception]] = []


def test(name):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            global PASS, FAIL
            try:
                fn(*args, **kwargs)
                PASS += 1
                print(f"  PASS  {name}")
            except _Skip as s:
                global SKIP
                SKIP += 1
                print(f"  SKIP  {name}: {s}")
            except Exception as e:
                FAIL += 1
                ERRORS.append((name, e))
                print(f"  FAIL  {name}: {e}")
                traceback.print_exc()

        return wrapper

    return decorator


class _Skip(Exception):
    pass


def run_tests():
    connect(require_test_url())
    client = get_client()
    client.login('admin', 'admin123')

    for d in client.get_datasets():
        if d['name'] in ('synth-test', 'synth-test-patches'):
            client.delete_dataset(d['id'])

    # Deterministic synthetic dataset: 12 images (11 annotated + 1 empty).
    spec = SyntheticSpec(num_images=12, seed=0)
    gen = generate(spec)
    manifest = gen.manifest()
    expected_class_counts = gen.class_counts()
    total_bboxes = sum(e['bbox_count'] for e in manifest.values())
    annotated = sum(1 for e in manifest.values() if e['has_annotations'])
    classes = sorted(expected_class_counts.keys())
    patches_dir = os.path.join(gen.data_root, 'synth', 'synthtest_patches')

    print(f"\nGenerated {len(gen.samples)} synthetic images at {gen.out_dir}")
    print(
        f"  annotated={annotated} empty={len(gen.samples) - annotated} "
        f"total_detections={total_bboxes} class_counts={expected_class_counts}",
    )
    print("=" * 60)

    ds = Dataset('synth-test')
    ds.add_samples(gen.fridge_samples())

    # --- TYPED-LABEL ROUND-TRIP ---
    print("\n--- Typed-label round-trip ---")

    @test("add_samples preserves every typed-label field on read-back")
    def _():
        g0 = gen.samples[0]  # first sample is annotated (the empty one is last)
        s = ds[g0.filepath]
        md = s.metadata
        exp = manifest[g0.filepath]
        dets = md['ground_truth']
        assert len(dets) == exp['bbox_count'], f"det count {len(dets)} != {exp['bbox_count']}"
        d0 = dets[0]
        assert d0['label'] in classes, f"bad label {d0['label']}"
        assert len(d0['bbox']) == 4 and all(0.0 <= v <= 1.0 for v in d0['bbox']), f"bbox {d0['bbox']}"
        assert 'confidence' in d0, "confidence dropped"
        assert d0.get('attributes', {}).get('occluded') is True, "attribute not preserved"
        seg = md['segmentation']
        assert seg['maskPath'].endswith('_mask.png') and 'mask_targets' in seg, f"seg {seg}"
        hm = md['heatmap']
        assert hm['type'] == 'heatmap' and hm['range'] == [0, 255] and hm['colormap'] == 'viridis', f"hm {hm}"
        poly = md['polygons'][0]
        assert poly['closed'] and poly['filled'] and len(poly['points']) >= 3, f"poly {poly.keys()}"
        kp = md['keypoints'][0]
        assert kp.get('_type') == 'keypoints' and len(kp['points']) >= 1, f"kp {kp}"
        assert md['classification']['label'] in classes, "classification dropped"

    _()

    # --- FILTER BY ANNOTATION ---
    print("\n--- Filter by annotation ---")

    @test("filter ground_truth.label matches the manifest, per class")
    def _():
        for cls in classes:
            want = len(gen.samples_with_label(cls))
            got = client.query_samples(
                ds._id,
                limit=1,
                filters={'field': 'ground_truth.label', 'operator': '=', 'value': cls},
            )['total']
            assert got == want, f"ground_truth.label={cls}: got {got}, want {want}"

    _()

    @test("filter polygons.label matches detections (per-element EXISTS path)")
    def _():
        for cls in classes:
            want = len(gen.samples_with_label(cls))
            got = client.query_samples(
                ds._id,
                limit=1,
                filters={'field': 'polygons.label', 'operator': '=', 'value': cls},
            )['total']
            assert got == want, f"polygons.label={cls}: got {got}, want {want}"

    _()

    @test("filter ground_truth.confidence (numeric per-element comparison)")
    def _():
        # every annotated sample has detections with confidence in [0.55, 0.99]
        ge = client.query_samples(
            ds._id,
            limit=1,
            filters={'field': 'ground_truth.confidence', 'operator': '>', 'value': 0.5},
        )['total']
        assert ge == annotated, f"conf>0.5 got {ge}, want {annotated}"
        none = client.query_samples(
            ds._id,
            limit=1,
            filters={'field': 'ground_truth.confidence', 'operator': '>', 'value': 0.999},
        )['total']
        assert none == 0, f"conf>0.999 got {none}, want 0"

    _()

    @test("count_values('ground_truth.label') equals the manifest class counts")
    def _():
        counts = ds.all().count_values('ground_truth.label')
        assert counts == expected_class_counts, f"got {counts}, want {expected_class_counts}"

    _()

    @test("empty-annotation sample is excluded by annotation filters, not counted as data loss")
    def _():
        total = client.query_samples(ds._id, limit=1)['total']
        assert total == spec.num_images, f"total {total} != {spec.num_images}"
        has_any = client.query_samples(
            ds._id,
            limit=1,
            filters={'field': 'ground_truth.confidence', 'operator': '>', 'value': 0.0},
        )['total']
        assert has_any == annotated, f"annotated {has_any} != {annotated}"
        assert total - has_any == spec.num_images - annotated  # the one empty sample

    _()

    # --- GEOLOCATION ---
    print("\n--- Geolocation ---")

    @test("samples carry lat/long and geo_within filters to a single site")
    def _():
        locs = [e.get('location') for e in manifest.values()]
        assert all(loc is not None for loc in locs), "some samples are missing a location"
        site = (-122.4194, 37.7749)  # first generator site (SF)
        d = 0.1
        box = [
            [site[0] - d, site[1] - d],
            [site[0] + d, site[1] - d],
            [site[0] + d, site[1] + d],
            [site[0] - d, site[1] + d],
        ]
        want = sum(1 for loc in locs if site[0] - d <= loc[0] <= site[0] + d and site[1] - d <= loc[1] <= site[1] + d)
        got = ds.samples[geo_within(box)].count()
        assert want > 0 and got == want, f"geo_within got {got}, want {want}"

    _()

    # --- MEDIA SERVING (render path) ---
    print("\n--- Media serving ---")

    @test("backend serves the generated image (proves it's under FRIDGE_DATA_ROOT)")
    def _():
        item = client.query_samples(
            ds._id,
            limit=1,
            filters={'field': 'ground_truth.confidence', 'operator': '>', 'value': 0.0},
        )['items'][0]
        r = client.session.get(f"{client.base_url}/samples/{item['id']}/media")
        assert r.status_code == 200, f"media status {r.status_code}"
        assert r.headers.get('content-type', '').startswith('image'), r.headers.get('content-type')
        assert len(r.content) > 100, "empty image body"

    _()

    # --- PATCHES (from bounding boxes) ---
    print("\n--- to_patches ---")

    @test("to_patches crops one real sub-image per detection")
    def _():
        patch_ds = ds.to_patches(
            'ground_truth',
            'synth-test-patches',
            patches_dir,
            image_loader=lambda p: Image.open(p).convert('RGB'),
        )
        n = client.query_samples(patch_ds._id, limit=1)['total']
        assert n == total_bboxes, f"crops {n} != detections {total_bboxes}"
        crop = client.query_samples(patch_ds._id, limit=1)['items'][0]
        md = crop.get('metadata') or {}
        assert md.get('parent_id') and md.get('label') in classes and len(md.get('bbox', [])) == 4, f"crop md {md}"

    _()

    # --- EVALUATION ---
    print("\n--- Evaluation ---")

    @test("evaluate_detections(predictions vs detections) returns a sane report")
    def _():
        rep = ds.evaluate_detections('predictions', 'ground_truth')
        assert 'mAP' in rep and 'per_class_ap' in rep, f"keys {list(rep)}"
        assert 0.0 < rep['mAP'] <= 1.0, f"mAP {rep['mAP']}"  # preds are mostly-correct copies

    _()

    @test("evaluate_classifications(pred vs gt) returns accuracy in [0,1]")
    def _():
        rep = ds.evaluate_classifications('pred_classification', 'classification')
        assert 'accuracy' in rep and 0.0 <= rep['accuracy'] <= 1.0, f"acc {rep.get('accuracy')}"

    _()

    # --- IMAGE HASHES ---
    print("\n--- Image hashes ---")

    @test("compute_hashes populates perceptual hashes for every image")
    def _():
        try:
            import imagehash  # noqa: F401
        except ImportError:
            raise _Skip("imagehash not installed (install freedomridge[hashes])")
        ds.compute_hashes(image_loader=lambda p: Image.open(p).convert('RGB'))
        fields = ds.get_hash_fields()
        assert 'dhash_8' in fields and 'phash_8' in fields, f"hash fields {fields}"
        stats = ds.get_hash_stats()
        assert 'dhash_8' in stats['fields'] and stats['total_samples'] == spec.num_images, f"hash stats {stats}"

    _()

    # --- WHOLE-IMAGE SIMILARITY INDEX ---
    print("\n--- Whole-image similarity index ---")

    @test("whole-image CLIP embeddings upload + find-similar")
    def _():
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except ImportError:
            raise _Skip("torch/transformers not installed (install freedomridge[embeddings])")
        from fridge.synth.demo import _clip_image_embeddings

        samples = ds.all().to_list()
        ids = [s.id for s in samples]
        vecs = _clip_image_embeddings([s.filepath for s in samples])
        client.upload_embeddings(
            ds._id,
            [{'sample_id': sid, 'vector': v, 'field_name': 'clip'} for sid, v in zip(ids, vecs)],
        )
        assert client.get_vectors(ds._id, field_name='clip', limit=1)['total'] == spec.num_images
        results = client.similarity_search(ds._id, sample_id=ids[0], field_name='clip', limit=5)
        assert len(results) >= 1, "similarity search returned nothing"

    _()

    # --- PATCH EMBEDDINGS + SEARCH (needs torch+transformers) ---
    print("\n--- Patch embeddings + search (optional) ---")

    @test("compute_patch_embeddings + patch_similarity_search")
    def _():
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except ImportError:
            raise _Skip("torch/transformers not installed (install freedomridge[patch-embeddings])")
        ds.compute_patch_embeddings(
            scales=['2x2'],
            image_loader=lambda p: Image.open(p).convert('RGB'),
            batch_size=8,
            reduce=False,
        )
        # reduce=False skips UMAP (so the field isn't panel-registered); validate the raw
        # patch vectors uploaded + are queryable via patch similarity search end-to-end.
        item = client.query_samples(
            ds._id,
            limit=1,
            filters={'field': 'ground_truth.confidence', 'operator': '>', 'value': 0.0},
        )['items'][0]
        results = ds.patch_similarity_search(item['id'], patch_scale='2x2', patch_row=0, patch_col=0, limit=5)
        assert len(results) >= 1, "patch similarity search returned nothing (patches not uploaded?)"
        assert 'patch_row' in results[0] and 'patch_col' in results[0], f"patch result shape {results[0]}"

    _()

    # --- DETERMINISM ---
    print("\n--- Determinism ---")

    @test("generate(seed=0) is reproducible (identical labels)")
    def _():
        import tempfile

        with tempfile.TemporaryDirectory() as t:
            g2 = generate(SyntheticSpec(num_images=12, seed=0), out_dir=t)

            def strip(m):
                return [
                    {k: v for k, v in e.items() if not (k.endswith('_path') or k == 'maskPath')} for e in m.values()
                ]

            assert strip(g2.manifest()) == strip(manifest), "non-deterministic manifest"

    _()

    # --- CLEANUP ---
    print("\n--- Cleanup ---")
    for d in client.get_datasets():
        if d['name'] in ('synth-test', 'synth-test-patches'):
            client.delete_dataset(d['id'])
    for path in (gen.out_dir, patches_dir):
        shutil.rmtree(path, ignore_errors=True)
    print("Deleted datasets + generated images")

    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed, {SKIP} skipped")
    if ERRORS:
        print("\nFailures:")
        for name, err in ERRORS:
            print(f"  {name}: {err}")
    print("=" * 60)
    return FAIL == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
