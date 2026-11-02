#!/usr/bin/env python3
"""Verify a local demo smoke dataset through its API; emits a compact JSON evidence report."""

import argparse
import io
import json
import math
import sys
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import connect, get_client  # noqa: E402

from examples._shared.importer import credentials, login  # noqa: E402


def stored(path):
    """A media path as importers record it: absolute below the data root, or an S3 URI (--media-uri)."""
    return path.startswith('s3://') or Path(path).is_absolute()


def verify_objects(client, dataset_name, expected_count, kind):
    dataset = client.get_dataset(dataset_name)
    assert dataset is not None, dataset_name
    dataset_id = dataset['id']
    first_page = client.query_samples(dataset_id, limit=1)
    parents, objects = {}, []
    checked_media: Counter[str] = Counter()
    config = client.get_dataset_by_id(dataset_id).get('segmentation_config') or {}

    def fetch(sample_id, field='filepath', thumbnail=False):
        response = client.session.get(
            f'{client.base_url}/samples/{sample_id}/media',
            params={'field': field, **({'w': 256} if thumbnail else {})},
            timeout=30,
        )
        response.raise_for_status()
        assert response.status_code == 200 and not response.history
        assert response.headers['Content-Type'].startswith('image/')
        return response

    counts: Counter[str] = Counter()
    schema = {field['name']: field for field in client.get_schema(dataset_id)}
    scalar_fields = (
        ('license_name', 'license_url', 'coco_url', 'flickr_url')
        if kind == 'coco'
        else ('OriginalURL', 'Author', 'License', 'Title', 'Rotation')
    )
    list_fields = (
        ('detections', 'polygons', 'keypoints', 'skeletons', 'captions')
        if kind == 'coco'
        else (
            'detections',
            'positive_labels',
            'points',
            'mask_instances',
            'relationships',
            'captions',
        )
    )
    array_fields = (
        ('class_names', 'supercategory_names')
        if kind == 'coco'
        else (
            'class_names',
            'parent_classes',
            'negative_classes',
            'point_states',
            'relationship_predicates',
            'mask_classes',
        )
    )
    representatives: dict = {}
    arrays: dict = {}
    caption = None
    media: dict = {}
    after_id = 0
    while True:
        page = client.query_samples(dataset_id, limit=25, after_id=after_id)
        if not page['items']:
            break
        for sample in page['items']:
            metadata = sample['metadata']
            assert stored(sample['filepath']), sample['filepath']
            if sample['media_type'] == 'object':
                objects.append(sample)
                fetch(sample['id'])
                checked_media['crops'] += 1
                continue
            assert sample['media_type'] == 'image'
            parents[sample['id']] = sample
            assert 'negative_labels' not in metadata
            assert all(isinstance(x, str) for x in metadata.get('narratives', []))
            assert all(isinstance(x, str) for x in metadata['captions'])
            assert not metadata.get('caption_annotations')
            assert all('label' not in x for x in metadata.get('verified_absent', []))
            for field in ('negative_classes', 'negative_parent_classes'):
                assert all(isinstance(x, str) for x in sample.get('array_fields', {}).get(field, sample.get(field, [])))
            for instance in metadata.get('mask_instances', []):
                assert any(d['id'] == instance['detection_id'] for d in metadata['detections'])
            fetch(sample['id'])
            checked_media['images'] += 1

            for field in scalar_fields:
                assert field in metadata and field in schema, field
                if metadata[field] not in (None, ''):
                    representatives.setdefault(field, metadata[field])
            for field in list_fields:
                counts[field] += len(metadata[field])
            for field in array_fields:
                assert field in schema, field
                values = sample.get('array_fields', {}).get(field, sample.get(field, []))
                if values:
                    arrays.setdefault(field, values[0])
            for field, value in metadata.items():
                if isinstance(value, dict) and 'maskPath' in value:
                    assert stored(value['maskPath']), value['maskPath']
                    assert config['mask_targets'][field] == {**config['mask_targets'][field], **value['mask_targets']}
                    assert all(
                        pixel in config['color_scheme']['fields'][field]['idColors'] for pixel in value['mask_targets']
                    )
                    response = fetch(sample['id'], f'{field}.maskPath')
                    with Image.open(io.BytesIO(response.content)) as mask:
                        pixels = set(mask.getdata()) - {0}
                        assert all(str(pixel) in config['mask_targets'][field] for pixel in pixels)
                    checked_media['masks'] += 1
                    counts[field] += 1
                    media.setdefault('mask', (sample['id'], f'{field}.maskPath'))
                    if metadata.get('rotation_applied'):
                        media.setdefault('rotated_mask', (sample['id'], f'{field}.maskPath'))
            media.setdefault('image', (sample['id'], 'filepath'))
            if metadata.get('rotation_applied'):
                media.setdefault('rotated_image', (sample['id'], 'filepath'))
            if metadata['captions']:
                caption = metadata['captions'][0][:40]
        after_id = page['items'][-1]['id']
    assert len(parents) == expected_count, len(parents)
    assert len(objects) == sum(len(s['metadata']['detections']) for s in parents.values())
    links = set()
    flag, source_flag = ('is_crowd', 'iscrowd') if kind == 'coco' else ('is_group_of', 'IsGroupOf')
    for obj in objects:
        metadata = obj['metadata']
        parent = parents[metadata['parent_id']]
        detection = next(d for d in parent['metadata']['detections'] if d['id'] == metadata['detection_id'])
        assert metadata['crop_bbox'] == detection['bbox']
        assert metadata['label'] == detection['label']
        assert obj['tags'] == [metadata['label']]
        assert metadata[flag] is bool(detection[source_flag])
        links.add((parent['id'], metadata['detection_id']))
    assert len(links) == len(objects)
    filters = {}
    for field, value in representatives.items():
        result = client.query_samples(dataset_id, limit=1, filters={'field': field, 'operator': '=', 'value': value})
        assert result['total'] > 0, (field, value)
        filters[field] = result['total']
    for field, value in arrays.items():
        result = client.query_samples(
            dataset_id,
            limit=1,
            filters={'field': field, 'operator': 'contains', 'value': value},
        )
        assert result['total'] > 0, (field, value)
        filters[field] = result['total']
    assert caption, 'No searchable captions'
    found = client.query_samples(
        dataset_id,
        limit=1,
        filters={'field': 'caption_text', 'operator': 'contains', 'value': caption},
    )
    assert found['total'] > 0
    # Negative control proves the search is applied, rather than merely accepting a field.
    absent = client.query_samples(
        dataset_id,
        limit=1,
        filters={'field': 'caption_text', 'operator': 'contains', 'value': 'cx_smoke_absent_8fbdcd28'},
    )
    assert absent['total'] == 0
    inventory = client.get_embedding_field_inventory(dataset_id)
    inventory_counts = {field['field_name']: field['count'] for field in inventory}
    assert inventory_counts == {'clip': expected_count, 'clip_obj': len(objects)}, inventory
    coordinates = {}
    for field, rows in (('clip', list(parents.values())), ('clip_obj', objects)):
        points = client.lookup_embedding_coordinates(dataset_id, [r['id'] for r in rows], field=field)['points']
        assert {p['sample_id'] for p in points} == {r['id'] for r in rows}
        assert all(p['x'] is not None and p['y'] is not None for p in points)
        coordinates[field] = len(points)
    media_status = {}
    for label, (sample_id, field) in media.items():
        response = fetch(sample_id, field)
        media_status[label] = {
            'status': response.status_code,
            'bytes': len(response.content),
            'redirects': len(response.history),
        }
        if label in ('image', 'rotated_image'):
            thumbnail = fetch(sample_id, field, thumbnail=True)
            media_status[f'{label}_thumbnail'] = {
                'status': thumbnail.status_code,
                'bytes': len(thumbnail.content),
                'redirects': len(thumbnail.history),
            }
    report = {
        'dataset': dataset_name,
        'sample_count': first_page['total'],
        'image_count': len(parents),
        'object_count': len(objects),
        'parent_links_checked': len(links),
        'all_media_200': dict(checked_media),
        'umap_coordinates': coordinates,
        'local_asset_paths': True,
        'flagged_objects': sum(bool(o['metadata'][flag]) for o in objects),
        'label_counts': dict(counts),
        'embedding_inventory': inventory,
        'filter_matches': filters,
        'caption_search_matches': found['total'],
        'caption_negative_control': absent['total'],
        'media': media_status,
    }
    print(json.dumps(report, indent=2))
    return report


