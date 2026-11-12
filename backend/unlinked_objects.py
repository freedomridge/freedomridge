"""Object samples with no image: finding them, and the filter that shows or deletes them.

Ingest accepts an object whose parent_id names no image of its dataset (its image may come later, and then links it),
so a dataset can hold objects with no image. Each one is of one kind, as 0029 counts them:

- image_gone: its parent_id is a sample id no sample has, in any dataset (its image was deleted, or never came);
- other_dataset: it names a sample of another dataset;
- object: it names another object of its dataset;
- not_a_sample_id: its parent_id isn't a sample id (text, a negative number, a fraction);
- no_parent_id: it has none (missing, or null).

An object whose image IS there but isn't linked yet is of none of them ('linkable'): the finder links those first.
Deleting takes only the kinds named, by default image_gone, as a bulk delete of this filter.
"""

from typing import Any, Dict, List, Optional

KINDS = ('image_gone', 'other_dataset', 'object', 'not_a_sample_id', 'no_parent_id')
DEFAULT_DELETE = ('image_gone',)
OPERATOR = 'unlinked_object'

# The count the finder and the app's "N objects have no image" ask; idx_samples_unlinked_objects answers it from the
# index alone (test_unlinked_objects.py plans it, custom and generic).
COUNT_SQL = "SELECT count(*) FROM samples WHERE dataset_id = $1 AND media_type = 'object' AND parent_id IS NULL"


def kind_sql(table: str, dataset: str) -> str:
    """The kind of an unlinked object row `table` of dataset `dataset` (SQL expressions), as 0029 decides it."""
    value = f"{table}.metadata->>'parent_id'"
    return (
        f"CASE WHEN coalesce({table}.metadata->'parent_id', 'null') = 'null' THEN 'no_parent_id' "
        f"WHEN NOT coalesce({value} ~ '^[0-9]{{1,10}}$', FALSE) THEN 'not_a_sample_id' "
        f"ELSE coalesce((SELECT CASE WHEN _named.dataset_id <> {dataset} THEN 'other_dataset' "
        f"WHEN _named.media_type = 'object' THEN 'object' ELSE 'linkable' END "
        f"FROM samples _named WHERE _named.id = CASE WHEN {value} ~ '^[0-9]{{1,10}}$' THEN ({value})::bigint END), "
        "'image_gone') END"
    )


def kinds_of(value: Any) -> List[str]:
    """The kinds a filter's value names: one, or a list; all of them when it names none."""
    if value is None or value == []:
        return list(KINDS)
    named = [value] if isinstance(value, str) else value
    if not isinstance(named, list) or any(kind not in KINDS for kind in named):
        raise ValueError(f'{OPERATOR} takes kinds from {", ".join(KINDS)}')
    return sorted(set(named), key=KINDS.index)


def filter_leaf(kinds: Optional[List[str]] = None) -> Dict[str, Any]:
    """The filter of the objects with no image of these kinds (all of them by default)."""
    return {'field': 'parent_id', 'operator': OPERATOR, 'value': kinds_of(kinds)}


def compile_leaf(value: Any, table: str, dataset: str, arg_idx: int, args: list) -> tuple[str, int]:
    """SQL for the filter: an object row with no image, of one of the kinds."""
    args.append(kinds_of(value))
    return (
        f"({table}.media_type = 'object' AND {table}.parent_id IS NULL "
        f"AND ({kind_sql(table, dataset)}) = ANY(${arg_idx}::text[]))",
        arg_idx + 1,
    )


async def counts(connection, dataset_id: int) -> Dict[str, Any]:
    """The objects with no image of a dataset, by kind, and those waiting to be linked."""
    rows = await connection.fetch(
        f"""SELECT {kind_sql('s', '$1')} AS kind, count(*) AS n FROM samples s
            WHERE s.dataset_id = $1 AND s.media_type = 'object' AND s.parent_id IS NULL GROUP BY 1""",
        dataset_id,
    )
    found = {row['kind']: row['n'] for row in rows}
    by_kind = {kind: found.get(kind, 0) for kind in KINDS}
    return {'total': sum(by_kind.values()), 'kinds': by_kind, 'linkable': found.get('linkable', 0)}


async def link(connection, dataset_id: int) -> int:
    """Link the objects of a dataset that have no image yet but whose image is there; returns how many."""
    return await connection.fetchval('SELECT link_objects($1, TRUE)', dataset_id)
