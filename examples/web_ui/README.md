# Taifeng Web UI Demo

类似 openclaw 的 chat + 数据流可视化最简实现。顶部下拉一键切换 **15 套** skill 演示包，浏览器实时看 LLM agent 的 EventMsg、tool call、HITL 审批、会话级可观测指标，并可续接历史会话。

web_ui 的设计原则：**它只是一个「基础能力」展台 —— 不重写任何 skill，直接加载各独立 demo（`examples/<name>/skills`）的现成 skill 目录**。每加一种内核用法，就在 [server.py](server.py) 的 `DEMOS` 里注册一项（必要时给 `DemoMeta` 加一个旋钮），把该用法「汇总」进来。

## 内置 demo 包（15）

按演示主题分组。每个 demo 内部独立 `EnginePool`（lazy 创建）+ 独立权限策略 + 独立持久化目录；切换下拉即换 demo，UI 自动清屏 + 重连 SSE + 重置指标。

### A. 多 agent 拓扑 / 编排

| Demo | 入口 skill | 演示什么 | HITL? |
| --- | --- | --- | --- |
| 🔒 **code_review** | `programmer` | call_skill → code-review 派发，HITL 弹窗底座 | ✅ 全弹 |
| 🧳 **travel_planner** | `trip-planner` | LLM 自主 fan-out 三路（航班/酒店/活动）+ 综合按日行程 | ❌ 静默 |
| 🧩 **orchestration** | `trip-planner` | **声明式编排**（SKILL.md 的 parallel/serial/when）确定性驱动子 skill | ❌ 静默 |
| ⚡ **concurrent_fanout** | `research-fanout` | **LLM 自主并发**：一条消息里多个 call_skill 并行派发（对照 orchestration 的声明式）| ❌ 静默 |
| 🔬 **research_assistant** | `research-lead` | sequential pipeline ①→②→③（source → fact → outline），上步输出=下步输入 | ❌ 静默 |
| 📋 **product_review** | `product-manager` | fan-out 3 reviewer（设计/工程/测试）+ 评分聚合 + 通过/驳回 | ✅ 全弹 |

### B. skill / context 机制

| Demo | 入口 skill | 演示什么 | HITL? |
| --- | --- | --- | --- |
| 📖 **read_skill_lazy** | `knowledge-router` | `read_skill` 懒加载（skill-as-context）：子 skill 正文按需注入、不预先进 prompt | ❌ 静默 |
| 🎯 **numeric_loop** | `numeric-tuner` | LLM 多轮 `run_script(apply_delta)` 调谐数值（同 skill 内反复调工具） | ❌ 静默 |
| 🗜️ **compression_showcase** | `chatty-assistant` | **极小 1k context window + SlidingWindowStrategy**，聊 2-3 轮触发自动压缩 | ❌ 静默 |

### C. 权限 / 隔离 / 指令（内核旋钮）

| Demo | 入口 skill | 演示什么 | HITL? |
| --- | --- | --- | --- |
| 🛡️ **permission_showcase** | `programmer` | 复用 code_review skills，覆盖策略 default_mode=allow + 红线 deny | ❌ 静默 |
| 🔐 **selective_approval** | `analysis-orchestrator` | 同 turn 派发 prd-evaluator（白名单 allow）+ swot-evaluator（ask）——按 skill 精细授权 | ⚖️ **部分** |
| 🛡️ **subagent_isolation** | `programmer` | `DispatchPolicy.subagent_approval_mode=auto_deny`：子 turn 内 ask 自动转 deny + emit `subagent_policy_overridden` | ❌ 静默 |
| 📋 **instructions** | `programmer` | 注入一层 `InstructionLayer`（house-style），合进 system_prompt 影响全程输出风格 | ❌ 静默 |

### D. infra 体现（进程内钩子 / 跨进程 MCP）

| Demo | 入口 skill | 演示什么 | HITL? |
| --- | --- | --- | --- |
| 🪝 **hooks_showcase** | `task-runner` | 业务钩子 `pre/post_skill_dispatch` 按**运行时 args** 拦截（scope=all 拒、recent 放）→ emit `skill_dispatch_hook_denied` | ❌ 静默 |
| 🔌 **mcp_showcase** | `market-assistant` | taifeng 作为 **MCP client**：spawn 外部 MCP server 子进程 + 注册其工具远程调用（跨进程）| ❌ 静默 |

