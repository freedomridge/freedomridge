"""The CHANGELOG.md heading that holds a release's notes, before and after the release is cut."""

import re
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[2] / 'CHANGELOG.md'


def release_heading(version: str, text: str | None = None) -> str:
    """`## <version> (<date>)` once the release commit has named the section, `## Unreleased` until then.

    A test of a change's notes names the release that carries it, so it reads the same notes after the release
    commit renames `## Unreleased` and the next release opens a new `## Unreleased` above them."""
    text = CHANGELOG.read_text() if text is None else text
    for line in text.splitlines():
        if re.fullmatch(rf'## {re.escape(version)} \(\d{{4}}-\d{{2}}-\d{{2}}\)', line.rstrip()):
            return line.rstrip()
    return '## Unreleased'
