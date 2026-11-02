"""Fail closed on database targets in tests. Standard library only for CLI callers."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

TEST_MARKERS = ('FRIDGE_TEST_MODE', 'PYTEST_CURRENT_TEST', 'FRIDGE_TEST_POSTGRES_CONTAINER', 'FRIDGE_TEST_POSTGRES_DSN')
PROTECTED_CONTAINERS = ('freedomridge-db', 'fr-drill', 'fr-drill2')
PROTECTED_PROJECTS = {'freedomridge', Path(__file__).resolve().parents[1].name}


class UnsafeTestDatabase(ValueError):
    def __init__(self, reason):
        super().__init__(f'Unsafe test database target: {reason}')


def test_mode():
    return any(os.environ.get(key) for key in TEST_MARKERS)


def container_identity(target):
    if not target or target.lstrip('/') in PROTECTED_CONTAINERS:
        raise UnsafeTestDatabase('an explicit disposable container is required')
    try:
        result = subprocess.run(
            ['docker', 'inspect', '--type', 'container', '--format', '{{json .}}', '--', target],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        info = json.loads(result.stdout)
        name, identity = info['Name'], info['Id']
        if name.lstrip('/') in PROTECTED_CONTAINERS or not re.fullmatch('[0-9a-f]{64}', identity):
            raise ValueError
        labels = info['Config']['Labels'] or {}
        if labels.get('com.docker.compose.project') in PROTECTED_PROJECTS:
            raise ValueError
        # Check both configured and active bindings, including stopped containers
        # and IPv6/wildcard bindings. The container-side port is irrelevant.
        for ports in (info['HostConfig']['PortBindings'], info['NetworkSettings']['Ports']):
            for bindings in (ports or {}).values():
                if any(binding['HostPort'] == '5432' for binding in bindings or []):
                    raise ValueError
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        raise UnsafeTestDatabase('container inspection failed or resolved to a protected container') from None
    return identity


def require_container(target, expected=None):
    # Resolve names, full IDs and ID prefixes to immutable IDs before comparing.
    identity = container_identity(target)
    if expected and target != expected and identity != container_identity(expected):
        raise UnsafeTestDatabase('container must match FRIDGE_TEST_POSTGRES_CONTAINER')
    return identity


def endpoint(url):
    try:
        parsed = urlsplit(url)
        host = unquote(parsed.hostname or '').lower()
        if parsed.scheme not in ('postgres', 'postgresql') or not host or parsed.fragment:
            raise ValueError
        if any(character.isspace() for character in url) or re.search(r'%(?![0-9a-fA-F]{2})', url):
            raise ValueError
        if any(character in host for character in ('/', ',', '\0')):
            raise ValueError
        # libpq query parameters override URI authority. Never allow routing
        # through service files, hostaddr, sockets, or multi-host failover in tests.
        forbidden = {'host', 'hostaddr', 'port', 'dbname', 'service', 'servicefile'}
        keys = {unquote(item.partition('=')[0]) for item in parsed.query.split('&')}
        if keys & forbidden:
            raise ValueError
        port = parsed.port if parsed.port is not None else 5432
        if not 0 < port <= 65535:
            raise ValueError
        return host, port
    except ValueError:
        raise UnsafeTestDatabase('use a single explicit TCP host and port without routing overrides') from None


def require_test_url(url):
    if any(os.environ.get(key) for key in ('PGHOSTADDR', 'PGPORT', 'PGSERVICE', 'PGSERVICEFILE')):
        raise UnsafeTestDatabase('clear libpq routing environment overrides in URL mode')
    expected = os.environ.get('FRIDGE_TEST_POSTGRES_DSN')
    if not expected or endpoint(url) != endpoint(expected):
        raise UnsafeTestDatabase('URL host and port must match FRIDGE_TEST_POSTGRES_DSN')


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['url']:
            require_test_url(os.environ.get('DATABASE_URL', ''))
        elif sys.argv[1:] == ['container']:
            print(
                require_container(
                    os.environ.get('FRIDGE_DB_CONTAINER', ''),
                    os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
                ),
            )
        else:
            raise UnsafeTestDatabase('unknown safety check')
    except UnsafeTestDatabase as error:
        sys.exit(str(error))
