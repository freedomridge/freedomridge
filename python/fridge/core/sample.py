import os
import warnings
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .dataset import Dataset  # noqa: F401 — quoted forward refs only


def _jsonable(value):
    """Make metadata JSON-serializable: datetime/date objects become ISO-8601 strings
    (which ingest then types as 'datetime' at first sight), recursing into dicts/lists.
    Without this, the natural ``metadata={"captured_at": datetime.now()}`` crashed at
    request time with an opaque "Object of type datetime is not JSON serializable"."""
    import datetime as _dt

    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class Sample:
    """
    A sample represents a single data point in a dataset.

    Default fields:
        id: Sample ID (auto-generated)
        filepath: Path to the source data
        media_type: Type of media (image, video, audio) - auto-computed from filepath
        tags: List of string tags
        metadata: Dictionary of custom fields
        created_at: When the sample was added to the dataset
        last_modified_at: When the sample was last modified

    Custom fields can be added dynamically by setting attributes on the sample.

    Named embeddings ({field_name: vector}): a field name's dimension is fixed across EVERY dataset on
    the server, not per dataset, so name fields after the model that produced them.
    """

    # Class-level set of known array field names (populated from server)
    _known_array_fields: set = set()

    def __init__(
        self,
        filepath: str,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        location: Optional[tuple] = None,
        heading: Optional[float] = None,
        embeddings: Optional[Dict[str, List[float]]] = None,
        hashes: Optional[Dict[str, str]] = None,
        array_fields: Optional[Dict[str, List[str]]] = None,
        _dataset: Optional['Dataset'] = None,
    ):
        self.id = None
        self.filepath = filepath
        self.media_type = self._compute_media_type(filepath)
        self.tags = tags or []
        self.metadata = metadata or {}
        self.location = location
        self.heading = heading
        self.hashes = hashes or {}
        self.array_fields = array_fields or {}  # {field_name: [values]}
        # On a mirror, this server's own fields (`@local.<name>`, docs/FOLLOWERS.md) by name without the prefix,
        # `tags` for `@local.tags`. Read with `sample['@local.weeds']`; write with Client.bulk_set_metadata.
        self.local: Dict[str, Any] = {}
        self.created_at = None
        self.last_modified_at = None
        self.distance = None

        # Named embeddings: {"clip": [vector], "dinov2": [vector]}
        self.embeddings = embeddings or {}

        self._dataset = _dataset
        self._snapshot_version_id = None
        self._custom_fields: Dict[str, Any] = {}
        # Metadata keys as they were when this sample was hydrated from the server. save() diffs
        # against it so `del s.metadata["x"]` actually removes the key remotely — a plain merge
        # can only add or overwrite, so the delete used to be a silent no-op. Assigning a whole new
        # dict re-baselines this (see __setattr__), so `s.metadata = {...}` stays a MERGE.
        self._original_metadata_keys: set = set(self.metadata)
        # Which columns the server actually sent. save() must not write back a field it never
        # loaded — a slim projection omits `tags`, and `tags` is a full REPLACE on the wire.
        # A sample built here (not hydrated) carries everything its constructor was given.
        self._loaded_fields: set = {"tags", "metadata", "location", "heading"}

    @classmethod
    def from_row(cls, item: Dict[str, Any], dataset: Optional['Dataset'] = None) -> 'Sample':
        """Build a Sample from an API row. THE single place that mapping lives.

        There used to be four hand-written copies of this (two in view.py, two in dataset.py) and
        they had drifted: `Dataset.iter_samples` and `checkout` both omitted the owning dataset, so
        the SDK's own documented idiom —

            for s in dataset.iter_samples():
                s.reviewed = True
                s.save()

        — raised ValueError on the primary streaming iterator while the identical loop over
        `dataset.all()` worked. `checkout` additionally dropped `location`, `media_type` and the
        timestamps, so a caller comparing historical state got silently wrong answers. Any new
        wire field had to be threaded through four sites; `distance` and `array_fields` had already
        diverged.
        """
        sample = cls(filepath=item.get("filepath", ""), _dataset=dataset)
        # /versions/... rows name it sample_id; /samples rows name it id.
        sample.id = item.get("id") if item.get("id") is not None else item.get("sample_id")
        sample.media_type = item.get("media_type") or "unknown"
        sample.tags = item.get("tags", []) or []
        sample.metadata = item.get("metadata", {}) or {}
        sample.created_at = item.get("created_at")
        sample.last_modified_at = item.get("last_modified_at")
        sample._snapshot_version_id = item.get("version_id")
        sample.array_fields = dict(item.get("array_fields") or {})
        sample.local = dict(item.get("local_metadata") or {})
        if item.get("local_tags") is not None:
            sample.local["tags"] = list(item["local_tags"])
        if item.get("distance") is not None:
            sample.distance = item["distance"]
        if item.get("location"):
            sample.location = item["location"]
        heading = item.get("heading")
        if heading is None and isinstance(item.get("location"), dict):
            heading = item["location"].get("heading")  # the samples endpoints nest it in location
        if heading is not None:
            sample.heading = heading
        # Dynamic array columns (inferred_tags, …) arrive as top-level keys.
        for af in cls._known_array_fields:
            if af != 'tags' and af in item and af not in sample.array_fields:
                sample.array_fields[af] = item[af] or []
        # Snapshot AFTER metadata is populated, so save() can tell deletions from never-present.
        sample._original_metadata_keys = set(sample.metadata)
        # Only the columns the server actually sent. A slim projection (select_fields) omits
        # `tags`, and PATCH treats a non-None `tags` as a full REPLACE — so writing back the
        # defaulted [] would erase every tag the sample had. See save().
        sample._loaded_fields = set(item)
        return sample

    def __getitem__(self, name: str) -> Any:
        """A field's value: `sample['@local.weeds']` is this server's own field on a mirror (in `local`), any other
        name a metadata key. A field the sample doesn't have raises KeyError. A Sample isn't a sequence: `for x in
        sample` and `list(sample)` stay a TypeError, as before this method existed."""
        if not isinstance(name, str):
            raise TypeError(f"Sample fields are read by name, not {type(name).__name__}")
        if name.startswith('@local.'):
            return self.local[name[len('@local.') :]]
        return self.metadata[name]

    def _compute_media_type(self, filepath: str) -> str:
        """Automatically determine media type from file extension."""
        ext = os.path.splitext(filepath)[1].lower()
        if ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff']:
            return 'image'
        elif ext in ['.mp4', '.avi', '.mov', '.mkv', '.webm']:
            return 'video'
        elif ext in ['.mp3', '.wav', '.ogg', '.flac']:
            return 'audio'
        else:
            return 'unknown'

    def __setattr__(self, name: str, value: Any):
        """Allow dynamic field assignment."""
        _core = (
            'id',
            'filepath',
            'media_type',
            'tags',
            'metadata',
            'location',
            'heading',
            'embeddings',
            'hashes',
            'array_fields',
            'local',
            'created_at',
            'last_modified_at',
            '_custom_fields',
            '_dataset',
            '_snapshot_version_id',
            '_original_metadata_keys',
            '_loaded_fields',
            'distance',
        )
        if name in _core:
            super().__setattr__(name, value)
            # Replacing the metadata dict WHOLESALE is a replace of our local view, not an
            # instruction to delete server-side keys. Re-baseline so save() sends a plain merge.
            #
            # Without this, `s = dataset[path]; s.metadata = {...}; s.save()` deleted every key the
            # new dict happened to omit. An importer may replace metadata on re-ingest,
            # which must preserve server-side fields like category_reported,
            # saturation and the uniqueness_* scores that nothing upstream produces. In-place
            # `del s.metadata[k]` still registers as a removal, which is the behaviour that was
            # actually wanted.
            if name == 'metadata' and hasattr(self, '_original_metadata_keys'):
                super().__setattr__('_original_metadata_keys', set(value or {}))
            # An explicit assignment is an intentional full replace, so it may be written back even
            # if the field was never loaded.
            if name in ('tags', 'location', 'heading') and hasattr(self, '_loaded_fields'):
                self._loaded_fields.add(name)
        elif name in self.__class__._known_array_fields:
            # Dynamic array field — store in array_fields dict
            if hasattr(self, 'array_fields'):
                self.array_fields[name] = value
            else:
                super().__setattr__(name, value)
        else:
            # Custom fields go into metadata
            if hasattr(self, 'metadata'):
                self.metadata[name] = value
            else:
                super().__setattr__(name, value)

    def __getattr__(self, name: str):
        # Only called when normal attribute lookup fails. Read __dict__ directly
        # instead of hasattr/getattr — the latter re-enters __getattr__ and
        # infinitely recurses on half-initialized instances (e.g. the empty
        # object pickle/copy.deepcopy create before restoring state). Bail on
        # dunders so those probes raise cleanly.
        if name.startswith('__'):
            raise AttributeError(name)
        array_fields = self.__dict__.get('array_fields')
        if array_fields and name in array_fields:
            return array_fields[name]
        metadata = self.__dict__.get('metadata')
        if metadata and name in metadata:
            return metadata[name]
        raise AttributeError(f"Sample has no field '{name}'")

    def save(self):
        """
        Save changes to this sample back to the database.

        The sample must have been loaded from a dataset (via iteration or query)
        for this to work. Changes to tags, metadata, and location are persisted.

        Deleting a metadata key works too — the update is a MERGE server-side, so removals are
        sent explicitly as ``remove_metadata_keys`` by diffing against the keys this sample was
        loaded with. Without that, ``del sample.metadata["x"]; sample.save()`` returned normally
        and left the key untouched: a silent failed write.

        datetime/date values are converted to ISO-8601 (the same treatment ``add_samples`` gives
        them), instead of raising "Object of type datetime is not JSON serializable" from inside
        requests.

        Example:
            sample = dataset.filter(scene="highway").first()
            sample.tags.append("reviewed")
            sample.metadata["quality_score"] = 0.95
            del sample.metadata["draft_note"]        # removed remotely
            sample.metadata["checked_at"] = datetime.now()
            sample.save()

        Raises:
            ValueError: If sample was not loaded from a dataset
        """
        if self._snapshot_version_id is not None:
            raise ValueError("Snapshot samples are read-only. Load the live sample to edit it.")
        if self._dataset is None:
            raise ValueError(
                "Cannot save: Sample must be loaded from a dataset. "
                "Use dataset.filter() or dataset.all() to get saveable samples.",
            )
        if self.id is None:
            raise ValueError("Cannot save: Sample has no ID. Add it to a dataset first with dataset.add_samples().")

        # Keys the sample had when it was loaded but no longer has -> explicit removals.
        removed = sorted(self._original_metadata_keys - set(self.metadata))

        # `tags` is a full REPLACE on the wire, so it may only be written back when we actually
        # know the sample's tags — i.e. the server sent them, or the caller assigned them. A slim
        # projection (select_fields) omits the column, leaving self.tags == [], and writing that
        # back would silently erase every tag the sample had. Tags are the app's primary curation
        # artifact and there is no history table for them.
        send_tags = "tags" in self._loaded_fields
        if not send_tags and self.tags:
            warnings.warn(
                f"Sample {self.id}: tags were modified but not saved — this sample was loaded "
                "without its tags (select_fields/slim), so writing them back would replace the "
                "real tags with only the ones set here. Re-load the sample including 'tags' "
                "(or use dataset.tag_samples) to change tags.",
                stacklevel=2,
            )

        # Use client to update the sample
        self._dataset.client.update_sample(
            self._dataset._id,
            self.id,
            tags=self.tags if send_tags else None,
            metadata=_jsonable(self.metadata),
            remove_metadata_keys=removed or None,
            location=self.location,
            heading=self.heading,
            array_fields=self.array_fields if self.array_fields else None,
        )
        # The server state now matches ours; re-baseline so a second save() doesn't try to remove
        # keys that are already gone.
        self._original_metadata_keys = set(self.metadata)

    def add_heatmap(
        self,
        name: str,
        map_path: str,
        range: "tuple | list",
        colormap: Optional[str] = None,
    ) -> None:
        """Attach a heatmap overlay to this sample.

        Writes ``sample.metadata[name]`` as the dict shape the Freedom Ridge
        frontend (canvas renderer) and backend composite both recognize::

            {"type": "heatmap", "map_path": "...", "range": [min, max], "colormap": "viridis"}

        The frontend looks up this shape by the ``type: "heatmap"`` marker and
        renders with the given colormap. The backend's ``?overlay=<name>`` route
        reads the same dict when pre-compositing grid thumbnails. ``range`` tells
        both paths how to interpret pixel values: pixel 0 is always transparent
        (no-overlay sentinel), values in ``[range[0], range[1]]`` map to colormap
        indices, values outside clamp to the endpoints.

        Call ``sample.save()`` afterwards to persist. For bulk ingestion prefer
        populating ``metadata`` up front and calling ``dataset.add_samples(...)``.

        Args:
            name: Metadata key under which to store the heatmap (e.g. a class
                name or ``"probability"`` — any unique identifier for this overlay).
            map_path: ``s3://bucket/key`` or absolute local path to the PNG.
            range: ``(start, end)`` pixel-value range for colormap lookup.
                **Order is meaningful** — ``(100, 0)`` inverts the colormap so
                high pixel values render as dark / low values render as bright.
            colormap: Optional colormap name (``viridis``, ``magma``, ``plasma``,
                ``inferno``, ``turbo``, ``gray``). Frontend falls back to viridis
                if omitted or unknown.

        Raises:
            ValueError: if ``name``, ``map_path`` are empty, ``range`` isn't 2
                values, or the range is degenerate (``range[0] == range[1]``).
        """
        if not name or not isinstance(name, str):
            raise ValueError("name must be a non-empty string")
        if not map_path or not isinstance(map_path, str):
            raise ValueError("map_path must be a non-empty string")
        if len(range) != 2:
            raise ValueError("range must be (start, end)")
        start, end = float(range[0]), float(range[1])
        if start == end:
            # A zero-width range can't be meaningfully mapped to a colormap —
            # renderers fall back to a span of 1 and essentially everything
            # ends up transparent. Catch it at the SDK boundary so callers
            # see the problem when they write instead of when they view.
            # (Reversed ranges where start > end are legal — they invert
            # the colormap.)
            raise ValueError("range start and end must differ")

        entry: Dict[str, Any] = {
            "type": "heatmap",
            "map_path": map_path,
            "range": [start, end],
        }
        if colormap:
            entry["colormap"] = colormap
        self.metadata[name] = entry

    def to_dict(self) -> Dict[str, Any]:
        """Convert sample to dictionary for API submission."""
        result = {
            "filepath": self.filepath,
            "media_type": self.media_type,
            "tags": self.tags,
            "metadata": _jsonable(self.metadata),
            "location": None,
            "heading": self.heading,
            "embeddings": self.embeddings if self.embeddings else None,
            "hashes": self.hashes if self.hashes else None,
        }
        if self.array_fields:
            result["array_fields"] = self.array_fields

        # Convert location tuple to GeoJSON if present
        if self.location is not None:
            if isinstance(self.location, (tuple, list)) and len(self.location) == 2:
                lon, lat = self.location
                result["location"] = {"type": "Point", "coordinates": [lon, lat]}
            elif isinstance(self.location, dict):
                result["location"] = self.location
            else:
                raise ValueError(f"Sample {self.filepath!r}: location must be a (lon, lat) pair or a GeoJSON Point")

        return result

    def __repr__(self):
        """String representation of the sample."""
        fields = {
            'id': self.id,
            'media_type': self.media_type,
            'filepath': self.filepath,
            'tags': self.tags,
            'metadata': self.metadata,
            'created_at': self.created_at,
            'last_modified_at': self.last_modified_at,
        }
        fields_str = ',\n    '.join(f"'{k}': {repr(v)}" for k, v in fields.items())
        return f"<Sample: {{\n    {fields_str}\n}}>"
