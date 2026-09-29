# tool-builtins-extended Specification

## Purpose
TBD - created by archiving change tool-builtins-extended. Update Purpose after archive.
## Requirements
### Requirement: apply_patch 工具原子化结构化补丁应用

系统 SHALL 提供 `taifeng.tool.builtins.make_apply_patch_tool(*, root_dir, policy=None, max_bytes=1MB) -> ToolSpec` 工厂。返回的 `ToolSpec`：

- `name = "apply_patch"`
- `parallel_safe = False`
- 输入 schema：`{"patches": [<PatchSpec>, ...]}`，其中 `PatchSpec` 三选一：
  - **edit**: `{"path": str, "old_text": str, "new_text": str}`
  - **create**: `{"path": str, "new_text": str, "create": true}`
  - **delete**: `{"path": str, "delete": true}`

handler SHALL 实现**两阶段原子语义**：

1. **phase 1 (dry run)**：遍历所有 patch 校验：
   - path SHALL 落在 `root_dir` 沙盒内（与 `file_io.py::_resolve_safe` 同语义）
   - PatchSpec 字段互斥：edit / create / delete 三选一；多选或全空 → 失败
   - edit 类：`old_text` 在文件中**恰好出现 1 次**
   - create 类：path **不存在**
   - delete 类：path **存在**
2. **phase 2 (apply)**：phase 1 全部通过后才执行；每个 patch 走 atomic write（tmp + os.replace）或 unlink

任一 phase 1 校验失败 SHALL 返回 `ToolResult.error` 且 **0 文件被改**。

#### Scenario: edit 成功修改文件
- **WHEN** 沙盒内 `foo.py` 含 `def f(x): return x`
- **AND** LLM 调 `apply_patch({"patches": [{"path": "foo.py", "old_text": "def f(x): return x", "new_text": "def f(x): return x + 1"}]})`
- **THEN** SHALL 返回 ToolResult.ok，data 含 `{"applied": 1}`
- **AND** 文件内容 SHALL 等于 `def f(x): return x + 1`

#### Scenario: create 新建文件
- **WHEN** 沙盒内不存在 `new.py`
- **AND** LLM 调 `apply_patch({"patches": [{"path": "new.py", "new_text": "hello", "create": true}]})`
- **THEN** SHALL 写入文件，内容 `hello`

#### Scenario: delete 删除文件
- **WHEN** 沙盒内存在 `obsolete.py`
- **AND** LLM 调 `apply_patch({"patches": [{"path": "obsolete.py", "delete": true}]})`
- **THEN** 文件 SHALL 被删除；ToolResult.ok

#### Scenario: 第 N 个 patch 校验失败时整体回滚
- **WHEN** patches=[edit-A, edit-B]，edit-A 校验通过，edit-B 的 old_text 不存在
- **THEN** SHALL 返回 ToolResult.error（reason 含 `patch_validation_failed`）
- **AND** 文件 A SHALL **未被修改**（原子语义）
- **AND** error message SHALL 包含 edit-B 的 path 与失败原因

#### Scenario: old_text 多次出现拒绝
- **WHEN** 文件含 `x` 出现 3 次
- **AND** patch `old_text="x"`
- **THEN** SHALL 失败，reason 含 `ambiguous_old_text`，metadata.occurrences == 3

#### Scenario: sandbox violation 拒绝
- **WHEN** patch `path="../../etc/passwd"`
- **THEN** SHALL 失败，reason 含 `sandbox_violation` 或等价描述

#### Scenario: create 已存在的 path 拒绝
- **WHEN** path 已存在的文件被请求 create
- **THEN** SHALL 失败，reason 含 `path_exists`

### Requirement: 子进程默认继承最小 env 白名单

派生子进程的三条路径（`shell_exec` / `run_in_background` / `ShellScriptExecutor`）SHALL 共用 `tool/subprocess_env.py` 的同一份白名单实现（`PATH` / `HOME` / `LANG` + 强制 `LC_ALL=C.UTF-8`）。`env` 参数缺省时 SHALL 使用该白名单，SHALL NOT 继承宿主完整 `os.environ`——否则子进程可读 API key 等全部凭据。需要额外变量的业务 SHALL 显式传入 `env`（此时白名单不再叠加）。

#### Scenario: 默认不泄漏凭据
- **WHEN** 宿主环境含 `LLM_BOOTSTRAP_API_KEY`，业务未传 `env`
- **THEN** 子进程环境 SHALL NOT 含该变量，且 SHALL 含 `PATH`

#### Scenario: 显式 env 原样生效
- **WHEN** 业务显式传入 `env`
- **THEN** 子进程 SHALL 使用该 env

### Requirement: 命令执行器 seam（sandbox-seam，ADR 0051）

`shell_exec` 与 `run_in_background` SHALL 经 `taifeng.tool.command_executor.CommandExecutor` 启动子进程，
不直接调 `asyncio.create_subprocess_*`：

