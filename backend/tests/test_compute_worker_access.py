"""A compute worker's lease doesn't outlive its owner's access to the job's dataset.

The lease was the only check after the claim: when an admin removed a power user's access to a private dataset,
their worker could still download the job's inputs, upload a result and renew the lease until the job ended.
"""

import os

import pytest

from backend.job_queue import JobPolicy
from backend.map_artifacts import LocalArtifactStore
from backend.routers import workers
from backend.tests import test_field_registry_postgres
from backend.tests.test_compute_worker_leases import KIND, client, enqueue, register, setup

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL database',
)


def test_removing_access_ends_what_the_worker_can_do_with_the_job(registry_database, monkeypatch, tmp_path):
    async def scenario(database):
        headers = await setup(database, monkeypatch)  # dataset 2 is private to user 12
        monkeypatch.setenv('FRIDGE_UMAP_MODEL_DIR', str(tmp_path))
        remote = workers.RemoteKind(
            policy=JobPolicy(max_failures=2, retry_seconds=1),
            inputs=('in.bin',),
            outputs=('out.bin',),
            output_limit=lambda job, name: 100,
        )
        monkeypatch.setattr(workers, 'REMOTE_KINDS', {KIND: remote})
        await database.execute('INSERT INTO dataset_access (dataset_id, user_id) VALUES (2, 10)')  # granted
        async with client() as api:
            worker = await register(api, headers[10])
            queued = await enqueue(database, dataset_id=2)
            claimed = await api.post(f'/api/workers/{worker}/claim', json={}, headers=headers[10])
            assert claimed.status_code == 200, claimed.text
            job = claimed.json()
            assert job['id'] == str(queued['id'])
            lease = job['lease_token']
            work = LocalArtifactStore().work_dir(job['id'])
            work.mkdir(parents=True)
            (work / 'in.bin').write_bytes(b'vectors')

            files = f'/api/workers/{worker}/jobs/{job["id"]}/files'
            transfer = {**headers[10], 'X-Fridge-Lease': lease}

            async def calls():
                return {
                    'download': await api.get(f'{files}/in.bin', headers=transfer),
                    'upload': await api.put(f'{files}/out.bin', content=b'map', headers=transfer),
                    'heartbeat': await api.post(
                        f'/api/workers/{worker}/jobs/{job["id"]}/heartbeat',
                        json={'lease_token': lease},
                        headers=headers[10],
                    ),
                }

            before = await calls()
            assert {name: response.status_code for name, response in before.items()} == {
                'download': 200,
                'upload': 200,
                'heartbeat': 200,
            }
            assert before['download'].content == b'vectors'

            # An admin removes the owner's access (DELETE /datasets/2/access/10).
            await database.execute('DELETE FROM dataset_access WHERE dataset_id = 2 AND user_id = 10')
            after = await calls()
            complete = await api.post(
                f'/api/workers/{worker}/jobs/{job["id"]}/complete',
                json={'lease_token': lease, 'result': {'points': 1}},
                headers=headers[10],
            )
            for response in [*after.values(), complete]:
                assert response.status_code == 409, response.text
                assert response.json()['detail'] == workers.ACCESS_LOST  # code lease_lost: drop this job, go on
            assert await database.fetchval('SELECT state FROM jobs WHERE id = $1', queued['id']) == 'running'

            # Giving the job back stays open: it goes back to the queue for a worker that may run it.
            released = await api.post(
                f'/api/workers/{worker}/jobs/{job["id"]}/release',
                json={'lease_token': lease},
                headers=headers[10],
            )
            assert released.json() == {'state': 'queued'}

    registry_database(scenario)
