# Third-party notices

Freedom Ridge's own code is [MIT licensed](LICENSE). Dependencies, datasets, and
downloaded model weights retain their own licenses. This summary does not replace
the licenses and notices included with those components.

## Mapbox GL JS

The map panel uses `mapbox-gl` 3.x, a proprietary, **non-OSS** dependency governed
by the [Mapbox Web SDK terms](https://www.mapbox.com/legal/tos/) and its
[license](https://github.com/mapbox/mapbox-gl-js/blob/v3.16.0/LICENSE.txt).
It is not relicensed under MIT. Built frontend bundles and Docker images include
the SDK and must retain its notices and comply with those terms.

Create a free Mapbox account and public access token and set
`NEXT_PUBLIC_MAPBOX_TOKEN` to enable the map panel. Other features work without
the token. Map usage is subject to Mapbox's usage limits, pricing, attribution
requirements, and terms; a free token does not mean unlimited free service.
See [Mapbox token documentation](https://docs.mapbox.com/help/getting-started/access-tokens/).
Restrict public tokens to your deployed URLs; never use a secret token here.

## Database and frontend distributions

- PostgreSQL and pgvector use the PostgreSQL license. PostGIS is GPL-2.0-or-later
  and runs in the separate database server. Redistributing a built database image
  also redistributes these components and requires preserving their licenses,
  notices, and applicable source availability obligations.
- Optional `@img/sharp-libvips-*` native binaries used by Next.js image processing
  include LGPL-3.0 components. Preserve their notices and applicable LGPL
  obligations when redistributing builds that include them.
- `lightningcss` and `axe-core` use MPL-2.0; `caniuse-lite` data uses CC-BY-4.0.
  Keep the packages' license and attribution files with redistributed components.
- Inter, fetched by `next/font/google` and self-hosted in frontend builds, uses
  the SIL Open Font License 1.1.

## Model weights and datasets

Model weights are downloaded separately, not included in this repository.
OpenAI CLIP and LAION CLIP use MIT; DINOv2 uses Apache-2.0. DINOv3 weights require
acceptance of [Meta's custom DINOv3 license](https://github.com/facebookresearch/dinov3/blob/main/LICENSE.md)
and access conditions; the project's MIT license does not grant rights to them.

Example importers do not redistribute datasets. EuroSAT uses MIT; Open Images
annotations use CC-BY-4.0 and its images carry individual licenses and attribution
(commonly CC-BY-2.0). BDD100K is for non-commercial research and requires
registration. COCO (annotations CC-BY-4.0, photos under individual Flickr
licenses), iNaturalist (per-photo licenses), KITTI (CC-BY-NC-SA-3.0,
non-commercial), OSV-5M (CC-BY-SA-4.0) and TuSimple (hosting rights not
established; local evaluation only) carry their own terms. KITTI's website asks
you to register and accept its terms before downloading; do that before running
the KITTI download. Review the source dataset terms before downloading, using, or
displaying data; see the [per-dataset licenses](examples/README.md#choose-a-dataset).

The [Contributor Covenant 2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/)
in `CODE_OF_CONDUCT.md` is adapted under CC-BY-4.0, with its attribution retained.

## Trademarks

FiftyOne and Voxel51 are trademarks of Voxel51, Inc. Freedom Ridge is an independent project,
not affiliated with or endorsed by Voxel51; FiftyOne is named in this repository only to compare
or credit. Other product names belong to their owners.