| 符号 | 含义 |
| --- | --- |
| `CommandSpec(command, shell, cwd, env)` | 一次执行请求；`env` 为工具按白名单构造的**完整**环境 |
| `CommandExecutor.start(spec) -> CommandProcess` | 启动并立即返回；失败抛 `OSError` |
| `CommandProcess` | `returncode` / `communicate()` / `kill()` / `wait()`（`asyncio.subprocess.Process` 天然满足） |
| `LocalCommandExecutor` | 默认：本机子进程（`shell=True` → shell，否则 `shlex.split` + exec） |
| `make_shell_exec_tool(executor=)` / `BackgroundTaskRegistry(executor=)` | 注入点；None = 本机 |

权限审批、黑名单、超时、输出截断、取消仍由工具统一负责——换执行器不改变这些语义。`shell_exec` SHALL
在 `interrupt_on_cancel(ctx.cancel)` 内等待子进程：turn 取消 / 截止时间到点即 kill 并返回
`cancelled (<reason>)` 错误结果；外部 task 取消照常外抛。argv 模式下 `shlex` 解析失败归为 `spawn_error`。

### Requirement: BackgroundTaskRegistry 进程内管理

系统 SHALL 提供 `taifeng.tool.builtins.BackgroundTaskRegistry`：

- `__init__(*, max_concurrent: int = 16)`
- `async spawn(command, *, cwd=None, env=None, max_output_bytes=64*1024) -> str`：返回 task_id 形如 `bg_<8hex>`
- `async wait(task_id, *, timeout: float | None = None) -> dict`：返回 `{"status": "completed"|"timeout"|"unknown", "exit_code": int|None, "stdout": str, "stderr": str}`
- `async kill(task_id) -> bool`：杀子进程，返回是否找到并 kill 成功
- `async shutdown() -> None`：kill 所有 + 清空 registry；幂等

并发上限：`spawn` 时若当前活跃 task 数 ≥ `max_concurrent` SHALL 抛 `RuntimeError("too_many_background_tasks")`，不静默丢弃。

未知 task_id 的 `wait` 调用 SHALL 返回 `{"status": "unknown", ...}` 而非抛错（业务可重试）。

#### Scenario: spawn + wait 完整生命周期
- **WHEN** `registry.spawn("echo hello")` 拿到 `task_id`
- **AND** `registry.wait(task_id)` 在 5s 内
- **THEN** SHALL 返回 `{"status": "completed", "exit_code": 0, "stdout": "hello\n", "stderr": ""}`

#### Scenario: 超时返回 status=timeout 不杀进程
- **WHEN** `registry.spawn("sleep 5")` 拿到 `task_id`
- **AND** `registry.wait(task_id, timeout=0.2)`
- **THEN** SHALL 返回 `{"status": "timeout", "exit_code": None, ...}`
- **AND** task 仍在 registry 中（可后续再 wait 或 kill）

#### Scenario: shutdown 终止所有任务
- **WHEN** registry 有 3 个 running task
- **AND** 业务侧调 `registry.shutdown()`
- **THEN** 所有子进程 SHALL 被 kill
- **AND** registry SHALL 为空（后续 wait 全部返回 unknown）

#### Scenario: max_concurrent 满拒绝新 spawn
- **WHEN** `max_concurrent=2`，已有 2 个 running task
- **AND** 第 3 个 `spawn(...)`
- **THEN** SHALL 抛 `RuntimeError("too_many_background_tasks")`

### Requirement: run_in_background / wait_for_task 工具

系统 SHALL 提供 `make_run_in_background_tool(*, registry, policy=None)` 与 `make_wait_for_task_tool(*, registry, default_timeout=120.0)` 工厂。

`run_in_background` 工具：
- input_schema：`{"command": str}`（必填）
- handler 走启发式 deny list（与 `make_shell_exec_tool` 一致），然后可选 `policy.check(scope="shell_exec", target=command)`
- 通过后 `registry.spawn(...)` 返回 `{"task_id": str, "command": str, "started_at": float}`
- `parallel_safe = False`

`wait_for_task` 工具：
- input_schema：`{"task_id": str, "timeout_seconds": number?}`
- 派发到 `registry.wait(...)`；timeout 走 `default_timeout` 当 LLM 未传
- timeout 状态 SHALL 返回 `ToolResult.ok(data={"status": "timeout", ...})`，**不**作为 error（让 LLM 决定是否继续 wait / 切换策略）
- 未知 task_id 同样走 `ToolResult.ok(data={"status": "unknown"})`
- `parallel_safe = True`（只读 task 状态）

**完成唤醒（background-completion-wake，ADR 0050）**：`make_run_in_background_tool(..., notify_on_exit=True)`（默认开）。
`BackgroundTaskRegistry.spawn(..., on_complete=)` 在任务结束（正常退出 / 被 kill / 收集失败）后恰好回调一次，参数与
`wait()` 结果同形（新增 `killed` 字段），回调异常只记日志。工具据此：

- 把中性完成摘要（`[background task <id> finished: exit=N]` + stdout 尾部 ≤2000 字符）经 `deliver_peer_message` 投递回发起 thread：
  根 thread → `queue_only`（运行中注入 / 空闲落史，根 turn 由宿主驱动）；spawn 子 thread → `trigger_turn`（空闲即唤醒续跑）；
  `call_skill` 阻塞子 thread 不可寻址 → 明确改投根 thread；
