# Filter query language

A filter written as text. The same text works in four places:

- **The app's filter cards:** each filter's card, and the **Add Filter** card, switch between **Builder** (the form) and **Query** (the filter as text). You can read, copy, paste or edit it, then press **Apply** (or Ctrl+Enter).
- **The SDK:** `dataset.query("...")` returns a view, and `dataset.create_view(name, query="...")` saves one.
- **The HTTP API:** `POST /api/datasets/{id}/filter_query/parse` and `.../format` convert between text and filters, for scripts and for AI tools.
- **Links:** `/datasets/<id>?q=<text>` opens the dataset on the filter, one chip per top-level `AND` part, and **Copy link** writes one when every chip has a text form ([links](DATASET_MANAGEMENT.md#links-to-a-filter-a-view-or-a-sample)).

The server parses every query against the dataset's own fields and concepts (`backend/filter_query.py`). A misspelt field or concept, an operator that doesn't apply, or a value of the wrong type is reported with its line, column and suggestions. It is never quietly turned into a filter that matches nothing.

```
serial_number in (103000752 103001025 103004080)
AND split = "train"
AND NOT (score < 0.2 OR note exists)
AND not concept "equipment" > 0.62
```

## In the filter cards

The filter bar shows the filters as chips. Click a chip and its card opens; **Builder | Query** at the top switches
between the form and that filter as text. The **Add Filter** card has the same switch beside its field search, so a
filter can be typed or pasted instead of picked.

- **Query shows the filter as the server writes it**, one rule per line joined with `AND`: `idx in (0 1 2)` comes back as `idx in (0, 1, 2)`.
- **Apply** (or Ctrl+Enter) applies the text to that chip, or on the Add Filter card adds it as a new chip. **Cancel** leaves the filter as it was.
- **A card holds one chip.** `a = 1 AND b = 2` typed in one card is one chip with both rules.
- **Switching back to Builder keeps the edit:** the text is read into the form. An empty Add Filter query goes back to the field list.
- **A mistake** shows its line, column and suggestion under the text, puts the cursor on it, and changes nothing.

## Parts and chips

A query is an AND of parts. In the SDK and the API, each top-level `AND` part becomes one stage: one chip in the filter bar, one stage in a saved view. (In the app, the text in one filter card stays one chip.) Brackets keep a group together as one part. In the example above, `(score < 0.2 OR note exists)` is one part, and `NOT (...)` is one part.

`AND` binds tighter than `OR`, as in SQL. So `a = 1 OR b = 2 AND c = 3` means `a = 1 OR (b = 2 AND c = 3)`. Use brackets when you mean the other one.

## Fields

- A field is named as the dataset names it: `split`, `score`, `detections.label`.
- The fields every dataset has are `id`, `filepath`, `media_type`, `created_at`, `last_modified_at` (when a sample
  last changed) and `tags`.
- On a mirror, its own fields are named `@local.<name>` and written as they are: `@local.weeds > 3`,
  `@local.tags in ("checked")` ([FOLLOWERS.md](FOLLOWERS.md#fields-of-its-own)).
- A name that isn't a plain word, or that is one of the language's keywords, goes in backticks: `` `weather station` = "A" `` or `` `in` = 1 ``.
- A field named `concept` needs no backticks: followed by an operator, `concept` is the field (`concept = "a"`,
  `concept exists`, `concept in (a b)`), and only followed by a name is it a [concept part](#concept-scores).
- Names are case sensitive. `Split` gets "unknown field 'Split'" with the suggestion `split`.

## Operators

| Operator | Meaning | Fields |
|---|---|---|
| `=` `!=` (also `==`, `<>`) | equal, not equal | all but objects |
| `>` `>=` `<` `<=` | compare | numbers, dates |
| `in (...)`, `not in (...)` | one of the listed values, or none of them | numbers, dates, text, lists |
| `between A and B` | `>= A` and `<= B`, as one part | numbers, dates |
| `contains "x"` (also `like "%x%"`) | case-insensitive substring | text, lists, `id` |
| `exists`, `not exists` | the field is present, or absent | all |
| `is null`, `is not null` | no value (absent or null), or a value | all |

`id contains "12"` keeps the samples whose id, written in decimal, contains `12` (12, 112, 1203, ...), and
`NOT (id contains "12")` every other sample. Text that isn't digits matches no sample.

Logic: `AND`, `OR` and `NOT`, with brackets. Keywords aren't case sensitive. `concept` is a keyword only where it
starts a [concept part](#concept-scores): followed by a name (quoted, or a plain word) it is a concept's score,
followed by an operator it is a field named concept. `within` counts as an operator there, so `concept within "US-KS"`
is the [region form](#regions)'s mistake (`within` applies only to `location`).

A negation of a field (`!=`, `not in`, or a `NOT` around one field's tests) keeps the samples with another value. On
a dataset without objects it keeps the samples without the field too: `split != "train"` keeps the samples that have
no `split`. On a dataset with objects it keeps only the samples that have the field, and a `NOT` around tests of two
or more fields works differently ([below](#on-a-dataset-with-objects)).

## Concept scores

A [concept](SDK.md#concepts-learned-scores) scores every sample from 0 to 1. A concept part keeps or hides the
samples by that score, as the Concepts panel's **show only** and **exclude** do:

- `concept "equipment" > 0.62` keeps the samples whose score for the dataset's concept named `equipment` passes the
  comparison: `>`, `>=`, `<` or `<=`.
- `not concept "equipment" > 0.62` hides them and keeps the rest, as the panel's exclude does. On a dataset without
  objects the rest includes the samples the concept has no score for; on a dataset with objects it is only the
  samples that have a score, here those at or below 0.62 ([below](#on-a-dataset-with-objects)).
  `NOT (concept "equipment" > 0.62)` is the same filter. A `NOT` around more than the one concept part, such as
  `NOT (concept "equipment" > 0.62 AND split = "train")`, is a `NOT` of its parts.
- `concept "equipment"`, with no comparison, takes the concept's saved threshold with `>`, as it is when the text is
  read. The text the app and `format` write always has the comparison, so a link keeps its meaning when the saved
  threshold changes.
- The name is quoted, or written bare when it is a plain word: `concept equipment > 0.62`. A name that is one of the
  language's keywords, or `within`, is quoted: `concept "in" > 0.5`, `concept "within" > 0.5`. Names are case
  sensitive.
- A concept part combines like any other: `split = "train" AND not concept "equipment" > 0.62`, inside brackets and
  with `OR`.

A concept is named by its name, so text that names one stops working when the concept is deleted or renamed: the
error names the unknown concept and suggests the dataset's close names. On a mirror, a `?q=` link names a concept,
so it resolves against that server's own concepts, since concepts and their ids belong to each instance.

An `f=` link, a saved view or the default view holds a concept by its id. A stage naming a concept the dataset no
longer has opens without that filter, and the app says so, naming the stage by its label: `From this link, “Exclude:
scrap” was left out: it filters by a concept this dataset no longer has.` A concept test inside a larger stage takes
that whole stage with it, as what is left would filter differently. A view that lost a stage this way differs from
what is applied, so **Update this view** is offered. The default view's notice is shown once in each browser, and
again when the view's filters change. Stages are checked against the dataset's concepts before they are applied: on a
slow server they wait for the list.

The server refuses a filter naming a concept that isn't the dataset's, deleted, never made or another dataset's,
wherever a filter is read (400: `Concept 12 doesn't exist; it may have been deleted`, or, for one that exists in
another dataset, `Concept 12 doesn't exist in this dataset; it may have been deleted`), and a bulk edit
naming one when it is submitted, with nothing queued. It used to keep every sample or none. The dataset's own
concept being rescored is still refused with a 409 until its job ends; another dataset's gets the 400 even then.

## Regions

`location within` names a loaded region ([REGIONS.md](REGIONS.md)): a country, a state or province, or a group of
them.

```
location within "US-KS"
location not within US:midwest
location within Kansas AND split = train
```

- **`location within R`** keeps the samples located inside R; **`location not within R`** keeps every other sample,
  samples with no location included (on a dataset with objects, see [below](#on-a-dataset-with-objects)). They are the
  filters `geo_region` and `geo_region_exclude`.
- **R is a key or a name, in any case,** quoted or not: `"US-KS"`, `us-ks`, `Kansas`, `"New South Wales"` (a name
  with spaces needs quotes). The text is always written back with the key: `location within "US-KS"`.
- **A name that several regions have is a mistake** whose suggestions are their keys, with their kinds and countries
  in the message: `'Georgia' names 2 regions: GE (country), US-GA (state or province of United States of America);
  write the key of the one you mean`.
- **A region this server doesn't have** (no boundary set loaded, or no such key or name) is a mistake too, with close
  keys as suggestions: `unknown region 'US-KZ': the boundary set isn't loaded on this server or has no such region`
  (suggestions `US-KS`, `US-KY`, `KZ-KUS`).
- **`within` and `not within` after `location` are the region form; `location` with any other operator is the
  dataset's own field named `location`,** as it was before regions: `location = "north field"` filters that field. On a
  dataset with no such field, `location = ...` is a mistake that points to `within`. `within` applies only to
  `location`.

## On a dataset with objects

On a dataset with object rows (each naming its image in `parent_id`), the dataset's page reads every filter across
the images and their objects: an image is kept when it matches the filter or one of its objects does, and on the
objects' grid an object is kept when it or its image does. The assistant reads filters the same way. An image and its
objects hold different fields, so there a negation of one field, or of a concept's score, is true only of a row that
has that field or score at all. Otherwise every object, which has none of its image's fields, would pass every
negation of them and bring its image back.

- `split != "train"`, `split not in (train val)` and `NOT (split contains "tr")` keep only the samples that have a
  `split`. On a dataset without objects they keep the samples with no `split` too.
- `not concept "equipment" > 0.62` keeps only the samples the concept scores at or below 0.62. On a dataset without
  objects it keeps the samples it has no score for too. The assistant's `concept_preview` says how many images have
  no score (`unscored`) before the filter is applied.
- A `NOT` around a condition on two or more fields isn't held to this: it is also true of a row that has none of those
  fields. So the images of such objects are kept too, and on the objects' grid the objects of such images. Where the
  objects have neither `colour` nor `n`, `NOT (colour = "red" AND n = 0)` keeps every image that has an object, the red
  images with `n` 0 among them.
- `location not within "US-KS"` keeps the same samples on both kinds of dataset, those outside the place and those
  with no location, when it is a filter of its own: one chip in the app, made from a filter card's Query or Builder
  tab, or a part joined by `AND` at the top of a query. It isn't guarded so, and is also true of an object with
  no location, so the images inside the place that have such objects are kept too, when a `NOT` is written around a
  place test (`NOT (location within "US-KS")`), when it sits inside an `OR` group, or, in the app, when it shares one
  filter card with another test (the card is one filter, linked to the objects as a whole). These cases concern
  objects with no location: an object with a location of its own is tested like any sample. To leave a place out on a
  dataset with objects, give `location not within` a filter card of its own.
- Every row has the built-in fields (`id`, `filepath`, `media_type`, `created_at`, `last_modified_at`), so nothing
  changes for them. A list (`tags`, or a list field kept in a column of its own) keeps the samples with no value on
  either kind of dataset: `tags not in (rejected)` keeps the samples without tags.

## Values

- **Text:** `"double"` or `'single'` quotes, with `\"`, `\\`, `\n` and `\t` escapes. A plain word needs no quotes: `split = train`. `and`, `or`, `not`, `in`, `contains`, `exists`, `is`, `null`, `between` and `like` always need quotes. `within` is a keyword only after `location`: a field or a value named within needs none. `concept`, a keyword only before a concept's name, doesn't need them either.
- **Numbers:** `12`, `-1.5`, `1e3`. A number too large for a double, such as `1e999`, is an error: `'1e999' is out of range for a number`.
- **Dates:** ISO text, quoted or not: `created_at >= 2026-01-01T00:00:00Z`. Text that isn't an ISO date and time is an error.
  On `created_at`, `last_modified_at` and any field declared `datetime`, dates compare as moments: `2026-01-01T10:00:00+02:00` is the same
  moment as `2026-01-01T08:00:00Z`, and a date or time written without an offset is UTC. A value written to the day,
  minute or second means that whole unit: `captured_at = 2026-01-01` is the whole UTC day, `captured_at <=
  2026-01-01T10:00` ends where 10:01 UTC starts, and `captured_at > 2026-01-01` starts on 2 January UTC; `in` and
  `not in` take each value's unit too, and a value written with a fraction of a second is that exact moment. One rule
  for every datetime, `created_at` and `last_modified_at` included. A sample
  whose value isn't a date and time never matches a range, and `!=` and `not in` include it. To filter by the
  clock time where a picture was taken, store that time as its own field without an offset (see
  [Dataset management](DATASET_MANAGEMENT.md#dates-and-times)).
- **Booleans:** `true` and `false`.
- **Lists:** brackets, `(...)` or `[...]`, with commas, spaces or newlines between values. A column of serial numbers pasted between `(` and `)` works as it is, and so does a JSON list.

A value takes the field's type:

- On a **text** field, a bare number stays text, so `serial_number in (007 8)` matches the text `"007"`.
- On a **number** field, a quoted number is a number.
- On a **number** field, `abc` is an error.

## Comments

`#` starts a comment that runs to the end of the line.

## Limits

A query takes at most 500 comparisons, and brackets and `NOT` nest at most 100 deep. A list in `in (...)` is one comparison however many values it holds, so put many values in one list rather than writing `a = 1 OR a = 2 OR …`. A concept part is one comparison.

## What stays a chip

These filters have no text form: regions drawn on the map, lassos, selections and similarity searches. Their values are ids, polygons or tokens rather than something to type. `format` returns `null` for each of them, and for a concept filter whose concept no longer exists, and they stay stages of their own. Their chips have no card: a click on a drawn map region, a lasso, a selection or a search toggles it between including and excluding what it holds. A named region (`location within "US-KS"`) has a text form, so it is a part like any other.

A concept's chip has no card either: its text is for links and for typing. A concept part on its own, in a link or in a filter card's text, makes the Concepts panel's chip (**Exclude:** or **Only:** and the concept's name), which the panel changes. So does a stage that is one concept test in a view saved by the SDK (`create_view(query=...)`) or the API, when the app opens that view. Inside a larger part it is one rule of that part's chip, which has a card.

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
- `unknown concept 'equpment'`, with the suggestion `equipment`
- `'=' does not apply to the concept 'equipment'; use one of: >, >=, <, <=`

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
