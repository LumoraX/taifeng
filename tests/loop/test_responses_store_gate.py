"""OpenAI Responses 对 terminal 原子持久化能力的构造期门禁。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.errors import UnsupportedPersistenceCapabilityError
from taifeng.llm.providers import SimClient
from taifeng.loop.pool import EnginePool
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry

if TYPE_CHECKING:
    from pathlib import Path


_RESPONSES_CAPABILITIES = ModelCapabilities(
    input_modalities=frozenset({"text", "image"}),
    provider="openai",
    protocol="responses",
    accepts_provider_state=True,
)


class _NonAtomicStore:
    """故意只实现 legacy MessageStore 的测试替身。"""

    async def append(self, item: object) -> None:
        """忽略单项。"""

    async def append_batch(self, items: object) -> None:
        """忽略普通 batch。"""


@pytest.mark.asyncio
async def test_responses_rejects_non_atomic_custom_store(skills_dir: Path) -> None:
    """非 audit Responses 必须在 Pool 构造期拒绝不具备原子能力的 store。"""
    registry = await FilesystemSkillRegistry.load(skills_dir)
    client = SimClient(turns=[], capabilities=_RESPONSES_CAPABILITIES)

    with pytest.raises(UnsupportedPersistenceCapabilityError):
        EnginePool(
            skill_registry=registry,
            model_client=client,
            store=_NonAtomicStore(),  # type: ignore[arg-type]
            tool_registry=ToolRegistry(),
            compressors=[],
        )


@pytest.mark.asyncio
async def test_responses_accepts_default_jsonl_atomic_store(
    skills_dir: Path, threads_dir: Path
) -> None:
    """公共工厂的默认 JSONL 包装必须透传原子 batch capability。"""
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=SimClient(turns=[], capabilities=_RESPONSES_CAPABILITIES),
        compressors=[],
    )

    await pool.close()
