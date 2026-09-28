# ADR 0047：工具参数派发前按 input_schema 校验（内置子集校验器）

- 状态：Accepted
- 日期：2026-09-28
- 关联：[capabilities/tool-argument-validation.md](../architecture/capabilities/tool-argument-validation.md)；[tool-whitelist](../architecture/capabilities/tool-whitelist.md)

## 背景

派发层只校验参数是合法 JSON 对象。缺必填字段、类型错误直接进 handler：要么 handler 抛出难懂的
异常，要么被 `args.get(..., 默认)` 静默兜底（`call_skill` 的必填 `reason` 就是如此——schema 写
required，handler 却容忍缺失，HITL 审批方拿到空理由）。模型拿不到「哪里错、该怎么改」的反馈。

## 决策

1. **schema 即契约，派发前强制**：不合 schema → 不执行 handler，返回违例清单 + schema 让模型改参。
2. **内置子集校验器，不引 jsonschema**：内核核心依赖保持最小（pydantic / anyio / httpx / pyyaml）。
   覆盖模型最常犯的错（type / required / enum / additionalProperties / 嵌套），不认识的关键字一律放过——
   误拦合法调用的代价远高于漏拦（漏拦时 handler 仍会自己报错）。
3. **全路径单一入口**：主派发、retry_tool、编排、resume 执行、子 thread 续跑都走 `arguments_rejection`，
   同一条调用在哪条路径执行规则都一样严。
4. **不开关**：没有「关闭校验」的旋钮——schema 与 handler 不一致是工具定义的 bug，应修 schema。

## 后果

- 依赖 handler 兜底的旧调用（如缺 `reason` 的 `call_skill`）现在会被拒一次、由模型补齐后重发。
  仓库内测试与示例的脚本化调用已补齐 `reason`；真实模型在严格 function-calling 下通常会填必填字段。
- 反馈含 schema 回显（≤2000 字符），多一次往返的 token 成本换来可自愈的错误。
