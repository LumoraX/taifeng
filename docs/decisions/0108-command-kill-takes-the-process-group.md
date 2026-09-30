# ADR 0108：本机命令按进程组终止

- 状态：Accepted
- 日期：2026-09-30
- 关联：Amends #0051；[tool-builtins-extended](../architecture/capabilities/tool-builtins-extended.md)

## 背景

`shell_exec` 被取消或超时、后台任务被 kill 时，工具调用进程对象的 `kill()` 再 `wait()`。本机执行器
返回的是 `asyncio.subprocess.Process`，`kill()` 只杀它自己——对 shell 模式就是只杀 shell。

shell 执行复合命令（`a; b`、`a && b`、`a &`、管道）时会 fork 出子进程，并把 stdout / stderr 留给它。
shell 被杀之后子进程还活着、还占着管道，于是：

- **孤儿进程**：被取消的命令其实还在跑，直到自己结束。所有 Python 版本都如此。
- **取消不返回**：Python 3.12 的 `Process.wait()` 要等管道全部关闭才返回，取消因此要等子进程自己跑完；
  3.13 在进程退出时即返回。main 上 `test_shell_exec_cancel_kills_process_promptly` 自 2026-09-29 起在
  Linux + Python 3.12 的 CI 上持续失败（CI 镜像的 `sh` 对单条命令也会 fork；macOS 的 `sh` 直接 exec，
  所以本机与 3.13 都看不到），`v2026.9.30.1` 带着这个缺陷发出。

`skill/scripts/shell.py` 跑脚本时早已按进程组终止，`CommandExecutor` 这条路径漏了。

命中 ADR 0017 规则①（R4 可取消不成立）。

## 决策

1. **`CommandProcess.kill()` 的约定是「这条命令连同它派生的进程一起结束」**，并且在已经结束的进程上
   调用不报错。这是对执行器实现方的要求：容器 / 远端沙箱的实现要杀的是整个容器内的命令树。
2. **`LocalCommandExecutor` 让每条命令自成一个进程组**（`start_new_session=True`，PID 即进程组 ID），
   返回的进程对象 `kill()` 时 `killpg(SIGKILL)`；组已不存在或无权按组杀时退回只杀主进程。
3. **输出收完之后 `kill()` 是空操作**。`communicate()` 返回意味着管道已到 EOF、进程已退出，没有成员
   需要杀；此后进程组 ID 可能被系统挪作他用，不再对它发信号。

## 不做

- **只修测试**（把命令换成不 fork 的写法）：缺陷是真的，被取消的命令不该继续跑。
- **在工具里直接 `os.killpg`**：工具不知道执行器把进程放在哪；终止语义属于执行器返回的进程对象。
- **先 SIGTERM 再 SIGKILL 的宽限期**：取消要求立即返回；需要优雅收尾的命令应自己处理，或由宿主的
  执行器实现。

## 影响

- R1：无业务概念。
- R2–R3：无影响。
- R4：`shell_exec` 的取消 / 超时、后台任务的 kill / shutdown 对派生出子进程的命令同样立即生效。
- R5：无影响。

### 行为变化

- 本机命令不再与宿主进程同一个会话 / 进程组：终端里按 Ctrl-C 产生的 SIGINT 不会直接送到这些命令，
  它们随 turn 取消或 pool 关闭被终止。
- 被取消、超时或被 kill 的命令不再留下继续运行的子进程。想让进程活过这条命令（守护进程）须自行
  脱离进程组（`setsid`）。
- `LocalCommandExecutor.start` 返回的是满足 `CommandProcess` 的包装对象，不再是
  `asyncio.subprocess.Process` 本身。

## 验证

`tests/tool/test_command_executor.py` 新增 6 项：取消 / 超时时子进程一并结束且调用在 5 秒内返回；后台任务
kill / shutdown 不留孤儿；输出收完后再 kill 不报错；两种模式返回的对象都满足 `CommandProcess`。修复前
这些用例在 Python 3.12 上等满 30 秒、在 3.13 上留下孤儿进程；修复后两个版本全量
`pytest tests/` 均为 3816 passed, 17 skipped。
