"""Tiny georeferenced tile: preserve RGB rendering, location and source metadata."""

import numpy as np
import pytest
import rasterio
from PIL import Image
from rasterio.transform import from_origin

from examples.eurosat.ingest import build_parser, iter_samples, render_rgb


def test_geotiff_mapping_and_render(tmp_path):
    directory = tmp_path / 'tif/Forest'
    directory.mkdir(parents=True)
    path = directory / 'Forest_1.tif'
    with rasterio.open(
        path,
        'w',
        driver='GTiff',
        width=4,
        height=4,
        count=13,
        dtype='uint16',
        crs='EPSG:4326',
        transform=from_origin(8, 49, 0.01, 0.01),
    ) as source:
        source.write(np.full((13, 4, 4), 1000, dtype='uint16'))
    args = build_parser().parse_args(['--data-root', str(tmp_path)])
    sample = next(iter_samples(args))
    assert sample.location == pytest.approx((8.02, 48.98))
    assert sample.metadata == {'label': 'Forest', 'source_tif': str(path)}
    assert sample.filepath == str(tmp_path / 'rgb/Forest/Forest_1.jpg')
    with rasterio.open(path) as source:
        render_rgb(source, tmp_path / 'render.jpg')
    with Image.open(tmp_path / 'render.jpg') as image:
        assert image.size == (4, 4) and image.mode == 'RGB'
