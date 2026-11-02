"""Bounded-memory, offline helpers shared by the local demo importers."""

import argparse
import colorsys
import copy
import getpass
import itertools
import json
import math
import mimetypes
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def positive(value):
    number = nonnegative(value)
    if not number:
        raise argparse.ArgumentTypeError("must be > 0")
    return number


def media_uri(value):
    """s3://BUCKET[/PREFIX], checked while parsing, before any dataset is created."""
    if not value.startswith('s3://') or not value.removeprefix('s3://').split('/', 1)[0]:
        raise argparse.ArgumentTypeError('expected s3://BUCKET or s3://BUCKET/PREFIX')
    return value.rstrip('/')


def parser(
    description,
    dataset_name,
    batch_size=16,
    require_data_root=True,
    limit_help=None,
    work_dir=False,
    media_uri_option=True,
):
    ap = argparse.ArgumentParser(description=description)
    if require_data_root:
        ap.add_argument(
            "--data-root",
            required=True,
            type=Path,
            help="Must be inside backend FRIDGE_DATA_ROOT, unless --media-uri is set",
        )
        ap.add_argument("--dataset-name", default=dataset_name)
        ap.add_argument("--on-duplicate", choices=("skip", "update", "error"), default="skip")
    if require_data_root and media_uri_option:
        ap.add_argument(
            "--media-uri",
            type=media_uri,
            help="s3://BUCKET/PREFIX: upload the media there and record s3:// paths (default: local paths)",
        )
    ap.add_argument("--limit", type=nonnegative, default=100, help=limit_help or "First N images in ID order; 0 = all")
    ap.add_argument("--field-name", default="clip")
    ap.add_argument("--model", default="openai/clip-vit-base-patch32")
    ap.add_argument("--device", default=None, help="Default: CUDA if available, otherwise CPU")
    ap.add_argument("--batch-size", type=positive, default=batch_size)
    ap.add_argument("--api-url", default=os.getenv("FRIDGE_API_URL", "http://localhost:8000/api"))
    credentials(ap)
    hash_arguments(ap)
    ap.add_argument("--no-reduce", action="store_true")
    if work_dir:
        ap.add_argument(
            "--work-dir",
            type=Path,
            help="SQLite workspace inside --data-root (default: <data-root>/.work)",
        )
    return ap


def hash_algorithm(name):
    """Reject an unknown hash name while parsing, before any dataset is created."""
    from fridge.core.hashes import resolve_hash_algorithms

    try:
        resolve_hash_algorithms([name])
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None
    return name


def hash_arguments(ap):
    ap.add_argument(
        '--hash-algorithms',
        nargs='+',
        type=hash_algorithm,
        default=['dhash_8', 'phash_8'],
        help='Perceptual hashes on image rows (default: dhash_8 phash_8); dhash_N, phash_N, ahash_N, whash_N',
    )
    ap.add_argument('--no-hashes', action='store_true', help='Skip perceptual hashes during import')


def attach_hashes(args, samples, images):
    """Hash the already decoded, served RGB pixels; resume never passes skipped rows."""
    if getattr(args, 'no_hashes', False):
        return
    from fridge.core.hashes import resolve_hash_algorithms

    algorithms = resolve_hash_algorithms(getattr(args, 'hash_algorithms', None))
    for sample, image in zip(samples, images, strict=True):
        if sample.media_type == 'image':
            sample.hashes.update({name: function(image) for name, function in algorithms.items()})


def credentials(ap):
    ap.add_argument('--username', default=os.getenv('FRIDGE_USERNAME', 'admin'))
    ap.add_argument('--password', default=os.getenv('FRIDGE_PASSWORD'), help='Prefer FRIDGE_PASSWORD or a TTY prompt')


def password(args):
    """None when FRIDGE_API_TOKEN is set: the SDK client then authenticates every request with it."""
    if os.getenv('FRIDGE_API_TOKEN'):
        return None
    if args.password:
        return args.password
    if sys.stdin.isatty():
        return getpass.getpass('Freedom Ridge password: ')
    raise ValueError('Set FRIDGE_PASSWORD (or supply --password); there is no default password')


def contained_path(path, data_root):
    path = path.resolve()
    if not path.is_relative_to(data_root.resolve()):
        raise ValueError(f'Path escaped --data-root: {path}')
    return path