def verify_gps(client, args):
    from examples.inaturalist.ingest import FIELD_TYPES
    from examples.osv5m.ingest import CATEGORY_COLUMNS, NUMBER_COLUMNS, column_type, field_name

    dataset = next(d for d in client.get_datasets() if d["name"] == args.dataset_name)
    dataset_id = dataset["id"]
    result = client.query_samples(dataset_id, limit=args.expected_count, offset=0)
    assert result["total"] == len(result["items"]) == args.expected_count
    samples = result["items"]
    schema = {f["name"]: f for f in client.get_schema(dataset_id)}
    if args.kind == "inaturalist":
        expected_types = FIELD_TYPES
    else:
        expected_types = {field_name(k): column_type(k) for k in CATEGORY_COLUMNS | NUMBER_COLUMNS | {"captured_at"}}
        expected_types.update(dict.fromkeys(("license", "license_url", "attribution"), "string"))
    filter_counts = {}
    for field, kind in expected_types.items():
        assert field in schema, field
        assert schema[field].get("declared_type") == kind, (field, schema[field])
        assert all(field in sample["metadata"] for sample in samples), field
        value = next((s["metadata"][field] for s in samples if s["metadata"][field] is not None), None)
        query_field = field
        operator = "=" if value is not None else "is_null"
        if field == "species":
            # Classification objects support contains queries on their label text.
            assert value is not None
            value, operator = value["label"], "contains"
        query = {"field": query_field, "operator": operator, "value": value}
        matched = client.query_samples(dataset_id, limit=args.expected_count, offset=0, filters=query)
        assert matched["total"] > 0, field
        for sample in matched["items"]:
            actual = sample["metadata"][field]
            if field == "species":
                assert value in actual["label"], (field, value, actual)
            else:
                assert actual == value, (field, value, actual)
        filter_counts[query_field] = matched["total"]
    response = client.session.post(f"{client.base_url}/datasets/{dataset_id}/map_points", json={"zoom": 12})
    response.raise_for_status()
    geo = response.json()
    assert geo["mode"] == "points" and not geo["capped"]
    assert len(geo["items"]) == args.expected_count
    points = {p["id"]: (p["lon"], p["lat"]) for p in geo["items"]}
    for sample in samples:
        assert points[sample["id"]] == tuple(sample["location"]["coordinates"])
        assert points[sample["id"]] == (sample["metadata"]["longitude"], sample["metadata"]["latitude"])
    inventory = client.get_embedding_field_inventory(dataset_id)
    assert {f["field_name"]: f["count"] for f in inventory}["clip"] == args.expected_count
    media = client.session.get(f"{client.base_url}/samples/{samples[0]['id']}/media")
    media.raise_for_status()
    assert media.headers["content-type"].startswith("image/") and len(media.content) > 0
    evidence = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "api_url": args.api_url,
        "dataset": args.dataset_name,
        "sample_count": result["total"],
        "map_points": len(points),
        "map_point_example": geo["items"][0],
        "map_capped": geo["capped"],
        "embedding_inventory": inventory,
        "media_status": media.status_code,
        "declared_types": expected_types,
        "filter_match_counts": filter_counts,
        "datetime_example": samples[0]["metadata"]["date" if args.kind == "inaturalist" else "captured_at"],
    }
    if args.kind == "osv5m":
        evidence["countries"] = len({s["metadata"]["country"] for s in samples})
    if args.expect_reduced:
        reduced = client.query_embeddings(dataset_id, field="clip")
        assert reduced["total"] == len(reduced["points"]) == args.expected_count
        evidence["reduced_points"] = len(reduced["points"])
    return evidence