- 在 engine 上 emit `background_task_completed`（`task_id` / `thread_id` / `delivered_to` / `exit_code` / `killed` / `delivered_via` / `woken`）。

工具上下文没有 engine 协调器（脱离 engine 的直接调用）时不投递，只能 `wait_for_task` 取结果。

#### Scenario: LLM 端到端跑一个长任务
- **WHEN** LLM 调 `run_in_background({"command": "sleep 1 && echo done"})` 拿到 task_id
- **AND** LLM 立刻调 `wait_for_task({"task_id": task_id, "timeout_seconds": 5})`
- **THEN** wait_for_task SHALL 返回 ToolResult.ok，data.status == `"completed"`，data.stdout 含 `"done"`

#### Scenario: wait_for_task timeout 不报 error
- **WHEN** task 仍在跑，LLM 用 timeout_seconds=0.2 调 wait_for_task
- **THEN** SHALL 返回 ToolResult.ok（is_error=False），data.status == `"timeout"`
- **AND** LLM 可再次调 wait_for_task 继续等

#### Scenario: 启发式 deny list 拦截危险命令
- **WHEN** LLM 调 `run_in_background({"command": "rm -rf /"})`
- **THEN** SHALL 在调 `registry.spawn` 之前被 `_quick_safety_check` 拦截
- **AND** 返回 ToolResult.error，reason 含 `safety_blocked`

### Requirement: http_request 工具发起受审批的 HTTP 调用

系统 SHALL 提供 `taifeng.tool.builtins.make_http_request_tool(*, policy=None, timeout_seconds=30.0, max_response_bytes=1MB, max_redirects=5, allowed_methods=("GET","HEAD","POST","PUT","PATCH","DELETE")) -> ToolSpec` 工厂。返回的 `ToolSpec`：

- `name = "http_request"`
- `parallel_safe = False`（保守：单一 ToolSpec 同时承载读写方法，序列化执行）
- `timeout_seconds` = 入参
- 输入 schema：
  - `url: string` —— 必填；必须以 `http://` 或 `https://` 开头
  - `method: string` —— 可选；枚举 `GET|HEAD|POST|PUT|PATCH|DELETE`；默认 `GET`；必须在工厂 `allowed_methods` 列表内
  - `headers: object` —— 可选；string→string；空 dict 等价不传
  - `body` —— 可选；string 直传、dict/list 自动 JSON 序列化
  - `timeout_seconds: number` —— 可选；覆盖工厂默认；若超过工厂上限 SHALL 拒绝（`bad_args`）

handler SHALL 执行如下顺序：

1. **入参校验** —— url 缺失 / 格式非法 / method 非法 / timeout 超限 → `ToolResult.error(reason="bad_args")`
2. **取消检查** —— `ctx.cancel.raise_if_cancelled()` 在发起请求前抛出（R4 红线）
3. **PermissionPolicy** —— `policy is None` → `ToolResult.error(reason="no_policy")`；否则 `await policy.check(PermissionRequest(scope="network", target=f"{method} {url}", reason="LLM 请求 HTTP 调用", metadata={"thread_id": ctx.thread_id, "call_id": ctx.call_id, "method": method, "url": url}))`；`granted=False` → `ToolResult.error(reason="permission_denied")`
4. **执行（逐跳审批）** —— `httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds), follow_redirects=False)`；dict/list body 走 `json=`，string body 走 `content=`。响应为 3xx 且 `resp.next_request` 非空时由内核自行跟随：每跳 `hop += 1`，`hop > max_redirects` → `ToolResult.error(reason="redirect_limit")`；`ctx.cancel.raise_if_cancelled()`；对 `next_request` 的 method / URL 重新构造 `PermissionRequest(scope="network", target=f"{method} {url}", metadata={..., "redirect_hop": hop, "redirect_from": <上一跳 URL>})` 过 policy，deny → `permission_denied`（MUST NOT 发出该跳）；放行后 `client.send(next_request)`。复用 httpx 的 `next_request` 保留 303 改 GET 等 RFC 7231 语义。**不做**内核级私网 / loopback 黑名单——那是策略层职责，metadata 里的 hop 信息供业务策略收紧
5. **响应序列化** —— `ToolResult.ok(output=<JSON 字符串>, status_code=..., bytes_in=..., truncated=..., method=..., url_final=...)`，output 形如：
   ```json
   {
     "status": <int>,
     "headers": {<lowercased name>: <str>},
     "body": "<= max_response_bytes 字节，超出截断>",
     "truncated": <bool>,
     "url_final": "<重定向后最终 URL>"
   }
   ```
6. **HTTP 4xx / 5xx 不算 ToolResult.error** —— `is_error=False`，让 LLM 自行解读 status code
7. **异常归类**：
   - `httpx.TimeoutException` → `ToolResult.error(reason="timeout")`
   - 跳数超过 `max_redirects`（内核自计数，不再依赖 `httpx.TooManyRedirects`）→ `ToolResult.error(reason="redirect_limit")`
   - `httpx.ConnectError` / `httpx.RequestError` → `ToolResult.error(reason="connect_error")`
   - 其他 Exception → `ToolResult.error(reason="unknown")` 且 `logger.exception(...)`

