"""On-demand compute workers (compute_launcher.py): a compute job that has waited with no worker free gets a spot
machine (on-demand when AWS has no spot capacity), within the time limits; every machine ends when AWS ends it, when
it's over its time, or when its worker never came or went quiet, and its token goes with it. EC2 is a fake here."""

import os

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from backend import compute_launcher, job_queue
from backend.routers import compute_launcher as launcher_router
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.version import VERSION

registry_database = registry_helpers.registry_database
USER = registry_helpers.USER
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


class FakeEC2:
    def __init__(self, spot_capacity=True, refuse=None):
        self.spot_capacity, self.refuse = spot_capacity, refuse
        self.requests, self.terminated, self.instances = [], [], {}

    def run_instances(self, **request):
        self.requests.append(request)
        if request.get('DryRun'):
            raise ClientError({'Error': {'Code': 'DryRunOperation', 'Message': 'would have succeeded'}}, 'RunInstances')
        if self.refuse:
            raise ClientError({'Error': {'Code': self.refuse, 'Message': 'not allowed'}}, 'RunInstances')
        if 'InstanceMarketOptions' in request and not self.spot_capacity:
            raise ClientError({'Error': {'Code': 'InsufficientInstanceCapacity', 'Message': 'none'}}, 'RunInstances')
        instance_id = f'i-{len(self.instances) + 1:04d}'
        tags = {tag['Key']: tag['Value'] for spec in request['TagSpecifications'] for tag in spec['Tags']}
        self.instances[instance_id] = {'InstanceId': instance_id, 'State': {'Name': 'running'}, 'Tags': tags}
        return {'Instances': [{'InstanceId': instance_id}]}

    def describe_instances(self, InstanceIds=None, Filters=None):
        if InstanceIds is not None:
            if any(i not in self.instances for i in InstanceIds):  # as AWS: one unknown id fails the request
                raise ClientError({'Error': {'Code': 'InvalidInstanceID.NotFound', 'Message': 'gone'}}, 'Describe')
            found = [self.instances[i] for i in InstanceIds]
        else:
            wanted = {f['Name']: f['Values'] for f in Filters}
            found = [
                instance
                for instance in self.instances.values()
                if instance.get('Tags', {}).get('fridge:launch') in wanted['tag:fridge:launch']
                and instance['State']['Name'] in wanted['instance-state-name']
            ]
        return {'Reservations': [{'Instances': found}]}

    def terminate_instances(self, InstanceIds):
        self.terminated += InstanceIds
        for instance_id in InstanceIds:
            self.instances[instance_id]['State'] = {'Name': 'shutting-down'}


async def configure(database, **settings):
    values = {
        'enabled': True,
        'region': 'us-east-1',
        'server_url': 'https://fr.example.com/api',
        'updated_by': USER['id'],
        **settings,
    }
    assignments = ', '.join(f'{key} = ${index + 1}' for index, key in enumerate(values))
    await database.execute(f'UPDATE compute_launcher SET {assignments}', *values.values())


async def waiting_fit(database, minutes=10):
    """A map re-fit's fit, queued for a compute worker for `minutes`."""
    job = await job_queue.enqueue_job(database.pool, 'map_refit', dataset_id=1, placement='compute', lane='maintenance')
    await database.execute(
        'UPDATE jobs SET available_at = now() - make_interval(mins => $2), '
        'updated_at = now() - make_interval(mins => $2) WHERE id = $1',
        job['id'],
        minutes,
    )
    return job['id']


async def run(database, ec2):
    await compute_launcher.schedule(database.pool, client_factory=lambda settings: ec2)


async def launches(database):
    return [dict(row) for row in await database.fetch('SELECT * FROM compute_launches ORDER BY id')]


async def connect_worker(database, name, heartbeat_minutes_ago=0):
    await database.execute(
        """INSERT INTO workers (id, placement, name, capabilities, heartbeat_at)
           VALUES ($1, 'compute', $1, '{"kinds": ["map_refit"]}', now() - make_interval(mins => $2))
           ON CONFLICT (id) DO UPDATE SET heartbeat_at = EXCLUDED.heartbeat_at""",
        name,
        heartbeat_minutes_ago,
    )


