import argparse
import asyncio
import os

import asyncpg

from backend.auth_policy import validate_new_password
from backend.passwords import hash_password
from backend.security import production

DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://freedomridge:password@localhost:5432/freedomridge')


async def reset_admin(password: str | None = None):
    password = password or os.getenv('FRIDGE_ADMIN_PASSWORD')
    supplied = bool(password)
    if not password:
        if production():
            raise RuntimeError('Set FRIDGE_ADMIN_PASSWORD or pass --password to reset the production admin')
        password = 'admin123'
    if production() and password.casefold() == 'admin123':
        raise RuntimeError('FRIDGE_ADMIN_PASSWORD must not be the seeded default password')
    validate_new_password(password, 'admin')
    hashed = await hash_password(password)
    connection = await asyncpg.connect(DATABASE_URL)
    try:
        async with connection.transaction():
            user_id = await connection.fetchval(
                """INSERT INTO users (username,email,role,password_hash,must_change_password,is_active)
                   VALUES ('admin','admin@freedomridge.local','admin',$1,$2,true)
                   ON CONFLICT (username) DO UPDATE SET password_hash=$1,must_change_password=$2,
                       is_active=true,role='admin'
                   RETURNING id""",
                hashed,
                not supplied,
            )
            await connection.execute('DELETE FROM sessions WHERE user_id=$1', user_id)
        print('Admin password reset successfully')
    finally:
        await connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Reset admin using FRIDGE_ADMIN_PASSWORD or --password')
    parser.add_argument('--password', help='New password (prefer FRIDGE_ADMIN_PASSWORD to avoid shell history)')
    asyncio.run(reset_admin(parser.parse_args().password))
