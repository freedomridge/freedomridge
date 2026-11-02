#!/usr/bin/env python3
"""Run demo importers in a stable order, with separate logs and a status summary."""

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

from examples._shared.catalog import DATA_DIRS, ORDER, selection  # noqa: E402
from examples._shared.importer import hash_arguments, media_uri  # noqa: E402

# Importers without --media-uri.
NO_MEDIA_URI = {'bdd100k', 'tusimple_lanes', 'tusimple_velocity'}


def load_env(path, environment):
    """Use python-dotenv quoting/comments, without interpolation or shell execution."""
    from dotenv.parser import parse_stream

    with path.open() as stream:
        for binding in parse_stream(stream):
            if binding.error or (binding.key and (not binding.key.isidentifier() or binding.value is None)):
                raise ValueError(f'{path.name}:{binding.original.line}: expected KEY=value')
            if binding.key:
                environment.setdefault(binding.key, binding.value)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--datasets', default='coco', help='Comma-separated names; tusimple selects both benchmarks')
    parser.add_argument('--all', action='store_true', help='All datasets, including manually acquired/local-only data')
    parser.add_argument('--data-root', type=Path)
    limit = parser.add_mutually_exclusive_group()
    limit.add_argument(
        '--limit',
        type=int,
        default=100,
        help='Per-importer limit: EuroSAT per class, BDD100K per split, otherwise total; 0 = all',
    )
    limit.add_argument('--full', action='store_true', help='Import all available samples (limit=0)')
    parser.add_argument('--device', choices=('auto', 'cuda', 'cpu'), default='auto')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--api-url')
    parser.add_argument('--tusimple-lanes-source', type=Path, help='Override FRIDGE_TUSIMPLE_LANES_SOURCE')
    parser.add_argument('--tusimple-velocity-source', type=Path, help='Override FRIDGE_TUSIMPLE_VELOCITY_SOURCE')
    parser.add_argument('--log-dir', type=Path, help='Default: <data-root>/.import-logs/<UTC timestamp>')
    parser.add_argument(
        '--media-uri',
        type=media_uri,
        help='s3://BUCKET/PREFIX: upload each dataset\'s media under PREFIX/<dataset folder> and record s3:// paths',
    )
    parser.add_argument('--no-reduce', action='store_true')
    hash_arguments(parser)
    return parser


def commands(args, environment):
    root = args.data_root or environment.get('FRIDGE_DATA_ROOT')
    if not root:
        raise ValueError('Set FRIDGE_DATA_ROOT or supply --data-root (without --media-uri the backend must serve it)')
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f'Data root is not a directory: {root}')
    if not (
        environment.get('FRIDGE_API_TOKEN')
        or (environment.get('FRIDGE_USERNAME') and environment.get('FRIDGE_PASSWORD'))
    ):
        raise ValueError('Set FRIDGE_API_TOKEN from Settings → API Tokens, or both FRIDGE_USERNAME and FRIDGE_PASSWORD')
    if args.limit < 0:
        raise ValueError('--limit must be >= 0')
    names = list(ORDER) if args.all else selection(args.datasets, (*ORDER, 'tusimple'))
    if 'tusimple' in names:
        names = [n for n in names if n != 'tusimple'] + ['tusimple_lanes', 'tusimple_velocity']
    if args.media_uri and (local_only := sorted(set(names) & NO_MEDIA_URI)):
        raise ValueError(
            f'--media-uri cannot include {", ".join(local_only)}: BDD100K records its own S3 paths '
            '(its --s3-bucket) and TuSimple\'s terms keep it local',
        )
    api = args.api_url or environment.get('FRIDGE_API_URL', 'http://localhost:8000/api')
    repo = Path(__file__).resolve().parents[1]
    result = []
    for name in ORDER:
        if name not in names:
            continue
        script = f'tusimple/ingest_{name.split("_")[1]}.py' if name.startswith('tusimple_') else f'{name}/ingest.py'
        command = [
            sys.executable,
            '-u',
            str(repo / 'examples' / script),
            '--data-root',
            str(root / DATA_DIRS.get(name, name)),
            '--limit',
            str(0 if args.full else args.limit),
            '--device',
            args.device,
            '--api-url',
            api,
        ]
        if args.no_reduce:
            command.append('--no-reduce')
        if args.media_uri:
            # One folder per dataset, as on disk: two importers' derived/ crops must not share keys.
            folder = DATA_DIRS.get(name, name)
            command.extend(['--media-uri', args.media_uri if folder == '.' else f'{args.media_uri}/{folder}'])
        command.extend(['--hash-algorithms', *args.hash_algorithms])
        if args.no_hashes:
            command.append('--no-hashes')
        if name.startswith('tusimple_'):
            folder = 'TusimpleLaneDetection' if name == 'tusimple_lanes' else 'TusimpleVelocity'
            source = (
                getattr(args, name + '_source')
                or environment.get('FRIDGE_' + name.upper() + '_SOURCE')
                or root / 'tusimple' / folder
            )
            command.extend(['--source-root', str(source)])
        result.append((name, command))
    environment['FRIDGE_DATA_ROOT'] = str(root)
    environment['HF_HUB_OFFLINE'] = environment['TRANSFORMERS_OFFLINE'] = '1'
    environment['PYTHONPATH'] = os.pathsep.join(filter(None, [str(repo / 'python'), environment.get('PYTHONPATH')]))
    return root, result


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    environment = dict(os.environ)
    try:
        if args.env_file:
            load_env(args.env_file, environment)
        root, runs = commands(args, environment)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    logs = args.log_dir or root / '.import-logs' / timestamp
    logs.mkdir(parents=True, exist_ok=True)
    statuses = []
    with (logs / 'status.txt').open('a') as status:
        for name, command in runs:
            path = logs / f'{name}.log'
            print(f'START {name}: {path}', flush=True)
            with path.open('a') as log:
                log.write(f'\nImport started {timestamp}\n')
                log.flush()
                try:
                    result = subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT, check=False)
                    code = result.returncode
                except OSError as error:
                    log.write(str(error) + '\n')
                    code = 1
            line = f'{name}: {"PASS" if code == 0 else "FAIL"} (exit {code}) — {path}'
            print(line, flush=True)
            status.write(line + '\n')
            status.flush()
            statuses.append(code)
    print(f'{len(statuses) - sum(bool(code) for code in statuses)}/{len(statuses)} imports passed; logs: {logs}')
    return int(any(statuses))


if __name__ == '__main__':
    raise SystemExit(main())
