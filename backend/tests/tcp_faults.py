"""A local PostgreSQL proxy that can silently drop an individual owned connection."""

import asyncio
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import asyncpg

from backend.database import init_connection


@asynccontextmanager
async def blackhole_pool(database):
    dsn = os.environ['FRIDGE_TEST_POSTGRES_DSN']
    address = urlsplit(dsn)
    namespace = await database.fetchval('SELECT current_schema()')
    flows, connections = {}, set()

    async def forward(reader, writer, dropped):
        while chunk := await reader.read(65536):
            if not dropped.is_set():
                writer.write(chunk)
                await writer.drain()

    async def accept(reader, writer):
        task = asyncio.current_task()
        assert task is not None
        connections.add(task)
        upstream, pumps = None, []
        try:
            upstream_reader, upstream = await asyncio.open_connection(address.hostname, address.port or 5432)
            port = writer.get_extra_info('peername')[1]
            dropped = asyncio.Event()
            flows[port] = dropped
            pumps = [
                asyncio.create_task(forward(reader, upstream, dropped)),
                asyncio.create_task(forward(upstream_reader, writer, dropped)),
            ]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            writer.close()
            if upstream is not None:
                upstream.close()
                await upstream.wait_closed()
            await writer.wait_closed()
            connections.discard(task)

    proxy = await asyncio.start_server(accept, '127.0.0.1', 0)
    pool = await asyncpg.create_pool(
        dsn,
        host='127.0.0.1',
        port=proxy.sockets[0].getsockname()[1],
        ssl=False,
        min_size=1,
        max_size=2,
        init=init_connection,
        server_settings={'search_path': namespace + ',public'},
    )
    try:
        yield pool, flows
    finally:
        pool.terminate()
        proxy.close()
        for task in list(connections):
            task.cancel()
        await asyncio.gather(*list(connections), return_exceptions=True)
        await proxy.wait_closed()
