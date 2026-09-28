# ADR 0051：shell 类工具经 CommandExecutor 启动进程（沙箱 seam 统一）

- 状态：Accepted
- 日期：2026-09-28
- 关联：[capabilities/tool-builtins-extended.md](../architecture/capabilities/tool-builtins-extended.md)；ADR 0009（scripts runtime / ScriptExecutor）；ADR 0017 规则③

## 背景

脚本运行时早有可插拔的 `ScriptExecutor`，但 `shell_exec` 与 `run_in_background` 直接
`asyncio.create_subprocess_*`：宿主想把命令放进 Docker / firejail / 远端沙箱只能整体重写工具，
重写时又容易丢掉审批、黑名单、白名单 env、超时这些内核保证。核实时还发现 `shell_exec` 不响应
`ctx.cancel`，turn 被取消 / 截止时间到后子进程要跑满超时。

## 决策

1. **只抽「启动」一步**：`CommandExecutor.start(CommandSpec) -> CommandProcess`。进程接口取
   `asyncio.subprocess.Process` 已有的四个成员，本机实现零包装，远端实现包一层即可。审批、黑名单、
   env 白名单、超时、截断、取消留在工具里——执行器换了，这些保证不变。
2. **不做沙箱实现**（规则③）：内核只给 seam + 本机默认；隔离后端由宿主或独立扩展包提供
   （参照 deepagents 把 sandbox 做成 partner 包）。
3. **顺带补 R4**：`shell_exec` 在 `interrupt_on_cancel` 内等待，取消即 kill 子进程。
