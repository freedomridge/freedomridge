# Regions

Named regions are countries, states or provinces, and named groups of states, loaded from public boundary data. The
filter `location within "US-KS"` keeps the samples located in Kansas, and `location not within "US-KS"` removes them.
The assistant uses them to answer "all the images in Kansas"; anyone can use them in the filter bar's query text, in
links, in the SDK and in the API.

Regions belong to the installation that loads them. They aren't part of the install: an administrator loads a
boundary set once, and loads it again to replace it.

## The boundary set

The first set, `natural-earth-admin1`, is [Natural Earth](https://www.naturalearthdata.com/)'s 1:10m "Admin 1 –
States, Provinces", version 5.1.1. Natural Earth is in the public domain. The file is the GeoJSON published with
that version's tag in Natural Earth's vector repository:

- URL: `https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.1/geojson/ne_10m_admin_1_states_provinces.geojson`
- SHA-256: `22d0e3ad85eb3e27f17cabf8ba2d50e554fbc27a87796ff891d958185da62fb5`
- Size: 40.7 MB (a file over 64 MB is refused)

It makes 251 countries, 4,596 states or provinces and 313 groups.

## Keys and names

Every region has a key, which filters and links use, and a name, which people read. Names are not unique: Georgia is
a country and a US state.

| Kind | Key | Name |
|---|---|---|
| `country` | its two-letter ISO code (`iso_a2`, `US`), else Natural Earth's three-letter code (`adm0_a3`, `SOL`) | `admin`: the name most of its features give, unless another country has it (below) |
| `state` (a state or province) | its ISO 3166-2 code (`iso_3166_2`, `US-KS`) when that is well formed (two capital letters, a dash, one to three capital letters or digits) and no other feature has it; else Natural Earth's own code (`adm1_code`, `PHL-5543`) | `name_en`, else `name`, else its key |
| `group` (a group of states or provinces) | its country's key, a colon, and the group's name as a slug (`US:midwest`) | the `region` text (`Midwest`) |

- **A country** is the union of the features with its key, and **a group** the union of its country's features that
  share a `region` property. Both are made when the set is loaded.
- **A slug** is the name with its letters folded to ASCII (accents stripped), lower-cased, with each run of anything
  other than a-z and 0-9, ASCII or not (spaces, dashes of any kind, punctuation, symbols), made one hyphen, and no
  hyphen at either end: `Grand Est` is `FR:grand-est`, and `Îles—du  Sud!` would be `iles-du-sud`.
- **Keys match in any case** and are written in one: states and countries upper-case (`us-ks` is `US-KS`), a group's
  slug lower-case after its country (`us:Midwest` is `US:midwest`).
- **A key is unique across every set loaded on the server**, so a filter's key names one region. (Custom sets an
  administrator loads later will need keys of their own.)
- **Two features that still share a key** after the fallback are one state: their land is joined under the key, never
  dropped, and the load report lists them (`keys_merged`). 5.1.1 has none.
