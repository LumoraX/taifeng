# ADR 0056：SKILL.md `inference` 块声明 skill 级推理参数

- 状态：Accepted
- 日期：2026-09-28
- 关联：[skill-system.md § inference](../architecture/skill-system.md)；ADR 0006（统一 Skill 抽象）/ 0046（thinking 回传）

## 背景

`ApiRequest` 早就有 `reasoning_effort` / `temperature` / `max_output_tokens`，OpenAI Chat / Responses、codex、
Anthropic、Gemini、litellm 各 provider 也都会翻译它们（Anthropic / Gemini 还把 `reasoning_effort` 映射成 thinking 预算）。
但内核的唯一请求构造点 `build_api_request` 从未设置它们：所有 skill 共用 client 的一刀切默认。结果是
「分类 / 抽取子 skill 要确定性输出」「重推理 skill 要 high effort」「短答 skill 要封顶输出」这些按任务分的需求
只能靠开多个 client 绕过，而同一会话里父子 skill 共享一个 client，根本绕不过去。

按 ADR 0017 规则①，这是内核机制缺口：参数通道在 provider 层已通、在 skill 声明层断开。

## 决策

1. **frontmatter 嵌套 `inference` 块**，三个键 `reasoning_effort` / `temperature` / `max_output_tokens`，
   映射为 `SkillDefinition.inference: SkillInference`（frozen dataclass，默认全 None）。
2. **按 entry skill 下发**：`build_api_request` 取 `entry.inference` 写入请求；顶层 entry 与 `call_skill` 子 turn
   走同一路径，父子各用各的声明，不继承。未声明字段为 None，行为与改动前完全一致。
3. **atomic / composite 通用**：atomic 经 `call_skill` 派发时同样独立采样，需要这组参数的恰恰常是 atomic
   （分类 / 抽取）。`model` 仍维持仅 composite 的旧约束，本 ADR 不改。
4. **加载期严格校验**，禁止静默回退：非 mapping、未知键、枚举外 effort、非数值或越界 temperature（[0, 2]）、
   非正整数 max_output_tokens（含布尔值）一律 `SkillValidationError`。拼错的键若被忽略，作者会误以为参数已生效。
5. **provider 不支持的组合由 provider 报错**：如 Anthropic extended thinking 拒绝自定义 temperature，
   沿用既有 `InvalidRequestError`，内核不做静默丢弃。

## 否决的方案

- **平铺为顶层键**（`temperature:` 与 `name:` 并列）：与 `requires` / `exposure` / `orchestration` 的分组风格不一致，
  也让「inference 内未知键报错」无从做起（顶层键另有业务透传语义）。
- **Pool / Engine 级默认值旋钮**：client 构造参数已承担全局默认（如 thinking 预算），再加一层默认会出现
  「client / pool / skill」三处真相源。skill 声明只覆盖本 skill，其余交给 client。

## 影响

- R1：只新增通用推理参数，无业务概念。R2：参数不进 prompt 前缀，不影响 cache 指纹。R3–R5：无变化。
- `max_output_tokens` 不自动联动 `ContextBudget.output_reserve_tokens`；声明大输出上限的 skill 需业务侧同步调大预留。

## 验证

`tests/skill/test_skill_inference.py`（13 例）：缺省全 None、合法块解析、10 种非法输入加载期报错、
父 entry 与派发子 skill 在真实 pool 中各自按声明下发（SimClient 记录请求）。
