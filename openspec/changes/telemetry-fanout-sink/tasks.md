# telemetry-fanout-sink 实施计划

> 任务 1 无前置，可与 `plugin-core` 并行；任务 2 前置 `plugin-core`。分支 `feat/telemetry-fanout-sink`。

## 1. FanoutSink
- [ ] 1.1 `tests/telemetry/test_fanout.py` 红测试：按加入顺序投递；成员抛错不影响其他成员且 `handle` 不抛；`CancelledError` 传播；重复 `add` 抛 `ValueError`；handle 关闭后不再收到；投递中增删不影响本次投递；空成员时 `handle` 正常返回。Acceptance：观察红。
- [ ] 1.2 新增 `telemetry/fanout.py`。Acceptance：`PYTHONPATH=src uv run pytest tests/telemetry/test_fanout.py -q` 全绿。

## 2. pool 接线与 capability
- [ ] 2.1 `tests/loop/test_pool_plugins.py` 红测试：传 `plugins` 时宿主 sink 仍收到全部 pool 级事件且排在最前；未传 `plugins` 时下游拿到的 sink 与宿主传入的是同一对象。Acceptance：观察红。
- [ ] 2.2 `loop/pool_plugins.py`：有插件时在 `create` 开头构造 `FanoutSink` 并传给原有使用方；`pool.py` 只改一处取值。Acceptance：测试绿；`pool.py` ≤ 800 行。
- [ ] 2.3 `ctx.telemetry.add_sink(sink) -> RegistrationHandle`。测试：插件 sink 收到其后加载插件的 `plugin_loaded` 和全部 `plugin_unloaded`；pool 关闭后成员为空。Acceptance：`tests/plugin/test_context.py` 相关用例绿。
- [ ] 2.4 加载失败测试：setup 失败回滚后 `FanoutSink` 中只剩宿主 sink。Acceptance：测试绿。

## 3. 验证
- [ ] 3.1 在 `docs/architecture/capabilities/plugin-system.md` 写明：插件 sink 第一版只收 pool 级事件；只收到自己加入之后的事件；engine 事件流不转发。
- [ ] 3.2 `PYTHONPATH=src uv run pytest tests/ -q`、ruff 门禁、mypy strict。Acceptance：全绿。
- [ ] 3.3 任务 2 改了 `loop/`，合入前跑真实回归并提交台账。Acceptance：原有场景全 PASS。
