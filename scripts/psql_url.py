"""Run psql against DATABASE_URL without putting its password on the command line."""

import os
import re
import sys
from urllib.parse import unquote, urlsplit, urlunsplit


def connection():
    url = os.environ.get('DATABASE_URL', '')
    parsed = urlsplit(url)
    if not url.startswith(('postgres://', 'postgresql://')) or not url.split('://', 1)[1]:
        raise ValueError
    if parsed.fragment or any(character.isspace() for character in url):
        raise ValueError
    if '\0' in url or '%00' in url.lower() or re.search(r'%(?![0-9a-fA-F]{2})', url):
        raise ValueError
    # Validate a single-host port before invoking psql. Multi-host URIs are validated by libpq.
    if ',' not in parsed.netloc:
        parsed.port
    netloc = parsed.netloc
    env = dict(os.environ)
    if '@' in netloc:
        userinfo, host = netloc.rsplit('@', 1)
        if ':' in userinfo:
            user, password = userinfo.split(':', 1)
            env['PGPASSWORD'] = unquote(password)
            netloc = f'{user}@{host}'
    query = []
    # libpq percent-decodes URI parameters, but unlike form data keeps literal '+'.
    for item in parsed.query.split('&'):
        key, separator, value = item.partition('=')
        if unquote(key) == 'password':
            if not separator:
                raise ValueError
            env['PGPASSWORD'] = unquote(value)
        elif item:
            query.append(item)
    return urlunsplit(parsed._replace(netloc=netloc, query='&'.join(query))), env


if __name__ == '__main__':
    try:
        target, environment = connection()
    except (ValueError, KeyError):
        sys.exit('Invalid DATABASE_URL; expected a PostgreSQL connection URI')
    if sys.argv[1:] != ['--check']:
        os.execvpe('psql', ['psql', f'--dbname={target}', *sys.argv[1:]], environment)
