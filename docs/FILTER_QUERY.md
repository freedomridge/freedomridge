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
| `is null`, `is not null` | no value (absent or null: never looked at), or a value; an empty list is a value, except in `tags` and the other [lists kept in a column of their own](#no-value-and-an-empty-list) | all |
| `is empty`, `is not empty` | a list that holds nothing, or a value that isn't one | all but the built-in fields and `location`; not a path through a list (`detections.label`: ask `detections is empty`) |

`id contains "12"` keeps the samples whose id, written in decimal, contains `12` (12, 112, 1203, ...), and
`NOT (id contains "12")` every other sample. Text that isn't digits matches no sample.

Logic: `AND`, `OR` and `NOT`, with brackets. Keywords aren't case sensitive. `concept` is a keyword only where it
starts a [concept part](#concept-scores): followed by a name (quoted, or a plain word) it is a concept's score,
followed by an operator it is a field named concept. `within` counts as an operator there, so `concept within "US-KS"`
is the [region form](#regions)'s mistake (`within` applies only to `location`).

**A sample with no value for a field never matches a comparison of that field, and never matches the `NOT` of one.**
Only `is null` and `not exists` find it. So a negation of a field (`!=`, `not in`, or a `NOT` around the field's
tests) keeps only the samples that have the field, with another value: `split != "train"` keeps the samples whose
`split` is something else, and not those that have no `split`. A `NOT` around tests of several fields keeps only the
samples that have every one of them. This is one rule for every dataset, with objects or without
([below](#on-a-dataset-with-objects) for how a dataset with objects asks it), and for every reader: the app, the SDK,
the API and the assistant.

- **A value is there and isn't `null`, and an empty list written in a field is a value**: it says the list was looked
  at and holds nothing. A list field a dataset adds in a column of its own (`dataset.create_array_field(...)`) is
  read so too: a sample never given a list there has no value, and one given `[]` has one. A sample's own tags
  (`tags`, and a mirror's `@local.tags`) have a value when they have at least one tag: every sample holds them, the
  empty list until one is written, so an empty one can't be told from one never set. So `tags not in (rejected)`
  keeps the samples that have a tag, none of them `rejected`, and `inferred_tags not in ("car")` keeps the samples
  whose `inferred_tags` is a list, empty or not, without `car`. A list of labels (`detections.label`) has a value when the list is there
  and is empty or holds a label: `detections.label != "car"` keeps the samples whose detections hold no car, an empty
  list among them, but not those whose detections have no label at all (something was found, and what it is isn't
  known), nor those with no `detections`. A `NOT` around an element match (`match_elements`, the SDK's
  `match_elements("detections", ...)`) keeps the samples whose field is a list, empty or not, none of whose elements
  meets the condition (an element without the keys the condition reads counts, and never meets it).
  `detections.label is null` shows the samples with no list, a `null` one, or detections none of which has a label,
  and `detections.label is not null` those whose list is empty or holds a label: the samples `!=` can show.
- **A value of another kind is a value.** `n != 2` keeps a sample whose `n` is the text `"abc"`, and `colour != "red"`
  one whose `colour` is the number `7`.
- **Inside a `NOT`, an absence test adds no field**: `NOT (score < 0.2 OR note exists)` needs a `score`, not a `note`.
- **A place, a lasso and a concept follow it too.** `location not within R` (and a map region or a drawn polygon
  excluded, and `NOT (location within R)`) keeps the samples that have a location outside R; a lasso excluded the
  samples that have a point on that map, none inside; `not concept "x" > 0.62` the samples that have a score, at or
  below 0.62.
- **The samples with no value are written out when they are wanted**: `split != "train" OR split is null`,
  `tags not in (rejected) OR tags is null`, `location not within "US-KS" OR location is null`.
- The built-in fields every sample has (`id`, `filepath`, `media_type`, `created_at`, `last_modified_at`) always have
  a value, so `id not in (...)` keeps every other sample.

A saved view, a link or a script keeps its filter and reads it by this rule.

### No value and an empty list

**`is null` means no value: the field was never looked at.** A sample whose metadata has no such key, or a `null`
under it, has no value. **An empty list is a value**: the list was looked at and nothing was found. So for a list in
a field, every sample is in exactly one of three: no value, an empty list, or a value that isn't one.

- `detections is null` shows the samples with no `detections` (no key, or `null`).
- `detections is empty` shows the samples whose `detections` is `[]`.
- `detections is not empty` is `NOT (detections is empty)`: the samples whose `detections` has a value that isn't an
  empty list. Like every `NOT` of a comparison it doesn't show a sample with no value.
- `is not null` shows the last two together.

A list of labels has no key of its own to look for, so on `detections.label` (and any path through a list) and on
`segmentation.labels`, `exists` and `not exists` answer as `is not null` and `is null`: an empty list exists. So they
do on a list field kept in a column of its own: `inferred_tags exists` shows the samples given a list, empty or not.

`is empty` reads any key of the metadata sample by sample: a value that is no list (text, a number) is never empty, so
`note is not empty` shows a sample whose `note` is the text `"ok"`. It is refused, with a message that says what to ask
instead, on a field that can hold no list (the built-in fields and `location`) and on a path through a list:
`detections.label is empty` says to ask `detections is empty`. A path through a list is a dotted name whose first part
the server knows as a list (the label fields, and any list field of the dataset), the same paths a comparison reads
element by element.

**A sample's own tags are the one kind of list where empty is no value**, and so are `@local.tags` on a mirror. Every
sample holds them, the empty list until a tag is written, so an empty one can't be told from one never set. There
`tags is null` and `tags is empty` both show the samples with no tags, `tags is not null` and `tags is not empty`
those with at least one, and `tags exists` those with at least one too.

A list field stored in a column of its own (`dataset.create_array_field(...)`) is a list in a field like any other:
a sample never given a list there has no value for it (`is null`, `not exists`), `[]` is a value (`is empty`), and a
`NOT` shows a sample whose list is empty. A sample an older version wrote without the field holds `[]` there (that
version stored `[]` for a sample written without it), so a `NOT` shows it and `is empty` finds it, and `is null`
doesn't. A dataset's samples from before it added the field are never set:
see [SDK.md](SDK.md#list-fields-in-a-column-of-their-own).

## Concept scores

A [concept](SDK.md#concepts-learned-scores) scores every sample from 0 to 1. A concept part keeps or hides the
samples by that score, as the Concepts panel's **show only** and **exclude** do:

- `concept "equipment" > 0.62` keeps the samples whose score for the dataset's concept named `equipment` passes the
  comparison: `>`, `>=`, `<` or `<=`.
- `not concept "equipment" > 0.62` hides them, as the panel's exclude does, and keeps only the samples that have a
  score, here those at or below 0.62, on every dataset, with objects or without
  ([negation](#operators); [below](#on-a-dataset-with-objects)). A sample the concept has not scored yet (one added
  since its scores were last computed) is hidden too until it is scored. `NOT (concept "equipment" > 0.62)` is the
  same filter. A `NOT` around more than the one concept part, such as
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

- **`location within R`** keeps the samples located inside R; **`location not within R`** keeps the samples located
  outside R, and not those with no location ([negation](#operators); on a dataset with objects, see
  [below](#on-a-dataset-with-objects)). They are the filters `geo_region` and `geo_region_exclude`.
- **`location is null`** (and `is not null`, `exists`, `not exists`) asks whether a sample has a location, so
  `location not within "US-KS" OR location is null` keeps the samples outside Kansas and those with no location.
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
  dataset with no such field, `location = ...` is a mistake that points to `within` and to `is null`. `within` applies
  only to `location`.

## On a dataset with objects

On a dataset with object rows (each naming its image in `parent_id`), an image and its objects hold different fields:
the images have `colour`, say, and the objects `label`. The dataset's page reads a filter on an image together with
its objects, and on an object together with its image, and never asks a row about a field it has no value for (a
value is there and isn't `null`). The assistant reads filters the same way. A filter sent without the page's wrapping
(from the SDK, say) reads each sample on its own values, as on a dataset without objects, by the same rule
([negation](#operators)).

- **A test is met when either row meets it.** The images' grid shows an image when the filter is met by its own
  values together with those of one of its objects, or by its own values alone. So tests joined by `AND` are met with
  the same object: `colour = "red" AND label = "car"` shows the red images that have a car, and `label = "car" AND
  score > 0.9` needs one object that is a car with that score. The objects' grid shows an object when the filter is
  met by its own values together with its image's: there `colour = "red" AND label = "car"` shows the cars of the red
  images. An object with no image has only its own values, and so has an image with no objects. The two bounds of a
  range (`between`, a slider, a histogram's bar) are one test, met by one row's value. `is not null` and `exists` are
  tests like any other.
- **A negation keeps only the rows that have the fields it compares**, as on any dataset; what is particular here is
  which rows are asked. `NOT (X)` shows a row when the row has a value for every field compared inside X, and X
  doesn't show it (X read as above, with one object). On the images' grid both are asked of the image as a whole, its
  own values and those of any of its objects; on the objects' grid, of the object with its image. `colour != "red"`
  is `NOT (colour = "red")`, `label not in (car)` is `NOT (label in (car))`, and `not concept "equipment" > 0.62`
  keeps only the rows that have a score. So on the images' grid `label != "car"` shows the images whose objects are
  labelled and none is a car; `NOT (label = "car" AND score > 0.9)` hides an image that has a car with that score,
  and shows one whose car scores lower and whose truck higher; and a field that both kinds have hides a row when
  either it or its partner has the value.
- **`is null` and `not exists` show the rows with nothing for the field**: no value for `is null` (absent or `null`;
  an empty list is a value), no key for `not exists` (a `null` under the key counts as there). On the images' grid
  neither the image nor any of its objects may have one; on the objects' grid, neither the object nor its image.
  `is empty` is a test like any other, and `is not empty` its `NOT`: on the images' grid `parts is empty` shows an
  image whose own `parts` is `[]` or that has an object whose `parts` is, and `parts is not empty` an image that or one
  of whose objects has a value for `parts`, where neither the image nor any of its objects has an empty list. On
  `location` these tests are
  about the sample's own location, the one a place tests, read the same way: on the images' grid `location is null`
  shows the images that have no location and none of whose objects has one. The built-in fields (`id`, `filepath`,
  `media_type`, `created_at`, `last_modified_at`) are on every row, so there `is null`, `is not null`, `exists` and
  `not exists` are answered by the grid's own row.
- **A `NOT`, an `is null` or a `not exists` inside a larger filter is read first, for the row as a whole**, and the
  rest as above. So on the images' grid `label = "car" AND NOT (occluded = true)` shows the images that have a car
  and no occluded object, and `label = "car" AND score is null` the images that have a car and in which neither the
  image nor any object has a `score`. A car that is not occluded is a question for the objects' grid, where the same
  filter shows each car whose `occluded` is set and isn't true.

Five images: 1 red with a car and a truck; 2 red with a truck; 3 blue with a car; 4 with no `colour`, with a car; 5
red with no objects.

| Filter | Images' grid | Objects' grid | Why the other images are out |
|---|---|---|---|
| `colour = "red" AND label = "car"` | 1 | the car of 1 | |
| `colour != "red"` | 3 | the car of 3 | 4 has no `colour` |
| `NOT (colour = "red")` | 3 | the car of 3 | the same filter as `!=` |
| `label != "car"` | 2 | the trucks of 1 and 2 | 1, 3 and 4 have a car; 5 has no `label` |
| `NOT (label = "car")` | 2 | the trucks of 1 and 2 | the same filter as `!=` |
| `NOT (colour = "red" AND label = "car")` | 2, 3 | the truck of 1, the truck of 2, the car of 3 | 1 is red with a car; 4 has no `colour`; 5 has no `label` |
| `colour is null` | 4 | the car of 4 | |
| `label is null` | 5 | none | |

- **An excluded place or lasso** (`location not within`, a map region or a lasso excluded) is the `NOT` of the place:
  an image shows when it or one of its objects has a location (a point on the lasso's map) and none of them is
  inside, and an object when it or its image has one and neither is inside. It reads the same as a chip of its own,
  sharing a filter card with another test, inside an `OR`, or written as `NOT (location within "US-KS")`.
- **A hiding test of a field every row has**, `id`, `filepath`, `media_type`, `created_at` or `last_modified_at`
  (`id not in (...)`, `filepath != "..."`), or of a list (`tags`, or a list field kept in a column of its own), is
  answered by the grid's own row: the row it names isn't shown, whichever row meets the rest of the filter. A list's
  needs a value there, as everywhere: `tags not in (rejected)` shows the rows of the grid that have a tag, none of
  them `rejected`, and `inferred_tags not in ("car")` the rows whose own list is there, empty or not, without `car`.
  `is not empty` of a list field (the `NOT` of `is empty`) is a hiding test of it too; its `is empty`, `in`,
  `contains`, `is not null` and `exists` are met by the row or a partner, and its `is null` and `not exists` are read
  for the row as a whole, as tags' are. `NOT (tags in (rejected))` is the same filter as `tags not in (rejected)`. The app's collapse of
  duplicates is read the same way: a duplicate image it hides doesn't come back through its objects, and on the
  objects' grid an object is hidden only when it is a duplicate itself, whatever its image. A `NOT` around tests of
  the built-in fields alone reads each row on its own: on the images' grid `NOT (filepath contains "x")` also shows
  an image whose path holds `x` when one of its objects' paths doesn't.

### A field's values

The sidebar lists a field's values, its range, and suggests values as you type. On a dataset with objects these come
from the rows that have the field for the view, of either kind, read as the grids read a row with its partner:

- **On the images' grid**, a field the objects have lists the values of the objects whose pair, the object and its
  image, meets the filter: each test met by the image or by that object, a `NOT` read for the image as a whole. So
  under `label = "car"` the picker of `occluded` offers the cars' values, not a truck's in the same image; under
  `NOT (label = "car")` it offers the values of every object of the images that have no car; under `colour = "red"`
  those of the objects of the red images. An object with no image is no image's partner, so its values are not
  listed there.
- **On the objects' grid**, a field the images have lists the values of the images of the objects shown. An object
  with no image is one of that grid's rows, so its own values are listed.
- **A field both kinds have** lists both: an object's value shows its image, and an image's value its objects.

Each value offered, ticked, shows at least one row; and when the list says it is complete, no value that would show
a row is missing from it. The view is read as it is sent, on every dataset: a rule of the field itself narrows its
values too, since a tick is applied as a stage beside it (with `label = "car"` applied, the picker of `label` offers
`car`). The page leaves out the field's own ticks that aren't applied yet, so several values of one field can be
ticked together.

The list and the picker read two views, by the page's choice; they differ by the ticks and by the ticked grid rows.
The list and the range read the applied filters with the four selections waiting to be applied (with fast mode off: a
brushed set, the grid rows you ticked, a drawn polygon, a lasso), so ticking rows shows their values. The picker reads
the applied filters with what the grid previews (the brushed set, the polygon, the lasso; not the ticked rows, which
the grid doesn't filter by, so a value outside them still shows rows) and the other fields' ticks.

Which of the two you are looking at: the picker opens on the field's list. With nothing typed its dropdown shows the
list's values (the list's view: no ticks, the ticked grid rows in), and a term you type that a complete list holds is
matched in the page, from that list. The server is asked, under the picker's view, for a term the list does not hold,
or when the field has no list. So with grid rows ticked the dropdown opens on the ticked rows' values, while a typed
term the list does not hold is searched in the whole view.

A range the view has no value for is no range, on every dataset: the answer's `min` and `max` are null, the view's own
answer (not the dataset's range, and not labelled as kept), and the sidebar shows "No compatible range available" for
the field. The dataset's range is kept only where the reading ran out of time or failed, or where fast mode didn't
compute the field for the filter.

**The number beside a suggestion** counts the rows that have the value among the rows read: on the images' grid, the
number beside an object's field's value counts objects, not images; on the objects' grid, the number beside an image's
field's value counts images, each once however many of its objects are shown; for a field both kinds have, both. Equal
numbers come in the order of the value, byte by byte. The sidebar's list itself shows no number.

**A field too varied to count in time** (an identifier, a path, a URL: nearly every value different) is still
answered. When counting runs past its 2 s, the server reads the values of up to a thousand of the view's rows without
counting them, under 1 s of its own: if those were all the rows, the answer is the counted one; otherwise it offers
some of the values (up to a hundred, in the order of the value) without numbers. If that runs out of time too, the
statistics' kept list answers, if they keep one. The picker says which: the values with "Could not search all values
in time: these are some of them", or, with none, "Could not search all values in time. Type a value and press Enter to
filter by it"; a typed term still filters (Enter, or Contains). A list that is complete and empty reads "No value for
this view." (with a term, "No matches").

Not read this way yet: the histogram panel of a field the other kind has (its buckets are empty there, and its
"missing" counts every row), and the sidebar's "(no value)" tick, which is offered whenever some sample of either kind
lacks the field.

### What the server skips

The server notes, for each field of a dataset, whether images and whether objects have been seen with it: its key on a
sample of that kind (a `null` under the key counts), a mirror's own `@local.` field included, and for `location` the
sample's location too (the field list's `on_images` and `on_objects`,
[Dataset management](DATASET_MANAGEMENT.md#which-kinds-of-sample-have-a-field)). Every reader of the page's filter
asks no row of a kind about a field that kind has never been seen with: no test of that row, no lookup of its partner.
That is the grids' pages and counts (the top bar's included), the sidebar's values and ranges, the histogram, the
aggregates, autocomplete, the map's points and extent, Select all, sparse sampling, the export, bulk edits by filter and
the assistant's tools. A bulk edit reads the note when it is submitted; a part of it that runs after a kind got its
first value of a field it skips runs without the note.
So a field that only one kind has costs about what it costs on a dataset without objects, give or take the join to the
partner the reading needs (an image field asked on the objects' grid). An answer never depends on it: a row with no
value for a field never meets a test of it (above), the note never says a kind lacks a field one of its samples has,
and a filter read while a kind gets its first value of a field is read again. A concept's scores, a lasso's points and
the built-in fields and lists are always asked. A dataset from before this was added is examined once by the job runner
after the upgrade; until then nothing is skipped.

## Values

- **Text:** `"double"` or `'single'` quotes, with `\"`, `\\`, `\n` and `\t` escapes. A plain word needs no quotes: `split = train`. `and`, `or`, `not`, `in`, `contains`, `exists`, `is`, `null`, `between` and `like` always need quotes. `within` is a keyword only after `location`: a field or a value named within needs none. `concept`, a keyword only before a concept's name, doesn't need them either, and `empty`, a word only after `is`, neither.
- **Numbers:** `12`, `-1.5`, `1e3`. A number too large for a double, such as `1e999`, is an error: `'1e999' is out of range for a number`.
- **Dates:** ISO text, quoted or not: `created_at >= 2026-01-01T00:00:00Z`. Text that isn't an ISO date and time is an error.
  On `created_at`, `last_modified_at` and any field declared `datetime`, dates compare as moments:
  `2026-01-01T10:00:00+02:00` is the same moment as `2026-01-01T08:00:00Z`, and a date or time written without an offset
  is UTC. A value written to the day, minute or second means that whole unit: `captured_at = 2026-01-01` is the whole
  UTC day, `captured_at <= 2026-01-01T10:00` ends where 10:01 UTC starts, and `captured_at > 2026-01-01` starts on 2
  January UTC; `in` and `not in` take each value's unit too, and a value written with a fraction of a second is that
  exact moment. One rule for every datetime, `created_at` and `last_modified_at` included. A sample whose value isn't a
  date and time never matches a range, and `!=` and `not in` include it (it has a value, of another kind). To filter by
  the clock time where a picture was taken, store that time as its own field without an offset (see
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
- `'is empty' can't be asked of 'detections.label', a path through the list 'detections': ask it of the list,
  detections is empty`, with the suggestion `detections`

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
