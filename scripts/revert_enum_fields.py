import asyncio
import os

import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL", "postgres://freedomridge:password@localhost:5432/freedomridge")


async def revert_enums():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Revert 'enum' fields back to 'string' if they are not 'media_type' or 'tags'
        # (generic metadata is treated as string)
        print("Reverting fields to 'string'...")
        result = await conn.execute("""
            UPDATE dataset_field_stats
            SET field_type = 'string'
            WHERE field_type = 'enum'
              AND field_name NOT IN ('media_type', 'tags')
        """)
        print(f"Revert complete: {result}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(revert_enums())
