"""Filters as short text, for the filter bar's query editor, scripts and AI tools (docs/FILTER_QUERY.md).

    serial_number in (103000752 103001025)
    AND split = "train"
    AND NOT (score < 0.2 OR note exists)
    AND not concept "equipment" > 0.62

parse_query turns text into ComplexFilter nodes, one per top-level AND part (one filter-bar chip each),
checking every field name, operator and value against the dataset's fields, and every concept name
against its concepts. Its errors carry a line, a column and suggestions, so a person or a model can fix
the text. format_filter turns a node back into text, or returns None for what the language doesn't
express (drawn map regions and lassos, selections, similarity, a concept that no longer exists); those
stay chips.

`location within "US-KS"` names a loaded region (backend/regions.py) by its key or its name. The caller looks up
what region_names finds in the text and hands parse_query the answers. On a dataset with no field named `location`,
`location is null`, `is not null`, `exists` and `not exists` ask whether a sample has a location.

`is null` asks for no value (not looked at); `is empty` for a list that holds nothing, which is a value. `is empty` is
refused on a field that can hold no list and on a path through a list (`detections.label`: the list is `detections`).
"""

import contextlib
import difflib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Collection, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .datetimes import literal_window

# Field kinds: which operators and values a field takes.
NUMBER, DATETIME, TEXT, LIST, BOOLEAN, OBJECT = 'number', 'datetime', 'text', 'list', 'boolean', 'object'

# `is empty` asks whether a list holds nothing. A key of the metadata of any kind is read row by row (a value that is no
# list is never empty); a field that can hold no list and a path through a list refuse it (emptiness_refusal).
_EMPTINESS = ['is empty', 'is not empty']
_PRESENCE = ['exists', 'not exists', 'is null', 'is not null', *_EMPTINESS]
# `between` and `like` are the SDK's older spellings of a range and of contains; text is never written
# with them.
OPERATORS_BY_KIND: Dict[str, List[str]] = {
    NUMBER: ['=', '!=', '>', '>=', '<', '<=', 'in', 'not in', 'between', *_PRESENCE],
    DATETIME: ['=', '!=', '>', '>=', '<', '<=', 'in', 'not in', 'between', *_PRESENCE],
    TEXT: ['=', '!=', 'in', 'not in', 'contains', 'like', *_PRESENCE],
    LIST: ['=', '!=', 'in', 'not in', 'contains', 'like', *_PRESENCE],
    BOOLEAN: ['=', '!=', *_PRESENCE],
    OBJECT: list(_PRESENCE),
}
# A sample's id is a number that also takes contains: its digits, written in decimal, contain the text.
_ID_TEXT = ['contains', 'like']
# The fields that can hold no list: a sample's own columns (no metadata key can have their names) and its location. The
# one list of them: the filter compiler (routers/datasets.py) refuses `is empty` by it too, and the page's query builder
# mirrors it (frontend/components/filter-stage-editor.tsx NO_LIST_FIELDS) for what it offers.
NO_LIST = frozenset({'id', 'filepath', 'media_type', 'created_at', 'last_modified_at', 'location'})
# Query-language operator -> ComplexFilter operator.
_TO_FILTER = {
    'exists': 'exists',
    'not exists': 'not_exists',
    'is null': 'is_null',
    'is not null': 'is_not_null',
    'is empty': 'is_empty',
    'is not empty': 'is_not_empty',
}
# ComplexFilter operator -> query-language operator, for every spelling the server accepts.
_FROM_FILTER = {
    '=': '=',
    '==': '=',
    '!=': '!=',
    '<>': '!=',
    '>': '>',
    '>=': '>=',
    '<': '<',
    '<=': '<=',
    'in': 'in',
    'not in': 'not in',
    'not_in': 'not in',
    'nin': 'not in',
    'contains': 'contains',
    'exists': 'exists',
    'not_exists': 'not exists',
    'is_null': 'is null',
    'is_not_null': 'is not null',
    'is_empty': 'is empty',
    'is_not_empty': 'is not empty',
}
_VALUELESS = set(_PRESENCE)
_KEYWORDS = {'and', 'or', 'not', 'in', 'contains', 'exists', 'is', 'null', 'between', 'like'}
# A concept's score test, `concept "name" > 0.62`: the concept_select filter (concept_exclude under NOT), as the
# Concepts panel makes it. `concept` is a keyword only where a name follows it: followed by an operator, it is a
# field named concept.
_CONCEPT_FILTERS = ('concept_select', 'concept_exclude')
_CONCEPT_COMPARISONS = ('>', '>=', '<', '<=')
_NUMBER = re.compile(r'[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?\Z')
# A mirror's own fields (`@local.weeds`) are written as they are too: no field path can start with `@`.
_PLAIN_FIELD = re.compile(r'(@local\.[A-Za-z_][A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*)\Z')
_WORD_END = set(' \t\r\n()[],=!<>"\'`#')
_CLOSE = {'(': ')', '[': ']'}
_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f'}  # and every escape json.dumps writes
# Limits that keep one request's work small (any user with access to the dataset can send one). A pasted list is
# one comparison however long, so they cost nothing real.
MAX_COMPARISONS = 500
MAX_DEPTH = 100


