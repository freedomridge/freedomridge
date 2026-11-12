"""Text for the datetime rule's tests (backend/datetimes.py): the shared cases, the edges of every refusal, and a few
hundred thousand generated strings, mostly near the grammar, from a fixed seed."""

import json
import random
from pathlib import Path

CASES = json.loads((Path(__file__).parent / 'datetime_cases.json').read_text())['cases']
# Characters a mutation puts in: the grammar's own, its near misses, and digits and spaces that aren't ASCII.
_ALPHABET = '0123456789-+:. TZtz,/_aAeEpPmMUC١０ \n\t'


def _two(rng: random.Random, high: int) -> str:
    return f'{rng.randint(0, high):02d}'


def _year(rng: random.Random) -> str:
    if rng.random() < 0.5:
        return rng.choice(['0000', '0001', '1582', '1600', '1900', '1970', '2000', '2024', '2026', '2100', '9999'])
    return f'{rng.randint(0, 9999):04d}'


def _near_grammar(rng: random.Random) -> str:
    """Grammar-shaped text whose numbers stray past their ranges now and then."""
    text = f'{_year(rng)}-{_two(rng, 13)}-{_two(rng, 32)}'
    if rng.random() < 0.15:
        return text
    text += rng.choice('TTT  ') + f'{_two(rng, 25)}:{_two(rng, 61)}'
    if rng.random() < 0.8:
        text += f':{_two(rng, 61)}'
        if rng.random() < 0.5:
            text += '.' + ''.join(rng.choice('0123456789') for _ in range(rng.randint(1, 9)))
    offset = rng.random()
    if offset < 0.3:
        return text
    if offset < 0.45:
        return text + 'Z'
    sign, hours, minutes = rng.choice('+-'), _two(rng, 17), _two(rng, 61)
    return text + sign + hours + rng.choice(['', minutes, ':' + minutes])


def _valid(rng: random.Random) -> str:
    """Text in the grammar with every number in range, a few days past the end of short months included."""
    text = f'{rng.randint(1, 9999):04d}-{rng.randint(1, 12):02d}-{rng.randint(1, 31):02d}'
    if rng.random() < 0.1:
        return text
    text += rng.choice('T ') + f'{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}'
    if rng.random() < 0.8:
        text += f':{rng.randint(0, 59):02d}'
        if rng.random() < 0.6:
            text += '.' + ''.join(rng.choice('0123456789') for _ in range(rng.randint(1, 8)))
    hours, minutes = f'{rng.randint(0, 15):02d}', f'{rng.randint(0, 59):02d}'
    return text + rng.choice(['', 'Z', rng.choice('+-') + rng.choice([hours, hours + minutes, f'{hours}:{minutes}'])])


def _mutated(rng: random.Random, text: str) -> str:
    characters = list(text)
    for _ in range(rng.randint(1, 3)):
        position = rng.randint(0, len(characters))
        action = rng.random()
        if action < 0.4 and position < len(characters):
            characters[position] = rng.choice(_ALPHABET)
        elif action < 0.7:
            characters.insert(position, rng.choice(_ALPHABET))
        elif characters:
            del characters[min(position, len(characters) - 1)]
    return ''.join(characters)


def corpus(size: int = 300_000, seed: int = 20261111) -> list[str]:
    rng = random.Random(seed)
    texts = [text for text, _ in CASES]
    # Long text: a long fraction (in the grammar), and long junk.
    texts += [
        '2026-01-01T10:00:00.' + '9' * 100_000 + 'Z',
        '2026-01-01T10:00:00.' + '1' * 5_000 + '+05:30',
        '2026-01-01T10:00:00.' + '1' * 5_000 + 'x',
        'x' * 100_000,
        '2026-01-01' + ' ' * 10_000,
    ]
    # Every date alone, month 00-13 and day 00-32, in years where the leap rules differ.
    for year in '0000 0001 0004 1582 1600 1700 1900 2000 2023 2024 2100 2400 9999'.split():
        texts += [f'{year}-{month:02d}-{day:02d}' for month in range(14) for day in range(33)]
    # Within an offset of the ends of Python's range, where only SQL has an instant.
    texts += ['0001-01-01T00:00+01:00', '0001-01-01T00:00+15:59', '0001-01-01T15:59-15:59', '9999-12-31T23:59-00:01']
    while len(texts) < size:
        pick = rng.random()
        if pick < 0.35:
            texts.append(_valid(rng))
        elif pick < 0.7:
            texts.append(_near_grammar(rng))
        elif pick < 0.92:
            texts.append(_mutated(rng, _valid(rng) if rng.random() < 0.5 else _near_grammar(rng)))
        else:
            text = _valid(rng)
            texts.append(text[: rng.randint(0, len(text))])
    return texts
