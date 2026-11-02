"""Password work runs outside the API event loop, including salt generation."""

import asyncio

import bcrypt

DEMO_PASSWORD = '!demo-account-no-password'
DUMMY_HASH = '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5GyYzpLdOvCe2'


async def hash_password(password: str) -> str:
    def hash_with_salt():
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    return await asyncio.to_thread(hash_with_salt)


async def verify_password(password: str, hashed: str | None) -> bool:
    try:
        if not hashed or hashed == DEMO_PASSWORD:
            await asyncio.to_thread(bcrypt.checkpw, password.encode(), DUMMY_HASH.encode())
            return False
        return await asyncio.to_thread(bcrypt.checkpw, password.encode(), hashed.encode())
    except ValueError:
        return False
