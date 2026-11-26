"""How the dataset page's two grids read a filter on a dataset with objects (docs/FILTER_QUERY.md, "On a dataset with
objects").

An image and its objects are rows of one table holding different fields: images have `colour`, objects have `label`.
The page wraps its filter B as `cg_self(B) OR has_matching_object(B)` for the images' grid and as `cg_self(B) OR
parent_matches(B)` for the objects' grid (frontend/contexts/FilterContext.tsx withGrain, backend/assistant/grain.py
image_grain). The compiler (routers/datasets.py) hands each such pair here, and B is read for an image together with
its objects, or for an object together with its image. A row is never asked about a field it has no value for:

- A test (a comparison, a place, a lasso, a concept's show, a selection) is met when either of the two rows meets it.
  On the images' grid the two are the image and ONE of its objects (or the image alone), so tests joined by AND are
  met with the same object; on the objects' grid they are the object and its image.
- `NOT (X)` holds when the row as a whole has a value for every field compared inside X and X does not hold for it.
  `!=` and `not in` are the NOT of `=` and `in`; a concept's exclude is the NOT of its show, with the score as the
  value. "As a whole" is the image with all its objects on the images' grid, the object with its image on the
  objects' grid. A NOT inside a larger filter is read first, for the row as a whole.
- `is null` holds when neither the row nor any of those partners has a value (absent or a JSON null), `not exists`
  when none has the key (a JSON null under it counts as there): both read first and for the row as a whole, like a
  NOT. `is not null` and `exists` are showing tests like any other.
- An excluded place or lasso leaves out the row when it or a partner is inside, and keeps the rows with no location
  or no point, as the exclusion standing alone does.
- A hiding test of a field every row has (the built-in columns, a list such as `tags`), and the duplicates' collapse,
  are answered by the grid's own row, as before. A NOT around tests of built-in fields only keeps the reading it had:
  each row on its own (`NOT (id contains ...)`: the grid's row).

Nothing is stored about which kind of row has which field: every rule reads the values. The SQL is built through the
`Rows` the compiler passes (`test` compiles a plain filter node against a row, `keyed` the same for a partner row with
the metadata key's test first; `objects` and `parent` reach a row's objects and an object's image, each as one EXISTS
the planner can run as a hashed set).
"""

from typing import Any, Callable, Optional, Protocol

# A filter node's place in the reading: ('test', node), ('not', unit, fields, concepts), ('absent', what it is the
# absence of: a test, or ('has', field) for a value), ('exclude', node), ('own', node), ('and', units), ('or', units).
Unit = tuple

_NEGATIONS = {'!=': '=', 'not in': 'in'}
# The compiler takes these spellings of a negation on a label path only; elsewhere they are refused, as before.
_LABEL_NEGATIONS = {'<>': '=', 'not_in': 'in', 'nin': 'in'}
_RANGES = {'>', '>=', '<', '<='}
_COMPARISONS = {
    '=',
    '==',
    '!=',
    '<>',
    'in',
    'not in',
    'not_in',
    'nin',
    '<',
    '<=',
    '>',
    '>=',
    'contains',
    'match_elements',
    'elem_match',
}
_EXCLUDES = {
    'geo_polygon_exclude': 'geo_polygon',
    'geo_region_exclude': 'geo_region',
    'embedding_polygon_exclude': 'embedding_polygon',
}
_ABSENT = {'is_null', 'not_exists'}
_PRESENT = {'is_not_null', 'exists'}
_CONCEPTS = {'concept_select', 'concept_exclude'}
# Answered by the grid's own row: the duplicates' collapse (a hiding of row identities, as `id not in` is), the
# unlinked objects' finder, and a wrapping operator found inside a wrapped filter (each reaches across on its own).
_OWN = {'collapse_duplicates', 'unlinked_object', 'cg_self', 'has_matching_object', 'parent_matches'}


class Rows(Protocol):
    """The compiler's side: SQL for a row (an opaque handle the compiler gives and takes back)."""

    def outer(self) -> Any: ...

    def test(self, node: Any, row: Any) -> str: ...

    def has(self, field: str, row: Any, presence: bool = False) -> str: ...

    def keyed(self, node: Any, row: Any) -> str: ...

    def objects(self, row: Any, inner: Callable[[Any], str], plain: Optional[Any] = None) -> str: ...

    def parent(self, row: Any, inner: Callable[[Any], str], plain: Optional[Any] = None) -> str: ...

    def row_field(self, field: Any) -> Optional[str]: ...

    def label_path(self, field: Any) -> bool: ...

    def id_list(self, node: Any) -> Optional[list]: ...


