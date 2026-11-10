"""Patch searches that run on a follower (docs/FOLLOWERS.md). A follower can hold patch vectors its primary doesn't,
computed on its own machine. A patch search on the primary on such a scale is relayed: the primary queues it in
follower_searches, the follower, which long-polls for them over its own connection, runs it on its mirror with the
same search as the primary would (patch_search.nearest_patches) and answers with the primary's sample ids, and the
primary shows the samples from its own tables. The primary never connects to the follower.

The queue lives in the primary's database: the backend runs several processes, so the waiting search and the
follower's poll can be in different ones.
"""

import asyncio
import logging
import time
from typing import Any

from fastapi import HTTPException, Request

from . import patch_search
from .database import db
from .sample_ids import ELEMENT_OPERATORS, SAMPLE_ID_FIELDS, SAMPLE_ID_OPERATORS, sample_ids_in, translate_ids

logger = logging.getLogger('fridge.follower_search')

RELAY_SECONDS = 30  # the longest a primary's search waits for its follower's answer (under nginx's 60 s)
CLAIM_WAIT_SECONDS = 25  # the longest a follower's poll waits for a search
CLAIM_POLL_SECONDS = 0.25
ANSWER_POLL_SECONDS = 0.1
FALLBACK_SECONDS = 3  # unclaimed this long, a search goes to the next provider that's online
PENDING_PER_FOLLOWER = 8  # searches queued or running on one follower; more are refused (429)
KEEP_SECONDS = 3600  # answered or expired searches are deleted after this
FOLLOWER_CONCURRENCY = 4  # searches a follower runs at once
POLL_RETRY_SECONDS = 20  # after the primary failed to answer a poll
OLD_PRIMARY_SECONDS = 600  # a primary without the search endpoints is asked again after this
LOCAL_CACHE_SECONDS = 60  # how long whether a dataset holds a patch scale itself is remembered
MAX_LIMIT = 5000  # the most results a primary may ask a follower for in one search
MAX_PER_IMAGE = 10

# Filter operators a relayed search may carry: all evaluate the same on a mirror, whose samples, fields, hashes and
# map coordinates are the primary's. Not concepts (each instance has its own) or a selection made on the primary
# (in_selection, a token stored there). Anything not listed is refused rather than passed on.
RELAYED_OPERATORS = frozenset(
    {
        '=',
        '!=',
        '>',
        '>=',
        '<',
        '<=',
        'contains',
        'in',
        'not in',
        'is_null',
        'is_not_null',
        'exists',
        'not_exists',
        'geo_polygon',
        'geo_polygon_exclude',
        'geo_near',
        'embedding_polygon',
        'embedding_polygon_exclude',
        'elem_match',
        'match_elements',
        'collapse_duplicates',
        'cg_self',
        'parent_matches',
        'has_matching_object',
    },
)
LOGIC = frozenset({'AND', 'OR', 'NOT'})


class RelayRefused(ValueError):
    """A relayed search this follower can't run as asked: the message says why."""


# --- On the primary -------------------------------------------------------------------------------------------


async def providers(source, dataset_id: int, base_field: str, scale: str) -> list[dict]:
    """The followers that reported this patch scale of the dataset, the first reporter first, each with whether it
    checked in within patch_search.ONLINE_SECONDS."""
    rows = await source.fetch(
        f"""SELECT f.id, f.name, f.last_seen_at,
                  COALESCE(f.last_seen_at > now() - interval '{patch_search.ONLINE_SECONDS} seconds', FALSE) AS online
           FROM follower_fields r JOIN followers f ON f.id = r.follower_id
           WHERE r.dataset_id = $1 AND r.base_field = $2 AND r.scale = $3
           ORDER BY r.first_reported_at, f.name""",
        dataset_id,
        base_field,
        scale,
    )
    return [dict(row) for row in rows]


_held_here: dict[tuple[int, str, str], tuple[float, bool]] = {}


