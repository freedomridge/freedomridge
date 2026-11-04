# Changelog

Freedom Ridge has one version for the server, the web app and the Python SDK (the `VERSION`
file), following [Semantic Versioning](https://semver.org). A release lists **upgrade notes** when
it needs more than the standard steps in [Upgrading](docs/UPGRADING.md).

## Unreleased

- Storage configurations can be limited to chosen datasets (**Settings → Storage
  Configuration → Datasets → Only these datasets**); other datasets are refused their
  buckets. Configurations still serve all datasets unless limited. Migration
  `0001_storage_config_dataset_scope.sql`.
- A storage configuration that is added, changed or deleted applies to every backend
  worker on its next request. Before, other workers could keep refusing a newly added
  bucket ("Bucket not authorized for this dataset", **Unavailable** grid tiles), or
  keep using old credentials, for up to 15 minutes.
- When several configurations cover a bucket, a dataset's own configuration
  (`storage_config_id`) now comes first, ahead of another configuration whose bucket
  name matches exactly.

## 1.0.0 (2026-11-02)

The first public release.