@dataclass(frozen=True)
class Concept:
    """A dataset's concept (backend/routers/concepts.py): its name is unique in the dataset."""

    id: int
    name: str
    threshold: float  # its saved cut, which `concept "name"` written without a comparison takes


class FilterQueryError(ValueError):
    """A query the dataset can't take. `detail` is {message, line, column, suggestions}."""

    def __init__(self, message: str, line: int, column: int, suggestions: Sequence[str] = ()):
        super().__init__(f'line {line}, column {column}: {message}')
        self.detail: Dict[str, Any] = {
            'message': message,
            'line': line,
            'column': column,
            'suggestions': list(suggestions),
        }


@dataclass
class RegionMatch:
    """What a region written after `within` names: one region's `key`; several `candidates` (key, kind, country name),
    as a name can (Georgia); or nothing, with close keys as `suggestions`. `loaded`: any boundary set is."""

    key: Optional[str] = None
    candidates: List[Tuple[str, str, str]] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    loaded: bool = True


_REGION_KINDS = {'country': 'country', 'state': 'state or province', 'group': 'group of states or provinces'}
_WITHIN = {'geo_region': 'within', 'geo_region_exclude': 'not within'}


def canonical_region_key(text: str) -> str:
    """A region key as it is stored, from any case: a state's or a country's upper-case (`us-ks` -> `US-KS`), a
    group's country upper-case and its slug lower-case (`us:Midwest` -> `US:midwest`)."""
    country, colon, rest = text.strip().partition(':')
    return f'{country.upper()}:{rest.lower()}' if colon else country.upper()


@dataclass
class _Token:
    kind: str  # word, string, field, op, open, close, comma, end
    text: str  # the value: a word, an unquoted string, an operator or a bracket
    line: int
    column: int

    def keyword(self, *words: str) -> bool:
        return self.kind == 'word' and self.text.lower() in words


