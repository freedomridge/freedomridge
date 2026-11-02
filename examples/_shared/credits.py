"""Dataset-level demo credits shared by importers and the description backfill."""

KITTI_ATTRIBUTION = 'KITTI Vision Benchmark Suite (Geiger, Lenz, Stiller, Urtasun), CC BY-NC-SA 3.0'
OSV5M_ATTRIBUTION = 'Mapillary contributors; OSV-5M authors (Astruc et al., CVPR 2024); CC-BY-SA 4.0'

DATASET_CREDITS = {
    'coco-val2017': (
        'COCO val2017 — COCO Consortium, annotations CC BY 4.0. Photos © their Flickr creators; '
        'individual licenses (including non-commercial terms) and source links are in image metadata. '
        'https://cocodataset.org/#termsofuse'
    ),
    'open-images-v7-val': (
        'Open Images V7 — Google Open Images and Localized Narratives, annotations CC BY 4.0. '
        'Photos © their respective creators; per-image Author, License and source links are in metadata. '
        'https://storage.googleapis.com/openimages/web/index.html'
    ),
    'inaturalist-2021-val': (
        'iNaturalist 2021 — Visipedia and iNaturalist contributors. Photos © their rights holders; '
        'individual licenses (including non-commercial terms), attribution and license URLs are in image metadata. '
        'https://github.com/visipedia/inat_comp/tree/master/2021'
    ),
    'kitti': (
        f'{KITTI_ATTRIBUTION}. Non-commercial evaluation; crops share alike. https://www.cvlibs.net/datasets/kitti/'
    ),
    'osv5m-test': OSV5M_ATTRIBUTION,
}