def test_a_waiting_fit_gets_one_spot_machine_that_runs_the_worker(registry_database):
    async def scenario(database):
        ec2 = FakeEC2()
        await waiting_fit(database)
        await run(database, ec2)  # off: nothing
        assert ec2.requests == [] and await launches(database) == []

        await configure(database)
        job_id = await waiting_fit(database, minutes=2)
        await run(database, ec2)  # waited 2 of 5 minutes, and the older one waits too: one machine
        [launch] = await launches(database)
        assert (launch['state'], launch['market'], launch['instance_id']) == ('starting', 'spot', 'i-0001')
        request = ec2.requests[0]
        assert request['InstanceType'] == 'r7i.2xlarge' and request['InstanceInitiatedShutdownBehavior'] == 'terminate'
        assert request['MetadataOptions']['HttpTokens'] == 'required' and request['ImageId'].startswith('resolve:ssm:')
        assert request['InstanceMarketOptions']['MarketType'] == 'spot'
        script = request['UserData']
        assert 'https://fr.example.com/api' in script and f'ghcr.io/freedomridge/backend:{VERSION}' in script
        assert f"--name {launch['worker_name']} --exit-when-idle 600" in script and "shutdown -h now" in script
        token = await database.fetchrow('SELECT * FROM api_tokens WHERE id = $1', launch['token_id'])
        assert token['scope'] == 'worker' and token['is_active'] and token['expires_at'] is not None
        assert token['token'] not in script  # the hash stays here; the machine gets the token itself
        message = await database.fetchval("SELECT message FROM jobs WHERE state = 'queued' ORDER BY created_at LIMIT 1")
        assert message == 'Starting a compute worker (r7i.2xlarge, spot)'

        # A machine starting covers the job: no second one. Its worker connects, and it's running.
        await run(database, ec2)
        assert len(ec2.requests) == 1
        await connect_worker(database, launch['worker_name'])
        await run(database, ec2)
        assert (await launches(database))[0]['state'] == 'running'
        assert len(ec2.requests) == 1 and job_id is not None

    registry_database(scenario)


def test_on_demand_when_aws_has_no_spot_capacity_and_a_refusal_is_recorded(registry_database):
    async def scenario(database):
        await configure(database)
        await waiting_fit(database)
        await run(database, FakeEC2(spot_capacity=False))
        [launch] = await launches(database)
        assert (launch['market'], launch['state']) == ('on-demand', 'starting')

        await database.execute("UPDATE compute_launches SET state = 'ended'")
        await run(database, FakeEC2(refuse='UnauthorizedOperation'))
        refused = (await launches(database))[-1]
        assert refused['state'] == 'ended' and 'AWS refused the launch' in refused['end_reason']
        assert not await database.fetchval('SELECT is_active FROM api_tokens WHERE id = $1', refused['token_id'])
        assert 'UnauthorizedOperation' in await database.fetchval('SELECT last_error FROM compute_launcher')

    registry_database(scenario)


