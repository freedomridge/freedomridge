"""Class-probability overlays (docs/SDK.md "Class probabilities"): the rule that turns a pixel's class probabilities
into one class. A field's default rule is stored in segmentation_config.class_rules[field]; the app applies rules
itself (frontend/lib/class-rules.ts), so the server only checks their shape.

A rule is either ``{"type": "argmax"}``, the most probable class, or ``{"type": "rules", "rules": [...],
"fallback": class or null}``. Each of ``rules`` is ``{"classes": [k, ...], "min": 0-1, "class": k}``: evaluated in
order, the first whose classes' summed probability is at least ``min`` gives the pixel its ``class``; none passing
gives ``fallback``, and null draws nothing. Class 0 is never drawn, as in index masks.
"""

MAX_RULES = 64
MAX_CLASSES = 256


def _class_index(value, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < MAX_CLASSES:
        raise ValueError(f'{what} must be a class index from 0 to {MAX_CLASSES - 1}, not {value!r}')
    return value


def validate_rule(rule) -> dict:
    """The rule in its stored form, or ValueError saying what's wrong with it."""
    if not isinstance(rule, dict):
        raise ValueError('A class rule is an object with a "type"')
    if rule.get('type') == 'argmax':
        if set(rule) != {'type'}:
            raise ValueError('An argmax rule has only "type"')
        return {'type': 'argmax'}
    if rule.get('type') != 'rules':
        raise ValueError('A class rule\'s "type" is "argmax" or "rules"')
    unknown = set(rule) - {'type', 'rules', 'fallback'}
    if unknown:
        raise ValueError(f'Unknown keys in a class rule: {sorted(unknown)}')
    rules = rule.get('rules')
    if not isinstance(rules, list) or not 1 <= len(rules) <= MAX_RULES:
        raise ValueError(f'"rules" is a list of 1 to {MAX_RULES} rules')
    checked = []
    for position, entry in enumerate(rules):
        where = f'Rule {position + 1}'
        if not isinstance(entry, dict) or set(entry) != {'classes', 'min', 'class'}:
            raise ValueError(f'{where} is an object with "classes", "min" and "class"')
        classes = entry['classes']
        if not isinstance(classes, list) or not classes:
            raise ValueError(f'{where}: "classes" is a non-empty list of class indices')
        classes = [_class_index(value, f'{where}: each of "classes"') for value in classes]
        if len(set(classes)) != len(classes):
            raise ValueError(f'{where}: "classes" lists a class twice')
        minimum = entry['min']
        if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not 0 <= minimum <= 1:
            raise ValueError(f'{where}: "min" is a probability from 0 to 1')
        checked.append({'classes': classes, 'min': float(minimum), 'class': _class_index(entry['class'], where)})
    fallback = rule.get('fallback')
    if fallback is not None:
        fallback = _class_index(fallback, '"fallback"')
    return {'type': 'rules', 'rules': checked, 'fallback': fallback}


def validate_config(config) -> dict:
    """segmentation_config with its class_rules checked: a dict of field name to rule."""
    if not isinstance(config, dict) or 'class_rules' not in config:
        return config
    rules = config['class_rules']
    if not isinstance(rules, dict):
        raise ValueError('segmentation_config.class_rules is an object of field name to rule')
    checked = {}
    for field, rule in rules.items():
        try:
            checked[field] = validate_rule(rule)
        except ValueError as error:
            raise ValueError(f'segmentation_config.class_rules.{field}: {error}') from None
    return {**config, 'class_rules': checked}
