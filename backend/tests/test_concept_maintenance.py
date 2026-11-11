"""Incremental concept work contracts, exclusively on a disposable PostgreSQL database."""

import json
import os

import pytest

from backend.tests import test_field_registry_postgres as registry_helpers

registry_database = registry_helpers.registry_database

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def sample(database, path, vector='[1,0]', dataset_id=1):
    sample_id = await database.fetchval(
        "INSERT INTO samples(dataset_id,filepath,media_type) VALUES($1,$2,'image') RETURNING id",
        dataset_id,
        path,
    )
    await database.execute(
        "INSERT INTO embeddings(dataset_id,sample_id,field_name,vector) VALUES("
        "(SELECT dataset_id FROM samples WHERE id = $1),$1,'clip',$2::text::vector)",
        sample_id,
        vector,
    )
    return sample_id


async def concept(database, *, dataset_id=1, name='car', kind='seed', weight='[1,0]', bias=0):
    return await database.fetchval(
        """INSERT INTO embedding_concepts(dataset_id,name,embedding_field,kind,weight,bias)
           VALUES($1,$2,'clip',$3,$4,$5) RETURNING id""",
        dataset_id,
        name,
        kind,
        weight,
        bias,
    )


async def score(database, concept_id, *, full=False):
    return await database.fetchval('SELECT score_concept($1,$2)', concept_id, full)


async def scores(database, concept_id):
    return {
        row['sample_id']: row['score']
        for row in await database.fetch(
            'SELECT sample_id,score FROM embedding_concept_scores WHERE concept_id=$1 ORDER BY sample_id',
            concept_id,
        )
    }


def test_incremental_scoring_does_not_lose_a_lower_id_that_commits_late(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        await score(database, concept_id, full=True)
        async with database.pool.acquire() as writer:
            async with writer.transaction():
                lower = await sample(writer, 'uncommitted')
                higher = await sample(database, 'committed')
                assert lower < higher
                assert await score(database, concept_id) == 1
        assert await score(database, concept_id) == 1
        assert set(await scores(database, concept_id)) == {lower, higher}

    registry_database(scenario)


def test_vector_written_before_concept_registration_is_scored_after_its_commit(registry_database):
    async def scenario(database):
        async with database.pool.acquire() as writer, writer.transaction():
            lower = await sample(writer, 'before-concept')
            concept_id = await concept(database)
            higher = await sample(database, 'after-concept')
            await score(database, concept_id, full=True)
            assert set(await scores(database, concept_id)) == {higher}
        await score(database, concept_id)
        assert set(await scores(database, concept_id)) == {lower, higher}

    registry_database(scenario)


def test_incremental_scoring_updates_replaced_vectors_and_removes_missing_vectors(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        sample_id = await sample(database, 'first')
        assert await score(database, concept_id, full=True) == 1
        await database.execute("UPDATE embeddings SET vector='[0,1]' WHERE sample_id=$1", sample_id)
        assert await score(database, concept_id) == 1
        assert (await scores(database, concept_id))[sample_id] == pytest.approx(0.5)
        await database.execute('DELETE FROM embeddings WHERE sample_id=$1', sample_id)
        await score(database, concept_id)
        assert await scores(database, concept_id) == {}
        assert await database.fetchval('SELECT scored_count FROM embedding_concepts WHERE id=$1', concept_id) == 0

    registry_database(scenario)


def test_incremental_counts_follow_sample_cascades_without_rescoring_other_samples(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        first = await sample(database, 'first')
        second = await sample(database, 'second')
        await score(database, concept_id, full=True)
        original = await database.fetchval(
            'SELECT ctid::text FROM embedding_concept_scores WHERE concept_id=$1 AND sample_id=$2',
            concept_id,
            second,
        )
        await database.execute('DELETE FROM samples WHERE id=$1', first)
        assert await score(database, concept_id) == 0
        assert await database.fetchval('SELECT scored_count FROM embedding_concepts WHERE id=$1', concept_id) == 1
        assert (
            await database.fetchval(
                'SELECT ctid::text FROM embedding_concept_scores WHERE concept_id=$1 AND sample_id=$2',
                concept_id,
                second,
            )
            == original
        )

    registry_database(scenario)


def test_incremental_scoring_does_not_read_unchanged_score_history(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        async with registry_helpers.bulk_setup(database) as connection:
            await connection.execute(
                """INSERT INTO samples(dataset_id,filepath,media_type)
                   SELECT 1,number::text,'image' FROM generate_series(1,100000) number""",
            )
            await connection.execute(
                """INSERT INTO embeddings(dataset_id,sample_id,field_name,vector)
                   SELECT dataset_id,id,'clip','[1,0]'::vector FROM samples WHERE dataset_id=1""",
            )
        await score(database, concept_id, full=True)
        await database.execute('ANALYZE embeddings; ANALYZE embedding_concept_scores; ANALYZE samples')
        for table in ('concept_embedding_pending', 'concept_score_pending', 'concept_score_count_deltas'):
            if await database.fetchval('SELECT to_regclass(current_schema()||$1)', '.' + table):
                await database.execute(f'VACUUM ANALYZE {table}')
        touched = []
        for repeat in range(3):
            await sample(database, f'new-{repeat}', '[0,1]')
            plan = await database.fetchval(
                'EXPLAIN(ANALYZE,BUFFERS,FORMAT JSON) SELECT score_concept($1,FALSE)',
                concept_id,
            )
            if isinstance(plan, str):
                plan = json.loads(plan)
            root = plan[0]['Plan']
            touched.append(root.get('Shared Hit Blocks', 0) + root.get('Shared Read Blocks', 0))
        assert max(touched[1:]) < 15000, touched  # One 1024-ID block, independent of the 100k history.
        assert await database.fetchval('SELECT scored_count FROM embedding_concepts WHERE id=$1', concept_id) == 100003

    registry_database(scenario)
