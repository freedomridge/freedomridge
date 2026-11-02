"""One concept-scores guard per statement, at the top level of the WHERE, PARALLEL SAFE.

Inside each concept EXISTS / NOT EXISTS the guard became a join filter: the unsafe function
forbade parallel plans, and a safe one could still hash every sample for an anti join. One
PARALLEL SAFE guard ANDed in front of the whole filter preserves the parallel plan as a
One-Time Filter."""

import json
import os
import re
from typing import Any

import asyncpg
import pytest

from backend.routers import datasets, embeddings
from backend.tests import test_field_registry_postgres

registry_database = test_field_registry_postgres.registry_database
needs_postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')


def select(concept_id):
    return {'field': '_concept', 'operator': 'concept_select', 'value': {'concept_id': concept_id, 'threshold': 0.5}}


def exclude(concept_id):
    return {'field': '_concept', 'operator': 'concept_exclude', 'value': {'concept_id': concept_id, 'threshold': 0.5}}


def cross_grain(base, operator='has_matching_object'):
    """FilterContext.withGrain's shape: the grain, then the row's own grain OR its partner."""
    return {
        'logic': 'AND',
        'filters': [
            {'field': 'media_type', 'operator': '=', 'value': 'image'},
            {
                'logic': 'OR',
                'filters': [
                    {'field': '_cgself', 'operator': 'cg_self', 'value': base},
                    {'field': '_crossgrain', 'operator': operator, 'value': base},
                ],
            },
        ],
    }


SHAPES = {
    'select': (select(4), [4]),
    'exclude': (exclude(4), [4]),
    'or': ({'logic': 'OR', 'filters': [select(4), exclude(9)]}, [4, 9]),
    'not': ({'logic': 'NOT', 'filters': [select(9)]}, [9]),
    'nested stages': ({'logic': 'AND', 'filters': [select(9), {'logic': 'AND', 'filters': [exclude(4)]}]}, [4, 9]),
    'cross-grain image': (cross_grain({'logic': 'AND', 'filters': [exclude(4), select(9)]}), [4, 9]),
    'cross-grain object': (cross_grain({'logic': 'AND', 'filters': [exclude(4)]}, 'parent_matches'), [4]),
}
GUARD = re.compile(r'\(\(SELECT require_concept_scores\(\$(\d+)::int\[\]\)\) AND ')


def assert_one_top_level_guard(sql: str, args: list, concept_ids: list) -> None:
    assert sql.count('require_concept_scores') == 1, sql
    # The guard is the first conjunct of the filter's own top-level AND: in no EXISTS, ARRAY() or OR.
    found = GUARD.match(sql)
    assert found, sql
    assert args[int(found.group(1)) - 1] == concept_ids


@pytest.mark.parametrize('shape', sorted(SHAPES))
def test_the_compiled_filter_holds_one_guard_at_its_top_level(shape):
    node, concept_ids = SHAPES[shape]
    args: list[Any] = [1]
    sql, next_idx = datasets.parse_filter_node(node, args, 2, None, None, 1)
    assert next_idx == len(args) + 1
    assert_one_top_level_guard(sql, args, concept_ids)


def test_a_filter_without_concepts_gets_no_guard():
    args: list[Any] = [1]
    sql, _ = datasets.parse_filter_node({'field': 'media_type', 'operator': '=', 'value': 'image'}, args, 2)
    assert 'require_concept_scores' not in sql and args == [1, 'image']


def test_query_string_filters_hold_one_guard():
    """GET list_samples, the histogram and /aggregate compile through build_sample_query_parts."""
    node, concept_ids = SHAPES['or']
    where, args, _ = datasets.build_sample_query_parts(1, filters=json.dumps(node))
    assert where.startswith(' WHERE dataset_id = $1 AND ')
    assert_one_top_level_guard(where.removeprefix(' WHERE dataset_id = $1 AND '), args, concept_ids)


