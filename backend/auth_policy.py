"""The one server-side password rule, for every site that sets a password.

Before this, the only guard was the React modal's `newPassword.length < 8`; the SDK / curl
path accepted "" and "a" for change-password, create-user and admin reset. The rule is
deliberately modest so nothing that runs today breaks:
the seeded dev/CI `admin123` passes, the frontend modals already require 8, and demo
guests have an unusable password sentinel.
"""

from fastapi import HTTPException

MIN_PASSWORD_LENGTH = 8  # matches ChangePasswordModal / CreateUserModal minLength

# The handful of passwords that show up in every breach list at these lengths.
_TOO_COMMON = frozenset(
    {
        "password",
        "password1",
        "passw0rd",
        "12345678",
        "123456789",
        "1234567890",
        "qwertyui",
        "qwerty123",
        "11111111",
        "iloveyou",
        "letmein1",
        "welcome1",
        "abcd1234",
    },
)


def validate_new_password(password: str, username: str | None = None) -> None:
    """Raise HTTP 400 with a user-facing reason if `password` may not be set."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
        )
    if username and password.lower() == username.lower():
        raise HTTPException(status_code=400, detail="Password must not be the username")
    if password.lower() in _TOO_COMMON:
        raise HTTPException(status_code=400, detail="That password is too common; choose another")
