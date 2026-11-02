import asyncio
import os

import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL", "postgres://freedomridge:password@localhost:5432/freedomridge")


async def init_db():
    print(f"Connecting to {DATABASE_URL}...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        print(f"Failed to connect: {e}")
        return

    print("Dropping existing tables...")
    await conn.execute("DROP TABLE IF EXISTS embeddings CASCADE;")
    await conn.execute("DROP TABLE IF EXISTS samples CASCADE;")
    await conn.execute("DROP TABLE IF EXISTS dataset_fields CASCADE;")
    await conn.execute("DROP TABLE IF EXISTS datasets CASCADE;")

    print("Reading schema.sql...")
    schema_path = os.path.join(os.path.dirname(__file__), "db", "schema.sql")
    with open(schema_path, "r") as f:
        schema_sql = f.read()

    print("Executing schema...")
    # Remove comment lines first
    lines = []
    for line in schema_sql.split('\n'):
        # Remove comment-only lines
        if line.strip().startswith('--') and 'CREATE' not in line:
            continue
        lines.append(line)

    cleaned_sql = '\n'.join(lines)

    # Now split by semicolon
    statements = [s.strip() for s in cleaned_sql.split(';') if s.strip()]

    for i, stmt in enumerate(statements):
        if not stmt or stmt.strip().startswith('--'):
            continue
        print(f"[{i + 1}/{len(statements)}] Executing: {stmt.split()[0:3]}...")
        try:
            await conn.execute(stmt)
            print("  Success")
        except Exception as e:
            print(f"  ERROR: {e}")
            raise

    print("\nDatabase initialized successfully!")

    # Verify tables were created
    tables = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
    print(f"\nTables created: {[r['tablename'] for r in tables]}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(init_db())
