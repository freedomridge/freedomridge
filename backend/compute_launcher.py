"""On-demand compute workers (docs/ON_DEMAND_WORKERS.md): when a compute job has waited with no worker free to take
it, start a cloud machine that runs the worker, and end every machine that's done, interrupted or over its time.

AWS EC2 for now. A machine is a spot instance of the configured type (on-demand only when AWS has no spot capacity
for it), booted from AWS's own Amazon Linux image (an SSM parameter, so there's no image to keep up to date), with a
startup script that runs ``python -m fridge.worker --exit-when-idle`` from the server's own backend image and powers
off when it exits. Launched with shutdown behaviour "terminate", powering off deletes it. Its worker token is made
for that machine, expires after the machine's time limit, and is revoked when the machine ends. The machine gets no
AWS permissions: everything it needs comes from this server over the API.

``schedule`` runs in the job runner once a minute: first it ends machines, then it starts at most one.
"""

import asyncio
import logging
import secrets
import shlex
from datetime import datetime, timedelta, timezone

from .crypto import decrypt_secret, hash_token
from .version import VERSION

logger = logging.getLogger(__name__)

SCHEDULE_SECONDS = 60
AMI = 'resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64'
ROOT_VOLUME_GB = 40  # the backend image and a fit's vectors
BOOT_MINUTES = 15  # a machine whose worker hasn't connected by then is ended
SILENT_MINUTES = 10  # a connected worker silent for this long: its machine is ended
WORKER_KINDS = ('map_refit',)  # what python -m fridge.worker takes
LOCK = 2026110801  # one job runner's schedule at a time: two would both see room under the limits
# AWS has no spot capacity for the type right now: launch it on-demand instead.
NO_SPOT_CAPACITY = (
    'InsufficientInstanceCapacity',
    'SpotMaxPriceTooLow',
    'MaxSpotInstanceCountExceeded',
    'InsufficientCapacityOnHost',
    'UnfulfillableCapacity',
)
# AWS's reason for an instance ending, in words.
END_REASONS = {
    'Client.InstanceInitiatedShutdown': 'its worker finished and the machine powered off',
    'Client.UserInitiatedShutdown': 'stopped from AWS',
    'Server.SpotInstanceTermination': 'AWS took the spot machine back; its job went back in the queue',
    'Server.SpotInstanceShutdown': 'AWS took the spot machine back; its job went back in the queue',
    'Server.InsufficientInstanceCapacity': 'AWS had no capacity for it',
    'Server.InternalError': 'AWS ended it after an internal error',
}


def ec2_client(settings):
    """An EC2 client for the configured region, with the configured keys or else the server's own credentials."""
    import boto3
    from botocore.config import Config

    keys = {}
    if settings['access_key_id']:
        keys = {
            'aws_access_key_id': settings['access_key_id'],
            'aws_secret_access_key': decrypt_secret(settings['secret_access_key_encrypted'] or ''),
        }
    # Short timeouts: the job runner's schedules run one after another, and AWS not answering mustn't hold them.
    config = Config(connect_timeout=5, read_timeout=30, retries={'max_attempts': 2})
    return boto3.client('ec2', region_name=settings['region'], config=config, **keys)


def image_of(settings) -> str:
    return settings['image'] or f'ghcr.io/freedomridge/backend:{VERSION}'


def user_data(settings, token: str, worker_name: str) -> str:
    """The machine's startup script: run the worker until it's idle, then power off (and so terminate)."""
    quoted = {
        'url': shlex.quote(settings['server_url'].rstrip('/')),
        'token': shlex.quote(token),
        'image': shlex.quote(image_of(settings)),
        'name': shlex.quote(worker_name),
    }
    idle = int(settings['idle_minutes']) * 60
    return f"""#!/bin/bash
# A Freedom Ridge compute worker, started on demand. Whatever happens, the machine powers off at the end, and was
# launched to terminate when it does.
trap 'shutdown -h now' EXIT
dnf install -y docker
systemctl start docker
docker run --rm --name fridge-worker --stop-timeout 60 \\
  -e FRIDGE_API_URL={quoted['url']} -e FRIDGE_API_TOKEN={quoted['token']} -e FRIDGE_WORKER_DIR=/tmp/fridge-worker \\
  {quoted['image']} python -m fridge.worker --name {quoted['name']} --exit-when-idle {idle}
"""


def launch_tag(identity: str, launch_id: int) -> str:
    """The fridge:launch tag: this server's database and the launch, so servers sharing an AWS account never take
    each other's machines for their own."""
    return f'{identity}-{launch_id}'


