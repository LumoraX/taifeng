# ADR 0071：grep 上下文行 / 跨行 / .gitignore，memory 可选删除协议，max_output_tokens 联动输出预留

- 状态：Accepted（Amends #0064、#0043、#0056）
- 日期：2026-09-29
- 关联：[capabilities/tool-builtins-extended.md § glob / grep、§ memory](../architecture/capabilities/tool-builtins-extended.md)；
  [capabilities/token-accounting-calibration.md](../architecture/capabilities/token-accounting-calibration.md)；
  [context-compression.md § K3 / 上下文 token 计数](../architecture/context-compression.md)；
  [skill-system.md § inference](../architecture/skill-system.md)；ADR 0017 / 0025 / 0038 / 0043 / 0056 / 0064 / 0066

## 背景

1. **grep 用起来要两步**：ADR 0064 的 grep 只输出命中行，模型几乎每次都要再 `file_read` 一次看上下文；逐行匹配也找不到
   跨行的函数签名 / 调用。遍历不认 `.gitignore`，构建产物、生成代码与本地密钥文件（`.env` 这类通常被忽略的文件）挤占
   结果名额并被带进上下文。
2. **记忆只能增不能删**：`MemoryStore` 没有删除语义，模型记错或过时的要点只能留在长期记忆里，下一轮 page-in 继续误导推理。
   ADR 0064 否决 delete 的理由是「需要扩协议、暂无需求」，并约定需求出现后以向后兼容的可选协议扩展。
3. **输出上限与上下文预算脱节**：SKILL.md `inference.max_output_tokens`（ADR 0056）声明了较大的输出上限后，soft / hard
   阈值仍只按 `ContextBudget.output_reserve_tokens`（ADR 0043，默认 0）计算——输入侧按整窗判定，压缩来得太晚，请求带着
   大输出上限发出去直接超窗，只能靠 overflow 自愈多烧一次失败请求。

## 决策

1. **grep 上下文行**：`context_before` / `context_after` / `context`（单侧优先），仅 `content` 模式；重叠 / 相邻区间合并为一组，
   不相邻组之间 `--`，上下文行 `路径-行号-行`（与 `grep -C` / `rg -C` 同形）。上下文行**占 `max_results` 名额**——
   输出总量仍由同一个上限约束，而不是 `max_results × (1 + B + A)`；一组放不下时整组不输出，不留只有前文的残组。
   其他模式或 multiline 下给非零上下文 → `bad_args`（不静默忽略）；`before + after >= max_results` → `bad_args`。
2. **grep 跨行**：`multiline=true` 时整文件 `re.MULTILINE | re.DOTALL` 匹配（CRLF 先规整为 LF），每个匹配输出
   `路径:起始行-结束行:"片段"`：片段用 JSON 字符串（换行转义、无歧义），受 `max_line_chars` 截断；行号按 `splitlines`
   切行，与 `file_read` 的 offset 同口径。
3. **`.gitignore`**：纯 Python 实现常见语法（注释、`!`、尾斜杠、`**`、锚定 `/`，子目录规则优先、被忽略目录不下探），
   不调用 git、不加依赖。glob 与 grep 共用工厂参数 `respect_gitignore`，**默认 True**，理由：
   - 两个工具（ADR 0064）尚未进入任何发布版本，改默认没有兼容成本；
   - 与 ripgrep 及主流编码 agent 的搜索工具默认一致，模型对「搜不到被忽略的文件」有正确预期；
   - 被忽略的路径多是依赖 / 构建产物 / 本地配置，既是噪声也是泄漏面（本地密钥文件常见于 `.gitignore`）；
   - 跳过从不静默：尾注给出被忽略路径数并提示「把被忽略目录作为 `path` 可显式搜索」，显式基点自身不受忽略规则影响。
   不支持的语法（POSIX 字符类等）该行不生效且计数告知；`.git/info/exclude`、全局 excludesFile、沙盒根之上的规则、
   「已跟踪文件不受忽略」等写入契约的「不支持」清单。
4. **病态正则仍不可打断**：Python `re` 无匹配超时；multiline 把最坏情形从单行扩大到整个文件。保持在契约「已知边界」，
   **不引入第三方 regex 库**（内核依赖保持最小，且带超时的引擎会让 `re` 语法兼容性成为新的契约面）。
5. **`ForgettableMemoryStore`**：新增 `runtime_checkable` 可选协议（继承 `MemoryStore`），只多一个
   `async forget(target: str, *, thread_id: str) -> int`。签名依据「模型能表达的删除依据」：`prefetch` 返回的是一段
   文本、没有结构化条目，模型手里只有文本——后端展示在 prefetch 结果里的记忆标识，或该条记忆的原文；解析交后端，
   推荐精确匹配。返回实际删除条数，0 = 没有匹配（正常结果）。
   - `MemoryStore` 不变，既有实现零改动；内核被动路径从不调用 `forget`。
   - memory 工具缺省动作集随 store 能力：可遗忘才出现 `delete`（schema 的 `target` 与描述随之出现），显式启用 `delete`
     而 store 不可遗忘 → 装配期 `ValueError`；想关掉删除的宿主显式传 `actions=("search", "save")`。
   - `delete` 与 `save` 同为改动后端的动作：启用即取 `external_non_idempotent` / `manual` 且串行（后端删除是按标识精确
     删还是按相似度删，内核无从得知，崩溃后交人裁决）。
   - `NullMemoryStore` 刻意不实现 `forget`（继承它的只读知识库不应获得删除入口）；`CompositeMemoryStore` 在有可遗忘子时
     才构造出带 `forget` 的实例（`__new__` 选择私有子类），避免「给了入口却什么都删不掉」；某子失败时其余照做，
     最后抛错写明已删数与失败明细。
   - 协议从 `taifeng.context` 导出，与 `make_memory_tool`（`taifeng.tool.builtins`）同层，不进稳定层（ADR 0066）。
