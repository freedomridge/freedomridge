"""Target guard for destructive SDK integration suites (also run outside pytest)."""

import os
from urllib.parse import unquote, urlsplit


def require_test_url():
    url = os.getenv('FRIDGE_TEST_URL')
    if not url:
        raise SystemExit('Unsafe SDK test target: set FRIDGE_TEST_URL to a disposable backend')
    try:
        parsed = urlsplit(url)
        host = unquote(parsed.hostname or '').lower().rstrip('.')
        port = parsed.port
        if parsed.scheme not in ('http', 'https') or not host or parsed.fragment:
            raise ValueError
        if any(character.isspace() for character in url) or parsed.username is not None or parsed.password is not None:
            raise ValueError
    except ValueError:
        raise SystemExit('Unsafe SDK test target: FRIDGE_TEST_URL must be an HTTP(S) URL without credentials') from None
    if host in ('localhost', '127.0.0.1', '::1') and port in (8000, 3000):
        if os.getenv('FRIDGE_ALLOW_LIVE_SDK_TESTS') != '1':
            raise SystemExit(
                'Unsafe SDK test target: local ports 8000 and 3000 require FRIDGE_ALLOW_LIVE_SDK_TESTS=1',
            )
    return url