def test_the_time_limits_hold_a_launch_and_the_job_says_why(registry_database):
    async def scenario(database):
        ec2 = FakeEC2()
        await configure(database, max_machines=1)
        await waiting_fit(database)
        # A free connected worker takes it: no machine.
        await connect_worker(database, 'desk')
        await run(database, ec2)
        assert ec2.requests == []
        await database.execute("UPDATE workers SET heartbeat_at = now() - interval '5 minutes'")

        # One machine is the most at once.
        await database.execute(
            "INSERT INTO compute_launches (region, instance_type, market, worker_name, state, instance_id) "
            "VALUES ('us-east-1', 'r7i.2xlarge', 'spot', 'on-demand-x', 'running', 'i-busy')",
        )
        ec2.instances['i-busy'] = {'InstanceId': 'i-busy', 'State': {'Name': 'running'}}
        await connect_worker(database, 'on-demand-x')
        await database.execute(
            "UPDATE workers SET current_job_id = NULL, heartbeat_at = now() WHERE name = 'on-demand-x'",
        )
        await database.execute(
            "UPDATE workers SET current_job_id = (SELECT id FROM jobs LIMIT 1) WHERE name = 'on-demand-x'",
        )
        await run(database, ec2)
        assert ec2.requests == []
        assert 'the most at once' in await database.fetchval("SELECT message FROM jobs WHERE state = 'queued'")

        # The month's machine-hours are used.
        await database.execute("UPDATE compute_launches SET state = 'ended', ended_at = now()")
        await database.execute(
            "INSERT INTO compute_launches (region, instance_type, market, worker_name, state, launched_at, ended_at) "
            "VALUES ('us-east-1', 'r7i.2xlarge', 'spot', 'on-demand-y', 'ended', "
            "GREATEST(date_trunc('month', now()), now() - interval '41 hours'), now())",
        )
        await configure(database, monthly_hours=0.01)
        await run(database, ec2)
        assert ec2.requests == []
        assert "machine-hours are used" in await database.fetchval("SELECT message FROM jobs WHERE state = 'queued'")

    registry_database(scenario)


def test_machines_end_when_aws_ends_them_when_over_time_or_when_their_worker_never_came(registry_database):
    async def scenario(database):
        ec2 = FakeEC2()
        await configure(database, max_hours_per_machine=6, max_machines=10)
        for _ in range(5):
            await waiting_fit(database)
            await database.execute("UPDATE compute_launches SET state = 'running'")  # don't hold the next launch
            await run(database, ec2)
        rows = await launches(database)
        assert len(rows) == 5
        await database.execute("UPDATE compute_launches SET state = 'running'")
        spot, old, never, silent, broken = rows
        # AWS took the first one back; the second has run 7 hours; the third's worker never came in 20 minutes;
        # the fourth's worker went quiet.
        ec2.instances[spot['instance_id']]['State'] = {'Name': 'terminated'}
        ec2.instances[spot['instance_id']]['StateReason'] = {'Code': 'Server.SpotInstanceTermination'}
        # The fifth powered itself off before its worker ever connected (its image didn't start).
        ec2.instances[broken['instance_id']]['State'] = {'Name': 'terminated'}
        ec2.instances[broken['instance_id']]['StateReason'] = {'Code': 'Client.InstanceInitiatedShutdown'}
        await database.execute(
            "UPDATE compute_launches SET launched_at = now() - interval '7 hours' WHERE id = $1",
            old['id'],
        )
        await connect_worker(database, old['worker_name'])
        await database.execute(
            "UPDATE compute_launches SET launched_at = now() - interval '20 minutes' WHERE id = $1",
            never['id'],
        )
        await connect_worker(database, silent['worker_name'], heartbeat_minutes_ago=15)
        await configure(database, enabled=False)  # turned off, it still ends machines, and starts none
        await run(database, ec2)
        ended = {row['id']: row for row in await launches(database)}
        assert 'AWS took the spot machine back' in ended[spot['id']]['end_reason']
        assert ended[old['id']]['end_reason'] == 'reached the 6-hour limit per machine'
        assert 'never connected' in ended[never['id']]['end_reason']
        assert 'went silent' in ended[silent['id']]['end_reason']
        assert 'powered off before its worker connected' in ended[broken['id']]['end_reason']
        assert all(row['state'] == 'ended' for row in ended.values())
        assert sorted(ec2.terminated) == sorted([old['instance_id'], never['instance_id'], silent['instance_id']])
        tokens = await database.fetch(
            'SELECT is_active FROM api_tokens WHERE id = ANY($1::int[])',
            [row['token_id'] for row in rows],
        )
        assert [token['is_active'] for token in tokens] == [False] * 5

    registry_database(scenario)


