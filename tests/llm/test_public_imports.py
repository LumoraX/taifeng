"""LLM provider 公共导入稳定性测试。"""

from __future__ import annotations

from importlib.metadata import version

import taifeng
from taifeng.llm import CodexResponsesClient as LlmCodexResponsesClient
from taifeng.llm.providers import CodexResponsesClient as ProviderCodexResponsesClient
from taifeng.llm.providers.codex import CodexResponsesClient


def test_runtime_version_matches_distribution_metadata() -> None:
    """运行时版本必须与安装元数据一致。"""
    assert taifeng.__version__ == version("taifeng")


def test_codex_responses_client_has_one_public_type_identity() -> None:
    """根包、llm、providers 与 provider 子包必须导出同一 class。"""
    assert taifeng.CodexResponsesClient is CodexResponsesClient
    assert LlmCodexResponsesClient is CodexResponsesClient
    assert ProviderCodexResponsesClient is CodexResponsesClient