def run_request(settings, tag: str, worker_name: str, script: str, *, spot: bool, dry_run: bool = False) -> dict:
    request = {
        'ImageId': AMI,
        'InstanceType': settings['instance_type'],
        'MinCount': 1,
        'MaxCount': 1,
        'UserData': script,
        'InstanceInitiatedShutdownBehavior': 'terminate',
        'MetadataOptions': {'HttpTokens': 'required', 'HttpEndpoint': 'enabled'},
        'BlockDeviceMappings': [
            {
                'DeviceName': '/dev/xvda',
                'Ebs': {'VolumeSize': ROOT_VOLUME_GB, 'VolumeType': 'gp3', 'DeleteOnTermination': True},
            },
        ],
        'TagSpecifications': [
            {
                'ResourceType': kind,
                'Tags': [
                    {'Key': 'Name', 'Value': worker_name},
                    {'Key': 'fridge:launch', 'Value': tag},
                ],
            }
            for kind in ('instance', 'volume')
        ],
    }
    if settings['subnet_id']:
        request['SubnetId'] = settings['subnet_id']
    if settings['security_group_ids']:
        request['SecurityGroupIds'] = list(settings['security_group_ids'])
    if spot:
        request['InstanceMarketOptions'] = {
            'MarketType': 'spot',
            'SpotOptions': {'SpotInstanceType': 'one-time', 'InstanceInterruptionBehavior': 'terminate'},
        }
    if dry_run:
        request['DryRun'] = True
    return request


def error_code(error) -> str:
    return getattr(error, 'response', {}).get('Error', {}).get('Code', '')


async def record_error(pool, message: str) -> None:
    logger.error('On-demand compute workers: %s', message)
    await pool.execute(
        'UPDATE compute_launcher SET last_error = $1, last_error_at = now()',
        message[:1000],
    )


async def end_launch(pool, launch_id: int, reason: str) -> None:
    async with pool.acquire() as connection, connection.transaction():
        token_id = await connection.fetchval(
            """UPDATE compute_launches SET state = 'ended', ended_at = COALESCE(ended_at, now()), end_reason = $2
               WHERE id = $1 AND state <> 'ended' RETURNING token_id""",
            launch_id,
            reason,
        )
        if token_id is not None:
            await connection.execute('UPDATE api_tokens SET is_active = FALSE WHERE id = $1', token_id)


async def terminate(pool, ec2, launch, reason: str) -> None:
    """End a machine: AWS deletes it, then its record ends and its token is revoked."""
    if launch['instance_id']:
        try:
            await asyncio.to_thread(ec2.terminate_instances, InstanceIds=[launch['instance_id']])
        except Exception as error:
            if error_code(error) != 'InvalidInstanceID.NotFound':
                await record_error(pool, f"Could not end {launch['instance_id']}: {error}")
                return  # tried again next minute
    await end_launch(pool, launch['id'], reason)


async def describe(ec2, **query) -> dict:
    """The instances AWS has, by id. An id AWS no longer has fails the whole request, so then each is asked alone,
    and only one whose own answer says so counts as gone."""
    try:
        answers = [await asyncio.to_thread(ec2.describe_instances, **query)]
    except Exception as error:
        if error_code(error) != 'InvalidInstanceID.NotFound' or len(query.get('InstanceIds', [])) < 2:
            if error_code(error) == 'InvalidInstanceID.NotFound':
                return {}
            raise
        answers = []
        for instance_id in query['InstanceIds']:
            try:
                answers.append(await asyncio.to_thread(ec2.describe_instances, InstanceIds=[instance_id]))
            except Exception as single:
                if error_code(single) != 'InvalidInstanceID.NotFound':
                    raise
    return {
        instance['InstanceId']: instance
        for answer in answers
        for reservation in answer.get('Reservations', [])
        for instance in reservation.get('Instances', [])
    }