def predicate(field, operator, value):
    return {"field": field, "operator": operator, "value": value}


def verify_lanes(client, module, benchmark, data_root, name, source_root, expected_count):
    from examples.tusimple.mapping import ATTRIBUTION, DATA_LICENSE

    dataset = next(d for d in client.get_datasets() if d["name"] == name)
    dataset_id = dataset["id"]
    response = client.query_samples(dataset_id, limit=1000)
    samples = response["items"]
    assert response["total"] == len(samples) == expected_count, (name, response["total"])
    assert len({sample["filepath"] for sample in samples}) == expected_count
    by_path = {sample["filepath"]: sample for sample in samples}
    label_field = "lanes" if benchmark == "lanes" else "vehicles"
    # Compare API labels/metadata to the selected archive annotations; fixture
    # tests independently check the coordinate and attribute transformations.
    with closing(module.iter_frames(source_root)) as frames:
        for frame in islice(frames, expected_count):
            sample = by_path[str(frame.destination(data_root, benchmark))]
            metadata = sample["metadata"]
            expected = module.map_labels(frame, metadata["width"], metadata["height"])
            assert all(metadata[key] == value for key, value in expected.items())
            assert metadata["clip_id"] == frame.clip_id and metadata["split"] == frame.split
            assert metadata["source_file"] == frame.member
            assert metadata["license"] == DATA_LICENSE and metadata["attribution"] == ATTRIBUTION
            assert sample["tags"] == [frame.split]

    labels = [label for sample in samples for label in sample["metadata"][label_field]]
    assert len({label["id"] for label in labels}) == len(labels)
    schema = {field["name"]: field for field in client.get_schema(dataset_id, lightning_mode=False)}
    count_field = "lane_count" if benchmark == "lanes" else "vehicle_count"
    assert schema[count_field]["type"] == "number"
    assert schema[label_field]["type"] == "list"
    assert schema["split"]["type"] == "enum"
    vectors = client.get_vectors(dataset_id, limit=1000)
    assert vectors["total"] == len(vectors["items"]) == expected_count
    assert {v["sample_id"] for v in vectors["items"]} == {s["id"] for s in samples}
    assert all(len(v["vector"]) == 512 and all(map(math.isfinite, v["vector"])) for v in vectors["items"])
    coverage = client.get_coverage(dataset_id)
    assert {"field": "clip", "present": expected_count, "missing": 0} in coverage["embeddings"]

    filters_checked = {}

    def check_filter(description, query, matches):
        expected = {sample["id"] for sample in samples if matches(sample["metadata"])}
        result = client.query_samples(dataset_id, filters=query, limit=1000)
        actual = {sample["id"] for sample in result["items"]}
        assert actual == expected and result["total"] == len(expected), description
        filters_checked[description] = len(expected)
        return expected

    splits = Counter(s["metadata"]["split"] for s in samples)
    media_checked = 0
    for split in splits:
        check_filter(f"split={split}", predicate("split", "=", split), lambda m: m["split"] == split)
        sample = next(s for s in samples if s["metadata"]["split"] == split)
        media = client.session.get(f"{client.base_url}/samples/{sample['id']}/media", timeout=30)
        media.raise_for_status()
        assert media.content == Path(sample["filepath"]).read_bytes()
        media_checked += 1
    check_filter(f"{count_field}>=4", predicate(count_field, ">=", 4), lambda m: m[count_field] >= 4)

    report = {
        "samples": expected_count,
        "splits": dict(splits),
        "label_field": label_field,
        "labels": len(labels),
        "clip_vectors": expected_count,
        "vector_dimensions": 512,
        "media_checks": media_checked,
        "schema_fields": sorted(schema),
        "filters": filters_checked,
    }
    if benchmark == "lanes":
        assert all(label["label"] == "lane" and len(label["paths"]) == 1 for label in labels)
        assert all(
            label["num_points"] == label["attributes"]["num_points"] == len(label["paths"][0]) for label in labels
        )
        assert all(not label["closed"] and not label["filled"] for label in labels)
        for points in (1, 48):
            check_filter(
                f"lane num_points={points}",
                predicate("lanes", "match_elements", predicate("num_points", "=", points)),
                lambda m: any(label["num_points"] == points for label in m["lanes"]),
            )
        report["polyline_paths"] = sum(len(label["paths"]) for label in labels)
        report["attributes"] = ["lane_index", "segment_index", "num_points"]
        return report

    motion = [d for d in labels if "speed" in d]
    assert motion and len(motion) < len(labels)
    for d in motion:
        attrs = d["attributes"]
        assert not {"velocity", "position"} & (attrs.keys() | d.keys())
        assert d["speed"] == attrs["speed"] == math.hypot(d["velocity_forward"], d["velocity_right"])
        assert d["distance"] == attrs["distance"] == math.hypot(d["position_forward"], d["position_right"])
        assert all(
            d[key] == attrs[key] for key in ("velocity_forward", "velocity_right", "position_forward", "position_right")
        )
    for sample in samples:
        m = sample["metadata"]
        assert m["has_motion"] == (m["split"] != "supp")
        if m["split"] == "supp":
            for d in m["vehicles"]:
                assert not {
                    "speed",
                    "distance",
                    "position",
                    "velocity",
                    "velocity_forward",
                    "velocity_right",
                    "position_forward",
                    "position_right",
                } & (d["attributes"].keys() | d.keys())

    def vehicle_filter(inner):
        return predicate("vehicles", "match_elements", inner)

    for component in ("velocity_forward", "velocity_right", "position_forward", "position_right"):
        check_filter(
            f"vehicle {component}>0",
            vehicle_filter(predicate(component, ">", 0)),
            lambda m: any(d.get(component, 0) > 0 for d in m["vehicles"]),
        )
    check_filter(
        "vehicle speed>1",
        vehicle_filter(predicate("speed", ">", 1)),
        lambda m: any(d.get("speed", -1) > 1 for d in m["vehicles"]),
    )
    check_filter(
        "vehicle distance<30",
        vehicle_filter(predicate("distance", "<", 30)),
        lambda m: any(d.get("distance", math.inf) < 30 for d in m["vehicles"]),
    )
    check_filter("has_motion=true", predicate("has_motion", "=", True), lambda m: m["has_motion"])

    # Choose a real-data threshold pair that distinguishes same-vehicle AND
    # from two independent EXISTS checks that could match different vehicles.
    for distance in (10, 20, 30, 40, 50, 60, 80):
        for speed in (0.1, 0.5, 1, 2, 3, 5, 10, 15):

            def near(d):
                return d.get("distance", math.inf) < distance

            def fast(d):
                return d.get("speed", -1) > speed

            together = {s["id"] for s in samples if any(near(d) and fast(d) for d in s["metadata"]["vehicles"])}
            separate = {
                s["id"]
                for s in samples
                if any(near(d) for d in s["metadata"]["vehicles"]) and any(fast(d) for d in s["metadata"]["vehicles"])
            }
            if together and separate - together:
                near_filter, fast_filter = predicate("distance", "<", distance), predicate("speed", ">", speed)
                check_filter(
                    f"same vehicle: distance<{distance} AND speed>{speed}",
                    vehicle_filter({"logic": "AND", "filters": [near_filter, fast_filter]}),
                    lambda m: any(near(d) and fast(d) for d in m["vehicles"]),
                )
                check_filter(
                    "independent vehicle predicates",
                    {"logic": "AND", "filters": [vehicle_filter(near_filter), vehicle_filter(fast_filter)]},
                    lambda m: any(near(d) for d in m["vehicles"]) and any(fast(d) for d in m["vehicles"]),
                )
                report["cross_vehicle_false_positive_ids_excluded"] = sorted(separate - together)
                break
        else:
            continue
        break
    else:
        raise AssertionError("No distinguishing same-vehicle filter found")
    report.update(
        motion_detections=len(motion),
        box_only_detections=len(labels) - len(motion),
        degenerate_boxes=sum(d["attributes"].get("degenerate_bbox", False) for d in labels),
        filterable_detection_keys=[
            "velocity_forward",
            "velocity_right",
            "position_forward",
            "position_right",
            "speed",
            "distance",
        ],
    )
    return report


