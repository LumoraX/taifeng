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

from taifeng.llm.providers.codex import CodexResponsesClient  # noqa: E402
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


@pytest.mark.parametrize("protocol", [None, "", "responses"])
def test_codex_protocol_selects_independent_responses_client(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str | None,
) -> None:
    """Codex 缺省或显式协议都只能收敛到独立 Responses client。"""
    monkeypatch.setenv("LLM_BOOTSTRAP_PROVIDER", "codex")
    if protocol is not None:
        monkeypatch.setenv("LLM_BOOTSTRAP_PROTOCOL", protocol)
    monkeypatch.setenv("LLM_BOOTSTRAP_API_KEY", "sk-test-placeholder")
    monkeypatch.setenv("LLM_BOOTSTRAP_BASE_URL", "https://proxy.example/v1/")

    client, meta = build_model_client()

    assert isinstance(client, CodexResponsesClient)
    assert client.capabilities.provider == "codex"
    assert client.capabilities.protocol == "responses"
    assert meta["provider"] == "codex"
    assert meta["protocol"] == "responses"
    assert meta["dialect"] == "codex-responses-v1"
    assert meta["model"] == "gpt-5.6-luna"
    assert meta["base_url"] == "https://proxy.example/v1"
    assert meta["api_key_tail"] == "***lder"


@pytest.mark.parametrize("protocol", ["chat", "response", "codex"])
def test_codex_rejects_non_responses_protocol(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    """Codex 不得回退 Chat，也不得容忍近似拼写。"""
    monkeypatch.setenv("LLM_BOOTSTRAP_PROVIDER", "codex")
    monkeypatch.setenv("LLM_BOOTSTRAP_PROTOCOL", protocol)

    with pytest.raises(ProviderBootstrapError, match="Codex protocol"):
        resolve_bootstrap_env()


def test_codex_requires_explicit_unified_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex 不得借用 OpenAI legacy URL 或内置代理域名。"""
    monkeypatch.setenv("LLM_BOOTSTRAP_PROVIDER", "codex")
    monkeypatch.setenv("LLM_BOOTSTRAP_OPENAI_BASE_URL", "https://legacy.example/v1")

    with pytest.raises(ProviderBootstrapError, match="base URL is required"):
        resolve_bootstrap_env()


@pytest.mark.parametrize(
    "base_url",
    [
        "proxy.example/v1",
        "ftp://proxy.example/v1",
        "https:///v1",
        "https://user:pass@proxy.example/v1",
        "https://proxy.example/v1?mode=codex",
        "https://proxy.example/v1#fragment",
        "https://proxy.example/v1/responses/",
    ],
)
def test_codex_rejects_non_api_root_base_url(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    """Codex endpoint 必须由合法 API root 唯一拼出。"""
    monkeypatch.setenv("LLM_BOOTSTRAP_PROVIDER", "codex")
    monkeypatch.setenv("LLM_BOOTSTRAP_BASE_URL", base_url)

    with pytest.raises(ProviderBootstrapError, match="Codex base URL"):
        resolve_bootstrap_env()


def test_codex_ignores_all_legacy_openai_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧 OpenAI key/model/base URL 不得流入 Codex 身份。"""
    monkeypatch.setenv("LLM_BOOTSTRAP_PROVIDER", "codex")
    monkeypatch.setenv("LLM_BOOTSTRAP_BASE_URL", "http://localhost:8080/v1")
    monkeypatch.setenv("LLM_BOOTSTRAP_OPENAI_API_KEY", "legacy-secret")
    monkeypatch.setenv("LLM_BOOTSTRAP_OPENAI_MODEL", "legacy-model")
    monkeypatch.setenv("LLM_BOOTSTRAP_OPENAI_BASE_URL", "https://legacy.example/v1")

    resolved = resolve_bootstrap_env()

    assert resolved == (
        "codex",
        "responses",
        None,
        "gpt-5.6-luna",
        "http://localhost:8080/v1",
    )
    with pytest.raises(ProviderBootstrapError, match="missing api_key"):
        build_model_client()


def test_codex_optional_key_still_validates_url_without_key_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """允许空 key 只延迟认证失败，不得跳过 URL 校验或泄露 tail。"""
    monkeypatch.setenv("LLM_BOOTSTRAP_PROVIDER", "codex")
    monkeypatch.setenv("LLM_BOOTSTRAP_BASE_URL", "https://proxy.example/v1/")

    client, meta = build_model_client(require_api_key=False)

    assert isinstance(client, CodexResponsesClient)
    assert meta["base_url"] == "https://proxy.example/v1"
    assert "api_key_tail" not in meta


def test_env_example_documents_openai_and_independent_codex_choice() -> None:
    """示例必须同时给出两个显式 provider 配置块，且 Codex 使用统一变量。"""
    content = (EXAMPLES_DIR.parent / ".env.example").read_text(encoding="utf-8")

    assert "LLM_BOOTSTRAP_PROVIDER=openai" in content
    assert "# LLM_BOOTSTRAP_PROVIDER=codex" in content
    assert "# LLM_BOOTSTRAP_PROTOCOL=responses" in content
    assert "# LLM_BOOTSTRAP_MODEL=gpt-5.6-luna" in content
    assert "# LLM_BOOTSTRAP_BASE_URL=https://your-codex-proxy.example/v1" in content
    assert "LLM_BOOTSTRAP_CODEX_" not in content