**覆盖的 agent pattern**：
- **拓扑**：并行收敛（`travel_planner`）/ 声明式编排（`orchestration`）/ LLM 自主并发（`concurrent_fanout`）/ 严格串行（`research_assistant`）/ 并行+决策（`product_review`）
- **HITL / 权限四态**：全弹（`code_review`）/ 按 skill 精细（`selective_approval`）/ 全通过+红线（`permission_showcase`）/ 子 turn 收紧（`subagent_isolation`）
- **context 机制**：懒加载（`read_skill_lazy`）/ 多轮工具（`numeric_loop`）/ 自动压缩（`compression_showcase`）
- **扩展点**：指令分层（`instructions`）/ 进程内钩子（`hooks_showcase`）/ 跨进程 MCP（`mcp_showcase`）

## 全局能力（贯穿所有 demo，非单个 demo）

这三项不是某个 demo 专属，而是 web_ui 对所有 demo 生效的基础能力：

- **🔭 会话级可观测面板**（右侧事件流上方）：把流经的每条 EventMsg 实时聚合成指标网格 —— `events / turns(+子) / tools(✗err) / skills / hitl / 拦截 / 压缩 / 失败 / tok↓↑ / cache%`。区别于顶部 loop-pill（每 turn 重置、只反映「这一轮」），可观测面板跨整个会话累计；拦截/失败标红、tool 出错标橙。clear / 切 demo / 切 session 时归零。
- **🕑 历史会话续接（R5 resume）**：header 的「历史」下拉列出该 demo 已持久化的 thread（标题取首条 user 消息 + 条数 + 时间）。选中即载入其对话并进入续接态（session 切 `resume:<tid>`、后端从 JSONL 物化历史续聊）；选「🆕 新会话」回到全新对话。证明对话持久化 + 跨会话可恢复（即便 server 重启）。
- **📊 ctx 占比 pill**（顶部 loop-pill）：当前 session context = 最后一次 root turn 的 input_tokens，按 LLM 真实 context window 渲染 `N.Nk / Mk (X%)`，分档变色（<60% 绿 / 60–85% 橙 / ≥85% 红）。

## UI 视觉

- **顶部**：demo 下拉 + session id + 「历史」会话下拉 + “填入示例”按钮 + demo 描述 + loop-pill（含 ctx 占比）+ SSE 连接状态
- **左侧 Chat**：user 蓝气泡 / assistant 绿气泡 / tool call+result 卡片
- **右侧**：顶部「会话级可观测指标网格」+ 下方「`EventMsg` 时间轴」，按 kind 上色（`tool_call_started` 黄、`skill_dispatched` 紫、`hitl_required` / `*_hook_denied` 红、`turn_completed` 绿…）
- **HITL Modal**：scope/target/reason/entry_skill/call_chain 全部展示，**允许 / 拒绝** 两键

## 技术要点

- **HITL 零 MCP**：审批走 SDK 原生 `CallbackPrompter`（不经 MCP）。`PermissionRequest` 通过 SSE 推前端，前端 POST 回包 `set_result(future)` 让 prompter 返回。（唯一用到 MCP 的是 `mcp_showcase`，且是 **client 方向**：连外部 MCP server 取工具，与 HITL 无关。）
- **每 demo 一个 prompter 闭包**：HITL 事件只推到该 demo 的订阅者，跨 demo 不串扰。
- **SSE 全量转发**：桥接层用 `engine.subscribe_all()` 把内核**所有** EventMsg 透到浏览器（按 submission_id 过滤、按 `is_root` 判退）——可观测面板与时间轴都吃这一条流，新增事件类型零改动即可见。
- **SSE 单向流**：浏览器原生 `EventSource`，零客户端依赖；15s 心跳防代理切连接。
- **静态 HTML**：单文件 `static/index.html`，无 build step、无 npm。
- **`DemoMeta` 旋钮化**：每种内核用法是 `DemoMeta` 上的一个可选字段 —— `permission_rules` / `policy_config_overrides` / `context_window_override` / `use_sliding_compressor` / `subagent_approval_mode` / `instruction_layers` / `hook_runner_factory` / `mcp_connect`。加 demo = 填一项，不改主流程。
- **`PermissionPolicy.from_dict`** —— 见 [server.py](server.py) `_make_policy()`，业务侧可用 Claude Code 风格的 dict / JSON 直接喂规则。