#### Scenario: 工厂返回 ToolSpec.parallel_safe=False
- **WHEN** 业务调 `make_http_request_tool(policy=None)`
- **THEN** SHALL 返回 ToolSpec.name == `"http_request"`、`parallel_safe is False`、`"url" in input_schema["required"]`

#### Scenario: policy=None 立即拒绝
- **WHEN** `make_http_request_tool(policy=None)` 派发 `{"url": "https://example.com/"}`
- **THEN** SHALL 返回 `ToolResult.error`，`is_error=True`，`data["reason"] == "no_policy"`
- **AND** SHALL **不**发起任何 httpx 请求

#### Scenario: GET 成功返回结构化 body
- **WHEN** mock transport 对 `GET https://api.example.com/v1/ping` 返回 `200 {"pong": true}`
- **AND** LLM 调 `{"url": "https://api.example.com/v1/ping"}`
- **THEN** SHALL 返回 `ToolResult.ok`，output JSON 含 `status=200` 且 body 含 `"pong"`
- **AND** ToolResult.data 含 `status_code=200`、`truncated=False`、`method="GET"`

#### Scenario: POST dict body 走 JSON 序列化
- **WHEN** LLM 调 `{"url": "https://api.example.com/v1/echo", "method": "POST", "body": {"a": 1}}`
- **AND** mock transport 校验请求 body 是 `{"a":1}` 且 content-type 含 `application/json`
- **THEN** SHALL 返回 ToolResult.ok

#### Scenario: 4xx 不标记为 error
- **WHEN** mock transport 返回 `404 Not Found`
- **THEN** SHALL 返回 `ToolResult.ok`（is_error=False），output JSON 含 `status=404`

#### Scenario: 5xx 不标记为 error
- **WHEN** mock transport 返回 `503 Service Unavailable`
- **THEN** SHALL 返回 `ToolResult.ok`（is_error=False），output JSON 含 `status=503`
- **AND** LLM 可读 status 自行重试或放弃

#### Scenario: body 超 max_response_bytes 截断
- **WHEN** 工厂 `max_response_bytes=1024`，mock transport 返回 2KB body
- **THEN** SHALL 返回 output JSON 中 `body` 长度 == 1024、`truncated=True`
- **AND** ToolResult.data["bytes_in"] == 2048（原始字节数）

#### Scenario: 超时归类为 timeout reason
- **WHEN** mock transport 抛 `httpx.ReadTimeout`
- **THEN** SHALL 返回 ToolResult.error，`data["reason"] == "timeout"`

#### Scenario: PermissionPolicy 拒绝
- **WHEN** policy 对 `target == "GET https://leaky.example/"` 返回 `PermissionDecision.deny(reason="not_in_allowlist")`
- **THEN** SHALL 返回 ToolResult.error，`data["reason"] == "permission_denied"`
- **AND** SHALL **不**发起请求

#### Scenario: 非法 url 归类 bad_args
- **WHEN** LLM 调 `{"url": "file:///etc/passwd"}`（缺 http/https scheme）
- **THEN** SHALL 返回 ToolResult.error，`data["reason"] == "bad_args"`

#### Scenario: method 不在 allowed_methods 拒绝
- **WHEN** 工厂 `allowed_methods=("GET",)`，LLM 调 `{"url": "https://x/", "method": "DELETE"}`
- **THEN** SHALL 返回 ToolResult.error，`data["reason"] == "bad_args"`

### Requirement: file_read 按行分页（offset/limit）

`make_file_read_tool` 产出的 `file_read` 工具 SHALL 支持可选 `offset` / `limit` 参数（**按行**，0 基），用于分页回读大文件（典型场景：`OffloadStrategy` 落盘的超大 tool 结果，见 [compaction-offload-strategy.md](compaction-offload-strategy.md)）。

- 二者均省略时，行为 SHALL 与历史版本**逐字节一致**（整文件读 + 超 `max_bytes` 截断）。
- 给定时按 `splitlines()` 切片后再对结果限幅，**绕过整文件 byte-cap**——否则大文件后段行不可达。
- `offset` / `limit` 给定时必须为非负整数，否则 `reason == "bad_args"`。

#### Scenario: offset/limit 读指定行区间
- **WHEN** 文件含 5 行，LLM 调 `{"path": "log.txt", "offset": 1, "limit": 2}`
- **THEN** SHALL 返回第 1–2 行（`"L1\nL2"`），不报错

#### Scenario: offset 越界返回空
- **WHEN** `offset` 超过文件行数
- **THEN** SHALL 返回空内容（`output == ""`），不报错（便于 LLM 探测分页边界）

#### Scenario: 省略分页参数等价旧行为
- **WHEN** 调 `{"path": "log.txt"}` 不带 offset/limit
- **THEN** 行为 SHALL 与本能力前一致（整文件读 + 超 `max_bytes` 截断）

