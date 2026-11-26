# The assistant

The assistant lets an AI client answer questions about this server's datasets: "which datasets have drone images
from last spring?", "what does the field `digital_gain` mean?", "how many images in Kansas carry one of these serial
numbers?". The client (Claude Code, or any other client of the protocol below) calls a fixed set of tools on the
server. Each tool reads the data **as the person using the client**: it sees the datasets that person can open, at
their level, and nothing else. No tool changes anything.

The client reaches the tools through the **MCP server** at `/mcp`. MCP, the Model Context Protocol, is how an AI client
calls tools on a server. The MCP server is the assistant's one way in for now (its door); a chat inside the app will be
a second.

## What switching it on means

- **This server calls no AI service and needs no AI account or key.** It only answers the tools' questions.
- **Each person's own AI client sends what the tools answer to that client's AI service**: Anthropic for Claude Code,
  another company for another client. That is dataset and field names and descriptions, counts, the values of fields
  not kept back, sample ids and links, and, with **The assistant may read samples' fields** on, samples' fields and
  file paths.
- So switching the MCP server on lets the people you choose send what they can already see in their datasets to the
  AI service their client uses. Nothing more: a person reads through the assistant only what they can open in the
  app.
- **What narrows it:** who may use it ([Who may use it](#who-may-use-it)); closing a dataset to the assistant
  ([Closing a dataset](#closing-a-dataset)); keeping a field's values back
  ([Keeping a field's values back](#keeping-a-fields-values-back)); and samples' fields, which it doesn't read unless
  an administrator allows it.
- **What it never does:** write, delete or share anything. Every tool only reads.
- **Administration tools** let an administrator's AI client read the server's status, background jobs, backups and
  index advice, behind a switch of their own, off as shipped ([Administration tools](#administration-tools)).

## Getting started

An administrator, once:

1. Set the server's public address, for example `FRIDGE_PUBLIC_URL=https://data.example.org`, and restart the
   backend. In production it goes in the same `.env` as `ALLOWED_ORIGINS`, and `docker-compose.prod.yml` passes it to
   the backend. The assistant's answers carry links into the app, and every link starts with this address
   ([Links](#links)).
2. Open **Settings → Assistant** and switch on **MCP server**. It ships off. Once it is on, the page links to the
   tokens page with the MCP kind chosen (step 5).
3. On the same page, say who may use it ([Who may use it](#who-may-use-it)). As shipped, only administrators may.
4. Places ("the images in Kansas") need region boundaries. On a Docker install they are ready a few minutes after the
   first start: the server loads them by itself. A source checkout loads them once `python -m backend.regions fetch`
   has run ([regions](REGIONS.md#loading-a-set)). **Settings → System status → Region boundaries** shows what is
   loaded.

Each person who uses it (a power user or an administrator: a guest can't use the MCP server, whatever the settings
say, and makes no tokens):

5. Open **Settings → API Tokens**, click **Create New Token**, give it a name, choose **Used by: An AI client (MCP)**
   and click **Create Token**. That kind is offered only to someone who may use the assistant right now. The token is
   shown once.
6. Copy what **Connect an AI client** shows: the address, and the header to send with every request
   (`Authorization: Bearer <token>`). For Claude Code the page shows the command to run:

   ```bash
   claude mcp add --transport http freedomridge https://data.example.org/mcp \
     --header "Authorization: Bearer <token>"
   ```

7. Check that it connected: `claude mcp list` shows `freedomridge` as connected, and inside Claude Code `/mcp` lists
   it.
8. Ask a first question, such as "Which datasets can you see on freedomridge?" (the client calls `list_datasets`),
   then "Describe the field-trials dataset" (`describe_dataset`), then "How many of its images are in Kansas?"
   (`find_place`, then `make_view`). Without region boundaries loaded, the assistant says it can't find Kansas as a
   place; a field that names the state still works (`state = "Kansas"`).

Two things to know before you start:
- An answer about images comes with a link that opens them in the app. Some links work only for you, for 30 days
  (they say so): save the view in the app to keep or share it.
- Administrators see a log of your calls: your name, each tool you called, the dataset and when. Never what you asked
  or what came back ([The call log](#the-call-log)).

### If it doesn't work

- **401 from the server:** the token isn't an MCP token, it expired (tokens last 90 days unless made otherwise), or it
  was revoked. Make a new MCP token.
- **403, "The MCP server is switched off on this server" or "Your account may not use the assistant on this
  server":** ask an administrator.
- **No "An AI client (MCP)" kind on the tokens page:** you may not use the assistant right now. Ask an administrator.
  A guest can't use the MCP server, and makes no tokens at all.
- **`claude mcp list` shows it failed to connect:** check that the address ends in `/mcp`, and that the header is
  exactly `Authorization: Bearer <token>`.

## Connecting an AI client

The MCP server is at `/mcp` on the app's own address: `https://YOUR_DOMAIN/mcp` in production, and in development
`http://localhost:8000/mcp`, or `http://localhost:3000/mcp` through the frontend's server. It answers clients of
protocol revisions 2026-07-28, 2025-11-25 and 2025-06-18 over streamable HTTP. Any client that can add a remote
(streamable HTTP) MCP server with a custom header can connect: give it the address and the header.

### Claude Code

The command in [Getting started](#getting-started) adds the server for you, in the current project (Claude Code's
default "local" scope). It leaves the token in your shell's history, and Claude Code keeps it in plain text in
`~/.claude.json`, as it does any header. To keep the token out of both, put the server in the project's `.mcp.json`
by hand and let Claude Code read the token from your environment:

```json
{
  "mcpServers": {
    "freedomridge": {
      "type": "http",
      "url": "https://YOUR_DOMAIN/mcp",
      "headers": {"Authorization": "Bearer ${FRIDGE_MCP_TOKEN}"}
    }
  }
}
```

Set `FRIDGE_MCP_TOKEN` in the shell that starts Claude Code. Claude Code asks before it uses a server from a
project's `.mcp.json` for the first time.

### The MCP token

An MCP token is a kind of its own. A token pasted into an AI client sits in that client's configuration, and the
model and its tools work with it. So an MCP token opens `/mcp` and nothing else: sent to any other part of the API it
is refused as a token that doesn't exist. And `/mcp` takes only MCP tokens: an ordinary API token, a worker or
follower token, or a browser's signed-in session gets 401, so a web page can't drive `/mcp` through someone's
browser. A request from a web page of another origin is refused (403) before anything else.

An MCP token is made, listed, revoked and expires like any other token (`"scope": "mcp"` on `POST /api/tokens`).
Power users and administrators make tokens. A guest can't use the MCP server, whatever the assistant's settings say:
the server refuses them, so a token made before someone became a guest is refused (403, "Your account may not use the
assistant on this server"). Made a power user again, the settings decide for them as before. Deactivating a person, or
resetting their password with **Also revoke their API tokens**, ends their MCP tokens with the rest. The server never
passes the token on: no tool carries it, and it is in no log. Anyone holding the token can read what you can read
through the assistant, so revoke it on the tokens page when it is no longer needed.

### Behind your own web server

The bundled nginx routes `/mcp` to the backend. If you keep your own web server or load balancer in front:
- route `/mcp` (exactly that path) to the backend, as you route `/api/`;
- pass the `Authorization` and `Origin` headers through unchanged (the server refuses an origin that isn't one of its
  own);
- allow request bodies of at least 256 KB;
- don't buffer the answers (nginx: `proxy_buffering off`): a call that runs a while is answered as a stream that sends
  a keepalive every 15 seconds, in every protocol revision the server speaks;
- allow reads of at least two minutes (a call stops at 60 seconds; the bundled nginx allows 10 minutes, as for
  `/api/`).

See also [Docker deployment](DOCKER_DEPLOYMENT.md#your-own-web-server-in-front). The server keeps nothing between
requests (no MCP session), so several backend processes or machines behind one address can each answer any
request.

## Switching it on, and who may use it

The assistant is off on a new server and on a server upgraded to this release. While it is off, every tool call is
refused with "The MCP server is switched off on this server". **Settings → Assistant** holds every setting:

- **MCP server**: the switch.
- **Roles**, **Groups whose members may use it**, and **People**: who may use it (below).
- **New datasets start closed to the assistant** ([Closing a dataset](#closing-a-dataset)).
- **The assistant may read samples' fields**: off as shipped, when `get_samples` gives only samples' ids and links.
  On, the model reads each sample's fields (their values, private fields left out) and its file path, which can show
  where files are kept, such as a bucket's and its folders' names.
- **Administration tools**: off as shipped ([Administration tools](#administration-tools)).

The settings belong to this server: nothing of them is synced to followers or from a primary.

### Who may use it

While the MCP server is switched on, in this order:

1. A person with an answer of their own gets that answer: **May not use it** keeps out someone their role or group
   would let in, and **May use it** lets in someone their role and groups would not.
2. Otherwise, a person in at least one ticked group may. A group only ever lets people in.
3. Otherwise their role decides (**Roles**: administrators and power users). As shipped: administrators yes, power
   users no.
4. A guest never may, whatever their own answer, their groups or the setting for guests say. That is a rule of the
   server, not only because guests make no tokens. Guests will have the assistant through the chat in the app: the
   setting for guests (`roles.guest`, which the page doesn't show), a guest's own answer and groups with guests in
   them are kept for it.
5. A demo account (the shared account of a public demo) never may, nor a follower instance's account, whatever the
   settings say.
6. An account that is inactive, or that owes a password change, never may.

The **People** table shows each person's own answer and whether they may use it now. A guest's row says **No**, and
their own answer can't be changed there. Deleting a group or a person removes their entry. The rule is checked again
on every tool call and on every request a tool makes: a person deactivated, made a guest, or taken off the list,
between two calls is stopped at the second, and a request to `/mcp` from such a person is refused (403, saying why).

### Through the API

Administrators only. `GET /api/admin/assistant` reads the settings, and `PATCH /api/admin/assistant` changes any of
them (a key left out doesn't change):

```json
{
  "mcp_enabled": true,
  "roles": {"admin": true, "power_user": false, "guest": false},
  "groups": [{"id": 3, "name": "agronomy"}],
  "users": [{"id": 12, "username": "sam", "allowed": false}],
  "new_datasets_closed": false,
  "sample_fields": false,
  "admin_tools": false
}
```

`PUT /api/admin/assistant/groups/{group_id}` ticks a group and `DELETE` takes that back.
`PUT /api/admin/assistant/users/{user_id}` with `{"allowed": true}` or `{"allowed": false}` sets a person's own answer,
and `DELETE` removes it. An unknown group or person is 404. Each answers the whole settings. `roles.guest`, a guest's
own answer and a group's guests are kept for the chat in the app: none of them lets a guest use the MCP server.

## What the assistant may read

The assistant reads what the person using it can open. Three things narrow that further.

### Closing a dataset

A dataset's manager can close it to the assistant: in its **Dataset Settings → General → Access**, switch off **The
assistant may read this dataset**, or send `PATCH /api/datasets/{id}` with `{"assistant_closed": true}` (manage level;
below it, "This needs manage access to the dataset" and nothing changes). Every answer that describes a dataset
carries `assistant_closed`.

For a closed dataset every tool answers "The assistant may not read this dataset", however it comes to it (by its id,
through one of its saved views, through a job or a search), and the tools that list datasets leave it out. That holds
for administrators too. Closing changes nothing outside the assistant: the same people open the dataset in the app and
through the API as before.

A new dataset is open, unless **New datasets start closed to the assistant** is ticked: then each dataset created
afterwards starts closed. Existing datasets are not changed by that setting, nor by the upgrade (they stay open). On a
mirror of another server's dataset ([followers](FOLLOWERS.md)) the switch is this server's own: it can be set there,
and a sync from the primary never changes it.

### Keeping a field's values back

Someone with edit in a dataset can keep a field's values from the assistant: in **Dataset Settings → Fields**, tick
**Keep this field's values from the assistant** under the field, or send
`PATCH /api/datasets/{id}/fields/{field}/description` with `{"values_private": true}`
([describe a field](DATASET_MANAGEMENT.md#describe-a-field-edit)). A tool may then say the field exists, what it means,
how many samples have it and how many distinct values it has, and never a value of it, nor its smallest or largest.
A field's label list counts as its values too: keeping `detections` back keeps `detections.label` back. The app still
shows the values to the people who can open the dataset. On a mirror the mark is this server's own, for every field,
the primary's included. Anyone with edit level in the dataset can set the mark, and can clear it again: an
administrator relying on it should know an editor can undo it.

**A private field can't be questioned either.** Hiding values in answers isn't enough: a model that may count
`salary > 100000` finds every value by halving. So a tool may test a private field only for having a value (`exists`,
`not_exists`), and for being, or not being, one of the values of a value list the server keeps, which the model holds
only a handle to. Comparing it with a value the model wrote is refused, even for equality, and so are ranges,
`contains`, sorting by it, a histogram of it, and its minimum, maximum, mean or quantiles. The refusal says why and
what works instead, in the same words whatever value was asked about. People in the app are not affected.

What a model can still learn about a private field, by design:
- that it exists and what it means;
- how many samples have it and how many different values it has;
- how many samples each of its (unnamed) values has, and which samples share one;
- how many samples a value list of it selects.

Values kept back are kept from the tools, not from the person's own credentials: a client holding the person's
ordinary API token reads every value through the API, as it always could, and the person's own pages (a working
view's `?w=` link, a value list's download) show them the values. How the values are kept back is described for
developers at the end.

### What people wrote reaches the model as data

The tools hand a model what people wrote, as they wrote it:
- dataset names and descriptions;
- field names, descriptions and units;
- concept names;
- saved views' names, descriptions and slugs;
- region names (those of the loaded boundary set);
- the values of every field not marked private, tags included.

Two things follow:
- **Don't write a value that must stay private into any of these.** The private mark keeps one field's values out of
  what the tools compute and answer; it can't find them in a name, a description or a slug.
- **These texts are untrusted input to the model.** Anyone with edit level can write "ignore your instructions" into a
  description, and a model reads it in a tool's result. A client must treat tool results as data. Nothing on this
  server acts on them: every tool only reads, and the words of a view's stages are the server's own.

## Limits and settings

| What | Value | Past it |
|---|---|---|
| `FRIDGE_PUBLIC_URL` (`.env`; the production Compose file passes it on) | the address every link starts with | unset: the first `ALLOWED_ORIGINS` entry (which must then be the public HTTPS origin), else `http://localhost:3000` |
| `FRIDGE_ASSISTANT_CALLS_AT_ONCE` (`.env`) | 4 tool calls of one person at once, in each server process | the next is answered `busy` at once ("You have 4 assistant calls running already…"), retryable |
| `FRIDGE_ASSISTANT_CALLS_PER_PROCESS` (`.env`) | 16 tool calls of everyone at once, in each server process | the next is answered `busy`, retryable |
| One tool call | 60 seconds | stopped, its work cancelled (its requests and their queries); the model is told "This call took longer than 60 seconds and was stopped. Ask for less at once." (`timeout`) |
| One database query made for a tool | 45 seconds, for every person (a guest's own limit when shorter) | `too_slow`, its message saying what ran out and what to do ([What is quick and what is slow](#what-is-quick-and-what-is-slow)); a number's `field_summary` whose second read runs out answers its first read's numbers with a `note` |
| One request to `/mcp` | 256 KB, and one call (several calls in one request, a JSON-RPC batch, are refused) | 413 (a batch: 400) |
| Working views and value lists | kept 30 days after their last use | removed by the server's housekeeping, every five minutes |
| Working views per person | 2,000 | making one more removes their least recently used |
| Value lists per person | 500 | the least recently used go; a list a kept view uses stays as long as the view, and counts |
| Stages in one view | 50 | `invalid`, no view made (removing or negating stages is never refused) |
| Values in one view's lists | 50,000, a list counted once per stage that uses it | `invalid`, no view made |
| Values in one kept list | 10,000 | `"list": null` and a `list_note`; `distinct` and the download still have them all |
| Filter text (`filter`, `add`) | 20,000 characters | `invalid` before anything is parsed: a long list of values goes in a value list |
| Other text arguments | names and searches 200 characters, `search_similar`'s text 1,000, a concept's name 1,000, a field's name 255, a handle 100 | `invalid` |
| A `?q=` link | 6,000 characters | a `?w=` link instead |
| An error's message | 2,000 characters; `suggestions` 20 of at most 200 | cut, ending with how many characters were left out |
| One answer | about 19 KB for a view's stage texts; a page of long entries | long stage texts shown cut with a `note` (the view keeps them whole); a page holds fewer than its `limit`, `next_offset` says where the rest start |
| The call log | rows kept 30 days | removed hourly by the job runner |

Every text argument's longest length is also in the tool's schema (`maxLength`), so a client sees it. Set the three
`.env` values before the backend starts. A server process is one running copy of the backend; the production image
runs 4 of them behind one address. The call counts are kept in each, with nothing shared between them, so in
production one person can have up to 16 calls running at once (4 in each) and the server up to 64 (16 in each). The
60 seconds hold for every call wherever it runs.

**The first search by words in each server process loads the text model**, as the app's own search by words does: a
download of a few hundred MB the first time a server needs it, then a load from disk, once per process. Until it has
loaded, that first `search_similar` with `text` takes as long as the load, and it can run into the 60 seconds. A call
stopped then doesn't stop the load, which finishes in its own thread, so the next call in that process finds the model
loaded. To warm it, an administrator runs a search by words in the app after a restart; each process loads its own
copy, so with four processes the first few searches may wait. A server that can't load the model (no internet for
its first download, or no text model installed) answers `invalid` in its own words, and a search by samples still works.
A failed load is remembered for a minute in each process: a search by words in that minute gets the same answer at once,
without trying again, and the first one after it tries once more.

## What is quick and what is slow

The assistant's instructions tell the model this, and `describe_dataset` marks each field `indexed: true` or `false`:

- A filter on an indexed field with `=`, `in` or a narrow range is quick at any size; on a list field, `in` (has any
  of these values) is, and `=` is not.
- A filter on a field that is not indexed, a `contains`, or a range that keeps a large share of the samples reads every
  sample of the dataset: seconds on millions of samples, and it can run out of time on a very large one. An indexed
  condition in the view first narrows what the rest reads.
- `field_values` and `field_summary` read every sample of the view, whether or not the field is indexed: an index makes
  a filter quick and does nothing for counting a field's values. Their time grows with the view's count, so on a large
  view the model narrows it first. For a whole dataset, `describe_dataset` already gives each field's range, or
  examples of its values, from the statistics, without reading the samples.

**`indexed`.** A metadata field is indexed when the dataset has an index on it: what the app shows as an indexed field
(`dataset.promote_field` in the SDK makes one). A list field the server keeps in a column of its own (`tags`, a
registered array field) is indexed for `in`, which its index serves; `=` on a list tests each sample's list, which no
index serves. The fields every dataset has, by the indexes of the samples table:

| Field | `indexed` | Why |
|---|---|---|
| `id` | true | the `(dataset_id, id)` key |
| `filepath` | true | the `(dataset_id, filepath)` unique index |
| `created_at` | true | the `(dataset_id, created_at)` index |
| `media_type` | true | the `(dataset_id, media_type)` index |
| `last_modified_at` | false | no index (one would slow every edit) |
| `tags` | true | its index serves `in` (has any of these values); `=` tests each sample's list and isn't served |

A registered array field is like `tags`: true, for `in`.

**`too_slow`.** Every database query made for a tool has a time limit ([Limits](#limits-and-settings)). When one runs
out, the tool answers `too_slow` (not retryable), never a partial count, and its message says what ran out:

- From `field_values` and `field_summary`: the tool reads every one of the view's N samples (the view's exact count,
  which the tool makes first), whether or not the field is indexed, so narrow the view first. When that count is
  itself what ran out, the message says the same without a number. It never advises an index, which would not help.
  A number's or a datetime's summary whose second read runs out answers instead ([Working views](#working-views)).
- From `make_view` and `refine_view`, and from `get_samples` when it sorts or filters: when the filter (or the sort)
  uses fields that are not indexed, it names them, says every sample of the dataset was read for them, and says to
  narrow the view by an indexed field first. It adds "or ask an administrator to index" a field only where an index
  would serve the test: a field tested with `=`, `in` or a range, or sorted by (a `contains`, `exists` or
  `not exists` reads every sample whether or not the field has an index). A list field tested otherwise than with `in`
  (with `=`, say), or sorted by, is named the same way, with the advice to test it with `in`. When it uses
  none, it says to narrow the view, since a `contains` or a wide range reads every sample all the same. Only the
  view's stages and the sort count, never the condition the tools add to leave object rows out. A private field's
  name may appear, never a value.
- From any other tool: "Counting this took longer than the server allows for one statement, so there is no answer, not
  even a partial one. Narrow the view first."

**Measured**, before and after the counting tools were changed to read the view once (twice for a number's summary):
the tools called in process with no time limit, median of three runs, in seconds. 4,000,000 image samples with about
1.5 KB of metadata each (68 fields), each measured field in an indexed copy and a plain copy with the same values,
PostgreSQL 18 with the repository's `postgresql.conf` (4 parallel workers, `work_mem` 128 MB), warm. A tenth is the
400,650 samples with `state = "Kansas"`. The answers before and after are the same, but for a mean's last digits.
The production Compose file's default `work_mem` is 16 MB, where a field with nearly every value different spills more
of its values to disk (only the values and their counts): a review measured `field_values` on such a field of
1,000,000 samples at 1.9 s with about 190 MB of temporary data, at 16 MB.

| Tool and field | Whole dataset, indexed | Whole, plain | A tenth, indexed | A tenth, plain |
|---|---|---|---|---|
| `field_values serial_number` (1,700 values) | 11.4 → 1.8 | 25.5 → 1.8 | 3.8 → 1.1 | 3.8 → 1.1 |
| `field_values state` (70 values) | 10.1 → 1.6 | 23.9 → 1.8 | 2.1 → 1.0 | 3.3 → 1.1 |
| `field_summary brightness` (a number, nearly every value different) | 48.9 → 9.1 | 47.4 → 9.0 | 7.8 → 2.4 | 7.7 → 2.4 |
| `field_summary digital_gain` (a number, about 250 values) | 38.2 → 4.8 | 36.7 → 5.2 | 7.0 → 2.1 | 6.9 → 2.2 |
| `field_summary serial_number` (text) | 11.2 → 1.9 | 25.7 → 1.9 | 4.0 → 1.1 | 4.0 → 1.1 |

The index makes no difference to either tool, as the rules above say. A number whose values are nearly all different
costs the most: its exact distinct count groups 4 million different values. On the same kind of server a count over
`=` on an indexed field took 0.01 to 0.02 s, and on a plain field about 1 s.

## The tools

Every tool reads. A tool answers `{"ok": true, "result": ...}` or `{"ok": false, "error": {"code", "message",
"retryable"}}`. The codes:

- `not_allowed`: the MCP server is switched off, or this person or account may not use it now (for an
  administration tool, also: its switch is off, or the person is not an administrator);
- `refused`: the API's own refusal, in its words (no access, a closed dataset, a level too low, a private field
  questioned);
- `not_found`: a dataset, field, handle or saved view that doesn't exist (a handle or a saved view this person can't
  see answers the same);
- `invalid`: arguments that don't fit, or that the server can't take; one about filter text carries the server's
  `details` (`line`, `column`, `suggestions`) so the model can correct it;
- `busy`: try again shortly (`retryable` is true);
- `too_slow`: a count or a read ran into the time limit for one database query; its message says what ran out and
  what to do ([What is quick and what is slow](#what-is-quick-and-what-is-slow));
- `unknown_tool`.

Through the MCP server two more: `timeout`, a call stopped at 60 seconds, and `failed`, a fault of the server's own
(which goes to the server's log). Every list in a result has an upper bound (a dataset's few embedding fields aside),
and comes with the true total and the offset that gets the rest.

| Tool | What it answers | What it reads |
|---|---|---|
| `list_datasets` | the datasets the person can open, by name | `GET /api/datasets` |
| `describe_dataset` | a dataset and its fields, from its statistics | the dataset, its schema, field statistics, coverage, concepts and views |
| `make_view` | a working view from filter text, its exact count and link | parse, format, `samples/query` |
| `refine_view` | a new view: stages added, removed, turned around, or a value list's | the same |
| `field_values` | a field's values in a view with their counts, kept as a value list | `aggregate`, `samples/query` |
| `field_summary` | a field's range, quantiles and histogram, or its commonest values | `aggregate`, `samples/query` |
| `get_samples` | a page of a view's samples with their links | `samples/query` |
| `find_place` | the regions a name could mean, each with its filter text | `GET /api/regions` |
| `list_concepts` | a dataset's concepts, each with its cut and filter text | `GET .../concepts`, format |
| `concept_preview` | what a concept's cut keeps and removes from a view, exactly | `samples/query`, `GET .../concepts/{id}/boundary` |
| `search_similar` | a view's images most like some words or samples, as a working view | `POST /api/search`, `samples/query` |
| `list_saved_views` | the saved views the person can see | `GET .../views` |
| `open_saved_view` | a saved view as a working view, with its own link | `GET /api/views/{slug}`, `samples/query` |

For an administrator, four more tools read the server's status, background jobs, backups and index advice while the
administration tools are switched on: [Administration tools](#administration-tools).

### `list_datasets`

The datasets the person can open and the assistant may read, by name; a dataset being deleted is left out. Arguments:
`search` (text the name contains, any letter case), `limit` (1 to 50, default 25), `offset`.

```json
{"datasets": [{"id": 4, "name": "field-trials", "description": "…", "level": "edit"}],
 "total": 37, "offset": 0, "next_offset": 25}
```

The list carries no counts: the app's own list of datasets has none, and counting every dataset of a page costs a scan
each. `describe_dataset` gives a dataset's.

### `describe_dataset`

One dataset and its fields, a page at a time, in the schema's order. Arguments: `dataset` (its id, required), `search`
(one or more words: a field is kept when any of them is in its name or description, any letter case, so
`dark bright gain` finds the fields about any of the three in one call; never matched on a field's values), `limit`
(1 to 100 fields, default 50), `offset`.

It reads only what the server already keeps about the dataset, its statistics, and never counts through the samples
field by field: a call costs about what opening the dataset's sidebar costs, and makes the same six requests however
many fields the page has (`GET /api/datasets/{id}`, then its `field-stats`, `schema`, `coverage`, `concepts` and
`views`, all as the person). One statement counts the samples, for the numbers no statistic keeps: the rows, the
object rows and the rows with a location. Counts per value belong to the tools asked about one field.

`dataset` has `samples`, the number a view of the whole dataset counts (what its page shows with no filter), and
`objects`, how many object rows it has besides. Each field has:
- `type`, its kind in the [filter language](FILTER_QUERY.md) (`number`, `datetime`, `text`, `list`, `boolean` or
  `object`);
- `indexed`: whether a filter on it with `=`, `in` or a narrow range is quick however large the dataset
  ([What is quick and what is slow](#what-is-quick-and-what-is-slow)), from what the schema already says;
- `description`, `unit` and `description_source`, as the schema gives them: what someone wrote (`"set"`), or the
  server's own description of a field it makes itself, such as `created_at` (`"default"`). A field nobody described
  has none, whatever its name: a field called `brightness` may hold 0 to 1 or 0 to 255
  ([field descriptions](SDK.md#field-descriptions)), so the instructions tell the model that a number field's scale
  is the dataset's own and to read its `range` (or its `field_summary`) before choosing a cut;
- `samples_with_value`: rows with the field, as the statistics count them, object rows included;
- `range`: smallest and largest, for number and datetime fields;
- `values`, for text and list fields: `distinct`, how many different values, and `examples`, up to five of them in the
  server's own order, without counts;
- `values_private` and `complete`.

With the fields come `fields_total`, `next_offset`, `undescribed_fields` (fields with no description at all, over the
whole dataset), `samples_with_location`, `embedding_fields`, `concepts`, and `saved_views` (those the person can see).

`values` comes from the list of a field's values the statistics keep, for an indexed field and the built-in `tags`
and `media_type` too: nothing is read from the samples for it, so until the dataset's statistics are first counted
`tags` and `media_type` may have none, and say `complete: false`. A field with more different values than the
statistics keep a list of (serial numbers, file names) has `"many": true`, `distinct` null and whatever examples the
server holds, possibly none: no fault of the statistics. A
boolean's `values` is null (its values are true and false), as is that of a field holding JSON lists, a media field
and a drawing itself (detections, polygons, masks). A drawing's label list (`detections.label`) has its labels.

`complete` is false when the field's statistics are not up to date: the field was never counted, or changes wait to be
counted that name the field or its drawing, or name no field, or the dataset's statistics were never counted whole or
wait to be published. The numbers given are then the ones the server has, `values.distinct` is null, and the model is
told so. The two cases of a list held in part are told apart this way: up to date, a list cut short is a field with
many values; behind, it waits for the count.

### Working views

A conversation narrows a dataset step by step. Each step is kept on the server as a **working view**, named by a
handle (`wv_...`) that the next tool call passes back; there is no session. A view is its person's, its dataset, its
**stages** (one filter each, as a chip of the app's filter bar) and the view it was made from. It is never changed:
refining one makes a new one, so an earlier handle goes back a step. A handle names the view for its person only
(anyone else gets `not_found`, as for a handle that never existed), and every use reads the dataset as that person at
that moment, so a handle gives nothing its person couldn't get without it. Deleting a dataset removes its views and
value lists. Nothing here writes to a dataset: the assistant's own tables are `assistant_views` and `assistant_lists`.
How long they are kept, and how many, is in [Limits](#limits-and-settings).

A tool that takes `view` also takes `dataset` instead, meaning the whole dataset; exactly one of the two.

**A view is of the dataset's images.** On a dataset that keeps objects (patches cut from an image) as rows beside its
images, every count, value, page and summary is over the images that pass the view's stages: what the dataset's page
shows when the view's link is opened, and `make_view` of the whole dataset counts what `describe_dataset` calls
`samples`. An image passes a stage when it matches itself or one of its objects does, so `label = "weed"` on a field
only objects carry keeps the images with a weed object. A field's values and summaries are the images' own:
`field_values` on a field only objects carry has no values.

- **`make_view`** (`dataset`, `filter`): filter text in the [filter language](FILTER_QUERY.md); each top-level AND part
  is a stage. Answers the handle, the exact `count`, `filter` and each stage's `text` as the server read them, and a
  `link`.
- **`refine_view`** (`view`, and any of `add`, `remove`, `negate`, `in_list`, `not_in_list`, applied in that order):
  add text as stages, drop or turn around stages by their number, or keep (or remove) the samples whose field has a
  value in a value list (`{"field", "list"}`). Answers the new view with `from`. Negating twice gives the original.
- **`field_values`** (`view` or `dataset`, `field`, `search`, `limit` up to 50, `offset`): the field's distinct values
  among the view's samples with their counts, commonest first (ties by value), a page at a time, with `distinct` (how
  many in all) and `samples_with_value`. For a list field (tags) a value is one element. It keeps the whole list under
  a handle (`vl_...`), the same for every page of one question, for `in_list` and `not_in_list`, and gives `download`,
  a CSV of every value for the person.
- **`field_summary`** (`view` or `dataset`, `field`): for a number or a datetime, `min`, `max`, `mean` (numbers),
  `quantiles` (p5 to p95), `distinct` and a `histogram` of at most 20 bins that count every sample with a value; for
  text, list and boolean fields, `distinct` and the five commonest values (`top`); and `samples_with_value` and
  `samples_without`. A number's histogram counts the values its `min`, `max` and quantiles count, a number written as
  text included. A number's or a datetime's summary reads the view twice; when only the second read (quantiles,
  histogram, distinct count) runs out of time, the answer still has the first's numbers, each exact, with
  `quantiles`, `histogram` and `distinct` null and a `note` that says so and how many samples the view has.
- **`get_samples`** (`view` or `dataset`, `limit` up to 50, `offset`, `sort_by`, `descending`, `fields`): a page of the
  view, each sample its `id` and a link. With **The assistant may read samples' fields** on, each sample also carries
  the `fields` asked for, its file path included; off, asking for fields is `refused`.

On a private field, `field_values` gives `values` as `[]` (with `distinct`, `samples_with_value`, `list` and `download`
as for any field) and refuses `search`; `field_summary` gives counts only; `get_samples` leaves it out of `fields`
(named in `withheld`) and can't sort by it. A value list's stage on it reads `serial in (38 values kept from the
assistant)`, and a value list of a private field filters that field only.

**Cost.** `make_view`, `refine_view` and `get_samples` cost what the app's grid costs for the same filter (one count,
one page). `field_values` and `field_summary` count the view (one count), then read its samples for the one field
asked about through the aggregate route: once, or twice for a number's or a datetime's summary (a list or a drawing's
labels take one more statement, for the samples that have the field). Looking the field up reads no samples. Every
count is exact for the person at the time of the call. [What is quick and what is slow](#what-is-quick-and-what-is-slow)
has the times.

### Links

Every view answer carries a `link` that opens the dataset in the app with that view's stages as chips. It starts with
the server's public address ([Limits and settings](#limits-and-settings)), never with the address a request came in on.

- `…/datasets/<id>?q=<the filter text>` when every stage can be written as text and the address fits the app's limit.
  Such a link is permanent and works for anyone who can open the dataset.
- Otherwise `…/datasets/<id>?w=<handle>`, with a `link_note` saying it works for this person for 30 days and that
  saving a view makes a permanent link: a stage with no text form, a long value list, or a stage on a private field
  the model didn't write (a value list's), whose text keeps the values back. A link never holds a token.
- A saved view opened with `open_saved_view` keeps its own `?view=` link.

A `?w=` link opens in the app for its own person ([links](DATASET_MANAGEMENT.md#links-to-a-filter-a-view-or-a-sample)).
The person's own endpoints, signed in with the browser's session or an ordinary API token, show the values kept from
the assistant; a request made for a tool is refused on every `/api/assistant/` route:
- `GET /api/assistant/views/{handle}`: `{"dataset_id", "stages", "created_at", "expires_at"}`, the stages as the app's
  filter bar keeps them (`{type, description, filter}`), with their real values. Another person's handle is a 404
  saying it belongs to someone else's conversation; an unknown or expired one a 404 saying views are kept 30 days.
- `GET /api/assistant/lists/{handle}/csv`: a CSV with a header row `"value","count"` and one row per distinct value,
  however many. Anyone else, and an unknown or expired handle: 404. A kept list's download is the list as it was when
  the tool answered; a list too long to keep is counted again when the download is opened. A text that would start a
  formula (`=`, `+`, `-`, `@` or a look-alike such as `＝`, after any leading spaces or double quotes, or a tab or a
  carriage return first) is written with an apostrophe in front, so a spreadsheet can't run it: at the start of a
  cell, and also after a `;`, a tab or a line break inside one, where a spreadsheet that reads the file with `;` (Excel
  in much of Europe) or tabs begins a new cell. A program reading the file sees those apostrophes, and ordinary text is
  touched too: `range 3; -5 to 5` becomes `range 3;' -5 to 5`. A text that is only a number (`-5`) gets none. Every
  text cell is in double quotes, and a number or a count is written bare: a spreadsheet that reads the file with `;`
  takes a line with no `;` for one cell, with the comma as its decimal mark, so a number next to an unquoted text could
  read as one formula.

### Finding

Six tools find what a person names: a place, a kind of image, images like some words, a view someone saved. Words a
person or a model wrote never become a stage's words: a stage is worded by the server (the filter language's writing,
a private field's values kept back) or by a fixed phrase for a stage with no text form.

**`find_place`** (`text`, `limit` 1 to 10): the regions of the loaded boundary set whose name or key matches, best
first (`GET /api/regions`, the search the app's filter text uses for names), with `total`. Each has `key`, `name`,
`kind` (`country`, `state` or `group`), `country` (the country a state or group belongs to) and `use`, the filter text
that keeps its samples (`location within "US-KS"`; `location not within` removes them). When several match (Georgia is
a country and a state) the model asks which is meant and then has that one's text. When a hundred or more of one kind
match, `total` is null and a `note` says to write more of the name. With no boundary set loaded: `"loaded": false`, no
places, and a `note` that an administrator loads boundaries under Settings → System status ([regions](REGIONS.md)). It
reads no dataset. A place is a region when the samples have a location; when a text field already names it
(`state = "Kansas"`), that field is the filter.

**`list_concepts`** (`dataset`, `limit` up to 50, `offset`): the dataset's concepts by name, each with its saved cut
(`threshold`), `ready` (false while its scores are being recomputed, when a filter on it is refused) and `use`, the
filter text that keeps the images above the cut (`concept "equipment" > 0.62`, written by the server).

**`concept_preview`** (`view` or `dataset`, `concept`, `threshold`, `limit` 1 to 10): what the cut would do before it
is applied. `above` and `below` are the grid's exact counts of the view's images with `concept "…" > t`, and with
`concept "…" <= t`, added; `unscored` is the images with no score; `keep` and `remove` are the filter texts for
`refine_view`, and `remove` does what the dataset's page does with that filter. `just_above` and `just_below` are the
images nearest the cut on each side, nearest first, with their scores and links. The server keeps those for the whole
dataset only, so they come for a `dataset` or a view with no stages, and are null with a `boundary_note` for a view
with stages (the counts are exact either way; nothing is approximated). A concept the dataset doesn't have is
`not_found` with its concepts' names (up to 20); one being rescored is `refused` in the API's words.

On a dataset with objects, an object row is never one of the nearest, and an image counts where it or one of its
objects does: an image whose objects score on the other side of the cut counts both above and below, and a `note` says
how many do. There, the app's `not concept …` keeps only images that have a score, so `remove` takes the unscored
images out too: `unscored` says how many before the filter is applied.

**`search_similar`** (`view` or `dataset`, `text` or `sample_ids` (1 to 20), `limit` 1 to 200, `field`): the images the
view selects most like the words or the samples, most similar first, by an embedding field (the dataset's `clip`, else
its first whole-image field, when `field` is left out), through `POST /api/search` with the view's stages, as the app's
search within its filters. The answer is a working view: the stages of the view searched, then one stage holding the
ids found, numbered `found_stage`, whose words are the server's (`id in (…)`), so `refine_view` with
`negate: [found_stage]` is the view searched without them. `samples` shows the first 20 with their scores (cosine
similarity), `searched` is the view's count, and `search_note` says when the search stopped before it had looked at
every image (more may match). With a vector index the search is as close as the index's, as in the app. Asked about a
whole dataset, the answer has no `from`. The words are sent to the search and nowhere else.

`count` is the new view's own count. On a dataset with objects it can be smaller than the number found (an image that
was in the view searched only through one of its objects isn't kept by the found stage, which keeps an image by its
own id), and a `note` then says how many and why. A dataset with no embedding field, a `field` that isn't one, words
for a model without text, a server that has no text model or can't load it, or samples not in the dataset: `invalid`,
saying which and what is possible. The first search by words in a server process waits for the text model to load
([Limits and settings](#limits-and-settings)).

**`list_saved_views`** (`dataset`, `limit` up to 50, `offset`): the saved views the person can see (their own, those
shared with them and the public ones), by name: `slug`, `name`, `description` (cut to 200 and 300 characters), `mine`,
`default` (the dataset's default view), `stages` and `link`, the view's own permanent `?view=` link.

**`open_saved_view`** (`slug`): the saved view as a working view, as the app opens it (its stages, else its compiled
filter): what `make_view` answers plus `"saved": {"slug", "name"}`, with the view's own `?view=` link. Each stage has
its `text`, written again by the server, or `"text": null` and a `label` (`a shape drawn on the map`, `a lasso on the
embedding map`, `a similarity search (50 samples)`, `picked samples (12 samples)`) for a stage with no text form or an
id list too long to show; never the words saved with the chip. A stage naming a concept the dataset no longer has is
left out, as the app leaves it out, with a `note` naming it by its place and the concept's id. A stage that tests a
private field for one of some values reads `serial = (a value kept from the assistant)` and still filters. A saved view
that ranges over or searches within a private field can't be opened by the assistant: `refused`, naming the view and
saying it filters a field whose values are kept from the assistant in a way only a person can apply, and to open it in
the app (its link); nothing of its stages is repeated. A slug the person can't see and one that doesn't exist are the
same `not_found`; a view of a dataset closed to the assistant is `refused`. Opening it changes nothing.

Both make views within a view's limits: a search adds one stage to the view searched, and a saved view brings its own,
so a search of a view of 50 stages, or a saved view of more than 50, is `invalid` and makes no view.

## Administration tools

Four more tools help an administrator run the server: `system_status`, `backup_status`, `list_jobs` and
`index_advice`. They read what Settings → System status and a dataset's page show, and they only read: each answer has a
`link` to the page where the administrator acts (removes an index, backs up now, stops a job). Calling one twice
changes nothing.

### Who may use them

**Settings → Assistant → Administration tools** is their switch, off as shipped and on a server upgraded to this
release (`admin_tools` on `GET` and `PATCH /api/admin/assistant`). A person may call one when, at that moment, they may
use the MCP server ([Who may use it](#who-may-use-it)), they are an administrator, and the switch is on. All three are
read again on every call and on every request a tool makes: an administrator made a power user, or the switch turned
off, is refused at the next request. Refused, a call answers `not_allowed` with "The administration tools are switched
off on this server" or "This tool is for administrators"; the MCP server's own refusals come first, in their words.

The MCP server lists them after the thirteen to a person who may call them now, and to nobody else. A client may keep
the list it was given for five minutes, so a change of the switch or of a role reaches its list within five minutes; a
call is decided at once. Called by name, they are never an unknown tool: someone who may not call one gets
`not_allowed`. Each is marked read-only.

### What goes to the AI client

What these tools read goes to the AI client the administrator connects, and from there to its AI service: the ids and
names of datasets and the names and sizes of their indexes, also for datasets closed to the assistant (an administrator
runs the whole server), never their samples, their fields' values or their jobs' messages; usernames (who started a
job, who removed an index); error messages (a failed job's, a failed backup's); and the backup folder and its files'
names.

Never: a token, a password or its hash, a storage key or secret, a connection string, a signed URL, the assistant's
sealing key, an email address, an IP address, a worker's host name, a commit or tree hash, or a file-system path other
than the backup folder. Each answer is built from named facts: a tool copies only the facts listed below, so something
a route adds later never reaches a client by itself. A free text (an error, a progress message, a reason) is cut to 500
characters, and an email or IP address, a web or connection address, a path outside the backup folder, a hash (12
hexadecimal characters or more, with a digit and a letter) or a secret in it is written `[email address]`,
`[IP address]`, `[address]`, `[path]`, `[hash]` or `[secret]`. That is a second line of defence for text the server
did not write: it can't recognise a host name inside a sentence.

The administrators' pages know nothing of a dataset closed to the assistant or of a field's values kept from it, and
their free text can quote data: a failed index build's database error quotes the value it could not read. So:

- **A job's messages** (its error, its progress message) are given only when every dataset the job is about is open
  to the assistant and has no field whose values are kept from it, or when it is about no dataset and its kind is
  known to be: a backup, a region load, the selection cleanup, the server's own index review, the removal of an index
  that belongs to no dataset. A kind is known to be about none only when the job reads no sample's values, or its
  messages are written by the app from counts and names alone. Restoring an index is not: it builds the index again
  from its stored definition, reading every row the index covers, and a hand-made expression index can fail quoting a
  sample's value from any dataset. A job is about the dataset it belongs to, the one it names in what it was asked to
  do (a dataset's index review belongs to none and names its dataset), and every dataset it writes: a follower's sync
  names only its link and writes that link's mirrors on this server. Otherwise `error` and the
  progress `message` are null and, when there was one to keep back, `error_withheld` says why: "This job's messages
  are kept back: its dataset is closed to the assistant", "This job's messages are kept back: its dataset has fields
  whose values are kept from the assistant", "This job's messages are kept back: its dataset no longer exists" (also
  for a sync whose link no longer exists or has no mirror left: a mirror's row goes with its dataset, so none left can
  mean deleted since), or "This job's messages are kept back: this kind of job is not yet known to be about no
  dataset" (a restore, and any other kind that belongs to no dataset, a kind added later included). `error_withheld` is
  null whenever nothing is kept back. The person reads the messages on the job's linked page. Known and left open:
  when some of a link's mirrors are left and one was deleted, a past sync's words are judged by the ones left.
- **An index's SQL definition** is judged by the constants it holds, whatever kind the advisor calls it: the advisor
  guesses a kind from the index's name, and a person can name an index like the app's
  (`idx_samples_site_gin … WHERE metadata->>'site' = 'Farm 12'`). A constant is a quoted text or a number: PostgreSQL
  prints strings, dates, intervals, arrays and jsonb quoted and numbers bare (`… = 4417`, `> 9000.5`,
  `= ANY (ARRAY[12, 31, 407])`), so a partial index for one plot, farm or year holds a field's value either way. A
  definition is given whole when every quoted text in it is one the server writes itself (a field's name:
  `metadata ->> 'field'`, `metadata -> 'field'`, `metadata ? 'field'`, with or without the `::text` PostgreSQL prints
  after it; a vector index's `field_name = '…'`; a storage parameter inside `WITH (…)`) and every number is the
  server's own (`dataset_id = N`, a storage parameter, a type's modifier such as `numeric(10,2)`; digits inside a name
  such as `embeddings_d7` are not numbers). It is also given whole when it is one of the indexes `schema.sql` ships:
  the same name on the same table, holding no quoted text and no number `schema.sql`'s own statement for it doesn't
  (those are the app's constants; an index made under a shipped index's name with a value of its own in it is not one
  of them). Otherwise, or when it is longer than 2,000 characters, `definition` is null and `definition_withheld` is
  true, and the words about that index (an overlap's `reason`, a `needed` reason) write each such quoted text `'…'`
  and each such number `…`. Everything the app builds stays whole: a field's index is its expression and
  `WHERE dataset_id = N`, a vector index quotes its field, a list column's index holds no constant.

Every answer is at most 20 KB; each list comes a page at a time with its true total.

### `system_status`

No arguments. The server's health, from `GET /api/admin/status`:

```json
{"checked_at": "2026-11-19T09:00:00+00:00", "server": {"version": "2.13.2", "restart_needed": false},
 "database": {"reachable": true, "size_bytes": 140000000000, "pending_migrations": [], "unknown_migrations": [],
              "stale_functions": []},
 "database_disk": {"total_bytes": 172000000000, "free_bytes": 50000000000, "level": "ok",
                   "low_below_bytes": 5368709120, "ingest_paused_below_bytes": 1073741824},
 "cache_disks": [{"folders": ["S3 image cache", "Crop cache"], "total_bytes": 172000000000,
                  "free_bytes": 50000000000, "level": "ok"}],
 "workers": {"server": {"online": 3, "last_seen": "2026-11-19T08:59:58+00:00"},
             "compute": {"online": 0, "last_seen": null}},
 "backups": {"level": "ok", "latest_at": "2026-11-19T02:00:05+00:00", "next_run_at": "2026-11-20T02:00:00+00:00"},
 "link": "https://fr.example.org/settings/status"}
```

`database_disk.level` is `ok`, `low` (below `low_below_bytes`: the app warns), `paused` (below
`ingest_paused_below_bytes`: new samples are refused until space is freed) or `unknown`; a cache disk is `ok` or `low`
and names its folders by their labels, never their paths. "Pending" means `pending_migrations` (migrations the code has
and the database hasn't run), `unknown_migrations` (ones the database ran that the code doesn't know), `stale_functions`
and `restart_needed` (the code on disk is newer than the code running), never a newer release: nothing checks for one.
A database that doesn't answer is `{"reachable": false, "error": "..."}`, and workers that can't be counted
`{"available": false, "error": "..."}`. `backups` is the short form; `backup_status` has the rest.

### `backup_status`

No arguments. The backups, from `GET /api/admin/status`, `GET /api/admin/backups/schedule` and the backup jobs:

```json
{"level": "failed", "folder": "/mnt/data/backups",
 "latest": {"file": "freedomridge_20261019T055351Z.sql.gz", "created_at": "2026-10-19T05:53:51+00:00",
            "size_bytes": 20040000000, "dump_seconds": 1019, "verify_seconds": 205, "tables": 30},
 "last_failure": {"failed_at": "2026-10-20T02:00:00+00:00", "error": "BackupError: free space 3 GB, need 40 GB"},
 "kept": {"count": 2, "total_bytes": 40080000000, "oldest_created_at": "2026-10-18T02:00:04+00:00"},
 "unverified_dumps": 0, "disk": {"total_bytes": 8000000000000, "free_bytes": 2000000000000},
 "schedule": {"interval_hours": 24, "start_hour": 2, "keep": 7, "next_run_at": "2026-10-21T02:00:00+00:00"},
 "running": null,
 "recent_runs": [{"job": "5b1c2e9a-...", "state": "failed", "started_at": "2026-10-20T02:00:01+00:00",
                  "finished_at": "2026-10-20T02:00:03+00:00", "file": null, "size_bytes": null,
                  "error": "RuntimeError: BackupError: free space 3 GB, need 40 GB"}],
 "link": "https://fr.example.org/settings/status"}
```

`level` is `ok`, `overdue` (the newest verified backup is older than the schedule allows), `failed` (a backup failed
after the newest verified one), `none`, `unreadable` (the folder can't be read: `folder` and `error` say which and why)
or `not_configured`: no backup folder is set, the other facts are null or empty, and `note` says "This server takes no
backups through the app (no backup folder is set). A deployment may back up another way; see the backup docs." (the
production Compose stack reads this way while its sidecar backs up elsewhere: [Backups](BACKUP_AND_MIGRATION.md)).
`latest` is the newest backup that was verified when it was made. `running` is `{"job", "state", "started_at"}` for a
backup queued or running now, else null. `recent_runs` are the last ten backup jobs, newest first, each run that
succeeded with its own backup's `file` and `size_bytes`.

### `list_jobs`

Background jobs across the whole server. Arguments: `state` (`needs_attention`, the default: failed, queued and
running; or one of `failed`, `running`, `queued`, `succeeded`, `cancelled`), `kind`, `dataset` (a dataset's id: its
jobs and those that name it), `since_hours` (1 to 8760, default 168), `limit` (1 to 50, default 20), `offset`.

```json
{"jobs": [{"id": "0b7e...", "kind": "field_index_build", "state": "failed",
           "dataset": {"id": 7, "name": "main"}, "field": "serial_number", "started_by": "sam",
           "created_at": "2026-11-18T09:14:03+00:00", "started_at": "2026-11-18T09:14:04+00:00",
           "finished_at": "2026-11-18T09:15:10+00:00", "progress": {"done": 0, "total": null, "message": null},
           "failures": 2, "max_failures": 2, "error": "QueryCanceledError: canceling statement due to statement timeout",
           "error_withheld": null, "link": "https://fr.example.org/datasets/7"}],
 "total": 3, "offset": 0, "next_offset": null,
 "counts": {"failed": 3, "running": 1, "queued": 0, "succeeded": 2114, "cancelled": 0},
 "kinds": ["backup", "field_index_build", "index_review", "selection_cleanup"],
 "since": "2026-11-12T09:20:00+00:00", "link": "https://fr.example.org/settings/status"}
```

Newest first. The window bounds the jobs that have ended (failed, succeeded, cancelled), by when they were created in
the last `since_hours`; a job still queued or running is listed and counted however old it is, for one waiting weeks
for a worker is what needs attention. `total` counts the jobs that match; `counts` is by state for the same `kind`,
`dataset` and window, whatever `state` asks; `kinds` are the kinds in the window, whatever the other filters, so a
model can ask for one. `progress` is `{"done", "total", "message"}`, its `message` null when the job has
none or it is kept back. `started_by` is a username, or "System". An unknown `dataset` is `not_found`; an unknown
`kind` gives an empty list. A job's result is never given.

It stands on `GET /api/admin/jobs/all` (administrators only): `state` (repeatable; every state without it), `kind`,
`dataset_id` (the jobs about that dataset), `since` (the jobs that have ended, created at or after it), `limit` (1 to
100, default 50) and `offset`, answering `{"jobs", "total", "counts", "kinds",
"offset", "next_offset"}`, each job with `id`, `kind`, `state`, `dataset_id` (the dataset it is about), `dataset_name`,
`field`, `started_by`, `created_at`, `started_at`, `finished_at`, `progress`, `failures`, `max_failures`,
`cancel_requested` and `error`: no result, and nothing of what a job was asked beyond the dataset it is about, but for
a `backup` job's `backup`, its own `{"file", "size_bytes", "tables"}`, null until it has succeeded. It reads
the jobs table through two indexes of its own (migration `0041_jobs_across_datasets.sql`) and never reads samples: on a
million jobs it answers a week's window in a few milliseconds and a year's in about a tenth of a second.

### `index_advice`

What the index advisor found at its last review. Arguments: `dataset` (one dataset's indexes; left out, the server's
own), `section` (one list, a page at a time: `overlapping`, `possibly_unused`, `suggested`, `needed`, `too_new`,
`removed`), `limit` (1 to 50, default 20), `offset` (with `section`).

```json
{"scope": "system", "dataset": null, "reviewed_at": "2026-11-18T03:00:04+00:00", "enough_history": true,
 "window_days": 7, "idle_days_required": 14, "review_running": false, "last_failure": null,
 "totals": {"overlapping": 10, "possibly_unused": 2, "suggested": 0, "needed": 41, "too_new": 3, "removed": 1,
            "redundant_bytes": 5400000000},
 "overlapping": [{"redundant": {"name": "idx_a", "table": "samples", "kind": "other", "method": "btree",
                                "dataset": null, "field": null, "size_bytes": 540000000, "scans": 12,
                                "last_scan": "2026-11-01T10:00:00+00:00", "valid": true, "needed": [],
                                "definition": "CREATE INDEX idx_a ON public.samples USING btree (a)",
                                "definition_withheld": false},
                  "kept": {"name": "idx_b", "...": "..."}, "rule": "prefix",
                  "reason": "Its columns (a) lead idx_b (a, b), which serves the same searches (reading a larger index)"}],
 "possibly_unused": [{"name": "idx_c", "...": "...", "idle_days": 31.5, "invalid": false}],
 "schedule": {"enabled": true, "frequency": "weekly", "weekday": 0, "time": "03:00", "timezone": "UTC",
              "next_run_at": "2026-11-23T03:00:00+00:00", "last_run_at": "2026-11-16T03:00:00+00:00"},
 "pending_actions": [], "offset": 0, "next_offset": null, "link": "https://fr.example.org/settings/status"}
```

Without `section`, the first `limit` entries of `overlapping` and `possibly_unused` (and `suggested`, for a dataset),
fewer when they would make the answer too large (`note` says so); with `section`, that list alone, and `next_offset`
is for it. `totals` counts every list; `redundant_bytes` is the size of the redundant indexes of `overlapping` and of
`possibly_unused`, each index once. An overlap is one of three rules: `duplicate` (the same columns, operators, order
and predicate), `operators` (the same columns, with an operator class whose operators the other's include) or `prefix`
(a B-tree whose columns lead the other's). `possibly_unused` waits for `enough_history`: the advisor calls an index
unused only once it has watched for `idle_days_required` days, and `note` says when it hasn't. `needed` lists why each
index must stay; `suggested` (a dataset's) is `{"field", "type", "reason", "excess_seconds", "build_seconds",
"pays_back", "usages"}`; `removed` is `{"index_name", "table", "field", "size_bytes", "reason", "removed_by",
"removed_at", "state"}`. An index's `dataset` is `{"id", "name"}` when it belongs to one, a closed dataset included.
With no review yet, `reviewed_at` is null, the lists are empty and `note` says an administrator starts one on the
page. `last_failure` is `{"finished_at", "error", "error_withheld"}`: a dataset's review keeps its error back as a
job's (a failed review of a closed dataset's indexes has `error` null and the sentence). The tool removes nothing: the
person removes or restores an index on the linked page.

## The call log

Every tool call made through the assistant leaves one row: who, through which door (`mcp`), which tool, which dataset
when the arguments named one, when it started, how long it took, and whether it succeeded (the error code when it
didn't). Never the arguments and never the result: filter text can hold serial numbers, and none of it is stored. A call
stopped at 60 seconds is `timeout`, and a call whose client went away before it answered is `cancelled`. A refusal for
being busy is logged too, but at most once a minute per person in each process, so a client refused in a loop shows up
without filling the log. Rows older than 30 days are removed hourly. Deleting a person or a dataset keeps its rows (a
deleted person's rows show no user).

Administrators read it under Settings → Assistant, **Recent calls**, and at
`GET /api/admin/assistant/calls?limit=&offset=` (newest first; `limit` 1 to 200, default 50):

```json
{
  "calls": [
    {"id": 81, "user_id": 12, "username": "sam", "door": "mcp", "tool": "describe_dataset", "dataset_id": 4,
     "started_at": "2026-11-18T09:14:03.512+00:00", "duration_ms": 182, "ok": false, "error_code": "refused"}
  ],
  "total": 1, "offset": 0, "next_offset": null
}
```

## How a tool reads (for developers)

`backend/assistant` holds the tools (`catalog.py`, `working.py`, `finding.py`, and the administration tools in
`admin.py`), the rule (`rule.py`) and the way a tool reads (`inner.py`, `api.py`). The door (`backend/mcp_door.py`)
calls these:

```python
from backend import assistant

assistant.tools()                                    # the tools, in their fixed order
assistant.admin_tools()                              # the administration tools, in their order
await assistant.tools_for(user_id=7, door='mcp')     # what that person could call through that door now
assistant.instructions()                             # the text a model is given before the tools
await assistant.door_refusal(user_id=7, door='mcp')  # None, or the sentence that says why not
await assistant.call_tool(user_id=7, door='mcp', name='list_datasets', arguments={})
```

- **Through the API, in process.** A tool calls the HTTP API's routes as an in-process ASGI request through the whole
  application, every middleware and dependency included, so every rule the API keeps holds and none is written twice.
  Only JSON answers are read; a request has no client address.
- **Identity on the scope.** The request carries the person on the ASGI scope (`fridge.assistant`), a key no request
  from outside can set; never a token or a cookie. `get_current_user` reads it first, reads the account afresh, and
  refuses (403) an inactive account, one that owes a password change, or one the rule keeps out. The user it answers
  is marked as the assistant's.
- **One place for the closed rule.** For a marked user, `check_dataset_access` refuses a closed dataset after the
  access check, and `filter_datasets_for_user` leaves it out (`backend/access.py`), so routes that find their dataset
  through a view, a job or a body follow without anything more. An administration tool's user is marked too, and
  neither applies to it: it reaches only the routes its tool lists, which name closed datasets like any other, and
  what could carry a closed dataset's samples is kept out of their answers ([Administration
  tools](#what-goes-to-the-ai-client)).
- **Two fences.** Each tool lists the (method, route) pairs it may call. A request for any other is refused before it
  is sent, and `get_current_user` refuses a request served by a route the tool doesn't list, and, for the thirteen,
  every administrators' route by its path, whoever the user is: `/api/admin/...`, and the administrators-only reads
  elsewhere (`/api/users`, `/api/groups`, `/api/followers`, `/api/primaries`, `/api/storage/configs`,
  `/api/compute-launcher`, `/api/workers`, a dataset's `/index-advisor`). Those answer across datasets and know nothing
  of a closed dataset. A job of no dataset reads to them as one that doesn't exist. Either refusal can only come from a
  tool's own fault, so `call_tool` raises `ToolFailed` for it. Only an administration tool may call an administrators'
  route: one its tool lists (`private.SPANNING_ROUTES` holds every route such a tool may list, and it may list no
  other), while its user may use the door, is an administrator and the administration tools are switched on, all read
  afresh for each request; the routes keep their own administrator check. `backend/tests/test_assistant_core.py`
  checks that every route a read tool lists is a GET or one of the POSTs the route guard counts as reads, is no
  administrators' route, and identifies its caller.
- **Private values in one place.** Every route a tool reads is sealed by `inner.py`, not by each tool
  (`private.py`, `guard.py`):
  - **Out:** every answer reaches a tool with each value of a private field replaced by a sealed token: AES-SIV
    encryption of the value, padded so it doesn't tell the value's length (every value up to 28 bytes seals to one
    length), and bound to its dataset and field. Equal values of a field give equal tokens. Nothing sealed is ranked:
    a private field's smallest and largest value (and any range of it) are left out, not sealed, and every list of its
    tokens comes by count, then by token. One value can have two tokens, a number's and a text's, depending on how a
    route wrote it (a count writes `7300` as `"7300"`): both filter the same samples.
  - **In:** a token opens only in the API's filter compiler, as the value of an `=`, `!=`, `in` or `not in` test of
    its own field, in a request about its own dataset. That is how a value list filters. Anywhere else it stays the
    literal string it is and matches nothing. A token is made only from what a dataset holds: an answer that may echo
    the request (the filter text's reading, an error) drops a private field's values instead of sealing them, so a
    model can't have its own guess turned into a token.
  - **Text:** filter text the server writes for a tool shows a private field's values as
    `(3 values kept from the assistant)`. A saved view's stage words that name one are written again the same way.
  - **Deny by default:** a tool may list a route only once its answer's shape is declared covered
    (`private.COVERED_ROUTES`), with a test that plants values and reads that route's answer. An administration
    tool may list only the routes of `private.SPANNING_ROUTES`, which span datasets: it copies only the facts it names
    from their answers, and `private.keep_back` first takes out a job's messages by its dataset and a hand-made index's
    definition that holds a quoted value. A route that names no
    dataset in its path declares where the dataset its answer is sealed for comes from (`private.DATASET_FROM`): none
    (the regions), its body (the search), or the one it resolved itself (a saved view by its slug, whose route keeps
    the dataset it checks on the request, and answers a private view its caller may not see as missing before it
    looks at the dataset).
  - **The key** is 64 random bytes the server makes for itself when the assistant's settings are first created
    (`assistant_settings.seal_key`). It is never derived from a configured secret such as `STORAGE_ENCRYPTION_KEY`,
    whose shipped default is public, and never rotated: a restore keeps it, so the assistant's links keep working; a
    new server makes its own; a key that changes anyway makes every earlier token match nothing, and the tool says so.
    Anyone who can read the database sees the values themselves, so the key's secrecy matters only against the model.
  - **Tests:** one test is made from the tool list. It plants values in private fields of every kind (text, a number,
    a list element, a tag, a drawing's label, a saved view's and the default view's filters as the app saves them) and
    searches every tool's whole answer for them. Others send every token back in every argument and filter position,
    ask every operator, sort, summary and histogram of every private field, and write out a halving attack, which must
    learn nothing.
- **The image grain.** A view's stages are put at the app's image grain before each request
  (`backend/assistant/grain.py`, the app's `withGrain`). A fixture both sides read
  (`frontend/tests/fixtures/image-grain-cases.json`) keeps the two the same.
- **What a read does on the side.** What it does for anyone in the app: reading a dataset's fields (`field-stats`) can
  fill the statistics caches, which may scan the samples' metadata keys for a moment, and, when that scan runs out of
  time on a large dataset never opened before, queue a full statistics recount, as opening the dataset's sidebar does.
  At most one such recount is queued or running at a time, and one that ended in the last hour without filling the
  cache isn't queued again. That is not a write to the dataset.
- **`GET /api/auth/me`** says whether the signed-in person could use the MCP server now:
  `"assistant": {"mcp": true}`. It is a hint for the app: each server process reads the switch at most every 5 seconds
  for it, so it costs no query while the MCP server is off, and an error reading it answers false. A guest's answer is
  false without a query. `GET /api/auth/me?fresh=1` reads the switch from the database for that request, whichever
  process answers; the API Tokens page asks so as it opens, so it offers the MCP kind just after the switch is turned
  on. The tools themselves read everything afresh.
- **No connection held across a request.** Nothing in `backend/assistant` holds a pooled database connection while a
  tool's request runs; the request takes its own, as any other does. A guest's request waits for a guest slot and has
  the guest statement timeout, as from the app, and a busy answer (429, 503, 504) is `busy`.
