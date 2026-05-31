"""ThreadDirectory Redis 实现骨架 —— 业务侧复制 + 修改连接字符串即可投产。

**本文件不在 src/ 内** —— taifeng src 不依赖任何 Redis 客户端（R1 红线）。
业务侧选 ``redis-py`` / ``aioredis`` / 国产 Tair 任一，自行管理连接池 / pool / retry / 多区域路由。

数据布局建议（按业务调整）：

- ``HSET taifeng:thread:<thread_id>`` —— hash 存元数据字段
- ``ZADD taifeng:threads:updated_at <updated_at> <thread_id>`` —— sorted set 用于按 updated_at 倒序 + 分页

依赖：

    pip install redis>=5.0

运行：

    PYTHONPATH=src python examples/persistence/redis_thread_directory.py
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

from taifeng.conversation import (
    ThreadDirectory,
    ThreadFilter,
    ThreadMetadata,
    ThreadNotFoundError,
    ThreadPage,
)

if TYPE_CHECKING:
    # 业务侧 import；本骨架不要求 import 通过（demo 不真连）
    import redis.asyncio as redis  # type: ignore[import-not-found]


class RedisThreadDirectory:
    """ThreadDirectory 协议的 Redis 实现示例骨架。

    完整实现请参考下方 TODO 注释；以下方法签名与协议契约一致。
    """

    def __init__(self, url: str, *, prefix: str = "taifeng") -> None:
        # 真实业务建议接受已构造好的 client（带 pool / retry / tracing）：
        #
        #     RedisThreadDirectory(client=my_company_redis_pool, prefix="...")
        #
        # 这里 demo 用 from_url 起一个 client
        import redis.asyncio as redis_lib

        self._r: "redis.Redis" = redis_lib.from_url(url, decode_responses=True)
        self._prefix = prefix

    def _thread_key(self, thread_id: str) -> str:
        return f"{self._prefix}:thread:{thread_id}"

    def _index_key(self) -> str:
        return f"{self._prefix}:threads:updated_at"

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

        # TODO: 解析 cursor (base64 {updated_at, thread_id})
        cursor_max = float("inf")
        if cursor is not None:
            try:
                payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
                cursor_max = float(payload["updated_at"])
            except Exception:
                # spec 要求损坏 cursor 重置 + 发 directory_cursor_reset 事件
                # （此 demo 省略 sink 注入 + 事件发射）
                cursor_max = float("inf")

        # ZREVRANGEBYSCORE：按 updated_at 倒序拉取候选
        ids = await self._r.zrevrangebyscore(
            self._index_key(),
            max=f"({cursor_max}" if cursor is not None else "+inf",
            min="-inf",
            start=0,
            num=limit * 2,  # 多取一些应对 filter / orphan 过滤
        )

        items: list[ThreadMetadata] = []
        for tid in ids:
            raw = await self._r.hgetall(self._thread_key(tid))
            if not raw:
                continue
            meta = self._decode(raw)
            # 过滤 (AND 语义)
            if filter is not None:
                if filter.entry_skill_id and meta.entry_skill_id != filter.entry_skill_id:
                    continue
                if filter.source and meta.source != filter.source:
                    continue
                if filter.tag and filter.tag not in meta.tags:
                    continue
                if filter.created_after and meta.created_at <= filter.created_after:
                    continue
                if filter.created_before and meta.created_at >= filter.created_before:
                    continue
            items.append(meta)
            if len(items) >= limit:
                break

        next_cursor: str | None = None
        if len(items) == limit:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps({"updated_at": last.updated_at, "thread_id": last.thread_id}).encode("utf-8")
            ).decode("ascii")
        return ThreadPage(items=items, next_cursor=next_cursor)

    async def get_metadata(self, thread_id: str) -> ThreadMetadata | None:
        raw = await self._r.hgetall(self._thread_key(thread_id))
        if not raw:
            return None
        return self._decode(raw)

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
        await self._r.hset(self._thread_key(meta.thread_id), mapping=self._encode(meta))
        await self._r.zadd(self._index_key(), {meta.thread_id: meta.updated_at})

    # ----------------------------------------------------------------
    # 编解码
    # ----------------------------------------------------------------

    @staticmethod
    def _encode(meta: ThreadMetadata) -> dict[str, str]:
        return {
            "thread_id": meta.thread_id,
            "created_at": str(meta.created_at),
            "updated_at": str(meta.updated_at),
            "entry_skill_id": meta.entry_skill_id,
            "source": meta.source,
            "tags": json.dumps(list(meta.tags)),
            "extra": json.dumps(meta.extra),
        }

    @staticmethod
    def _decode(raw: dict[str, str]) -> ThreadMetadata:
        return ThreadMetadata(
            thread_id=raw["thread_id"],
            created_at=float(raw["created_at"]),
            updated_at=float(raw["updated_at"]),
            entry_skill_id=raw["entry_skill_id"],
            source=raw.get("source", "user"),
            tags=tuple(json.loads(raw.get("tags", "[]"))),
            extra=json.loads(raw.get("extra", "{}")),
        )


# ====================================================================
# 用法示例（不真连 Redis）
# ====================================================================


def main_skeleton() -> None:
    """演示如何注入到 EnginePool（不真跑）。"""
    print(
        """
    # 业务侧代码：
    import taifeng
    from examples.redis_thread_directory import RedisThreadDirectory

    directory = RedisThreadDirectory(url="redis://prod-cluster:6379")
    engine_pool = await taifeng.EnginePool.create(
        skills_dir=...,
        storage_dir=...,                  # JSONL 主存仍在本地
        model_client=...,
        thread_directory=directory,       # 元数据走 Redis
    )

    # 元数据查询走 Redis；消息体 (load_history) 仍读本地 JSONL 文件。
    # 见 docs/usage.md「业务侧替换 ThreadDirectory」节获取完整教程。
    """
    )


if __name__ == "__main__":
    main_skeleton()
