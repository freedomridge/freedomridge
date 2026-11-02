import asyncio

from backend.database import Database


async def main():
    db = Database()
    await db.connect()
    try:
        datasets = await db.fetch("SELECT id, name FROM datasets ORDER BY id")
        print("Datasets found:")
        for d in datasets:
            print(f"ID: {d['id']}, Name: {d['name']}")
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
