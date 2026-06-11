"""金样录制器 —— 包装真实 ModelClient，把事件流形状签名落盘金样 fixture。

llm-golden-calibration（mock 重设计三部曲之三）的录制端：

- ``RecordingClient`` 包装任意 ``ModelClient``，拦截每次 sampling 的
  ``ResponseEvent`` 流，**录制时即调用 sim/shape.py 归约为形状签名**——
  fixture 不存原始事件，零文本零数值，脱敏是结构性保证（design D2）。
- ``flush_golden`` 把单场景去重后的签名原子写入
  ``tests/llm/golden/<scenario>.jsonl``（每行 signature + 录制元数据）。

录制边界（与 design D3/D5 一致）：

- terminal == "truncated" 的签名**不入金样**：消费侧提前断流（取消 / 异常 /
  rewind 中止）与 provider 截断在录制端不可区分，截断形状不是稳定契约；
- 去重按比对维度（``comparable()``）——chunking 等只录不比字段取首个观测值；
- 只应对 PASS 场景调用 ``flush_golden``（调用方 capability_matrix 负责把关）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from taifeng.llm.providers.sim.shape import ShapeSignature, extract_shape

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.llm.client import ModelClient, ModelClientSession
    from taifeng.llm.events import ResponseEvent
    from taifeng.llm.types import ApiRequest
    from taifeng.loop.cancellation import CancellationToken

# 金样固定落仓库 tests/llm/golden/（examples/real_llm/_recorder.py → parents[2] = 仓库根）
GOLDEN_DIR = Path(__file__).resolve().parents[2] / "tests" / "llm" / "golden"


class _RecordingSession:
    """turn 级包装 session：透传事件并在流终结时归约签名上报。"""

    def __init__(self, inner: ModelClientSession, recorder: RecordingClient) -> None:
        self._inner = inner
        self._recorder = recorder

    async def __aenter__(self) -> _RecordingSession:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._inner.__aexit__(*exc)

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        events: list[ResponseEvent] = []
        try:
            async for ev in self._inner.stream(request):
                events.append(ev)
                yield ev
        finally:
            # provider 抛错（rate_limit / content_filter…）或消费侧断流也要归约：
            # error 终态是合法金样；truncated 在 _on_stream_end 内被滤掉
            if events:
                self._recorder._on_stream_end(extract_shape(events))  # noqa: SLF001


class RecordingClient:
    """包装任意 ``ModelClient`` 的录制客户端（ModelClient 协议同构，可直接入 EnginePool）。

    用法：跑场景前 ``begin_scenario(id)`` 标记归属；场景 PASS 后
    ``flush_golden(id)`` 落盘。并发 spawn 子轨的 sampling 同归当前场景
    （capability_matrix 场景间串行，无跨场景串扰）。
    """

    def __init__(self, inner: ModelClient) -> None:
        self._inner = inner
        self._scenario: str | None = None
        # 场景 → 按观测顺序去重后的签名列表（去重键 = 比对维度 comparable()）
        self.signatures: dict[str, list[ShapeSignature]] = {}
        self.truncated_skipped = 0  # 被滤掉的截断签名计数（透明可审计，无静默丢弃）

    def begin_scenario(self, scenario_id: str) -> None:
        """标记后续 sampling 归属的场景。"""
        self._scenario = scenario_id
        self.signatures.setdefault(scenario_id, [])

    def session(
        self, *, cancel: CancellationToken, model: str | None = None
    ) -> _RecordingSession:
        """创建包装 session（透传 inner，挂录制旁路）。"""
        return _RecordingSession(self._inner.session(cancel=cancel, model=model), self)

    def _on_stream_end(self, sig: ShapeSignature) -> None:
        """单次 sampling 流终结回调：滤截断 → 按比对维度去重入册。"""
        if sig.terminal == "truncated":
            self.truncated_skipped += 1
            return
        if self._scenario is None:
            raise RuntimeError("RecordingClient: 录制前必须先 begin_scenario()")
        bucket = self.signatures[self._scenario]
        key = json.dumps(sig.comparable(), ensure_ascii=False, sort_keys=True)
        seen = {
            json.dumps(s.comparable(), ensure_ascii=False, sort_keys=True) for s in bucket
        }
        if key not in seen:
            bucket.append(sig)

    def flush_golden(
        self,
        scenario_id: str,
        *,
        provider: str,
        model: str,
        commit: str,
        recorded_at: str,
        golden_dir: Path = GOLDEN_DIR,
    ) -> Path | None:
        """把场景签名原子写入金样 JSONL；该场景无签名则不动旧金样，返回 None。

        每行结构：``{"signature": {...}, "recorded_at": ..., "commit": ...,
        "provider": ..., "model": ...}``——元数据只录不比（可溯源对账台账）。
        """
        sigs = self.signatures.get(scenario_id, [])
        if not sigs:
            return None
        golden_dir.mkdir(parents=True, exist_ok=True)
        path = golden_dir / f"{scenario_id}.jsonl"
        lines = [
            json.dumps(
                {
                    "signature": sig.to_dict(),
                    "recorded_at": recorded_at,
                    "commit": commit,
                    "provider": provider,
                    "model": model,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            for sig in sigs
        ]
        # 原子写：先写临时文件再 replace，避免中断留半截金样
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path
