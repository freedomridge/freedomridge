# Decision: three kinds of remote party stay separate, and when to revisit

Decided 2026-11-11. Status: in force. Revisit when one of the triggers below is met.

## The question

Three kinds of program connect to a Freedom Ridge server from outside. Each was built for its own purpose, with its
own table, token scope and way of asking for work. A 2026-11 review asked whether they should become one model
(a `peers` table, one request channel, permissions as rows) before a fourth kind, such as an annotation tool or a
model explainer, is built on the same pattern.

## What exists

| | Compute workers | Followers | Relayed patch searches |
|---|---|---|---|
| Record | `workers` (and `compute_launches` for on-demand machines) | On the primary: `followers`, `follower_datasets`, and `follower_departures` for subscriptions it left. On the follower: `primary_links` (the address, the token, the primary's identity), `mirrors`, `mirror_samples`, `mirror_restores` | `follower_searches` |
| Token | `api_tokens.scope = 'worker'` | `api_tokens.scope = 'follower'`, bound to the first installation that used it | the follower's token |
| How it gets work | claims a job and holds a lease | polls a change feed per dataset | long-polls for searches (pending, claimed, then answered, failed or cancelled) |
| Liveness | heartbeat on the worker row | `followers.last_seen_at`. `follower_datasets.acked_txid` is progress per dataset, and bounds how long the change logs are kept | the primary waits a fixed time (30 seconds) for an answer |
| What it may do | whatever its owner may do on the job's dataset | Per dataset: read its samples, vectors, coordinates, fields and settings. Send tag and view changes back when `tags` or `views` is `both`, acting as its account, which deletes only the views it made. Get the images when `images` is true. Its own (`@local.`) fields never leave it | Answer searches the primary queued for datasets it follows, with the primary's sample ids only. The primary shows rows from its own tables under the asking user's access, so the follower is trusted for ids and nothing more |

All three are pull-only: the server never connects to the remote party. A shared request channel would have to keep
that property.

## The decision

**Keep the three as they are.** No `peers` table and no shared request channel now.

Why:
- No fourth kind of remote party is planned. A shared model built before its second real user is a guess at what
  that user needs.
- The follower code reached its current shape in 2.6 to 2.9 and has only just settled. Moving its permissions and
  its search relay onto new tables would change both sides of the follower protocol for no change a user can see.
- Each mechanism is small, and adding a fourth of the same size is cheaper than the move.

## What the single model would be

So that the choice can be made again with the same facts.

- One table for anything that connects: its kind, token, installation id, the protocol version it speaks and what
  it can do.
- One request channel: the server records a request for a peer; the peer collects it over its own connection and
  answers. Relayed patch search would be its first user.
- Permissions as rows (grants): dataset × action × optional field. The same rows could narrow an API token, so
  "read dataset A, write labels to field X" is said one way for followers, tools and tokens.

What it would take:
1. A migration creating the tables and copying each follower and its per-dataset options into them.
2. The followers router and the sync reading permissions from grants in place of the three columns.
3. The patch-search relay moved onto the request channel.
4. Token narrowing in the API.
5. Compute workers left as they are at first.

Both sides of the follower protocol change, so a server on the new model and a follower on an older release must
keep working together while installations upgrade.

A smaller step exists: **grants only**. Items 1, 2 and 4 without the `peers` table or the request channel. It
gives narrowed tokens and per-field permissions and leaves the three ways of connecting alone.

## When to revisit

Any one of these is a reason to reopen the decision:

1. **A second outside application is planned** that attaches to a server the way a follower does (an annotation
   tool, a model explainer). One new kind can be a fourth mechanism; two is the point where the shared model costs
   less than the copies.
2. **A tool needs to write to some fields and not others**, or a token must be limited to some datasets or
   actions. That is the grants step on its own, and it is worth doing before the tool is built.
3. **A second kind of relayed request** appears (something a server asks a follower or tool to compute, besides
   patch search). Two relays are the case for one request channel.
4. **The three mechanisms drift**: the same fix has to be made in more than one of them (lease or liveness
   handling, token binding, access re-checks). Each such fix is evidence for sharing that part.
5. **Per-dataset follower options grow past three**, or need to differ per field. Columns stop being the simplest
   form at that point.
6. **A follower needs to send its own data up**: its `@local.` fields or its model's predictions to the primary.
   That was considered and dropped on 2026-11-10, which makes it the most likely reason this reopens. It needs
   per-field write grants for a follower: trigger 2, for followers.
7. **Two instances need to edit one dataset as equals** (two-way sync). Today one side is always the primary. That
   changes the follower protocol anyway, which is the cheapest moment to move to the shared model.
8. **A need to list or revoke every outside party in one place** (an audit, or cutting off everything one account
   set up). Deactivating an account or resetting its password already revokes its tokens across the three by
   scope, so this is the weakest of the triggers.

When none of these holds, the decision stands.

## Related

- Followers: [docs/FOLLOWERS.md](../FOLLOWERS.md).
- Compute workers and on-demand machines: [docs/ON_DEMAND_WORKERS.md](../ON_DEMAND_WORKERS.md).
- A follower's own fields (`@local.`), which a labelling tool would build on: [docs/FOLLOWERS.md](../FOLLOWERS.md).
