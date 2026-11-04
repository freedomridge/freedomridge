# Media storage: local files and S3

The browser never reads media files directly. It requests
`/api/samples/ID/media`, and the backend checks that the user may see the dataset
before it reads the file from local disk or S3. In production, nginx proxies that
route (and the admin-only legacy `/media/s3/` route) to the backend. nginx has no
direct file alias and does not cache media responses. Media requests use their own
nginx rate and connection zones, so a thumbnail grid cannot use up the API
allowance; see the limits table in [Deploy to AWS](DEPLOY_TO_AWS.md#10-public-demo-behavior-and-credits).

## Local files

- Sample filepaths are absolute. The backend serves only files below its own
  `FRIDGE_DATA_ROOT` and returns 403 for anything else.
- With `docker-compose.prod.yml`, set `DATA_VOLUME_PATH` and `FRIDGE_DATA_ROOT` in
  `.env` to the same absolute host directory. The backend and job-runner mount it
  read-only at that same path. The AWS guide uses `/data/datasets`.
- `HOST_DATA_ROOT` does not translate sample paths.
- Keep secrets, database files and backups outside the media root.

## S3

A sample whose filepath is `s3://BUCKET/KEY` is fetched by the backend. For each
request it chooses among the storage configurations that cover BUCKET (their bucket
name is BUCKET, or one of their bucket patterns matches it) and that the sample's
dataset may use, in this order:

1. the dataset's own storage configuration;
2. a configuration limited to chosen datasets, this one among them;
3. a configuration that serves all datasets.

Among equally specific configurations, an exact bucket name beats a pattern, then the
oldest wins. When no configuration covers BUCKET, the environment variables below
serve their own bucket, but only for a dataset that has no storage configuration of
its own while no default configuration exists.

The backend refuses any other bucket, so a sample path cannot make the server read
an arbitrary bucket with its credentials. The app only reads dataset buckets; it
never writes or deletes objects in them. Adding, changing, limiting or deleting a
configuration applies to every backend worker on its next request.

### Settings → Storage Configuration

Admins manage storage configurations in **Settings → Storage Configuration**
(**Add S3 Bucket**): name, bucket name, region, endpoint URL (for MinIO or another
S3-compatible store), additional bucket patterns such as `client-*`, access key ID
and secret, **Public bucket (no credentials required)** and **Set as default
storage**. The secret key is stored encrypted with `STORAGE_ENCRYPTION_KEY`; keep
that key with your backups, or restored configurations cannot be decrypted.
**Test Connection** lists up to five objects (`ListObjectsV2`) and reads the bucket
region when allowed. A dataset uses a particular configuration when it is created
or updated with its `storage_config_id`; otherwise the bucket-name match or the
default applies.

**Datasets** chooses which datasets may use a configuration. **All datasets** is the
default, and the only choice for the default configuration. **Only these datasets**
limits it to the ticked datasets: every other dataset is refused its buckets, even
ones its bucket patterns match, and with none ticked it serves no dataset. Through the
API this is `restricted` with `dataset_ids`. A limited configuration can only become a
dataset's own `storage_config_id` once it lists that dataset, and it can't drop a
dataset that uses it that way.

`docker-compose.prod.yml` passes only `STORAGE_TYPE`, `S3_BUCKET` and `S3_REGION`
from `.env` (below). The AWS deployment sets them to its media bucket, which the
backend reads with the instance role; configure any other bucket on this page. A
configuration marked **default** replaces that environment configuration for every
dataset, so the media bucket would no longer be authorized; on such a stack add other
buckets without marking them default.

**Public buckets.** Only a storage configuration with **Public bucket** set makes
anonymous (unsigned) requests. Every other configuration signs its requests: with
no keys entered, the backend uses boto3's default credential chain (environment,
`~/.aws`, then an instance role). With no credentials anywhere, requests fail.
On a public configuration, **Test Connection** currently uses that same default
chain instead of an anonymous request, so its result can differ from what media
serving does. A public bucket is readable by anyone on the Internet, whatever the
app's dataset permissions say.

### Environment variables

The backend also reads an environment configuration, the last fallback in the order
above. A backend you start yourself reads all of these variables; the production
Compose stack passes `STORAGE_TYPE`, `S3_BUCKET` and `S3_REGION`.

| Variable | Meaning |
|---|---|
| `STORAGE_TYPE` | `s3` enables the environment configuration; the default `local` disables it |
| `S3_BUCKET` | The one bucket this configuration serves |
| `S3_REGION` | Default `us-east-1` |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | Keys. If unset, the backend reads `aws_access_key_id`, `aws_secret_access_key`, `region`, `s3_bucket` and `s3_endpoint_url` from the `[fridge]` (or `[default]`) section of the INI file at `FRIDGE_CONFIG` (default `~/.aws/credentials`); otherwise boto3's default chain applies |
| `S3_ENDPOINT_URL` | MinIO or another S3-compatible endpoint |
| `S3_LOCAL_CACHE_PATH` | Download cache (default `$TMPDIR/freedomridge-s3-cache`; production `/cache/s3`) |
| `FRIDGE_CACHE_MAX_BYTES` | Byte budget for the media caches (default 10 GiB) |

Downloaded objects are cached as `S3_LOCAL_CACHE_PATH/DATASET_ID/BUCKET/KEY`. In
production the `worker` service prunes `/cache/s3`, `/cache/crop` and
`/cache/composite` hourly to `FRIDGE_CACHE_MAX_BYTES`. Model weights in
`/cache/huggingface` are never pruned. The cache is not part of a backup; it refills
as media is viewed.

## A minimal read-only IAM policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListForTestConnection",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET"
    },
    {
      "Sid": "ReadMedia",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::YOUR_BUCKET/*"
    }
  ]
}
```

Media serving needs only `s3:GetObject`; **Test Connection** needs `s3:ListBucket`
(`s3:GetBucketLocation` is optional). To limit access to a prefix, use
`arn:aws:s3:::YOUR_BUCKET/PREFIX/*` for `GetObject` and an `s3:prefix` condition on
`ListBucket`. Attach the policy to a dedicated IAM user whose keys you enter in
Settings, or, on EC2, to the instance role and leave the keys empty.

On the AWS stack the Terraform instance role grants no dataset-bucket access. It
can only upload and list dumps under the backup bucket's `database/` prefix, and it
cannot download them. Every container on the instance shares that role, so adding
this policy to it gives all of them read access to the bucket.
