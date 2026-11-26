"""The sources of ``Dataset.add_samples_from``, and each copied object linked to the copy of its image.

A copy reads a ``DatasetView`` (a whole dataset is one), a similarity result (a ``DatasetView`` too) or the view
of a dataset version (``VersionView``); nothing else. A plain view, and a version's view that isn't cut, are
narrowed with filters (a version's filters query the version's rows), a page at a time. A source that filters
would change (cut with skip()/limit(), a similarity result, a cut version's view) is listed once instead: its
samples' ids, paths and media types, held in memory, and read back by id from where they came from (the dataset
for a view of it, the VERSION for a version's view, never the dataset's current rows).

An object (``media_type`` ``object``) names its image by sample id in ``metadata.parent_id``. Ids are numbered by
each server, across all its datasets, so the source's id never names the copy of the image: on the same server it
names the source's image (another dataset), and on another server whatever sample has that number there. So the
copy writes every image before any object, and gives each object the target's id of the sample at its image's path
(after the path rule), or null when the target has no image there: no copied object keeps a source id.
"""

import re
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .sample import Sample
from .version_view import VersionView
from .view import DatasetView, F, SimilarityResultView

OBJECT = 'object'
_DIGITS = re.compile(r'[0-9]{1,10}')
_MAX_ID = 2**31 - 1
_LISTED = ['id', 'filepath', 'media_type']

Listing = List[Tuple[Any, str, str]]  # (id, filepath, media_type) of each sample of a listed source


def parent_sample_id(value: Any) -> Optional[int]:
    """``metadata.parent_id`` as a sample id, as the server reads it (a whole number, or up to ten digits as text),
    or None when it isn't one: such a value names no sample anywhere and is copied as it is."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _DIGITS.fullmatch(value):
        number = int(value)
    else:
        return None
    return number if 1 <= number <= _MAX_ID else None


def refuse_unknown(source: Any) -> None:
    """A source the copy can't read correctly is refused before anything is read."""
    if not isinstance(source, (DatasetView, VersionView)):
        raise TypeError(
            f"add_samples_from copies a Dataset, a view of one (dataset.filter(...), dataset.all(), a similarity "
            f"result) or a version's view (dataset.version(...)), not {type(source).__name__}",
        )


def listing(view, batch_size: int) -> Optional[Listing]:
    """The (id, filepath, media_type) of every sample of a source that filters would change (cut, or a similarity
    result), read once; None for a plain view or a version's view that isn't cut, narrowed with filters instead."""
    cut = view._limit_val is not None or (isinstance(view, DatasetView) and bool(view._offset_val))
    if isinstance(view, SimilarityResultView):
        samples = view.iter_samples()
    elif cut:
        samples = view.select_fields(_LISTED).iter_samples(batch_size=batch_size)
    else:
        return None
    return [(sample.id, sample.filepath, sample.media_type) for sample in samples]


def _by_ids(view, ids: List[Any], batch_size: int) -> Iterator[Sample]:
    """The samples of a listed source with these ids, in id order, a page of ``batch_size`` at a time: a version's
    rows from the version, the others from the dataset."""
    if isinstance(view, VersionView):
        whole = view._clone(_limit_val=None)
        for start in range(0, len(ids), batch_size):
            yield from whole.filter(F('id').is_in(ids[start : start + batch_size])).iter_samples(batch_size=batch_size)
        return
    for start in range(0, len(ids), batch_size):
        chunk = DatasetView(view._dataset, F('id').is_in(ids[start : start + batch_size]).to_dict())
        chunk._selected_fields = list(view._selected_fields) if view._selected_fields else None
        yield from chunk.iter_samples(batch_size=batch_size)