def _tokens(query: str) -> List[_Token]:
    tokens, i, line, line_start = [], 0, 1, 0
    while i < len(query):
        char, column = query[i], i - line_start + 1
        if char == '\n':
            i, line, line_start = i + 1, line + 1, i + 1
        elif char in ' \t\r':
            i += 1
        elif char == '#':  # a comment runs to the end of the line
            while i < len(query) and query[i] != '\n':
                i += 1
        elif char in '([':
            tokens.append(_Token('open', char, line, column))
            i += 1
        elif char in ')]':
            tokens.append(_Token('close', char, line, column))
            i += 1
        elif char == ',':
            tokens.append(_Token('comma', char, line, column))
            i += 1
        elif char in '=!<>':
            op = query[i : i + 2] if query[i : i + 2] in ('!=', '>=', '<=', '==', '<>') else char
            if op == '!':
                raise FilterQueryError("'!' on its own isn't an operator; use != for not equal", line, column)
            tokens.append(_Token('op', {'==': '=', '<>': '!='}.get(op, op), line, column))
            i += len(op)
        elif char in '"\'`':
            text, i = _quoted(query, i, line, column)
            tokens.append(_Token('field' if char == '`' else 'string', text, line, column))
        else:
            start = i
            while i < len(query) and query[i] not in _WORD_END:
                i += 1
            tokens.append(_Token('word', query[start:i], line, column))
    tokens.append(_Token('end', '', line, len(query) - line_start + 1))
    return tokens


def _quoted(query: str, start: int, line: int, column: int) -> Tuple[str, int]:
    quote, i, text = query[start], start + 1, []
    while i < len(query) and query[i] != quote:
        if query[i] == '\n':
            break
        if query[i] == '\\' and query[i + 1 : i + 2] == 'u' and re.fullmatch(r'[0-9a-fA-F]{4}', query[i + 2 : i + 6]):
            text.append(chr(int(query[i + 2 : i + 6], 16)))  # as json.dumps writes control characters
            i += 5
        elif query[i] == '\\' and i + 1 < len(query):
            i += 1
            text.append(_ESCAPES.get(query[i], query[i]))
        else:
            text.append(query[i])
        i += 1
    if i >= len(query) or query[i] != quote:
        raise FilterQueryError(f'this {quote} has no closing {quote} on its line', line, column)
    return ''.join(text), i + 1


def _show(token: _Token) -> str:
    return 'the end of the query' if token.kind == 'end' else repr(token.text)


