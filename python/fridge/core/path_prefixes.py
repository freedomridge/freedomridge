"""The path rule of ``Dataset.add_samples_from(..., path_prefixes={old: new})``: the copies get new paths.

A sample's ``filepath`` can't be changed once it is added (it is how the app recognises the sample), so moved
media is handled by copying the samples into a new dataset with new paths. The rule replaces the start of a path,
at a folder boundary: ``/media/old`` matches ``/media/old`` and ``/media/old/a.jpg``, never ``/media/older/a.jpg``.
The longest ``old`` that fits wins, and the rest of the path is kept exactly. ``new`` may be a local folder, a path
relative to the server's data root (``""`` is the data root itself) or a cloud location (``s3://bucket/prefix``).

A copy must never merge two samples silently: two source samples given the same path would become one in the
target (the second one is skipped as "already copied"). :func:`check_collisions` finds that before anything is
written; see its docstring for how, and what it costs.
"""

from typing import Any, Dict, Iterator, List, Optional, Tuple

from .view import DatasetView, F

_OUTSIDE = object()  # _tail: the path isn't under the prefix


def _normalize(prefix: str) -> str:
    """The prefix without trailing slashes, except a root keeps its one: ``/`` and ``s3://`` (a scheme's root)."""
    scheme, found, rest = prefix.partition('://')
    if found:
        return f'{scheme}://{rest.rstrip("/")}'
    stripped = prefix.rstrip('/')
    return stripped if stripped or not prefix else '/'


def _separator(prefix: str) -> str:
    """What comes between a prefix and the rest of a path under it: nothing after a root, ``/`` otherwise."""
    return '' if prefix.endswith('/') else '/'


def _tail(path: str, prefix: str) -> Any:
    """The rest of ``path`` under ``prefix`` (without the separating ``/``), None when it IS the prefix, or _OUTSIDE.
    The empty prefix, the data root of relative paths, holds every path: ``join('', tail)`` is ``tail``."""
    if prefix == '':
        return path if path else None
    if path == prefix and not prefix.endswith('/'):
        return None
    start = prefix + _separator(prefix)
    return path[len(start) :] if path.startswith(start) else _OUTSIDE


def _join(prefix: str, tail: Optional[str]) -> str:
    if tail is None:
        return prefix
    return tail if prefix == '' else prefix + _separator(prefix) + tail


