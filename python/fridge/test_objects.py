"""Object/patch grain integration test — the in-dataset object view + find-unlabeled.

Covers the cross-grain behaviors that regressed when objects diverged from patches (these
tests would have caught them):
  - grain media_type filter: image grain excludes objects; object grain returns only objects
  - embedding_polygon on an object field resolves to the objects' PARENT images (image grain)
    AND to the objects themselves (object grain) — the lasso-resolves-to-parents behavior
    that makes objects work like patches
  - image -> objects pivot (parent_id IN selected images)
  - object -> patch cross-search (find-unlabeled) with region-level IoU exclusion

Runs against a live backend; creates + destroys an 'objgrain-test' dataset.

Usage (requires FRIDGE_TEST_URL pointing to a disposable backend):
    python -m fridge.test_objects
"""

import sys
import traceback

from fridge import Dataset, connect
from fridge._test_safety import require_test_url
from fridge.core.client import get_client

PASS = 0
FAIL = 0
ERRORS: list[tuple[str, Exception]] = []


def test(name):
    def decorator(fn):
        global PASS, FAIL
        try:
            fn()
            PASS += 1
            print(f"  PASS  {name}")
        except Exception as e:
            FAIL += 1
            ERRORS.append((name, e))
            print(f"  FAIL  {name}: {e}")
            traceback.print_exc()
        return fn

    return decorator


