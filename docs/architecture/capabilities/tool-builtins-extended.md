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
4. **执行** —— `httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds), follow_redirects=True, max_redirects=max_redirects)`；dict/list body 走 `json=`，string body 走 `content=`
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
   - `httpx.TooManyRedirects` → `ToolResult.error(reason="redirect_limit")`
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