class _Parser:
    def __init__(
        self,
        query: str,
        fields: Dict[str, str],
        regions: Mapping[str, RegionMatch],
        concepts: Sequence[Concept],
        paths: Mapping[str, str],
    ):
        self.tokens = _tokens(query)
        self.position = 0
        self.fields = fields
        self.paths = paths
        self.regions = regions
        self.concepts = {concept.name: concept for concept in concepts}
        self.depth = 0
        self.comparisons = 0

    def peek(self, ahead: int = 0) -> _Token:
        return self.tokens[min(self.position + ahead, len(self.tokens) - 1)]

    def take(self) -> _Token:
        token = self.peek()
        self.position += 1
        return token

    def fail(self, message: str, token: _Token, suggestions: Sequence[str] = ()) -> FilterQueryError:
        return FilterQueryError(message, token.line, token.column, suggestions)

    def conjuncts(self) -> List[Dict[str, Any]]:
        if self.peek().kind == 'end':
            return []
        node, grouped = self.either()
        token = self.peek()
        if token.kind != 'end':
            hint = ' (a closing bracket with no opening one?)' if token.kind == 'close' else ''
            raise self.fail(f'expected AND, OR or the end of the query, found {_show(token)}{hint}', token)
        if not grouped and node.get('logic') == 'AND':
            return node['filters']
        return [node]

    # Each level returns (node, grouped): grouped when the node was written in brackets, which keeps it
    # one part (and one chip) instead of being merged into the AND or OR around it.
    def either(self) -> Tuple[Dict[str, Any], bool]:
        return self.chain('or', self.both)

    def both(self) -> Tuple[Dict[str, Any], bool]:
        return self.chain('and', self.negation)

    def chain(self, word: str, part) -> Tuple[Dict[str, Any], bool]:
        node, grouped = part()
        if not self.peek().keyword(word):
            return node, grouped
        parts = [node]
        while self.peek().keyword(word):
            self.take()
            parts.append(part()[0])
        return {'logic': word.upper(), 'filters': parts}, False

    def negation(self) -> Tuple[Dict[str, Any], bool]:
        if self.peek().keyword('not'):
            with self.nested(self.take()):
                node = self.negation()[0]
            # NOT of one concept test (`not concept "x" > 0.5`, or `NOT (concept "x" > 0.5)`) is the concept's
            # hide filter, the Concepts panel's chip: the same samples, those it has no score for included.
            if node.get('operator') == 'concept_select':
                return {**node, 'operator': 'concept_exclude'}, False
            return {'logic': 'NOT', 'filters': [node]}, False
        if self.peek().kind == 'open':
            opening = self.take()
            with self.nested(opening):
                node, _ = self.either()
            closing = self.take()
            if closing.kind != 'close':
                raise self.fail(f'expected ) to close the ( at line {opening.line}, column {opening.column}', closing)
            return node, True
        node = self.comparison()
        return node, 'logic' in node  # a range (between) stays one part

    @contextlib.contextmanager
    def nested(self, token: _Token) -> Iterator[None]:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise self.fail(f'brackets and NOTs nest at most {MAX_DEPTH} deep', token)
        try:
            yield
        finally:
            self.depth -= 1

    def comparison(self) -> Dict[str, Any]:
        name = self.peek(1)
        # `concept` and then a name (quoted, or a plain word) is a concept's score test; `concept` and then an
        # operator is the field named concept. A plain word is never an operator: those are all keywords, and
        # `within`, the region form's, which is a mistake after any field but `location`.
        concept = self.peek().keyword('concept') and (
            name.kind == 'string' or (name.kind == 'word' and not name.keyword(*_KEYWORDS, 'within'))
        )
        token = self.take()
        self.comparisons += 1
        if self.comparisons > MAX_COMPARISONS:
            raise self.fail(
                f'a query takes at most {MAX_COMPARISONS} comparisons; put many values in one in (...)',
                token,
            )
        if concept:
            return self.concept()
        if token.kind == 'field' or (token.kind == 'word' and token.text.lower() not in _KEYWORDS):
            field = token.text
        else:
            raise self.fail(f'expected a field name, found {_show(token)}', token)
        # `location within` names a region; `location` with any other operator is the dataset's own field of that name.
        if field == 'location' and (
            self.peek().keyword('within') or (self.peek().keyword('not') and self.peek(1).keyword('within'))
        ):
            return self.region()
        kind = self.fields.get(field)
        if kind is None:
            if field == 'location':
                # The sample's own location: `is null`, `is not null`, `exists` and `not exists` ask whether it has
                # one, as their filters do, so the samples an excluded place doesn't show can be found.
                if self.peek().keyword('exists', 'is') or (
                    self.peek().keyword('not') and self.peek(1).keyword('exists')
                ):
                    operator_token = self.peek()
                    operator = self.operator(field)
                    self.emptiness(field, operator, operator_token)
                    return {'field': field, 'operator': _TO_FILTER[operator], 'value': None}
                raise self.fail(
                    f"'location' takes within or not within and a region, such as location within \"US-KS\", or is "
                    f'null, is not null, exists or not exists; found {_show(self.peek())}',
                    self.peek(),
                )
            hint = "; a concept's score is written concept \"name\" > 0.5" if token.keyword('concept') else ''
            raise self.fail(f'unknown field {field!r}{hint}', token, _close_names(field, self.fields))
        operator_token = self.peek()
        operator = self.operator(field)
        self.emptiness(field, operator, operator_token)
        operators = OPERATORS_BY_KIND[kind] + (_ID_TEXT if field == 'id' else [])
        if operator not in operators:
            allowed = ', '.join(operators)
            raise self.fail(
                f'{operator!r} does not apply to the {kind} field {field!r}; use one of: {allowed}',
                operator_token,
            )
        if field == 'id' and operator in _ID_TEXT:
            kind = TEXT  # `id contains "12"`: the id written in decimal, a text test
        if operator in _VALUELESS:
            return {'field': field, 'operator': _TO_FILTER[operator], 'value': None}
        if operator in ('in', 'not in'):
            return {'field': field, 'operator': operator, 'value': self.values(field, kind)}
        if operator == 'between':
            low = self.value(self.take(), field, kind)
            if not self.peek().keyword('and'):
                raise self.fail(f'expected AND in {field} between ... AND ..., found {_show(self.peek())}', self.peek())
            self.take()
            high = self.value(self.take(), field, kind)
            return {
                'logic': 'AND',
                'filters': [
                    {'field': field, 'operator': '>=', 'value': low},
                    {'field': field, 'operator': '<=', 'value': high},
                ],
            }
        value_token = self.peek()
        value = self.value(self.take(), field, kind)
        if operator == 'like':
            value = str(value).strip('%')
            if '%' in value:
                raise self.fail("like only takes '%text%' (contains text); use contains", value_token)
            operator = 'contains'
        return {'field': field, 'operator': operator, 'value': value}

    def emptiness(self, field: str, operator: str, token: _Token) -> None:
        """Refuse `is empty` and `is not empty` where they can't be asked (emptiness_refusal), with the list to ask as
        the suggestion for a path through a list."""
        through = self.paths.get(field)
        refusal = emptiness_refusal(field, operator, through=through)
        if refusal is not None:
            raise self.fail(refusal, token, [through] if through is not None else [])

    def region(self) -> Dict[str, Any]:
        """`location within <region>` or `location not within <region>`, after the field. `within` is a keyword only
        here, so a field or a value named within needs no quotes."""
        operator = 'geo_region' if self.take().keyword('within') else 'geo_region_exclude'
        if operator == 'geo_region_exclude':
            self.take()  # within, after not
        value = self.take()
        if value.kind != 'string' and (value.kind != 'word' or value.text.lower() in _KEYWORDS):
            raise self.fail(f'expected a region after within, such as "US-KS" or Kansas, found {_show(value)}', value)
        match = self.regions.get(value.text, RegionMatch())
        if match.key is not None:
            return {'field': 'location', 'operator': operator, 'value': match.key}
        if match.candidates:
            named = ', '.join(
                f'{key} ({_REGION_KINDS.get(kind, kind)}{"" if kind == "country" else " of " + country})'
                for key, kind, country in match.candidates
            )
            raise self.fail(
                f'{value.text!r} names {len(match.candidates)} regions: {named}; write the key of the one you mean',
                value,
                [key for key, _, _ in match.candidates],
            )
        if not match.loaded:
            raise self.fail(f'unknown region {value.text!r}: no boundary set is loaded on this server', value)
        raise self.fail(
            f"unknown region {value.text!r}: the boundary set isn't loaded on this server or has no such region",
            value,
            match.suggestions,
        )

    def concept(self) -> Dict[str, Any]:
        token = self.take()
        concept = self.concepts.get(token.text)
        if concept is None:
            known = '' if self.concepts else '; this dataset has no concepts'
            raise self.fail(f'unknown concept {token.text!r}{known}', token, _close_names(token.text, self.concepts))
        operator = self.peek()
        if operator.kind != 'op' and not operator.keyword('in', 'not', 'contains', 'like', 'between', 'exists', 'is'):
            if operator.kind in ('word', 'string') and _NUMBER.match(operator.text.strip()):
                name = json.dumps(concept.name, ensure_ascii=False)
                raise self.fail(
                    f"expected a comparison after concept {name}, found {operator.text!r}: a concept's score is "
                    f'compared, as in concept {name} > {operator.text.strip()}',
                    operator,
                )
            # No comparison: the concept's saved cut, as the Concepts panel applies it.
            value = {'concept_id': concept.id, 'op': '>', 'threshold': float(concept.threshold)}
            return {'field': '_concept', 'operator': 'concept_select', 'value': value}
        self.take()
        if operator.keyword('is') and (
            self.peek().keyword('empty') or (self.peek().keyword('not') and self.peek(1).keyword('empty'))
        ):
            refusal = emptiness_refusal(None, 'is not empty' if self.peek().keyword('not') else 'is empty')
            if refusal is not None:
                raise self.fail(refusal, operator)
        if operator.text not in _CONCEPT_COMPARISONS:
            raise self.fail(
                f'{operator.text!r} does not apply to the concept {concept.name!r}; use one of: '
                + ', '.join(_CONCEPT_COMPARISONS),
                operator,
            )
        threshold = self.take()
        if threshold.kind not in ('word', 'string') or threshold.text.lower() in _KEYWORDS:
            raise self.fail(
                f"expected the concept's threshold after {operator.text!r}, such as 0.5, found {_show(threshold)}",
                threshold,
            )
        text = threshold.text.strip()
        if not _NUMBER.match(text):
            raise self.fail(f"{text!r} is not a number, and a concept's threshold is a number", threshold)
        if not math.isfinite(float(text)):
            raise self.fail(f'{text!r} is out of range for a number', threshold)
        value = {'concept_id': concept.id, 'op': operator.text, 'threshold': float(text)}
        return {'field': '_concept', 'operator': 'concept_select', 'value': value}

    def operator(self, field: str) -> str:
        token = self.take()
        if token.kind == 'op':
            return token.text
        word = token.text.lower() if token.kind == 'word' else ''
        if word in ('in', 'contains', 'exists', 'between', 'like'):
            return word
        if word == 'within' or (word == 'not' and self.peek().keyword('within')):
            raise self.fail(f"within applies only to 'location' (location within \"US-KS\"), not to {field!r}", token)
        if word == 'not' and self.peek().keyword('in', 'exists'):
            return f'not {self.take().text.lower()}'
        if word == 'is':
            # `empty` is a plain word everywhere else (a field or a value named empty needs no quotes).
            if self.peek().keyword('null', 'empty'):
                return f'is {self.take().text.lower()}'
            if self.peek().keyword('not') and self.peek(1).keyword('null', 'empty'):
                self.take()
                return f'is not {self.take().text.lower()}'
            raise self.fail(
                f'expected null, not null, empty or not empty after is, found {_show(self.peek())}',
                self.peek(),
            )
        operators = (
            '= != > >= < <= in, not in, between, contains, exists, not exists, is null, is not null, is empty, '
            'is not empty'
        )
        raise self.fail(f'expected an operator after {field!r} ({operators}), found {_show(token)}', token)

    def values(self, field: str, kind: str) -> List[Any]:
        opening = self.take()
        if opening.kind != 'open':
            raise self.fail(f'expected a list after in, such as ("a", "b") or (1 2 3), found {_show(opening)}', opening)
        values: List[Any] = []
        while True:
            token = self.take()
            if token.kind == 'close' and token.text == _CLOSE[opening.text]:
                break
            if token.kind == 'comma':
                continue
            if token.kind == 'end':
                raise self.fail(f'this list has no closing {_CLOSE[opening.text]}', opening)
            values.append(self.value(token, field, kind))
        if not values:
            raise self.fail('a list needs at least one value', opening)
        return values

    def value(self, token: _Token, field: str, kind: str) -> Any:
        if token.kind == 'string':
            text = token.text
        elif token.kind == 'word' and token.text.lower() not in _KEYWORDS:
            text = token.text
        else:
            raise self.fail(f'expected a value for {field!r}, found {_show(token)}', token)
        if kind == NUMBER:
            text = text.strip()
            if not _NUMBER.match(text):
                raise self.fail(f'{text!r} is not a number, and {field!r} is a number field', token)
            # 1e999 reads as infinity, which no answer can carry and no filter compares with.
            if not math.isfinite(float(text)):
                raise self.fail(f'{text!r} is out of range for a number', token)
            return int(text) if re.fullmatch(r'[+-]?\d+', text) else float(text)
        if kind == BOOLEAN:
            if text.lower() not in ('true', 'false'):
                raise self.fail(f'{field!r} is true or false, not {text!r}', token)
            return text.lower() == 'true'
        if kind == DATETIME and literal_window(text) is None:  # as the filter compiler reads it; the text is kept
            raise self.fail(
                f'{text!r} is not a date and time; {field!r} takes ISO, such as 2026-01-31 or 2026-01-31T12:00:00Z',
                token,
            )
        return text


