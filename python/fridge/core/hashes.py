"""Named perceptual hashes shared by SDK backfills and local importers."""

from functools import partial

DEFAULT_HASH_ALGORITHMS = ('dhash_8', 'phash_8')


def resolve_hash_algorithms(algorithms=None):
    """Map standard imagehash names (dhash_N, phash_N, ahash_N, whash_N) to hash functions.

    Callers pass RGB pixels without EXIF transposition, matching the SDK image
    loader. imagehash performs its own grayscale conversion and returns lowercase,
    zero-padded hexadecimal strings (16 characters for the default 64-bit hashes).
    """
    import imagehash

    functions = {
        'dhash': imagehash.dhash,
        'phash': imagehash.phash,
        'ahash': imagehash.average_hash,
        'whash': imagehash.whash,
    }
    resolved = {}
    for name in DEFAULT_HASH_ALGORITHMS if algorithms is None else algorithms:
        kind, separator, size = name.rpartition('_')
        if not separator or kind not in functions or not size.isdecimal() or int(size) < 2:
            raise ValueError(f'Invalid hash algorithm {name!r}; use dhash_N, phash_N, ahash_N or whash_N (N >= 2)')
        if kind == 'whash' and int(size) & (int(size) - 1):
            raise ValueError('whash size must be a power of two')
        resolved[name] = partial(_hex_hash, functions[kind], int(size))
    if not resolved:
        raise ValueError('At least one hash algorithm is required')
    return resolved


def _hex_hash(function, size, image):
    return str(function(image, hash_size=size))