### 权限规则示例

**Style A（语法糖）—— 推荐**：

```python
PermissionPolicy.from_dict({
    "default_mode": "allow",
    "deny": [
        "Bash(re:^rm\\s+-rf\\s+/)",   # rm -rf / 强拒
        "Bash(re:^sudo\\b)",
    ],
    "ask": [
        "Skill(re:^(?!read_).+)",      # 子 skill 派发到非 read_* → 弹窗
    ],
    "allow": [
        "Bash(openspec *)",            # openspec 命令全部放行
        "FileRead(/data/*)",
    ],
}, prompter=my_prompter)
```

**Style B（明文规则）—— 业务从 DB / config 加载时常用**：

```python
PermissionPolicy.from_dict({
    "default_mode": "ask",
    "rules": [
        {"scope": "tool_use", "target_pattern": "shell_exec",
         "args_match": {"cmd": "re:^openspec\\s"}, "mode": "allow",
         "reason": "ops_safe_subcommands"},
    ],
}, prompter=my_prompter)
```

支持的 alias：`Bash` / `ShellExec` / `Skill` / `Script` / `FileRead` / `FileWrite` / `ApplyPatch`。pattern 三态：字面 / `re:` 正则 / `glob:` 通配（payload 含 `*`/`?` 时自动加 `glob:` 前缀）。

## 运行

```bash
# 1. 装运行时依赖（fastapi + uvicorn 在 dev group，demo only）
uv pip install -e ".[dev,litellm]"

# 2. .env（任一位置）
#   - taifeng/.env
#   - ../api/.env

# 新形态（推荐）—— 通过 provider 切 native client，多 provider 共用一套 env
LLM_BOOTSTRAP_PROVIDER=openai            # 可选：openai|anthropic|gemini|deepseek
LLM_BOOTSTRAP_API_KEY=sk-...
LLM_BOOTSTRAP_MODEL=gpt-4o-mini          # 可选；按 provider 给合理默认
LLM_BOOTSTRAP_BASE_URL=https://...       # 可选；仅 openai-compat 网关需要

# 旧形态（向后兼容）—— 等价于 PROVIDER=openai，已有部署不必动
# LLM_BOOTSTRAP_OPENAI_API_KEY=sk-...
# LLM_BOOTSTRAP_OPENAI_MODEL=gpt-4o-mini
# LLM_BOOTSTRAP_OPENAI_BASE_URL=https://...

# 3. 跑
cd taifeng
PYTHONPATH=src uv run python examples/web_ui/server.py
# 浏览器 http://localhost:8765
```

## 多 provider 支持

server 启动时会按 `LLM_BOOTSTRAP_PROVIDER` 走对应 native client：

| provider | client | 默认模型 |
| --- | --- | --- |
| `openai`（默认） | `OpenAICompatClient` | `gpt-4o-mini` |
| `anthropic` | `AnthropicClient` | `claude-haiku-4-5-20251001` |
| `gemini` | `GeminiClient` | `gemini-2.0-flash-exp` |
| `deepseek` | `DeepSeekClient` | `deepseek-chat` |

底层走 `examples/_provider_bootstrap.py` 共享 helper —— 所有 `examples/` 下的 demo 一致复用同一套 env 形态。

## 添加你自己的 demo

在 [server.py](server.py) 的 `DEMOS` dict 里追加一项：

```python
"my_demo": DemoMeta(
    demo_id="my_demo",
    title="🚀 我的 demo",
    description="一句话说做什么",
    skills_dir=EXAMPLES_DIR / "my_demo" / "skills",
    entry_skill_id="my-entry-skill",
    sample_prompt="点 “填入示例” 后写到 input 框的请求",
    hitl_on_skill_dispatch=True,  # call_skill 是否弹 HITL
),
```

skills 目录满足 SKILL.md 标准（至少一个 `entry: true`）即可 —— **复用已有独立 demo 的 skill 目录，不必为 web_ui 重写**。重启 server，下拉自动出现。

要演示某个内核旋钮，再加对应 `DemoMeta` 可选字段即可（无需改主流程）：