def emptiness_refusal(
    field: Optional[str],
    operator: str,
    *,
    through: Optional[str] = None,
    derived: bool = False,
) -> Optional[str]:
    """Why `is empty` or `is not empty` (`operator`, `is_empty` or `is empty`) can't be asked of the field, or None (and
    None for any other operator): the one place for the refusal and its words, which the filter text and the filter
    compiler both give. Refused: a field that can hold no list (NO_LIST, a concept's score for `field` None, and an
    embedding's or a hash's presence, `derived`, which only the compiler is asked about: the text knows no such field),
    and a path through a list, `through` naming its list, which is what to ask. Any other field is read row by row."""
    words = operator.replace('_', ' ')
    if words not in _EMPTINESS:
        return None
    if through is not None:
        return (
            f"{words!r} can't be asked of {field!r}, a path through the list {through!r}: ask it of the list, "
            f'{through} {words}'
        )
    if field is None:
        return f"{words!r} is a test of a list, and a concept's score is no list: ask it of a list field"
    if field in NO_LIST or derived:
        return f'{words!r} is a test of a list, and {field!r} holds no list: ask it of a list field'
    return None


def _close_names(name: str, fields: Collection[str]) -> List[str]:
    same_case = [field for field in fields if field.lower() == name.lower()]
    return same_case or difflib.get_close_matches(name, list(fields), n=3, cutoff=0.6)


