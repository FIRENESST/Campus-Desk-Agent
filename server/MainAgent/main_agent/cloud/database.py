import asyncio
import logging

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from .errors import ServiceError


class Database:
    """Bounded synchronous pool behind threads; works on Windows Proactor too."""

    def __init__(self, dsn: str, max_size: int = 12):
        self.pool = ConnectionPool(
            dsn, min_size=1, max_size=max_size, timeout=10, open=False,
            kwargs={"autocommit": True, "row_factory": dict_row, "connect_timeout": 5,
                    "options": "-c statement_timeout=15000 -c lock_timeout=5000 -c timezone=UTC"},
        )
        # The library must never include connection strings in application logs.
        logging.getLogger("psycopg.pool").setLevel(logging.CRITICAL)

    async def open(self):
        await asyncio.to_thread(self.pool.open, wait=True, timeout=15)

    async def close(self):
        await asyncio.to_thread(self.pool.close)

    async def run(self, operation):
        def transaction():
            try:
                with self.pool.connection() as connection, connection.transaction():
                    return operation(connection)
            except psycopg.errors.UniqueViolation:
                raise ServiceError(409, "conflict", "资源冲突或已有活跃任务") from None
            except (psycopg.Error, PoolTimeout):
                raise ServiceError(503, "database_unavailable", "数据库暂不可用") from None

        return await asyncio.to_thread(transaction)
