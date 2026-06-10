"""Taifeng 内置工具集。

核心工具：
    - read_skill / call_skill —— skill-as-context 范式（按需取 + 派发子 skill）

可选工具（业务侧自选注入）：
    - file_read / file_write —— 沙盒文件 IO
    - shell_exec —— 阻塞式 shell 执行
    - run_script —— SKILL.md 内 scripts 派发（ADR 0009）
    - apply_patch —— 结构化原子化补丁（M4）
    - run_in_background / wait_for_task —— shell 长任务后台执行（M4）
    - http_request —— 受 PermissionPolicy(scope='network') 审批的 HTTP 调用（P0）
    - spawn_skill / await_skills / join_skill / kill_skill —— detached-spawn LLM 入口
"""

from taifeng.tool.builtins.apply_patch import make_apply_patch_tool
from taifeng.tool.builtins.background import (
    BackgroundTaskRegistry,
    BackgroundTaskTimeoutError,
    make_run_in_background_tool,
    make_wait_for_task_tool,
)
from taifeng.tool.builtins.call_skill import make_call_skill_tool
from taifeng.tool.builtins.file_io import make_file_read_tool, make_file_write_tool
from taifeng.tool.builtins.http_request import make_http_request_tool
from taifeng.tool.builtins.read_skill import make_read_skill_tool
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool
from taifeng.tool.builtins.run_script import make_run_script_tool
from taifeng.tool.builtins.send_message import make_send_message_tool
from taifeng.tool.builtins.shell import make_shell_exec_tool
from taifeng.tool.builtins.spawn_skill import (
    make_await_skills_tool,
    make_join_skill_tool,
    make_kill_skill_tool,
    make_spawn_skill_tool,
)
from taifeng.tool.builtins.todo import TodoStore, make_todo_write_tool
from taifeng.tool.builtins.wait_peer import make_wait_peer_tool

__all__ = [
    "TodoStore",
    "BackgroundTaskRegistry",
    "BackgroundTaskTimeoutError",
    "make_apply_patch_tool",
    "make_await_skills_tool",
    "make_call_skill_tool",
    "make_file_read_tool",
    "make_file_write_tool",
    "make_http_request_tool",
    "make_join_skill_tool",
    "make_kill_skill_tool",
    "make_read_skill_tool",
    "make_request_user_input_tool",
    "make_run_in_background_tool",
    "make_run_script_tool",
    "make_send_message_tool",
    "make_todo_write_tool",
    "make_shell_exec_tool",
    "make_spawn_skill_tool",
    "make_wait_peer_tool",
    "make_wait_for_task_tool",
]