async def reap(pool, ec2, settings, live: list, identity: str) -> None:
    """Every live machine: ended if AWS ended it, if it's over its time, or if its worker never came or went quiet."""
    now = datetime.now(timezone.utc)
    ids = [launch['instance_id'] for launch in live if launch['instance_id']]
    try:
        states = await describe(ec2, InstanceIds=ids) if ids else {}
    except Exception as error:
        await record_error(pool, f'Could not look at the machines: {error}')
        return
    for launch in live:
        if not launch['instance_id']:
            # The runner stopped between AWS starting it and the answer being recorded: find it by its tag.
            try:
                tagged = await describe(
                    ec2,
                    Filters=[
                        {'Name': 'tag:fridge:launch', 'Values': [launch_tag(identity, launch['id'])]},
                        {'Name': 'instance-state-name', 'Values': ['pending', 'running']},
                    ],
                )
            except Exception as error:
                await record_error(pool, f'Could not look for launch {launch["id"]}: {error}')
                continue
            if tagged:
                await pool.execute(
                    'UPDATE compute_launches SET instance_id = $2 WHERE id = $1',
                    launch['id'],
                    next(iter(tagged)),
                )
            elif now - launch['launched_at'] > timedelta(minutes=BOOT_MINUTES):
                await end_launch(pool, launch['id'], 'AWS never answered the launch')
            continue
        instance = states.get(launch['instance_id'])
        state = (instance or {}).get('State', {}).get('Name')
        worker = await pool.fetchrow(
            "SELECT heartbeat_at FROM workers WHERE placement = 'compute' AND name = $1 "
            'ORDER BY heartbeat_at DESC LIMIT 1',
            launch['worker_name'],
        )
        if instance is None or state in ('shutting-down', 'terminated', 'stopping', 'stopped'):
            code = (instance or {}).get('StateReason', {}).get('Code', '')
            reason = END_REASONS.get(code, f'the machine ended ({code or state or "gone"})')
            if code == 'Client.InstanceInitiatedShutdown' and worker is None and launch['connected_at'] is None:
                reason = (
                    "the machine powered off before its worker connected: Docker or the image didn't start "
                    "(the instance's console output in AWS says why)"
                )
            await end_launch(pool, launch['id'], reason)
            continue
        age = now - launch['launched_at']
        if age > timedelta(hours=settings['max_hours_per_machine']):
            await terminate(
                pool,
                ec2,
                launch,
                f"reached the {settings['max_hours_per_machine']:g}-hour limit per machine",
            )
        elif worker is None and age > timedelta(minutes=BOOT_MINUTES):
            await terminate(pool, ec2, launch, f'its worker never connected within {BOOT_MINUTES} minutes')
        elif worker is not None and now - worker['heartbeat_at'] > timedelta(minutes=SILENT_MINUTES):
            await terminate(pool, ec2, launch, f'its worker went silent for {SILENT_MINUTES} minutes')
        elif worker is not None and launch['state'] == 'starting':
            await pool.execute(
                "UPDATE compute_launches SET state = 'running', connected_at = now() WHERE id = $1",
                launch['id'],
            )


async def hours_this_month(source) -> float:
    return float(
        await source.fetchval(
            """SELECT COALESCE(sum(extract(epoch FROM LEAST(COALESCE(ended_at, now()), now())
                                       - GREATEST(launched_at, date_trunc('month', now()))) / 3600), 0)
               FROM compute_launches WHERE COALESCE(ended_at, now()) > date_trunc('month', now())""",
        ),
    )


async def waiting_jobs(pool, settings) -> list:
    """Compute jobs a launched worker would take that have waited at least wait_minutes."""
    return await pool.fetch(
        """SELECT id FROM jobs
           WHERE state = 'queued' AND placement = 'compute' AND kind = ANY($1::text[])
             AND GREATEST(available_at, updated_at) < now() - make_interval(mins => $2)
           ORDER BY created_at""",
        list(WORKER_KINDS),
        settings['wait_minutes'],
    )


async def launch_if_needed(pool, ec2, settings, identity: str) -> None:
    waiting = await waiting_jobs(pool, settings)
    if not waiting:
        return
    # A free worker takes the next one; a machine already starting takes one when it connects.
    free = await pool.fetchval(
        """SELECT count(*) FROM workers WHERE placement = 'compute' AND current_job_id IS NULL
             AND heartbeat_at > clock_timestamp() - interval '60 seconds'
             AND (NOT capabilities ? 'kinds' OR capabilities->'kinds' ?| $1::text[])""",
        list(WORKER_KINDS),
    )
    starting = await pool.fetchval("SELECT count(*) FROM compute_launches WHERE state = 'starting'")
    if len(waiting) <= free + starting:
        return
    live = await pool.fetchval("SELECT count(*) FROM compute_launches WHERE state <> 'ended'")
    used = await hours_this_month(pool)
    ids = [row['id'] for row in waiting]
    if live >= settings['max_machines']:
        await note_waiting(
            pool,
            ids,
            f"Waiting for a compute worker: {live} on-demand machine(s) running, the most at once",
        )
        return
    if used >= settings['monthly_hours']:
        await note_waiting(
            pool,
            ids,
            f"Waiting for a compute worker: this month's {settings['monthly_hours']:g} on-demand "
            'machine-hours are used',
        )
        return
    if not settings['server_url'] or not settings['region'] or settings['updated_by'] is None:
        await record_error(pool, 'Turned on but not set up: it needs a region, the server URL and an admin who set it')
        return
    await launch(pool, ec2, settings, ids[0], identity)