#### Scenario: 非法分页参数归类 bad_args
- **WHEN** LLM 调 `{"path": "x", "offset": -1}` 或 `offset` 非整数
- **THEN** SHALL 返回 ToolResult.error，`data["reason"] == "bad_args"`


### Requirement: glob / grep 沙盒内只读文件搜索（opt-in，ADR 0064 / 0071）

系统 SHALL 提供两个工厂，均**默认不注册**，业务经 `EnginePool.create(extra_tools=[...])` 显式启用（入口 skill 仍需在 `tool_names` 声明，见 [tool-whitelist](tool-whitelist.md)）：

- `taifeng.tool.builtins.make_glob_tool(*, root_dir, policy=None, max_results=200, exclude_dirs=DEFAULT_SEARCH_EXCLUDE_DIRS, respect_gitignore=True, timeout_seconds=30.0) -> ToolSpec`
- `taifeng.tool.builtins.make_grep_tool(*, root_dir, policy=None, max_results=200, max_line_chars=500, max_file_bytes=2MB, exclude_dirs=DEFAULT_SEARCH_EXCLUDE_DIRS, respect_gitignore=True, timeout_seconds=30.0) -> ToolSpec`

上限参数非正时工厂 SHALL 抛 `ValueError`。实现：`tool/builtins/{glob_search,grep_search,grep_scan,search_walk,gitignore}.py`，纯 Python（不依赖 rg 二进制，不调用 git）。

**ToolSpec 静态声明**（两者相同）：`parallel_safe=True`、`effect_kind="pure"`、`reconciliation="none"`；`input_schema` 带 `additionalProperties: false`（派发前按 [tool-argument-validation](tool-argument-validation.md) 预校验）。

| 工具 | 参数 | 输出（LLM 可见） | `data`（telemetry） |
| --- | --- | --- | --- |
| `glob` | `pattern`（必填）/ `path`（基点目录，缺省沙盒根） | 每行一个文件路径 | `count` / `truncated` / `skipped_symlinks` / `skipped_ignored` / `gitignore_unsupported` / `unreadable` |
| `grep` | `pattern`（必填，Python `re`）/ `path`（目录或文件）/ `include`（glob 过滤）/ `ignore_case`（缺省 false）/ `output_mode` ∈ `content`（缺省）· `files_with_matches` · `count` / `context_before` · `context_after` · `context`（非负整数，缺省 0）/ `multiline`（缺省 false） | `content`：匹配行 `路径:行号:行`（行号 1 基），上下文行 `路径-行号-行`，不相邻的组之间一行 `--`；multiline 时每个匹配一行 `路径:起始行-结束行:"片段"`；`files_with_matches`：路径；`count`：`路径:匹配行数`（multiline 为匹配数） | `mode` / `multiline` / `count` / `context_lines` / `truncated` / `files_scanned` / `skipped_binary` / `skipped_large` / `skipped_symlinks` / `skipped_ignored` / `gitignore_unsupported` / `unreadable` |

行为约束：