def verify_kitti(client, args):
    """Check both grains, cuboid links/projections, media, GPS and embedding coverage."""
    from fridge import Calibration

    dataset = client.get_dataset(args.dataset_name)
    assert dataset is not None
    dataset_id = dataset['id']
    parents, objects = {}, []
    checked_media = 0
    after_id = 0
    while True:
        page = client.query_samples(dataset_id, limit=100, after_id=after_id)
        if not page['items']:
            break
        for sample in page['items']:
            assert stored(sample['filepath']), sample['filepath']
            if sample['media_type'] == 'object':
                objects.append(sample)
            else:
                assert sample['media_type'] == 'image'
                parents[sample['id']] = sample
            response = client.session.get(f"{client.base_url}/samples/{sample['id']}/media", timeout=30)
            response.raise_for_status()
            with Image.open(io.BytesIO(response.content)) as image:
                image.verify()
            checked_media += 1
        after_id = page['items'][-1]['id']
    assert len(parents) == args.expected_count
    expected_links, outside = {}, 0
    gps_count, lidar_count, cuboid_count, detection_count = 0, 0, 0, 0
    for parent_id, sample in parents.items():
        metadata = sample['metadata']
        assert metadata['license'] == 'CC BY-NC-SA 3.0' and 'KITTI' in metadata['attribution']
        raw = metadata['calibration']
        calibration = Calibration(raw['P'], raw['image_size'], raw['transforms'], raw.get('camera'))
        width, height = raw['image_size']
        cuboids = metadata.get('ground_truth_3d', [])
        detections = metadata.get('ground_truth', [])
        cuboid_count += len(cuboids)
        detection_count += len(detections)
        if metadata['split'] == 'training':
            assert metadata['num_cuboids'] == len(cuboids)
            assert len(detections) == len(cuboids) + metadata['num_dontcare']
        for box in cuboids:
            assert box['_type'] == 'cuboid' and box['label'] != 'DontCare'
            assert any(d['id'] == box['id'] and d['bbox'] == box['attributes']['bbox_2d'] for d in detections)
            _, extent = calibration.project(box)
            if extent is None:
                outside += 1
            else:
                expected_links[parent_id, box['id']] = (
                    box,
                    [value / scale for value, scale in zip(extent, (width, height, width, height))],
                )
        if metadata.get('gps_available'):
            gps_count += 1
            assert sample['location'] and 0 <= sample['location']['heading'] < 360
            assert datetime.fromisoformat(metadata['captured_at'].replace('Z', '+00:00')).tzinfo is not None
            assert metadata['oxts_timestamp_raw'].endswith(' (Europe/Berlin local)')
        if 'lidar' in metadata:
            lidar_count += 1
            assert metadata['lidar']['frame'] == 'lidar' and metadata['lidar']['num_points'] > 0
            assert stored(metadata['lidar']['path']), metadata['lidar']['path']
    actual_links = set()
    for sample in objects:
        metadata = sample['metadata']
        key = (metadata['parent_id'], metadata['cuboid_id'])
        assert key not in actual_links
        actual_links.add(key)
        box, bbox = expected_links[key]
        parent = parents[key[0]]
        assert metadata['cuboid_field'] == 'ground_truth_3d'
        assert metadata['parent_filepath'] == parent['filepath']
        assert metadata['label'] == box['label']
        assert all(abs(a - b) < 1e-10 for a, b in zip(metadata['crop_bbox'], bbox))
        assert sample['location'] == parent['location']
        for field in ('license', 'attribution', 'captured_at', 'altitude_m', 'speed_kmh'):
            assert metadata.get(field) == parent['metadata'].get(field)
    assert actual_links == expected_links.keys()
    schema = {f['name']: f for f in client.get_schema(dataset_id, lightning_mode=False)}
    if cuboid_count:
        assert schema['ground_truth_3d']['type'] in ('list', 'cuboids')
        stats = client.session.get(f'{client.base_url}/datasets/{dataset_id}/field-stats', timeout=30)
        stats.raise_for_status()
        fields = {f['name']: f for f in stats.json()}
        assert fields['ground_truth_3d']['type'] == 'cuboids'
        assert fields['occluded']['type'] == 'enum'
    if gps_count:
        assert schema['captured_at'].get('declared_type', schema['captured_at']['type']) == 'datetime'
        assert schema['oxts_timestamp_raw'].get('declared_type', schema['oxts_timestamp_raw']['type']) == 'string'
        response = client.session.post(
            f'{client.base_url}/datasets/{dataset_id}/map_points',
            json={'zoom': 12, 'filters': predicate('media_type', '=', 'image')},
            timeout=30,
        )
        response.raise_for_status()
        geo = response.json()
        assert geo['mode'] == 'points' and not geo['capped']
        points = {p['id']: (p['lon'], p['lat']) for p in geo['items']}
        assert points == {s['id']: tuple(s['location']['coordinates']) for s in parents.values() if s['location']}
    if args.expect_gps:
        assert gps_count == len(parents)
    inventory = client.get_embedding_field_inventory(dataset_id)
    embedded = {f['field_name']: f['count'] for f in inventory}
    reduced = {}
    if not args.skip_embeddings:
        for field, expected in ((args.field_name, len(parents)), ('clip_obj', len(objects))):
            assert embedded.get(field, 0) == expected
            if args.expect_reduced and expected:
                coords = client.query_embeddings(dataset_id, field=field)
                assert coords['total'] == len(coords['points']) == expected
                reduced[field] = expected
    return dict(
        dataset=args.dataset_name,
        images=len(parents),
        objects=len(objects),
        cuboids=cuboid_count,
        detections=detection_count,
        objects_without_extent=outside,
        gps_images=gps_count,
        lidar_images=lidar_count,
        media_checks=checked_media,
        embedding_inventory=inventory,
        reduced_points=reduced,
    )


