"""A lasso (an `embedding_polygon` filter) records the layout of the map it was drawn on (`layout_id`, from the
scatter). Once the map is laid out again (embedding_layouts), the same polygon may select other samples. The app warns
on the lasso's chip; on the server, a bulk delete by such a filter is refused, other bulk edits go ahead and say so
(`stale_lassos`), and so does a samples query, for the SDK (docs/architecture/JOBS.md).

The rule is the app's (frontend/lib/stale-lasso.ts), in one place here: a lasso is stale when its map has a layout id
and the lasso's differs, a lasso without one counting as different. A map without an id is still in the layout every
lasso without one was drawn on; a lasso with an id on a map without one can't be told apart (a mirror that hasn't taken
its primary's id yet), so it isn't stale. On a mirror the map's id is the mirror's own (embedding_layouts)."""

from typing import Any, Optional

LASSO_OPERATORS = ('embedding_polygon', 'embedding_polygon_exclude')
# Bulk operations that remove data: refused on a filter holding a stale lasso, when submitted and in every unit.
DESTRUCTIVE = ('delete', 'delete_embeddings')


class StaleLassoRefused(Exception):
    """A destructive bulk job's lasso went stale after it was submitted: the unit is rolled back and the job fails."""


def lasso_is_stale(recorded: Optional[str], current: Optional[str]) -> bool:
    return current is not None and recorded != current


def lassos(node: Any) -> list[tuple[str, Optional[str]]]:
    """(map, recorded layout id) of every lasso in a filter tree: nested groups, and the filters a cross-grain
    wrapper holds as its value."""
    if not isinstance(node, dict):
        return []
    found = []
    if node.get('operator') in LASSO_OPERATORS and isinstance(node.get('field'), str):
        recorded = node.get('layout_id')
        found.append((node['field'], recorded if isinstance(recorded, str) else None))
    for child in node.get('filters') or []:
        found.extend(lassos(child))
    if isinstance(node.get('value'), dict):
        found.extend(lassos(node['value']))
    return found


async def stale_maps(source, dataset_id: int, node: Any) -> list[str]:
    """The maps of the filter's lassos drawn on an earlier layout of them, sorted. No query when it holds no lasso."""
    found = lassos(node)
    if not found:
        return []
    rows = await source.fetch(
        'SELECT field_name, layout_id::text AS layout_id FROM embedding_layouts '
        'WHERE dataset_id = $1 AND field_name = ANY($2::text[])',
        dataset_id,
        sorted({field for field, _ in found}),
    )
    current = {row['field_name']: row['layout_id'] for row in rows}
    return sorted({field for field, recorded in found if lasso_is_stale(recorded, current.get(field))})


def describe(maps: list[str]) -> str:
    """'the clip map', 'the clip and dino maps'."""
    if len(maps) == 1:
        return f'the {maps[0]} map'
    return f"the {', '.join(maps[:-1])} and {maps[-1]} maps"


def refusal(maps: list[str]) -> str:
    return (
        f'The filter holds a lasso drawn on an earlier layout of {describe(maps)}: it may select different samples '
        'now, so nothing is deleted. Redraw the lasso on the map as it is now, or remove it, then delete again.'
    )


def stopped(maps: list[str]) -> str:
    return (
        f'T{describe(maps)[1:]} {"was" if len(maps) == 1 else "were"} laid out again after this delete was '
        'submitted: its lasso may select different samples now, so the job stopped here and deleted nothing more. '
        'Redraw the lasso on the map as it is now, or remove it, then delete again.'
    )
