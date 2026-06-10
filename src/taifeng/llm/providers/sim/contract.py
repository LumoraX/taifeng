"""协议合同校验 —— conformance 模拟器的「服务端审请求」环节。

对 engine 发来的 ``ApiRequest`` 校验跨 provider 通用的协议不变量
（对照 ``loop/prompt.py:history_to_api_messages`` 产出的真实消息形状）：

1. **call_id 配对**：assistant 声明的每个 ``tool_calls[].id`` 必须被同
   ``tool_call_id`` 的 tool 消息恰好核销一次（不得重复声明 / 重复核销 / 悬空）；
   **采样时刻（messages 末尾）不得有未核销 id**——直接打击 resume / rewind /
   中间态重建 / 工具结果未回传类 bug；
2. **未核销期间不得出现 user 消息**（steering 在迭代边界并入，合法请求不会把
   user 插进未核销区）；
3. **messages 非空**。

显式不校验的（taifeng 合法结构）：
- system 消息可出现在任意位置（compacted 摘要 / 业务 system_injection 是合法中段 system）；
- 并行 fan-out 的「多条 assistant function_call 消息先后声明、输出交错核销」结构。

违规一律抛 ``SimContractViolation``（普通 Exception，**不入 LLMError 体系**——
否则会被 provider retry / 错误分类路径当可恢复错误消化掉，测试就红不了）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from taifeng.llm.providers.sim.script import SimTurn
    from taifeng.llm.providers.sim.server import RecordedRequest
    from taifeng.llm.types import ApiRequest


class SimContractViolation(Exception):  # noqa: N818 —— 信号语义命名，仓库先例见 SuspendSignal
    """协议合同违规。

    ``rule`` 是机读规则标识（empty_messages / duplicate_declaration /
    dangling_output / duplicate_settlement / user_while_pending /
    unsettled_at_sampling / unknown_tool_response / expect_*），便于测试精确断言。
    """

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(f"[{rule}] {message}")
        self.rule = rule


class RequestContractValidator:
    """无状态合同校验器：每次 ``validate(request)`` 独立检查一份请求。"""

    def validate(self, request: ApiRequest) -> None:
        """校验请求侧协议合同；发现第一处违规即抛 ``SimContractViolation``。"""
        if not request.messages:
            raise SimContractViolation("empty_messages", "请求 messages 为空")

        declared: dict[str, str] = {}  # call_id -> tool name（声明区）
        settled: set[str] = set()      # 已核销 call_id

        for idx, msg in enumerate(request.messages):
            if msg.role == "assistant" and msg.tool_calls:
                # 声明：每个 tool_call id 只允许声明一次
                for tc in msg.tool_calls:
                    call_id = str(tc.get("id", ""))
                    if not call_id:
                        raise SimContractViolation(
                            "duplicate_declaration",
                            f"messages[{idx}] assistant tool_call 缺 id",
                        )
                    if call_id in declared:
                        raise SimContractViolation(
                            "duplicate_declaration",
                            f"messages[{idx}] call_id={call_id!r} 重复声明",
                        )
                    declared[call_id] = _tool_call_name(tc)
            elif msg.role == "tool":
                # 核销：必须引用已声明且未核销的 call_id
                call_id = str(msg.tool_call_id or "")
                if call_id not in declared:
                    raise SimContractViolation(
                        "dangling_output",
                        f"messages[{idx}] tool 消息引用未声明的 call_id={call_id!r}（悬空输出）",
                    )
                if call_id in settled:
                    raise SimContractViolation(
                        "duplicate_settlement",
                        f"messages[{idx}] call_id={call_id!r} 重复核销",
                    )
                settled.add(call_id)
            elif msg.role == "user":
                # 未核销期间不得出现 user 消息（steering 在迭代边界并入）
                pending = set(declared) - settled
                if pending:
                    raise SimContractViolation(
                        "user_while_pending",
                        f"messages[{idx}] 存在未核销 call_id={sorted(pending)} 时出现 user 消息",
                    )
            # system 消息任意位置合法（compacted / system_injection），不校验

        pending = set(declared) - settled
        if pending:
            raise SimContractViolation(
                "unsettled_at_sampling",
                f"采样时刻仍有未核销 call_id={sorted(pending)}（工具结果未回传 / 重建漂移）",
            )

    def validate_expect(self, turn: SimTurn, record: RecordedRequest) -> None:
        """逐 turn 声明式断言（``SimTurn.expect``）：给关键测试加请求侧密度。

        规则标识以 ``expect_`` 前缀区分于通用合同规则，便于测试精确断言。
        """
        expect = turn.expect
        if expect is None:
            return
        blob = record.blob()
        for needle in expect.must_contain:
            if needle not in blob:
                raise SimContractViolation(
                    "expect_must_contain", f"请求全文未包含期望子串 {needle!r}"
                )
        for call_id in expect.must_include_output_for:
            if record.function_call_output_text(call_id) is None:
                raise SimContractViolation(
                    "expect_missing_output", f"请求中缺少 call_id={call_id!r} 的工具结果"
                )
        n = len(record.request.messages)
        if expect.min_messages is not None and n < expect.min_messages:
            raise SimContractViolation(
                "expect_message_count", f"messages 数 {n} < 下界 {expect.min_messages}"
            )
        if expect.max_messages is not None and n > expect.max_messages:
            raise SimContractViolation(
                "expect_message_count", f"messages 数 {n} > 上界 {expect.max_messages}"
            )
        if expect.predicate is not None and not expect.predicate(record.request):
            raise SimContractViolation("expect_predicate", "自定义谓词返回 False")

    def validate_response_side(self, turn: SimTurn, request: ApiRequest) -> None:
        """响应侧反查：脚本将吐的 tool_call，其 name 必须已注册进本请求 tools。

        抓「engine 没把工具注册进请求却被脚本调用」类 bug（如 extra_tools 漏注册、
        snapshot 没刷新）。
        """
        offered = {t.name for t in request.tools}
        for tc in turn.tool_calls:
            name = str(tc.get("name", ""))
            if name not in offered:
                raise SimContractViolation(
                    "unknown_tool_response",
                    f"脚本要吐 tool_call name={name!r}，但请求 tools 只注册了 {sorted(offered)}",
                )


def _tool_call_name(tc: dict[str, object]) -> str:
    """取 tool_call 的工具名：兼容扁平 ``{"name": ...}`` 与 OpenAI 嵌套
    ``{"function": {"name": ...}}`` 两种形状（prompt.py 产出的是嵌套形）。"""
    function = tc.get("function")
    if isinstance(function, dict):
        return str(function.get("name", ""))
    return str(tc.get("name", ""))
