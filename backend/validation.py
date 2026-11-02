"""Shared checks for user-supplied names and tags.

The API stored whatever it was given: an empty or whitespace-only username could be created and
could log in, dataset / version / saved-view / token names could be blank or differ from
an existing one only by case or a trailing space, and tags were
stored verbatim — duplicates, "" and "  " included.

Name errors are HTTPException(400) with a plain-string detail: the frontend shows `detail` as the
error message, and a Pydantic 422's list-shaped detail would render as "[object Object]".
"""

from fastapi import HTTPException

NAME_MAX = 255


def clean_name(value: str, what: str) -> str:
    """`value` without surrounding whitespace; 400 when that leaves nothing or more than NAME_MAX."""
    name = value.strip()
    if not name:
        raise HTTPException(status_code=400, detail=f"{what} must not be empty")
    if len(name) > NAME_MAX:
        raise HTTPException(status_code=400, detail=f"{what} must be at most {NAME_MAX} characters")
    return name


def clean_tags(tags: list[str]) -> list[str]:
    """Tags without surrounding whitespace, empties dropped, duplicates removed (first one kept)."""
    out: list[str] = []
    for tag in tags:
        tag = tag.strip()
        if tag and tag not in out:
            out.append(tag)
    return out