async def held_here(source, dataset_id: int, base_field: str, scale: str) -> bool:
    """Whether this dataset holds the patch scale itself (its first tile has a vector here). Asked only when a
    follower reports the scale, and remembered for LOCAL_CACHE_SECONDS: without an index on field_name, the answer
    "no" probes every sample of the dataset (2.7.0's per-dataset field registry makes it a lookup)."""
    key = (dataset_id, base_field, scale)
    cached = _held_here.get(key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    held = bool(
        await source.fetchval(
            """SELECT 1 FROM embeddings e JOIN samples s ON e.sample_id = s.id
               WHERE s.dataset_id = $1 AND e.field_name = $2 LIMIT 1""",
            dataset_id,
            f'{base_field}_patch_{scale}_r0c0',
        ),
    )
    _held_here[key] = (time.monotonic() + LOCAL_CACHE_SECONDS, held)
    return held


async def relayable(source, dataset_id: int, base_field: str, scale: str) -> bool:
    """Whether a patch search on this scale goes to a follower: one reports it, and the dataset doesn't hold it."""
    return bool(await providers(source, dataset_id, base_field, scale)) and not await held_here(
        source,
        dataset_id,
        base_field,
        scale,
    )


def _seen(follower: dict) -> str:
    return follower['last_seen_at'].strftime('%Y-%m-%d %H:%M UTC') if follower['last_seen_at'] else 'never'


async def relay(request: Request, dataset_id: int, base_field: str, scale: str, kind: str, body: dict) -> list[dict]:
    """Run a patch search on the follower that holds this scale, and return its answer as rows like
    patch_search.nearest_patches'. 503 when no follower holding it is online, 429 when it's busy, 504 when it doesn't
    answer within RELAY_SECONDS, 502 when it can't run the search."""
    candidates = await providers(db, dataset_id, base_field, scale)
    if not candidates:
        raise HTTPException(status_code=404, detail=f'No {base_field} {scale} patch vectors here or on a follower')
    online = [follower for follower in candidates if follower['online']]
    if not online:
        first = candidates[0]
        raise HTTPException(
            status_code=503,
            detail=f"These {scale} patches are on the follower '{first['name']}', which is offline (last seen "
            f'{_seen(first)}): try again once it is running',
        )
    provider, fallback = online[0], (online[1] if len(online) > 1 else None)
    await db.execute(
        f"""DELETE FROM follower_searches WHERE created_at < now() - interval '{KEEP_SECONDS} seconds'""",
    )
    # A soft cap: two searches checking at once can both get in, which is fine for protecting the follower.
    busy = await db.fetchval(
        "SELECT count(*) FROM follower_searches WHERE follower_id = $1 AND state IN ('pending', 'claimed')",
        provider['id'],
    )
    if busy >= PENDING_PER_FOLLOWER:
        raise HTTPException(
            status_code=429,
            detail=f"The follower '{provider['name']}' is busy with other searches: try again in a moment",
        )
    search_id = await db.fetchval(
        """INSERT INTO follower_searches (follower_id, dataset_id, kind, request)
           VALUES ($1, $2, $3, $4::jsonb) RETURNING id""",
        provider['id'],
        dataset_id,
        kind,
        body,
    )
    started = time.monotonic()
    try:
        while True:
            row = await db.fetchrow(
                'SELECT follower_id, state, result, error FROM follower_searches WHERE id = $1',
                search_id,
            )
            if row is None:  # its follower was removed
                raise HTTPException(
                    status_code=503,
                    detail=f"The follower '{provider['name']}' no longer follows this dataset",
                )
            if row['state'] == 'answered':
                return await hydrate(dataset_id, row['result'] or [])
            if row['state'] == 'failed':
                raise HTTPException(
                    status_code=502,
                    detail=f"The follower '{provider['name']}' couldn't run this search: {row['error']}",
                )
            if row['state'] == 'cancelled':
                # Its follower stopped following the dataset (unfollowed, or POST /follower/leave): the fallback takes
                # it, or it ends now.
                moved = None
                if fallback is not None:
                    moved = await db.fetchval(
                        f"""UPDATE follower_searches SET follower_id = $2, state = 'pending' WHERE id = $1
                             AND state = 'cancelled'
                             AND (SELECT count(*) FROM follower_searches
                                  WHERE follower_id = $2 AND state IN ('pending', 'claimed')) < {PENDING_PER_FOLLOWER}
                           RETURNING follower_id""",
                        search_id,
                        fallback['id'],
                    )
                if moved is None or fallback is None:
                    raise HTTPException(
                        status_code=503,
                        detail=f"The follower '{provider['name']}' no longer follows this dataset",
                    )
                provider, fallback = fallback, None
                continue
            waited = time.monotonic() - started
            if row['state'] == 'pending' and fallback is not None and waited > FALLBACK_SECONDS:
                # The fallback takes it only within its own cap (a soft cap, as above).
                moved = await db.fetchval(
                    f"""UPDATE follower_searches SET follower_id = $2 WHERE id = $1 AND state = 'pending'
                         AND (SELECT count(*) FROM follower_searches
                              WHERE follower_id = $2 AND state IN ('pending', 'claimed')) < {PENDING_PER_FOLLOWER}
                       RETURNING follower_id""",
                    search_id,
                    fallback['id'],
                )
                if moved is not None:
                    provider, fallback = fallback, None
            if waited > RELAY_SECONDS or await request.is_disconnected():
                break
            await asyncio.sleep(ANSWER_POLL_SECONDS)
    finally:
        await db.execute(
            "UPDATE follower_searches SET state = 'cancelled' WHERE id = $1 AND state IN ('pending', 'claimed')",
            search_id,
        )
    raise HTTPException(
        status_code=504,
        detail=f"The follower '{provider['name']}' didn't answer this search within {RELAY_SECONDS} s",
    )


async def hydrate(dataset_id: int, results: list[dict]) -> list[dict]:
    """The follower's answer as search rows, from this dataset's own samples: only ids in this dataset count, so a
    follower can't read samples of a dataset it doesn't follow. Samples deleted here since its copy drop out."""
    ids = [entry['origin_id'] for entry in results]
    samples = {
        row['id']: row
        for row in await db.fetch(
            """SELECT id, filepath, metadata, media_type, tags, ST_AsGeoJSON(location)::json AS location
               FROM samples WHERE dataset_id = $1 AND id = ANY($2::int[])""",
            dataset_id,
            ids,
        )
    }
    rows = []
    for entry in results:
        sample = samples.get(entry['origin_id'])
        if sample is not None:
            rows.append(
                {
                    'sample_id': sample['id'],
                    'field_name': entry['field'],
                    'distance': entry['distance'],
                    **{key: sample[key] for key in ('filepath', 'metadata', 'media_type', 'tags', 'location')},
                },
            )
    return rows


async def claim(follower: dict, wait: float) -> dict | None:
    """The oldest search queued for this follower, now claimed by it, waiting up to `wait` seconds for one. Each
    check takes a connection of its own, so a waiting poll holds none."""
    deadline = time.monotonic() + wait
    while True:
        row = await db.fetchrow(
            # Only of a dataset it still follows: one queued just as its subscription went isn't its to run.
            """UPDATE follower_searches SET state = 'claimed', claimed_at = now()
               WHERE id = (SELECT s.id FROM follower_searches s
                           WHERE s.follower_id = $1 AND s.state = 'pending'
                             AND EXISTS (SELECT 1 FROM follower_datasets f
                                         WHERE f.follower_id = s.follower_id AND f.dataset_id = s.dataset_id)
                           ORDER BY s.created_at FOR UPDATE OF s SKIP LOCKED LIMIT 1)
               RETURNING id, dataset_id, kind, request""",
            follower['id'],
        )
        if row is not None:
            return {
                'id': str(row['id']),
                'dataset_id': row['dataset_id'],
                'kind': row['kind'],
                'request': row['request'],
            }
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(min(CLAIM_POLL_SECONDS, max(0.0, deadline - time.monotonic())))


async def answer(follower: dict, search_id: str, results: list[dict] | None, error: str | None) -> bool:
    """Record a follower's answer to a search it claimed: its results (the primary's sample ids), or why it couldn't
    run it. False when the search isn't its, or was cancelled meanwhile."""
    request = await db.fetchval(
        "SELECT request FROM follower_searches WHERE id = $1::uuid AND follower_id = $2 AND state = 'claimed'",
        search_id,
        follower['id'],
    )
    if request is None:
        return False
    if results is not None and len(results) > int(request.get('limit', 0)):
        error, results = f"answered {len(results)} results for a limit of {request.get('limit')}", None
    status = await db.execute(
        """UPDATE follower_searches SET state = $3, result = $4::jsonb, error = $5, answered_at = now()
           WHERE id = $1::uuid AND follower_id = $2 AND state = 'claimed'""",
        search_id,
        follower['id'],
        'failed' if error is not None else 'answered',
        results,
        error,
    )
    return status.endswith(' 1')


# --- On the follower --------------------------------------------------------------------------------------------


def check_filters(node: Any) -> None:
    """Refuse a relayed filter this instance can't evaluate as the primary would (RELAYED_OPERATORS)."""
    if node is None:
        return
    if not isinstance(node, dict):
        raise RelayRefused('a filter node is not an object')
    if 'filters' in node or 'logic' in node:
        if node.get('logic', 'AND') not in LOGIC or not isinstance(node.get('filters', []), list):
            raise RelayRefused(f"a filter group's logic {node.get('logic')!r} isn't known here")
        for child in node.get('filters', []):
            check_filters(child)
        return
    operator = node.get('operator')
    if operator in ('concept_select', 'concept_exclude', 'concept_present'):
        raise RelayRefused('a filter on a concept: concepts belong to each instance, so search on the follower')
    if operator == 'in_selection':
        raise RelayRefused('a selection made on the primary: save it as a view first')
    if operator not in RELAYED_OPERATORS:
        raise RelayRefused(f'the filter operator {operator!r} is not one a follower runs')
    if node.get('field') in SAMPLE_ID_FIELDS and operator not in SAMPLE_ID_OPERATORS:
        raise RelayRefused(
            f"{node['field']} {operator} on sample ids: a follower numbers its samples differently, so only =, !=, in "
            'and not in on them are passed on',
        )
    # Nested sample filters (parent_matches, has_matching_object) are checked like the rest. An element match's value
    # is a condition on each label element's own values, the same on a mirror: nothing in it to translate or refuse.
    value = node.get('value')
    if operator not in ELEMENT_OPERATORS and isinstance(value, dict) and ('filters' in value or 'operator' in value):
        check_filters(value)


async def run_search(pool, link_id: int, item: dict) -> list[dict]:
    """Run a search the primary relayed here, on its mirror, and return the answer in the primary's sample ids:
    [{origin_id, field, distance}]. RelayRefused says why it can't."""
    request = item['request']
    async with pool.acquire() as connection:
        mirror = await connection.fetchrow(
            "SELECT * FROM mirrors WHERE link_id = $1 AND origin_dataset_id = $2 AND state = 'following'",
            link_id,
            item['dataset_id'],
        )
        if mirror is None:
            raise RelayRefused('this instance holds no live mirror of that dataset')
        dataset_id = mirror['dataset_id']
        base_field, scale = request['base_field'], request['scale']
        limit, per_image = int(request['limit']), int(request.get('per_image', 1))
        if not (1 <= limit <= MAX_LIMIT and 1 <= per_image <= MAX_PER_IMAGE):
            raise RelayRefused(f'a search for {limit} results, {per_image} per image, is more than a follower runs')
        filters = request.get('filters') or None
        check_filters(filters)
        named = sample_ids_in(filters, set())
        seed_origin = request.get('sample_id')
        if seed_origin is not None:
            named.add(seed_origin)
        to_local = {
            row['origin_id']: row['sample_id']
            for row in await connection.fetch(
                'SELECT origin_id, sample_id FROM mirror_samples WHERE dataset_id = $1 AND origin_id = ANY($2::int[])',
                dataset_id,
                sorted(named),
            )
        }
        exclude = None
        if item['kind'] == 'patch':
            seed = to_local.get(seed_origin)
            tile = f"{base_field}_patch_{scale}_r{int(request['row'])}c{int(request['col'])}"
            vector = await connection.fetchrow(
                'SELECT vector, vector_dims(vector) AS dims FROM embeddings WHERE sample_id = $1 AND field_name = $2',
                seed,
                tile,
            )
            if seed is None or vector is None:
                raise RelayRefused(f'no {tile} vector here for sample {seed_origin}')
            seed_vector, dims, exclude = vector['vector'], vector['dims'], (seed, tile)
        elif item['kind'] == 'object_to_patch':
            seed_vector = request['seed_vector']
            dims = len(seed_vector.strip('[]').split(','))
        else:
            raise RelayRefused(f"a search of kind {item['kind']!r} isn't known here")
        rows = await patch_search.nearest_patches(
            connection,
            dataset_id=dataset_id,
            fields=await patch_search.indexed_patch_fields(connection, dataset_id, base_field, scale, dims),
            like_pattern=f'{base_field}_patch_{scale}_%',
            seed_vector=seed_vector,
            dims=dims,
            limit=limit,
            per_image=per_image,
            filters=translate_ids(filters, to_local) if filters else None,
            exclude=exclude,
        )
        to_origin = {
            row['sample_id']: row['origin_id']
            for row in await connection.fetch(
                'SELECT sample_id, origin_id FROM mirror_samples WHERE dataset_id = $1 AND sample_id = ANY($2::int[])',
                dataset_id,
                [row['sample_id'] for row in rows],
            )
        }
    return [
        {'origin_id': to_origin[row['sample_id']], 'field': row['field_name'], 'distance': float(row['distance'])}
        for row in rows
        if row['sample_id'] in to_origin
    ]


async def serve_link(pool, link_id: int, stopping: asyncio.Event) -> None:
    """Answer one primary's relayed searches, FOLLOWER_CONCURRENCY at a time, until stopping or the link is
    removed. A primary without the search endpoints (before protocol 2) is asked again every OLD_PRIMARY_SECONDS."""
    from . import follower_sync

    slots = asyncio.Semaphore(FOLLOWER_CONCURRENCY)
    running: set[asyncio.Task] = set()

    async def handle(client, item):
        try:
            try:
                results, error = await run_search(pool, link_id, item), None
            except RelayRefused as refused:
                results, error = None, str(refused)
            except Exception as failure:  # the primary's user gets an answer either way
                logger.exception('A relayed search failed here')
                results, error = None, f'{type(failure).__name__}: {failure}'[:500]
            await follower_sync.call(
                client,
                'POST',
                f"/follower/searches/{item['id']}",
                body={'results': results, 'error': error},
            )
        except Exception as failure:
            logger.warning('Could not answer relayed search %s: %s', item['id'], failure)
        finally:
            slots.release()

    while not stopping.is_set():
        link = await pool.fetchrow('SELECT * FROM primary_links WHERE id = $1', link_id)
        if link is None:
            break
        delay = 0.0
        try:
            async with follower_sync.connect(link, await follower_sync.installation_id(pool)) as client:
                try:
                    while not stopping.is_set():
                        await slots.acquire()
                        try:
                            item = await follower_sync.call(
                                client,
                                'GET',
                                '/follower/searches',
                                params={'wait': CLAIM_WAIT_SECONDS},
                            )
                        except BaseException:
                            slots.release()
                            raise
                        if not item:
                            slots.release()
                            continue
                        task = asyncio.create_task(handle(client, item))
                        running.add(task)
                        task.add_done_callback(running.discard)
                finally:
                    # The searches already running answer through this client: let them, before it closes (a
                    # failed poll, the primary restarting, or stopping).
                    if running:
                        await asyncio.wait(set(running), timeout=RELAY_SECONDS)
        except follower_sync.PrimaryRefused as refused:
            delay = OLD_PRIMARY_SECONDS if refused.status in (404, 405) else POLL_RETRY_SECONDS
        except Exception as failure:
            logger.info('Search poll of primary %s: %s', link_id, failure)
            delay = POLL_RETRY_SECONDS
        if delay:
            try:
                await asyncio.wait_for(stopping.wait(), delay)
            except (asyncio.TimeoutError, TimeoutError):
                pass
    for task in running:
        task.cancel()


async def serve(pool, stopping: asyncio.Event) -> None:
    """On a follower: answer the relayed searches of every primary it follows. Primaries added or removed are picked
    up within a minute."""
    tasks: dict[int, asyncio.Task] = {}
    while not stopping.is_set():
        try:
            links = {row['id'] for row in await pool.fetch('SELECT id FROM primary_links')}
        except Exception as failure:
            logger.info('Could not list primaries: %s', failure)
            links = set(tasks)
        for link_id in links - set(tasks):
            tasks[link_id] = asyncio.create_task(serve_link(pool, link_id, stopping))
        for link_id in [link_id for link_id, task in tasks.items() if task.done()]:
            del tasks[link_id]
        try:
            await asyncio.wait_for(stopping.wait(), 60)
        except (asyncio.TimeoutError, TimeoutError):
            pass
    for task in tasks.values():
        task.cancel()
    await asyncio.gather(*tasks.values(), return_exceptions=True)