class PathRule:
    """A validated ``{old: new}`` rule. An empty rule is no rule: build one with :meth:`parse`."""

    def __init__(self, rules: List[Tuple[str, str]]):
        # Longest `old` first: the first that fits is the longest.
        self.rules = sorted(rules, key=lambda rule: len(rule[0]), reverse=True)

    @classmethod
    def parse(cls, path_prefixes: Any) -> Optional['PathRule']:
        """The rule, or None for no rule (None or ``{}``). A bad rule raises ValueError before anything is read."""
        if path_prefixes is None:
            return None
        if not isinstance(path_prefixes, dict):
            raise ValueError(
                f"path_prefixes must be a dict from the start of a path to what replaces it, not "
                f"{type(path_prefixes).__name__}",
            )
        rules: Dict[str, Tuple[str, str]] = {}
        for old, new in path_prefixes.items():
            if not isinstance(old, str) or not isinstance(new, str):
                raise ValueError(f"path_prefixes maps text to text: {old!r}: {new!r} isn't")
            if old == '':
                raise ValueError("path_prefixes can't replace an empty start: name the folder the paths start with")
            key = _normalize(old)
            if key in rules:
                raise ValueError(
                    f"path_prefixes names the same path twice ({rules[key][0]!r} and {old!r}; a trailing / makes "
                    "no difference)",
                )
            rules[key] = (old, _normalize(new))
        if not rules:
            return None
        return cls([(old, new) for old, (_, new) in rules.items()])

    def _match(self, path: str) -> Optional[Tuple[str, str, Optional[str]]]:
        for old, new in self.rules:
            tail = _tail(path, old)
            if tail is not _OUTSIDE:
                return old, new, tail
        return None

    def apply(self, path: str) -> str:
        """The path a sample at ``path`` is copied to."""
        found = self._match(path)
        return path if found is None else _join(found[1], found[2])

    def apply_metadata(self, value: Any) -> Any:
        """Metadata with every text value that starts with an ``old`` replaced, at any depth. Keys are kept."""
        if isinstance(value, str):
            return self.apply(value)
        if isinstance(value, dict):
            return {key: self.apply_metadata(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.apply_metadata(item) for item in value]
        return value

    def preimages(self, path: str) -> List[str]:
        """Every path the rule sends to ``path``: the path itself when the rule leaves it alone, and for each rule
        whose ``new`` holds it, the path under that rule's ``old``."""
        found = [path] if self._match(path) is None else []
        for old, new in self.rules:
            tail = _tail(path, new)
            if tail is _OUTSIDE:
                continue
            candidate = _join(old, tail)
            if candidate not in found and self.apply(candidate) == path:
                found.append(candidate)
        return found

    def _probe_prefixes(self) -> Tuple[List[str], List[str]]:
        """The prefixes whose paths can take part in a collision (see check_collisions): every ``new``, and the
        ``old`` of one rule of each pair whose ``new`` folders overlap. Returned as (news, olds)."""
        news = sorted({new for _, new in self.rules})
        olds: List[str] = []
        covered: set = set()
        for i, (old_a, new_a) in enumerate(self.rules):
            for old_b, new_b in self.rules[i + 1 :]:
                overlap = new_a == new_b or _tail(new_a, new_b) is not _OUTSIDE or _tail(new_b, new_a) is not _OUTSIDE
                if overlap and old_a not in covered and old_b not in covered:
                    covered.add(old_a)
                    olds.append(old_a)
        return news, olds


def _in_id_order(view):
    """The same samples paged by id: a DatasetView without its sort (a version's view pages by id already)."""
    clone = view._clone()
    if isinstance(clone, DatasetView):
        clone._sort_by = None
    return clone


def _paths(view: DatasetView, batch_size: int) -> Iterator[str]:
    for sample in view.select_fields(['id', 'filepath']).iter_samples(batch_size=batch_size):
        yield sample.filepath


def _refuse(first: str, second: str, path: str) -> None:
    raise ValueError(
        f"path_prefixes would give two samples of the source the same path, so nothing was copied: {first!r} and "
        f"{second!r} would both become {path!r}. Each copy must keep a path of its own: change the rule (a longer "
        "start, or a different new folder for one of them).",
    )


def _refuse_empty(path: str) -> None:
    raise ValueError(
        f"path_prefixes would give {path!r} an empty path, so nothing was copied: a path can't be the folder that "
        "is replaced by the data root",
    )


def check_collisions(rule: PathRule, view, batch_size: int, listed: Optional[List[str]] = None) -> None:
    """Raise ValueError, naming an example, when the rule would give two samples of ``view`` the same path.

    Reads the source only. Two different paths can end up equal only when at least one of them is replaced and
    its new path lies under some ``new``; the other is either a path left alone that is already there, under
    that ``new``, or a path replaced by another rule whose ``new`` overlaps (one rule maps different paths to
    different paths). So only two kinds of source path can take part:

    - paths under some ``new``;
    - paths under the ``old`` of one rule of each pair whose ``new`` folders overlap (merging folders).

    Those are found with the server's ``contains`` filter (a superset: it isn't anchored and ignores case), kept
    when they really start with the folder, and for each one, every other path the rule would send to the same
    place is computed and looked up exactly (``filepath in [...]``, served by the unique index). Memory stays one
    page. The usual move, to a folder the source has nothing in, is one filtered scan on the server that sends
    nothing back. The worst case (a relative or root ``new``, which ``contains`` can't narrow, or merged folders)
    sends every path once, with one indexed lookup per page.

    A source that filters would change (cut with skip()/limit(), a similarity result, a cut version's view) comes
    with its paths already read (``listed``, copy_objects.listing): they are compared in memory. A plain view, and
    a version's view that isn't cut (whose filters query the version's rows), are probed with filters.
    """
    if listed is not None:
        seen: Dict[str, str] = {}
        for path in listed:
            new_path = rule.apply(path)
            if new_path == '':
                _refuse_empty(path)
            if seen.get(new_path, path) != path:
                _refuse(seen[new_path], path, new_path)
            seen[new_path] = path
        return

    base = _in_id_order(view)
    news, olds = rule._probe_prefixes()
    probes = sorted(set(news + olds))
    if '' not in probes:
        condition = F('filepath').contains(probes[0])
        for prefix in probes[1:]:
            condition = condition | F('filepath').contains(prefix)
        base = base.filter(condition)

    def involved(path: str) -> bool:
        return any(_tail(path, prefix) is not _OUTSIDE for prefix in probes)

    pending: Dict[str, List[Tuple[str, str]]] = {}  # other path -> [(path, new path)]

    def look_up() -> None:
        if not pending:
            return
        names = sorted(pending)
        for start in range(0, len(names), batch_size):
            found = _in_id_order(view).filter(F('filepath').is_in(names[start : start + batch_size]))
            for other in _paths(found, batch_size):
                path, new_path = pending[other][0]
                _refuse(path, other, new_path)
        pending.clear()

    for path in _paths(base, batch_size):
        if not involved(path):
            continue
        new_path = rule.apply(path)
        if new_path == '':
            _refuse_empty(path)
        for other in rule.preimages(new_path):
            if other != path:
                pending.setdefault(other, []).append((path, new_path))
        if len(pending) >= batch_size:
            look_up()
    look_up()
