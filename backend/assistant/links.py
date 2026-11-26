"""Links the assistant gives a person: to a view in the app, to a sample, to a value list's download. They start with
the server's configured public address, never with the address a request came in on (an inner request has none).

The address: FRIDGE_PUBLIC_URL when set; else the first of ALLOWED_ORIGINS when that is set (a production deploy must
set it to the HTTPS public origin: docker-compose.prod.yml); else http://localhost:3000, the app's own address on a
server run as the quick start runs it.
"""

import os
from typing import Optional
from urllib.parse import quote

# The app's own limit for a link (frontend/lib/links.ts MAX_LINK_LENGTH): proxies commonly cap a request line at 8 KB.
MAX_LINK_LENGTH = 6000
DEFAULT_ADDRESS = 'http://localhost:3000'


def public_address() -> str:
    explicit = os.getenv('FRIDGE_PUBLIC_URL', '').strip().rstrip('/')
    if explicit:
        return explicit
    origins = os.getenv('ALLOWED_ORIGINS')
    if origins:
        first = next((origin.strip().rstrip('/') for origin in origins.split(',') if origin.strip()), '')
        if first:
            return first
    return DEFAULT_ADDRESS


def text_link(dataset_id: int, query: str) -> Optional[str]:
    """`?q=` with the filter text, when the link fits the app's limit; None otherwise."""
    link = f'{public_address()}/datasets/{dataset_id}?q={quote(query, safe="")}'
    return link if len(link) <= MAX_LINK_LENGTH else None


def view_link(dataset_id: int, handle: str) -> str:
    return f'{public_address()}/datasets/{dataset_id}?w={quote(handle, safe="")}'


def saved_view_link(dataset_id: int, slug: str) -> str:
    """A saved view's own permanent link, as the app's Copy link writes it."""
    return f'{public_address()}/datasets/{dataset_id}?view={quote(slug, safe="")}'


def sample_link(dataset_id: int, sample_id: int) -> str:
    return f'{public_address()}/datasets/{dataset_id}?sample={sample_id}'


def list_download(handle: str) -> str:
    return f'{public_address()}/api/assistant/lists/{quote(handle, safe="")}/csv'
