"""Public demo sources and stable local layouts (reference download list: 2026-10-22).

Sizes are planning estimates; the downloader verifies each server's actual length.
No source is contacted by --list or by importing this module.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    url: str
    path: str
    extract_to: str | None = None
    checksum: str | None = None  # algorithm:hex, if a release publishes a fixed digest


GCS = 'https://storage.googleapis.com/openimages'
HF = 'https://huggingface.co/datasets/osv5m/osv5m/resolve/main'
KITTI = 'https://s3.eu-central-1.amazonaws.com/avg-kitti'
SOURCES = {
    'coco': [
        Source(
            'http://images.cocodataset.org/annotations/annotations_trainval2017.zip',
            'coco/annotations_trainval2017.zip',
            'coco',
            'sha256:113a836d90195ee1f884e704da6304dfaaecff1f023f49b6ca93c4aaae470268',
        ),
        # TensorFlow Datasets' published COCO release checksums:
        # https://github.com/tensorflow/datasets/blob/master/tensorflow_datasets/url_checksums/coco.txt
        Source(
            'http://images.cocodataset.org/zips/val2017.zip',
            'coco/val2017.zip',
            'coco',
            'sha256:4f7e2ccb2866ec5041993c9cf2a952bbed69647b115d0f74da7ce8f4bef82f05',
        ),
    ],
    'open_images': [
        *[
            Source(f'{GCS}/{version}/{name}', f'open_images_v7/annotations/{name}')
            for version, name in [
                ('v5', 'validation-annotations-bbox.csv'),
                ('v5', 'validation-annotations-human-imagelabels-boxable.csv'),
                ('v7', 'oidv7-val-annotations-human-imagelabels.csv'),
                ('v7', 'oidv7-val-annotations-point-labels.csv'),
                ('v5', 'validation-annotations-object-segmentation.csv'),
                ('v6', 'oidv6-validation-annotations-vrd.csv'),
                ('v7', 'oidv7-class-descriptions.csv'),
                ('v7', 'oidv7-class-descriptions-boxable.csv'),
                ('2018_04', 'bbox_labels_600_hierarchy.json'),
                ('2018_04/validation', 'validation-images-with-rotation.csv'),
            ]
        ],
        *[
            Source(
                f'{GCS}/v5/validation-masks/validation-masks-{digit}.zip',
                f'open_images_v7/masks/validation-masks-{digit}.zip',
                'open_images_v7/masks',
            )
            for digit in '0123456789abcdef'
        ],
        Source(
            'https://s3.amazonaws.com/open-images-dataset/tar/validation.tar.gz',
            'open_images_v7/validation.tar.gz',
            'open_images_v7',
        ),
        Source(
            'https://storage.googleapis.com/localized-narratives/annotations/'
            'open_images_validation_localized_narratives.jsonl',
            'open_images_v7/annotations/open_images_validation_localized_narratives.jsonl',
        ),
    ],
    'inaturalist': [
        Source(
            f'https://ml-inat-competition-datasets.s3.amazonaws.com/2021/{name}',
            f'inaturalist/{name}',
            'inaturalist',
            'md5:' + digest,
        )
        # https://github.com/visipedia/inat_comp/blob/master/2021/README.md#data
        for name, digest in (
            ('val.json.tar.gz', '4d761e0f6a86cc63e8f7afc91f6a8f0b'),
            ('val.tar.gz', 'f6f6e0e242e3d4c9569ba56400938afc'),
        )
    ],
    'osv5m': [
        Source(f'{HF}/test.csv', 'osv5m/test.csv'),
        Source(f'{HF}/README.md', 'osv5m/README.md'),
        *[
            Source(f'{HF}/images/test/{i:02}.zip', f'osv5m/images/test/{i:02}.zip', 'osv5m/images/test')
            for i in range(5)
        ],
    ],
    'eurosat': [
        Source(
            'https://huggingface.co/datasets/torchgeo/eurosat/resolve/main/EuroSATallBands.zip',
            'eurosat/EuroSATallBands.zip',
            'eurosat',
        ),
    ],
    'kitti': [
        Source(f'{KITTI}/{name}', f'kitti/{name}')
        for name in (
            'data_object_image_2.zip',
            'data_object_label_2.zip',
            'data_object_calib.zip',
            'data_object_velodyne.zip',
            'devkit_object.zip',
        )
    ],
}
MANUAL = {
    'bdd100k': 'Create a free account at https://bdd-data.berkeley.edu/ and accept the terms. '
    'Download images, labels and optional telemetry; see examples/bdd100k/README.md. No automated download.',
    'tusimple': 'Obtain archives from the official https://github.com/TuSimple/tusimple-benchmark links. '
    'Access and dataset redistribution permission are not established; see examples/tusimple/README.md. '
    'Local evaluation only; no automated download.',
}
CATALOG = {
    'coco': (
        '1.07 GB download; 2.6 GB disk',
        'Annotations CC BY 4.0; Flickr photo licenses; default excludes NoDerivs',
    ),
    'open_images': ('~14–16 GB', 'Annotations CC BY 4.0; photos normally CC BY 2.0'),
    'inaturalist': ('~8.94 GB', 'Per-photo licenses; default excludes NoDerivatives'),
    'osv5m': ('~9–11 GB', 'CC BY-SA 4.0'),
    'eurosat': ('~2 GB', 'MIT (EuroSAT release)'),
    'kitti': ('41.35 GB archives; ~20 GB training media + crops', 'CC BY-NC-SA 3.0; non-commercial'),
    'bdd100k': ('~7 GB + labels/telemetry', 'Non-commercial research; account required'),
    'tusimple': ('~27.84 GB', 'Local evaluation only; hosting rights unestablished'),
}
ORDER = (
    'coco',
    'inaturalist',
    'osv5m',
    'open_images',
    'kitti',
    'eurosat',
    'tusimple_lanes',
    'tusimple_velocity',
    'bdd100k',
)
DATA_DIRS = {
    'open_images': 'open_images_v7',
    'tusimple_lanes': 'tusimple',
    'tusimple_velocity': 'tusimple',
    'bdd100k': '.',
}


def selection(value, allowed):
    names = list(dict.fromkeys(name.strip() for name in value.split(',') if name.strip()))
    unknown = set(names) - set(allowed)
    if not names or unknown:
        raise ValueError(f'Choose comma-separated datasets from {", ".join(allowed)}; unknown: {sorted(unknown)}')
    return names
