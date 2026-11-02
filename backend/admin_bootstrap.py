"""Refuse the public seeded credential before accepting any production traffic."""

import os

from fastapi import HTTPException

from .auth_policy import validate_new_password
from .passwords import hash_password, verify_password
from .security import production


async def bootstrap_admin(database):
    if not production():
        return
    async with database.pool.acquire() as connection, connection.transaction():
        # Serialize worker startup so only one rotates the seeded credential.
        admin = await connection.fetchrow("SELECT id,password_hash FROM users WHERE username='admin' FOR UPDATE")
        if not admin or not await verify_password('admin123', admin['password_hash']):
            return
        password = os.getenv('FRIDGE_ADMIN_PASSWORD')
        if not password or password.casefold() == 'admin123':
            raise RuntimeError('Set FRIDGE_ADMIN_PASSWORD to a non-default password before starting production')
        try:
            validate_new_password(password, 'admin')
        except HTTPException as error:
            raise RuntimeError(f'Invalid FRIDGE_ADMIN_PASSWORD: {error.detail}') from None
        await connection.execute(
            'UPDATE users SET password_hash=$1, must_change_password=false WHERE id=$2',
            await hash_password(password),
            admin['id'],
        )
        await connection.execute('DELETE FROM sessions WHERE user_id=$1', admin['id'])