def test_the_scatter_rejection_sources_share_one_guard():
    args: list[Any] = [1, 'clip']
    rejected, _, probed = embeddings._scatter_rejections({'logic': 'AND', 'filters': [exclude(4), exclude(9)]}, args)
    assert rejected.count('require_concept_scores') == 1, rejected
    guard = re.search(r'\) _rejected WHERE \(SELECT require_concept_scores\(\$(\d+)::int\[\]\)\)$', rejected)
    assert guard, rejected
    assert args[int(guard.group(1)) - 1] == [4, 9]
    assert not any('require_concept_scores' in shape for shape in probed)


async def seed(database):
    """Images 1-3 (one object under image 1), concept 4 scores image 1 high, concept 9 image 2."""
    await database.execute("""
        INSERT INTO samples (id,dataset_id,filepath,media_type,metadata) VALUES
            (1,1,'a','image','{}'),(2,1,'b','image','{}'),(3,1,'c','image','{}'),
            (4,1,'d','object','{"parent_id":1}');
        INSERT INTO embedding_concepts (id,dataset_id,name,embedding_field,kind,weight) VALUES
            (4,1,'sky','clip','seed','[1,0]'),(9,1,'road','clip','seed','[0,1]');
        INSERT INTO embedding_concept_scores (concept_id,sample_id,score) VALUES
            (4,1,0.9),(4,2,0.1),(4,3,0.1),(9,1,0.1),(9,2,0.9),(9,3,0.1);
    """)


@needs_postgres
def test_the_guard_is_parallel_safe_and_takes_every_concept_at_once(registry_database):
    async def scenario(database):
        signature = (
            "SELECT proparallel::text AS parallel, pg_get_function_arguments(oid) AS arguments FROM pg_proc "
            "WHERE proname = 'require_concept_scores' AND pronamespace = to_regnamespace(current_schema())"
        )
        expected = [{'parallel': 's', 'arguments': 'target_concepts integer[]'}]
        assert [dict(row) for row in await database.fetch(signature)] == expected
        await seed(database)
        assert await database.fetchval('SELECT require_concept_scores($1::int[])', [4, 9])
        await database.execute('UPDATE embedding_concepts SET scores_ready = FALSE WHERE id = 9')
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='Concept "road" is being rescored'):
            await database.fetchval('SELECT require_concept_scores($1::int[])', [4, 9])

    registry_database(scenario)


@needs_postgres
def test_one_guard_call_per_statement_and_the_parallel_plan_is_kept(registry_database):
    async def scenario(database):
        await seed(database)
        async with database.pool.acquire() as connection, connection.transaction():
            args: list[Any] = [1]
            either = {'logic': 'OR', 'filters': [select(4), exclude(9)]}
            sql, _ = datasets.parse_filter_node(either, args, 2, None, None, 1)
            query = f'SELECT count(*) FROM samples WHERE dataset_id = $1 AND {sql}'
            await connection.execute("SET LOCAL track_functions = 'pl'")
            assert await connection.fetchval(query, *args) == 3  # images 1 and 3, object 4
            calls = await connection.fetchval(
                "SELECT calls FROM pg_stat_xact_user_functions WHERE funcname = 'require_concept_scores'",
            )
            assert calls == 1
            plan = '\n'.join(row[0] for row in await connection.fetch(f'EXPLAIN {query}', *args))
            assert 'One-Time Filter' in plan and 'InitPlan' in plan, plan
            # Free workers make a parallel plan the cheapest: it appears unless something is parallel unsafe.
            for setting in ('parallel_setup_cost', 'parallel_tuple_cost', 'min_parallel_table_scan_size'):
                await connection.execute(f'SET LOCAL {setting} = 0')
            for node in (
                exclude(9),
                select(4),
            ):  # representative large-dataset shapes (an OR of EXISTS is never parallel)
                args = [1]
                sql, _ = datasets.parse_filter_node(node, args, 2, None, None, 1)
                query = f'SELECT count(*) FROM samples WHERE dataset_id = $1 AND {sql}'
                plan = '\n'.join(row[0] for row in await connection.fetch(f'EXPLAIN {query}', *args))
                assert 'Gather' in plan and 'One-Time Filter' in plan, plan

    registry_database(scenario)