def parse_query(
    query: str,
    fields: Dict[str, str],
    *,
    regions: Optional[Mapping[str, RegionMatch]] = None,
    concepts: Sequence[Concept] = (),
    paths: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, Any]]:
    """The query's top-level AND parts as ComplexFilter nodes. `fields` maps each field name to its kind
    (NUMBER, DATETIME, TEXT, LIST, BOOLEAN or OBJECT), `regions` each text region_names found to what it names,
    `concepts` are the dataset's, which `concept "name"` names, and `paths` maps each field that is a path through a
    list (`detections.label`) to that list (`detections`). Raises FilterQueryError at the first mistake."""
    return _Parser(query, fields, regions or {}, concepts, paths or {}).conjuncts()


def region_names(query: str) -> List[str]:
    """The regions the query names after `location within` or `location not within`, as written, for the caller to
    look up before parse_query. A query that can't be read names none (parse_query reports why)."""
    try:
        tokens = _tokens(query)
    except FilterQueryError:
        return []
    found = []
    for i, token in enumerate(tokens):
        if token.kind not in ('word', 'field') or token.text != 'location':
            continue
        after = 2 if tokens[i + 1].keyword('within') else 3 if tokens[i + 1].keyword('not') else 0
        if after == 3 and not tokens[i + 2].keyword('within'):
            after = 0
        value = tokens[i + after] if after else None
        if value is not None and value.kind in ('word', 'string') and value.text not in found:
            found.append(value.text)
    return found