def writable_directory(path, data_root):
    path = contained_path(path, data_root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def require_files(paths):
    missing = [str(p) for p in paths if not p.is_file() or not p.stat().st_size]
    if missing:
        raise FileNotFoundError(
            "Required local files are missing (downloads may still be arriving):\n" + "\n".join(missing),
        )


def json_sections(path, chunk_size=65536):
    """Stream a JSON object's top-level arrays, one element at a time.

    COCO's monolithic JSON needs no extra parser dependency. RAM is bounded by
    the largest individual record plus a read buffer, including for skipped arrays.
    Non-array sections (e.g. info) are emitted as a single value.
    """
    decoder = json.JSONDecoder()
    with path.open(encoding="utf-8") as source:
        buffer, eof = "", False

        def fill():
            nonlocal buffer, eof
            more = source.read(chunk_size)
            buffer += more
            eof = not more

        def peek():
            nonlocal buffer
            buffer = buffer.lstrip()
            while not buffer and not eof:
                fill()
                buffer = buffer.lstrip()
            if not buffer:
                raise ValueError(f"Truncated JSON: {path}")
            return buffer[0]

        def take(char):
            nonlocal buffer
            if peek() != char:
                raise ValueError(f"Expected {char!r} in {path}")
            buffer = buffer[1:]

        def value():
            nonlocal buffer
            peek()
            while True:
                try:
                    result, end = decoder.raw_decode(buffer)
                    # A number may straddle chunks; wait for its delimiter.
                    if end == len(buffer) and not eof:
                        fill()
                        continue
                    buffer = buffer[end:]
                    return result
                except json.JSONDecodeError:
                    if eof:
                        raise ValueError(f"Invalid or truncated JSON: {path}") from None
                    fill()

        take('{')
        if peek() != '}':
            while True:
                key = value()
                take(':')
                if peek() == '[':
                    take('[')
                    if peek() != ']':
                        while True:
                            yield key, value()
                            if peek() == ']':
                                break
                            take(',')
                    take(']')
                else:
                    yield key, value()
                if peek() == '}':
                    break
                take(',')
        take('}')
        while True:
            if buffer.strip():
                raise ValueError(f"Trailing content in {path}")
            if eof:
                break
            buffer = ''
            fill()


class AnnotationIndex:
    """Disposable on-disk join index; neither annotations nor sample IDs accumulate in RAM."""

    def __init__(self, work_dir, data_root=None):
        self.temp = tempfile.TemporaryDirectory(
            prefix="fridge-index-",
            dir=writable_directory(work_dir, data_root or work_dir),
        )
        self.db = sqlite3.connect(Path(self.temp.name) / "annotations.sqlite")
        self.db.execute("PRAGMA cache_size = -8192")
        self.db.execute("PRAGMA temp_store = FILE")
        self.db.execute("CREATE TABLE records (kind TEXT, image_id TEXT, payload TEXT)")
        self.db.execute("CREATE INDEX record_lookup ON records(kind, image_id)")

    def add(self, rows):
        iterator = iter(rows)
        while batch := list(itertools.islice(iterator, 1000)):
            self.db.executemany(
                "INSERT INTO records VALUES (?, ?, ?)",
                ((kind, str(image_id), json.dumps(row, separators=(',', ':'))) for kind, image_id, row in batch),
            )
            self.db.commit()

    def rows(self, kind, image_id):
        for (payload,) in self.db.execute(
            "SELECT payload FROM records WHERE kind = ? AND image_id = ? ORDER BY rowid",
            (kind, str(image_id)),
        ):
            yield json.loads(payload)

    def images(self, limit):
        for (payload,) in self.db.execute(
            "SELECT payload FROM records WHERE kind = 'images' ORDER BY image_id LIMIT ?",
            (limit or -1,),
        ):
            yield json.loads(payload)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()
        self.temp.cleanup()


class Assets:
    """Atomic local derivatives, confined to the dataset's data root."""

    def __init__(self, data_root):
        self.data_root = data_root.resolve()
        self.directory = writable_directory(self.data_root / 'derived', self.data_root)

    def path(self, relative):
        return contained_path(self.directory / relative, self.data_root)

    def save(self, relative, image, *, decoded=False):
        """Optionally return the encoded pixels without another disk read (lossy JPEGs)."""
        path = self.path(relative)
        writable_directory(path.parent, self.data_root)
        handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=path.suffix)
        os.close(handle)
        try:
            options = {'quality': 95} if path.suffix == '.jpg' else {}
            if decoded:
                with BytesIO() as buffer:
                    image.save(buffer, format=Image.registered_extensions()[path.suffix], **options)
                    Path(temporary).write_bytes(buffer.getbuffer())
                    buffer.seek(0)
                    with Image.open(buffer) as encoded:
                        saved_image = encoded.convert('RGB')
            else:
                image.save(temporary, **options)
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return saved_image if decoded else str(path)

    def mask(self, relative, mask, field, class_id, label, slot):
        from fridge import Segmentation

        # Canvas reads the red byte. Partition >255 classes into stable banks,
        # so one slot never assigns the same pixel ID to two different classes.
        bank, pixel = divmod(class_id - 1, 255)
        pixel += 1
        if bank:
            field += f'_bank_{bank}'
        if mask is not None:
            with mask.convert('L') as grayscale:
                with grayscale.point(lambda value: pixel if value else 0) as encoded:
                    self.save(relative, encoded)
        descriptor = Segmentation(str(self.path(relative)), labels=[label], mask_targets={str(pixel): label})
        descriptor['slot'] = slot
        return field, descriptor


