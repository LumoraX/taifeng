"""public-api —— 稳定层 / 实验层 / 弃用机制（ADR 0066）。

稳定层 = ``taifeng.__all__``。快照守护：任何增删都必须同步改 ``tests/public_api_snapshot.txt``，
让 API 变化成为有意为之的决定；移除须先走弃用期（``taifeng._deprecation``）。
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import inspect
from pathlib import Path

import pytest

import taifeng
import taifeng.experimental
from taifeng import _deprecation
from taifeng._deprecation import DeprecatedAlias

_SNAPSHOT = Path(__file__).parent / "public_api_snapshot.txt"


def test_stable_api_matches_snapshot() -> None:
    """顶层 ``__all__`` 与快照一致；不一致时提示按 ADR 0066 处理。"""
    recorded = set(_SNAPSHOT.read_text(encoding="utf-8").split())
    current = set(taifeng.__all__)
    assert current == recorded, (
        f"public API changed — added {sorted(current - recorded)}, removed "
        f"{sorted(recorded - current)}. Update tests/public_api_snapshot.txt deliberately; "
        "removing a stable name requires a deprecation period (ADR 0066)."
    )


@pytest.mark.parametrize("module", [taifeng, taifeng.experimental], ids=["stable", "experimental"])
def test_every_exported_name_resolves(module: object) -> None:
    for name in module.__all__:  # type: ignore[attr-defined]
        if name.startswith("Otel"):
            continue  # optional extra，按需 lazy import（未装 extra 时访问会在构造期报错）
        assert getattr(module, name) is not None


def test_experimental_names_are_not_in_stable_layer() -> None:
    """同一符号不能同时处在两层（晋升时从实验层移到顶层）。"""
    assert not set(taifeng.experimental.__all__) & set(taifeng.__all__)


def test_deprecated_alias_warns_and_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """弃用期旧名照常可用，但发 DeprecationWarning 写明替代写法。"""
    monkeypatch.setitem(_deprecation.DEPRECATED_ALIASES, "OldBudget", DeprecatedAlias(
        target="taifeng.context.budget:ContextBudget", since="2026.9.29",
        removal_not_before="2026-11-01", replacement="taifeng.ContextBudget"))
    with pytest.warns(DeprecationWarning, match="use taifeng.ContextBudget instead"):
        resolved = taifeng.OldBudget  # type: ignore[attr-defined]
    assert resolved is taifeng.ContextBudget


def test_unknown_attribute_still_raises() -> None:
    with pytest.raises(AttributeError):
        taifeng.NoSuchThing  # type: ignore[attr-defined]  # noqa: B018


def test_deprecation_registry_targets_exist() -> None:
    """登记中的弃用别名都能解析到真实对象（防止别名指向已删除的位置）。"""
    for alias in _deprecation.DEPRECATED_ALIASES.values():
        module_path, attr = alias.target.split(":", 1)
        assert hasattr(importlib.import_module(module_path), attr)


# ---------------------------------------------------------------------------
# 平台对接面（ADR 0110）
# ---------------------------------------------------------------------------


def _annotation_names(node: ast.expr | None) -> set[str]:
    """一个注解里出现的全部名字（字符串注解一并解析）。"""
    if node is None:
        return set()
    names: set[str] = set()
    pending: list[ast.AST] = [node]
    while pending:
        current = pending.pop()
        for child in ast.walk(current):
            if isinstance(child, ast.Name):
                names.add(child.id)
            elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                with contextlib.suppress(SyntaxError):
                    pending.append(ast.parse(child.value, mode="eval").body)
    return names


def _signature_classes(protocol: type) -> set[type]:
    """协议公开方法签名里引用到的 taifeng 类（类型别名、内建类型不算）。"""
    module = importlib.import_module(protocol.__module__)
    tree = ast.parse(inspect.getsource(module))
    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("taifeng"):
            for alias in node.names:
                imported[alias.asname or alias.name] = node.module
    definition = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == protocol.__name__
    )
    used: set[str] = set()
    for member in definition.body:
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if member.name.startswith("_"):
                continue
            for argument in (*member.args.args, *member.args.kwonlyargs):
                used |= _annotation_names(argument.annotation)
            used |= _annotation_names(member.returns)
    classes: set[type] = set()
    for name in used - {protocol.__name__}:
        if name in imported:
            resolved = getattr(importlib.import_module(imported[name]), name, None)
        else:
            resolved = getattr(module, name, None)
        if inspect.isclass(resolved) and resolved.__module__.startswith("taifeng"):
            classes.add(resolved)
    return classes


def test_stable_protocol_signatures_only_use_exported_types() -> None:
    """外部包只用公共 API 就能把稳定层的协议实现完整：签名里的类型也在公共 API 里。

    起因：``MessageStore.list_threads`` 返回 ``ThreadInfo``，而它曾经没有导出，下游只能去
    import 内部模块。
    """
    exported = {
        getattr(module, name)
        for module in (taifeng, taifeng.experimental)
        for name in module.__all__
        if not name.startswith("Otel")
    }
    protocols = [
        obj for name in taifeng.__all__
        if inspect.isclass(obj := getattr(taifeng, name, None)) and getattr(obj, "_is_protocol", False)
    ]
    assert len(protocols) >= 20
    missing = {
        protocol.__name__: sorted(cls.__name__ for cls in _signature_classes(protocol) - exported)
        for protocol in protocols
    }
    assert {name: gaps for name, gaps in missing.items() if gaps} == {}


_PLATFORM_FACING = (
    # 会话存储
    "ThreadInfo", "AtomicBatchMessageStore", "BatchAppendAck", "BatchConflictError",
    # 模型客户端与模拟器
    "ModelClientSession", "ModelCapabilities", "RetryConfig", "InputCostEstimator",
    "SimClient", "SimTurn", "RoutingSimClient",
    # 压缩策略
    "CompressionTrigger",
    # 操作与遥测
    "CancelTurn", "TelemetrySink", "ConsoleSink", "JsonlSink",
    # 内置工具工厂
    "make_shell_exec_tool", "make_file_read_tool", "make_file_write_tool", "make_http_request_tool",
    "make_glob_tool", "make_grep_tool", "make_memory_tool", "make_request_user_input_tool",
    "make_spawn_skill_tool", "make_await_skills_tool", "make_join_skill_tool", "make_kill_skill_tool",
    "make_read_skill_tool", "make_call_skill_tool", "make_run_script_tool", "make_search_skills_tool",
    # 用户文件输入
    "FileAttachmentV1", "FileInputPolicy", "FilePart",
    # MCP
    "McpClient", "McpHttpClient", "McpToolBinding", "bind_mcp_tools", "register_mcp_tools_async",
    "ElicitationHandler", "ElicitationRequest", "ElicitationResult",
)


@pytest.mark.parametrize("name", _PLATFORM_FACING)
def test_platform_facing_symbols_are_in_the_stable_layer(name: str) -> None:
    assert name in taifeng.__all__
    assert getattr(taifeng, name) is not None


@pytest.mark.parametrize("name", [
    "McpHttpClient", "McpToolBinding", "bind_mcp_tools",
    "FileAttachmentV1", "FileInputPolicy", "FilePart",
])
def test_promoted_names_stay_importable_from_experimental(name: str) -> None:
    """晋升后在实验层保留一个发布版本：照常可用，提示改从顶层导入。"""
    assert name not in taifeng.experimental.__all__
    with pytest.warns(DeprecationWarning, match="promoted to the stable layer"):
        resolved = getattr(taifeng.experimental, name)
    assert resolved is getattr(taifeng, name)


def test_experimental_still_rejects_unknown_names() -> None:
    with pytest.raises(AttributeError):
        taifeng.experimental.NoSuchThing  # type: ignore[attr-defined]  # noqa: B018


async def test_a_downstream_test_can_be_written_with_stable_names_only(tmp_path: Path) -> None:
    """下游包只靠稳定层就能起一个模拟会话、开文件工具、跑完一轮并取消下一轮。"""
    for name, front in (
        ("entry", "type: composite\nentry: true\nmodel: sim-model\nchild_skills: [helper]\n"
                  "tool_names: [file_write]\n"),
        ("helper", "type: atomic\n"),
    ):
        (tmp_path / "skills" / name).mkdir(parents=True)
        (tmp_path / "skills" / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\nversion: 1.0.0\n{front}---\n# {name}\n",
            encoding="utf-8",
        )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pool = await taifeng.EnginePool.create(
        skills_dir=tmp_path / "skills", storage_dir=tmp_path / "threads", compressors=[],
        model_client=taifeng.SimClient(turns=[
            taifeng.SimTurn(tool_calls=[{
                "id": "c1", "name": "file_write",
                "arguments": '{"path": "note.txt", "content": "hello"}',
            }]),
            taifeng.SimTurn(text="done"),
        ]),
        extra_tools=[taifeng.make_file_write_tool(root_dir=workspace)],
        retry_config=taifeng.RetryConfig(max_attempts=1),
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="entry")
    submission = await engine.submit(taifeng.UserMessage(text="write it"))
    async for event in engine.subscribe(submission):
        if event.msg.kind in ("turn_completed", "turn_failed"):
            assert event.msg.kind == "turn_completed", event.msg.data
            break
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "hello"
    # CancelTurn 在稳定层：对已结束的提交取消是空操作，不报错
    await engine.submit(taifeng.CancelTurn(submission_id=submission))
    (listed,) = await pool.store.list_threads()
    assert isinstance(listed, taifeng.ThreadInfo)
    await pool.close()