def copy_passes(view, listed: Optional[Listing], batch_size: int) -> Tuple[bool, List[Iterator[Sample]]]:
    """(whether the source holds objects, the passes that copy it). Without objects, the one pass is the source
    itself, as before (a similarity result aside, below); with them, its other samples first, then its objects.

    A plain view is split with filters, after one count of its objects; a listed source by the ids its listing
    holds. A similarity result's rows lack the heading and the array fields, so its samples are always read again
    by id, whole.
    """
    if listed is None:
        if not len(view.filter(F('media_type') == OBJECT)):
            return False, [view.iter_samples(batch_size=batch_size)]
        return True, [
            view.filter(F('media_type') != OBJECT).iter_samples(batch_size=batch_size),
            view.filter(F('media_type') == OBJECT).iter_samples(batch_size=batch_size),
        ]
    object_ids = [sample_id for sample_id, _, kind in listed if kind == OBJECT]
    if not object_ids and isinstance(view, SimilarityResultView):
        return False, [_by_ids(view, [sample_id for sample_id, _, _ in listed], batch_size)]
    if not object_ids:
        return False, [view.iter_samples(batch_size=batch_size)]
    others = [sample_id for sample_id, _, kind in listed if kind != OBJECT]
    return True, [_by_ids(view, others, batch_size), _by_ids(view, object_ids, batch_size)]


def _images(dataset, field: str, values: list) -> List[Sample]:
    """The dataset's samples that aren't objects whose `field` (id or filepath) is one of `values`: one indexed
    read. Paged from id 0 with no count, which spares the server's first-page planning probe (measured: 60 ms to
    20 ms for 500 paths on a 400,000-sample target)."""
    view = DatasetView(dataset, F(field).is_in(values).to_dict()).select_fields(_LISTED)
    view._limit_val = len(values)
    samples, _ = view._execute_query(after_id=0, skip_count=True)
    return [sample for sample in samples if sample.media_type != OBJECT]


def image_paths(view, listed: Optional[Listing]) -> Callable[[List[int]], Dict[Any, str]]:
    """How the copy finds the paths of objects' images in the source: {id: path} of the images among some ids.

    For a view of the dataset, the dataset's samples with those ids (an image outside the copied view counts). For
    a version's view, the version's: a cut view's listing first, then the version, by id; an image the version
    doesn't hold has no path, and its objects no image in the copy.
    """
    if not isinstance(view, VersionView):
        return lambda ids: {sample.id: sample.filepath for sample in _images(view._dataset, 'id', ids)}
    held = {sample_id: (path, kind) for sample_id, path, kind in listed or []}
    version = VersionView(view._dataset, view._version)

    def paths(ids: List[int]) -> Dict[Any, str]:
        found: Dict[Any, str] = {
            sample_id: held[sample_id][0] for sample_id in ids if held.get(sample_id, ('', OBJECT))[1] != OBJECT
        }
        others = [sample_id for sample_id in ids if sample_id not in held]
        if others:
            rows = version.filter(F('id').is_in(others)).select_fields(_LISTED).iter_samples(batch_size=len(others))
            found.update({sample.id: sample.filepath for sample in rows if sample.media_type != OBJECT})
        return found

    return paths


def parents_in_target(image_paths_of, target, rule, objects: List[Sample]) -> Dict[int, int]:
    """{the source's parent id: the target's id of the image at that image's path (after the rule)} for the images
    of these objects that are in the target: the images' paths in the source (``image_paths``), then the samples
    at those paths in the target, in one indexed read."""
    wanted = sorted({pid for sample in objects if (pid := parent_sample_id(sample.metadata.get('parent_id')))})
    if not wanted:
        return {}
    paths = image_paths_of(wanted)
    if not paths:
        return {}
    new_paths = {sample_id: rule.apply(path) if rule is not None else path for sample_id, path in paths.items()}
    in_target: Dict[str, Any] = {
        sample.filepath: sample.id for sample in _images(target, 'filepath', sorted(set(new_paths.values())))
    }
    return {sample_id: in_target[path] for sample_id, path in new_paths.items() if path in in_target}
