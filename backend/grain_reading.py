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
- `NOT (X)` holds when the row as a whole has a value for everything X compares and X does not hold for it (what X
  compares: backend/negation.py, the one place that knows it, which the compiler asks for a NOT on one row too).
  `!=` and `not in` are the NOT of `=` and `in`; a concept's exclude is the NOT of its show, with the score as the
  value; an excluded place or lasso is the NOT of the place or the lasso, so the row as a whole has a location (or a
  point on that map) and neither it nor a partner is inside. "As a whole" is the image with all its objects on the
  images' grid, the object with its image on the objects' grid. A NOT inside a larger filter is read first, for the
  row as a whole.
- `is null` holds when neither the row nor any of those partners has a value (absent or a JSON null), `not exists`
  when none has the key (a JSON null under it counts as there): both read first and for the row as a whole, like a
  NOT. `is not null` and `exists` are showing tests like any other.
- A hiding test of a field every row has (the built-in columns) or of a list (`tags`), and the duplicates' collapse,
  are answered by the grid's own row, as before; a list's needs at least one element there. A NOT around tests of
  built-in fields only keeps the reading it had: each row on its own (`NOT (id contains ...)`: the grid's row).

Every rule reads the values. For speed only, the compiler's `Rows` may skip a kind of row for a field that kind has
never been seen with (backend/field_kinds.py): its `test`, `keyed` and `has` then answer FALSE without reading the row,
and a partner whose test is FALSE is not looked up. The parts below fold such constants as SQL's three-valued logic
does (x OR FALSE is x, x AND FALSE is FALSE, NOT FALSE is TRUE), building the parts in order, so the answer is the one
the full statement gives. The SQL is built through the `Rows` the compiler passes (`test` compiles a plain filter node
against a row, `keyed` the same for a partner row with the metadata key's test first, `has_need` that a row has what a
NOT needs; `objects` and `parent` reach a row's objects and an object's image, each as one EXISTS the planner can run
as a hashed set).
"""

from functools import partial
from typing import Any, Callable, Iterable, Optional, Protocol

from . import negation
from .negation import Need

# A filter node's place in the reading: ('test', node), ('not', unit, needs), ('absent', what it is the absence of: a
# test, or ('has', field) for a value), ('own', node), ('and', units), ('or', units).
Unit = tuple

_RANGES = {'>', '>=', '<', '<='}
_ABSENT = {'is_null', 'not_exists'}
_PRESENT = {'is_not_null', 'exists'}
# Answered by the grid's own row: the duplicates' collapse (a hiding of row identities, as `id not in` is), the
# unlinked objects' finder, and a wrapping operator found inside a wrapped filter (each reaches across on its own).
_OWN = {'collapse_duplicates', 'unlinked_object', 'cg_self', 'has_matching_object', 'parent_matches'}


class Rows(Protocol):
    """The compiler's side: SQL for a row (an opaque handle the compiler gives and takes back)."""

    def outer(self) -> Any: ...

    def test(self, node: Any, row: Any) -> str: ...

    def has(self, field: str, row: Any, presence: bool = False) -> str: ...

    def has_need(self, need: Need, row: Any) -> str: ...

    def builtin(self, field: Any) -> bool: ...

    def filters_nothing(self, node: Any) -> bool: ...

    def keyed(self, node: Any, row: Any) -> str: ...

    def objects(self, row: Any, inner: Callable[[Any], str], plain: Optional[Any] = None) -> str: ...

    def parent(self, row: Any, inner: Callable[[Any], str], plain: Optional[Any] = None) -> str: ...

    def row_field(self, field: Any) -> Optional[str]: ...

    def label_path(self, field: Any) -> bool: ...

    def id_list(self, node: Any) -> Optional[list]: ...

    def mark(self) -> Any: ...

    def rollback(self, mark: Any) -> None: ...


def read(base: Any, grid: str, rows: Rows) -> str:
    """SQL for the grid's row (rows.outer()): `grid` 'image' for has_matching_object's pair, 'object' for
    parent_matches'."""
    reader = _Reader(rows)
    unit = reader.unit(base)
    row = rows.outer()
    return reader.image(unit, row) if grid == 'image' else reader.object(unit, row)


def read_not(base: Any, grid: str, rows: Rows) -> str:
    """SQL for NOT (the page's wrapping of `base`), as withGrain and the assistant write an excluded region: the row as
    a whole has a value for everything `base` compares (negation.needs) and `base`, read for the row as `read` reads it,
    doesn't hold. With nothing compared (tests of built-in fields only) it is the plain NOT of that reading."""
    reader = _Reader(rows)
    unit = reader.unit(base)
    row = rows.outer()
    read, having = (reader.image, reader.image_needs) if grid == 'image' else (reader.object, reader.object_needs)
    needs = [partial(having, need, row) for need in negation.needs(base, rows)]
    return reader.all_of([*needs, lambda: _not(read(unit, row))])


_CONSTANTS = ('TRUE', 'FALSE')


class Lookup(str):
    """The SQL of a partner lookup (Rows.objects, Rows.parent): never NULL, so its NOT needs no COALESCE, and written as
    NOT EXISTS the planner can read it as an anti-join (a count of the images' grid's `label != "car"` on a million
    images with four million objects: 1.0 s, against 7.1 s as a test of each image)."""


def _and(parts: list[str]) -> str:
    """AND of SQL already built: FALSE if one part is, TRUE parts left out. The caller makes sure a part left out holds
    no parameter (a constant never does: _Reader.all_of, _Reader.any_of)."""
    if 'FALSE' in parts:
        return 'FALSE'
    parts = [part for part in parts if part != 'TRUE']
    if not parts:
        return 'TRUE'
    return parts[0] if len(parts) == 1 else '(' + ' AND '.join(parts) + ')'


def _or(parts: list[str]) -> str:
    if 'TRUE' in parts:
        return 'TRUE'
    parts = [part for part in parts if part != 'FALSE']
    if not parts:
        return 'FALSE'
    return parts[0] if len(parts) == 1 else '(' + ' OR '.join(parts) + ')'


def _not(sql: str) -> str:
    """NOT, with a test that is NULL for a row (no value) counted as not holding."""
    if sql in _CONSTANTS:
        return 'TRUE' if sql == 'FALSE' else 'FALSE'
    if isinstance(sql, Lookup):
        return f'NOT {sql}'
    return f'NOT COALESCE({sql}, FALSE)'


def _neg(sql: str) -> str:
    """NOT of a test that is never NULL (an EXISTS)."""
    if sql in _CONSTANTS:
        return 'TRUE' if sql == 'FALSE' else 'FALSE'
    return f'NOT {sql}'


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

    def all_of(self, parts: Iterable[Callable[[], str]]) -> str:
        """The AND of the parts, built in order. A FALSE one makes it FALSE and nothing built for it stays among the
        statement's parameters, so a part that comes out a constant never holds one."""
        mark, built = self.rows.mark(), []
        for part in parts:
            sql = part()
            if sql == 'FALSE':
                self.rows.rollback(mark)
                return 'FALSE'
            built.append(sql)
        return _and(built)

    def any_of(self, parts: Iterable[Callable[[], str]]) -> str:
        """The OR of the parts, built in order: TRUE as soon as one is (all_of)."""
        mark, built = self.rows.mark(), []
        for part in parts:
            sql = part()
            if sql == 'TRUE':
                self.rows.rollback(mark)
                return 'TRUE'
            built.append(sql)
        return _or(built)

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
        labels = field == 'segmentation.labels' or self.rows.label_path(field)
        if operator in negation.NEGATIONS or (operator in negation.LABEL_NEGATIONS and labels):
            if row_field or not isinstance(field, str):
                return ('own', node)
            positive = {**node, 'operator': negation.NEGATIONS.get(operator) or negation.LABEL_NEGATIONS[operator]}
            return ('not', ('test', positive), negation.needs(positive, self.rows))
        if operator == 'concept_exclude':
            value = node.get('value')
            concept = value.get('concept_id') if isinstance(value, dict) else None
            if concept is None:
                return ('own', node)  # no concept: no condition, as the compiler reads it
            positive = {**node, 'operator': 'concept_select'}
            return ('not', ('test', positive), negation.needs(positive, self.rows))
        if operator in negation.EXCLUSIONS:
            if self.rows.filters_nothing(node):
                return ('own', node)  # no polygon: no condition, as the compiler reads it
            positive = {**node, 'operator': negation.EXCLUSIONS[operator]}
            return ('not', ('test', positive), negation.needs(positive, self.rows))
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
            isinstance(leaf, dict) and _operator(leaf) in negation.COMPARISONS and kind
            for leaf, kind in zip(leaves, kinds)
        ):
            # Tests of built-in fields and lists only: as before. A NOT of built-in tests asks each row on its own (the
            # grid's row alone for `id contains`, the sidebar's typed terms); a list's, the grid's row, as `not in`,
            # which needs at least one element there.
            if all(kind == 'builtin' for kind in kinds):
                id_text = all(leaf.get('field') == 'id' and _operator(leaf) == 'contains' for leaf in leaves)
                if id_text and all(self._and_or(child) for child in children):
                    return ('own', node)
                return ('test', node)
            return ('own', node)
        return ('not', self.unit(inner), negation.needs(inner, self.rows))

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
            return self.any_of(partial(self.image, child, row) for child in unit[1])
        if kind == 'test':
            test = unit[1]
            return self.any_of(
                [partial(rows.test, test, row), lambda: rows.objects(row, lambda o: rows.keyed(test, o), test)],
            )
        if kind == 'not':
            _, inner, needs = unit
            having = [partial(self.image_needs, need, row) for need in needs]
            return self.all_of([*having, lambda: _not(self.image(inner, row))])
        if kind == 'absent':
            return self.image_absent(unit[1], row)
        if kind == 'has':
            return self.image_has(unit[1], row, presence=True)
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
            return self.all_of(
                [
                    lambda: _not(rows.has(field, row, True)),
                    lambda: _neg(rows.objects(row, lambda o: rows.has(field, o, True))),
                ],
            )
        test = unit[1]
        return self.all_of(
            [lambda: _not(rows.test(test, row)), lambda: _neg(rows.objects(row, lambda o: rows.keyed(test, o)))],
        )

    def image_has(self, field: str, row: Any, presence: bool = False) -> str:
        rows = self.rows
        return self.any_of(
            [
                partial(rows.has, field, row, presence),
                lambda: rows.objects(row, lambda o: rows.has(field, o, presence)),
            ],
        )

    def image_needs(self, need: Need, row: Any) -> str:
        """The image as a whole has what a NOT needs: it or one of its objects."""
        rows = self.rows
        return self.any_of(
            [partial(rows.has_need, need, row), lambda: rows.objects(row, lambda o: rows.has_need(need, o))],
        )

    def image_and(self, units: list, row: Any) -> str:
        """The row-level parts each for the image; the tests together, with the same object."""
        parts = [partial(self.image, child, row) for child in units if not _depends(child)]
        paired = [child for child in units if _depends(child)]
        if len(paired) == 1:
            parts.append(partial(self.image, paired[0], row))
        elif paired:
            parts.append(partial(self.image_pair, paired, row))
        return self.all_of(parts)

    def image_pair(self, units: list, row: Any) -> str:
        """Two or more parts with tests, ANDed: met by the image alone, or by the image with one of its objects."""
        rows = self.rows
        listed = next((unit for unit in units if unit[0] == 'test' and rows.id_list(unit[1]) is not None), None)
        if listed is not None:
            # A list of ids (a selection, a search's results) among them: (the image is listed AND the rest holds for
            # it) OR (a listed object of it meets the rest with it), so each side is found through the list.
            test, rest = listed[1], [unit for unit in units if unit is not listed]
            return self.any_of(
                [
                    lambda: self.all_of([partial(rows.test, test, row), partial(self.image_and, rest, row)]),
                    lambda: rows.objects(
                        row,
                        lambda o: self.all_of([partial(rows.keyed, test, o), partial(self.paired, ('and', rest), o)]),
                        test,
                    ),
                ],
            )
        whole: Unit = ('and', units)
        # An object that meets none of the tests adds nothing to the image alone, so only those that meet one are read:
        # a condition on the object alone, which the planner applies where it reads the objects.
        return self.any_of(
            [
                partial(self.alone, whole, row),
                lambda: rows.objects(
                    row,
                    lambda o: self.all_of([partial(self.some, whole, o), partial(self.paired, whole, o)]),
                ),
            ],
        )

    def some(self, unit: Unit, o: Any) -> str:
        """The object meets one of the unit's tests on its own."""
        return self.any_of(partial(self.rows.keyed, test, o) for test in _tests(unit))

    def alone(self, unit: Unit, row: Any) -> str:
        """The unit met by the image's own values (its row-level parts read for the image as a whole)."""
        kind = unit[0]
        if kind == 'and':
            return self.all_of(partial(self.alone, child, row) for child in unit[1])
        if kind == 'or':
            return self.any_of(partial(self.alone, child, row) for child in unit[1])
        if kind == 'test':
            return self.rows.test(unit[1], row)
        return self.image(unit, row)

    def paired(self, unit: Unit, o: Any) -> str:
        """The unit met by an object `o` together with its image: each test by either, the row-level parts by the
        image as a whole. The image is reached from the object (rows.parent), so the object's EXISTS stays one the
        planner can run as a hashed set."""
        rows, kind = self.rows, unit[0]
        if kind == 'and':
            return self.all_of(partial(self.paired, child, o) for child in unit[1])
        if kind == 'or':
            return self.any_of(partial(self.paired, child, o) for child in unit[1])
        if kind == 'test':
            test = unit[1]
            return self.any_of(
                [partial(rows.test, test, o), lambda: rows.parent(o, lambda p: rows.keyed(test, p), test)],
            )
        return rows.parent(o, lambda p: self.image(unit, p))

    # --- The objects' grid: an object with its image ---

    def object(self, unit: Unit, row: Any) -> str:
        rows, kind = self.rows, unit[0]
        if kind == 'and':
            return self.all_of(partial(self.object, child, row) for child in unit[1])
        if kind == 'or':
            return self.any_of(partial(self.object, child, row) for child in unit[1])
        if kind == 'test':
            test = unit[1]
            return self.any_of(
                [partial(rows.test, test, row), lambda: rows.parent(row, lambda p: rows.keyed(test, p), test)],
            )
        if kind == 'not':
            _, inner, needs = unit
            having = [partial(self.object_needs, need, row) for need in needs]
            return self.all_of([*having, lambda: _not(self.object(inner, row))])
        if kind == 'absent':
            return self.object_absent(unit[1], row)
        if kind == 'has':
            return self.object_has(unit[1], row, presence=True)
        return rows.test(unit[1], row)

    def object_absent(self, unit: Unit, row: Any) -> str:
        """`is null` / `not exists` for the object with its image (image_absent)."""
        rows = self.rows
        if unit[0] == 'has':
            field = unit[1]
            return self.all_of(
                [
                    lambda: _not(rows.has(field, row, True)),
                    lambda: _neg(rows.parent(row, lambda p: rows.has(field, p, True))),
                ],
            )
        test = unit[1]
        return self.all_of(
            [lambda: _not(rows.test(test, row)), lambda: _neg(rows.parent(row, lambda p: rows.keyed(test, p)))],
        )

    def object_has(self, field: str, row: Any, presence: bool = False) -> str:
        rows = self.rows
        return self.any_of(
            [
                partial(rows.has, field, row, presence),
                lambda: rows.parent(row, lambda p: rows.has(field, p, presence)),
            ],
        )

    def object_needs(self, need: Need, row: Any) -> str:
        """The object with its image has what a NOT needs: it or its image."""
        rows = self.rows
        return self.any_of(
            [partial(rows.has_need, need, row), lambda: rows.parent(row, lambda p: rows.has_need(need, p))],
        )
