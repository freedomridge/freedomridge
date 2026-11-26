"""The administration tools (backend/assistant/admin.py), the fence around them, and GET /api/admin/jobs/all: who may
call them and when, what they read, and that nothing of a private field's values or a closed dataset's samples, and
nothing the contract keeps out of every answer, reaches one."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import mcp_types as types
import pytest

from backend import assistant, backups, mcp_door
from backend.assistant import admin, inner, private, rule
from backend.assistant import api as assistant_api
from backend.assistant.identity import Identity, administrators_route
from backend.tests import test_assistant_core as core
from backend.tests.test_field_registry_postgres import ingest

registry_database = core.registry_database
world = core.world
ADMIN, POWER, TRIALS, CLOSED = core.ADMIN, core.POWER, core.TRIALS, core.CLOSED
KEPT = 4  # an open dataset with a private field
PLANT = 'ZQXPLANT7731'  # a value of the private field, quoted by errors, messages and an index
SECRETS = ('ops@example.org', '10.20.30.40', 'postgres://fr:hunter2@db.internal:5432/fr', '/var/lib/data/x.db')
NAMES = [tool.name for tool in assistant.admin_tools()]


async def switch(w: core.World, **settings: Any) -> None:
    response = await w.api(ADMIN, 'PATCH', '/api/admin/assistant', json=settings)
    assert response.status_code == 200, response.text


async def job(w: core.World, kind: str, state: str, **columns: Any) -> uuid.UUID:
    """A job row as the queue leaves one (state, times, error), written directly."""
    job_id = uuid.uuid4()
    created = columns.pop('created_at', datetime.now(timezone.utc))
    running = state == 'running'
    await w.database.execute(
        """INSERT INTO jobs (id, kind, dataset_id, args, state, message, result, error, created_at, updated_at,
               available_at, started_at, finished_at, lease_token, lease_expires_at, worker_id, created_by)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9, $9, $9, $10, $11, $12, $13, $14)""",
        job_id,
        kind,
        columns.get('dataset_id'),
        columns.get('args', {}),
        state,
        columns.get('message'),
        columns.get('result'),
        columns.get('error'),
        created,
        None if state in ('queued', 'running') else created + timedelta(minutes=1),
        uuid.uuid4() if running else None,
        datetime.now(timezone.utc) + timedelta(minutes=1) if running else None,
        'test-worker' if running else None,
        columns.get('created_by'),
    )
    return job_id


async def keep_a_field(w: core.World) -> None:
    await w.database.execute("INSERT INTO datasets (id, name, is_global) VALUES ($1, 'kept-fields', FALSE)", KEPT)
    await ingest([{'secret': PLANT, 'crop': 'corn'}], dataset_id=KEPT)
    marked = await w.api(
        ADMIN,
        'PATCH',
        f'/api/datasets/{KEPT}/fields/secret/description',
        json={'values_private': True},
    )
    assert marked.status_code == 200, marked.text


def leaks(answer: Any) -> list[str]:
    text = json.dumps(answer)
    return [value for value in (PLANT, *SECRETS) if value in text]


# --- the words of a route, a definition and a free text ---


def test_the_administrators_routes_by_path():
    for path in (
        '/api/admin',
        '/api/admin/status',
        '/api/admin/jobs/all',
        '/api/users',
        '/api/users/names',
        '/api/groups',
        '/api/followers/3',
        '/api/primaries',
        '/api/storage/configs/2',
        '/api/compute-launcher',
        '/api/workers',
        '/api/datasets/7/index-advisor',
        '/api/datasets/7/index-advisor/review',
    ):
        assert administrators_route(path), path
    for path in ('/api/datasets', '/api/datasets/7', '/api/datasets/7/schema', '/api/usersx', '/api/administer'):
        assert not administrators_route(path), path


def test_a_definition_is_kept_when_it_holds_a_quoted_text_the_server_does_not_write():
    given = [
        'CREATE INDEX idx_samples_meta_7_crop ON public.samples USING btree (((metadata ->> \'crop\'::text))) '
        'WHERE (dataset_id = 7)',
        "CREATE INDEX a ON public.samples USING btree ((metadata -> 'box'::text))",
        "CREATE INDEX a ON public.samples USING btree (id) WHERE (metadata ? 'crop'::text)",
        'CREATE UNIQUE INDEX jobs_pkey ON public.jobs USING btree (id)',
        # A vector index's field, and storage parameters: what the server writes itself.
        'CREATE INDEX hnsw_emb_d7_clip ON public.embeddings_d7 USING hnsw (((vector)::vector(4)) vector_cosine_ops) '
        "WITH (m='16', ef_construction='64') WHERE (field_name = 'clip'::text)",
        "CREATE INDEX a ON public.samples USING btree (id) WITH (fillfactor='70', deduplicate_items=off)",
        # The server's own numbers, and digits that aren't numbers.
        'CREATE INDEX a ON public.t USING btree (((a)::numeric(10,2)), ((b)::character varying(255))) '
        'WITH (fillfactor=70)',
        'CREATE INDEX a ON public.t USING btree (name COLLATE "C.UTF-8" text_pattern_ops)',
        # Each type PostgreSQL prints with a modifier.
        'CREATE INDEX a ON public.t USING btree (((a)::character varying(255)), ((b)::bit varying(8)), ((c)::bit(8)))',
        'CREATE INDEX a ON public.t USING btree (((a)::timestamp(3) without time zone), ((b)::time(3) with time zone))',
        'CREATE INDEX a ON public.t USING btree (((a)::interval(3)), ((b)::interval second(3)))',
        'CREATE INDEX a ON public.t USING btree (((a)::interval day to second(3)), ((b)::double precision))',
        'CREATE INDEX a ON public.t USING btree (((v)::vector(4)), ((w)::public.vector(4)), ((n)::numeric(10,2)))',
        'CREATE INDEX a ON public.samples USING btree (dataset_id, created_at)',
        'CREATE INDEX a ON public.t USING btree (a) WHERE ((b IS NOT NULL) AND (c = true))',
    ]
    kept = [
        "CREATE INDEX a ON public.samples USING btree (((metadata ->> 'code'::text))) WHERE "
        f"((metadata ->> 'code'::text) = '{PLANT}'::text)",
        "CREATE INDEX a ON public.jobs USING btree (created_at) WHERE (state = 'queued'::text)",
        "CREATE INDEX a ON public.samples USING btree ((local_metadata ->> 'x'::text))",
        "CREATE INDEX a ON public.samples USING btree ((metadata ->> E'x'::text))",
        'CREATE INDEX a ON public.samples USING btree (id) WHERE (filepath = $$secret$$)',
        "CREATE INDEX a ON public.samples USING btree (id) WHERE (filepath = 'never closed",
        # Numbers are values too: PostgreSQL prints them bare.
        "CREATE INDEX a ON public.samples USING btree (id) WHERE ((((metadata ->> 'plot'::text))::integer = 4417) "
        'AND (dataset_id = 7))',
        "CREATE INDEX a ON public.samples USING btree (id) WHERE (((metadata ->> 'yield'::text))::double precision "
        '> 9000.5)',
        "CREATE INDEX a ON public.samples USING btree (id) WHERE (((metadata ->> 'farm'::text))::integer = ANY "
        '(ARRAY[12, 31, 407]))',
        'CREATE INDEX a ON public.t USING btree (a) WHERE (b > (-5))',
        # A number after a cast and a bare word is no type's modifier.
        "CREATE INDEX a ON public.t USING btree (a) WHERE ((((metadata ->> 'a'::text))::boolean AND f(4417)))",
        "CREATE INDEX a ON public.t USING btree (a) WHERE ((((metadata ->> 'a'::text))::integer OR g(12)))",
        "CREATE INDEX a ON public.t USING btree (a) WHERE (b > '-5'::integer)",
    ]
    for definition in given:
        assert not private.definition_kept(definition), definition
    for definition in kept:
        assert private.definition_kept(definition), definition
    reason = f"Its columns ((metadata ->> 'crop'::text), (x = '{PLANT}'::text)) lead idx_b"
    assert private.quoted_kept(reason) == "Its columns ((metadata ->> 'crop'::text), (x = '…'::text)) lead idx_b"
    numbers = "Its columns ((((metadata ->> 'plot'::text))::integer = 4417), (y > 9000.5)) lead idx_b (dataset_id = 7)"
    assert private.quoted_kept(numbers) == (
        "Its columns ((((metadata ->> 'plot'::text))::integer = …), (y > …)) lead idx_b (dataset_id = 7)"
    )
    # Every definition is judged by what it quotes, whatever kind the advisor guessed from its name; one of
    # schema.sql's own indexes (the same name on the same table) is given whole whatever it quotes.
    # name -> (table, the constants schema.sql's statement holds)
    shipped = {'idx_jobs_ready': ('jobs', {'queued'}), 'idx_has': ('storage_configs', {'1', '0'})}
    has = 'CREATE INDEX idx_has ON public.storage_configs USING btree (array_length(bucket_patterns, 1)) WHERE '
    for bound, kept_back in (('0', False), ('7', True)):  # a look-alike that changes only a number: withheld
        entry = {
            'name': 'idx_has',
            'table': 'storage_configs',
            'definition': f'{has}(array_length(bucket_patterns, 1) > {bound})',
        }
        assert private._definition_withheld(entry, shipped) is kept_back
    ready = {'name': 'idx_jobs_ready', 'table': 'jobs', 'kind': 'core', 'definition': kept[1]}
    elsewhere = {**ready, 'table': 'samples'}  # its name on another table: a person's
    remade = {**ready, 'definition': kept[1].replace("'queued'", f"'{PLANT}'")}  # its name, a value of a person's
    named_like = [
        {'name': 'idx_samples_site_gin', 'table': 'samples', 'kind': 'array', 'definition': kept[0]},
        {'name': 'idx_samples_meta_7_hot', 'table': 'samples', 'kind': 'field', 'definition': kept[0]},
        {'name': 'samples_planted_excl', 'table': 'samples', 'kind': 'core', 'definition': kept[0], 'needed': [reason]},
    ]
    plain = {'name': 'a', 'table': 'samples', 'kind': 'other', 'definition': given[0]}
    huge = {'name': 'huge', 'table': 'samples', 'kind': 'other', 'definition': 'CREATE INDEX huge ' + 'x' * 2000}
    pair = {'redundant': named_like[0], 'kept': ready, 'rule': 'prefix', 'reason': reason}
    own = {'redundant': ready, 'kept': plain, 'rule': 'prefix', 'reason': "Its columns ((state = 'queued'::text))"}
    out = private._index_words_kept(
        {'review': {'too_new': [ready, elsewhere, *named_like, plain, huge, remade], 'overlapping': [pair, own]}},
        shipped,
    )
    entries = out['review']['too_new']
    assert [entry['definition_withheld'] for entry in entries] == [False, True, True, True, True, False, True, True]
    assert entries[0]['definition'] == kept[1] and entries[5]['definition'] == given[0]
    assert all(entry['definition'] is None for entry in entries[1:5] + entries[6:])
    assert entries[4]['needed'] == [private.quoted_kept(reason)]  # its own words, quoted values written '…'
    assert "'…'" in out['review']['overlapping'][0]['reason']
    assert out['review']['overlapping'][1]['reason'] == own['reason']  # both given whole: as written
    assert PLANT not in json.dumps(out)


def test_free_text_never_holds_what_no_answer_may():
    text = (
        'pg_dump: error: connection to server at "db.internal" (10.20.30.40), port 5432 failed; mail ops@example.org; '
        'url postgres://fr:hunter2@db.internal:5432/fr; socket /var/run/postgresql/.s.PGSQL.5432; '
        'file /mnt/backups/freedomridge_20261019T055351Z.sql.gz; password=hunter2 token: abc; ipv6 fe80::1 ::1; '
        'commit 3049ae8f1c2b tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904; key AKIAABCDEFGHIJKLMNOP; '
        'free 5368709120 bytes, id 7b2e1d, word deadbeef; '
        'token-like Xy3kQ9vLm2Np8RtW5zBc7DfG1hJ4 at 2026-11-18 03:00:04 cast (metadata->>\'x\')::float'
    )
    said = admin._said(text, '/mnt/backups')
    assert said is not None
    for gone in (
        '10.20.30.40',
        'ops@example.org',
        'hunter2',
        '/var/run/postgresql',
        'fe80::1',
        '3049ae8f1c2b',
        '4b825dc642cb6eb9a060e54bf8d69288fbee4904',
        'AKIAABCDEFGHIJKLMNOP',
        'Xy3kQ9vLm2Np8RtW5zBc7DfG1hJ4',
    ):
        assert gone not in said, (gone, said)
    for kept in (
        '/mnt/backups/freedomridge_20261019T055351Z.sql.gz',
        '2026-11-18 03:00:04',
        '::float',
        'port 5432',
        'free 5368709120 bytes, id 7b2e1d, word deadbeef',  # ordinary numbers and words stay
    ):
        assert kept in said, (kept, said)
    assert len(admin._said('x' * 2000) or '') <= 500


# --- who may call them, and when ---


def test_only_an_administrator_with_the_switch_on_and_the_door_open(world):
    async def scenario(w: core.World):
        assert len(assistant.tools()) == 13 and NAMES == ['system_status', 'backup_status', 'list_jobs', 'index_advice']
        assert core.refused(await w.tool(ADMIN, 'system_status'), 'not_allowed') == rule.MCP_OFF
        await w.switch_on(power_user=True)
        assert core.refused(await w.tool(ADMIN, 'system_status'), 'not_allowed') == rule.ADMIN_TOOLS_OFF
        assert await assistant.tools_for(user_id=ADMIN, door='mcp') == assistant.tools()
        await switch(w, admin_tools=True)
        assert (await w.api(ADMIN, 'GET', '/api/admin/assistant')).json()['admin_tools'] is True
        listed = await assistant.tools_for(user_id=ADMIN, door='mcp')
        assert [tool.name for tool in listed] == [tool.name for tool in assistant.tools()] + NAMES
        for name in NAMES:
            assert (await w.tool(ADMIN, name))['ok'], name
            assert core.refused(await w.tool(POWER, name), 'not_allowed') == rule.ADMINS_ONLY
        assert await assistant.tools_for(user_id=POWER, door='mcp') == assistant.tools()
        assert await assistant.tools_for(user_id=ADMIN, door='chat') == assistant.tools()
        assert core.refused(await w.tool(ADMIN, 'no_such_tool'), 'unknown_tool')
        # A refusal needs no valid arguments: it comes first.
        assert core.refused(await w.tool(POWER, 'list_jobs', limit=0), 'not_allowed') == rule.ADMINS_ONLY
        assert core.refused(await w.tool(ADMIN, 'list_jobs', state='broken'), 'invalid')

    world(scenario)


def test_each_request_reads_the_role_and_the_switch_afresh(world):
    async def scenario(w: core.World):
        await w.switch_on(power_user=True)  # a power user may use the door: the role is what refuses them here
        await switch(w, admin_tools=True)
        handler = inner.route_handler('GET', admin.STATUS)
        identity = Identity(ADMIN, 'mcp', frozenset({('GET', handler)}), admin=True)
        status, _ = await core._raw(identity, 'GET', admin.STATUS)
        assert status == 200
        await switch(w, admin_tools=False)
        status, body = await core._raw(identity, 'GET', admin.STATUS)
        assert (status, body['detail'], body['code']) == (403, rule.ADMIN_TOOLS_OFF, assistant_api.NOT_ALLOWED_CODE)
        await switch(w, admin_tools=True)
        await w.database.execute("UPDATE users SET role = 'power_user' WHERE id = $1", ADMIN)
        status, body = await core._raw(identity, 'GET', admin.STATUS)
        assert (status, body['detail']) == (403, rule.ADMINS_ONLY)

    world(scenario)


def test_no_other_tool_reaches_an_administrators_route_and_an_administration_tool_no_other_route(world):
    async def scenario(w: core.World):
        await w.switch_on()
        await switch(w, admin_tools=True)
        for method, template in private.SPANNING_ROUTES:
            path = template.format(id=TRIALS)
            if not administrators_route(path):
                continue
            handler = inner.route_handler(method, template)
            status, body = await core._raw(Identity(ADMIN, 'mcp', frozenset({(method, handler)})), method, path)
            assert (status, body['code']) == (403, assistant_api.ROUTE_REFUSED_CODE), (path, body)
            with pytest.raises(inner.RouteNotListed):
                await inner.request(
                    Identity(ADMIN, 'mcp'),
                    [(method, template)],
                    method,
                    template,
                    path_params={'id': 1},
                )
        for template in ('/api/datasets/{id}/schema', '/api/datasets/{id}'):
            with pytest.raises(inner.RouteNotListed):
                await inner.request(
                    Identity(ADMIN, 'mcp', admin=True),
                    [('GET', template)],
                    'GET',
                    template,
                    path_params={'id': TRIALS},
                )
        for tool in assistant.admin_tools():
            assert set(tool.routes) <= set(private.SPANNING_ROUTES), tool.name
            assert tool.kind == 'read' and all(method == 'GET' for method, _ in tool.routes)

    world(scenario)


# --- GET /api/admin/jobs/all ---


def test_the_jobs_of_the_whole_server(world):
    async def scenario(w: core.World):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=30)
        failed = await job(w, 'field_index_build', 'failed', dataset_id=TRIALS, error={'type': 'E', 'message': 'x'})
        made = await job(w, 'backup', 'succeeded', result={'file': 'f.sql.gz', 'size_bytes': 1, 'tables': 3, 'x': 'no'})
        await job(w, 'backup', 'failed', created_at=old, error={'type': 'E', 'message': 'old'})
        waiting = await job(w, 'map_refit', 'queued', dataset_id=TRIALS, created_at=old)
        review = await job(w, 'index_review', 'succeeded', args={'scope': 'dataset', 'dataset_id': CLOSED})
        week = (now - timedelta(days=7)).isoformat()
        response = await w.api(ADMIN, 'GET', '/api/admin/jobs/all', params={'since': week})
        page = response.json()
        assert response.status_code == 200 and set(page) == {
            'jobs',
            'total',
            'counts',
            'kinds',
            'offset',
            'next_offset',
        }
        # The window bounds the jobs that ended; the queued job made a month ago is in it, the old failure isn't.
        assert page['counts'] == {'queued': 1, 'running': 0, 'succeeded': 2, 'failed': 1, 'cancelled': 0}
        assert page['total'] == 4 and page['kinds'] == ['backup', 'field_index_build', 'index_review', 'map_refit']
        assert {item['id'] for item in page['jobs']} == {str(failed), str(waiting), str(review), str(made)}
        backup = next(item for item in page['jobs'] if item['id'] == str(made))
        assert backup['backup'] == {'file': 'f.sql.gz', 'size_bytes': 1, 'tables': 3}  # only what a backup holds
        first = next(item for item in page['jobs'] if item['id'] == str(failed))
        assert set(first) == {
            'id',
            'kind',
            'state',
            'dataset_id',
            'dataset_name',
            'field',
            'started_by',
            'created_at',
            'started_at',
            'finished_at',
            'progress',
            'failures',
            'max_failures',
            'cancel_requested',
            'error',
        }
        assert next(item for item in page['jobs'] if item['id'] == str(review))['dataset_id'] == CLOSED
        named = await w.api(ADMIN, 'GET', '/api/admin/jobs/all', params={'dataset_id': CLOSED, 'since': week})
        assert [item['id'] for item in named.json()['jobs']] == [str(review)]
        # counts are the dataset's; kinds are the window's, of every dataset.
        assert named.json()['counts']['succeeded'] == 1 and named.json()['kinds'] == page['kinds']
        every = await w.api(ADMIN, 'GET', '/api/admin/jobs/all', params={'dataset_id': TRIALS})
        assert {item['id'] for item in every.json()['jobs']} == {str(failed), str(waiting)}
        states = await w.api(
            ADMIN,
            'GET',
            '/api/admin/jobs/all',
            params=[('state', 'failed'), ('state', 'queued'), ('since', week), ('limit', '1')],
        )
        assert states.json()['total'] == 2 and len(states.json()['jobs']) == 1 and states.json()['next_offset'] == 1
        assert states.json()['counts'] == page['counts']  # counts don't depend on state
        kind = await w.api(ADMIN, 'GET', '/api/admin/jobs/all', params={'kind': 'backup'})
        assert kind.json()['counts']['failed'] == 1 and kind.json()['total'] == 2  # no window: the old one too
        failure = next(item for item in kind.json()['jobs'] if item['state'] == 'failed')
        assert failure['backup'] is None  # null until it has succeeded
        assert (await w.api(ADMIN, 'GET', '/api/admin/jobs/all', params={'kind': 'nothing'})).json()['jobs'] == []
        assert (await w.api(ADMIN, 'GET', '/api/admin/jobs/all', params={'dataset_id': 999})).status_code == 404
        assert (await w.api(POWER, 'GET', '/api/admin/jobs/all')).status_code == 403

    world(scenario)


# --- what an answer holds ---


def test_nothing_planted_reaches_an_answer(world, monkeypatch, tmp_path):
    async def scenario(w: core.World):
        await keep_a_field(w)
        await w.switch_on()
        await switch(w, admin_tools=True)
        quoting = {'type': 'InvalidTextRepresentationError', 'message': f'invalid input syntax: "{PLANT}"'}
        open_failed = await job(
            w,
            'field_index_build',
            'failed',
            dataset_id=TRIALS,
            error={'type': 'E', 'message': 'ok'},
        )
        await job(w, 'field_index_build', 'failed', dataset_id=KEPT, error=quoting, created_by=ADMIN)
        await job(w, 'field_index_build', 'failed', dataset_id=CLOSED, error=quoting)
        await job(w, 'statistics', 'running', dataset_id=KEPT, message=f'counting {PLANT}')
        await job(w, 'index_review', 'failed', args={'scope': 'dataset', 'dataset_id': CLOSED}, error=quoting)
        await job(w, 'index_review', 'failed', args={'scope': 'dataset', 'dataset_id': KEPT}, error=quoting)
        await job(w, 'dataset_deletion', 'failed', dataset_id=777, error=quoting)  # a dataset gone since
        quiet = await job(w, 'statistics', 'succeeded', dataset_id=CLOSED)  # nothing to keep back
        mixed = {'type': 'RuntimeError', 'message': 'BackupError: ' + ' '.join(SECRETS)}
        await job(w, 'backup', 'failed', error=mixed)
        dump = {'file': 'freedomridge_20261018T020000Z.sql.gz', 'size_bytes': 42, 'tables': 30}
        await job(w, 'backup', 'succeeded', result=dump, created_at=datetime.now(timezone.utc) - timedelta(days=1))
        # An index holding the value, and a review that saw it.
        await w.database.execute(
            f"CREATE INDEX idx_planted ON samples ((metadata->>'secret')) WHERE metadata->>'secret' = '{PLANT}'",
        )
        from backend import index_advisor

        review, _ = index_advisor.build_review(await index_advisor.collect(w.database.pool, None), None)
        assert any(entry['name'] == 'idx_planted' for entry in review['too_new'])
        await job(w, 'index_review', 'succeeded', args={'scope': 'system'}, result=review)
        dataset_review, _ = index_advisor.build_review(await index_advisor.collect(w.database.pool, CLOSED), CLOSED)
        await job(
            w,
            'index_review',
            'succeeded',
            args={'scope': 'dataset', 'dataset_id': CLOSED},
            result=dataset_review,
        )
        monkeypatch.setattr(backups, 'BACKUP_DIR', str(tmp_path))
        (tmp_path / 'freedomridge_20261019T055351Z.failed.json').write_text(json.dumps({'error': mixed['message']}))

        cases: list[tuple[str, dict[str, Any]]] = [
            ('system_status', {}),
            ('backup_status', {}),
            ('list_jobs', {}),
            ('list_jobs', {'state': 'failed'}),
            ('list_jobs', {'state': 'running'}),
            ('list_jobs', {'dataset': KEPT}),
            ('list_jobs', {'dataset': CLOSED, 'state': 'failed'}),
            ('index_advice', {}),
            *[('index_advice', {'section': section}) for section in admin.SECTIONS],
            ('index_advice', {'dataset': CLOSED}),
            ('index_advice', {'dataset': KEPT}),
        ]
        for name, arguments in cases:
            answer = await w.tool(ADMIN, name, **arguments)
            assert answer['ok'], (name, arguments, answer)
            assert not leaks(answer), (name, arguments, leaks(answer))
            assert len(json.dumps(answer['result'])) <= 20_000, (name, arguments)

        jobs = (await w.tool(ADMIN, 'list_jobs', state='failed'))['result']['jobs']
        words = {(item['dataset'] or {}).get('id'): item['error_withheld'] for item in jobs if item['error'] is None}
        assert words[CLOSED] == admin.WITHHELD[private.CLOSED]
        assert words[KEPT] == admin.WITHHELD[private.PRIVATE_FIELDS]
        assert words[777] == admin.WITHHELD[private.GONE]
        shown = next(item for item in jobs if item['id'] == str(open_failed))
        assert (
            shown['error'] == 'E: ok'
            and shown['error_withheld'] is None
            and shown['link'].endswith(f'/datasets/{TRIALS}')
        )
        backup = next(item for item in jobs if item['kind'] == 'backup')
        assert backup['error'].startswith('RuntimeError: BackupError:') and backup['link'].endswith('/settings/status')
        running = (await w.tool(ADMIN, 'list_jobs', state='running'))['result']['jobs'][0]
        assert running['progress']['message'] is None and running['error_withheld']
        done = (await w.tool(ADMIN, 'list_jobs', state='succeeded'))['result']['jobs']
        nothing = next(item for item in done if item['id'] == str(quiet))
        assert nothing['error'] is None and nothing['error_withheld'] is None
        assert set(nothing['progress']) == {'done', 'total', 'message'}
        too_new: list[dict[str, Any]] = []
        offset: Any = 0
        while offset is not None:  # every index of a new schema is too new to judge: many pages
            page = (await w.tool(ADMIN, 'index_advice', section='too_new', limit=50, offset=offset))['result']
            assert not leaks(page) and len(json.dumps(page)) <= 20_000
            too_new, offset = too_new + page['too_new'], page['next_offset']
        assert len(too_new) == page['totals']['too_new']
        planted = next(entry for entry in too_new if entry['name'] == 'idx_planted')
        assert planted['definition'] is None and planted['definition_withheld'] is True
        assert any(entry['definition'] and not entry['definition_withheld'] for entry in too_new)
        # Every index of the app's own kinds, in every list, carries the route's definition whole (the test schema's
        # name, `registry_test_` and 32 hexadecimal characters, is in each).
        route = (await w.api(ADMIN, 'GET', '/api/admin/index-advisor')).json()['review']
        for section in ('needed', 'too_new', 'possibly_unused'):
            given: list[dict[str, Any]] = []
            offset = 0
            while offset is not None:
                page = (await w.tool(ADMIN, 'index_advice', section=section, limit=50, offset=offset))['result']
                given, offset = given + page[section], page['next_offset']
            assert [entry['name'] for entry in given] == [entry['name'] for entry in route[section]], section
            for mine, theirs in zip(given, route[section]):
                if theirs['name'] != 'idx_planted':  # every other index of a new schema is the app's own
                    assert (mine['definition'], mine['definition_withheld']) == (theirs['definition'], False), mine
        assert route['needed'] and any(entry['name'].endswith('_pkey') for entry in route['needed'])
        closed = (await w.tool(ADMIN, 'index_advice', dataset=CLOSED))['result']
        assert closed['dataset'] == {'id': CLOSED, 'name': 'Closed one'} and closed['scope'] == 'dataset'
        assert closed['last_failure'] is None or closed['last_failure']['error'] is None
        state = (await w.tool(ADMIN, 'backup_status'))['result']
        assert state['level'] == 'failed' and state['folder'] == str(tmp_path)
        assert state['last_failure']['error'].startswith('BackupError:')
        assert state['recent_runs'][0]['state'] == 'failed' and state['recent_runs'][0]['file'] is None
        assert (state['recent_runs'][1]['file'], state['recent_runs'][1]['size_bytes']) == (dump['file'], 42)

    world(scenario)


def test_a_dataset_reviews_failure_is_kept_back_with_its_dataset(world):
    async def scenario(w: core.World):
        await keep_a_field(w)
        await w.switch_on()
        await switch(w, admin_tools=True)
        for dataset_id in (CLOSED, KEPT, TRIALS):
            await job(
                w,
                'index_review',
                'failed',
                args={'scope': 'dataset', 'dataset_id': dataset_id},
                error={'type': 'E', 'message': f'{PLANT} in {dataset_id}'},
            )
        await job(w, 'index_review', 'failed', args={'scope': 'system'}, error={'type': 'E', 'message': 'system went'})
        answers = {
            dataset_id: (await w.tool(ADMIN, 'index_advice', dataset=dataset_id))['result']['last_failure']
            for dataset_id in (CLOSED, KEPT, TRIALS)
        }
        assert answers[CLOSED]['error'] is None and answers[CLOSED]['error_withheld'] == admin.WITHHELD[private.CLOSED]
        assert answers[KEPT]['error'] is None
        assert answers[KEPT]['error_withheld'] == admin.WITHHELD[private.PRIVATE_FIELDS]
        assert answers[TRIALS] == {
            'finished_at': answers[TRIALS]['finished_at'],
            'error': f'{PLANT} in {TRIALS}',
            'error_withheld': None,
        }
        system = (await w.tool(ADMIN, 'index_advice'))['result']
        assert system['last_failure']['error'] == 'system went' and 'note' in system  # no review yet
        assert system['reviewed_at'] is None and system['overlapping'] == [] and system['totals']['needed'] == 0
        assert (
            core.refused(await w.tool(ADMIN, 'index_advice', dataset=999), 'not_found') == 'Dataset not found: id 999'
        )
        assert core.refused(await w.tool(ADMIN, 'index_advice', offset=5), 'invalid')

    world(scenario)


def test_index_advice_counts_each_list_and_pages_one(world):
    async def scenario(w: core.World):
        await w.switch_on()
        await switch(w, admin_tools=True)

        def entry(name: str, size: int, dataset_id: Any = None) -> dict[str, Any]:
            return {
                'name': name,
                'table': 'samples',
                'kind': 'ann' if dataset_id else 'other',
                'method': 'btree',
                'dataset_id': dataset_id,
                'field': None,
                'orphan': False,
                'definition': f'CREATE INDEX {name} ON public.samples USING btree (id) ' + 'x' * 600,
                'size_bytes': size,
                'scans': 1,
                'last_scan': None,
                'observed_since': None,
                'valid': True,
                'needed': [],
                'later_key': 'never shown',
            }

        pairs: list[dict[str, Any]] = [
            {'redundant': entry(f'r{n}', 100, CLOSED), 'kept': entry(f'k{n}', 200), 'rule': 'prefix', 'reason': 'why'}
            for n in range(40)
        ]
        unused = [{**entry('r0', 100), 'invalid': False, 'idle_days': 30.0}, {**entry('u1', 7), 'invalid': True}]
        result = {
            'scope': 'system',
            'dataset_id': None,
            'reviewed_at': '2026-11-18T03:00:04+00:00',
            'window_days': 7,
            'idle_days_required': 14,
            'enough_history': False,
            'possibly_unused': unused,
            'too_new': [],
            'building': [],
            'overlapping': pairs,
            'needed': [entry('n0', 5)],
            'missing_core': [],
        }
        await job(w, 'index_review', 'succeeded', args={'scope': 'system'}, result=result)
        answer = (await w.tool(ADMIN, 'index_advice'))['result']
        assert answer['totals'] == {
            'overlapping': 40,
            'possibly_unused': 2,
            'suggested': 0,
            'needed': 1,
            'too_new': 0,
            'removed': 0,
            'redundant_bytes': 40 * 100 + 7,  # r0 once
        }
        assert 0 < len(answer['overlapping']) < 20 and answer['possibly_unused'] and 'section' in answer['note']
        assert 'watched' in answer['note'] and len(json.dumps(answer)) <= 20_000
        assert answer['overlapping'][0]['redundant']['dataset'] == {'id': CLOSED, 'name': 'Closed one'}
        assert 'later_key' not in json.dumps(answer) and 'orphan' not in answer['overlapping'][0]['kept']
        assert answer['overlapping'][0]['kept']['definition'] == pairs[0]['kept']['definition']  # whole, never cut
        seen, offset = [], 0
        while offset is not None:
            page = (await w.tool(ADMIN, 'index_advice', section='overlapping', limit=50, offset=offset))['result']
            assert len(json.dumps(page)) <= 20_000
            seen += [pair['redundant']['name'] for pair in page['overlapping']]
            offset = page['next_offset']
        assert seen == [f'r{n}' for n in range(40)]

    world(scenario)


def test_the_apps_own_indexes_keep_their_definitions_whole(world):
    async def scenario(w: core.World):
        await w.switch_on()
        await switch(w, admin_tools=True)
        schema = 'registry_test_' + uuid.uuid4().hex  # a long hexadecimal name, as the test database's schema

        def entry(name: str, kind: str, definition: str, **more: Any) -> dict[str, Any]:
            return {
                'name': name,
                'table': 'samples',
                'kind': kind,
                'method': 'btree',
                'dataset_id': None,
                'field': None,
                'orphan': False,
                'definition': definition,
                'size_bytes': 8192,
                'scans': 0,
                'last_scan': None,
                'observed_since': None,
                'valid': True,
                'needed': [],
                **more,
            }

        own = [
            entry('samples_pkey', 'core', f'CREATE UNIQUE INDEX samples_pkey ON {schema}.samples USING btree (id)'),
            entry(
                'idx_jobs_ready',  # one of schema.sql's own, on its own table: whole, whatever it quotes
                'core',
                f'CREATE INDEX idx_jobs_ready ON {schema}.jobs USING btree (placement, available_at, created_at, id) '
                "WHERE (state = 'queued'::text)",
                table='jobs',
                needed=['only index for foreign key jobs_created_by_fkey (created_by → users)'],
            ),
            entry(
                'idx_samples_meta_7_crop',
                'field',
                f"CREATE INDEX idx_samples_meta_7_crop ON {schema}.samples USING btree (((metadata ->> 'crop'::text))) "
                'WHERE (dataset_id = 7)',
                dataset_id=7,
                field='crop',
            ),
            entry(
                'hnsw_emb_7_clip',
                'ann',
                f'CREATE INDEX hnsw_emb_7_clip ON {schema}.embeddings_d7 USING hnsw (vector vector_cosine_ops) '
                "WITH (m='16', ef_construction='64') WHERE (field_name = 'clip'::text)",
                dataset_id=7,
            ),
            entry(
                'idx_samples_tags_gin',
                'array',
                f'CREATE INDEX idx_samples_tags_gin ON {schema}.samples USING gin (tags)',
            ),
        ]
        hand = entry(
            'idx_samples_site_gin',  # named like an array field's index, quoting a value: judged all the same
            'array',
            f"CREATE INDEX idx_samples_site_gin ON {schema}.samples USING gin (((metadata -> 'labels'::text))) "
            f"WHERE ((metadata ->> 'site'::text) = '{PLANT}'::text)",
            needed=[f"backs a constraint on (site = '{PLANT}')"],
        )
        pair = {
            'redundant': own[1],
            'kept': own[0],
            'rule': 'prefix',
            'reason': "Its columns ((state = 'queued'::text))",
        }
        theirs = {'redundant': hand, 'kept': own[0], 'rule': 'prefix', 'reason': f"Its columns ((x = '{PLANT}'::text))"}
        result = {
            'scope': 'system',
            'dataset_id': None,
            'reviewed_at': '2026-11-18T03:00:04+00:00',
            'window_days': 7,
            'idle_days_required': 14,
            'enough_history': True,
            'possibly_unused': [{**index, 'invalid': False, 'idle_days': 20.0} for index in (*own, hand)],
            'too_new': [*own, hand],
            'building': [],
            'overlapping': [pair, theirs],
            'needed': own,
            'missing_core': [],
        }
        await job(w, 'index_review', 'succeeded', args={'scope': 'system'}, result=result)
        planted = {index['name']: index['definition'] for index in own}
        for section in ('needed', 'too_new', 'possibly_unused', 'overlapping'):
            answer = (await w.tool(ADMIN, 'index_advice', section=section, limit=50))['result']
            entries = answer[section]
            if section == 'overlapping':
                assert entries[0]['reason'] == pair['reason']  # the app's own indexes' words, as written
                assert entries[1]['reason'] == "Its columns ((x = '…'::text))"  # a person's index: its value hidden
                entries = [side for item in entries for side in (item['redundant'], item['kept'])]
            for index in entries:
                if index['name'] == 'idx_samples_site_gin':
                    assert (index['definition'], index['definition_withheld']) == (None, True)
                else:
                    assert (index['definition'], index['definition_withheld']) == (planted[index['name']], False)
            assert PLANT not in json.dumps(answer)
        needed = (await w.tool(ADMIN, 'index_advice', section='needed'))['result']['needed']
        assert needed[1]['needed'] == own[1]['needed']
        unused = (await w.tool(ADMIN, 'index_advice', section='possibly_unused', limit=50))['result']
        assert unused['possibly_unused'][-1]['needed'] == ["backs a constraint on (site = '…')"]

    world(scenario)


def test_everything_the_app_builds_comes_back_whole_and_a_persons_index_named_like_it_does_not(world):
    async def scenario(w: core.World):
        from backend import index_advisor, index_jobs

        await w.switch_on()
        await switch(w, admin_tools=True)
        built = []
        # A promoted field's index of each type, as index_jobs.field_handler builds it (without CONCURRENTLY here).
        for field_type, field in (('number', 'height'), ('string', 'crop'), ('datetime', 'taken_at')):
            name = f'idx_samples_meta_{TRIALS}_{field}'
            expression = index_jobs.field_index_expression(field_type, field)
            await w.database.execute(f'CREATE INDEX "{name}" ON samples {expression} WHERE dataset_id = {TRIALS}')
            built.append(name)
        # A vector index, as the vector index build makes it on the dataset's partition.
        vector = index_jobs.ann_index_name(TRIALS, 'clip')
        await w.database.execute(
            f'CREATE INDEX "{vector}" ON "{index_jobs.partition(TRIALS)}" USING hnsw ((vector::vector(4)) '
            "vector_cosine_ops) WHERE field_name = 'clip'",
        )
        # A registered list field's column and its index, as the field registration makes them.
        await w.database.execute('ALTER TABLE samples ADD COLUMN labels_list TEXT[]')
        await w.database.execute('CREATE INDEX idx_samples_labels_list_gin ON samples USING gin (labels_list)')
        built += [vector, 'idx_samples_labels_list_gin']
        # A person's indexes named like the app's, and a constraint of theirs, each quoting a value.
        persons = ['idx_samples_site_gin', f'idx_samples_meta_{TRIALS}_hot', 'samples_planted_excl']
        await w.database.execute(
            "CREATE INDEX idx_samples_site_gin ON samples USING gin ((metadata -> 'labels')) "
            f"WHERE metadata->>'site' = '{PLANT}'",
        )
        await w.database.execute(
            f"CREATE INDEX idx_samples_meta_{TRIALS}_hot ON samples ((metadata->>'serial')) "
            f"WHERE dataset_id = {TRIALS} AND metadata->>'serial' = '{PLANT}'",
        )
        await w.database.execute(
            f"ALTER TABLE samples ADD CONSTRAINT samples_planted_excl EXCLUDE USING btree (id WITH =) "
            f"WHERE (filepath = '{PLANT}')",
        )
        # One of schema.sql's own indexes, dropped and made again under its name on its table, quoting a value; another
        # changing only a number of its own.
        await w.database.execute('DROP INDEX idx_jobs_ready')
        await w.database.execute(f"CREATE INDEX idx_jobs_ready ON jobs (placement) WHERE state = '{PLANT}'")
        await w.database.execute('DROP INDEX idx_storage_configs_has_patterns')
        await w.database.execute(
            'CREATE INDEX idx_storage_configs_has_patterns ON storage_configs ((array_length(bucket_patterns, 1))) '
            'WHERE array_length(bucket_patterns, 1) > 7',
        )
        persons += ['idx_jobs_ready', 'idx_storage_configs_has_patterns']
        # Partial indexes for one plot, yield or farm: numbers that are a field's value, of a dataset with a private
        # field, of a closed one, and of none (a person's index named like nothing of the app's).
        await keep_a_field(w)
        values = ('77044177', '90004.75', '40733')
        await w.database.execute(
            f"CREATE INDEX idx_samples_meta_{KEPT}_plot ON samples ((metadata->>'plot')) "
            f"WHERE (metadata->>'plot')::integer = {values[0]} AND dataset_id = {KEPT}",
        )
        await w.database.execute(
            f"CREATE INDEX idx_samples_meta_{CLOSED}_farm ON samples ((metadata->>'farm')) "
            f"WHERE (metadata->>'farm')::integer = ANY (ARRAY[12, 31, {values[2]}]) AND dataset_id = {CLOSED}",
        )
        await w.database.execute(
            "CREATE INDEX idx_yield_high ON samples ((metadata->>'yield')) "
            f"WHERE (metadata->>'yield')::float > {values[1]}",
        )
        persons += [f'idx_samples_meta_{KEPT}_plot', f'idx_samples_meta_{CLOSED}_farm', 'idx_yield_high']
        # A person's index holding no constant at all: whole.
        await w.database.execute('CREATE INDEX idx_by_created ON samples (dataset_id, created_at)')
        built.append('idx_by_created')
        for dataset_id in (None, TRIALS, KEPT, CLOSED):
            review, _ = index_advisor.build_review(await index_advisor.collect(w.database.pool, dataset_id), dataset_id)
            await job(w, 'index_review', 'succeeded', args=index_advisor.scope_args(dataset_id), result=review)
        shipped = index_advisor.core_indexes()
        seen: dict[str, dict[str, Any]] = {}
        scopes: list[dict[str, Any]] = [{}, {'dataset': TRIALS}, {'dataset': KEPT}, {'dataset': CLOSED}]
        for arguments in scopes:
            for section in ('needed', 'too_new', 'possibly_unused', 'overlapping'):
                offset: Any = 0
                while offset is not None:
                    page = (await w.tool(ADMIN, 'index_advice', section=section, limit=50, offset=offset, **arguments))[
                        'result'
                    ]
                    text = json.dumps(page)
                    assert not any(value in text for value in (PLANT, *values)) and len(text) <= 20_000
                    for item in page[section]:
                        for entry in (item['redundant'], item['kept']) if section == 'overlapping' else (item,):
                            seen[entry['name']] = entry
                    offset = page['next_offset']
        actual = {
            row['name']: row['definition']
            for row in await w.database.fetch(
                "SELECT c.relname AS name, pg_get_indexdef(c.oid) AS definition FROM pg_class c "
                "WHERE c.relkind IN ('i', 'I') AND c.relnamespace = current_schema()::regnamespace",
            )
        }
        for name in built:
            assert (seen[name]['definition'], seen[name]['definition_withheld']) == (actual[name], False), name
        for name in persons:
            assert (seen[name]['definition'], seen[name]['definition_withheld']) == (None, True), name
        listed = [name for name in seen if name in shipped and name not in persons]
        assert len(listed) > 20, listed  # schema.sql's own indexes, in the system review's lists
        for name in listed:
            assert (seen[name]['definition'], seen[name]['definition_withheld']) == (actual[name], False), name
        constraints = [name for name in seen if name.endswith('_pkey')]
        assert constraints and all(seen[name]['definition'] == actual[name] for name in constraints)

    world(scenario)


def test_a_jobs_words_go_by_every_dataset_it_writes_and_else_by_a_kind_known_to_be_about_none(world):
    async def scenario(w: core.World):
        await keep_a_field(w)
        await w.switch_on()
        await switch(w, admin_tools=True)
        await w.database.execute("INSERT INTO datasets (id, name, is_global) VALUES (5, 'another-open', FALSE)")
        links = {}
        for name, mirrored in (('open', [TRIALS]), ('closed', [5, CLOSED]), ('private', [KEPT]), ('empty', [])):
            links[name] = await w.database.fetchval(
                "INSERT INTO primary_links (url, token_encrypted) VALUES ($1, 'x') RETURNING id",
                f'https://{name}.example.org/api',
            )
            for dataset_id in mirrored:
                await w.database.execute(
                    'INSERT INTO mirrors (dataset_id, link_id, origin_dataset_id, feed_cursor) VALUES ($1, $2, $1, 0)',
                    dataset_id,
                    links[name],
                )
        quoting = {'type': 'MirrorFailed', 'message': f'A mirror failed: invalid input syntax: "{PLANT}"'}
        timeout = {'type': 'MirrorFailed', 'message': 'A mirror failed: the primary took too long'}
        made = {
            name: await job(
                w,
                'follower_sync',
                'failed',
                args={'link_id': link},
                error=timeout if name == 'open' else quoting,
            )
            for name, link in links.items()
        }
        made['gone'] = await job(w, 'follower_sync', 'failed', args={'link_id': 999999}, error=quoting)
        made['mystery'] = await job(w, 'a_kind_added_later', 'failed', error=quoting)
        # A hand-made expression index removed through the advisor, whose restore failed on a sample's value.
        gain = "CREATE INDEX idx_gain ON public.samples USING btree ((((metadata ->> 'gain'::text))::double precision))"
        failed = {
            'type': 'InvalidTextRepresentationError',
            'message': f'invalid input syntax for type double precision: "{PLANT}"',
        }
        made['restore'] = await job(
            w,
            'index_restore',
            'failed',
            args={'index_name': 'idx_gain', 'kind': 'other', 'definition': gain},
            error=failed,
            message=f'Restoring idx_gain: "{PLANT}"',
        )
        await w.database.execute(
            'INSERT INTO removed_indexes (index_name, table_name, definition, kind, reason, dropped_at, '
            "restore_job_id) VALUES ('idx_gain', 'samples', $1, 'other', 'unused', clock_timestamp(), $2)",
            gain,
            made['restore'],
        )
        known = {
            kind: await job(w, kind, 'failed', args=args, error={'type': 'E', 'message': f'{kind} went'})
            for kind, args in (
                ('backup', {'keep': 7}),
                ('region_load', {'set': 'natural-earth-admin1'}),
                ('selection_cleanup', {}),
                ('index_review', {'scope': 'system'}),
                ('index_drop', {'index_name': 'x', 'kind': 'other', 'dataset_id': None}),
            )
        }
        answer = (await w.tool(ADMIN, 'list_jobs', state='failed', limit=50))['result']
        assert not leaks(answer)
        jobs = {item['id']: item for item in answer['jobs']}
        sentences = {name: jobs[str(job_id)]['error_withheld'] for name, job_id in made.items()}
        assert sentences == {
            'open': None,
            'closed': admin.WITHHELD[private.CLOSED],
            'private': admin.WITHHELD[private.PRIVATE_FIELDS],
            'empty': admin.WITHHELD[private.GONE],  # no mirror left: maybe deleted since
            'gone': admin.WITHHELD[private.GONE],
            'mystery': admin.WITHHELD[private.UNKNOWN_KIND],
            'restore': admin.WITHHELD[private.UNKNOWN_KIND],  # a rebuild reads every row the index covers
        }
        assert jobs[str(made['open'])]['error'] == 'MirrorFailed: A mirror failed: the primary took too long'
        assert all(jobs[str(item)]['error'] is None for name, item in made.items() if name != 'open')
        assert jobs[str(made['restore'])]['progress']['message'] is None
        # The planted value is in no answer of any tool.
        cases: list[tuple[str, dict[str, Any]]] = [
            ('list_jobs', {'state': 'failed', 'limit': 50}),
            ('list_jobs', {'kind': 'index_restore'}),
            ('index_advice', {}),
            ('index_advice', {'section': 'removed'}),
            ('system_status', {}),
            ('backup_status', {}),
        ]
        for name, arguments in cases:
            reply = await w.tool(ADMIN, name, **arguments)
            assert reply['ok'] and PLANT not in json.dumps(reply), (name, arguments)
        removed = (await w.tool(ADMIN, 'index_advice', section='removed'))['result']['removed']
        assert [item['index_name'] for item in removed] == ['idx_gain']
        for kind, job_id in known.items():
            assert jobs[str(job_id)]['error'] == f'E: {kind} went' and jobs[str(job_id)]['error_withheld'] is None
        # A job with no controls row is still listed.
        await w.database.execute('DELETE FROM job_controls WHERE job_id = $1', known['backup'])
        listed = (await w.api(ADMIN, 'GET', '/api/admin/jobs/all', params={'kind': 'backup'})).json()
        assert [item['id'] for item in listed['jobs']] == [str(known['backup'])]
        assert listed['jobs'][0]['cancel_requested'] is False

    world(scenario)


def test_status_and_backups_read_the_route_by_named_facts(world, monkeypatch):
    async def scenario(w: core.World):
        await w.switch_on()
        await switch(w, admin_tools=True)
        status = (await w.tool(ADMIN, 'system_status'))['result']
        assert set(status) == {
            'checked_at',
            'server',
            'database',
            'database_disk',
            'cache_disks',
            'workers',
            'backups',
            'link',
        }
        assert set(status['server']) == {'version', 'restart_needed'} and status['database']['reachable'] is True
        assert status['backups'] == {'level': 'not_configured', 'latest_at': None, 'next_run_at': None}
        assert all(isinstance(folder, str) for disk in status['cache_disks'] for folder in disk['folders'])
        assert set(status['workers']) == {'server', 'compute'} and status['link'].endswith('/settings/status')
        none = (await w.tool(ADMIN, 'backup_status'))['result']
        assert none['level'] == 'not_configured' and none['note'] == admin.NOT_CONFIGURED and none['recent_runs'] == []

    world(scenario)


def test_list_jobs_stays_small_and_says_where_the_rest_start(world):
    async def scenario(w: core.World):
        await w.switch_on()
        await switch(w, admin_tools=True)
        for _ in range(50):
            await job(w, 'backup', 'failed', error={'type': 'E', 'message': 'long ' * 400})
        answer = (await w.tool(ADMIN, 'list_jobs', limit=50))['result']
        assert len(json.dumps(answer)) <= 20_000 and answer['total'] == 50
        assert answer['next_offset'] == len(answer['jobs']) < 50
        assert all(len(item['error']) <= 500 for item in answer['jobs'])
        assert set(answer) == {'jobs', 'total', 'offset', 'next_offset', 'counts', 'kinds', 'since', 'link'}
        assert core.refused(await w.tool(ADMIN, 'list_jobs', dataset=999), 'not_found') == 'Dataset not found: id 999'

    world(scenario)


def test_the_mcp_door_lists_them_for_whom_they_are_for_and_logs_the_dataset(world):
    async def scenario(w: core.World):
        await w.switch_on(power_user=True)
        await switch(w, admin_tools=True)

        def context(user_id: int) -> Any:
            return SimpleNamespace(request=SimpleNamespace(scope={mcp_door._USER: {'id': user_id}}))

        listed = await mcp_door._list_tools(context(ADMIN), None)
        assert [tool.name for tool in listed.tools][-4:] == NAMES
        assert all(tool.annotations and tool.annotations.read_only_hint for tool in listed.tools)
        assert [tool.name for tool in (await mcp_door._list_tools(context(POWER), None)).tools] == [
            tool.name for tool in assistant.tools()
        ]
        refused = await mcp_door._call_tool(context(POWER), types.CallToolRequestParams(name='list_jobs', arguments={}))
        assert refused.is_error and refused.structured_content['error']['message'] == rule.ADMINS_ONLY
        await mcp_door._call_tool(
            context(ADMIN),
            types.CallToolRequestParams(name='list_jobs', arguments={'dataset': TRIALS}),
        )
        row = await w.database.fetchrow('SELECT tool, dataset_id, ok FROM assistant_calls ORDER BY id DESC LIMIT 1')
        assert dict(row) == {'tool': 'list_jobs', 'dataset_id': TRIALS, 'ok': True}

    world(scenario)