def format_filter(
    node: Dict[str, Any],
    *,
    concepts: Sequence[Concept] = (),
    kept: Collection[str] = (),
) -> Optional[str]:
    """`node` as one top-level part of a query (bracketed when it is an AND or OR of several parts), or
    None when the language can't express it. A concept's filter is written with the concept's name, from
    `concepts`, and always with its comparison, so the text keeps its meaning when the saved cut changes.
    A comparison on a field in `kept` (a private field, for a request made for the assistant) is written without
    its values: `serial in (3 values kept from the assistant)`, which no query reads back."""
    text = _format(node, {concept.id: concept.name for concept in concepts}, kept)
    if text is None:
        return None
    logic, filters = node.get('logic'), node.get('filters') or []
    return f'({text})' if logic in ('AND', 'OR') and len(filters) > 1 else text


def _format(node: Dict[str, Any], concepts: Dict[int, str], kept: Collection[str] = ()) -> Optional[str]:
    if not isinstance(node, dict):
        return None
    if 'logic' in node or 'filters' in node:
        logic, filters = str(node.get('logic', 'AND')).upper(), node.get('filters') or []
        parts = [_format(child, concepts, kept) for child in filters]
        if not parts or any(part is None for part in parts):
            return None
        if logic == 'NOT':
            return f'NOT ({" AND ".join(parts)})'  # type: ignore[arg-type]
        if logic not in ('AND', 'OR'):
            return None
        if len(parts) == 1:
            return parts[0]
        bracketed = [
            f'({part})' if child.get('logic') in ('AND', 'OR') and len(child.get('filters') or []) > 1 else part
            for child, part in zip(filters, parts)
        ]
        return f' {logic} '.join(bracketed)  # type: ignore[arg-type]
    if node.get('operator') in _WITHIN:
        value = node.get('value')
        if node.get('field') != 'location' or not isinstance(value, str) or not value.strip():
            return None
        return f'location {_WITHIN[node["operator"]]} {json.dumps(canonical_region_key(value), ensure_ascii=False)}'
    if node.get('operator') in _CONCEPT_FILTERS:
        return _format_concept(node, concepts)
    operator, field, value = _FROM_FILTER.get(str(node.get('operator'))), node.get('field'), node.get('value')
    if operator is None or not isinstance(field, str) or not field:
        return None
    name = field if _PLAIN_FIELD.match(field) and field.lower() not in _KEYWORDS else _backticked(field)
    if operator in _VALUELESS:
        return f'{name} {operator}'
    if kept and _kept(field, kept):
        count = len(value) if isinstance(value, list) and operator in ('in', 'not in') else None
        what = 'a value' if count is None else f'{count} value{"" if count == 1 else "s"}'
        return f'{name} {operator} ({what} kept from the assistant)'
    if operator in ('in', 'not in'):
        items = [_format_value(item) for item in (value if isinstance(value, list) else [value])]
        if not items or any(item is None for item in items):
            return None
        return f'{name} {operator} ({", ".join(items)})'  # type: ignore[arg-type]
    text = _format_value(value)
    return None if text is None else f'{name} {operator} {text}'