def test_admins_set_it_up_with_the_secret_kept_and_check_it_with_a_dry_run(monkeypatch, registry_database):
    async def scenario(database):
        monkeypatch.setattr(launcher_router, 'db', database)
        with pytest.raises(HTTPException) as unset:
            await launcher_router.set_launcher(launcher_router.LauncherSettings(enabled=True), user=USER)
        assert unset.value.status_code == 400 and 'region' in unset.value.detail
        settings = launcher_router.LauncherSettings(
            enabled=True,
            region='eu-west-1',
            server_url='https://fr.example.com/api/',
            access_key_id='AKIAEXAMPLEEXAMPLE12',
            secret_access_key='a-secret',
            security_group_ids=['sg-0123abcd'],
        )
        saved = await launcher_router.set_launcher(settings, user=USER)
        assert saved['has_secret'] and 'secret_access_key_encrypted' not in saved
        assert saved['server_url'] == 'https://fr.example.com/api'
        stored = await database.fetchval('SELECT secret_access_key_encrypted FROM compute_launcher')
        assert stored and stored != 'a-secret'
        # Saving again without a secret keeps it.
        kept = await launcher_router.set_launcher(settings.model_copy(update={'secret_access_key': None}), user=USER)
        assert kept['has_secret']

        ec2 = FakeEC2()
        checked = await compute_launcher.check(await database.fetchrow('SELECT * FROM compute_launcher'), lambda s: ec2)
        assert checked.startswith('AWS would start') and ec2.requests[0]['DryRun'] is True
        assert ec2.requests[0]['SecurityGroupIds'] == ['sg-0123abcd']
        listed = await launcher_router.get_launcher(user=USER)
        assert listed['settings']['region'] == 'eu-west-1' and listed['launches'] == []

    registry_database(scenario)


def test_one_machine_aws_no_longer_knows_ends_only_its_own_record(registry_database):
    async def scenario(database):
        ec2 = FakeEC2()
        await configure(database, max_machines=5)
        for _ in range(2):
            await waiting_fit(database)
            await run(database, ec2)
            await database.execute("UPDATE compute_launches SET state = 'running'")
        first, second = await launches(database)
        await connect_worker(database, second['worker_name'])
        del ec2.instances[first['instance_id']]  # aged out of AWS while the server was down
        await configure(database, enabled=False)
        await run(database, ec2)
        first, second = await launches(database)
        assert first['state'] == 'ended' and second['state'] == 'running'
        assert await database.fetchval('SELECT is_active FROM api_tokens WHERE id = $1', second['token_id'])

    registry_database(scenario)


def test_one_job_runner_at_a_time_and_an_unrecorded_launch_is_found_by_its_tag(registry_database):
    async def scenario(database):
        ec2 = FakeEC2()
        await configure(database)
        await waiting_fit(database)
        async with database.pool.acquire() as other:
            await other.execute('SELECT pg_advisory_lock($1)', compute_launcher.LOCK)
            await run(database, ec2)  # another runner has this minute
            assert ec2.requests == []
            await other.execute('SELECT pg_advisory_unlock($1)', compute_launcher.LOCK)

        # A runner stopped after AWS started the machine but before it recorded it: found by its tag, adopted.
        identity = await database.fetchval('SELECT system_identifier::text FROM pg_control_system()')
        launch_id = await database.fetchval(
            "INSERT INTO compute_launches (region, instance_type, market, worker_name) "
            "VALUES ('us-east-1', 'r7i.2xlarge', 'spot', 'on-demand-lost') RETURNING id",
        )
        ec2.instances['i-lost'] = {
            'InstanceId': 'i-lost',
            'State': {'Name': 'running'},
            'Tags': {'fridge:launch': compute_launcher.launch_tag(identity, launch_id)},
        }
        await run(database, ec2)
        adopted = next(row for row in await launches(database) if row['id'] == launch_id)
        assert (adopted['instance_id'], adopted['state']) == ('i-lost', 'starting')
        # Starting, it covers the waiting job. Once it's gone, a new machine starts, its tag naming this database and
        # its launch.
        assert len(ec2.requests) == 0
        await database.execute("UPDATE compute_launches SET state = 'ended' WHERE id = $1", launch_id)
        await run(database, ec2)
        launched = next(row for row in await launches(database) if row['id'] != launch_id)
        assert ec2.instances[launched['instance_id']]['Tags']['fridge:launch'] == f"{identity}-{launched['id']}"

    registry_database(scenario)
