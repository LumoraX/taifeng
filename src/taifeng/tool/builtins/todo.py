"""todo builtin —— 内置任务清单工具(pinned-state 官方范例)。

参照 hermes ``todo_tool.py``(LLM 自管任务清单 + 压缩后重注入)与 Claude Code
TodoWrite(整表替换语义);差异:
  1. **整表替换**而非 add/complete 分步——幂等、无分步竞态、LLM 心智负担最低
     (每次提交全量清单,与其在 prompt 中看到的 pinned 注记同构);
  2. **双注入装配**:``TodoStore`` 直接实现 ``PinnedStateSource``,同一实例
     同时传 ``extra_tools=[make_todo_write_tool(store)]`` 与
     ``pinned_state_sources=[store]``——清单自动穿越压缩(E1 机制,无新事件)。

不做持久化(进程内状态;R5 由 pinned 注入项落史保证——resume 后清单内容在
历史可见);不默认注册(extra_tools opt-in);作用域 = 业务构造的实例(通常
一 engine 一 store)。
"""

from __future__ import annotations

from typing import Any

from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

#: 合法任务状态(Claude Code 同款三态);未知值在工具入口显式拒绝
TODO_STATUSES = ("pending", "in_progress", "completed")

#: 渲染前缀:pending → [ ] / in_progress → [~] / completed → [x]
_STATUS_MARK = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


class TodoStore:
    """进程内任务清单 —— 兼作 ``PinnedStateSource``(压缩后自动钉回)。

    ``replace(items)`` 原子整表替换;``format_for_injection()`` 渲染 markdown
    checklist(空清单返回 None,不注入)。
    """

    name = "todo"
    """PinnedStateSource 标识(registry 内唯一;事件/审计用)。"""

    def __init__(self, *, max_chars: int = 2000) -> None:
        """
        Args:
            max_chars: pinned 渲染上限(超出由 E1 的 truncate_middle 截断)。
        """
        self.max_chars = max_chars
        self._items: list[dict[str, str]] = []

    @property
    def items(self) -> list[dict[str, str]]:
        """当前清单的只读视图(拷贝,外部修改不影响 store)。"""
        return [dict(i) for i in self._items]

    def replace(self, items: list[dict[str, str]]) -> None:
        """原子整表替换。调用方(工具入口)负责校验;此处信任内部契约。"""
        self._items = [
            {"content": i["content"], "status": i["status"]} for i in items
        ]

    def format_for_injection(self) -> str | None:
        """渲染 markdown checklist;空清单返回 None(本次不注入,零噪声)。"""
        if not self._items:
            return None
        lines = [f"{_STATUS_MARK[i['status']]} {i['content']}"
                 for i in self._items]
        return "## 任务清单\n" + "\n".join(lines)


def make_todo_write_tool(store: TodoStore) -> ToolSpec:
    """构造 ``todo_write`` 工具 —— LLM 整表替换任务清单。

    Args:
        store: 清单后端;与 ``pinned_state_sources`` 传同一实例即获得
            「清单穿越压缩」能力。
    """

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # 系统边界校验:items 数组、每项 content 非空字符串 + status 三态内。
        # 违例显式拒绝(bad_args),不静默纠正(禁 silent fallback)。
        items = args.get("items")
        if not isinstance(items, list):
            return ToolResult.error(
                "missing_or_invalid_argument: items 必须为数组",
                reason="bad_args",
            )
        for idx, it in enumerate(items):
            if not isinstance(it, dict):
                return ToolResult.error(
                    f"invalid_argument: items[{idx}] 必须为对象",
                    reason="bad_args",
                )
            content = it.get("content")
            if not isinstance(content, str) or not content.strip():
                return ToolResult.error(
                    f"invalid_argument: items[{idx}].content 必须为非空字符串",
                    reason="bad_args",
                )
            status = it.get("status")
            if status not in TODO_STATUSES:
                return ToolResult.error(
                    f"invalid_argument: items[{idx}].status {status!r} "
                    f"不在 {TODO_STATUSES} 内",
                    reason="bad_args",
                )
        store.replace(items)
        # 返回渲染后的全量清单:LLM 即刻确认写入结果,与 pinned 注记视觉同构
        return ToolResult.ok(store.format_for_injection() or "(清单已清空)")

    return ToolSpec(
        name="todo_write",
        description=(
            "维护你的任务清单(**整表替换**):每次必须提交*完整*清单"
            "(含已完成项),漏掉的项会丢失。\n\n"
            "status 三态:pending(待办)/ in_progress(进行中)/ "
            "completed(已完成)。建议同一时刻至多一项 in_progress。\n"
            "清单会在上下文压缩后自动保留(你始终能在历史中看到它)。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "完整任务清单(整表替换)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "任务内容(非空)",
                            },
                            "status": {
                                "type": "string",
                                "enum": list(TODO_STATUSES),
                                "description": "任务状态",
                            },
                        },
                        "required": ["content", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["items"],
            "additionalProperties": False,
        },
        handler=handler,
        parallel_safe=False,  # 写共享清单状态,串行保护
    )