6. **输出预留联动**：本 turn 生效预留 = `max(output_reserve_tokens, entry skill 的 max_output_tokens)`，
   由 `ContextBudget.with_output_reserve` 派生（未声明或不更大时原样返回，默认行为不变）。`TurnRunner.effective_budget`
   按**自己的** entry skill 派生，压缩 soft 预检与交给策略的 `CompressionContext.budget`、预算提示、发送前 hard 预检统一
   读它；`TurnRunner.budget` 保持配置值并原样传给 call_skill / spawn 子 runner，子 turn 各按自己的声明派生，不继承父的
   放大值。声明值 `>= context_window` → `OutputReserveExceedsWindowError`（`ValueError` 子类），turn 在首次预算判定处
   `turn_failed`、不发请求。`CompactNow(target_tokens)` 的临时预算改为 `replace` 配置预算并按生效可用窗口折算比例，
   使 soft_limit 仍恰为 target（此前临时预算丢掉了输出预留等字段）。

## 否决的方案

- **上下文行不占名额**：`max_results=200`、`context=10` 时输出可达 4200 行，工具上限形同虚设。
- **非 content 模式下静默忽略上下文参数**（`rg -l -C3` 的行为）：违背禁 silent fallback；模型收到 `bad_args` 能立刻改对。
- **`respect_gitignore` 默认 False**：兼容最稳，但工具尚未发布、没有需要保护的旧行为；默认关意味着多数宿主永远搜到依赖与
  产物目录，而「搜不到被忽略的文件」已有尾注与显式基点两条出路。
- **调用 `git check-ignore` / `git ls-files`**：引入运行时二进制与仓库前提（沙盒根不一定是 git 仓库），且跨进程取消语义
  要重新对齐——与 ADR 0064 否决外部 rg 同理。
- **引入第三方 `regex` 库（带 timeout）**：见决策 4。
- **扩 `MemoryStore` 协议加 `forget`**：所有既有实现（含业务侧）立即不满足协议；可选协议 + `isinstance` 判定零破坏。
- **按条目 id 删除（`forget(ids: list[str])`）**：`prefetch` 不返回结构化条目，模型拿不到 id；要 id 就得先改 prefetch
  返回形态，破坏面远大于收益。后端想用 id，把 id 渲染进 prefetch 文本即可，`target` 照样能承载。
- **`NullMemoryStore` 提供返回 0 的 no-op `forget`**：所有继承它的只读知识库会自动获得一个永远删不掉的 `delete` 入口。
- **在 `SkillSnapshot` / pool 装配期校验所有 skill 的 `max_output_tokens < context_window`**：窗口可经 `UpdateBudget`
  运行期缩小、skill 可热更，装配期校验既不充分也会让 `pool.py`（已超 800 行）继续变长；运行期在 turn 首次判定处显式失败
  已足够（不发请求、错误里写明 skill 与数值）。
- **把 `output_reserve_tokens` 自动设成 `max_output_tokens` 并写回 engine budget**：会让子 turn 的声明污染父 turn，
  也让 `UpdateBudget` 的语义含混；派生值只在单个 runner 内生效。

## R1–R5 影响（决策 6 涉及压缩触发）

- R1：无业务概念；预留来自业务注入的 `ContextBudget` 与 skill 自己的 `inference` 声明。
- R2：不改压缩策略的 cache 语义，只让 soft / hard 更早越过——压缩更早发生而不是更晚；`CompressionResult` 契约不变。
- R3：沿用 `budget_hint_injected` / `context_budget_exceeded` / `compaction_started` 事件，其中阈值字段按生效预算填；
  预留过大以 `turn_failed{kind="OutputReserveExceedsWindowError"}` 透出。
- R4：纯计算，无阻塞；grep / .gitignore 读取仍在工作线程内，停止信号覆盖新增扫描路径（multiline 每 256 个匹配检查一次）。
- R5：生效预算不持久化，resume 后按 entry skill 重新派生，与冷启动一致。

## 验证

- `tests/tool/test_grep_scan.py`（23 例）：上下文合并 / 相邻不分隔 / 单侧覆盖 / 截断与残组 / 参数拒绝，跨行起止行号、片段、
  CRLF、count / files 模式、片段截断、空匹配行号、停止信号。
- `tests/tool/test_gitignore.py`（47 例）：31 条语法用例、否定与层级优先序、不支持语法计数、读取边界（符号链接 / 超大 /
  非 UTF-8）、遍历剪枝与祖先规则、显式基点、父目录被忽略不可重新纳入、glob / grep 默认开关与尾注。
- `tests/tool/test_memory.py`（+16 例）与 `tests/context/test_memory_composite.py`（+5 例）：delete 按能力出现、副作用分类、
  委托与计数、0 条、非法计数、参数拒绝、后端异常、取消、EnginePool 端到端；组合器可遗忘判定、求和、部分失败显式报错、copy 后保留能力。
- `tests/context/test_token_calibration.py`（+6 例）与 `tests/loop/test_output_reserve_turn.py`（7 例）：`with_output_reserve`
  语义；真实 EnginePool + SimClient 下声明值收紧 soft / hard（压缩咨询、预算提示、hard 预检）、call_skill 子 turn 双向不继承、
  spawn 子 turn 用自己的声明、预留不小于窗口时 `turn_failed` 且零请求、CompactNow target 精确。把 `effective_budget`
  退化为配置预算后其中 5 例失败（回归可检出）。
- 本 ADR 改动了基础层（`context/`、`loop/`），真实 LLM 台账由集成方统一重跑（`examples/real_llm/capability_matrix.py`）。