def run_tests():
    connect(require_test_url())
    client = get_client()
    client.login('admin', 'admin123')

    for d in client.get_datasets():
        if d['name'] == 'objgrain-test':
            client.delete_dataset(d['id'])

    ds = Dataset('objgrain-test', description='object/patch grain integration test')
    dsid = ds._id
    print(f"\nDataset: {ds.name} (id={dsid})")
    print("=" * 60)

    def query_ids(filters):
        return {it['id'] for it in client.query_samples(dsid, filters=filters, limit=1000)['items']}

    # Two images, each carrying one 2x2 patch tile (r0c0 = the top-left quadrant). imgB's tile
    # is close to imgA's objects in CLIP space; imgA's tile is identical to a covered object.
    img_a, img_b = client.add_samples(
        dsid,
        [
            {
                'filepath': 's3://objgrain/imgA.jpg',
                'media_type': 'image',
                # `detections` exercises the dotted-label (metadata->'detections' @>) path.
                # `note` is on imgA ONLY (and on no object) — exercises cross-grain is_null so the
                # image grid doesn't leak imgA via its child objects.
                'metadata': {'k': 'A', 'detections': [{'label': 'cat'}], 'note': 'x'},
                'embeddings': {'clip_img': [1, 0, 0, 0], 'clip_patch_2x2_r0c0': [1, 0, 0, 0]},
            },
            {
                'filepath': 's3://objgrain/imgB.jpg',
                'media_type': 'image',
                'metadata': {'k': 'B', 'detections': [{'label': 'dog'}]},
                'embeddings': {'clip_img': [0, 1, 0, 0], 'clip_patch_2x2_r0c0': [0.98, 0.02, 0, 0]},
            },
        ],
    )['ids']

    # imgA has two objects near the origin in clip_obj space; imgB has one far away. The
    # first imgA object covers the top-left quadrant ([0,0,.5,.5] == the r0c0 patch tile).
    obj_a0, obj_a1, obj_b0 = client.add_samples(
        dsid,
        [
            {
                'filepath': 's3://objgrain/imgA#o0',
                'media_type': 'object',
                'metadata': {'parent_id': img_a, 'crop_bbox': [0, 0, 0.5, 0.5], 'label': 'cat'},
                'embeddings': {'clip_obj': [1, 0, 0, 0]},
            },
            {
                'filepath': 's3://objgrain/imgA#o1',
                'media_type': 'object',
                'metadata': {'parent_id': img_a, 'crop_bbox': [0.5, 0.5, 0.5, 0.5], 'label': 'cat'},
                'embeddings': {'clip_obj': [0.9, 0.1, 0, 0]},
            },
            {
                'filepath': 's3://objgrain/imgB#o0',
                'media_type': 'object',
                'metadata': {'parent_id': img_b, 'crop_bbox': [0.5, 0.5, 0.5, 0.5], 'label': 'dog'},
                'embeddings': {'clip_obj': [0, 1, 0, 0]},
            },
        ],
    )['ids']

    # 2D coords for clip_obj so the embedding_polygon filter has points to test (A near the
    # origin, B far away).
    client.upload_coordinates(
        dsid,
        'clip_obj',
        [
            {'sample_id': obj_a0, 'x': 0.0, 'y': 0.0},
            {'sample_id': obj_a1, 'x': 0.2, 'y': 0.2},
            {'sample_id': obj_b0, 'x': 9.0, 'y': 9.0},
        ],
    )
    # 2D coords for clip_img too (imgA near the origin, imgB far) so we can test the REVERSE
    # direction: a lasso in IMAGE/clip_img space resolving to those images' objects.
    client.upload_coordinates(
        dsid,
        'clip_img',
        [
            {'sample_id': img_a, 'x': 0.0, 'y': 0.0},
            {'sample_id': img_b, 'x': 9.0, 'y': 9.0},
        ],
    )

    print("\n--- grain media_type filter ---")

    @test("image grain (media_type != object) returns only the images")
    def _():
        assert query_ids(
            {'logic': 'AND', 'filters': [{'field': 'media_type', 'operator': '!=', 'value': 'object'}]},
        ) == {
            img_a,
            img_b,
        }

    @test("object grain (media_type = object) returns only the objects")
    def _():
        assert query_ids(
            {'logic': 'AND', 'filters': [{'field': 'media_type', 'operator': '=', 'value': 'object'}]},
        ) == {
            obj_a0,
            obj_a1,
            obj_b0,
        }

    # The grid applies a panel's grain by COUPLING the two grains: a panel shows rows of its
    # grain that match the base filter directly OR are the cross-grain partner (parent image /
    # child object) of a matching row. Mirror FilterContext.withGrain EXACTLY so these tests
    # exercise the same filter the UI builds — for the lasso AND simple filters AND selections.
    def with_grain(base_filters, grain):
        grain_pred = (
            {'field': 'media_type', 'operator': '=', 'value': 'object'}
            if grain == 'object'
            else {'field': 'media_type', 'operator': '!=', 'value': 'object'}
        )
        if not base_filters:
            return {'logic': 'AND', 'filters': [grain_pred]}
        cross_op = 'parent_matches' if grain == 'object' else 'has_matching_object'

        # An embedding region carries a known grain (clip_obj -> object; clip_img/clip_patch_* ->
        # image). An off-grain region couples to the PARTNER row on its own; the remaining leaves
        # flow through the OR. Mirrors withGrain() in FilterContext.tsx.
        def field_grain(f):
            return 'object' if f == 'clip_obj' else 'image'

        def off_grain_emb(f):
            return f.get('operator') == 'embedding_polygon' and field_grain(f.get('field')) != grain

        off = [f for f in base_filters if off_grain_emb(f)]
        rest = [f for f in base_filters if not off_grain_emb(f)]
        # Annotated so mypy doesn't infer dict[str, str] from grain_pred and reject the nested
        # OR/value dicts appended below (CI runs mypy over the whole package, including this file).
        parts: list = [grain_pred]
        if rest:
            base_node = {'logic': 'AND', 'filters': rest}
            parts.append(
                {
                    'logic': 'OR',
                    'filters': [
                        {'field': '_cgself', 'operator': 'cg_self', 'value': base_node},
                        {'field': '_crossgrain', 'operator': cross_op, 'value': base_node},
                    ],
                },
            )
        for emb in off:
            parts.append({'field': '_crossemb', 'operator': cross_op, 'value': {'logic': 'AND', 'filters': [emb]}})
        return {'logic': 'AND', 'filters': parts}

    print("\n--- cross-grain link: embeddings lasso (both directions) ---")
    # Polygons around the origin: clip_obj covers obj_a0+obj_a1; clip_img covers imgA.
    region = {'field': 'clip_obj', 'operator': 'embedding_polygon', 'value': [[-1, -1], [1, -1], [1, 1], [-1, 1]]}
    img_region = {'field': 'clip_img', 'operator': 'embedding_polygon', 'value': [[-1, -1], [1, -1], [1, 1], [-1, 1]]}

    @test("object lasso + IMAGE grid -> the objects' PARENT images")
    def _():
        ids = query_ids(with_grain([region], 'image'))
        assert ids == {img_a}, f"expected parent imgA, got {ids}"

    @test("object lasso + OBJECT grid -> the objects in the region")
    def _():
        ids = query_ids(with_grain([region], 'object'))
        assert ids == {obj_a0, obj_a1}, f"expected obj_a0/obj_a1, got {ids}"

    @test("image lasso + OBJECT grid -> the lassoed images' OBJECTS")
    def _():
        ids = query_ids(with_grain([img_region], 'object'))
        assert ids == {obj_a0, obj_a1}, f"expected imgA's objects, got {ids}"

    @test("image lasso + IMAGE grid -> the images in the region")
    def _():
        ids = query_ids(with_grain([img_region], 'image'))
        assert ids == {img_a}, f"expected imgA, got {ids}"

    print("\n--- cross-grain link: MIXED grain (object selection AND off-grain lasso) ---")

    @test("object selection AND off-grain image-lasso + OBJECT grid -> intersection by parent")
    def _():
        # The reported bug: an object-grain selection (e.g. an object-similarity result) AND'd with
        # a clip_img lasso returned 0, because the whole mixed-grain filter was applied as ONE
        # cross-grain unit (no row is both an object in the selection AND an image in the region).
        # Now the off-grain region couples to the PARENT and intersects with the selection.
        ids = query_ids(with_grain([{'field': 'id', 'operator': 'in', 'value': [obj_a0]}, img_region], 'object'))
        assert ids == {obj_a0}, f"expected obj_a0 (selected AND parent imgA in region), got {ids}"

    @test("object selection whose parent is OUTSIDE the lasso + OBJECT grid -> empty")
    def _():
        # obj_b0's parent (imgB) is not in the clip_img region, so the intersection is correctly
        # empty — proving the AND really intersects (not vacuously everything).
        ids = query_ids(with_grain([{'field': 'id', 'operator': 'in', 'value': [obj_b0]}, img_region], 'object'))
        assert ids == set(), f"expected empty (parent imgB outside region), got {ids}"

    print("\n--- cross-grain link: SIMPLE filters (the bug — only the lasso cross-linked before) ---")

    @test("image-attr filter (k=A) + OBJECT grid -> the objects of image A")
    def _():
        ids = query_ids(with_grain([{'field': 'k', 'operator': '=', 'value': 'A'}], 'object'))
        assert ids == {obj_a0, obj_a1}, f"expected imgA's objects, got {ids}"

    @test("object-attr filter (label=cat) + IMAGE grid -> the parent images of cat objects")
    def _():
        ids = query_ids(with_grain([{'field': 'label', 'operator': '=', 'value': 'cat'}], 'image'))
        assert ids == {img_a}, f"expected imgA (parent of the cats), got {ids}"

    @test("object-attr filter (label=cat) + OBJECT grid -> the cat objects themselves")
    def _():
        ids = query_ids(with_grain([{'field': 'label', 'operator': '=', 'value': 'cat'}], 'object'))
        assert ids == {obj_a0, obj_a1}, f"expected the cat objects, got {ids}"

    print("\n--- cross-grain link: SELECTION (grid checkboxes / brushed ids) ---")

    @test("select image A + OBJECT grid -> image A's objects")
    def _():
        ids = query_ids(with_grain([{'field': 'id', 'operator': 'in', 'value': [img_a]}], 'object'))
        assert ids == {obj_a0, obj_a1}, f"expected imgA's objects, got {ids}"

    @test("select object obj_b0 + IMAGE grid -> its parent image B")
    def _():
        ids = query_ids(with_grain([{'field': 'id', 'operator': 'in', 'value': [obj_b0]}], 'image'))
        assert ids == {img_b}, f"expected imgB (parent of obj_b0), got {ids}"

    print("\n--- cross-grain link: EXCLUSION filters (!= / not_in) — the whole-grain leak ---")
    # Before the presence-guard fix EVERY exclusion filter returned the whole grain (NULL-tolerant
    # negation vacuously matched the grain that lacks the field). These assert the cross-linked
    # subset instead, and FAIL (return the full set) without the guard.

    @test("EXCLUDE image-attr (k != A) + OBJECT grid -> objects of the non-A images only")
    def _():
        ids = query_ids(with_grain([{'field': 'k', 'operator': '!=', 'value': 'A'}], 'object'))
        assert ids == {obj_b0}, f"expected only imgB's object (was the whole grain), got {ids}"

    @test("EXCLUDE object-attr (label != cat) + OBJECT grid -> the non-cat objects only")
    def _():
        ids = query_ids(with_grain([{'field': 'label', 'operator': '!=', 'value': 'cat'}], 'object'))
        assert ids == {obj_b0}, f"expected only the dog object (was the whole grain), got {ids}"

    @test("EXCLUDE object-attr (label != cat) + IMAGE grid -> images with a non-cat object only")
    def _():
        ids = query_ids(with_grain([{'field': 'label', 'operator': '!=', 'value': 'cat'}], 'image'))
        assert ids == {img_b}, f"expected only imgB (has a dog object), got {ids}"

    @test("EXCLUDE via 'not in' (the UI's facet-exclude operator) + OBJECT grid -> subset, not all")
    def _():
        # 'not in' (with a space) is the operator the frontend actually emits; it must be guarded too.
        ids = query_ids(with_grain([{'field': 'label', 'operator': 'not in', 'value': ['cat']}], 'object'))
        assert ids == {obj_b0}, f"expected only the non-cat object (was the whole grain), got {ids}"

    @test("EXCLUDE native id (duplicates 'collapse') + OBJECT grid -> excludes it, no cross-branch leak")
    def _():
        # id exclusion is a same-grain row-identity filter: the cross-grain partner has no meaning,
        # so the excluded object must NOT reappear via parent_matches.
        ids = query_ids(with_grain([{'field': 'id', 'operator': 'not in', 'value': [obj_a0]}], 'object'))
        assert ids == {obj_a1, obj_b0}, f"expected obj_a0 excluded (was the whole grain), got {ids}"

    @test("EXCLUDE dotted-label via 'not in' [list] + IMAGE grid -> images without a cat detection")
    def _():
        # 'not in' + a LIST on a dotted-label field (detections.label) used to 500 (the list got
        # wrapped and bound to ::text). Must return the cross-linked subset.
        ids = query_ids(with_grain([{'field': 'detections.label', 'operator': 'not in', 'value': ['cat']}], 'image'))
        assert ids == {img_b}, f"expected only imgB (no cat detection), got {ids}"

    @test("EXCLUDE dotted-label via 'not in' [list] + OBJECT grid -> objects of those images")
    def _():
        ids = query_ids(with_grain([{'field': 'detections.label', 'operator': 'not in', 'value': ['cat']}], 'object'))
        assert ids == {obj_b0}, f"expected imgB's object (no cat detection), got {ids}"

    @test("EXCLUDE via NOT-group (sidebar exclude-contains) + OBJECT grid -> non-leaking subset")
    def _():
        # The sidebar's exclude-contains/exclude-range emit a NOT GROUP (logic='NOT'), strictified
        # by a separate branch from leaf !=/not in. NOT(k=A) excludes imgA; only imgB's object
        # survives. Without the single-field exists guard, cg_self would vacuously match every
        # object (objects have no `k`) and leak the whole grain.
        ids = query_ids(
            with_grain([{'logic': 'NOT', 'filters': [{'field': 'k', 'operator': '=', 'value': 'A'}]}], 'object'),
        )
        assert ids == {obj_b0}, f"expected imgB's object only, got {ids}"

    print("\n--- cross-grain ABSENCE ops (is_null / not_exists) must not leak via the partner ---")

    @test("is_null on an image field + IMAGE grid -> only the image lacking it (no partner leak)")
    def _():
        # `note` is on imgA only. imgB lacks it -> should match. The bug: has_matching_object
        # rendered `note IS NULL` against OBJECT rows (which never have `note`) -> vacuously true
        # -> imgA leaked in via its child objects. Now the partner arm is dropped for absence ops.
        ids = query_ids(with_grain([{'field': 'note', 'operator': 'is_null', 'value': None}], 'image'))
        assert ids == {img_b}, f"expected only imgB (lacks note), got {ids}"

    @test("not_exists on an image field present on ALL images + IMAGE grid -> empty (no partner leak)")
    def _():
        # Both images HAVE detections; objects never do. Without the fix has_matching_object made
        # every image with a child object match -> both leaked; correct answer is empty.
        ids = query_ids(with_grain([{'field': 'detections', 'operator': 'not_exists', 'value': None}], 'image'))
        assert ids == set(), f"expected empty (every image has detections), got {ids}"

    # NOTE (known limitation): the symmetric object-grid case — an image-only field's absence
    # filtered on the OBJECT grid — still matches every object via cg_self (objects literally lack
    # the field), since the SQL layer can't tell an image-only key from an object key without
    # per-grain field-presence. Documented; not a data-correctness leak (it under-filters, never
    # shows wrong rows).

    print("\n--- image -> objects pivot ---")

    @test("parent_id IN [imgA] returns imgA's objects (plain field, not dotted)")
    def _():
        ids = query_ids(
            {
                'logic': 'AND',
                'filters': [
                    {'field': 'media_type', 'operator': '=', 'value': 'object'},
                    {'field': 'parent_id', 'operator': 'in', 'value': [img_a]},
                ],
            },
        )
        assert ids == {obj_a0, obj_a1}, f"expected imgA objects, got {ids}"

    print("\n--- object -> patch cross-search (find-unlabeled) ---")

    @test("object->patches surfaces an UNCOVERED similar tile + IoU-excludes covered ones")
    def _():
        resp = client.session.post(
            f"{client.base_url}/search/object-to-patches",
            json={'dataset_id': dsid, 'object_sample_id': obj_a0, 'base_field': 'clip', 'limit': 10},
        )
        resp.raise_for_status()
        parents = {h['sample_id'] for h in resp.json()}
        # imgB's r0c0 tile is similar to the seed and NOT covered by an object -> surfaced.
        assert img_b in parents, f"expected imgB's uncovered tile surfaced, got {parents}"
        # imgA's r0c0 tile (== the seed object's box) is covered -> region-level IoU excludes it.
        assert img_a not in parents, f"imgA's covered tile should be IoU-excluded, got {parents}"

    print("\n--- order_by_parent: image/patch search ranks the OBJECT grid by parent image ---")

    def query_order(order_ids, by_parent):
        resp = client.session.post(
            f"{client.base_url}/datasets/{dsid}/samples/query",
            json={
                'filters': {'logic': 'AND', 'filters': [{'field': 'media_type', 'operator': '=', 'value': 'object'}]},
                'order_by_ids': order_ids,
                'order_by_parent': by_parent,
                'limit': 100,
            },
        )
        resp.raise_for_status()
        return [it['id'] for it in resp.json()['items']]

    @test("order_by_parent ranks objects by their parent image's position (imgB first)")
    def _():
        ids = query_order([img_b, img_a], True)
        assert ids == [obj_b0, obj_a0, obj_a1], f"expected [b0,a0,a1], got {ids}"

    @test("order_by_parent flips with the image order, id tiebreak stable (imgA first)")
    def _():
        # imgA's two objects tie on parent rank; the secondary `id` keeps the order total/stable
        # so offset paging can't duplicate or skip a tied row.
        ids = query_order([img_a, img_b], True)
        assert ids == [obj_a0, obj_a1, obj_b0], f"expected [a0,a1,b0], got {ids}"

    print("\n--- concept_exclude cross-grain leak: an off-grain concept must not vacuously match ---")
    # A clip_img concept is scored only on IMAGE rows; a clip_obj concept only on OBJECT rows.
    img_concept = client.create_concept(dsid, 'cimg', seed_ids=[img_a], embedding_field='clip_img', kind='seed')
    obj_concept = client.create_concept(dsid, 'cobj', seed_ids=[obj_a0], embedding_field='clip_obj', kind='seed')
    ic = img_concept.get('id') or img_concept.get('concept_id')
    oc = obj_concept.get('id') or obj_concept.get('concept_id')

    def cexclude(cid, thr):
        return {
            'field': '_concept',
            'operator': 'concept_exclude',
            'value': {'concept_id': cid, 'op': '>', 'threshold': thr},
        }

    @test("clip_img concept_exclude(exclude ALL images) + OBJECT grid -> empty (no whole-grain leak)")
    def _():
        # Every image is excluded, so 'objects of non-excluded images' is empty. The bug made
        # cg_self vacuously TRUE for objects (which have no clip_img score) -> all objects leaked.
        ids = query_ids(with_grain([cexclude(ic, -0.5)], 'object'))
        assert ids == set(), f"expected empty (all parents excluded), got {ids}"

    @test("clip_img concept_exclude(exclude NO image) + OBJECT grid -> all objects (path still works)")
    def _():
        ids = query_ids(with_grain([cexclude(ic, 1.5)], 'object'))
        assert ids == {obj_a0, obj_a1, obj_b0}, f"expected all objects, got {ids}"

    @test("clip_obj concept_exclude(exclude ALL objects) + IMAGE grid -> empty (symmetric, no leak)")
    def _():
        # Images have no clip_obj score; the bug leaked the whole image grain the same way.
        ids = query_ids(with_grain([cexclude(oc, -0.5)], 'image'))
        assert ids == set(), f"expected empty (all child objects excluded), got {ids}"

    # SAME-GRAIN pairing (H2): the concept grain == the panel grain, so the cross-grain PARTNER is
    # the off-grain row. The partner branch must ALSO carry the presence guard or it vacuously
    # matches (the parent image has no clip_obj score) and leaks nearly the whole object grain.
    @test("clip_obj concept_exclude(exclude ALL) + OBJECT grid -> empty (partner branch guarded)")
    def _():
        ids = query_ids(with_grain([cexclude(oc, -0.5)], 'object'))
        assert ids == set(), f"expected empty (no object survives the exclude), got {ids}"

    @test("clip_img concept_exclude(exclude ALL) + IMAGE grid -> empty (partner branch guarded)")
    def _():
        ids = query_ids(with_grain([cexclude(ic, -0.5)], 'image'))
        assert ids == set(), f"expected empty (no image survives the exclude), got {ids}"

    print("\n--- compute_visualization reduces a PATCH-scale field via the client-side path ---")

    @test("compute_visualization('clip_patch_NxN') expands tiles, reduces jointly, field appears")
    def _():
        # A patch-scale field is virtual — its data lives across one regular field per tile. The
        # server-side /reduce endpoint holds every tile vector in the worker and drops the
        # connection on large patch fields; compute_visualization must instead expand the tiles,
        # reduce them JOINTLY, and upload per-tile coords through the normal API. Build a FULL 2x2
        # grid over 16 images (64 tiles, well past the >=5 floor) so the joint reduction is real.
        for d in client.get_datasets():
            if d['name'] == 'patchviz-test':
                client.delete_dataset(d['id'])
        pv = Dataset('patchviz-test', description='patch-scale compute_visualization integration')
        samples = []
        for i in range(16):
            emb = {'clip_img': [float(i + 1), 1.0, 0.0, 0.0]}
            for r in range(2):
                for c in range(2):
                    emb[f'clip_patch_2x2_r{r}c{c}'] = [float(i + 1), float(r + 1), float(c + 1), 1.0]
            samples.append({'filepath': f's3://patchviz/img{i}.jpg', 'media_type': 'image', 'embeddings': emb})
        sample_ids = client.add_samples(pv._id, samples)['ids']

        try:
            # BEFORE the fix this raised "Not enough embeddings (found 0)" — exact-match get_vectors
            # never saw the virtual field. Now it expands to the 4 tiles (64 vectors) and reduces.
            pv.compute_visualization(field_name='clip_patch_2x2')

            fields = client.get_embedding_fields(pv._id)
            names = {(f['field_name'] if isinstance(f, dict) else f) for f in fields}
            assert 'clip_patch_2x2' in names, f"patch field should appear after reduce, got {names}"
            # Verify per-tile coord ROUTING (the risky new idx_by_field grouping): EACH of the 4
            # tiles must carry reduced coords for ALL 16 samples. A bug that mis-routed every point
            # to one tile (or dropped tiles) would leave the others without coords here.
            for r in range(2):
                for c in range(2):
                    pts = client.lookup_embedding_coordinates(
                        pv._id,
                        sample_ids,
                        field=f'clip_patch_2x2_r{r}c{c}',
                    )['points']
                    got = {p['sample_id'] for p in pts if p.get('x') is not None and p.get('y') is not None}
                    assert got == set(sample_ids), f"tile r{r}c{c}: coords for all 16 samples expected, got {len(got)}"
        finally:
            client.delete_dataset(pv._id)

    client.delete_dataset(dsid)
    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed")
    for name, err in ERRORS:
        print(f"  {name}: {err}")
    print("=" * 60)
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if run_tests() else 1)
