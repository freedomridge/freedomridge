"""Filters as short text, for the filter bar's query editor, scripts and AI tools (docs/FILTER_QUERY.md).

    serial_number in (103000752 103001025)
    AND split = "train"
    AND NOT (score < 0.2 OR note exists)

parse_query turns text into ComplexFilter nodes, one per top-level AND part (one filter-bar chip each),
checking every field name, operator and value against the dataset's fields. Its errors carry a line, a
column and suggestions, so a person or a model can fix the text. format_filter turns a node back into
text, or returns None for what the language doesn't express (concept scores, map and lasso regions,
selections, similarity); those stay chips.
"""

import contextlib
import datetime
import difflib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

# Field kinds: which operators and values a field takes.
NUMBER, DATETIME, TEXT, LIST, BOOLEAN, OBJECT = 'number', 'datetime', 'text', 'list', 'boolean', 'object'

_PRESENCE = ['exists', 'not exists', 'is null', 'is not null']
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
# Query-language operator -> ComplexFilter operator.
_TO_FILTER = {'exists': 'exists', 'not exists': 'not_exists', 'is null': 'is_null', 'is not null': 'is_not_null'}
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
}
_VALUELESS = set(_PRESENCE)
_KEYWORDS = {'and', 'or', 'not', 'in', 'contains', 'exists', 'is', 'null', 'between', 'like'}
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
    def __init__(self, query: str, fields: Dict[str, str]):
        self.tokens = _tokens(query)
        self.position = 0
        self.fields = fields
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
                return {'logic': 'NOT', 'filters': [self.negation()[0]]}, False
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
        token = self.take()
        self.comparisons += 1
        if self.comparisons > MAX_COMPARISONS:
            raise self.fail(
                f'a query takes at most {MAX_COMPARISONS} comparisons; put many values in one in (...)',
                token,
            )
        if token.kind == 'field' or (token.kind == 'word' and token.text.lower() not in _KEYWORDS):
            field = token.text
        else:
            raise self.fail(f'expected a field name, found {_show(token)}', token)
        kind = self.fields.get(field)
        if kind is None:
            raise self.fail(f'unknown field {field!r}', token, _close_names(field, self.fields))
        operator_token = self.peek()
        operator = self.operator(field)
        if operator not in OPERATORS_BY_KIND[kind]:
            allowed = ', '.join(OPERATORS_BY_KIND[kind])
            raise self.fail(
                f'{operator!r} does not apply to the {kind} field {field!r}; use one of: {allowed}',
                operator_token,
            )
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

    def operator(self, field: str) -> str:
        token = self.take()
        if token.kind == 'op':
            return token.text
        word = token.text.lower() if token.kind == 'word' else ''
        if word in ('in', 'contains', 'exists', 'between', 'like'):
            return word
        if word == 'not' and self.peek().keyword('in', 'exists'):
            return f'not {self.take().text.lower()}'
        if word == 'is':
            if self.peek().keyword('null'):
                self.take()
                return 'is null'
            if self.peek().keyword('not') and self.peek(1).keyword('null'):
                self.take()
                self.take()
                return 'is not null'
            raise self.fail(f'expected null or not null after is, found {_show(self.peek())}', self.peek())
        operators = '= != > >= < <= in, not in, between, contains, exists, not exists, is null, is not null'
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
            return int(text) if re.fullmatch(r'[+-]?\d+', text) else float(text)
        if kind == BOOLEAN:
            if text.lower() not in ('true', 'false'):
                raise self.fail(f'{field!r} is true or false, not {text!r}', token)
            return text.lower() == 'true'
        if kind == DATETIME:
            try:  # as the filter compiler reads a date (datasets._utc_datetime); the text itself is kept
                datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
            except ValueError:
                raise self.fail(
                    f'{text!r} is not a date and time; {field!r} takes ISO, such as 2026-01-31 or 2026-01-31T12:00:00Z',
                    token,
                ) from None
        return text


def _close_names(name: str, fields: Dict[str, str]) -> List[str]:
    same_case = [field for field in fields if field.lower() == name.lower()]
    return same_case or difflib.get_close_matches(name, list(fields), n=3, cutoff=0.6)


def parse_query(query: str, fields: Dict[str, str]) -> List[Dict[str, Any]]:
    """The query's top-level AND parts as ComplexFilter nodes. `fields` maps each field name to its kind
    (NUMBER, DATETIME, TEXT, LIST, BOOLEAN or OBJECT). Raises FilterQueryError at the first mistake."""
    return _Parser(query, fields).conjuncts()


def format_filter(node: Dict[str, Any]) -> Optional[str]:
    """`node` as one top-level part of a query (bracketed when it is an AND or OR of several parts), or
    None when the language can't express it."""
    text = _format(node)
    if text is None:
        return None
    logic, filters = node.get('logic'), node.get('filters') or []
    return f'({text})' if logic in ('AND', 'OR') and len(filters) > 1 else text


def _format(node: Dict[str, Any]) -> Optional[str]:
    if not isinstance(node, dict):
        return None
    if 'logic' in node or 'filters' in node:
        logic, filters = str(node.get('logic', 'AND')).upper(), node.get('filters') or []
        parts = [_format(child) for child in filters]
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
    operator, field, value = _FROM_FILTER.get(str(node.get('operator'))), node.get('field'), node.get('value')
    if operator is None or not isinstance(field, str) or not field:
        return None
    name = field if _PLAIN_FIELD.match(field) and field.lower() not in _KEYWORDS else _backticked(field)
    if operator in _VALUELESS:
        return f'{name} {operator}'
    if operator in ('in', 'not in'):
        items = [_format_value(item) for item in (value if isinstance(value, list) else [value])]
        if not items or any(item is None for item in items):
            return None
        return f'{name} {operator} ({", ".join(items)})'  # type: ignore[arg-type]
    text = _format_value(value)
    return None if text is None else f'{name} {operator} {text}'


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
