# Filter query language

A filter written as text. The same text works in two places:

- **The SDK:** `dataset.query("...")` returns a view, and `dataset.create_view(name, query="...")` saves one.
- **The HTTP API:** `POST /api/datasets/{id}/filter_query/parse` and `.../format` convert between text and filters, for scripts and for AI tools.

The server parses every query against the dataset's own fields (`backend/filter_query.py`). A misspelt field, an operator that doesn't apply, or a value of the wrong type is reported with its line, column and suggestions. It is never quietly turned into a filter that matches nothing.

```
serial_number in (103000752 103001025 103004080)
AND split = "train"
AND NOT (score < 0.2 OR note exists)
```

## Parts and chips

A query is an AND of parts. Each top-level `AND` part becomes one stage: one chip in the filter bar, one stage in a saved view. Brackets keep a group together as one part. In the example above, `(score < 0.2 OR note exists)` is one part, and `NOT (...)` is one part.

`AND` binds tighter than `OR`, as in SQL. So `a = 1 OR b = 2 AND c = 3` means `a = 1 OR (b = 2 AND c = 3)`. Use brackets when you mean the other one.

## Fields

- A field is named as the dataset names it: `split`, `score`, `detections.label`.
- The fields every dataset has are `id`, `filepath`, `media_type`, `created_at` and `tags`.
- A name that isn't a plain word, or that is one of the language's keywords, goes in backticks: `` `weather station` = "A" `` or `` `in` = 1 ``.
- Names are case sensitive. `Split` gets "unknown field 'Split'" with the suggestion `split`.

## Operators

| Operator | Meaning | Fields |
|---|---|---|
| `=` `!=` (also `==`, `<>`) | equal, not equal | all but objects |
| `>` `>=` `<` `<=` | compare | numbers, dates |
| `in (...)`, `not in (...)` | one of the listed values, or none of them | numbers, dates, text, lists |
| `between A and B` | `>= A` and `<= B`, as one part | numbers, dates |
| `contains "x"` (also `like "%x%"`) | case-insensitive substring | text, lists |
| `exists`, `not exists` | the field is present, or absent | all |
| `is null`, `is not null` | no value (absent or null), or a value | all |

Logic: `AND`, `OR` and `NOT`, with brackets. Keywords aren't case sensitive.

## Values

- **Text:** `"double"` or `'single'` quotes, with `\"`, `\\`, `\n` and `\t` escapes. A plain word needs no quotes: `split = train`. `and`, `or`, `not`, `in`, `contains`, `exists`, `is`, `null`, `between` and `like` always need quotes.
- **Numbers:** `12`, `-1.5`, `1e3`.
- **Dates:** ISO text, quoted or not: `created_at >= 2026-01-01T00:00:00Z`. Text that isn't an ISO date and time is an error.
- **Booleans:** `true` and `false`.
- **Lists:** brackets, `(...)` or `[...]`, with commas, spaces or newlines between values. A column of serial numbers pasted between `(` and `)` works as it is, and so does a JSON list.

A value takes the field's type:

- On a **text** field, a bare number stays text, so `serial_number in (007 8)` matches the text `"007"`.
- On a **number** field, a quoted number is a number.
- On a **number** field, `abc` is an error.

## Comments

`#` starts a comment that runs to the end of the line.

## Limits

A query takes at most 500 comparisons, and brackets and `NOT` nest at most 100 deep. A list in `in (...)` is one comparison however many values it holds, so put many values in one list rather than writing `a = 1 OR a = 2 OR …`.

## What stays a chip

These filters have no text form: concept scores, map regions, lassos, selections and similarity searches. Their values are ids, polygons or tokens rather than something to type. `format` returns `null` for each of them, and they stay stages of their own.

## HTTP API

Both routes need access to the dataset.

**`POST /api/datasets/{id}/filter_query/parse`**

The request body:

```json
{"query": "split = train AND score >= 0.5", "current": []}
```

The response:

```json
{
  "filters": [
    {"field": "split", "operator": "=", "value": "train"},
    {"field": "score", "operator": ">=", "value": 0.5}
  ],
  "parts": ["split = \"train\"", "score >= 0.5"],
  "filter": {"logic": "AND", "filters": ["...the same two filters..."]},
  "reuse": [null, null]
}
```

- **`filters`:** one per part, in the `ComplexFilter` format the rest of the API takes ([panels.md](panels.md)).
- **`parts`:** each part written back as text.
- **`filter`:** their AND, ready for `samples/query`, `map_points` or a view.
- **`current` and `reuse`:** `current` takes the filters already applied, one per chip. `reuse[i]` is the index of the one equal to part `i`, so a caller keeps that chip, with its label and its default-view mark, instead of making a new one.

A mistake is a 400:

```json
{"detail": {
  "message": "line 2, column 5: unknown field 'seriel'",
  "errors": [{"message": "unknown field 'seriel'", "line": 2, "column": 5, "suggestions": ["serial"]}]
}}
```

Other messages include:

- `'>' does not apply to the text field 'split'; use one of: =, !=, in, not in, ...`
- `'abc' is not a number, and 'score' is a number field`
- `expected ) to close the ( at line 1, column 1`

**`POST /api/datasets/{id}/filter_query/format`**

The request body is `{"filters": [...]}`. The response is `{"parts": ["split = \"train\"", null, ...], "query": "split = \"train\"\nAND ..."}`. A part is `null` where the filter has no text form, and `query` joins the others.

## SDK

```python
view = dataset.query("serial in (103000752 103001025) AND split = train")
dataset.create_view("Serials", query="serial in (103000752 103001025)")
filters = client.parse_filter_query(dataset_id, "score > 0.5")["filter"]
```

`dataset.query()` and `create_view(query=...)` raise `ValueError` with the server's message and suggestions:

```
line 1, column 1: unknown field 'seriel' (did you mean 'serial'?)
```

Earlier releases parsed `query()` in the SDK itself. That parser accepted any field name, and read `a OR b AND c` as `(a OR b) AND c`. `BETWEEN` and `LIKE '%x%'` are still accepted.
