import asyncio
import os

import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL", "postgres://freedomridge:password@localhost:5432/freedomridge")


async def check_schema():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("""
            SELECT column_name, data_type, udt_name
            FROM information_schema.columns
            WHERE table_name = 'dataset_field_stats'
        """)
        print("Schema for dataset_field_stats:")
        for r in rows:
            print(f" - {r['column_name']}: {r['data_type']} ({r['udt_name']})")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(check_schema())