@needs_postgres
def test_the_guard_refuses_before_any_row_is_read(registry_database):
    """A One-Time Filter runs even when no row matches: an empty dataset refuses like a full one."""

    async def scenario(database):
        await seed(database)
        await database.execute('UPDATE embedding_concepts SET scores_ready = FALSE WHERE id = 4')
        for dataset_id in (1, 2):
            args: list[Any] = [dataset_id]
            sql, _ = datasets.parse_filter_node(exclude(4), args, 2, None, None, dataset_id)
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='is being rescored'):
                await database.fetchval(f'SELECT count(*) FROM samples WHERE dataset_id = $1 AND {sql}', *args)

    registry_database(scenario)


@needs_postgres
def test_the_broad_scatter_refuses_a_concept_being_rescored(registry_database):
    """The rank-probing scatter path compiles its own concept sources; its one guard still refuses."""
    from backend.tests.test_broad_scatter import CONCEPT, populate

    async def scenario(database):
        await populate(database, 40)
        request = embeddings.EmbeddingsRequest(filters=CONCEPT, sample_pct=100)
        assert await embeddings._query_broad_exclusions(1, request) is not None  # the path is taken
        await database.execute('UPDATE embedding_concepts SET scores_ready = FALSE WHERE id = 1')
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='Concept "sky" is being rescored'):
            await embeddings._query_broad_exclusions(1, request)

    registry_database(scenario)


# --- Compiler collects the ids it reads; no refusal is lost ---------

MIXED_CASE = ('Concept_Select', 'CONCEPT_EXCLUDE', 'Concept_Present')


def spelled(operator, concept_id=4):
    """A concept leaf with the operator as an API or SDK user may spell it (the compiler lower-cases it)."""
    return {'field': '_concept', 'operator': operator, 'value': {'concept_id': concept_id, 'threshold': 0.5}}


@pytest.mark.parametrize('operator', MIXED_CASE)
def test_a_mixed_case_concept_operator_is_guarded(operator):
    for node in (spelled(operator), cross_grain({'logic': 'AND', 'filters': [spelled(operator)]})):
        args: list[Any] = [1]
        sql, _ = datasets.parse_filter_node(node, args, 2, None, None, 1)
        assert 'embedding_concept_scores' in sql
        assert_one_top_level_guard(sql, args, [4])
    assert datasets._filter_concept_ids(spelled(operator)) == {4}  # the sidebar's up-front check


def test_the_scatter_rejections_guard_a_mixed_case_exclude():
    args: list[Any] = [1, 'clip']
    rejected, _, _ = embeddings._scatter_rejections(spelled('CONCEPT_EXCLUDE'), args)
    assert rejected.count('require_concept_scores') == 1 and args[-1] == [4]


def test_the_compile_path_collects_concepts_without_a_second_walk(monkeypatch):
    """The guard covers what the compiler read; no second walk over every value list (12 ms per 100k ids)."""

    def walked(node):
        raise AssertionError('parse_filter_node walked the filter tree a second time')

    monkeypatch.setattr(datasets, '_filter_concept_ids', walked)
    args: list[Any] = [1]
    many = {'field': 'id', 'operator': 'in', 'value': list(range(100_000))}
    sql, _ = datasets.parse_filter_node({'logic': 'AND', 'filters': [many, exclude(4)]}, args, 2, None, None, 1)
    assert_one_top_level_guard(sql, args, [4])


async def refusal(call):
    """The app answers ObjectNotInPrerequisiteStateError (SQLSTATE 55000) with 409."""
    try:
        await call
    except asyncpg.ObjectNotInPrerequisiteStateError as error:
        return 409, str(error)
    return 200, ''