def _kept(field: str, kept: Collection[str]) -> bool:
    """`field` is a kept field or a path inside one (`detections.label` of `detections`)."""
    parts = field.split('.')
    return any('.'.join(parts[:end]) in kept for end in range(1, len(parts) + 1))


def _format_concept(node: Dict[str, Any], concepts: Dict[int, str]) -> Optional[str]:
    # The filter compiler's defaults for a value without them: `>` and 0.5.
    value = node.get('value')
    if not isinstance(value, dict):
        return None
    concept_id, op, threshold = value.get('concept_id'), value.get('op', '>'), value.get('threshold', 0.5)
    name = concepts.get(concept_id) if isinstance(concept_id, int) and not isinstance(concept_id, bool) else None
    number = isinstance(threshold, (int, float)) and not isinstance(threshold, bool) and _finite(threshold)
    if name is None or op not in _CONCEPT_COMPARISONS or not number:
        return None
    negation = 'not ' if node.get('operator') == 'concept_exclude' else ''
    return f'{negation}concept {json.dumps(name, ensure_ascii=False)} {op} {json.dumps(threshold)}'


def _finite(number: float) -> bool:
    try:
        return math.isfinite(number)
    except OverflowError:  # an int too large for a float
        return False


def _format_value(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return None


def _backticked(field: str) -> str:
    return '`' + field.replace('\\', '\\\\').replace('`', '\\`') + '`'