def read(base: Any, grid: str, rows: Rows) -> str:
    """SQL for the grid's row (rows.outer()): `grid` 'image' for has_matching_object's pair, 'object' for
    parent_matches'."""
    reader = _Reader(rows)
    unit = reader.unit(base)
    row = rows.outer()
    return reader.image(unit, row) if grid == 'image' else reader.object(unit, row)


def _and(parts: list[str]) -> str:
    if not parts:
        return 'TRUE'
    return parts[0] if len(parts) == 1 else '(' + ' AND '.join(parts) + ')'


def _or(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else '(' + ' OR '.join(parts) + ')'


def _not(sql: str) -> str:
    """NOT, with a test that is NULL for a row (no value) counted as not holding."""
    return f'NOT COALESCE({sql}, FALSE)'


def _is_group(node: Any) -> bool:
    return isinstance(node, dict) and ('logic' in node or 'filters' in node)


def _operator(node: dict) -> str:
    return str(node.get('operator', '=')).lower()


def _leaves(node: Any) -> list:
    if _is_group(node):
        children = node.get('filters') or []
        return [leaf for child in (children if isinstance(children, list) else []) for leaf in _leaves(child)]
    return [node]


def _depends(unit: Unit) -> bool:
    """Whether the unit's answer depends on which object is paired with the image (it holds a test outside a NOT)."""
    if unit[0] == 'test':
        return True
    if unit[0] in ('and', 'or'):
        return any(map(_depends, unit[1]))
    return False


def _tests(unit: Unit) -> list:
    """The tests a unit holds outside its NOTs and other row-level parts."""
    if unit[0] == 'test':
        return [unit[1]]
    if unit[0] in ('and', 'or'):
        return [test for child in unit[1] for test in _tests(child)]
    return []


class _Reader:
    def __init__(self, rows: Rows):
        self.rows = rows

    # --- What each node of the filter is ---

    def unit(self, node: Any) -> Unit:
        if not isinstance(node, dict):
            return ('test', node)  # the compiler refuses it
        if _is_group(node):
            logic = str(node.get('logic', 'AND')).upper()
            children = node.get('filters', [])
            if not children or not isinstance(children, list) or logic not in ('AND', 'OR', 'NOT'):
                return ('own', node)  # TRUE for an empty group, the compiler's 400 for a malformed one
            if logic == 'NOT':
                return self.negation(node, children)
            if logic == 'OR':
                return ('or', [self.unit(child) for child in children])
            return ('and', self.grouped(children))
        operator, field = _operator(node), node.get('field')
        row_field = self.rows.row_field(field)
        if operator in _NEGATIONS or (operator in _LABEL_NEGATIONS and self.rows.label_path(field)):
            if row_field or not isinstance(field, str):
                return ('own', node)
            positive = {**node, 'operator': _NEGATIONS.get(operator) or _LABEL_NEGATIONS[operator]}
            return ('not', ('test', positive), [field], [])
        if operator == 'concept_exclude':
            value = node.get('value')
            concept = value.get('concept_id') if isinstance(value, dict) else None
            if concept is None:
                return ('own', node)  # no concept: no condition, as the compiler reads it
            return ('not', ('test', {**node, 'operator': 'concept_select'}), [], [concept])
        if operator in _EXCLUDES:
            return ('exclude', node)
        if operator in _ABSENT or operator in _PRESENT:
            if row_field == 'builtin' or not isinstance(field, str):
                return ('own', node)  # every row has its built-in fields
            if operator in _PRESENT:
                return ('test', node)
            # `is null` is the absence of a value, `not exists` of the key (a JSON null under it counts as there).
            return ('absent', ('has', field) if operator == 'is_null' else ('test', {**node, 'operator': 'exists'}))
        if operator in _OWN:
            return ('own', node)
        return ('test', node)

    def grouped(self, children: list) -> list:
        """An AND's parts. Two or more range comparisons of one field (a range's two bounds, written `between`, by a
        slider or a histogram's bar) are one test, met by one row's value, as the compiler takes them as one window."""
        ranges: dict[str, list] = {}
        for child in children:
            if not _is_group(child) and isinstance(child, dict) and _operator(child) in _RANGES:
                if isinstance(child.get('field'), str):
                    ranges.setdefault(child['field'], []).append(child)
        units: list = []
        for child in children:
            field = child.get('field') if isinstance(child, dict) and not _is_group(child) else None
            bounds = ranges.get(field) if isinstance(field, str) and _operator(child) in _RANGES else None
            if bounds is not None and len(bounds) > 1:
                if child is bounds[0]:
                    units.append(('test', {'logic': 'AND', 'filters': bounds}))
                continue
            units.append(self.unit(child))
        return units

    def negation(self, node: dict, children: list) -> Unit:
        inner = {'logic': 'AND', 'filters': children}
        leaves = _leaves(inner)
        kinds = [self.rows.row_field(leaf.get('field')) if isinstance(leaf, dict) else None for leaf in leaves]
        if leaves and all(
            isinstance(leaf, dict) and _operator(leaf) in _COMPARISONS and kind for leaf, kind in zip(leaves, kinds)
        ):
            # Tests of fields every row has, only: as before. A NOT of built-in tests asks each row on its own (the
            # grid's row alone for `id contains`, the sidebar's typed terms); a list's, the grid's row, as `not in`.
            if all(kind == 'builtin' for kind in kinds):
                id_text = all(leaf.get('field') == 'id' and _operator(leaf) == 'contains' for leaf in leaves)
                if id_text and all(self._and_or(child) for child in children):
                    return ('own', node)
                return ('test', node)
            return ('own', node)
        fields: dict[str, None] = {}
        concepts: dict[Any, None] = {}
        for leaf in leaves:
            if not isinstance(leaf, dict):
                continue
            operator, field = _operator(leaf), leaf.get('field')
            if operator in _COMPARISONS and isinstance(field, str) and not self.rows.row_field(field):
                fields[field] = None
            elif operator in _CONCEPTS and isinstance(leaf.get('value'), dict):
                concept = leaf['value'].get('concept_id')
                if concept is not None:
                    concepts[concept] = None
        return ('not', self.unit(inner), list(fields), list(concepts))

    @staticmethod
    def _and_or(node: Any) -> bool:
        """A leaf, or AND / OR groups of leaves only (as the sidebar's several typed terms)."""
        if not _is_group(node):
            return True
        return str(node.get('logic', 'AND')).upper() in ('AND', 'OR') and all(
            _Reader._and_or(child) for child in node.get('filters') or []
        )

    # --- The images' grid: an image, with ONE of its objects (or alone) for the tests, with all of them as a whole ---

    def image(self, unit: Unit, row: Any) -> str:
        rows, kind = self.rows, unit[0]
        if kind == 'and':
            return self.image_and(unit[1], row)
        if kind == 'or':
            return _or([self.image(child, row) for child in unit[1]])
        if kind == 'test':
            test = unit[1]
            cross = rows.objects(row, lambda o: rows.keyed(test, o), test)
            return f'({rows.test(test, row)} OR {cross})'
        if kind == 'not':
            _, inner, fields, concepts = unit
            having = [self.image_has(field, row) for field in fields]
            having += [self.image_scored(concept, row) for concept in concepts]
            return _and([*having, _not(self.image(inner, row))])
        if kind == 'absent':
            return self.image_absent(unit[1], row)
        if kind == 'has':
            return self.image_has(unit[1], row, presence=True)
        if kind == 'exclude':
            return self.excluded(unit[1], row, rows.objects)
        return rows.test(unit[1], row)

    def image_absent(self, unit: Unit, row: Any) -> str:
        """`is null` / `not exists`: neither the image nor any object has the value (or the key). Written `NOT ... AND
        NOT EXISTS (...)`: at the top of a statement the planner runs it as an anti-join, in parallel, where an EXISTS
        in the scan's condition keeps the scan to one process (measured: a count of `colour is null` over a million
        images with four million objects took 0.14 s so, 0.40 s the other way). A negated comparison stays a NOT around
        the test and its EXISTS: as an anti-join its plan rested on estimates of the partners that went wrong."""
        rows = self.rows
        if unit[0] == 'has':
            field = unit[1]
            own, cross = _not(rows.has(field, row, True)), rows.objects(row, lambda o: rows.has(field, o, True))
        else:
            test = unit[1]
            own, cross = _not(rows.test(test, row)), rows.objects(row, lambda o: rows.keyed(test, o))
        return f'({own} AND NOT {cross})'

    def image_has(self, field: str, row: Any, presence: bool = False) -> str:
        rows = self.rows
        return f'({rows.has(field, row, presence)} OR {rows.objects(row, lambda o: rows.has(field, o, presence))})'

    def image_scored(self, concept: Any, row: Any) -> str:
        rows, node = self.rows, _scored(concept)
        return f'({rows.test(node, row)} OR {rows.objects(row, lambda o: rows.test(node, o))})'

    def image_and(self, units: list, row: Any) -> str:
        """The row-level parts each for the image; the tests together, with the same object."""
        parts = [self.image(child, row) for child in units if not _depends(child)]
        paired = [child for child in units if _depends(child)]
        if len(paired) == 1:
            parts.append(self.image(paired[0], row))
        elif paired:
            parts.append(self.image_pair(paired, row))
        return _and(parts)

    def image_pair(self, units: list, row: Any) -> str:
        """Two or more parts with tests, ANDed: met by the image alone, or by the image with one of its objects."""
        rows = self.rows
        listed = next((unit for unit in units if unit[0] == 'test' and rows.id_list(unit[1]) is not None), None)
        if listed is not None:
            # A list of ids (a selection, a search's results) among them: (the image is listed AND the rest holds for
            # it) OR (a listed object of it meets the rest with it), so each side is found through the list.
            test, rest = listed[1], [unit for unit in units if unit is not listed]
            own = f'({rows.test(test, row)} AND {self.image_and(rest, row)})'
            cross = rows.objects(row, lambda o: _and([rows.keyed(test, o), self.paired(('and', rest), o)]), test)
            return f'({own} OR {cross})'
        whole: Unit = ('and', units)
        # An object that meets none of the tests adds nothing to the image alone, so only those that meet one are read:
        # a condition on the object alone, which the planner applies where it reads the objects.
        cross = rows.objects(row, lambda o: _and([self.some(whole, o), self.paired(whole, o)]))
        return f'({self.alone(whole, row)} OR {cross})'

    def some(self, unit: Unit, o: Any) -> str:
        """The object meets one of the unit's tests on its own."""
        return _or([self.rows.keyed(test, o) for test in _tests(unit)])

    def alone(self, unit: Unit, row: Any) -> str:
        """The unit met by the image's own values (its row-level parts read for the image as a whole)."""
        kind = unit[0]
        if kind == 'and':
            return _and([self.alone(child, row) for child in unit[1]])
        if kind == 'or':
            return _or([self.alone(child, row) for child in unit[1]])
        if kind == 'test':
            return self.rows.test(unit[1], row)
        return self.image(unit, row)

    def paired(self, unit: Unit, o: Any) -> str:
        """The unit met by an object `o` together with its image: each test by either, the row-level parts by the
        image as a whole. The image is reached from the object (rows.parent), so the object's EXISTS stays one the
        planner can run as a hashed set."""
        rows, kind = self.rows, unit[0]
        if kind == 'and':
            return _and([self.paired(child, o) for child in unit[1]])
        if kind == 'or':
            return _or([self.paired(child, o) for child in unit[1]])
        if kind == 'test':
            test = unit[1]
            return f'({rows.test(test, o)} OR {rows.parent(o, lambda p: rows.keyed(test, p), test)})'
        return rows.parent(o, lambda p: self.image(unit, p))

    # --- The objects' grid: an object with its image ---

    def object(self, unit: Unit, row: Any) -> str:
        rows, kind = self.rows, unit[0]
        if kind == 'and':
            return _and([self.object(child, row) for child in unit[1]])
        if kind == 'or':
            return _or([self.object(child, row) for child in unit[1]])
        if kind == 'test':
            test = unit[1]
            return f'({rows.test(test, row)} OR {rows.parent(row, lambda p: rows.keyed(test, p), test)})'
        if kind == 'not':
            _, inner, fields, concepts = unit
            having = [self.object_has(field, row) for field in fields]
            having += [self.object_scored(concept, row) for concept in concepts]
            return _and([*having, _not(self.object(inner, row))])
        if kind == 'absent':
            return self.object_absent(unit[1], row)
        if kind == 'has':
            return self.object_has(unit[1], row, presence=True)
        if kind == 'exclude':
            return self.excluded(unit[1], row, rows.parent)
        return rows.test(unit[1], row)

    def object_absent(self, unit: Unit, row: Any) -> str:
        """`is null` / `not exists` for the object with its image (image_absent)."""
        rows = self.rows
        if unit[0] == 'has':
            field = unit[1]
            own, cross = _not(rows.has(field, row, True)), rows.parent(row, lambda p: rows.has(field, p, True))
        else:
            test = unit[1]
            own, cross = _not(rows.test(test, row)), rows.parent(row, lambda p: rows.keyed(test, p))
        return f'({own} AND NOT {cross})'

    def object_has(self, field: str, row: Any, presence: bool = False) -> str:
        rows = self.rows
        return f'({rows.has(field, row, presence)} OR {rows.parent(row, lambda p: rows.has(field, p, presence))})'

    def object_scored(self, concept: Any, row: Any) -> str:
        rows, node = self.rows, _scored(concept)
        return f'({rows.test(node, row)} OR {rows.parent(row, lambda p: rows.test(node, p))})'

    # --- Both ---

    def excluded(self, node: dict, row: Any, partners: Callable[..., str]) -> str:
        """An excluded place or lasso: the row's own exclusion (which keeps a row with no location or no point), and no
        partner inside. An exclusion that excludes nothing (fewer than three points) stays so."""
        own = self.rows.test(node, row)
        if own == 'TRUE':
            return own
        inside = {**node, 'operator': _EXCLUDES[_operator(node)]}
        return f'({own} AND NOT {partners(row, lambda r: self.rows.test(inside, r))})'


def _scored(concept: Any) -> dict:
    return {'field': '_cpresent', 'operator': 'concept_present', 'value': {'concept_id': concept}}
