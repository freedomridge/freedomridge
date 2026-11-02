"""HTTP errors with the application's string detail and a separate machine-readable code."""

from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from collections.abc import Mapping


class CodedHTTPException(HTTPException):
    def __init__(self, status_code: int, detail: str, code: str, headers: 'Mapping[str, str] | None' = None):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code