@needs_postgres
@pytest.mark.parametrize('operator', MIXED_CASE)
def test_a_mixed_case_concept_filter_is_refused_on_every_read_path(registry_database, operator):
    async def scenario(database):
        await seed(database)
        await database.execute("""
            INSERT INTO embeddings(sample_id,field_name,vector) SELECT id,'clip','[1,0]' FROM samples;
            INSERT INTO embedding_coordinates(sample_id,field_name,x,y) SELECT id,'clip',id,id FROM samples;
        """)
        await database.execute('UPDATE embedding_concepts SET scores_ready = FALSE WHERE id = 4')
        node = spelled(operator)
        calls = {
            'grid count': datasets.list_samples_post(1, datasets.SamplesRequest(filters=node, limit=0)),
            'grid page': datasets.list_samples_post(1, datasets.SamplesRequest(filters=node, skip_count=True)),
            'field stats': datasets._get_dataset_field_stats_impl(1, node),
            'field stats exact': datasets._get_dataset_field_stats_impl(1, node, lightning_mode=False),
            'scatter': embeddings.get_embeddings_post(1, embeddings.EmbeddingsRequest(filters=node)),
        }
        outcomes = {name: await refusal(call) for name, call in calls.items()}
        wrong = {name: result for name, result in outcomes.items() if result[0] != 409 or 'rescored' not in result[1]}
        assert not wrong, wrong

    registry_database(scenario)


@needs_postgres
@pytest.mark.parametrize('labels', ['detections', 'segmentation'])
def test_the_sidebar_label_lists_pass_a_refusal_on(registry_database, monkeypatch, labels):
    """A concept that starts rescoring after the up-front check refuses the filtered label query: a 409,
    not an unfiltered label list."""
    from backend.tests.test_field_registry_postgres import ingest

    async def scenario(database):
        await ingest([{'detections': [{'label': 'person'}], 'segmentation': {'labels': ['road']}}] * 3)
        await database.execute("""
            INSERT INTO embedding_concepts (id,dataset_id,name,embedding_field,kind,weight)
                VALUES (4,1,'sky','clip','seed','[1,0]');
            INSERT INTO embedding_concept_scores (concept_id,sample_id,score) SELECT 4,id,0.9 FROM samples;
        """)
        marker = "->>'label'" if labels == 'detections' else "'segmentation'->'labels'"
        attempted = []

        class RescoredMidRequest:
            def __getattr__(self, name):
                return getattr(database, name)

            async def fetch(self, query, *args):
                if 'require_concept_scores' in query and marker in query and 'jsonb_array_elements' in query:
                    attempted.append(query)
                    raise asyncpg.ObjectNotInPrerequisiteStateError(
                        'Concept "sky" is being rescored; use it again when its scoring job finishes',
                    )
                return await database.fetch(query, *args)

        monkeypatch.setattr(datasets, 'db', RescoredMidRequest())
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='being rescored'):
            await datasets._get_dataset_field_stats_impl(1, select(4), lightning_mode=False)
        assert attempted

    registry_database(scenario)


@needs_postgres
def test_concept_tuning_reads_check_readiness_in_their_own_statement(registry_database, monkeypatch):
    from backend.routers import concepts

    async def scenario(database):
        await seed(database)
        statements = []

        class Recording:
            def __getattr__(self, name):
                method = getattr(database, name)
                if name not in ('fetch', 'fetchrow', 'fetchval'):
                    return method

                async def recorded(query, *args):
                    statements.append(query)
                    return await method(query, *args)

                return recorded

        monkeypatch.setattr(concepts, 'db', Recording())
        calls = [
            lambda: concepts.preview_concept(1, 4),
            lambda: concepts.concept_boundary(1, 4),
            lambda: concepts.concept_step(1, 4, 0.5, 'up'),
            lambda: concepts.concept_step(1, 4, 0.5, 'down'),
            lambda: concepts.concept_step(1, 4, 2.0, 'up'),  # nothing above: the max() read
            lambda: concepts.concept_step(1, 4, -1.0, 'down'),  # nothing below: the min() read
        ]
        for call in calls:
            await call()
        reads = {query for query in statements if 'embedding_concept_scores' in query}
        assert len(reads) == 7, reads
        assert all('require_concept_scores' in query for query in reads), reads
        await database.execute('UPDATE embedding_concepts SET scores_ready = FALSE WHERE id = 4')
        for call in calls:
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='Concept "sky" is being rescored'):
                await call()

    registry_database(scenario)
