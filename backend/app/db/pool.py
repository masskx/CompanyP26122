"""Shared psycopg 3 connection pool for openGauss (opened lazily, dict rows)."""

from typing import Any


class PgPool:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 5) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        self._pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False,
                                         kwargs={"row_factory": dict_row})
        self._opened = False

    async def connection(self) -> Any:
        if not self._opened:
            await self._pool.open(wait=False)
            self._opened = True
        return self._pool.connection()

    async def close(self) -> None:
        if self._opened:
            await self._pool.close()
            self._opened = False
