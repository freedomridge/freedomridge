"""What each field of a dataset means: a description and a unit written by people (dataset_fields, 0035), and the
server's own for the fields every dataset has and those the SDK's compute_metadata() and compute_metrics() write.

A field with no row of its own (id, filepath, created_at, last_modified_at, tags, media_type, and the label fields
the statistics find, such as detections.label) is given one when someone describes it, with origin 'system': the
registry's readers already pass over those rows, which older databases have for tags and media_type."""

import unicodedata
from collections.abc import Mapping
from typing import Any, Optional

DESCRIPTION_LIMIT = 1000
UNIT_LIMIT = 40

# Fields every dataset lists, none of which has a dataset_fields row of its own.
BUILT_IN_FIELDS = frozenset({'id', 'filepath', 'created_at', 'last_modified_at', 'tags', 'media_type'})

# (description, unit): true of how each value is made, so each was read from the code that makes it. The metrics
# are python/fridge/core/dataset.py's compute_image_metrics() on the image as 8-bit RGB, its grey levels Pillow's
# convert("L"); the metadata is its _media_metadata(). A user's own field of the same name gets these too, until
# someone describes it.
DEFAULTS: dict[str, tuple[str, Optional[str]]] = {
    'id': ("The sample's number on this server, unique across all of its datasets.", None),
    'filepath': ("Where the sample's file is, such as an s3:// address or a path on the server's disk.", None),
    'created_at': (
        'When the sample was added: the time given by whoever added it, or the server\'s clock when none was given.',
        None,
    ),
    'last_modified_at': (
        'When the sample was last edited, such as a change to its metadata, tags or location; new vectors and '
        'renaming or clearing a field across the dataset don\'t count.',
        None,
    ),
    'tags': ('Labels that people and scripts put on the sample to group, filter and select samples.', None),
    'media_type': (
        "The kind of file the sample is, such as image, video or audio: given when it was added, or else guessed from "
        "the file's extension.",
        None,
    ),
    'width': ("The image's width in pixels, as stored in the file (a camera's rotation tag isn't applied).", 'px'),
    'height': ("The image's height in pixels, as stored in the file (a camera's rotation tag isn't applied).", 'px'),
    'aspect_ratio': (
        'Width divided by height, rounded to 4 decimal places: above 1 for a landscape image, below 1 for portrait.',
        None,
    ),
    'megapixels': ('Width times height in millions of pixels, rounded to 3 decimal places.', 'MP'),
    'file_size_bytes': (
        "The image file's size, recorded only when the sample's path named a file on the disk of the machine that "
        'ran compute_metadata() (never for s3:// files).',
        'bytes',
    ),
    'brightness': (
        "Perceived brightness of the image's average colour, the square root of 0.241·R² + 0.691·G² + 0.068·B² of "
        'its mean red, green and blue values, from 0 (black) to 255 (white).',
        None,
    ),
    'blurriness': (
        'Variance of the Laplacian of the grey-level image: low values suggest a blurry image and high values a '
        'sharp one, despite the name.',
        None,
    ),
    'contrast': ('The highest minus the lowest value over all pixels and all three colour channels, 0 to 255.', None),
    'entropy': (
        'Shannon entropy of the red, green and blue values taken as one 768-bin histogram, from about 1.6 for a single '
        'flat colour to about 9.6 when every value is equally common.',
        'bits',
    ),
    'salt_and_pepper': (
        'Share of pixels that are lone near-white (grey level 244 or more) or near-black (10 or less) specks, the '
        'ones a 2×2 morphological opening removes: a sign of salt-and-pepper noise.',
        '%',
    ),
    'saturation': (
        'Mean colour saturation of the pixels on the HSV scale, from 0 (grey) to 255 (fully saturated colour).',
        None,
    ),
    'pct_underexposed': ('Share of pixels whose grey level is 5 or less (of 255): crushed shadows.', '%'),
    'pct_overexposed': ('Share of pixels whose grey level is 250 or more (of 255): blown highlights.', '%'),
}


def clean_text(value: Optional[str], limit: int, what: str, *, line_breaks: bool) -> Optional[str]:
    """`value` as stored: trimmed, None for None or nothing left. Raises ValueError over `limit` characters, or for a
    control character (a line break or tab is allowed where `line_breaks`; \\r\\n is kept as \\n)."""
    if value is None:
        return None
    text = value.replace('\r\n', '\n').strip()
    if not text:
        return None
    allowed = {'\n', '\t'} if line_breaks else set()
    if any(unicodedata.category(char) in ('Cc', 'Cs') and char not in allowed for char in text):
        raise ValueError(f'{what} may not hold control characters')
    if len(text) > limit:
        raise ValueError(f'{what} can be at most {limit:,} characters long (this one is {len(text):,})')
    return text


def described(name: str, stored: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """The four keys a field carries in the schema and field-stats responses, from its stored row (None: no row, or
    nothing written). A description or unit someone wrote replaces the default's; each falls back to it on its own."""
    default_description, default_unit = DEFAULTS.get(name, (None, None))
    description = stored['description'] if stored else None
    unit = stored['unit'] if stored else None
    return {
        'description': description or default_description,
        'unit': unit or default_unit,
        'values_private': bool(stored and stored['values_private']),
        'description_source': 'set' if description else 'default' if default_description else None,
    }


# The fields of a dataset ($1) someone described, gave a unit or marked private, as one JSON object by name: a
# subquery for a read the request makes anyway (the schema's of its dataset row), so it costs no query of its own.
DESCRIBED_SQL = """(SELECT COALESCE(jsonb_object_agg(field_name, jsonb_build_object(
        'description', description, 'unit', unit, 'values_private', values_private)), '{}'::jsonb)
    FROM dataset_fields WHERE dataset_id = $1 AND (description IS NOT NULL OR unit IS NOT NULL OR values_private))"""
