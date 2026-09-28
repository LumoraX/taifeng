# ADR 0060：read_skill 读取 skill 目录内附属文件

- 状态：Accepted
- 日期：2026-09-28
- 关联：[skill-system.md § 注入策略](../architecture/skill-system.md)；ADR 0006（统一 Skill 抽象）/ 0058（严格加载）

## 背景

Agent Skills 范式的渐进加载有三层：元数据（id + description）→ SKILL.md 正文 → 正文引用的附属文件（`references/`、
`FORMS.md` 等）。taifeng 实现了前两层，第三层缺失：`read_skill` 只能返回正文，作者要么把全部细节塞进正文（每次读取都付全量
token），要么额外开放 `file_read` 并自行处理路径与可见性。按 ADR 0017 规则②，这是认知回路里「按需取上下文」原语的缺口。

## 决策

1. `read_skill` 增加可选 `path`：skill 目录内的相对路径；省略时行为不变（返回正文）。
2. 安全边界：绝对路径拒绝；`resolve()` 展开符号链接后必须仍在 skill 目录内；必须是普通文件、UTF-8 文本、≤ 256KiB；
   可见性与读正文同一规则（当前 entry 可达图）。错误以 `ToolResult.error` + `reason` 返回，不抛出。
3. 读文件放到工作线程（`anyio.to_thread`），不阻塞事件循环。

## 否决的方案

- **读取正文时附上目录文件清单**：会改变所有 skill 的 `read_skill` 输出（含 scripts 目录），正文本就负责引用它需要的文件。
- **`allowed-tools` 作为 `tool_names` 的别名**（review 时一并提出）：语义不同——Agent Skills / Claude Code 里它表示「使用这些
  工具时免审批」，`tool_names` 是 composite 的可见工具白名单；工具名也不通用（`Bash` ≠ `shell_exec`），而 atomic 声明
  `tool_names` 会直接校验失败。映射只会让生态 skill 以错误语义加载。该字段已原样保留在 `frontmatter_raw`，宿主可映射到
  `PermissionPolicy`。

## 验证

`tests/tool/test_read_skill.py`（11 例）：无 path 取正文、读附属文件、`..` / 嵌套 `..` / 绝对路径 / 符号链接逃逸拒绝、
缺失 / 目录 / 二进制 / 超限拒绝、不可见 skill 的附属文件不可读。