1. **路径约束与 file_read 一致**：基点经 `file_io._resolve_safe` 解析（跟随符号链接后仍须落在 `root_dir` 内），否则 `reason="sandbox_violation"`。遍历中**不跟随**指向目录的符号链接；指向文件的链接仅当解析后仍在沙盒内才纳入；其余（沙盒外 / 目录 / 悬空）跳过并计入 `skipped_symlinks`。输出路径一律**相对沙盒根**（POSIX 分隔），可直接作 `file_read` / `apply_patch` 的 `path`。
2. **噪声目录**：`exclude_dirs` 内的目录名（任意深度）不下探；默认 `DEFAULT_SEARCH_EXCLUDE_DIRS = {.git, .hg, .svn, node_modules, .venv, __pycache__, .mypy_cache, .pytest_cache, .ruff_cache, .tox}`（公开常量，可 `| {...}` 扩展）。显式把排除目录作为基点时照常搜索。
   - **.gitignore**（`respect_gitignore=True`，默认）：规则来自沙盒根到搜索基点沿途各级目录与遍历中进入的子目录的 `.gitignore`（普通文件，符号链接不读；> 1MiB / 非 UTF-8 / 读失败计入 `unreadable`）。被命中的路径跳过并计入 `skipped_ignored`，被命中的目录**不下探**（因此父目录被忽略的文件无法经 `!` 重新纳入，与 git 一致），尾注 `` [skipped N path(s) ignored by .gitignore; pass an ignored directory as `path` to search inside it] ``。显式指定的搜索基点自身不受忽略规则影响（其下路径仍逐条判定）；grep 的基点是文件时照常搜索。支持的语法：空行、`#` 注释（`\#` 转义）、行尾未转义空格去除（`\ ` 保留）、`!` 否定（`\!` 转义；同一文件内最后命中的规则说了算，子目录 `.gitignore` 优先于父目录）、尾斜杠只匹配目录、开头或中间含 `/` 即锚定到该 `.gitignore` 所在目录（否则任意深度按名称匹配）、`*` / `?` / `[...]`（`[!...]` / `[^...]` 取反）不跨 `/`、`**` 独立成段时匹配零或多层目录（开头 / 中间 / 结尾三种位置）。**不支持**：POSIX 字符类 `[[:alpha:]]`、未闭合 `[`、行尾孤立 `\`——该行不生效并计入 `gitignore_unsupported`，尾注告知「这些规则对应的路径照常被搜索」；`.git/info/exclude`、全局 `core.excludesFile`、`.ignore` / `.rgignore`、沙盒根之上的 `.gitignore`、`core.ignorecase`（一律大小写敏感）、「已被 git 跟踪的文件不受忽略规则影响」（不读 git 索引）。`respect_gitignore=False` 时不读任何 `.gitignore`（与 ADR 0071 之前一致）。
3. **glob 语义**：模式相对基点；`*` / `?` / `[...]` 不跨 `/`，`**` 匹配零或多段目录，`{a,b}` 可嵌套展开（上限 64 个展开式）；大小写敏感；只列文件。空模式 / 绝对路径 / 含 `..` 段 / 花括号不配对 / 超 1000 字符 → `bad_args`。grep 的 `include` 同一语法，**不含 `/` 时只比对文件名**（任意深度，同 `grep --include` / `rg --glob`），含 `/` 时比对相对基点的路径。
4. **排序**：按路径逐段字典序（深度优先、同层按名称码点序），确定性输出；不按修改时间排序。
5. **上限与截断**：结果超过 `max_results`（glob 按文件；grep `content` 按**输出行**——匹配行与上下文行都占名额、`--` 不占，multiline 按匹配；其余按文件）SHALL 停止遍历并在输出尾追加 `[results truncated at N <单位>; narrow ...]`（单位：`matching lines` / `output lines (matches + context)` / `matches` / `files`），`data.truncated=True`。grep 单行（含上下文行）超过 `max_line_chars` 截断并注明原长度（`…[line truncated, L chars]`）。零命中是正常结果（`no files matched` / `no matches found`），不是 error。
   - **上下文行**（grep，仅 `content` 且非 multiline）：`context` 同时设前后文，`context_before` / `context_after` 单侧优先。前文从「上次输出之后」与「匹配行 − before」中较晚者开始，重叠或相邻区间合并为一组、不重复输出；与上一组（含上一个文件）不相邻时先输出一行 `--`。一组（前文 + 匹配行）放不下剩余名额时整组不输出并截断（不留只有前文的残组）；后文被名额截断同样标记截断。参数非负整数（bool 不算）；非零上下文配 `files_with_matches` / `count` 或 `multiline=true` → `bad_args`（不静默忽略）；`context_before + context_after >= max_results` → `bad_args`（保证至少一整组放得下）。
   - **跨行匹配**（grep `multiline=true`）：先把 CRLF 规整为 LF，再以 `re.MULTILINE | re.DOTALL` 对整个文件 `finditer`——`^` / `$` 匹配每行首尾（只认 `\n`），`.` 可匹配换行。`content` 每个匹配一行 `路径:起始行-结束行:"片段"`：片段是 JSON 字符串（换行显示为 `\n`），超过 `max_line_chars` 截断并追加 ` …[match truncated, L chars]`；起始行 = 匹配起点所在行，结束行 = 匹配最后一个字符所在行（结尾换行符属于它结束的那行；空匹配取起点），行号按 `str.splitlines` 切行，与 `file_read` 的 `offset` 同口径。`count` 计匹配数；`files_with_matches` 首个匹配即列出。单文件仍受 `max_file_bytes` 约束，工作线程每 256 个匹配检查一次停止信号。
6. **跳过必须告知**：grep 对大于 `max_file_bytes` 的文件跳过并在尾注列出（至多 5 个名字 + 余数）；前 8KB 含 NUL 的二进制文件与严格 UTF-8 解码失败的文件跳过并计数（与 `file_read` 口径一致）；读不了的目录 / 文件计数。所有跳过都以 `[skipped ...]` 尾注出现，SHALL NOT 静默。
7. **权限**：`policy` 非空时每次调用审批**一次**：`PermissionRequest(scope="file_read", target=<基点绝对路径>, metadata={thread_id, call_id, submission_id, tool, pattern})`；拒绝 → `reason="permission_denied"` 且不读任何文件。审批粒度是「整棵子树」，不逐文件审批（逐文件在 `ask` 模式下不可用）；需要更细隔离请缩小 `root_dir` 或配置 `exclude_dirs`。`policy=None` 不审批（同 `file_read`）。
8. **R4 取消**：遍历与读文件在 `anyio.to_thread` 工作线程执行（`abandon_on_cancel=True`），线程在每个目录项、每 1024 行检查停止信号；token 取消（`on_cancel` 回调）与 await 被放弃（工具超时 / 外部取消）都会点亮它。token 取消 SHALL 返回 `ToolResult.error("cancelled (<reason>)", reason="cancelled")`。
9. 其余错误：参数非法 / 正则编译失败 → `bad_args`；glob 基点不是目录 → `reason="not_found"`（`not_a_directory`）；grep 基点不存在 → `not_found`。

已知边界：Python `re` 无匹配超时，病态正则的灾难性回溯无法被打断（逐行模式在单行上，multiline 模式在整个文件上，后者代价更高）——工具超时后 await 立即返回，工作线程在当前匹配结束后的下一个检查点退出。不引入带超时的第三方 regex 库（ADR 0071）。

#### Scenario: 递归 glob 按路径排序
- **WHEN** 沙盒含 `b.py`、`a/z.py`，LLM 调 `glob({"pattern": "**/*.py"})`
- **THEN** SHALL 返回 `a/z.py\nb.py`，`data.count == 2`

#### Scenario: 截断明确告知
- **WHEN** 工厂 `max_results=3`，命中 5 个
- **THEN** 输出 SHALL 只含前 3 条 + `results truncated at 3` 尾注，`data.truncated is True`

#### Scenario: 符号链接逃逸
- **WHEN** 沙盒内 `escape_dir` → 沙盒外目录、`escape.txt` → 沙盒外文件
- **THEN** 以它们为 `path` SHALL 返回 `sandbox_violation`；从沙盒根遍历 SHALL NOT 产出其内容，且尾注含 `skipped 2 symlink(s)`

#### Scenario: 二进制与超大文件跳过
- **WHEN** grep 遇到含 NUL 的文件、非 UTF-8 文件与超过 `max_file_bytes` 的文件
- **THEN** SHALL 跳过三者，尾注分别告知（超大文件列出名字）

#### Scenario: 未知参数被预校验拒绝
- **WHEN** LLM 调 `glob({"pattern": "*", "recursive": true})`
- **THEN** 派发层 SHALL 以 `invalid_arguments` 拒绝，handler 不执行

#### Scenario: 上下文行合并与分隔
- **WHEN** `a.txt` 第 3、5、9 行命中，LLM 调 `grep({"pattern": "hit", "context": 1})`
- **THEN** 输出 SHALL 为 `a.txt-2-…`、`a.txt:3:…`、`a.txt-4-…`、`a.txt:5:…`、`a.txt-6-…`、`--`、`a.txt-8-…`、`a.txt:9:…`、`a.txt-10-…`，`data.count == 3`、`data.context_lines == 6`

#### Scenario: 跨行匹配
- **WHEN** `m.py` 前三行是 `def f(` / `    x,` / `):`，LLM 调 `grep({"pattern": "def f\\(.*?\\):", "multiline": true})`
- **THEN** 输出 SHALL 为 `m.py:1-3:"def f(\n    x,\n):"`

#### Scenario: 遵循 .gitignore
- **WHEN** 沙盒根 `.gitignore` 含 `build/`，LLM 调 `glob({"pattern": "**/*.py"})`
- **THEN** 结果 SHALL NOT 含 `build/` 下的文件，尾注含 `ignored by .gitignore`；以 `path="build"` 调用时 SHALL 照常列出

### Requirement: memory 工具——模型主动检索 / 写入 / 删除长期记忆（opt-in，ADR 0064 / 0071）

系统 SHALL 提供 `taifeng.tool.builtins.make_memory_tool(store, *, actions=None, max_result_chars=4000, max_save_chars=2000, timeout_seconds=30.0) -> ToolSpec`（`name="memory"`）。它是 K3 `MemoryStore`（[context-compression § K3](../context-compression.md#k3-长期记忆-swap-接口memorystore)）的**模型侧入口**：读写删全部委托注入的 `store`，内核不内置任何存储后端；`MemoryStore` 协议本身不变，删除走可选协议 `ForgettableMemoryStore`。**默认不注册**；装配 = 同一 store 双注入：`EnginePool.create(memory_store=store, extra_tools=[make_memory_tool(store)])`（只注册工具、不传 `memory_store` 也合法：模型可主动读写，但内核不做被动 page-in / 写回）。

| action | 参数 | 委托 | 成功输出 |
| --- | --- | --- | --- |
| `search` | `query`（非空，≤2000 字符） | `store.prefetch(query, thread_id=ctx.thread_id)` | 返回文本；空串 → `no relevant memory found`；超 `max_result_chars` 截断并追加 `[memory result truncated to N chars]` |
| `save` | `content`（非空，≤`max_save_chars`） | `store.writeback(thread_id=ctx.thread_id, items=[item])` | `saved to memory (N chars)` |
| `delete` | `target`（非空，≤2000 字符） | `store.forget(target, thread_id=ctx.thread_id)` | 返回 N>0 → `deleted N memory record(s)`；0 → `no matching memory found; nothing deleted`（正常结果）；`data.deleted = N` |

`save` 写入的 `item` SHALL 为 `ResponseItem(kind="assistant_message", thread_id=ctx.thread_id, payload={"text": content, "model": ""}, metadata={"source": "memory_tool", "call_id": ctx.call_id})`（`MEMORY_TOOL_SOURCE` 常量）。没有更新动作：「改一条记忆」= delete 旧的 + save 新的。

**可选协议** `taifeng.context.ForgettableMemoryStore(MemoryStore, Protocol)`（`runtime_checkable`）只多一个 `async forget(target: str, *, thread_id: str) -> int`：`prefetch` 只返回一段文本、没有结构化条目，模型能表达的删除依据只有文本——后端在 prefetch 结果里展示的记忆标识（如 `[mem:42]`），或该条记忆的原文 / 足以唯一定位它的片段；解析由后端决定，推荐精确匹配而非按语义相似度批量删除。返回实际删除条数（`>= 0`），后端失败抛异常。内核被动路径从不调用 `forget`。`NullMemoryStore` 刻意**不**实现它（继承它的只读知识库不获得删除入口）；`CompositeMemoryStore` 在任一子 store 可遗忘时构造出满足该协议的实例（`forget` 按注册序转发给可遗忘的子并返回总数，某子抛错或返回非法计数时其余照做，最后抛 `RuntimeError` 写明已删除数与失败明细），没有可遗忘的子时实例不带 `forget`。

行为约束：

1. **动作集合**：`actions=None`（缺省）= `("search", "save")`，store 满足 `ForgettableMemoryStore` 时再加 `"delete"`；显式传入时只能是 `MEMORY_ACTIONS = ("search", "save", "delete")` 的非空子集，含未知动作、或含 `delete` 而 store 不可遗忘 → 工厂抛 `ValueError`。`input_schema` 的 `action.enum` 只含已启用动作，未启用动作的参数（`query` / `content` / `target`）不出现在 schema 中（`additionalProperties: false`），描述也只写已启用动作。store 可遗忘但宿主不想开放删除 → 显式 `actions=("search", "save")`。
2. **副作用分类取已启用动作中最保守一档**：含 `save` 或 `delete` → `parallel_safe=False`、`effect_kind="external_non_idempotent"`、`reconciliation="manual"`（后端写入 / 删除是否幂等内核无从得知，崩溃后交人裁决）；仅 `search` → `parallel_safe=True`、`pure`、`none`。两档均属 ADR 0025 合法组合。只读后端（如继承 `NullMemoryStore` 只覆写 `prefetch` 的知识库）SHALL 用 `actions=("search",)` 装配，否则 `save` 会落到 no-op 的 writeback。
3. **错误显式返回**：参数缺失 / 空白 / 超长 / 未启用动作 → `bad_args`；`content` 超 `max_save_chars` → `too_large`（不截断写入）；store 抛 `Exception` → `ToolResult.error("memory_error: <action> failed: <类型>: <消息>", reason="memory_error", action=...)` 并记 warning 日志；`forget` 返回负数 / bool / 非整数 → `memory_error`（`store returned invalid count`）。与内核被动钩子（best-effort 吞异常）相反：这是模型主动动作，失败 SHALL 让模型看见。
4. **R4**：store 调用包在 `interrupt_on_cancel(ctx.cancel)` 内，后端阻塞时 token 取消原地打断并返回 `cancelled (<reason>)`；外部 task 取消照常外抛。
5. **与被动写回的关系**：turn 结束的 `writeback` 仍会收到本 turn 新增 items（含 `memory` 的 function_call，其参数里有同一段 content）；需要区分「模型主动记忆」与脏页写回、或按 `call_id` 去重的后端读 `item.metadata`。

#### Scenario: save 委托 writeback
- **WHEN** LLM 调 `memory({"action": "save", "content": "用户偏好简洁回答"})`
- **THEN** `store.writeback` SHALL 收到恰一条 `assistant_message`，`metadata == {"source": "memory_tool", "call_id": <call_id>}`

#### Scenario: store 抛错显式返回
- **WHEN** `store.prefetch` 抛 `ConnectionError("vector db down")`
- **THEN** SHALL 返回 `is_error=True`、`data["reason"] == "memory_error"`，输出含 `ConnectionError: vector db down`

#### Scenario: 未注册时不可见
- **WHEN** 入口 skill 声明 `tool_names: [memory]`，但 `extra_tools` 未含该工具
- **THEN** 发给模型的请求 tools SHALL NOT 含 `memory`

#### Scenario: 只读装配
- **WHEN** `make_memory_tool(store, actions=("search",))`
- **THEN** `parallel_safe is True`、`effect_kind == "pure"`，schema 不含 `content`，调 `save` 返回 `bad_args` 且不触达 writeback

#### Scenario: 可遗忘 store 才提供 delete
- **WHEN** store 实现了 `forget`，`make_memory_tool(store)`
- **THEN** `action.enum == ["search", "save", "delete"]`、schema 含 `target`；对不可遗忘的 store 同样装配 SHALL NOT 出现 `delete` / `target`，显式 `actions=("search", "delete")` SHALL 抛 `ValueError`

#### Scenario: delete 委托 forget
- **WHEN** LLM 调 `memory({"action": "delete", "target": "[mem:7]"})`，`forget` 返回 2
- **THEN** `store.forget` SHALL 收到 `("[mem:7]", thread_id=<当前 thread>)`，输出 `deleted 2 memory record(s)`；返回 0 时输出 `no matching memory found; nothing deleted` 且不是 error
