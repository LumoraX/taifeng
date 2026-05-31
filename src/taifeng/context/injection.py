"""压缩注入位置枚举。

参照：docs/decisions/0004-cache-aware-compression.md
"""

from __future__ import annotations

from enum import Enum


class InitialContextInjection(Enum):
    """压缩后摘要的注入位置。

    决定了 prompt cache 命中的可能性，是 Taifeng 区别于其他框架的核心红线。
    任何 ``CompressionStrategy.compress()`` 必须显式传入此枚举值。
    """

    BEFORE_LAST_USER_MESSAGE = "before_last_user_message"
    """允许动 head。pre-turn 压缩使用。

    适用场景：
        - 会话开始前的预压缩
        - 用户主动触发 /compact

    Cache 影响：
        完全失效。next turn 第一次请求会重建 cache。
    """

    DO_NOT_INJECT = "do_not_inject"
    """只改 tail，head 保持 byte-identical。mid-turn 压缩使用。

    适用场景：
        - turn 内 token 溢出
        - tool 结果过长触发的应急压缩

    Cache 影响：
        保持命中。``cache_anchor_index`` 之前的 history 不会被改写，
        prompt cache 在 next request 可以继续命中。
    """

    MANUAL_HANDOFF = "manual_handoff"
    """用户显式触发的接力压缩。允许动任意位置，但需要 LLM 重新生成摘要。

    适用场景：
        - 用户输入 ``/compact`` 命令
        - API 主动调用 ``CompactNow`` op

    Cache 影响：
        全部失效。显式接受这个代价以换取更彻底的清理。
    """
