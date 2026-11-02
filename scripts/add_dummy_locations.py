import asyncio
import random

import asyncpg

DATABASE_URL = "postgres://freedomridge:password@localhost:5432/freedomridge"

# Clusters: (lat, lon)
CLUSTERS = [
    (51.5074, -0.1278),  # London
    (40.7128, -74.0060),  # New York
    (35.6762, 139.6503),  # Tokyo
    (48.8566, 2.3522),  # Paris
    (-33.8688, 151.2093),  # Sydney
]


async def add_dummy_locations():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Get all sample IDs
        rows = await conn.fetch("SELECT id FROM samples")
        print(f"Found {len(rows)} samples")

        for row in rows:
            sample_id = row['id']

            # Pick a random cluster
            center_lat, center_lon = random.choice(CLUSTERS)

            # Add random offset (approx 10km radius)
            # 1 deg lat ~ 111km. 0.1 deg ~ 11km.
            lat = center_lat + random.uniform(-0.1, 0.1)
            lon = center_lon + random.uniform(-0.1, 0.1)

            heading = random.uniform(0, 360)

            # Update
            # ST_SetSRID(ST_MakePoint(lon, lat), 4326)
            # Note: PostGIS uses (lon, lat) order for Point
            await conn.execute(
                """
                UPDATE samples
                SET location = ST_SetSRID(ST_MakePoint($1, $2), 4326),
                    heading = $3
                WHERE id = $4
                """,
                lon,
                lat,
                heading,
                sample_id,
            )

        print("Updated all samples with dummy locations")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(add_dummy_locations())
