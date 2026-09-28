# ADR 0064：opt-in 的 glob / grep 搜索工具与 memory 薄工具

- 状态：Accepted
- 日期：2026-09-28
- 关联：[capabilities/tool-builtins-extended.md § glob / grep、§ memory](../architecture/capabilities/tool-builtins-extended.md)；
  [context-compression.md § K3](../architecture/context-compression.md#k3-长期记忆-swap-接口memorystore)；
  ADR 0017（立项四规则）/ 0025（effect 分类）/ 0028（效果权限模型）/ 0045（工具崩溃对账）/ 0047（参数预校验）

## 背景

1. **文件搜索缺口**：内置文件工具只有 `file_read` / `file_write` / `apply_patch`，模型要找「某个函数在哪」只能逐个
   猜路径，或借 `shell_exec` 跑 `find` / `grep`。后者要放开整条 shell 权限（`scope=shell_exec` + 启发式黑名单），
   输出不受控，且继承 shell 的副作用分类——崩溃后按 ADR 0045 挂起交人，而搜索本是可安全重发的只读动作。
2. **记忆只有被动面**：K3 `MemoryStore` 由内核在 turn 前后被动调用（`prefetch` 的 query 由内核按最后一条用户消息
   或 `memory_query_builder` 构造；`writeback` 收本 turn 新增 items）。模型无法在 turn 中途按自己的判断
   「查一下以前说过什么」或「把这条结论记下来」。

## 决策

1. **glob / grep 作为 opt-in builtin**（`make_glob_tool` / `make_grep_tool`）。命中 ADR 0017 §三的 builtins 收录标准
   「通用 IO 原语（file/shell/http）」。静态声明 `parallel_safe=True`、`effect_kind="pure"`、`reconciliation="none"`：
   崩溃恢复可安全重发，可与其他只读工具并行。
   - 沙盒判定复用 `file_io._resolve_safe`，与 `file_read` 同一语义；遍历不跟随目录符号链接（防环、防逃逸），
     指向沙盒外的文件链接跳过并告知。
   - 权限走 `file_read` 效果（ADR 0028），**每次调用审批一次**，target = 搜索基点绝对路径（子树粒度）。
   - 结果**按路径排序**而不是按修改时间：同样的调用得到同样的输出——与 `pure` 分类的「重发等价」一致，
     journal 回放 / 测试可复现，history 里的工具结果也不会因为文件被 touch 而漂移；并且按路径的深度优先遍历
     天然有序，命中 `max_results + 1` 即可停，不必先 stat 全量文件再排序。按 mtime 排序对「刚改过的文件」
     更友好，但模型可以用 `path` / `include` 收窄范围达到同样目的，确定性更难从外部补回来。
   - 纯 Python 实现（`os.scandir` + `re`），阻塞 IO 放 `anyio.to_thread`，token 取消与工具超时都点亮工作线程的
     停止信号（R4）。截断、二进制 / 非 UTF-8 / 超大文件、跳过的符号链接都在输出尾注明确告知，不静默。
2. **memory 作为 opt-in 薄工具**（`make_memory_tool(store)`）。模型主动的检索 / 记忆是认知回路原语（规则②），
   但存储是外部成熟服务能承担的（规则③）——所以工具**只做委托**：`search` → `prefetch`，`save` → `writeback`；
   内核不内置任何后端，**不扩协议**（协议没有删除语义，工具也不提供 delete / update）。
   - `save` 写入一条 `assistant_message`，`metadata={"source": "memory_tool", "call_id": ...}`：既有按
     user/assistant 文本沉淀的后端零改动可收；要区分主动记忆与脏页写回、或按 call_id 去重的后端读 metadata。
   - 一个工具多个动作，副作用分类取**已启用动作中最保守**的一档：含 `save` → `external_non_idempotent` /
     `manual` 且串行（后端写入是否幂等内核无从得知，崩溃后交人而不是引导重发）；`actions=("search",)` 的只读
     装配 → `pure` / `none` 且可并行。只读知识库（继承 `NullMemoryStore` 只覆写 `prefetch`）必须用只读装配，
     否则 `save` 会落到 no-op 的 writeback，模型以为记住了实际什么都没存。
   - 后端异常以 `reason="memory_error"` 显式返回。被动钩子吞异常是因为它们不能打断 turn；主动动作失败必须让
     模型看见，否则它会基于「已记住」继续推理。
3. **三个工具都默认不注册**，业务经 `EnginePool.create(extra_tools=[...])` 显式启用，入口 skill 在 `tool_names`
   声明——与 `http_request` / `todo_write` / spawn 工具同一启用方式。理由：
   - 文件搜索会把沙盒内任意文件内容带进上下文，是否开放、开放哪个根目录、配什么审批，都是宿主的安全决策；
   - memory 工具没有 store 就无意义，而 store 本身就是宿主注入的；
   - 内核默认工具面只保留 skill 范式必需的 `read_skill` / `call_skill` / `run_script`，默认注册会改变所有既有
     部署发给模型的 tools 列表（prompt 指纹与 cache 前缀随之变化）。
   公共符号只进 `taifeng.tool.builtins.__all__`（同 `make_http_request_tool` / `make_file_read_tool`），
   不进顶层 `taifeng.__all__`。

## 否决的方案

- **调用外部 `rg` 二进制**：搜索更快、功能更全，但给内核加了一个运行时二进制依赖，沙盒 / 符号链接 / 取消语义
  要跨进程重新对齐，且各平台行为不一。内核定位是可嵌入的 Python 包，纯 Python 在「万级文件的工作区」量级够用；
  需要 rg 性能的宿主可以自己注册基于 rg 的同名工具。
- **grep 逐文件审批**：权限最细，但一次 grep 可能触及成千上万个文件，`ask` 模式下逐个弹窗不可用，
  `SuspendingPrompter` 下会反复挂起。子树粒度 + 可配的 `root_dir` / `exclude_dirs` 足以表达隔离需求。
- **把二进制 / 非 UTF-8 文件按替换字符解码后照搜**：能多搜到一些 ASCII 片段，但与 `file_read`（严格 UTF-8，
  失败报错）口径不一致——搜得到却读不了的结果只会把模型引到死路。
- **memory 工具提供 delete / update**：需要扩 `MemoryStore` 协议。遗忘 / 覆盖策略（按时间衰减、按冲突合并、
  人工审核）因后端而异，属于后端职责；目前没有嵌入方拉动（ADR 0017 辅助判据），等需求出现再以向后兼容的
  可选协议扩展。
- **memory 拆成 `memory_search` / `memory_save` 两个工具**：各自分类更精确，但模型面多一个工具、描述重复；
  用 `actions` 控制启用集合并据此计算分类，同样能让只读装配拿到 `pure` + 可并行。
- **内核内置一个默认记忆后端（如 JSONL / SQLite）**：直接违反规则③——向量库 / KV / RAG 是 userspace。

## 验证

- `tests/tool/test_search_walk.py`：glob 编译（`**` / `{a,b}` 嵌套 / 非法模式）、逐段匹配、遍历顺序、排除目录、
  符号链接策略、工作线程在 token 取消与 await 被放弃两种情形下都退出。
- `tests/tool/test_glob_search.py` / `test_grep_search.py`：正常路径、三种输出模式、include / ignore_case、
  截断告知、二进制 / 非 UTF-8 / 超大文件跳过、`..` 与符号链接逃逸、权限 scope / target / 拒绝、取消、
  schema 拒绝未知参数；真实 `EnginePool` + `SimClient` 端到端调用一次 grep。
- `tests/tool/test_memory.py`：两个动作的委托与 item 形状、参数校验、结果截断、store 抛错显式返回、
  后端阻塞时取消、按动作的副作用分类（均为 ADR 0025 合法组合）、未注册时请求里不可见、启用后端到端 save 一次。