- **Groups exist only where the file names them.** Natural Earth's `region` column is meaningful for the United
  States (`US:midwest`, `US:northeast`, `US:south`, `US:west`) and fills in unevenly elsewhere: in 5.1.1, 47
  countries have groups (France's 18 regions, Italy's 20, Spain's 19, the United Kingdom's 16, Russia's 6 federal
  districts, Canada's 3, ...), most have none, and some are odd (a single Croatian feature in a Slovenian region;
  Serbia's 11 groups of one district each).
- **A damaged group name makes no group:** a `region` holding a `?` or the replacement character (U+FFFD) is read as
  no region for that feature. In 5.1.1 that is one feature, `VN-69` (`VNM-452`), whose `Ðông B?c` is a damaged
  copy of Vietnam's `Đông Bắc` (`VN:dong-bac`). The load report lists such features (`group_names_damaged`).
- **What 5.1.1 makes a loader decide**, and how it is decided:
  - 60 ISO codes are shared by several features (`PH-MNL` by 17, `BA-BIH` by 9): those features
    are keyed by `adm1_code`.
  - 188 ISO codes are not well formed (`-99-X16~`, `XK-X01~`): keyed by `adm1_code`. Some `adm1_code`s hold `+` and
    `?` (`ATA+99?`); quote them in query text and encode them in a URL.
  - 12 features have no `iso_a2` (`-1`), so 11 countries are keyed by `adm0_a3`: Somaliland (`SOL`), Northern Cyprus
    (`CYN`), the Akrotiri and Dhekelia bases (`WSB`, `ESB`), and others.
  - Three country keys gather features with different `admin` names: `AU` (Australia, and Ashmore and Cartier
    Islands) is Australia, `NL` (the Netherlands and the Caribbean Netherlands) is Netherlands, and `PS` (Gaza Strip,
    West Bank: one feature each) is "Gaza Strip and West Bank".
  - **No two countries share a name.** When another country key already has the name a country would get, the one
    with the most features keeps it, and the other is named from its own features: their `geonunit` when that
    differs, else its single feature's name, else its key. In 5.1.1 that is Tokelau: its feature has `iso_a2` `TK`
    and `admin` New Zealand, so `TK` is named Tokelau (its `geonunit`), and New Zealand stays `NZ`'s alone. The load
    report lists each renamed country and what the file offered (`countries_renamed`).
  - 7 features have no name (`ATA+99?`, `RUS+99?`, ...): named by their key.
  - 1 geometry is invalid and is repaired (`ST_MakeValid`).
  - `US-DC`'s English name is "Washington", like `US-WA`'s, so the name Washington names two states.
- **Keys can change between versions.** ISO codes are stable, but the fallback keys (`adm1_code`, `adm0_a3`) and the
  groups are Natural Earth's own and may change in a later release. The loaded version is recorded, and a load that
  removes keys reports them (in its job's result, on the command line, and in `GET /api/admin/regions`), so an
  administrator knows which saved views and links to update: a filter naming a key that is gone is refused, not
  emptied.

## Loading a set (administrators)

**`POST /api/admin/regions/load`** with `{"set": "natural-earth-admin1"}` starts a background job that downloads the
set's file from its pinned URL over HTTPS, checks its size and SHA-256, and loads it. The answer is a 202:
`{"job_id": "...", "state": "queued", "started": true}`. While a region load is queued or running, a second request
returns that job with `"started": false`. The job shows in the Jobs list like other jobs; its result is the load's
report: the counts of each kind, the decisions above, and `removed_keys`.

**A server with no internet** loads the same file from disk, from the repository root with the backend's
environment (`DATABASE_URL`):

```bash
python -m backend.regions load ne_10m_admin_1_states_provinces.geojson --set natural-earth-admin1
```

The file must be the pinned one. `--no-verify` loads a file the administrator vouches for; when it isn't the pinned
file, `--set-version` says which version it is, and that is what is recorded.

**Loading a set again replaces that set's regions and nothing else**, in one transaction: filters keep the old regions
until the new ones are in.

**`GET /api/admin/regions`** lists the sets this server can load and what is loaded of each:

```json
{"sets": [{
  "name": "natural-earth-admin1", "title": "Natural Earth 1:10m admin 1 states and provinces",
  "version": "5.1.1", "url": "https://raw.githubusercontent.com/...", "sha256": "22d0e3ad...", "licence": "Public domain (...)",
  "loaded": {"version": "5.1.1", "sha256": "22d0e3ad...", "verified": true, "loaded_from": "download",
             "loaded_at": "2026-11-18T10:00:00+00:00", "loaded_by": {"id": 1, "username": "admin"},
             "counts": {"country": 251, "state": 4596, "group": 313}, "removed_keys": []},
  "load_job": {"job_id": "...", "state": "running"}
}]}
```

`loaded` is `null` until the set is loaded; `loaded_by` is `null` for a load from the command line. `load_job` is
the region load queued or running for the set, so a page can follow a load another administrator started; `null`
when there is none.

## Finding regions

Both need a signed-in user, and no dataset.

**`GET /api/regions?search=kansas&kind=state&limit=20`** lists matching regions, best first:
`[{"key": "US-KS", "kind": "state", "name": "Kansas", "country_key": "US", "country_name": "United States of America"}]`.
A key equal to the text comes first, then names equal to it, names starting with it, and names containing it, all
ignoring case; ties go by kind (country, state, group), then name. `kind` is optional; `limit` is 1 to 100
(default 20). With no set loaded the list is empty.

**`GET /api/regions/US-KS`** gives the region with its set, version and bounding box:
`{"key", "kind", "name", "country_key", "country_name", "set", "version", "bbox": [west, south, east, north]}`. A
region across the antimeridian has west > east, as in GeoJSON: the United States' box is `[172.48, 18.91, -66.98,
71.41]`. The box's longitudes come from each of the region's polygons: it leaves out the widest stretch of longitude
with no polygon, so it crosses the antimeridian only when that stretch is elsewhere, and a region that covers every
longitude (Antarctica) gets `[-180, ..., 180, ...]`. With `?outline=true` it adds `outline`, a GeoJSON MultiPolygon simplified to at most 5,000 vertices with
coordinates to 5 decimals (about 120 KB at most), for drawing on a map. An unknown key is a 404 with the message the
filter gives.

## The filter

| Operator | Value | Keeps |
|---|---|---|
| `geo_region` | a region key | the samples whose `location` is inside the region, or on its border |
| `geo_region_exclude` | a region key | every other sample, samples with no location included |

```json
{"field": "location", "operator": "geo_region", "value": "US-KS"}
```

- **The field is `location`**; the value is a key in any case.
- **Islands and holes are exact:** a sample on an island of Alaska is in `US-AK`, and one in Lesotho is not in `ZA`.
  A point exactly on a border between two states is in both.
- **A key this server doesn't have is a 400**, never a filter that matches nothing, with up to three close keys:
  `Invalid input: Unknown region 'US-KZ': the boundary set isn't loaded on this server or has no such region (did
  you mean 'US-KS' (Kansas) or 'US-KY' (Kentucky) or 'KZ-KUS' (Kostanay)?)`, or, before any set is loaded,
  `Invalid input: Unknown region 'US-KS': no boundary set is loaded on this server`. A bulk edit is refused when it
  is submitted.
- **It applies everywhere a filter does:** sample queries and counts, the sidebar's values, histograms, `aggregate`,
  the map's points and extent, selections, searches, versions and their differences, and bulk edits. On a version
  it reads the locations the version stored.
- **A follower doesn't run it for a patch search its primary relays** (it refuses the filter, as it does a concept's):
  its boundary set may not be the primary's.
- **As text** it is `location within "US-KS"` and `location not within "US-KS"` ([FILTER_QUERY.md](FILTER_QUERY.md#regions)).

## In the app

- **The chip.** A filter that is one region test, `location within "US-KS"` or `location not within "US-KS"`, is a
  chip with a globe that names the region and its country: "In Kansas, United States of America", "Not in Kansas,
  United States of America" (a country by its name alone: "In Fiji"). It is the same chip however the filter came:
  typed in a filter card's Query box, a `q=` or `f=` link, a saved view, the dataset's default view, or a stage made
  elsewhere (the assistant). A click turns it around between in and not in, as a drawn map region's chip does; its ✕
  removes it. The name comes from `GET /api/regions/{key}`: while it loads, the chip shows the key. For a key this
  server has no region for, the chip keeps the key, turns amber with a ⚠ and has the server's refusal as its tooltip,
  and the grid shows the same refusal, as for any filter the server refuses.
- **Links.** A region chip has a text form, so Copy link and the address write a view of ordinary filters and region
  chips as `?q=`, which opens on the same chips (a name is written as its key). A name typed in a filter card that
  several regions share (Georgia) is a mistake shown in the card, with their keys as suggestions.
- **On the map.** While a region chip is applied, the map draws the region's outline (`?outline=true`) under the
  points, in the purple of the map's region chip: a line and a light fill for "in", a dashed line for "not in".
  Several chips draw several outlines. A region across the antimeridian (Fiji, Russia, the United States) is drawn
  without a line along it where its pieces meet. A region inside a larger filter (an OR) isn't outlined: only chips
  are. Applying a region in the page (a filter card, or a chip's filter card edited into one) moves the map as drawing
  a polygon does: to the samples inside it with the other filters (as Zoom to Data would), or, with none there, to the
  region's bounding box. Turning a chip around or removing it doesn't move the map. Nor do region chips that are
  brought back or in, whenever they arrive, as a drawn polygon brought back doesn't: a link, a saved view (opened or
  chosen in Saved Views), the dataset's default view (opened, the Default view button, Clear All), a reload, Back and
  Forward.
- **Datasets with objects.** On a dataset with objects, `not within` is coupled across the grains as a drawn
  region's exclude is: the image grid keeps the images outside the region with no object inside it, the object grid
  the objects outside it whose image isn't inside it.
- **Loading boundaries.** Administrators load a set on Settings → System status, in the Region boundaries card. It
  shows each set and what is loaded of it (version, when, by whom, the number of countries, states or provinces and
  groups), and a button that downloads and loads it. The card follows the job it starts (Loading region boundaries,
  also in Background jobs) to its result, or to its failure as the job gives it: on a server with no internet, that
  says which command loads the file from disk. A load already under way when the page opens (`load_job`: started by
  another administrator, or before a reload) is followed the same way, the button disabled until it ends. After a set
  is loaded again, the card lists the keys the load removed.

## Followers

Regions are not synced: each instance loads its own sets. A follower copies a shared view as it is, so a view
naming a region works there once the follower has loaded the same set. Until then, opening the view is refused with
the 400 above; the sync itself is unaffected. A set's regions are the same everywhere for a given version.

## Speed

Each region is also kept cut into pieces of at most 256 vertices (`region_parts`: 27,117 pieces for this set), and
listed by the quarter-degree cells it touches (`region_cells`: 936,220 cells, 40 MB), each marked inside the region
(one piece covers it) or on its border. Loading the set makes both.

- **A sample is tested on its own** wherever the filter sits: under OR or NOT, inside the cross-grain link the app
  puts around every filter of a dataset with objects, on a version's rows, in a bulk edit's units, and for an include
  ANDed with the other filters. First the region's bounding box, through the location index; then the sample's cell:
  a cell inside the region answers at once, a cell on its border tests the location against the whole region, and
  no cell means outside. The two cell sets are hashed once per statement and sized by the region, never by its
  samples: Antarctica's, the largest, take 4 MB.
- **An exclude ANDed with the other filters, in a statement that reads every row** (a count, the map, the sidebar)
  is an anti-join from the region's pieces to the samples: each piece finds its samples through the location index,
  so it costs what the region holds, not the whole dataset. A statement that stops early (a grid page, the sidebar's
  quick probe, a histogram preview's drawn rows) tests each row instead, since the anti-join reads the whole region
  before its first row.

Counts as the app runs them, in milliseconds, on 2,000,000 located samples spread over the world with denser areas
(and 1,000,000 more in another dataset). "Under OR" is the region in an OR with another condition; "grain link" is
the app's cross-grain wrapper of a dataset with objects.

| Region | Samples inside | Include | Exclude | Include under OR / grain link | Exclude under OR / grain link |
|---|---|---|---|---|---|
| `US-KS` (Kansas) | 64,025 | 62 | 276 | 71 / 863 | 950 / 950 |
| `US-AK` (Alaska: islands, across the antimeridian) | 62,981 | 210 | 258 | 246 / 733 | 745 / 798 |
| `CA` (Canada: 412 polygons) | 128,462 | 525 | 477 | 561 / 1,096 | 1,168 / 1,192 |
| `RU` (Russia) | 188,960 | 348 | 425 | 406 / 791 | 834 / 865 |
| `US` (United States) | 425,819 | 850 | 1,537 | 989 / 969 | 1,019 / 1,025 |
| `US`, in 2,000,000 samples mostly in the United States | 1,581,987 | 1,563 | 2,127 | 1,940 / 1,683 | 1,696 / 1,720 |

Before the cells, a sample was tested against the whole region, which took 5 µs inside Canada's box: Canada under the
grain link took 6.7 s, the United States 4.3 s, and 8.1 s where it held most of the dataset.

A grid page of 50 (`ORDER BY id LIMIT 50`), and its count, as the grid asks for them (ms; the rows and counts are the
same either way). "Joined" was the page of an exclude before; it now tests each row:

| Region, exclude | Samples left | Page, joined → per row (16 MB / 4 MB work_mem) | Page at row 2,000 (16 MB / 4 MB) | Count, joined (16 MB / 4 MB) |
|---|---|---|---|---|
| `US-KS` | 1,985,987 | 212 → 0.5 / 197 → 0.6 | 113 → 20 / 106 → 19 | 325 / 303 |
| `CA` | 1,921,906 | 454 → 11 / 522 → 9 | 307 → 72 / 284 → 51 | 676 / 520 |
| `US`, mostly in the United States | 417,225 | 14,469 → 7 / 12,010 → 7 | 1,130 → 34 / 1,241 → 35 | 2,718 / 2,693 |

An include's page is the same as before: 84 ms for Kansas, 0.75 s for Canada, and 2.0 to 2.5 s for the United States
where it holds most of the dataset (the planner reads the samples in the region's box and sorts them).