async def note_waiting(pool, job_ids: list, message: str) -> None:
    await pool.execute(
        "UPDATE jobs SET message = $2 WHERE id = ANY($1::uuid[]) AND state = 'queued' AND message IS DISTINCT FROM $2",
        job_ids,
        message,
    )


async def launch(pool, ec2, settings, job_id, identity: str) -> None:
    """Start one machine for a waiting job: its record and token first, then AWS; spot unless AWS has none."""
    raw = secrets.token_urlsafe(32)
    async with pool.acquire() as connection, connection.transaction():
        launch_id = await connection.fetchval(
            """INSERT INTO compute_launches (job_id, region, instance_type, market, worker_name)
               VALUES ($1, $2, $3, 'spot', 'pending') RETURNING id""",
            job_id,
            settings['region'],
            settings['instance_type'],
        )
        worker_name = f'on-demand-{launch_id}'
        token_id = await connection.fetchval(
            # Expires an hour after the machine's limit, by the database's clock as token checks are.
            'INSERT INTO api_tokens (user_id, token, name, expires_at, scope) '
            "VALUES ($1, $2, $3, NOW() + ($4::double precision + 1) * INTERVAL '1 hour', 'worker') RETURNING id",
            settings['updated_by'],
            hash_token(raw),
            worker_name,
            float(settings['max_hours_per_machine']),
        )
        await connection.execute(
            'UPDATE compute_launches SET worker_name = $2, token_id = $3 WHERE id = $1',
            launch_id,
            worker_name,
            token_id,
        )
    script = user_data(settings, raw, worker_name)
    market = 'spot'
    try:
        try:
            answer = await asyncio.to_thread(
                ec2.run_instances,
                **run_request(settings, launch_tag(identity, launch_id), worker_name, script, spot=True),
            )
        except Exception as error:
            if error_code(error) not in NO_SPOT_CAPACITY:
                raise
            market = 'on-demand'
            answer = await asyncio.to_thread(
                ec2.run_instances,
                **run_request(settings, launch_tag(identity, launch_id), worker_name, script, spot=False),
            )
    except Exception as error:
        await end_launch(pool, launch_id, f'AWS refused the launch: {error}')
        await record_error(pool, f'Could not start a machine: {error}')
        return
    instance_id = answer['Instances'][0]['InstanceId']
    await pool.execute(
        'UPDATE compute_launches SET instance_id = $2, market = $3 WHERE id = $1',
        launch_id,
        instance_id,
        market,
    )
    await note_waiting(pool, [job_id], f"Starting a compute worker ({settings['instance_type']}, {market})")
    await pool.execute('UPDATE compute_launcher SET last_error = NULL, last_error_at = NULL')
    logger.info('Started %s (%s, %s) for job %s', instance_id, settings['instance_type'], market, job_id)


async def schedule(pool, client_factory=ec2_client) -> None:
    """The job runner's minute: end machines, then start one if a job needs it. One runner at a time: another
    holding the lock has this minute."""
    async with pool.acquire() as connection:
        if not await connection.fetchval('SELECT pg_try_advisory_lock($1)', LOCK):
            return
        try:
            await _schedule(pool, client_factory)
        finally:
            await connection.execute('SELECT pg_advisory_unlock($1)', LOCK)


async def _schedule(pool, client_factory) -> None:
    settings = await pool.fetchrow('SELECT * FROM compute_launcher')
    live = await pool.fetch("SELECT * FROM compute_launches WHERE state <> 'ended' ORDER BY id")
    if settings is None or (not settings['enabled'] and not live):
        return
    try:
        ec2 = client_factory(settings)
    except Exception as error:
        await record_error(pool, f'No AWS client: {error}')
        return
    identity = await pool.fetchval('SELECT system_identifier::text FROM pg_control_system()')
    if live:
        await reap(pool, ec2, settings, live, identity)
    if settings['enabled']:
        await launch_if_needed(pool, ec2, settings, identity)


async def check(settings, client_factory=ec2_client) -> str:
    """A dry-run launch with these settings: AWS answers whether it would have been allowed."""
    ec2 = client_factory(settings)
    script = user_data(settings, 'dry-run', 'on-demand-check')
    try:
        await asyncio.to_thread(
            ec2.run_instances,
            **run_request(settings, 'check', 'on-demand-check', script, spot=True, dry_run=True),
        )
    except Exception as error:
        if error_code(error) == 'DryRunOperation':
            return 'AWS would start this machine with these settings.'
        raise
    return 'AWS would start this machine with these settings.'
