"""A mirror's own fields, addressed as `@local.<name>` (docs/FOLLOWERS.md): where each one is stored, for every place
that turns a field name into SQL. `@local.tags` is the native array local_tags; any other name is a key of
local_metadata. The name after the prefix has no dots, so the dotted-path rules for metadata never apply to it."""

import re
from dataclasses import dataclass
from typing import Any, Optional

from .field_registry import LOCAL_PREFIX

LOCAL_TAGS = LOCAL_PREFIX + 'tags'
_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}\Z')


@dataclass(frozen=True)
class LocalField:
    name: str  # after the prefix, checked against _NAME: safe to format into SQL as a JSON key

    @property
    def array(self) -> bool:
        return self.name == 'tags'

    @property
    def column(self) -> str:
        """The samples column it's stored in."""
        return 'local_tags' if self.array else 'local_metadata'

    @property
    def text(self) -> str:
        """Its value as text, as `metadata->>'x'` is a metadata field's."""
        return 'local_tags' if self.array else f"local_metadata->>'{self.name}'"


def local_field(field: Any) -> Optional[LocalField]:
    """Where an `@local.` field is stored; None for any other name. A bad name after the prefix raises ValueError."""
    if not isinstance(field, str) or not field.startswith(LOCAL_PREFIX):
        return None
    name = field[len(LOCAL_PREFIX) :]
    if not _NAME.match(name):
        raise ValueError(
            f'Invalid field name: {field!r} (after {LOCAL_PREFIX}, a letter or _, then letters, digits or _)',
        )
    return LocalField(name)
