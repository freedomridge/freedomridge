"""Sync a dataset between two Freedom Ridge instances over the network.

Copies samples, metadata, embeddings, and image hashes — a complete
dataset transfer via the API. No direct database access needed.

Usage:
    python -m fridge.sync_between_instances \
        --source http://source-ip:8000/api \
        --target http://target-ip:8000/api \
        --dataset large-dataset

    # Skip embeddings/hashes for a faster metadata-only sync
    python -m fridge.sync_between_instances \
        --source http://source-ip:8000/api \
        --target http://target-ip:8000/api \
        --dataset large-dataset --skip-embeddings --skip-hashes
"""

import argparse
import sys
import time
from typing import Dict

from .core.client import Client, _raise_for_status


def sync(
    source_url: str,
    target_url: str,
    dataset_name: str,
    source_user: str = "admin",
    source_pass: str = "admin123",
    target_user: str = "admin",
    target_pass: str = "admin123",
    batch_size: int = 1000,
    skip_embeddings: bool = False,
    skip_hashes: bool = False,
):
    # --- Connect ---
    source = Client(source_url)
    source.login(source_user, source_pass)
    print(f"Connected to source: {source_url}")

    target = Client(target_url)
    target.login(target_user, target_pass)
    print(f"Connected to target: {target_url}")

    # --- Dataset setup ---
    source_ds = source.get_dataset(dataset_name)
    if not source_ds:
        print(f"Dataset '{dataset_name}' not found on source")
        sys.exit(1)
    source_id = source_ds['id']

    target_ds = target.get_dataset(dataset_name)
    if target_ds:
        target_id = target_ds['id']
        print(f"Target dataset exists (id: {target_id})")
    else:
        target_ds = target.create_dataset(
            dataset_name,
            description=source_ds.get('description'),
            is_global=bool(source_ds.get('is_global')),
        )
        target_id = target_ds['id']
        print(f"Created target dataset (id: {target_id})")

    # Copy settings
    target.session.patch(
        f"{target.base_url}/datasets/{target_id}",
        json={
            'media_fields': source_ds.get('media_fields', ['filepath']),
            'grid_media_field': source_ds.get('grid_media_field', 'filepath'),
            'modal_media_field': source_ds.get('modal_media_field', 'filepath'),
            'segmentation_config': source_ds.get('segmentation_config'),
        },
    )

    # --- Sync samples ---
    resp = source.get_samples(source_id, limit=1, offset=0)
    total = resp.get('total', 0)
    print(f"\n--- Syncing {total:,} samples ---")

    # Build mapping: source sample_id -> target sample_id
    id_map = {}
    offset = 0
    synced = 0
    # Per-field (name, transferred, expected) so the end of the run can refuse to claim success
    # on a short transfer instead of printing ✅ over it.
    totals: Dict[str, list] = {'hashes': [], 'embeddings': []}
    start = time.time()

    # Dynamic array columns (inferred_tags, …) are real TEXT[] columns, not metadata, so they are
    # returned as top-level keys and have to be carried across explicitly — SampleCreate takes them
    # under `array_fields`. They used to be dropped silently.
    # `tags` is the built-in samples column, sent as row['tags'] below. It is listed with the array
    # fields but is not a dynamic one, and the target refuses it under `array_fields`.
    array_field_names = [f['name'] for f in source.list_array_fields(source_id) if f['name'] != 'tags']
    if array_field_names:
        print(f"Array fields to carry across: {', '.join(array_field_names)}")
        existing = {f['name'] for f in target.list_array_fields(target_id)}
        for name in array_field_names:
            if name not in existing:
                target.create_array_field(target_id, name)

    while offset < total:
        # query_samples (POST /samples/query), NOT get_samples: GET /samples declares
        # response_model=SampleListResponse, whose Sample model has no keys for the dynamic array
        # columns, so Pydantic STRIPS them from the response. Reading samples through it meant the
        # array_fields carried below were always empty — the target got the columns created and
        # every value blank. /samples/query has no response_model and returns the raw rows,
        # including the array columns.
        resp = source.query_samples(source_id, limit=batch_size, offset=offset)
        items = resp.get('items', [])
        if not items:
            break

        payload = []
        for item in items:
            row = {
                'filepath': item['filepath'],
                'media_type': item.get('media_type', 'image'),
                'tags': item.get('tags', []),
                'metadata': item.get('metadata', {}),
                'location': item.get('location'),
                'heading': item.get('heading'),
            }
            arrays = {name: item[name] for name in array_field_names if item.get(name)}
            if arrays:
                row['array_fields'] = arrays
            payload.append(row)

        # add_samples wraps the list into {"samples": ...} itself — pass the bare
        # list, not a pre-wrapped dict (double-wrapping 422s on the first batch).
        # on_duplicate='skip' so re-syncing an existing target doesn't error.
        target.add_samples(target_id, payload, on_duplicate='skip')

        # Map source->target by FILEPATH, not a positional zip of returned ids:
        # 'skip' returns ids only for fresh inserts, so on a re-sync (targets
        # already exist) a positional zip misaligns source and target ids
        # . Look up the target id for every source filepath in the batch.
        fp_to_target = target.lookup_samples(target_id, 'filepath', [item['filepath'] for item in items])
        for item in items:
            tgt = fp_to_target.get(item['filepath'])
            if tgt is not None:
                id_map[item['id']] = tgt

        synced += len(items)
        offset += batch_size

        elapsed = time.time() - start
        rate = synced / elapsed if elapsed > 0 else 0
        print(f"  Samples: {synced:,} / {total:,} ({rate:.0f}/s)")

    print(f"Samples complete: {synced:,} in {time.time() - start:.0f}s")

    # --- Sync embeddings ---
    if not skip_embeddings:
        # field-inventory, NOT /fields: /fields is the SCATTER's list — only fields with
        # computed coords, with patch fields collapsed into pseudo names like `clip_patch_4x4`
        # that match no row. Using it meant an embedded-but-unprojected field (e.g. millions of dinov3
        # vectors) was silently never copied, and patch fields fetched 0 vectors, all under ✅.
        inventory = source.get_embedding_field_inventory(source_id)
        emb_fields = [f['field_name'] for f in inventory]
        emb_counts = {f['field_name']: f['count'] for f in inventory}

        if emb_fields:
            print(f"\n--- Syncing embeddings ({len(emb_fields)} fields) ---")

            for field_name in emb_fields:
                print(f"  Field: {field_name}")
                emb_offset = 0
                emb_synced = 0
                emb_expected = emb_counts.get(field_name)
                page_size = 5000

                while True:
                    resp = source.session.get(
                        f"{source.base_url}/datasets/{source_id}/embeddings/vectors",
                        params={'field_name': field_name, 'limit': page_size, 'offset': emb_offset},
                    )
                    if not resp.ok:
                        print(f"    Error fetching vectors: {resp.status_code}")
                        break

                    data = resp.json()
                    items = data.get('items', [])
                    if not items:
                        break

                    # Remap sample IDs
                    emb_payload = []
                    for item in items:
                        target_sample_id = id_map.get(item['sample_id'])
                        if target_sample_id:
                            emb_payload.append(
                                {
                                    'sample_id': target_sample_id,
                                    'vector': item['vector'],
                                    'field_name': field_name,
                                },
                            )

                    if emb_payload:
                        target.upload_embeddings(target_id, emb_payload)

                    emb_synced += len(items)
                    emb_offset += page_size
                    print(f"    {emb_synced:,} / {data.get('total', '?')} vectors")

                    if len(items) < page_size:
                        break

                # Sync UMAP coordinates
                emb_offset = 0
                while True:
                    resp = source.session.get(
                        f"{source.base_url}/datasets/{source_id}/embeddings/vectors",
                        params={'field_name': field_name, 'limit': page_size, 'offset': emb_offset},
                    )
                    # Note: vectors endpoint doesn't return x/y coordinates
                    # UMAP would need to be recomputed on the target
                    break

                print(f"    {field_name}: {emb_synced:,} vectors synced")
                # A truncated embedding transfer must count as a shortfall too. The `break` on a
                # non-2xx above means a mid-field failure otherwise ended in ✅ with a partial copy.
                if emb_expected is not None:
                    totals['embeddings'].append((field_name, emb_synced, emb_expected))

            print("Note: UMAP visualizations need to be recomputed on the target.")
            print("  Run: dataset.compute_visualization(field_name='...')")
        else:
            print("\nNo embeddings to sync.")

    # --- Sync hashes ---
    if not skip_hashes:
        # Raise, don't swallow: a transient 500 here (e.g. the documented pool-exhaustion one)
        # degraded into "No hashes to sync." and then printed ✅ with zero hashes transferred —
        # the same false success as the missing hash read, one level up.
        resp = source.session.get(f"{source.base_url}/datasets/{source_id}/hashes/fields")
        _raise_for_status(resp)
        hash_fields = resp.json()

        if hash_fields:
            print(f"\n--- Syncing hashes ({len(hash_fields)} fields) ---")

            for hf in hash_fields:
                field_name = hf['field_name']
                field_count = hf['count']
                print(f"  Field: {field_name} ({field_count:,} hashes)")

                # Page the real hashes out of sample_hashes via the export endpoint.
                #
                # This used to read `item['metadata'][field_name]` off GET /samples — where a hash
                # has never lived: hashes are rows in the sample_hashes table and the Sample
                # response model has no hashes field. So hash_payload was empty on every batch and
                # this loop transferred ZERO hashes, after re-paging every sample once PER hash
                # field (3M/1000 x 2 fields = 6,000 requests, each running a full COUNT(*)).
                # Duplicate detection was then dead on the copy, under a "✅ Sync complete".
                after = 0
                hash_synced = 0
                unmapped = 0
                # The export endpoint bounds a page (_MAX_EXPORT_PAGE). --batch-size is a
                # user-supplied number, and a larger one used to 422 the whole hash phase AFTER
                # samples and embeddings had already been copied.
                hash_page = min(batch_size, 50_000)

                while True:
                    items = source.export_hashes(
                        source_id,
                        field_name,
                        after_id=after,
                        limit=hash_page,
                    ).get('items', [])
                    if not items:
                        break

                    hash_payload = []
                    for it in items:
                        target_sample_id = id_map.get(it['sample_id'])
                        if target_sample_id:
                            hash_payload.append(
                                {
                                    'sample_id': target_sample_id,
                                    'field_name': field_name,
                                    'hash_hex': it['hash_hex'],
                                },
                            )
                        else:
                            unmapped += 1

                    if hash_payload:
                        result = target.upload_hashes(target_id, hash_payload)
                        # Count what the SERVER says it wrote, not what we sent — upload_hashes
                        # silently drops sample_ids that don't belong to the target dataset.
                        hash_synced += result.get('written', len(hash_payload))

                    after = items[-1]['sample_id']  # keyset cursor
                    if len(items) < hash_page:
                        break

                note = f" ({unmapped:,} had no matching target sample)" if unmapped else ""
                print(f"    Synced {hash_synced:,} / {field_count:,} {field_name} hashes{note}")
                totals['hashes'].append((field_name, hash_synced, field_count))
        else:
            print("\nNo hashes to sync.")

    # A partial transfer must never report success. This tool is what backup.py points users to as
    # THE supported single-dataset transfer, so "✅" has to mean the copy is actually usable.
    shortfalls = [f"{name}: {got:,} of {want:,} hashes" for name, got, want in totals['hashes'] if got != want]
    shortfalls += [f"{name}: {got:,} of {want:,} vectors" for name, got, want in totals['embeddings'] if got != want]
    if synced != total:
        shortfalls.insert(0, f"samples: {synced:,} of {total:,}")
    if shortfalls:
        print(f"\n❌ Sync INCOMPLETE: '{dataset_name}' → {target_url} (dataset id {target_id})")
        for line in shortfalls:
            print(f"   - {line}")
        raise SystemExit(1)

    print(f"\n✅ Sync complete: '{dataset_name}' → {target_url} (dataset id {target_id})")
    return target_id


def main():
    parser = argparse.ArgumentParser(
        description="Sync a dataset (samples, metadata, embeddings, hashes) between two Freedom Ridge instances.",
    )
    parser.add_argument("--source", required=True, help="Source API base URL, e.g. http://host:8000/api")
    parser.add_argument("--target", required=True, help="Target API base URL")
    parser.add_argument("--dataset", required=True, help="Dataset name to sync")
    parser.add_argument("--source-user", default="admin")
    parser.add_argument("--source-pass", default="admin123")
    parser.add_argument("--target-user", default="admin")
    parser.add_argument("--target-pass", default="admin123")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--skip-embeddings", action="store_true")
    parser.add_argument("--skip-hashes", action="store_true")
    args = parser.parse_args()

    sync(
        source_url=args.source,
        target_url=args.target,
        dataset_name=args.dataset,
        source_user=args.source_user,
        source_pass=args.source_pass,
        target_user=args.target_user,
        target_pass=args.target_pass,
        batch_size=args.batch_size,
        skip_embeddings=args.skip_embeddings,
        skip_hashes=args.skip_hashes,
    )


if __name__ == "__main__":
    main()
