"""
DatasetView and query building for the Freedom Ridge SDK.

This module provides a Pythonic API for filtering,
sorting, and iterating over dataset samples.

Two query syntaxes are supported:

1. Direct Attribute Access:
   s = dataset.samples
   view = s[(s.brightness > 0.5) & (s.scene == "highway")]

2. SQL-like String:
   view = dataset.query("brightness > 0.5 AND scene = 'highway'")
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, cast

from .client import _raise_for_status

if TYPE_CHECKING:
    from .dataset import Dataset  # noqa: F401 — quoted forward refs only
    from .sample import Sample  # noqa: F401


# Fields the server can project in slim mode as-is (backend/routers/datasets.py's slim branch).
# Anything else that isn't a dynamic array column is a metadata key and must be asked for as
# `metadata.<name>`.
_SLIM_NATIVE_FIELDS = frozenset(
    {"id", "filepath", "media_type", "tags", "created_at", "last_modified_at", "metadata"},
)
# slim can't project these at all, so selecting one falls back to a full row rather than
# silently returning it as None.
_SLIM_UNSUPPORTED_FIELDS = frozenset({"location", "heading"})


class Condition:
    """
    Represents a filter condition that can be combined with AND/OR/NOT.

    This is the result of comparisons like `s.brightness > 0.5`.
    """

    def __init__(self, field: str, operator: str, value: Any):
        self.field = field
        self.operator = operator
        self.value = value
        self._logic: Optional[str] = None
        self._conditions: List[Condition] = []

    def to_dict(self) -> Dict[str, Any]:
        """Convert to Freedom Ridge filter dict format."""
        if self._logic:
            # Compound condition (AND/OR/NOT)
            return {"logic": self._logic, "filters": [c.to_dict() for c in self._conditions]}
        else:
            # Simple condition
            return {"field": self.field, "operator": self.operator, "value": self.value}

    def __and__(self, other: Condition) -> Condition:
        """Combine with AND: (a > 5) & (b < 10)"""
        result = Condition("", "", None)
        result._logic = "AND"

        # Flatten nested ANDs
        left_conditions = self._conditions if self._logic == "AND" else [self]
        right_conditions = other._conditions if other._logic == "AND" else [other]
        result._conditions = left_conditions + right_conditions

        return result

    def __or__(self, other: Condition) -> Condition:
        """Combine with OR: (a == 'x') | (a == 'y')"""
        result = Condition("", "", None)
        result._logic = "OR"

        # Flatten nested ORs
        left_conditions = self._conditions if self._logic == "OR" else [self]
        right_conditions = other._conditions if other._logic == "OR" else [other]
        result._conditions = left_conditions + right_conditions

        return result

    def __invert__(self) -> Condition:
        """Negate with NOT: ~(a == 'x')"""
        result = Condition("", "", None)
        result._logic = "NOT"
        result._conditions = [self]
        return result

    def __repr__(self) -> str:
        if self._logic:
            conds = ", ".join(repr(c) for c in self._conditions)
            return f"Condition({self._logic}: [{conds}])"
        return f"Condition({self.field} {self.operator} {self.value!r})"


class FieldExpression:
    """
    Represents a field reference for building query expressions.

    Supports comparison operators that return Condition objects:
    - field > value
    - field >= value
    - field < value
    - field <= value
    - field == value
    - field != value

    Also supports methods:
    - field.is_in([...])
    - field.contains("...")
    - field.exists()
    """

    def __init__(self, field_name: str):
        self.field_name = field_name

    def __gt__(self, value: Any) -> Condition:
        return Condition(self.field_name, ">", value)

    def __ge__(self, value: Any) -> Condition:
        return Condition(self.field_name, ">=", value)

    def __lt__(self, value: Any) -> Condition:
        return Condition(self.field_name, "<", value)

    def __le__(self, value: Any) -> Condition:
        return Condition(self.field_name, "<=", value)

    def __eq__(self, value: Any) -> Condition:  # type: ignore[override]  # DSL: comparison builds a Condition, not bool
        return Condition(self.field_name, "=", value)

    def __ne__(self, value: Any) -> Condition:  # type: ignore[override]  # DSL: comparison builds a Condition, not bool
        return Condition(self.field_name, "!=", value)

    def is_in(self, values: List[Any]) -> Condition:
        """Check if field value is in a list: field.is_in(['a', 'b'])"""
        return Condition(self.field_name, "in", values)

    def not_in(self, values: List[Any]) -> Condition:
        """Check if field value is not in a list: field.not_in(['a', 'b'])"""
        return Condition(self.field_name, "not in", values)

    def contains(self, value: str) -> Condition:
        """Check if field contains substring: field.contains('foo')"""
        return Condition(self.field_name, "contains", value)

    def exists(self) -> Condition:
        """Check if the field is present in a sample's metadata.

        Emits the backend's JSONB key-existence operator (`metadata ? field`).
        (It used to emit `!= None`, which broke once `!=` became NULL-aware and
        started matching missing-key rows too — i.e. every sample.)
        """
        return Condition(self.field_name, "exists", None)

    def __repr__(self) -> str:
        return f"FieldExpression({self.field_name!r})"


# Short alias for building per-object element conditions: F("confidence") > 0.9
F = FieldExpression


def match_elements(field: str, condition: 'Condition') -> 'Condition':
    """Match samples where ANY element of an array-of-objects field satisfies a condition.

    Lets you combine per-object conditions (which the simple ``field.sub`` filters
    can't), e.g. detections that are BOTH a car AND high-confidence:

        from fridge import match_elements, F
        s = dataset.samples
        view = s[match_elements("detections", (F("label") == "car") & (F("confidence") > 0.9))]

    The inner condition's field names refer to keys WITHIN each array element
    (e.g. "label", "confidence", "bbox").
    """
    return Condition(field, "match_elements", condition.to_dict())


def geo_near(point, max_distance, field: str = "location") -> 'Condition':
    """Match samples within ``max_distance`` metres of ``point`` (``[lon, lat]``).

    ``max_distance`` is required (in metres) so the radius is always an explicit
    choice rather than a hidden default.

        view = dataset.samples[geo_near([-122.4, 37.77], 5000)]  # within 5 km
    """
    return Condition(field, "geo_near", {"point": list(point), "max_distance": max_distance})


def geo_within(polygon, field: str = "location") -> 'Condition':
    """Match samples whose location falls within ``polygon`` (a list of ``[lon, lat]`` points)."""
    return Condition(field, "geo_polygon", [list(p) for p in polygon])


class SampleQuery:
    """
    Query builder that provides attribute access for building conditions.

    Usage:
        s = dataset.samples
        view = s[(s.brightness > 0.5) & (s.scene == "highway")]
    """

    def __init__(self, dataset: 'Dataset'):
        self._dataset = dataset

    def __getattr__(self, name: str) -> FieldExpression:
        """Access a field by name: s.brightness returns FieldExpression('brightness')"""
        if name.startswith('_'):
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
        return FieldExpression(name)

    def __getitem__(self, condition: Condition) -> 'DatasetView':
        """Execute query with condition: s[s.brightness > 0.5]"""
        view = DatasetView(self._dataset)
        view._filters = condition.to_dict()
        return view

    def all(self) -> 'DatasetView':
        """Return a view of all samples (no filter)."""
        return DatasetView(self._dataset)


class DatasetView:
    """
    A filtered, sorted view of a dataset.

    Views are lazy - they don't execute queries until you iterate or call len().
    Views are chainable - each method returns a new view.

    Example:
        view = (dataset.samples
            [(dataset.samples.scene == "highway")]
            .order_by("brightness")
            .limit(100))

        for sample in view:
            print(sample.filepath)
    """

    def __init__(self, dataset: 'Dataset', filters: Optional[Dict] = None):
        self._dataset = dataset
        self._filters: Optional[Dict] = filters
        self._sort_by: Optional[str] = None
        self._sort_order: str = "asc"
        self._limit_val: Optional[int] = None
        self._offset_val: int = 0
        self._selected_fields: Optional[List[str]] = None

        # Cache for results
        self._cached_results: Optional[List] = None
        self._cached_count: Optional[int] = None

    def _clone(self) -> 'DatasetView':
        """Create a copy of this view for chaining."""
        new_view = DatasetView(self._dataset)
        new_view._filters = self._filters.copy() if self._filters else None
        new_view._sort_by = self._sort_by
        new_view._sort_order = self._sort_order
        new_view._limit_val = self._limit_val
        new_view._offset_val = self._offset_val
        new_view._selected_fields = self._selected_fields.copy() if self._selected_fields else None
        return new_view

    def filter(self, condition: Condition) -> 'DatasetView':
        """Add a filter condition. Chainable."""
        new_view = self._clone()

        if new_view._filters:
            # Combine with existing filters using AND
            new_view._filters = {"logic": "AND", "filters": [new_view._filters, condition.to_dict()]}
        else:
            new_view._filters = condition.to_dict()

        return new_view

    def match(self, condition: Condition) -> 'DatasetView':
        """Alias for filter(). Chainable."""
        return self.filter(condition)

    def exclude(self, condition: Condition) -> 'DatasetView':
        """Exclude samples matching condition. Chainable."""
        return self.filter(~condition)

    def order_by(self, field: str, descending: bool = False) -> 'DatasetView':
        """Sort by a field. Chainable."""
        new_view = self._clone()
        new_view._sort_by = field
        new_view._sort_order = "desc" if descending else "asc"
        return new_view

    def sort_by(self, field: str, reverse: bool = False) -> 'DatasetView':
        """Alias for order_by(). Chainable."""
        return self.order_by(field, descending=reverse)

    def limit(self, n: int) -> 'DatasetView':
        """Limit to n samples. Chainable."""
        new_view = self._clone()
        new_view._limit_val = n
        return new_view

    def skip(self, n: int) -> 'DatasetView':
        """Skip first n samples. Chainable."""
        new_view = self._clone()
        new_view._offset_val = n
        return new_view

    def offset(self, n: int) -> 'DatasetView':
        """Alias for skip(). Chainable."""
        return self.skip(n)

    def select_fields(self, fields: List[str]) -> 'DatasetView':
        """Select only specific fields for better performance. Chainable."""
        new_view = self._clone()
        new_view._selected_fields = fields
        return new_view

    def take(self, n: int) -> 'DatasetView':
        """Return the first ``n`` samples of this view (equivalent to ``limit(n)``)."""
        return self.limit(n)

    def _execute_query(self, after_id: Optional[int] = None, skip_count: bool = False) -> tuple:
        """Execute the query and return (items, total).

        ``after_id`` switches the server to keyset pagination (``id > after_id``, ordered by id)
        and ``skip_count`` drops the ``SELECT count(*)``. Both matter for iteration: without them
        every page re-counts the whole filtered set and re-scans ``OFFSET`` rows. See iter_samples.
        """
        from .sample import Sample

        client = self._dataset.client

        # Build request
        params: Dict[str, Any] = {"limit": self._limit_val or 100, "offset": self._offset_val}
        if after_id is not None:
            params["after_id"] = after_id
        if skip_count:
            params["skip_count"] = True

        if self._filters:
            params["filters"] = self._filters

        if self._sort_by:
            params["sort_by"] = self._sort_by
            params["sort_order"] = self._sort_order

        if self._selected_fields:
            # `slim` is what makes `fields` mean anything — the endpoint IGNORES `fields` unless
            # slim is set, so select_fields() used to be a no-op on the wire: it still shipped the
            # full metadata JSONB for every row, the exact cost the call exists to avoid.
            #
            # But slim also renames things. A METADATA field must be asked for as
            # `metadata.<name>`; a bare name matches none of the server's branches and is silently
            # dropped, so `values('grp')` would come back all None. And `location`/`heading` aren't
            # projectable in slim mode at all — requesting them would drop them — so those fall
            # back to a full row, which is correct, just not optimised.
            wire, slim_ok = [], True
            for f in self._selected_fields:
                if f in _SLIM_NATIVE_FIELDS or f in Sample._known_array_fields:
                    wire.append(f)
                else:
                    # Not slim-able: `location`/`heading` aren't projectable at all, and a METADATA
                    # field comes back through `metadata->>'x'` — TEXT — so a number arrives as a
                    # string and `sum(view.values('score'))` raises. Correctness beats the
                    # optimisation: fall back to a full row, which is what it did before.
                    slim_ok = False
                    break
            params["fields"] = ",".join(wire if slim_ok else self._selected_fields)
            if slim_ok:
                params["slim"] = True

        # Call the POST endpoint for complex filters
        response = client.session.post(f"{client.base_url}/datasets/{self._dataset._id}/samples/query", json=params)
        _raise_for_status(response)
        data = response.json()

        items = data.get("items", [])
        total = data.get("total", 0)

        # Convert to Sample objects
        samples = []
        for item in items:
            samples.append(Sample.from_row(item, self._dataset))

        return samples, total

    def __iter__(self) -> Iterator:
        """Iterate over samples in the view."""
        return self.iter_samples(progress=False)

    def iter_samples(
        self,
        progress: bool = False,
        autosave: bool = False,
        batch_size: Optional[int] = None,
    ) -> Iterator:
        """
        Iterate over samples in the view with optional progress bar.

        Args:
            progress: Whether to show a progress bar (requires tqdm)
            autosave: Whether to automatically save modified samples
            batch_size: Number of samples per batch (default: the view's limit, else 1000)

        Yields:
            Sample objects

        Example:
            # Basic iteration with progress bar
            for sample in view.iter_samples(progress=True):
                process(sample)

            # Efficient field selection with progress
            for sample in view.select_fields(["id", "filepath"]).iter_samples(progress=True):
                print(sample.filepath)

            # Auto-save modifications
            for sample in view.iter_samples(autosave=True):
                sample.tags.append("processed")
                # No need to call sample.save()
        """
        # Determine batch size - default 1000 for optimal performance
        # (benchmarks show ~2500/sec for batch sizes 500-5000, 1000 is a good balance)
        _batch_size = batch_size or self._limit_val or 1000
        offset = self._offset_val
        total_fetched = 0

        # Keyset (id > after_id) whenever we can, which is the default case. It was OFFSET-only,
        # and _execute_query never sent skip_count, so EVERY page also ran a full
        # `SELECT count(*)` over the filtered set: on a 3M-sample dataset `for s in
        # dataset.all()` meant ~3,000 requests, ~3,000 full counts and ~4.5e9 rows skipped by
        # OFFSET — quadratic, and inherited by ids(), to_list(), values() and io.export().
        # Dataset.iter_samples was already fixed this way; this is the same shape.
        #
        # A requested sort forces the OFFSET path: the server's keyset branch orders by id, so
        # `after_id` would silently discard sort_by.
        use_keyset = not self._sort_by
        after_id: Optional[int] = None
        total = 0
        first_page = True

        pbar = None
        if progress:
            try:
                from tqdm import tqdm
            except ImportError:
                print("Warning: tqdm not installed. Install with `pip install tqdm` for progress bars.")
                progress = False
            # The bar's total now comes from the first page below — this used to run a whole extra
            # _execute_query() (a full page fetch AND a count) purely to read `total`.

        try:
            while True:
                # Clone to not modify this view
                batch_view = self._clone()
                batch_view._limit_val = _batch_size
                # The FIRST keyset page still carries the caller's offset — the server applies
                # OFFSET only when after_id is absent, so `skip(n)` is honoured once and the
                # cursor rides on from there. Zeroing it unconditionally silently ignored
                # skip()/offset(): `all().skip(100).limit(100)` returned page 1 again, so the
                # documented paging recipe (docs/SDK.md) handed back the same rows forever.
                batch_view._offset_val = 0 if (use_keyset and after_id is not None) else offset

                # Count once, on the first page; every later page carries it forward.
                samples, page_total = batch_view._execute_query(
                    after_id=after_id if use_keyset else None,
                    skip_count=not first_page,
                )
                if first_page:
                    total = page_total
                    if progress:
                        bar_total = min(total, self._limit_val) if self._limit_val else total
                        pbar = tqdm(total=bar_total, desc="Iterating samples")
                    first_page = False

                if not samples:
                    break

                for sample in samples:
                    yield sample
                    total_fetched += 1

                    if pbar:
                        pbar.update(1)

                    # Auto-save if enabled
                    if autosave and sample._dataset is not None:
                        sample.save()

                    # Stop if we've hit the limit
                    if self._limit_val and total_fetched >= self._limit_val:
                        return

                if use_keyset:
                    after_id = samples[-1].id
                    # A short page is the end of the data — with skip_count there is no total to
                    # compare against on later pages.
                    if len(samples) < _batch_size:
                        break
                else:
                    offset += len(samples)
                    if offset >= total:
                        break
        finally:
            if pbar:
                pbar.close()

    def _count(self) -> int:
        """Count this view's samples without materializing rows (limit=0 query).

        The server counts the filters; this view's skip()/limit() window is applied here, the
        same way iteration applies it, so ``len(view) == len(view.to_list())``.
        """
        client = self._dataset.client
        payload: Dict[str, Any] = {"limit": 0, "offset": 0}
        if self._filters:
            payload["filters"] = self._filters
        response = client.session.post(
            f"{client.base_url}/datasets/{self._dataset._id}/samples/query",
            json=payload,
        )
        _raise_for_status(response)
        total = max(response.json().get("total", 0) - (self._offset_val or 0), 0)
        return min(total, self._limit_val) if self._limit_val else total

    def __len__(self) -> int:
        """Number of samples in this view: the filter matches, after skip() and limit()."""
        if self._cached_count is not None:
            return self._cached_count

        self._cached_count = self._count()
        return self._cached_count

    def count(self) -> int:
        """Number of samples in this view: the filter matches, after skip() and limit()."""
        return len(self)

    def first(self) -> Optional['Sample']:
        """Get the first sample, or None if empty."""
        for sample in self.limit(1):
            return sample
        return None

    def to_list(self) -> List:
        """Convert view to a list of samples."""
        return list(self)

    def export(self, export_dir, format="coco", label_field="ground_truth"):
        """Export this view's detections to 'coco' or 'yolo' (a filtered subset)."""
        from .io import export

        return export(self, export_dir, format=format, label_field=label_field)

    def values(self, field: str) -> List[Any]:
        """Extract the value of ``field`` for every sample in the view.

        The result is aligned with the view length (and with ``ids()``):
        samples that don't have the field contribute ``None`` rather than being
        silently dropped.
        """
        result = []
        for sample in self.select_fields([field]):
            if field in sample.metadata:
                result.append(sample.metadata[field])
            else:
                # Falls through Sample.__getattr__ (array fields / real columns
                # like filepath); missing fields yield None to stay aligned.
                result.append(getattr(sample, field, None))
        return result

    def _bulk_target(self):
        # Limited/offset/materialized views capture their exact resolved membership.
        if self._limit_val is not None or self._offset_val or isinstance(self, SimilarityResultView):
            return {'sample_ids': self.ids()}
        if (
            self._filters
            and self._filters.get('field') == 'id'
            and self._filters.get('operator') == 'in'
            and not {'filters', 'logic'} & self._filters.keys()
        ):
            values = self._filters.get('value', [])
            return {'sample_ids': values if isinstance(values, list) else [values], 'filters': self._filters}
        return {'filters': self._filters or {}}

    def tag_samples(self, tags, *, wait=True, timeout=1800):
        return self._dataset.client.bulk_tag_samples(
            self._dataset.id,
            add_tags=tags,
            wait=wait,
            timeout=timeout,
            **self._bulk_target(),
        )

    def untag_samples(self, tags, *, wait=True, timeout=1800):
        return self._dataset.client.bulk_tag_samples(
            self._dataset.id,
            remove_tags=tags,
            wait=wait,
            timeout=timeout,
            **self._bulk_target(),
        )

    def set_values(self, field, value, *, wait=True, timeout=1800):
        result = self._dataset.client.bulk_set_metadata(
            self._dataset.id,
            metadata={field: value},
            wait=wait,
            timeout=timeout,
            **self._bulk_target(),
        )
        return result['updated_count'] if wait else result

    def delete_samples(self, *, wait=True, timeout=1800):
        return self._dataset.client.bulk_delete_samples(
            self._dataset.id,
            wait=wait,
            timeout=timeout,
            **self._bulk_target(),
        )

    def ids(self) -> List[int]:
        """Get all sample IDs in this view."""
        # Don't use select_fields as it may cause issues with some backends
        return [sample.id for sample in self]

    # =========================================================================
    # AGGREGATION METHODS
    # =========================================================================

    def aggregate(self, aggregations: List[Dict]) -> List[Dict]:
        """Run one or more aggregations server-side in a single request.

        Each entry is ``{"field": str, "op": str, ...}``. Numeric ops: count,
        sum, mean, std, min, max, bounds, quantiles (+ ``"quantiles": [..]``).
        Categorical ops: count_values, distinct, values (+ optional ``"limit"``;
        uncapped by default). Scoped by this view's filters. Returns the list of
        result dicts, one per aggregation, in order.

        Example:
            view.aggregate([
                {"field": "brightness", "op": "mean"},
                {"field": "brightness", "op": "std"},
                {"field": "scene", "op": "count_values"},
            ])
        """
        return self._dataset.client.aggregate(self._dataset._id, aggregations, filters=self._filters)

    def _aggregate_one(self, field: str, op: str, **extra):
        spec = {"field": field, "op": op}
        spec.update(extra)
        results = self.aggregate([spec])
        return results[0] if results else None

    def count_values(self, field: str, limit: Optional[int] = None) -> Dict[Any, int]:
        """Count occurrences of each DISTINCT value of a field (exact, server-side).

        Returns a dict mapping each distinct value to its count, scoped by the
        view's filters. Uncapped by default; pass ``limit`` for the top-N most
        frequent only. For numeric binning use ``histogram_values()`` instead.

        Example:
            view = dataset.filter(weather="clear")
            scene_counts = view.count_values("scene")
            # {"highway": 1500, "city street": 800, ...}
        """
        spec: Dict[str, Any] = {"field": field, "op": "count_values"}
        if limit is not None:
            spec["limit"] = limit
        res = self.aggregate([spec])
        rows = res[0]["value"] if res else []
        return {r["value"]: r["count"] for r in rows}

    def distinct(self, field: str, limit: Optional[int] = None) -> List[Any]:
        """Get the distinct values of a field (exact, server-side, uncapped by default).

        Args:
            field: Field name.
            limit: Optional cap on the number of values returned.

        Example:
            view = dataset.filter(weather="clear")
            scenes = view.distinct("scene")
            # ["highway", "city street", "residential", ...]
        """
        spec: Dict[str, Any] = {"field": field, "op": "distinct"}
        if limit is not None:
            spec["limit"] = limit
        res = self.aggregate([spec])
        return res[0]["value"] if res else []

    def bounds(self, field: str) -> tuple:
        """Get the exact (min, max) of a numeric field, scoped by the view's filters.

        Example:
            view = dataset.filter(scene="highway")
            min_speed, max_speed = view.bounds("speed")
        """
        res = self._aggregate_one(field, "bounds")
        if not res or not res.get("value"):
            return (None, None)
        lo, hi = res["value"]
        return (lo, hi)

    def histogram_values(self, field: str, bins: int = 10) -> List[Dict]:
        """
        Get histogram buckets for a numeric field.

        Args:
            field: Field name to compute histogram for
            bins: Number of bins (default 10)

        Returns:
            List of dicts with 'range' and 'count' keys

        Example:
            view = dataset.filter(scene="highway")
            hist = view.histogram_values("speed", bins=20)
            # [{"range": "0-5", "count": 100}, {"range": "5-10", "count": 250}, ...]
        """
        client = self._dataset.client

        filters_json = json.dumps(self._filters) if self._filters else None

        params = {"field": field, "bins": bins}
        if filters_json:
            params["filters"] = filters_json

        response = client.session.get(f"{client.base_url}/datasets/{self._dataset._id}/histogram", params=params)
        _raise_for_status(response)
        data = response.json()

        return data.get("buckets", [])

    def sum(self, field: str) -> float:
        """Sum of a numeric field (computed server-side)."""
        res = self._aggregate_one(field, "sum")
        return (res.get("value") if res else None) or 0.0

    def mean(self, field: str) -> float:
        """Mean of a numeric field (computed server-side)."""
        res = self._aggregate_one(field, "mean")
        return (res.get("value") if res else None) or 0.0

    def std(self, field: str) -> float:
        """Sample standard deviation of a numeric field (computed server-side)."""
        res = self._aggregate_one(field, "std")
        return (res.get("value") if res else None) or 0.0

    def quantiles(self, field: str, quantiles: Optional[List[float]] = None) -> Dict[float, Optional[float]]:
        """Compute quantiles of a numeric field (server-side).

        Returns a dict mapping each requested quantile to its value. Defaults to
        quartiles [0.25, 0.5, 0.75].

        Example:
            view.quantiles("brightness", [0.1, 0.5, 0.9])
        """
        qs = quantiles or [0.25, 0.5, 0.75]
        res = self._aggregate_one(field, "quantiles", quantiles=qs)
        vals = res.get("value") if res else None
        if not vals:
            return {q: None for q in qs}
        return dict(zip(qs, vals))

    def schema(self) -> List[Dict]:
        """Return the dataset's field schema (name/type/min/max/values per field)."""
        return self._dataset.client.get_schema(self._dataset._id, filters=self._filters)

    # =========================================================================
    # SIMILARITY SEARCH
    # =========================================================================

    def sort_by_similarity(
        self,
        sample: Optional['Sample'] = None,
        sample_id: Optional[int] = None,
        vector: Optional[list] = None,
        text: Optional[str] = None,
        field_name: str = "clip",
        limit: Optional[int] = None,
    ) -> 'DatasetView':
        """
        Sort view by similarity to a reference sample, vector, or text prompt.

        Args:
            sample: A Sample object to find similar samples to
            sample_id: Alternatively, a sample ID to find similar samples to
            vector: Alternatively, a raw embedding vector
            text: Alternatively, a natural-language prompt (CLIP text encoding —
                server-side when available, else locally via sentence-transformers).
                The field must be CLIP-family.
            field_name: Embedding field to use (default: "clip")
            limit: Max results (default: view's limit or 50)

        Returns:
            New DatasetView sorted by similarity (closest first)

        Example:
            # Find samples similar to a specific sample
            ref_sample = dataset.filter(scene="highway").first()
            similar = dataset.all().sort_by_similarity(ref_sample, limit=20)

            # ...or to a description
            rivers = dataset.all().sort_by_similarity(text="a river through farmland")

            for sample in similar:
                print(f"{sample.id}: distance={sample.distance:.4f}")
        """
        # Determine sample_id
        if sample is not None:
            if hasattr(sample, 'id') and sample.id:
                search_sample_id = sample.id
            else:
                raise ValueError("Sample must have an ID to use for similarity search")
        elif sample_id is not None:
            search_sample_id = sample_id
        elif vector is not None or text is not None:
            search_sample_id = None
        else:
            raise ValueError("Must provide sample, sample_id, vector, or text")

        # Use limit from view if not specified
        result_limit = limit or self._limit_val or 50

        client = self._dataset.client

        # Call similarity search API
        results = client.similarity_search(
            dataset_id=self._dataset._id,
            sample_id=search_sample_id,
            vector=vector,
            text=text,
            field_name=field_name,
            limit=result_limit,
            filters=self._filters,
        )

        # Convert results to Sample objects with distance
        from .sample import Sample

        samples = []
        for item in results:
            samples.append(Sample.from_row(item, self._dataset))

        # Create a new view that yields these samples directly
        # This is a "materialized" view since it has precomputed results
        return SimilarityResultView(samples, self._dataset)

    def __repr__(self) -> str:
        parts = [f"DatasetView({self._dataset.name!r})"]
        if self._filters:
            parts.append(f"  filters={json.dumps(self._filters, indent=2)}")
        if self._sort_by:
            parts.append(f"  sort_by={self._sort_by!r} ({self._sort_order})")
        if self._limit_val:
            parts.append(f"  limit={self._limit_val}")
        if self._offset_val:
            parts.append(f"  offset={self._offset_val}")
        return "\n".join(parts)


def _sample_field_value(sample, field: str):
    """Resolve a field value from a Sample (metadata, array fields, or attribute)."""
    md = getattr(sample, "metadata", None)
    if md and field in md:
        return md[field]
    return getattr(sample, field, None)


def _matches_filter(sample, node) -> bool:
    """Evaluate a Freedom Ridge filter dict against a Sample in-memory."""
    if not node:
        return True
    if "logic" in node:
        subs = node.get("filters", [])
        logic = node["logic"]
        if logic == "AND":
            return all(_matches_filter(sample, s) for s in subs)
        if logic == "OR":
            return any(_matches_filter(sample, s) for s in subs)
        if logic == "NOT":
            return not _matches_filter(sample, subs[0]) if subs else True
        return True

    field = node.get("field")
    op = node.get("operator")
    value = node.get("value")

    # Per-object match: any element of the array field satisfies the inner filter.
    if op in ("match_elements", "elem_match"):
        elements = _sample_field_value(sample, field)
        if not isinstance(elements, (list, tuple)):
            return False
        return any(isinstance(el, dict) and _matches_element(el, value) for el in elements)

    return _compare(_sample_field_value(sample, field), op, value)


def _matches_element(el: dict, node) -> bool:
    """Evaluate a filter node against a single array element (fields = element keys)."""
    if not node:
        return True
    if "logic" in node:
        subs = node.get("filters", [])
        logic = node["logic"]
        if logic == "AND":
            return all(_matches_element(el, s) for s in subs)
        if logic == "OR":
            return any(_matches_element(el, s) for s in subs)
        if logic == "NOT":
            return not _matches_element(el, subs[0]) if subs else True
        return False
    return _compare(el.get(node.get("field")), node.get("operator"), node.get("value"))


def _compare(actual, op, value) -> bool:
    """Evaluate a single comparison. Raises on operators that can't be evaluated
    in-memory, so a similarity view never silently matches everything."""
    if op in ("=", "=="):
        return actual == value
    if op in ("!=", "<>"):
        return actual != value
    if op == "exists":
        return actual is not None
    if op == "in":
        if isinstance(actual, (list, tuple, set)):
            return any(a in value for a in actual)
        return actual in value
    if op == "not in":
        if isinstance(actual, (list, tuple, set)):
            return all(a not in value for a in actual)
        return actual not in value
    if op == "contains":
        if isinstance(actual, (list, tuple, set, str)):
            return value in actual
        return False
    if op in (">", "<", ">=", "<="):
        if actual is None:
            return False
        try:
            return {">": actual > value, "<": actual < value, ">=": actual >= value, "<=": actual <= value}[op]
        except TypeError:
            return False
    raise NotImplementedError(
        f"operator {op!r} is not supported on in-memory similarity results; "
        f"apply it before sort_by_similarity (server-side) instead.",
    )


class SimilarityResultView(DatasetView):
    """A materialized, chainable view of similarity-search results.

    Returned by ``sort_by_similarity()``. Holds a precomputed, distance-ordered
    list of samples (each carrying a ``distance``). Supports the full
    ``DatasetView`` chaining API — filter/match/exclude/order_by/limit/skip/
    to_list/ids/first/count/values and aggregations — evaluated **in-memory**
    over the materialized results, so distances and ordering are preserved
    without re-querying the backend.
    """

    def __init__(self, samples: List[Sample], dataset):
        super().__init__(dataset)
        self._materialized = list(samples)

    def _clone(self) -> 'SimilarityResultView':
        new_view = SimilarityResultView(self._materialized, self._dataset)
        new_view._filters = self._filters.copy() if self._filters else None
        new_view._sort_by = self._sort_by
        new_view._sort_order = self._sort_order
        new_view._limit_val = self._limit_val
        new_view._offset_val = self._offset_val
        new_view._selected_fields = list(self._selected_fields) if self._selected_fields else None
        return new_view

    def _filtered(self) -> List[Sample]:
        """Materialized samples after in-memory filters (no offset/limit/sort)."""
        if not self._filters:
            return list(self._materialized)
        return [s for s in self._materialized if _matches_filter(s, self._filters)]

    def _resolve(self) -> List[Sample]:
        """Filtered + sorted + sliced view of the materialized samples."""
        items = self._filtered()
        if self._sort_by:
            field = self._sort_by
            reverse = self._sort_order == "desc"
            try:
                items = sorted(
                    items,
                    key=lambda s: (_sample_field_value(s, field) is None, _sample_field_value(s, field)),
                    reverse=reverse,
                )
            except TypeError:
                # Mixed/unorderable types — fall back to string ordering.
                items = sorted(
                    items,
                    key=lambda s: (_sample_field_value(s, field) is None, str(_sample_field_value(s, field))),
                    reverse=reverse,
                )
        start = self._offset_val or 0
        items = items[start:]
        if self._limit_val is not None:
            items = items[: self._limit_val]
        return items

    # --- data access (all in-memory) ---
    def __iter__(self):
        return iter(self._resolve())

    def iter_samples(self, progress: bool = False, autosave: bool = False, batch_size: Optional[int] = None):
        for s in self._resolve():
            yield s
            if autosave and s._dataset is not None:
                s.save()

    def __len__(self) -> int:
        # The same skip()/limit() window as _resolve(), without sorting.
        total = max(len(self._filtered()) - (self._offset_val or 0), 0)
        return min(total, self._limit_val) if self._limit_val is not None else total

    def count(self) -> int:
        return len(self)

    def first(self) -> Optional[Sample]:
        resolved = self._resolve()
        return resolved[0] if resolved else None

    def to_list(self) -> List[Sample]:
        return self._resolve()

    def ids(self) -> List[int]:
        # Sample.id is initialized to None and filled in from the backend;
        # materialized similarity results always carry real ids.
        return [cast("int", s.id) for s in self._resolve()]

    def values(self, field: str) -> List[Any]:
        return [_sample_field_value(s, field) for s in self._resolve()]

    # --- aggregations (in-memory over filtered results) ---
    def count_values(self, field: str, limit: Optional[int] = None) -> Dict[Any, int]:
        counts: Dict[Any, int] = {}
        for s in self._filtered():
            v = _sample_field_value(s, field)
            if isinstance(v, (list, tuple, set)):
                for item in v:
                    if item is not None:  # match the server's WHERE v IS NOT NULL
                        counts[item] = counts.get(item, 0) + 1
            elif v is not None:
                counts[v] = counts.get(v, 0) + 1
        if limit is not None:
            top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
            return dict(top)
        return counts

    def distinct(self, field: str, limit: Optional[int] = None) -> List[Any]:
        keys = list(self.count_values(field).keys())
        return keys[:limit] if limit is not None else keys

    def _numeric_values(self, field: str) -> List[float]:
        out = []
        for s in self._filtered():
            v = _sample_field_value(s, field)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(float(v))
        return out

    def bounds(self, field: str) -> tuple:
        vals = self._numeric_values(field)
        return (min(vals), max(vals)) if vals else (None, None)

    def sum(self, field: str) -> float:
        return float(sum(self._numeric_values(field)))

    def mean(self, field: str) -> float:
        vals = self._numeric_values(field)
        return sum(vals) / len(vals) if vals else 0.0

    def std(self, field: str) -> float:
        vals = self._numeric_values(field)
        if len(vals) < 2:
            return 0.0
        m = sum(vals) / len(vals)
        return (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5

    def quantiles(self, field: str, quantiles: Optional[List[float]] = None) -> Dict[float, Optional[float]]:
        qs = quantiles or [0.25, 0.5, 0.75]
        vals = sorted(self._numeric_values(field))
        if not vals:
            return {q: None for q in qs}

        def q_at(q):
            if len(vals) == 1:
                return vals[0]
            pos = q * (len(vals) - 1)
            lo = int(pos)
            hi = min(lo + 1, len(vals) - 1)
            frac = pos - lo
            return vals[lo] * (1 - frac) + vals[hi] * frac

        return {q: q_at(q) for q in qs}

    def aggregate(self, aggregations: List[Dict]) -> List[Dict]:
        raise NotImplementedError(
            "aggregate() is not supported on similarity results; use the "
            "individual aggregations (count_values/distinct/bounds/sum/mean/"
            "std/quantiles), which compute in-memory over the results.",
        )

    def schema(self):
        # The base schema() hits the backend dataset schema, which would ignore
        # the materialized result set — refer the caller to the dataset instead.
        raise NotImplementedError(
            "schema() is not meaningful on similarity results; call it on the dataset/view (dataset.all().schema()).",
        )

    def sort_by_similarity(self, *args, **kwargs):
        raise NotImplementedError(
            "Re-running similarity on an already-materialized similarity result "
            "is not supported; start from dataset.all() (or a filtered view).",
        )

    def histogram_values(self, field: str, bins: int = 10) -> List[Dict]:
        vals = self._numeric_values(field)
        if not vals:
            return []
        lo, hi = min(vals), max(vals)
        if lo == hi:
            return [{"range": f"{lo:.2f} - {hi:.2f}", "count": len(vals)}]
        width = (hi - lo) / bins
        buckets = [0] * bins
        for v in vals:
            idx = min(int((v - lo) / width), bins - 1)
            buckets[idx] += 1
        return [
            {"range": f"{lo + i * width:.2f} - {lo + (i + 1) * width:.2f}", "count": c} for i, c in enumerate(buckets)
        ]

    def __repr__(self):
        return f"SimilarityResultView({len(self._filtered())} samples)"


def parse_query_string(query: str) -> Dict[str, Any]:
    """
    Parse a SQL-like query string into Freedom Ridge filter dict format.

    Supports:
    - Comparisons: field > 5, field = 'value', field != 'x'
    - AND/OR: field > 5 AND other = 'x'
    - IN: field IN ('a', 'b', 'c')
    - BETWEEN: field BETWEEN 0.3 AND 0.8
    - LIKE/CONTAINS: field LIKE '%pattern%'
    - NOT: NOT field = 'x'
    - Parentheses for grouping

    Example:
        parse_query_string("brightness > 0.5 AND scene = 'highway'")
    """
    query = query.strip()

    if not query:
        return {}

    # Tokenize
    tokens = _tokenize_query(query)

    # Parse into AST
    ast = _parse_expression(tokens, 0)[0]

    return ast


def _tokenize_query(query: str) -> List[str]:
    """Tokenize a query string."""
    # Pattern matches: strings, numbers, operators, words, parentheses
    pattern = r"""
        '([^']*)'|           # Single-quoted string
        "([^"]*)"|           # Double-quoted string
        (\d+\.?\d*)|         # Number
        (>=|<=|!=|<>|=|>|<)| # Comparison operators
        (\(|\))|             # Parentheses
        (\w+)|               # Words (AND, OR, field names, etc.)
        (,)                  # Comma
    """

    tokens = []
    for match in re.finditer(pattern, query, re.VERBOSE | re.IGNORECASE):
        groups = match.groups()
        # Find which group matched
        for g in groups:
            if g is not None:
                tokens.append(g)
                break

    return tokens


def _parse_expression(tokens: List[str], pos: int) -> tuple:
    """Parse an expression, handling AND/OR."""
    left, pos = _parse_term(tokens, pos)

    while pos < len(tokens):
        token = tokens[pos].upper()

        if token == "AND":
            right, pos = _parse_term(tokens, pos + 1)
            left = {"logic": "AND", "filters": [left, right]}
        elif token == "OR":
            right, pos = _parse_term(tokens, pos + 1)
            left = {"logic": "OR", "filters": [left, right]}
        else:
            break

    return left, pos


def _parse_term(tokens: List[str], pos: int) -> tuple:
    """Parse a single term (condition or parenthesized expression)."""
    if pos >= len(tokens):
        return {}, pos

    token = tokens[pos]

    # Handle NOT
    if token.upper() == "NOT":
        inner, pos = _parse_term(tokens, pos + 1)
        return {"logic": "NOT", "filters": [inner]}, pos

    # Handle parentheses
    if token == "(":
        inner, pos = _parse_expression(tokens, pos + 1)
        if pos < len(tokens) and tokens[pos] == ")":
            pos += 1
        return inner, pos

    # Must be a field comparison
    return _parse_comparison(tokens, pos)


def _parse_comparison(tokens: List[str], pos: int) -> tuple:
    """Parse a comparison like 'field > 5' or 'field IN (...)'."""
    if pos >= len(tokens):
        return {}, pos

    field = tokens[pos]
    pos += 1

    if pos >= len(tokens):
        return {"field": field, "operator": "exists", "value": True}, pos

    op_token = tokens[pos].upper()
    pos += 1

    # Handle IN
    if op_token == "IN":
        values, pos = _parse_in_list(tokens, pos)
        return {"field": field, "operator": "in", "value": values}, pos

    # Handle BETWEEN
    if op_token == "BETWEEN":
        min_val = _parse_value(tokens[pos])
        pos += 1
        if pos < len(tokens) and tokens[pos].upper() == "AND":
            pos += 1
        max_val = _parse_value(tokens[pos])
        pos += 1
        return {
            "logic": "AND",
            "filters": [
                {"field": field, "operator": ">=", "value": min_val},
                {"field": field, "operator": "<=", "value": max_val},
            ],
        }, pos

    # Handle LIKE / CONTAINS
    if op_token in ("LIKE", "CONTAINS"):
        value = _parse_value(tokens[pos])
        pos += 1
        # Strip % wildcards for contains
        if isinstance(value, str):
            value = value.strip("%")
        return {"field": field, "operator": "contains", "value": value}, pos

    # Handle IS NULL / IS NOT NULL: the same unary operators filter(x__is_null=...) sends, so a
    # missing key counts as null. (`= None` / `!= None` matched no samples / every sample.)
    if op_token == "IS":
        next_token = tokens[pos].upper() if pos < len(tokens) else ""
        if next_token == "NULL":
            pos += 1
            return {"field": field, "operator": "is_null", "value": None}, pos
        elif next_token == "NOT":
            pos += 1
            if pos < len(tokens) and tokens[pos].upper() == "NULL":
                pos += 1
            return {"field": field, "operator": "is_not_null", "value": None}, pos

    # Standard comparison
    op_map = {"=": "=", "==": "=", "!=": "!=", "<>": "!=", ">": ">", "<": "<", ">=": ">=", "<=": "<="}

    operator = op_map.get(op_token, "=")

    if pos >= len(tokens):
        return {"field": field, "operator": operator, "value": None}, pos

    value = _parse_value(tokens[pos])
    pos += 1

    return {"field": field, "operator": operator, "value": value}, pos


def _parse_in_list(tokens: List[str], pos: int) -> tuple:
    """Parse an IN list like ('a', 'b', 'c')."""
    values = []

    if pos < len(tokens) and tokens[pos] == "(":
        pos += 1

    while pos < len(tokens):
        token = tokens[pos]

        if token == ")":
            pos += 1
            break
        elif token == ",":
            pos += 1
            continue
        else:
            values.append(_parse_value(token))
            pos += 1

    return values, pos


def _parse_value(token: str) -> Any:
    """Parse a value token into appropriate Python type."""
    # Check for numeric
    try:
        if "." in token:
            return float(token)
        return int(token)
    except ValueError:
        pass

    # Boolean
    if token.upper() == "TRUE":
        return True
    if token.upper() == "FALSE":
        return False
    if token.upper() == "NULL":
        return None

    # String (already unquoted by tokenizer)
    return token
