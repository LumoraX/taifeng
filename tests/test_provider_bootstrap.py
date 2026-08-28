"""examples 共享 provider bootstrap 的环境变量契约测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
if str(EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLES_DIR))

from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    resolve_bootstrap_env,
)

from taifeng.llm.providers.openai import (  # noqa: E402
    OpenAIChatClient,
    OpenAIResponsesClient,
)

BOOTSTRAP_ENV_KEYS = (
    "LLM_BOOTSTRAP_PROVIDER",
    "LLM_BOOTSTRAP_PROTOCOL",
    "LLM_BOOTSTRAP_API_KEY",
    "LLM_BOOTSTRAP_MODEL",
    "LLM_BOOTSTRAP_BASE_URL",
    "LLM_BOOTSTRAP_OPENAI_API_KEY",
    "LLM_BOOTSTRAP_OPENAI_MODEL",
    "LLM_BOOTSTRAP_OPENAI_BASE_URL",
)


@pytest.fixture(autouse=True)
def _clean_bootstrap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离宿主 shell 与其他测试留下的 bootstrap 环境变量。"""
    for key in BOOTSTRAP_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize(
    ("protocol", "client_type"),
    [
        ("chat", OpenAIChatClient),
        ("responses", OpenAIResponsesClient),
    ],
)
def test_openai_protocol_selects_official_client(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    client_type: type[OpenAIChatClient] | type[OpenAIResponsesClient],
) -> None:
    """OpenAI 子协议必须选择对应的官方 adapter。"""
    monkeypatch.setenv("LLM_BOOTSTRAP_PROVIDER", "openai")
    monkeypatch.setenv("LLM_BOOTSTRAP_PROTOCOL", protocol)
    monkeypatch.setenv("LLM_BOOTSTRAP_API_KEY", "sk-test-placeholder")
    monkeypatch.setenv("LLM_BOOTSTRAP_MODEL", "gpt-5.6")

    client, meta = build_model_client()

    assert isinstance(client, client_type)
    assert client.capabilities.protocol == protocol
    assert meta["provider"] == "openai"
    assert meta["protocol"] == protocol


def test_openai_protocol_defaults_to_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置 OpenAI 子协议时维持 Chat 入口的低惊讶默认值。"""
    monkeypatch.setenv("LLM_BOOTSTRAP_PROVIDER", "openai")
    monkeypatch.setenv("LLM_BOOTSTRAP_API_KEY", "sk-test-placeholder")

    client, meta = build_model_client()

    assert isinstance(client, OpenAIChatClient)
    assert meta["protocol"] == "chat"


def test_openai_rejects_unknown_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI protocol 拼写错误必须 fail closed。"""
    monkeypatch.setenv("LLM_BOOTSTRAP_PROVIDER", "openai")
    monkeypatch.setenv("LLM_BOOTSTRAP_PROTOCOL", "response")

    with pytest.raises(ProviderBootstrapError, match="expected 'chat' or 'responses'"):
        resolve_bootstrap_env()


def test_non_openai_rejects_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """其他 provider 不得静默吞掉仅属于 OpenAI 的协议配置。"""
    monkeypatch.setenv("LLM_BOOTSTRAP_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_BOOTSTRAP_PROTOCOL", "responses")

    with pytest.raises(
        ProviderBootstrapError,
        match="LLM_BOOTSTRAP_PROTOCOL is only valid for provider='openai'",
    ):
        resolve_bootstrap_env()
