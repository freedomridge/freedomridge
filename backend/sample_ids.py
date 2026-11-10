"""Sample ids in filters across instances: each instance numbers its own samples, and mirror_samples maps a
mirror's to its primary's. Shared views (follower_sync, mirror_writes) and relayed searches (follower_search) translate
them with these, so the two can't drift apart. No imports from either: follower_sync imports the routers, which
import follower_search."""

# Leaf fields whose values are sample ids: each instance has its own (mirror_samples maps them).
SAMPLE_ID_FIELDS = frozenset({'id', 'parent_id', 'metadata.parent_id'})
# What a filter may do with them across instances: name ids, which are translated (translate_ids), or test whether
# one is set. A range or a substring can't be translated: the other instance numbers its samples in another order.
TRANSLATED_ID_OPERATORS = ('=', '!=', 'in', 'not in')
SAMPLE_ID_OPERATORS = frozenset({*TRANSLATED_ID_OPERATORS, 'is_null', 'is_not_null', 'exists', 'not_exists'})
# Operators whose value is a condition on each label element (its own keys, an `id` among them), not on samples.
ELEMENT_OPERATORS = ('elem_match', 'match_elements')


def ranges_on_ids(node) -> bool:
    """Whether a filter compares sample ids in a way another instance can't translate (`id > 15`)."""
    if isinstance(node, dict):
        if node.get('operator') in ELEMENT_OPERATORS:
            return False
        if node.get('field') in SAMPLE_ID_FIELDS and 'operator' in node and node['operator'] not in SAMPLE_ID_OPERATORS:
            return True
        return any(ranges_on_ids(value) for value in node.values())
    if isinstance(node, list):
        return any(ranges_on_ids(value) for value in node)
    return False


def sample_ids_in(node, found: set) -> set:
    """The sample ids a filter names (`id in [...]` from a brushed selection, `id = n`, an object's parent)."""
    if isinstance(node, dict):
        if node.get('operator') in ELEMENT_OPERATORS:
            return found
        if node.get('field') in SAMPLE_ID_FIELDS and node.get('operator') in TRANSLATED_ID_OPERATORS:
            value = node.get('value')
            found.update(v for v in (value if isinstance(value, list) else [value]) if isinstance(v, int))
        for value in node.values():
            sample_ids_in(value, found)
    elif isinstance(node, list):
        for value in node:
            sample_ids_in(value, found)
    return found


def translate_ids(node, mapping: dict):
    """A filter with one instance's sample ids replaced by the other's. A sample that isn't there matches nothing
    either way: it leaves a list, and a single id becomes 0, which no sample has."""
    if isinstance(node, dict):
        if node.get('operator') in ELEMENT_OPERATORS:
            return node
        node = {key: translate_ids(value, mapping) for key, value in node.items()}
        if node.get('field') in SAMPLE_ID_FIELDS and node.get('operator') in TRANSLATED_ID_OPERATORS:
            value = node.get('value')
            if isinstance(value, list):
                node['value'] = [mapping[v] for v in value if v in mapping]
            elif isinstance(value, int):
                node['value'] = mapping.get(value, 0)
        return node
    if isinstance(node, list):
        return [translate_ids(value, mapping) for value in node]
    return node