def segmentation_settings(config, samples):
    """Merge dataset-level hover targets and distinct slot colors, preserving user settings."""
    config = copy.deepcopy(config or {})
    for sample in samples:
        for field, value in sample.metadata.items():
            if not isinstance(value, dict) or 'maskPath' not in value:
                continue
            slot = value['slot']
            rgb = colorsys.hsv_to_rgb((slot * 0.61803398875) % 1, 0.75, 0.95)
            color = '#' + ''.join(f'{round(channel * 255):02x}' for channel in rgb)
            targets = config.setdefault('mask_targets', {}).setdefault(field, {})
            colors = config.setdefault('color_scheme', {}).setdefault('fields', {}).setdefault(field, {})
            for pixel, label in value['mask_targets'].items():
                targets[pixel] = label
                colors.setdefault('idColors', {}).setdefault(pixel, color)
    return config


class Media:
    """Where sample media is recorded: local paths below --data-root, or with --media-uri the same files
    uploaded to S3 under that prefix, keyed by their path below --data-root. A batch's files are uploaded
    before the batch is committed, so every recorded s3:// path exists. The local files stay in place for
    resume and verification. Uploads use boto3's usual credentials (AWS_PROFILE, environment variables)."""

    def __init__(self, args, client=None):
        self.data_root = args.data_root.resolve()
        self.uri = getattr(args, 'media_uri', None)
        self._client = client
        self._uploaded = set()  # this run's uploads: a parent's image is not sent again with its objects
        if self.uri:
            self.bucket, _, prefix = self.uri.removeprefix('s3://').partition('/')
            self.base = f's3://{self.bucket}/' + (f'{prefix}/' if prefix else '')
            # Importers record resolved and as-given paths; both name files below the data root.
            self.roots = tuple({str(args.data_root.absolute()) + os.sep, str(self.data_root) + os.sep})

    @property
    def client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client('s3', config=Config(max_pool_connections=UPLOAD_THREADS))
        return self._client

    def remote(self, path):
        """The path recorded for a file: with --media-uri, the S3 URI of a file below the data root.
        Anything else (local mode, relative paths, URLs) is recorded as it is."""
        if not self.uri or not isinstance(path, str) or not path.startswith(self.roots):
            return path
        return self.base + contained_path(Path(path), self.data_root).relative_to(self.data_root).as_posix()

    def lookup(self, dataset, paths):
        """dataset.lookup_samples('filepath', ...) for local paths, keyed by the local paths."""
        recorded = {self.remote(path): path for path in paths}
        return {
            recorded[path]: sample_id for path, sample_id in dataset.lookup_samples('filepath', list(recorded)).items()
        }

    @staticmethod
    def _rewrite(sample, convert):
        """A copy of `sample` with its filepath and the strings in its metadata (top level, and one level
        down: mask and LiDAR paths, parent_filepath, source files) passed through `convert`."""
        clone = copy.copy(sample)
        clone.filepath = convert(sample.filepath)
        clone.metadata = {
            key: {k: convert(v) for k, v in value.items()} if isinstance(value, dict) else convert(value)
            for key, value in sample.metadata.items()
        }
        return clone

    def publish(self, samples):
        """The samples as they should be recorded: with --media-uri, copies naming S3, after every file
        they name below the data root is uploaded (a recorded s3:// path always exists, and no local path
        reaches the server). Locally the samples themselves."""
        if not self.uri:
            return samples
        uploads = {}

        def record(value):
            remote = self.remote(value)
            if remote is not value and remote not in self._uploaded:
                uploads[remote] = value
            return remote

        published = [self._rewrite(sample, record) for sample in samples]
        self.upload(uploads)
        self._uploaded.update(uploads)
        return published

    def restore(self, sample):
        """A sample read back from the server, with its recorded S3 paths mapped to the local files."""
        if not self.uri:
            return sample

        def local(value):
            if isinstance(value, str) and value.startswith(self.base):
                return str(self.data_root / value.removeprefix(self.base))
            return value

        return self._rewrite(sample, local)

    def upload(self, files):
        """Upload {s3 uri: local path} in parallel; the first failure cancels the files not yet started."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not files:
            return
        client = self.client  # created here, once: boto3's default session is not thread-safe

        def put(remote, path):
            content_type = mimetypes.guess_type(path)[0] or 'application/octet-stream'
            with open(path, 'rb') as body:
                client.put_object(
                    Bucket=self.bucket,
                    Key=remote.removeprefix(f's3://{self.bucket}/'),
                    Body=body,
                    ContentType=content_type,
                )

        with ThreadPoolExecutor(UPLOAD_THREADS) as pool:
            futures = [pool.submit(put, remote, path) for remote, path in files.items()]
            try:
                for future in as_completed(futures):
                    future.result()
            except Exception as error:
                for future in futures:
                    future.cancel()
                if aws_login_needed(error):
                    raise SystemExit(
                        f'Uploading to {self.uri} needs a current AWS login ({error}). Log in again '
                        '(e.g. aws sso login) and rerun the same command: the import resumes where it stopped.',
                    ) from error
                raise


UPLOAD_THREADS = 16
_EXPIRED_CODES = {'ExpiredToken', 'ExpiredTokenException', 'RequestExpired', 'InvalidToken', 'InvalidClientTokenId'}


def aws_login_needed(error):
    """Missing or expired AWS credentials, as opposed to any other upload failure."""
    from botocore.exceptions import ClientError, NoCredentialsError, SSOError, TokenRetrievalError

    if isinstance(error, (NoCredentialsError, SSOError, TokenRetrievalError)):
        return True
    return isinstance(error, ClientError) and error.response.get('Error', {}).get('Code') in _EXPIRED_CODES


def check_media(client, sample_id, media=None):
    with client.session.get(f'{client.base_url}/samples/{sample_id}/media', timeout=30, stream=True) as response:
        if response.status_code == 403 and media is not None and media.uri:
            raise RuntimeError(
                f'Image API returned 403: the server may not read {media.uri}. Name that bucket in its S3_BUCKET '
                'setting or in a storage configuration (docs/S3_PERMISSIONS.md), then rerun.',
            )
        if response.status_code == 403:
            raise RuntimeError(
                'Image API returned 403: --data-root must be inside the backend FRIDGE_DATA_ROOT '
                '(including resolved symlink targets). Move/copy the data there and rerun.',
            )
        response.raise_for_status()


class ClipEncoder:
    """One lazy, cached-only encoder; preserve each importer's vector normalization."""

    def __init__(self, args):
        self.args = args
        self.model = self.processor = None

    def __call__(self, images, normalize=False):
        import torch
        from fridge.core.clip import image_vectors, pixel_inputs
        from transformers import CLIPModel, CLIPProcessor

        if self.model is None:
            self.device = self.args.device
            if self.device in (None, 'auto'):
                self.device = auto_device(torch)
            print(f'Loading cached {self.args.model} on {self.device}', flush=True)
            self.model = CLIPModel.from_pretrained(self.args.model, local_files_only=True).to(self.device).eval()
            self.processor = CLIPProcessor.from_pretrained(self.args.model, local_files_only=True)
        assert self.processor is not None
        inputs = pixel_inputs(self.processor, images).to(self.device)
        with torch.inference_mode():
            vectors = image_vectors(self.model.get_image_features(**inputs))
            if normalize:
                vectors = vectors / vectors.norm(dim=-1, keepdim=True).clamp_min(1e-12)
        return vectors.cpu().tolist()

    def encode(self, samples, images, field):
        for sample, vector in zip(samples, self(images, normalize=True), strict=True):
            sample.embeddings = {field: vector}


