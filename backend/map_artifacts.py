"""Where map re-fit files live: a small storage interface over the UMAP model folder.

Locally that is FRIDGE_UMAP_MODEL_DIR (default $FRIDGE_DATA_ROOT/umap_models, as for the SDK's saved
models). Object storage such as S3 can implement the same methods later. The database stores model
locations as keys relative to the store, so every process (runner, projection daemon, SDK on the same
machine) resolves them against its own mount of the folder.

    managed/<dataset>__<field>/<model_id>.joblib   immutable models; embedding_map_state names the current one
    work/<job_id>/                                 one re-fit's export, fit outputs and log; disposable

Legacy saved models (<dataset>__<field>.joblib at the top level) are never touched here.
"""

import hashlib
import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

_MODEL_KEY = re.compile(r'^managed/(\d+)__([^/]+)/([0-9a-f-]{36})\.joblib$')


def default_root() -> Path:
    return Path(
        os.getenv('FRIDGE_UMAP_MODEL_DIR')
        or os.path.join(os.getenv('FRIDGE_DATA_ROOT', os.path.expanduser('~/.fridge')), 'umap_models'),
    )


def model_key(dataset_id: int, field: str, model_id) -> str:
    return f'managed/{dataset_id}__{field}/{uuid.UUID(str(model_id))}.joblib'


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(16 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class LocalArtifactStore:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root is not None else default_root()

    def path(self, key: str) -> Path:
        if not _MODEL_KEY.match(key):
            raise ValueError(f'Not a managed model key: {key!r}')
        return self.root / key

    def work_dir(self, job_id) -> Path:
        return self.root / 'work' / str(uuid.UUID(str(job_id)))

    def put_model(self, source: Path, dataset_id: int, field: str, model_id) -> str:
        """Move a finished model file into the store under its immutable key; returns the key."""
        key = model_key(dataset_id, field, model_id)
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)  # same filesystem: work/ and managed/ share the root
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return key

    def delete(self, key: str) -> None:
        target = self.path(key)
        target.unlink(missing_ok=True)
        try:
            target.parent.rmdir()
        except OSError:
            pass  # other models of the field remain

    def models(self) -> Iterator[tuple[str, uuid.UUID]]:
        """(key, model_id) of every managed model file."""
        managed = self.root / 'managed'
        if not managed.is_dir():
            return
        for path in managed.glob('*/*.joblib'):
            key = path.relative_to(self.root).as_posix()
            match = _MODEL_KEY.match(key)
            if match:
                yield key, uuid.UUID(match.group(3))

    def work_jobs(self) -> list[uuid.UUID]:
        work = self.root / 'work'
        if not work.is_dir():
            return []
        found = []
        for entry in work.iterdir():
            try:
                found.append(uuid.UUID(entry.name))
            except ValueError:
                continue
        return found

    def remove_work(self, job_id) -> None:
        """Delete one job's work folder: its files, then its (at most two-level) folders."""
        folder = self.work_dir(job_id)
        if not folder.is_dir():
            return
        for path in sorted(folder.rglob('*'), key=lambda item: len(item.parts), reverse=True):
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()
        folder.rmdir()
