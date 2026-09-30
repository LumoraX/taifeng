# ADR 0112：MCP stdio server 经 `CommandExecutor` 启动；持续读走 stderr

- 状态：Accepted
- 日期：2026-09-30
- 关联：Amends #0051；[tool-builtins-extended](../architecture/capabilities/tool-builtins-extended.md)、
  [mcp-client](../architecture/capabilities/mcp-client.md)；ADR 0108

## 背景

ADR 0051 让 `shell_exec` 与 `run_in_background` 经 `CommandExecutor` 启动进程，宿主由此把命令放进容器或
沙盒。MCP stdio server 没有走这条路：`McpStdioClient.spawn` 自己 `create_subprocess_exec`。宿主代为运行
一个第三方 MCP server 时，它就跑在宿主节点上、带着宿主进程的全部环境变量——模型能调的工具里，它反而
是隔离最弱的一个。

`CommandProcess` 也撑不起这件事：协议只有 `communicate()`（一次性收完输出），而 MCP stdio 是持续对话，
要边写 stdin 边读 stdout。

顺带查出一个已发布的缺陷：**server 往 stderr 写得多就会卡死**。客户端把 stderr 接成了管道却从不读，
管道写满（常见实现 64 KiB）后 server 阻塞在写日志上，再也不回应请求。官方 SDK 写的 server 每个请求都
往 stderr 打日志，长连接迟早撞上。

命中 ADR 0017 规则①。

## 决策

1. **`CommandSpec.stdin: bool = False`**。True 表示调用方要向进程持续写入，执行器返回的进程须满足
   `StreamingCommandProcess`；False 表示进程的标准输入是空的。
2. **`StreamingCommandProcess`**：在 `CommandProcess` 之上提供 `stdin` / `stdout` / `stderr` 三个流
   （`CommandInput`：`write` / `drain` / `close` / `is_closing`；`CommandOutput`：`readline` / `read`）。
   `asyncio` 的流天然满足，远端执行器包一层即可。三个协议进稳定层。只会一次性收输出的执行器照旧只
   实现 `CommandProcess`——它们不能用来跑 MCP server，`spawn` 会拒绝并终止那个进程。
3. **`McpStdioClient.spawn(executor=)`**：给出时经执行器启动（`CommandSpec(shell=False, stdin=True)`，argv
   用 `shlex.join` 拼成命令文本、执行器按 `shlex.split` 拆回）；不给时行为不变。
4. **经执行器启动时不给 `env` 就是最小白名单**（`default_safe_env`）。执行器约定 `CommandSpec.env` 是完整
   环境；沿用「不给就继承宿主环境」会把宿主的凭据默认送进沙盒。直接拉起的路径维持原样（继承）。
5. **`LocalCommandExecutor` 对不要 stdin 的命令接 `DEVNULL`**。此前没有指定 stdin，命令继承宿主进程的
   标准输入——宿主是服务进程时，读 stdin 的命令会去抢宿主的输入流或挂住不动。
6. **客户端持续读走 server 的 stderr**，只留 16 KiB 尾部，经 `client.stderr_tail` 取用（排查 server 起不来
   时看它最后说了什么）。两条启动路径都如此。

## 不做

- **把不经执行器的路径也改成最小环境**：现有调用方依赖继承（`npx`、`uvx` 起的 server 要读用户的
  配置与代理变量），改了是破坏性变化。
- **给 `CommandProcess` 本身加流**：已有的执行器实现（一次性收输出的远端实现）会立刻不满足协议。
- **把 server 的 stderr 转成事件**：server 日志的量与内容都不受控；宿主要的话自己读 `stderr_tail` 或在
  执行器里接走。

## 影响

- R1：无业务概念。
- R2、R3、R5：无影响。
- R4：经执行器启动的 server 在 `close()` 超时后被 `kill()`——本机执行器杀的是整个进程组（ADR 0108）。

### 行为变化

- 本机命令的标准输入从「继承宿主」变为空：依赖读宿主 stdin 的命令会立刻读到 EOF。
- MCP server 的 stderr 不再堆在管道里；此前靠管道写满而「碰巧不出错」的场景不受影响，此前会卡死的
  场景恢复正常。
- `McpStdioClient.__init__` 的 `proc` 参数类型放宽为 `StreamingCommandProcess`
  （`asyncio.subprocess.Process` 仍然满足）。

## 验证

- `tests/mcp/test_stdio_executor.py`（8 项）：经注入的执行器启动并完成工具调用、argv 往返不变（含带空格的
  路径）；不给 env 时只有白名单变量、宿主的环境变量不外流；执行器返回没有流的进程 → `TypeError` 且进程被
  终止；执行器启动失败原样上抛；server 往 stderr 写 2 MiB 后握手照常完成（两条路径，修复前直接拉起的
  路径超时）、尾部可取且有上限；本机执行器只在要求时给 stdin，否则读到 EOF。
- `examples/mcp_interop/verify.py`：官方 MCP SDK 2.2.0 的 server 经 `LocalCommandExecutor` 启动后同样通过
  全部检查（三种接法共 33 项）。