def auto_device(torch):
    """Test CUDA execution, and explain a CPU fallback on GPU hosts."""
    reason = None
    try:
        if torch.cuda.is_available():
            torch.empty(1, device='cuda').add_(1)
            torch.cuda.synchronize()
            return 'cuda'
    except (RuntimeError, AssertionError) as error:
        reason = str(error)
    try:
        gpu = subprocess.run(['nvidia-smi', '-L'], capture_output=True, text=True, timeout=5)
        present = gpu.returncode == 0 and bool(gpu.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        present = False
    if present or reason:
        print(
            'WARNING: NVIDIA/CUDA hardware is present but CUDA is unusable; using CPU. '
            'Check nvidia-smi and install a PyTorch build compatible with your driver '
            '(see examples/README.md). ' + (reason or ''),
            file=sys.stderr,
            flush=True,
        )
    return 'cpu'


def login(client, args=None):
    if args is None:
        args = argparse.Namespace(username=os.getenv('FRIDGE_USERNAME', 'admin'), password=os.getenv('FRIDGE_PASSWORD'))
    secret = password(args)
    if secret is not None:
        client.login(args.username, secret)


def existing_dataset(client, name):
    """Load a dataset by name without creating it: Dataset(name) creates a missing one, so a typo would
    silently start a new, empty dataset."""
    from fridge import Dataset

    info = client.get_dataset(name)
    if info is None:
        raise SystemExit(f"No dataset named {name!r} at {client.base_url}")
    return Dataset.from_id(info['id'])


def duplicates(dataset, paths, policy, media=None):
    existing = {}
    if policy != 'update':
        existing = media.lookup(dataset, paths) if media else dataset.lookup_samples('filepath', paths)
    if existing and policy == 'error':
        raise RuntimeError(f'{len(existing)} duplicate filepaths; use --on-duplicate skip or update')
    return existing


def add_batch(dataset, samples, policy, media=None):
    result = dataset.add_samples(media.publish(samples) if media else samples, on_duplicate=policy)
    if result.get('existing'):
        raise RuntimeError(f"Duplicate conflict during upload: {result['existing']}")
    return result


def reduce_if_written(client, dataset, args, field, written, recover=False):
    if args.no_reduce or getattr(args, 'skip_embeddings', False):
        return
    if recover:
        inventory = {item['field_name']: item['count'] for item in client.get_embedding_field_inventory(dataset._id)}
        count = inventory.get(field, 0)
        if count < 5:
            print(f'Skipping {field} UMAP: need at least 5 embeddings (have {count})', flush=True)
            return
        if not written:
            pending = client.get_vectors(dataset._id, field_name=field, limit=1, pending_only=True)
            written = bool(pending['items'])
    if written:
        try:
            result = dataset.compute_visualization(field_name=field)
            if result.get('skipped') and result.get('reason') == 'map_managed':
                print(f'Skipping {field} UMAP: map managed by scheduled re-fits', flush=True)
                return
            if result.get('ranked') is False:
                raise RuntimeError(
                    f'{field} coordinates are saved but ranking the map failed; retry ranking without re-fitting: '
                    f'get_client().recompute_viz_rank({dataset._id}, {field!r}, full=True)',
                )
            print(
                f"{field} UMAP: fit {result['fit_sample_size']:,}; placed {result['reduced']:,} points",
                flush=True,
            )
        except Exception as error:
            print(f'Samples are imported, but building the {field} map failed: {error}', flush=True)
            print(
                f"Build the map later: Dataset({args.dataset_name!r}).compute_visualization(field_name={field!r})",
                flush=True,
            )
            if getattr(args, 'reduce_errors', 'raise') != 'warn':
                raise
    else:
        print(f'Skipping reduction / UMAP: no samples written for {field}', flush=True)


def object_samples(parent, parent_id, assets):
    """One stable crop path and object row for EVERY box, including crowd/group regions."""
    from fridge import Sample

    for detection in parent.metadata['detections']:
        label = detection['label']
        attrs = {**detection.get('attributes', {}), **{k: v for k, v in detection.items() if k != 'attributes'}}
        attrs.pop('bbox')
        attrs.pop('id')
        attrs.update(
            parent_id=parent_id,
            crop_bbox=detection['bbox'],
            label=label,
            detection_id=detection['id'],
        )
        if 'iscrowd' in attrs:
            attrs['is_crowd'] = bool(attrs['iscrowd'])
        if 'IsGroupOf' in attrs:
            attrs['is_group_of'] = bool(attrs['IsGroupOf'])
        path = assets.path(f"objects/{parent.metadata['image_id']}/{detection['id']}.jpg")
        sample = Sample(str(path), tags=[label], metadata=attrs, location=parent.location)
        sample.media_type = 'object'
        yield sample


def cuboid_object_samples(parent, parent_id, assets, field):
    """Plan SDK cuboid object rows without decoding or rewriting existing crops."""
    from fridge import Calibration, objects_from_cuboids

    raw = parent.metadata['calibration']
    calibration = Calibration(raw['P'], raw['image_size'], raw['transforms'], raw.get('camera'))
    location, heading = parent.location, parent.heading
    if isinstance(location, dict):
        heading = location.get('heading', heading)
        location = tuple(location['coordinates'])
    directory = assets.path(f'objects/{parent_id}')
    if directory.exists() and any(path.is_symlink() for path in directory.iterdir()):
        raise ValueError('Refusing symlinks in crop directory')
    rows = objects_from_cuboids(
        parent_id,
        parent.filepath,
        None,
        parent.metadata.get(field, []),
        calibration,
        directory,
        location=location,
        field=field,
        write_crops=False,
    )
    for row in rows:
        contained_path(Path(row.filepath), assets.data_root)
        row.heading = heading
    return rows


def save_crop(parent_image, sample, assets):
    x, y, width, height = sample.metadata['crop_bbox']
    # Preserve even subpixel/zero-area annotations as a minimum one-pixel crop.
    left = max(0, min(parent_image.width - 1, math.floor(x * parent_image.width)))
    top = max(0, min(parent_image.height - 1, math.floor(y * parent_image.height)))
    right = max(left + 1, min(parent_image.width, math.ceil((x + width) * parent_image.width)))
    bottom = max(top + 1, min(parent_image.height, math.ceil((y + height) * parent_image.height)))
    with parent_image.crop((left, top, right, bottom)) as crop:
        assets.save(Path(sample.filepath).relative_to(assets.directory), crop)
    return rgb_image(sample.filepath)


def ingest_objects(
    args,
    index,
    make_sample,
    description,
    array_fields=(),
    *,
    object_factory=object_samples,
    persisted_parents=False,
    encoder_factory=None,
    dataset_finish=None,
    recover_reduction=False,
    refresh_description=False,
):
    """Commit parents first, then resumable same-dataset objects, in bounded batches."""
    from fridge import Dataset, Sample, connect, get_client

    media = Media(args)
    backend_root = os.getenv('FRIDGE_DATA_ROOT')
    if backend_root and not media.uri:
        contained_path(args.data_root, Path(backend_root))
    secret = password(args)
    if args.field_name == 'clip_obj':
        raise ValueError('--field-name clip_obj is reserved for object embeddings')
    assets = Assets(args.data_root)
    connect(args.api_url)
    client = get_client()
    if secret is not None:
        client.login(args.username, secret)
    dataset = Dataset(args.dataset_name, description=description)
    # Demo importers opt in so resumed imports acquire their current data credits.
    if refresh_description:
        client.update_dataset(dataset._id, description=description)
    if not getattr(args, 'skip_embeddings', False):
        for field in (args.field_name, 'clip_obj'):
            client.register_field_model(dataset._id, field, args.model)
    for field in array_fields:
        dataset.create_array_field(field)
    config = client.get_dataset_by_id(dataset._id).get('segmentation_config') or {}
    encoder = None
    started = time.monotonic()
    counts = dict(images_written=0, images_skipped=0, objects_written=0, objects_skipped=0)
    if persisted_parents:
        counts['objects_without_extent'] = 0
    checked = False

    def write(samples, images, field, grain):
        nonlocal encoder
        attach_hashes(args, samples, images)
        if not getattr(args, 'skip_embeddings', False):
            if encoder is None:
                encoder = (encoder_factory or ClipEncoder)(args)
            encoder.encode(samples, images, field)
        result = add_batch(dataset, samples, args.on_duplicate, media)
        if result.get('existing') and args.on_duplicate == 'error':
            raise RuntimeError(f"Duplicate filepaths: {result['existing']}")
        ids = result.get('ids', [])
        counts[f'{grain}_written'] += len(ids)
        counts[f'{grain}_skipped'] += result.get('skipped', 0)
        # Normal add/update returns IDs in input order. Concurrent skips need a lookup.
        if len(ids) == len(samples):
            return dict(zip((s.filepath for s in samples), ids))
        return media.lookup(dataset, [s.filepath for s in samples])

    records = iter(index.images(args.limit))
    while batch := list(itertools.islice(records, args.batch_size)):
        paths = [make_sample(row, assets, metadata_only=True) for row in batch]
        existing = duplicates(dataset, paths, args.on_duplicate, media)
        parents, samples, images = [], [], []
        try:
            for row, path in zip(batch, paths):
                sample, image = make_sample(row, assets, annotations_only=path in existing)
                parents.append(sample)
                if path in existing:
                    counts['images_skipped'] += 1
                else:
                    samples.append(sample)
                    images.append(image)
            parent_ids = dict(existing)
            if samples:
                parent_ids.update(write(samples, images, args.field_name, 'images'))
            # Also check on resume: a previous process may have stopped after parent insertion.
            if not checked and parents:
                check_media(client, parent_ids[parents[0].filepath], media)
                checked = True
            settings = segmentation_settings(config, parents)
            if settings != config:
                client.update_dataset(dataset._id, segmentation_config=settings)
                config = settings
        finally:
            for image in images:
                image.close()
        if persisted_parents:
            rows = client.query_samples(
                dataset._id,
                limit=len(parent_ids),
                filters={'field': 'id', 'operator': 'in', 'value': list(parent_ids.values())},
            )['items']
            if {row['id'] for row in rows} != set(parent_ids.values()):
                raise RuntimeError('Parent query incomplete after image upload')
            parents = [media.restore(Sample.from_row(row)) for row in rows]
        for parent in parents:
            rows = object_factory(parent, parent_ids[parent.filepath], assets)
            if persisted_parents:
                counts['objects_without_extent'] += len(parent.metadata.get('ground_truth_3d', [])) - len(rows)
            objects = iter(rows)
            parent_image = None
            try:
                while object_batch := list(itertools.islice(objects, args.batch_size)):
                    existing_objects = duplicates(
                        dataset,
                        [s.filepath for s in object_batch],
                        args.on_duplicate,
                        media,
                    )
                    counts['objects_skipped'] += len(existing_objects)
                    pending = [s for s in object_batch if s.filepath not in existing_objects]
                    # A recorded S3 crop was uploaded before its row existed; only a local one can be missing.
                    repair = [
                        s
                        for s in object_batch
                        if s.filepath in existing_objects and not media.uri and not Path(s.filepath).is_file()
                    ]
                    if not pending and not repair:
                        continue
                    if parent_image is None:
                        parent_image = rgb_image(parent.filepath)
                    crops = []
                    try:
                        for sample in repair:
                            save_crop(parent_image, sample, assets).close()
                        for sample in pending:
                            if args.on_duplicate == 'skip' and Path(sample.filepath).is_file():
                                crops.append(rgb_image(contained_path(Path(sample.filepath), assets.data_root)))
                            else:
                                crops.append(save_crop(parent_image, sample, assets))
                        if pending:
                            write(pending, crops, 'clip_obj', 'objects')
                    finally:
                        for crop in crops:
                            crop.close()
            finally:
                if parent_image is not None:
                    parent_image.close()
        print(f'{counts}; {time.monotonic() - started:.1f}s', flush=True)
    dataset.refresh_stats()
    if dataset_finish:
        dataset_finish(client, dataset)
    for field, grain in ((args.field_name, 'images'), ('clip_obj', 'objects')):
        reduce_if_written(client, dataset, args, field, counts[f'{grain}_written'], recover=recover_reduction)
    print(f'Done: dataset={args.dataset_name}, {counts}, seconds={time.monotonic() - started:.1f}')
    return counts


def rgb_image(path, rotation=0):
    with Image.open(path) as source:
        image = source.convert('RGB')
    if rotation:
        rotated = image.rotate(rotation, expand=True)
        image.close()
        return rotated
    return image


def coordinates(latitude, longitude):
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lon, lat


def utc_datetime(value):
    """ISO datetime, normalized to UTC; absent dates stay null, never become 'now'."""
    if value is None or not str(value).strip():
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ingest(
    args,
    samples,
    field_types,
    description,
    counts=None,
    encoder_factory=None,
    prepare=None,
    dataset_setup=None,
    local_path=None,
    refresh_description=False,
):
    """Lookup duplicates BEFORE decoding/embedding; persist one complete batch at a time."""
    from fridge import Dataset, connect, get_client
    from PIL import Image, UnidentifiedImageError

    media = Media(args)
    backend_root = os.getenv('FRIDGE_DATA_ROOT')
    if backend_root and not media.uri:
        contained_path(args.data_root, Path(backend_root))
    counts = counts if counts is not None else Counter()
    start = time.monotonic()
    connect(args.api_url)
    client = get_client()
    login(client, args)
    dataset = Dataset(args.dataset_name, description=description)
    # Demo importers opt in so resumed imports acquire their current data credits.
    if refresh_description:
        client.update_dataset(dataset._id, description=description)
    if dataset_setup:
        dataset_setup(client, dataset)
    encoder = None
    media_checked = False
    samples = iter(samples)
    while batch := list(itertools.islice(samples, args.batch_size)):
        existing = duplicates(dataset, [s.filepath for s in batch], args.on_duplicate, media)
        if not media_checked and existing:
            check_media(client, next(iter(existing.values())), media)
            media_checked = True
        counts["duplicate_skipped"] += len(existing)
        pending, images = [], []
        try:
            for sample in batch:
                if sample.filepath in existing:
                    continue
                try:
                    if prepare:
                        prepare(sample)
                    path = local_path(sample) if local_path else sample.filepath
                    with Image.open(contained_path(Path(path), args.data_root)) as source:
                        rgb = source.convert("RGB")
                except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
                    counts["unreadable_image"] += 1
                    print(f"Skipping unreadable image {sample.filepath}: {exc}", flush=True)
                    continue
                pending.append(sample)
                images.append(rgb)
            if not pending:
                continue
            attach_hashes(args, pending, images)
            if encoder is None:
                encoder = (encoder_factory or ClipEncoder)(args)
            vectors = encoder(images)
            if len(vectors) != len(pending):
                raise RuntimeError("Encoder returned the wrong number of vectors")
            for sample, vector in zip(pending, vectors):
                sample.embeddings = {args.field_name: vector}
            result = add_batch(dataset, pending, args.on_duplicate, media)
            counts["written"] += len(result.get("ids", []))
            counts["duplicate_skipped"] += result.get("skipped", 0)
            if not media_checked and result.get("ids"):
                check_media(client, result["ids"][0], media)
                media_checked = True
            print(f"Progress: {dict(counts)}", flush=True)
        finally:
            for image in images:
                image.close()

    # Null-only columns still need their intended types for the Filters UI.
    schema = {f["name"]: f for f in dataset.get_schema()}
    for name, kind in field_types.items():
        if name in schema and schema[name].get("declared_type", schema[name]["type"]) != kind:
            dataset.set_field_type(name, kind)
    dataset.refresh_stats()
    print(f"Import complete: {dict(counts)}; elapsed={time.monotonic() - start:.1f}s", flush=True)
    reduce_if_written(client, dataset, args, args.field_name, counts['written'])
    return counts
