"""验证工作区 offload 场景能拒绝能力未生效或证据缺失的假通过。"""

from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from typing import Any

_ROOT = Path(__file__).resolve().parents[2]


def _modules(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """加载示例模块，同时禁止模拟验证读取任何 dotenv 文件。"""
    monkeypatch.syspath_prepend(str(_ROOT / "examples"))
    monkeypatch.syspath_prepend(str(_ROOT / "examples" / "real_llm"))
    bootstrap = importlib.import_module("_provider_bootstrap")
    monkeypatch.setattr(bootstrap, "load_dotenv_files", lambda *paths: [])
    assert importlib.util.find_spec("_offload_workspace") is not None, "缺少工作区 offload 场景"
    return importlib.import_module("_offload_workspace"), importlib.import_module("capability_matrix")


@pytest_asyncio.fixture
async def verified_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """通过真实内核跑一次动态 sim，供后续破坏证据的测试复用。"""
    module, matrix = _modules(monkeypatch)
    scenario = next(sc for sc in matrix.SCENARIOS if sc.demo_id == "offload_workspace")
    client = module.OffloadWorkspaceSimClient()
    result = await matrix.run_scenario(client, scenario, tmp_path / "logs")
    assert matrix._verdict(result)[0] == "✅PASS"
    assert not client.ledger.violations
    return module, result


async def test_workspace_offload_reads_random_middle_token(verified_run: tuple[Any, Any]) -> None:
    """口令必须由随机工作区文件分页回读，首个工具后请求只能看到 stub。"""
    module, result = verified_run
    proof = result.state["evidence"]
    workspace = result.state["workspace"]
    assert proof["path"].startswith(f"_offload/{proof['thread_id']}/")
    assert workspace.root.startswith("memory://")
    assert result.state["token"] in proof["answer"]
    assert len(workspace.files[workspace.resolve(proof["path"])]) > 8 * 1024
    assert module.verify_offload_evidence(result, proof["thread_id"], proof["answer"])


@pytest.mark.parametrize("damage", ["offload_count", "cleartext", "paging", "workspace_read"])
async def test_driver_rejects_missing_offload_proof(
    verified_run: tuple[Any, Any], damage: str,
) -> None:
    """逐一移除关键证据时，驱动必须拒绝最终回答碰巧正确的结果。"""
    module, result = verified_run
    proof = result.state["evidence"]
    if damage == "workspace_read":
        result.state["workspace"].reads.clear()
    else:
        for index, event in enumerate(result.events):
            data = dict(event.data)
            if damage == "offload_count" and event.kind == "compaction_completed":
                data["detail"] = {"offloaded": 0}
            elif (damage == "cleartext" and event.kind == "llm_request_recorded"
                  and "[offloaded" in json.dumps(data)):
                data["leaked_fulltext"] = result.state["token"]
                damage = "done"
            elif (damage == "paging" and event.kind == "tool_call_started"
                  and data.get("name") == "file_read"):
                arguments = json.loads(data["arguments"])
                arguments.pop("offset", None)
                data["arguments"] = json.dumps(arguments)
            result.events[index] = SimpleNamespace(kind=event.kind, data=data)
    with pytest.raises(AssertionError):
        module.verify_offload_evidence(result, proof["thread_id"], proof["answer"])


async def test_driver_rejects_wrong_final_answer(verified_run: tuple[Any, Any]) -> None:
    """即使工具与事件都正确，最终答案没有口令仍应失败。"""
    module, result = verified_run
    with pytest.raises(AssertionError):
        module.verify_offload_evidence(result, result.state["evidence"]["thread_id"], "未找到")


async def test_example_workspace_obeys_protocol_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """内存示例履行七成员协议，分页回读不依赖宿主文件。"""
    module, _ = _modules(monkeypatch)
    from taifeng.tool.workspace import WorkspaceFS, WorkspacePathError

    workspace = module.ExampleWorkspaceFS()
    assert isinstance(workspace, WorkspaceFS)
    assert workspace.resolve("") == workspace.root
    assert not (await workspace.metadata("missing")).exists
    with pytest.raises(FileNotFoundError):
        await workspace.read_bytes("missing")
    with pytest.raises(FileNotFoundError):
        await workspace.write_bytes("nested/file", b"", create_parents=False)
    for path in ("../escape", "/local/file", "memory://other/file", "a/../../escape"):
        with pytest.raises(WorkspacePathError):
            workspace.resolve(path)
    await workspace.write_bytes("nested/file", b"")
    assert await workspace.read_bytes("nested/file") == b""
    assert [entry.name for entry in await workspace.list_directory("")] == ["nested"]
    with pytest.raises(OSError):
        await workspace.remove("nested")
    await workspace.remove("nested", recursive=True)
    assert not (await workspace.metadata("nested/file")).exists


async def test_cancelled_workspace_write_has_no_side_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    """工作区写入在协作检查点响应取消，不产生半写文件。"""
    module, _ = _modules(monkeypatch)
    import anyio

    workspace = module.ExampleWorkspaceFS()
    with anyio.CancelScope() as scope:
        scope.cancel()
        await workspace.write_bytes("cancelled", b"value")
    assert not workspace.files and not workspace.writes


def test_assembly_generates_new_secret_each_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """各次真实运行使用不同口令，避免模型依靠固定答案通过。"""
    module, _ = _modules(monkeypatch)
    first = module.setup_offload_workspace(tmp_path, None)
    second = module.setup_offload_workspace(tmp_path, None)
    assert first.state["token"] != second.state["token"]
    assert first.state["workspace"] is not second.state["workspace"]


@pytest.mark.parametrize("decoy", ["second_fetch", "wrong_path_read", "unpaged_read"])
async def test_unrelated_output_cannot_end_secret_leak_check(
    verified_run: tuple[Any, Any], decoy: str,
) -> None:
    """其他取全文或无效回读之后泄露口令，不能由后续正常分页回读洗成通过。"""
    module, result = verified_run
    proof = result.state["evidence"]
    token = result.state["token"]
    workspace = result.state["workspace"]
    raw = workspace.files[workspace.resolve(proof["path"])].decode()
    request = next(event for event in result.events if event.kind == "llm_request_recorded"
                   and "[offloaded" in json.dumps(event.data))
    leaked_request = json.loads(json.dumps(request.data))
    leaked_request["messages"].append({
        "role": "tool", "tool_call_id": "decoy-call", "content": raw,
    })
    name = module.FETCH_TOOL if decoy == "second_fetch" else "file_read"
    arguments = {"path": proof["path"], "offset": module.MIDDLE_LINE, "limit": 3}
    if decoy == "wrong_path_read":
        arguments["path"] = "other/file"
    elif decoy == "unpaged_read":
        arguments.pop("offset")
        arguments.pop("limit")
    decoy_events = [
        SimpleNamespace(kind="tool_call_started", data={
            "name": name, "call_id": "decoy-call",
            "arguments": json.dumps({} if decoy == "second_fetch" else arguments),
        }),
        SimpleNamespace(kind="tool_call_completed", data={
            "name": name, "call_id": "decoy-call", "is_error": False,
            "output": raw if decoy == "second_fetch" else f"MIDDLE-TOKEN: {token}",
        }),
        SimpleNamespace(kind="llm_request_recorded", data=leaked_request),
    ]
    read_index = next(index for index, event in enumerate(result.events)
                      if event.kind == "tool_call_started" and event.data["name"] == "file_read")
    result.events[read_index:read_index] = decoy_events
    with pytest.raises(AssertionError, match="分页回读前模型已见过完整口令"):
        module.verify_offload_evidence(result, proof["thread_id"], proof["answer"])
