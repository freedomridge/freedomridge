"""A read-only, queryable view of a dataset version (snapshot) for the Freedom Ridge SDK."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional

from .sample import Sample

if TYPE_CHECKING:
    from .dataset import Dataset
    from .view import Condition


class VersionReadOnlyError(ValueError):
    """Raised when a write is attempted on a version: snapshots never change."""


_WRITE_METHODS = frozenset({'tag_samples', 'untag_samples', 'set_values', 'delete_samples', 'save'})
_UNSUPPORTED_METHODS = frozenset(
    {'order_by', 'sort_by', 'skip', 'offset', 'histogram_values', 'schema', 'export', 'sort_by_similarity'},
)


class VersionView:
    """The samples of one version, queried like a ``DatasetView`` but always against the stored snapshot.

    Every query runs in the database against the version's stored rows, so counting or aggregating a
    multi-million-sample version never pulls samples to the client. Views are lazy and chainable.

    Example:
        v = dataset.version("v1.8")
        v.count()
        v.distinct("source_id")
        v.count_values("split")
        train = v.filter(F("split").is_in(["train", "val"])).select_fields(["source_id", "category"])
        for sample in train:
            print(sample.metadata["source_id"])
    """

    def __init__(
        self,
        dataset: 'Dataset',
        version: Dict[str, Any],
        filters: Optional[Dict] = None,
        selected_fields: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ):
        self._dataset = dataset
        self._version = version
        self._filters = filters
        self._selected_fields = selected_fields
        self._limit_val = limit

    @property
    def version_id(self) -> int:
        return self._version['id']

    @property
    def name(self) -> str:
        return self._version['name']

    def _clone(self, **changes: Any) -> 'VersionView':
        new = copy.copy(self)
        for name, value in changes.items():
            setattr(new, name, value)
        return new

    def __getattr__(self, name: str):
        if name in _WRITE_METHODS:
            raise VersionReadOnlyError(f"Version {self._version['name']!r} is read-only: {name}() is not available")
        if name in _UNSUPPORTED_METHODS:
            raise NotImplementedError(f"{name}() is not supported on a version view")
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    # --- Building the query ---

    def filter(self, condition: 'Condition') -> 'VersionView':
        """Keep samples matching ``condition`` (same filter DSL as DatasetView). Chainable."""
        new = condition.to_dict()
        return self._clone(_filters={"logic": "AND", "filters": [self._filters, new]} if self._filters else new)

    def match(self, condition: 'Condition') -> 'VersionView':
        """Alias for filter()."""
        return self.filter(condition)

    def exclude(self, condition: 'Condition') -> 'VersionView':
        """Drop samples matching ``condition``. Chainable."""
        return self.filter(~condition)

    def select_fields(self, fields: List[str]) -> 'VersionView':
        """Read only these fields (native columns, array fields, or metadata keys); ``id`` always comes."""
        return self._clone(_selected_fields=list(fields))

    def limit(self, n: int) -> 'VersionView':
        """Iterate at most ``n`` samples (in id order). Chainable."""
        return self._clone(_limit_val=n)

    def take(self, n: int) -> 'VersionView':
        """Alias for limit()."""
        return self.limit(n)

    # --- Reading ---

    def iter_samples(self, batch_size: int = 1000) -> Iterator[Sample]:
        """Iterate the matching samples in id order as they were at the version (keyset-paged, read-only)."""
        after_id: Optional[int] = None
        yielded = 0
        while True:
            page = batch_size if self._limit_val is None else min(batch_size, self._limit_val - yielded)
            if page <= 0:
                return
            items = self._dataset.client.query_version_samples(
                self._dataset._id,
                self.version_id,
                filters=self._filters,
                fields=self._selected_fields,
                limit=page,
                after_id=after_id,
            )
            for item in items:
                yield Sample.from_row(item, self._dataset)
            yielded += len(items)
            if len(items) < page:
                return
            after_id = items[-1]['id']

    def __iter__(self) -> Iterator[Sample]:
        return self.iter_samples()

    def first(self) -> Optional[Sample]:
        """The first matching sample by id, or None."""
        for sample in self.limit(1):
            return sample
        return None

    def aggregate(self, aggregations: List[Dict]) -> List[Dict]:
        """Run server-side aggregations over the matching samples; same specs and results as DatasetView."""
        return self._dataset.client.aggregate_version(
            self._dataset._id,
            self.version_id,
            aggregations,
            filters=self._filters,
        )

    def count(self) -> int:
        """Number of matching samples (counted in the database)."""
        total = self.aggregate([{"field": "id", "op": "count"}])[0]["value"]
        return total if self._limit_val is None else min(total, self._limit_val)

    def __len__(self) -> int:
        return self.count()

    def distinct(self, field: str, limit: Optional[int] = None) -> List[Any]:
        """Distinct values of ``field`` among the matching samples (exact, uncapped unless ``limit``)."""
        spec: Dict[str, Any] = {"field": field, "op": "distinct"}
        if limit is not None:
            spec["limit"] = limit
        return self.aggregate([spec])[0]["value"]

    def count_values(self, field: str, limit: Optional[int] = None) -> Dict[Any, int]:
        """Samples per distinct value of ``field``, most frequent first (exact, uncapped unless ``limit``)."""
        spec: Dict[str, Any] = {"field": field, "op": "count_values"}
        if limit is not None:
            spec["limit"] = limit
        return {row["value"]: row["count"] for row in self.aggregate([spec])[0]["value"]}

    def __repr__(self) -> str:
        return f"VersionView({self._dataset.name!r}, version={self._version['name']!r}, filters={self._filters})"
