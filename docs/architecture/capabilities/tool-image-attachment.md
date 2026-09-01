# Capability: tool-image-attachment（工具图片附件）

## Purpose

补内核的一个**机制缺口**：输入面是多模态的（user 消息可带图），工具返回面却是单模态的（`ToolResult.output: str`）。后果是模型只在会话**开始那一刻**能看见图；一旦进入 agent loop，它的感官被内核单方面降级成纯文本——**不是少了个功能，是模型的输入面被内核截断**。

按 [ADR 0017](../../decisions/0017-kernel-positioning-criteria.md) 规则①（内核机制缺口→做）立项。规则④不适用：codex 的 `FunctionCallOutputContentItem::InputImage` 是同一机制的成熟先例，本能力不是「对齐别家功能表」。

由此解锁的范式：**看什么由看的人决定**。取图从「turn 开始前定死的输入」变成「loop 内可反复发起的动作」，观察→判断→再观察的回路留在执行者手里，而不是由上游编排层代为选帧。

## 内核 / 业务边界

本能力**只提供机制，不提供产品**。边界如下，越界即违反 R1：

| 关注点 | 内核提供 | 业务提供 |
| --- | --- | --- |
| 图片来源 | ✗ | 取图工具自身（读盘 / 调用视觉 API / 抽帧……全部业务实现） |
| 图片承载 | `ToolResult.attachments` 契约位 + `ImageAttachmentV1.from_bytes` 构造器 | 调用它 |
| 资源策略 | `ImageInputPolicy` 的**执行**（数量 / 字节 / MIME / 尺寸 / 帧数） | 策略**取值**（注入 `ImageInputPolicy`；不注入即整体关闭） |
| 模型能力 | `ModelCapabilities.tool_output_modalities` 的声明位与判定 | 选哪个 client / 哪个模型 |
| 可用性门控 | `requires.modalities` 比对与隐藏 | SKILL.md 里声明需要 `tool_output_image` |
| 协议适配 | Responses / Codex 原生 `input_image`；其余档 in-band 降级 | ✗ |
| 成本 | 计入 `ContextBudget`、走 `InputCostEstimator` | 注入估算器（未注入走保守上界） |
| 淘汰 | 压缩策略的附件感知（去重 / 剪枝 / 落盘回避） | 选哪档压缩策略 |

**内核明确不做**（这些是消费方的事，出现在 `src/` 内即越界）：
- 不知道「帧」「录像」「病灶」等任何领域概念——只知道 attachment
- 不替业务决定看哪张图、看几张、何时看
- 不提供任何取图工具的实现或 schema

## 数据契约

| 结构 | 模块 | 要点 |
| --- | --- | --- |
| `ToolResult.attachments: tuple[ImageAttachmentV1, ...]` | `tool/spec.py` | 默认 `()` = 与既有行为逐位一致。`output` 始终是**权威文本投影**（压缩视图 / telemetry / 降级档都读它），`attachments` 只承载额外的非文本部分，两者不重复表达同一内容 |
| `ImageAttachmentV1.from_bytes(data, *, media_type, detail)` | `llm/image_input.py` | 由字节一次算对 base64 / size / sha256 三者，避免调用方手写而错在 admission |
| `function_call_output(..., attachments=)` | `conversation/models.py` | payload 仅在附件非空时才写 `attachments` 键——空则形状逐键与既有一致，冷恢复与审计比对不受影响 |
| `ModelCapabilities.tool_output_modalities` | `llm/client.py` | 与 `input_modalities` **分开**声明：Chat 的 user 消息能带图但 tool 消息不能，合并声明必然误判。默认 text-only，能力显式打开，**不得**据模型名 / 域名推断 |
| `SkillRequirements.modalities` | `skill/definition.py` | skill 声明需要的模态标签（`tool_output_image` / `input_image`） |
| `RuntimeCapabilities.modalities` | `skill/eligibility.py` | 可用标签集；由内核 `derive_modality_tags` 从 client 自己的声明派生，与业务自定义标签取并集 |
| `ApiFunctionCallOutputItem.output: PartContent` | `llm/types.py` | 纯文本仍是 `str`（wire 逐位不变），带图为 `[TextPart, ImagePart, ...]` |

## 行为契约

1. **准入前置**：附件在 **durable append 之前**由 `admit_tool_attachments` 完成校验。渲染期才失败意味着脏 item 已落 JSONL，冷恢复会在同一处反复炸且重试救不回。
2. **策略未启用即拒**：未注入 enabled `ImageInputPolicy` 时工具返回附件 → `UnsupportedModalityError`。空附件短路，非图片工具零影响。
3. **准入失败 = 该次工具调用判错**，不上抛出批。上抛会留下无 output 的悬空 `function_call`，配对断裂直接 400；转为 `is_error` 的 fco 既保配对又如实告知模型。
4. **两类失败分明**：
   - **准入期**（策略未启用 / sha256 不符 / 超限 / 帧数非法）→ **如实抛**，这是配置或数据错误。
   - **能力期**（协议或模型收不下图）→ **in-band 文本占位符降级**，不炸 turn。理由：模型不支持图片是选型事实而非错误；炸 turn 会让一条专科轨拖垮整个 join-barrier，降级把失败留在轨内且模型可见。