| 想演示 | 加这个字段 | 参考 demo |
| --- | --- | --- |
| 自定义权限策略 | `policy_config_overrides` / `permission_rules` | permission_showcase / selective_approval |
| 上下文压缩 | `context_window_override` + `use_sliding_compressor` | compression_showcase |
| 子 turn 审批模式 | `subagent_approval_mode` | subagent_isolation |
| 指令分层注入 | `instruction_layers` | instructions |
| 进程内业务钩子 | `hook_runner_factory` | hooks_showcase |
| 连接外部 MCP server | `mcp_connect` | mcp_showcase |

## 想验证什么

| 场景 | 怎么验 |
| --- | --- |
| HITL **允许** | code_review → 弹窗点允许 → `skill_dispatched` → 子 turn 完成 → 主 turn 收 review 报告 |
| HITL **拒绝** | code_review → 弹窗点拒绝 → `skill_dispatch_permission_denied` → LLM 收 deny reason 兜底回复 |
| 权限策略对照 | permission_showcase → 同 skills 但 default_mode=allow → skill 派发"无声通过"，时间轴无 HITL 事件 |
| 按 skill 精细授权 | selective_approval → 同一 turn 内 prd-evaluator **静默**（白名单）+ swot-evaluator **弹窗**（ask），鲜明对比 |
| 自动上下文压缩 | compression_showcase → context_window=1024 + SlidingWindowStrategy → 聊 2-3 轮看到 `compaction_started` / `compaction_completed` 事件 + history 中段被 placeholder 替换 |
| 多轮工具调用 | numeric_loop → 看时间轴 `tool_call_started/completed` 重复 10+ 次 → 收敛后 `turn_completed` |
| 并行 fan-out + 综合 | travel_planner → 一次 turn 内 3 次 `skill_dispatched`（flights / hotels / activities）→ planner 综合按日行程 |
| Sequential pipeline | research_assistant → 时间轴 3 次 `skill_dispatched` **严格有先后**（source → fact → report），上步输出 = 下步输入 |
| 多维评审 + 评分聚合 | product_review → 3 次 fan-out 各弹一次 HITL → 三份评分 JSON → PM 综合输出"通过 / 修改 / 驳回" |
| 声明式 vs 自主并发 | orchestration（SKILL.md 声明确定性驱动）对比 concurrent_fanout（LLM 临场把多个 call_skill 放进一条消息并发派发） |
| skill 懒加载 | read_skill_lazy → 时间轴出现 `read_skill` 取子 skill 正文（不派发子 turn），省 token |
| 子 turn 权限收紧 | subagent_isolation → 父放行、子 turn 内 ask 自动转 deny → 时间轴 `subagent_policy_overridden` |
| 指令分层注入 | instructions → house-style 层合进 system_prompt，观察输出是否遵循（P0/P1/P2 + 风险评级） |
| 业务钩子按 args 拦截 | hooks_showcase → scope=all 被 `skill_dispatch_hook_denied`、改 scope=recent 放行 |
| 跨进程 MCP 工具 | mcp_showcase → 时间轴 `lookup_stock_price` / `convert_currency` 工具调用，结果来自 MCP server 子进程 |
| 子 skill 文档读取 | 任何 demo → `read_skill` 调用静默放行（不弹窗） |
| 会话级可观测 | 任意 demo 多聊几轮 → 右上指标网格累计 turns/tools/skills/拦截/压缩/tok/cache% |
| 历史会话续接（resume）| 聊完 → 「历史」下拉选该 thread → 载入对话续聊（server 重启后仍可恢复，证明持久化）|
| 跨 session 隔离 | 改 session id 输入框 → 同 demo 不同 session 各自维护独立对话历史 |
| 跨 demo 隔离 | 切换下拉 → 各 demo 用独立 EnginePool + storage（`.runs/<demo_id>/`） |

## 文件

```
examples/web_ui/
├── README.md          本文件
├── server.py          FastAPI app（15 个 DEMOS 注册 + per-demo pool + SSE 桥接
│                       + HITL + /api/threads resume 端点 + MCP client 生命周期）
├── static/
│   └── index.html     单文件 UI（demo 下拉 + 历史下拉 + chat + 可观测面板
│                       + 时间轴 + HITL modal）
└── .runs/<demo_id>/   每个 demo 独立 JSONL 落盘（.gitignore 自动排除）
```