def verify_basic(client, args):
    dataset = client.get_dataset(args.dataset_name)
    assert dataset is not None
    page = client.query_samples(dataset['id'], limit=args.expected_count)
    assert page['total'] == len(page['items']) == args.expected_count
    for sample in page['items']:
        response = client.session.get(f"{client.base_url}/samples/{sample['id']}/media", timeout=30)
        response.raise_for_status()
        assert response.headers['content-type'].startswith('image/') and response.content
    inventory = client.get_embedding_field_inventory(dataset['id'])
    assert {f['field_name']: f['count'] for f in inventory}['clip'] == args.expected_count
    if args.expect_reduced:
        points = client.query_embeddings(dataset['id'], field='clip')
        assert points['total'] == len(points['points']) == args.expected_count
    return {'dataset': args.dataset_name, 'samples': args.expected_count, 'embedding_inventory': inventory}


def build_parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        '--kind',
        required=True,
        choices=(
            'coco',
            'open_images',
            'inaturalist',
            'osv5m',
            'tusimple_lanes',
            'tusimple_velocity',
            'eurosat',
            'bdd100k',
            'kitti',
        ),
    )
    result.add_argument('--dataset-name', required=True)
    result.add_argument('--api-url', required=True)
    result.add_argument('--expected-count', '--count', type=int, default=100)
    result.add_argument('--expect-reduced', action='store_true')
    result.add_argument('--expect-gps', action='store_true', help='KITTI: require GPS on every image')
    result.add_argument('--skip-embeddings', action='store_true', help='KITTI: verify a metadata-only import')
    result.add_argument('--field-name', default='clip', help='KITTI image embedding field')
    result.add_argument('--data-root', type=Path)
    result.add_argument('--source-root', type=Path)
    credentials(result)
    return result


def main():
    args = build_parser().parse_args()
    if not __debug__:
        raise SystemExit('Verification requires assertions; run Python without -O or PYTHONOPTIMIZE')
    connect(args.api_url)
    client = get_client()
    login(client, args)
    if args.kind in ('coco', 'open_images'):
        verify_objects(client, args.dataset_name, args.expected_count, args.kind)
        return
    if args.kind in ('inaturalist', 'osv5m'):
        report = verify_gps(client, args)
    elif args.kind == 'kitti':
        report = verify_kitti(client, args)
    elif args.kind.startswith('tusimple_'):
        from examples.tusimple import ingest_lanes, ingest_velocity

        benchmark = args.kind.split('_')[1]
        module = ingest_lanes if benchmark == 'lanes' else ingest_velocity
        if not args.data_root or not args.source_root:
            raise ValueError('TuSimple verification needs --data-root and --source-root')
        report = verify_lanes(
            client,
            module,
            benchmark,
            args.data_root,
            args.dataset_name,
            args.source_root,
            args.expected_count,
        )
    else:
        report = verify_basic(client, args)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
