"""ThreadDirectory PostgreSQL 实现骨架 —— 业务侧复制 + 填 DSN 即可投产。

**本文件不在 src/ 内** —— taifeng src 不依赖任何 PG 客户端（R1 红线）。
业务侧选 ``asyncpg`` / ``psycopg`` / SQLAlchemy 任一；本骨架用 asyncpg 范式示例。

表结构（asyncpg-friendly 简化版，复用 SqliteThreadDirectory 字段）：

    CREATE TABLE taifeng_thread (
        thread_id      TEXT PRIMARY KEY,
        created_at     DOUBLE PRECISION NOT NULL,
        updated_at     DOUBLE PRECISION NOT NULL,
        entry_skill_id TEXT NOT NULL,
        source         TEXT NOT NULL,
        tags           JSONB NOT NULL DEFAULT '[]'::jsonb,
        extra          JSONB NOT NULL DEFAULT '{}'::jsonb
    );
    CREATE INDEX idx_taifeng_thread_updated_at ON taifeng_thread(updated_at DESC, thread_id DESC);
    CREATE INDEX idx_taifeng_thread_entry_skill ON taifeng_thread(entry_skill_id);
    CREATE INDEX idx_taifeng_thread_tags_gin ON taifeng_thread USING GIN(tags);

依赖：

    pip install asyncpg>=0.29

运行：

    PYTHONPATH=src python examples/persistence/postgres_thread_directory.py
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

from taifeng.conversation import (
    ThreadFilter,
    ThreadMetadata,
    ThreadNotFoundError,
    ThreadPage,
)

if TYPE_CHECKING:
    # 业务侧 import；本骨架不要求 import 通过
    import asyncpg  # type: ignore[import-not-found]


class PostgresThreadDirectory:
    """ThreadDirectory 协议的 asyncpg 实现示例骨架。

    生产环境应复用业务侧已有的 connection pool（带 retry / circuit breaker /
    多区域路由 / OpenTelemetry tracing）。本骨架仅演示协议方法体如何映射 SQL。
    """

    def __init__(self, dsn: str, *, table_name: str = "taifeng_thread") -> None:
        import asyncpg as _asyncpg

        self._dsn = dsn
        self._table = table_name
        self._pool: "asyncpg.Pool | None" = None
        self._lib = _asyncpg

    async def _ensure_pool(self) -> "asyncpg.Pool":
        if self._pool is None:
            # 生产侧建议外部注入已配好的 pool；这里 demo 直建
            self._pool = await self._lib.create_pool(self._dsn, min_size=1, max_size=10)
        return self._pool

    # ----------------------------------------------------------------
    # ThreadDirectory protocol
    # ----------------------------------------------------------------

    async def list_threads(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        filter: ThreadFilter | None = None,
    ) -> ThreadPage:
        if limit < 1 or limit > 1000:
            raise ValueError(f"limit must be in [1, 1000]; got {limit}")

        cursor_pair: tuple[float, str] | None = None
        if cursor is not None:
            try:
                payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
                cursor_pair = (float(payload["updated_at"]), str(payload["thread_id"]))
            except Exception:
                cursor_pair = None  # spec 损坏 cursor 重置

        where: list[str] = []
        params: list[Any] = []
        if filter is not None:
            if filter.entry_skill_id is not None:
                where.append(f"entry_skill_id = ${len(params) + 1}")
                params.append(filter.entry_skill_id)
            if filter.source is not None:
                where.append(f"source = ${len(params) + 1}")
                params.append(filter.source)
            if filter.created_after is not None:
                where.append(f"created_at > ${len(params) + 1}")
                params.append(filter.created_after)
            if filter.created_before is not None:
                where.append(f"created_at < ${len(params) + 1}")
                params.append(filter.created_before)
            if filter.tag is not None:
                where.append(f"tags @> ${len(params) + 1}::jsonb")
                params.append(json.dumps([filter.tag]))
        if cursor_pair is not None:
            where.append(
                f"(updated_at < ${len(params) + 1} OR "
                f"(updated_at = ${len(params) + 1} AND thread_id < ${len(params) + 2}))"
            )
            params.extend([cursor_pair[0], cursor_pair[1]])

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        sql = (
            f"SELECT thread_id, created_at, updated_at, entry_skill_id, source, tags, extra "
            f"FROM {self._table} {where_sql} "
            f"ORDER BY updated_at DESC, thread_id DESC LIMIT ${len(params) + 1}"
        )
        params.append(limit)

        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        items = [self._row_to_metadata(dict(r)) for r in rows]
        next_cursor: str | None = None
        if len(items) == limit:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps({"updated_at": last.updated_at, "thread_id": last.thread_id}).encode("utf-8")
            ).decode("ascii")
        return ThreadPage(items=items, next_cursor=next_cursor)

    async def get_metadata(self, thread_id: str) -> ThreadMetadata | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT thread_id, created_at, updated_at, entry_skill_id, source, tags, extra "
                f"FROM {self._table} WHERE thread_id = $1",
                thread_id,
            )
        return self._row_to_metadata(dict(row)) if row else None

    async def update_metadata(self, thread_id: str, patch: dict[str, Any]) -> None:
        existing = await self.get_metadata(thread_id)
        if existing is None:
            raise ThreadNotFoundError(thread_id)
        import time as _t
        merged = ThreadMetadata(
            thread_id=existing.thread_id,
            created_at=existing.created_at,
            updated_at=_t.time(),
            entry_skill_id=patch.get("entry_skill_id", existing.entry_skill_id),
            source=patch.get("source", existing.source),
            tags=tuple(patch.get("tags", existing.tags)),
            extra=dict(patch.get("extra", existing.extra)),
        )
        await self.upsert_metadata(merged)

    async def upsert_metadata(self, meta: ThreadMetadata) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {self._table} "
                "(thread_id, created_at, updated_at, entry_skill_id, source, tags, extra) "
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb) "
                "ON CONFLICT (thread_id) DO UPDATE SET "
                "created_at = EXCLUDED.created_at, "
                "updated_at = EXCLUDED.updated_at, "
                "entry_skill_id = EXCLUDED.entry_skill_id, "
                "source = EXCLUDED.source, "
                "tags = EXCLUDED.tags, "
                "extra = EXCLUDED.extra",
                meta.thread_id,
                meta.created_at,
                meta.updated_at,
                meta.entry_skill_id,
                meta.source,
                json.dumps(list(meta.tags)),
                json.dumps(meta.extra),
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ----------------------------------------------------------------
    @staticmethod
    def _row_to_metadata(row: dict[str, Any]) -> ThreadMetadata:
        tags_val = row.get("tags", [])
        extra_val = row.get("extra", {})
        # asyncpg 默认把 jsonb 解码为 Python 对象（list / dict）
        if isinstance(tags_val, str):
            tags_val = json.loads(tags_val)
        if isinstance(extra_val, str):
            extra_val = json.loads(extra_val)
        return ThreadMetadata(
            thread_id=row["thread_id"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            entry_skill_id=row["entry_skill_id"],
            source=row.get("source", "user"),
            tags=tuple(tags_val),
            extra=dict(extra_val),
        )


def main_skeleton() -> None:
    """演示如何注入到 EnginePool（不真连 PG）。"""
    print(
        """
    # 业务侧代码：
    import taifeng
    from examples.postgres_thread_directory import PostgresThreadDirectory

    directory = PostgresThreadDirectory(dsn="postgresql://user:pass@host:5432/db")
    engine_pool = await taifeng.EnginePool.create(
        skills_dir=...,
        storage_dir=...,                  # JSONL 主存仍在本地
        model_client=...,
        thread_directory=directory,       # 元数据走 PostgreSQL
    )

    # 业务侧已有 connection pool 的情况下：自行重构构造接受外部 pool。
    """
    )


if __name__ == "__main__":
    main_skeleton()
