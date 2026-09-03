# ADR 0028：权限模型以「效果」为正典，`tool_use` 仅作兜底

- 状态：Accepted
- 日期：2026-09-03
- 关联：[permission-gate 能力契约](../architecture/capabilities/permission-gate.md)；openspec change `wave1-security-hardening`

## 背景

2026-09-03 全系统审查发现：Claude Code 风格的 Style A 规则（`Bash(rm -rf *)` / `FileRead(/etc/*)`）**对内置工具永远不匹配**，`{"deny": ["Bash(rm -rf *)"]}` 形同虚设。根因不是单个 bug，而是仓库里同时存在两种权限模型：

- **运行时**（`shell_exec` / `file_read` / `file_write` / `http_request` / `run_script` / `call_skill`、`from_capability_tier`）早已按**效果**发请求：`scope="shell_exec", target=<完整命令>`、`scope="file_read", target=<解析后绝对路径>`、`scope="network", target="<METHOD> <URL>"`。
- **契约与别名表**（permission-gate.md §PermissionRequest、`_PERMISSION_ALIAS_TABLE`）停留在早期 `for_tool_call` 设计：`scope="tool_use", target=<工具名>, metadata["args"]` + `args_match`。别名表甚至写 `args_key: cmd`，而 `shell_exec` 的参数名叫 `command`——说明这条路径从未接过真实请求。

契约自身也已矛盾：同一文档第 165 行 MCP prompter 场景用的就是 `scope="shell_exec"`。

## 决策

**效果模型（effect-based）为内核权限模型的正典**：

1. `scope` 表达**做了什么类型的事**（`shell_exec` / `file_read` / `file_write` / `network` / `script_exec` / `skill_dispatch` / `compaction`），`target` 表达**规范化后的作用对象**（完整命令串 / 解析后绝对路径 / `"<METHOD> <URL>"` / `"<skill_id>/<script_name>"` / skill id）。
2. `tool_use` **只是兜底**：给没有更细效果类型的工具（业务经 `for_tool_call` 自注册的工具）使用，`metadata["args"]` + `PermissionRule.args_match` 是它的配套机制。内置效果类工具 MUST NOT 以 `tool_use` 形状发请求。
3. Style A 别名一律映射到效果 scope，payload 即 `target_pattern`，不产出 `args_match`：`Bash`/`ShellExec`→`shell_exec`、`FileRead`→`file_read`、`FileWrite`→`file_write`、新增 `Network`→`network`（省略 method 时前缀 `"* "` 使任意 method 命中）、`Skill`/`Script` 不变。
4. 过渡例外：`apply_patch` 目前发 `tool_use/apply_patch` 且不带路径——效果模型下它应按路径发 `file_write`。记入 backlog，`ApplyPatch(p)` 别名暂保留为 `tool_use`。

## 为什么是效果模型

| 维度 | 效果模型 | 工具模型（tool_use + args） |
| --- | --- | --- |
| 参照 | OS 级权限：Deno `--allow-read=<path>` / `--allow-net=<host>`、Android 权限、seccomp / Landlock、codex `SandboxPolicy` | 应用级：Claude Code `Bash(cmd)` / `Read(path)` |
| 与工具解耦 | 新工具只要发同一效果（写文件 → `file_write` + 路径），既有规则自动覆盖 | 规则与工具名 + 参数名耦合，每个工具各写一份 |
| 匹配对象 | 规范化值（绝对路径、拼好的命令串）—— `path="../x"` 与 `"/abs/x"` 同一规则处理 | 原始 args dict，`cmd` 还是 `command`、相对还是绝对全靠规则作者猜 |
| 一工具多效果 | `apply_patch` 读 + 写 + 删可按路径分别发 | 一条规则表达不了 |
| Prompter 展示 | 「要写 /etc/hosts」 | 「要调 apply_patch，参数 {...}」 |

taifeng 定位是 LLM OS 内核（ADR 0017），权限属于内核机制；OS 级的标准做法就是按效果授权。运行时已经站在这一边，本 ADR 只是把它写成标准并让契约与语法糖归位。

## 替代方案

**改内置工具对齐 `for_tool_call` 形状**（`scope="tool_use", target=<tool_name>, metadata.args`）：契约文档不用动，但会破坏所有按 `scope="shell_exec"` 写的 Style B 规则与自定义 Prompter 展示，`from_capability_tier` 也要重写，且工具模型本身可扩展性更差（上表）。否决。

## 后果

- **契约层 BREAKING**：`PermissionRule.parse("Bash(x)")` 返回形状从 `tool_use` + `args_match` 变为 `shell_exec` + `target_pattern`。src 内无消费者依赖旧形状；解析测试随契约改。
- 业务若曾写 Style A deny 规则：修复后**开始真正生效**，可能拦住此前意外放行的命令——属预期，发布说明写明。
- 运行时 `PermissionRequest` 形状零变化；Style B 规则、Prompter、grant 不受影响。
- backlog：`apply_patch` 改按路径发 `file_write`，完成后 `ApplyPatch` 别名并入 `FileWrite`。
- 新增效果类工具时的义务：发对应效果 scope 的请求，并在 permission-gate.md 的 scope 表登记 target 形状。
