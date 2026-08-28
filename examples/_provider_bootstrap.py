"""examples/ 共享的 LLM provider bootstrap —— 让所有 demo 走统一 env 形态。

各 demo 不再各自复制 `_load_env` + provider 选择逻辑；改为：

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # examples/
    from _provider_bootstrap import (
        load_dotenv_files,
        build_model_client,
        ProviderBootstrapError,
    )

    load_dotenv_files()
    client, meta = build_model_client()
    print(f"[setup] {meta['provider']}/{meta['model']}")

支持两套环境变量命名：

    # 新形态（推荐）
    LLM_BOOTSTRAP_PROVIDER=openai|anthropic|gemini|deepseek
    LLM_BOOTSTRAP_PROTOCOL=chat|responses                  # 仅 openai；默认 chat
    LLM_BOOTSTRAP_API_KEY=...
    LLM_BOOTSTRAP_MODEL=...                              # 可省，按 provider 默认
    LLM_BOOTSTRAP_BASE_URL=...                           # 可省，OpenAI 端点或兼容网关

    # 旧形态（向后兼容；隐式 provider=openai）
    LLM_BOOTSTRAP_OPENAI_API_KEY=...
    LLM_BOOTSTRAP_OPENAI_MODEL=...
    LLM_BOOTSTRAP_OPENAI_BASE_URL=...

各 provider 默认模型：
    openai   → gpt-4o-mini                        （默认走 OpenAIChatClient）
    anthropic→ claude-haiku-4-5-20251001          （走 AnthropicClient）
    gemini   → gemini-2.0-flash-exp               （走 GeminiClient）
    deepseek → deepseek-chat                      （走 DeepSeekClient）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from taifeng.llm.providers import (
    AnthropicClient,
    DeepSeekClient,
    GeminiClient,
)
from taifeng.llm.providers.openai import OpenAIChatClient, OpenAIResponsesClient

if TYPE_CHECKING:
    from taifeng.llm.client import ModelClient


class ProviderBootstrapError(RuntimeError):
    """env 解析或 client 构造失败。"""


_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
    "gemini": "gemini-2.0-flash-exp",
    "deepseek": "deepseek-chat",
}


# Provider × model 名片段 → context window（tokens）的查找表。按子串前缀匹配
# 即可（model id 一般含版本号后缀，如 ``claude-haiku-4-5-20251001``）。表里没
# 列出的 fallback 到 provider 默认值；provider 也没列就回 ``None``，前端不显示
# 百分比、只显示绝对 tokens 数。
#
# 数字来源（截至 2026 上半年公开资料；过时时由业务侧覆盖 env / 自行 PR）：
#   - openai gpt-4o / gpt-4-turbo / gpt-4o-mini → 128k
#   - openai gpt-5 / gpt-5-mini                  → 400k（公开宣称）
#   - openai gpt-3.5-turbo                       → 16k
#   - anthropic claude 4.x / 3.5                 → 200k（默认）
#   - anthropic claude-*-[1m] / 1m 后缀变体      → 1000k
#   - gemini 1.5/2.0/2.5 flash + pro             → 1000k–2000k
#   - deepseek-chat / deepseek-reasoner          → 64k
_PROVIDER_DEFAULT_CONTEXT: dict[str, int] = {
    "openai": 128_000,
    "anthropic": 200_000,
    "gemini": 1_000_000,
    "deepseek": 64_000,
}

# 模型 id 子串 → 覆盖值。优先级高于 provider 默认。匹配按出现顺序遍历；先命中先用。
_MODEL_CONTEXT_OVERRIDES: list[tuple[str, int]] = [
    # OpenAI 系列
    ("gpt-3.5-turbo-16k", 16_000),
    ("gpt-3.5", 16_000),
    ("gpt-5", 400_000),
    # Anthropic：带 [1m] 后缀或 -1m- 中缀的 = 1M context 版本
    ("[1m]", 1_000_000),
    ("-1m-", 1_000_000),
    ("-1m", 1_000_000),
    # Gemini 1.5 pro 是 2M（其他 flash / 2.0 / 2.5 系列走 provider 默认 1M）
    ("gemini-1.5-pro", 2_000_000),
]


def resolve_context_window(provider: str, model: str) -> int | None:
    """按 (provider, model) 查找 context window，未知时返回 ``None``。

    匹配规则：先扫 ``_MODEL_CONTEXT_OVERRIDES``（model 子串前缀匹配），命中即返；
    否则用 ``_PROVIDER_DEFAULT_CONTEXT[provider]``；都没有返回 ``None``。
    """
    model_lc = model.lower()
    for needle, value in _MODEL_CONTEXT_OVERRIDES:
        if needle in model_lc:
            return value
    return _PROVIDER_DEFAULT_CONTEXT.get(provider)


def _repo_root_candidates(start: Path | None = None) -> list[Path]:
    """返回常见 .env 路径候选（仓库根 + 父目录兜底）。"""
    here = (start or Path(__file__)).resolve()
    # examples/_provider_bootstrap.py → examples/ → <repo-root>/
    taifeng_root = here.parent.parent
    parent_root = taifeng_root.parent
    return [
        taifeng_root / ".env",
        parent_root / ".env",
    ]


def load_dotenv_files(*extra_paths: Path) -> list[Path]:
    """把候选 .env 中的 ``LLM_BOOTSTRAP_*`` / ``TAIFENG_*`` 拷到 ``os.environ``（不覆盖已有）。

    返回实际加载成功的路径列表（用于日志）。已有环境变量优先级最高，
    `.env` 仅作 fallback。``TAIFENG_*`` 用于 example 自身的旋钮（如 web_ui 的
    ``TAIFENG_WEBUI_EXTRA_SKILLS_DIR``），与 LLM 凭据同样支持从 .env 提供。
    """
    loaded: list[Path] = []
    candidates: list[Path] = list(_repo_root_candidates()) + list(extra_paths)
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if not (k.startswith("LLM_BOOTSTRAP_") or k.startswith("TAIFENG_")):
                continue
            if k in os.environ:
                continue
            os.environ[k] = v
        loaded.append(path)
    return loaded


def resolve_bootstrap_env() -> tuple[str, str | None, str | None, str, str | None]:
    """从 env 解析 ``(provider, protocol, api_key, model, base_url)``。

    OpenAI protocol 默认 ``chat``，可选 ``responses``；其他 provider 不接受
    protocol。兼容新旧两套凭据命名。未知组合抛 ``ProviderBootstrapError``。
    """
    provider = (os.environ.get("LLM_BOOTSTRAP_PROVIDER") or "").lower().strip()

    # 旧形态自动识别（无新 PROVIDER 但有旧 OPENAI key）
    if not provider and os.environ.get("LLM_BOOTSTRAP_OPENAI_API_KEY"):
        provider = "openai"
    if not provider:
        provider = "openai"  # 终极兜底

    if provider not in _DEFAULT_MODELS:
        raise ProviderBootstrapError(
            f"unknown LLM_BOOTSTRAP_PROVIDER={provider!r}; "
            f"expected one of {sorted(_DEFAULT_MODELS)}",
        )

    configured_protocol = (
        os.environ.get("LLM_BOOTSTRAP_PROTOCOL") or ""
    ).lower().strip()
    protocol: str | None = None
    if provider == "openai":
        protocol = configured_protocol or "chat"
        if protocol not in {"chat", "responses"}:
            raise ProviderBootstrapError(
                f"unknown OpenAI protocol={protocol!r}; "
                "expected 'chat' or 'responses'",
            )
    elif configured_protocol:
        raise ProviderBootstrapError(
            "LLM_BOOTSTRAP_PROTOCOL is only valid for provider='openai'",
        )

    # api_key：新形态 → 旧形态（仅 openai）
    api_key = os.environ.get("LLM_BOOTSTRAP_API_KEY")
    if not api_key and provider == "openai":
        api_key = os.environ.get("LLM_BOOTSTRAP_OPENAI_API_KEY")

    # model：新形态 → 旧形态（仅 openai） → provider 默认值
    model = os.environ.get("LLM_BOOTSTRAP_MODEL")
    if not model and provider == "openai":
        model = os.environ.get("LLM_BOOTSTRAP_OPENAI_MODEL")
    if not model:
        model = _DEFAULT_MODELS[provider]

    # base_url：新形态 → 旧形态（仅 openai）
    base_url = os.environ.get("LLM_BOOTSTRAP_BASE_URL")
    if not base_url and provider == "openai":
        base_url = os.environ.get("LLM_BOOTSTRAP_OPENAI_BASE_URL")

    return provider, protocol, api_key, model, base_url


def build_model_client(
    *,
    require_api_key: bool = True,
    timeout_seconds: float = 300.0,
) -> tuple[ModelClient, dict[str, str | int]]:
    """按 env 构造 native ``ModelClient`` + 返回 meta（用于日志）。

    ``require_api_key=True``（默认）时若 api_key 缺失会 raise
    ``ProviderBootstrapError``；某些场景（如 web_ui demo 启动时 key 还没设）
    可传 ``False`` 让构造继续，调用 LLM 时再失败。
    """
    provider, protocol, api_key, model, base_url = resolve_bootstrap_env()

    if not api_key:
        if require_api_key:
            raise ProviderBootstrapError(
                f"missing api_key for provider={provider!r}; "
                "set LLM_BOOTSTRAP_API_KEY (or legacy "
                "LLM_BOOTSTRAP_OPENAI_API_KEY for provider=openai)",
            )
        api_key = ""

    if provider == "openai":
        openai_client_type = (
            OpenAIResponsesClient if protocol == "responses" else OpenAIChatClient
        )
        client: ModelClient = openai_client_type(
            api_key=api_key,
            model=model,
            base_url=base_url or "https://api.openai.com/v1",
            timeout_seconds=timeout_seconds,
        )
    elif provider == "anthropic":
        client = AnthropicClient(
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    elif provider == "gemini":
        client = GeminiClient(
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    elif provider == "deepseek":
        client = DeepSeekClient(
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    else:  # pragma: no cover —— 已在 resolve_bootstrap_env 拦截
        raise ProviderBootstrapError(f"unsupported provider: {provider}")

    meta: dict[str, str | int] = {"provider": provider, "model": model}
    if protocol is not None:
        meta["protocol"] = protocol
    if base_url:
        meta["base_url"] = base_url
    if api_key:
        meta["api_key_tail"] = f"***{api_key[-4:]}" if len(api_key) >= 4 else "***"
    ctx_win = resolve_context_window(provider, model)
    if ctx_win is not None:
        meta["context_window"] = ctx_win
    return client, meta


__all__ = [
    "ProviderBootstrapError",
    "build_model_client",
    "load_dotenv_files",
    "resolve_bootstrap_env",
    "resolve_context_window",
]