5. **降级是兜底不是主路径**：主路径是 `requires.modalities` 的**路由期**门控——拿不到图片能力时该 skill 根本不进 `available_child_skills`。占位符只覆盖门控之外的残余场景（未声明要求、热重载换 client、业务标签漏报）。
6. **图片留在 fco 内部**，不合成 `user_message`。后者会污染五处把 `kind == "user_message"` 当 turn 边界锚点的消费者（`count_turns` 的 `t{k}` 节点号、编排种子定位、resume 重放区间、按轮截断），且在并行工具场景下打断同轮合并、把一次采样劈成两条 assistant（thinking 模型 400）。
7. **投影单一真相**：`_tool_output_content` 同时服务 Chat 与 Responses 两条渲染路径，与 user 消息侧共用取图 / 建 part 的 helper。文本在首项、图片按 attachment 顺序在后；文本为空时不生成空 `TextPart`。
8. **user 侧与工具侧的不对称是有意的**：user 消息遇能力不足**抛错**（用户明确塞了图却看不到 = 输入被吞，必须让调用方知道），工具侧降级（图是 agent 自己取的，留在轨内更合适）。
9. **父 thread 不承载子 thread 的图**：`call_skill` 回传仍是纯字符串，拓扑不变。图只在取图的那条 thread 的 history 内重放——子 thread 因此是天然的视觉沙盒。

## 协议分档

| 档 | provider | 行为 |
| --- | --- | --- |
| 原生 | `openai/responses`、`codex/responses` | fco 的 `output` 投影为 `input_text` / `input_image` 数组，协议原生接受 |
| 降级 | `openai/chat`、`openai_compat`、其余未声明的 | 文本 + in-band 占位符；协议无位置承载图片 |

Anthropic 的 `tool_result` 原生支持内嵌 image block，但该 provider 当前连 image **输入**都未声明能力，故不在本能力范围内，另立。

## 测试接入

- `tests/tool/test_tool_result_attachments.py` —— `ToolResult` 契约位
- `tests/llm/test_image_admission.py` —— `from_bytes` 自洽、`admit_tool_attachments` 五类拒绝
- `tests/loop/test_tool_image_output.py` —— 两条渲染路径 + 降级 + **同轮合并回归锁**
- `tests/llm/test_tool_image_wire.py` —— wire 投影，纯文本保持裸字符串
- `tests/skill/test_skill_visibility.py` —— 模态门控与标签派生
- `tests/loop/test_prompt_image_input.py` —— 门控接线（text-only 隐藏 / responses 可见，互为对照）

CI 全部走 Sim；真实 LLM 回归走 `examples/real_llm/capability_matrix.py`，结果落 `docs/real-llm-ledger.md`。

**真实端点验证**：`examples/real_llm/test_codex_image_matrix.py::codex_tool_image_output`
是本能力的常驻回归场景——历史含 `function_call_output.output = [TextPart, ImagePart]`，
断言走 structured output，要求模型报出**只存在于图内**的几何信息（蓝色三角形）。
它区分「wire 收下了」与「模型看见了」两件不同的事：前者只证明不 400，后者才证明
能力成立。与 `codex_image_tool_call` 方向相反（那条是图片在**输入侧**驱动 function
call）。结果落 `docs/real-llm-ledger.md`。

## R1–R5 影响

| 红线 | 影响 |
| --- | --- |
| R1 | 纯机制，零业务概念。模态标签是开放字符串集；`derive_modality_tags` 只读注入对象**自己的声明**，不内置模型名→能力表 |
| R2 | 附件随 fco 追加在 tail，前缀稳定，不触发 cache 失效。压缩侧只改写 anchor 之后条目 |
| R3 | 附件计数 / 字节 / MIME / sha256 / detail 可暴露；**base64 正文绝不进** telemetry、request capture 与压缩摘要。`audit_redaction` 按 `type=="image"` 形状识别，嵌在 fco 内的 `ImagePart` 自动脱敏 |
| R4 | 不引入长时操作；准入是纯计算 |
| R5 | payload 形状向后兼容（空附件不写键）；`ImageAttachmentV1` 本就是可持久化 canonical 形态，冷恢复重放语义不变 |

## 能力边界（如实记录）

- **只接受完整内联 canonical base64** 的 PNG / JPEG / WebP / 非动画 GIF。不支持 URL、临时路径、file id、音频、视频、PDF、图片生成。
- **无跨 thread 图片共享**：兄弟 skill 之间、join-barrier 聚合轨与专科轨之间不传图。codex 有多 agent 亦不传图；跨 agent 传图属产品编排层，非内核机制缺口。
- **无累计图片预算**：`max_images` 是单次调用的约束，thread 内累计张数靠 `ContextBudget` + 压缩策略被动淘汰，无独立上限旋钮。
- **无图片版 offload**：`OffloadStrategy` 回避带附件条目——图片落盘后 `file_read` 回来是文本、模型看不见，无法兑现其无损可回溯契约。物化↔digest 双向回溯留作后续。
- **strict SessionJournal 不支持**：其原子批形态是「单个 tool_outcome_committed + 唯一 fco 会话项」，附件需要第二条会话项，属能力契约违约 → fail closed。
- **能力判定是 turn 树全局的**：`ModelCapabilities` 挂在 client 类上，而 `SKILL.md` 的 `model` 是 per-skill；两者脱钩，详见 [per-skill 模型绑定草案](../../superpowers/specs/2026-09-01-per-skill-model-binding-design.md)。本能力用路由期门控 + 兜底降级覆盖该错配，未修根因。
