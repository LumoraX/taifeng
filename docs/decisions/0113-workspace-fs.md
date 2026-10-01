# ADR 0113：WorkspaceFS——文件类工具经协议读写工作区

- 状态：Accepted
- 日期：2026-09-30
- 关联：[workspace-fs](../architecture/capabilities/workspace-fs.md)、
  [tool-builtins-extended](../architecture/capabilities/tool-builtins-extended.md)；ADR 0051、0064、0071、0112

## 背景

ADR 0051 让命令经 `CommandExecutor` 进容器或远端沙盒，但文件类工具仍直接读写宿主机目录：
`file_read` / `file_write` / `apply_patch` 用 `Path.read_text` / `os.replace`，`glob` / `grep` 用 `os.scandir`。
命令在沙盒里改了文件，模型用 `file_read` 读到的却是宿主机上的旧内容——两个对不上的世界。容器后端靠
「同路径挂载」勉强对齐，远端后端无解。

命中 ADR 0017 规则③：内核定协议，隔离实现走外部。

## 决策

1. **`WorkspaceFS` 协议，7 个成员**：`root`、`resolve`、`read_bytes`、`write_bytes`、`metadata`、
   `list_directory`、`remove`。形状对齐已有的外部实现（沙盒守护进程的文件访问接口），适配是薄薄一层。
2. **协议只管文件访问**。路径规则、权限、上限、分页、补丁的先校验后应用、搜索的顺序与 `.gitignore`
   语义留在工具里：换实现不改变模型看到的行为。
3. **`resolve` 返回的规范路径以 `root` 为前缀**，并用作权限 target 与结果回显。每个方法自己再校验
   一次边界，不信任调用方。失败用标准 `OSError` 子类表达。
4. **五个文件类工具工厂都接受 `workspace=`**，与 `root_dir=` 二选一；`root_dir` 等价于
   `LocalWorkspaceFS(root_dir)`，既有写法与行为不变。
5. **搜索工具的遍历逻辑只写一份**。遍历是工作线程里的同步代码，`WorkspaceFS` 是异步协议：抽出同步的
   `SearchFs` 访问面，本机实现照旧用 `os.scandir`，非本机实现把每次访问经
   `run_coroutine_threadsafe` 送回事件循环（事件循环此时在 await 工作线程，是空着的）。
6. **`apply_patch` 删除目录在校验阶段拒绝**（`not_a_file`）。此前目录能通过校验、到应用阶段才失败，
   排在它前面的 patch 已经落盘，破坏了「任一失败则零修改」。

## 不做

- **给搜索工具另写一套异步遍历**：两份遍历逻辑必然漂移（顺序、`.gitignore`、名额记账）；桥接的代价
  只是每次目录访问多一次线程切换。
- **协议里加按偏移读、流式、`rename`、`stat` 链接目标**：现有工具用不到；远端符号链接因此一律不跟随，
  在输出尾注里告知。需要时加可选能力，不动这 7 个成员。
- **offload 压缩策略落盘的目录走 `WorkspaceFS`**：那是内核自己的溢出存储，不是模型的工作区。
- **把 `LocalWorkspaceFS` 做成 `file_read` 之外的通用沙盒**：它只约束路径，不隔离进程。

## 影响

- R1：无业务概念。
- R2、R3、R5：无影响。
- R4：搜索的停止信号照常生效；非本机工作区单次访问 60 秒未返回按读取失败计。

### 行为变化

- 本机文件读写从事件循环挪到工作线程（不再阻塞事件循环）；结果不变。
- `apply_patch` 删除目录：从应用阶段的 `apply_io_error`（可能已部分落盘）变为校验阶段的
  `patch_validation_failed: not_a_file`（零修改）。
- 工具结果与权限 target 里的路径类型不变（本机仍是绝对路径字符串）。

## 验证

- `tests/tool/test_workspace_fs.py`（24 项）覆盖本机实现、内存工作区上的五个工具、权限 target、入参互斥、
  非本机搜索的各条边界；既有的文件 / 搜索工具测试未改一行全部通过。全量 `pytest tests/` 3908 passed,
  17 skipped；ruff 门禁与 `mypy src/` 清零。
- 跨包实测（2026-09-30，taifeng-sandbox `1b4fcbb`）：把沙盒守护进程真的作为另一个进程跑起来，在它已有的
  `DaemonWorkspace` 上只补 `root` / `resolve` 两个成员就满足 `WorkspaceFS`；内核的五个文件类工具经它的线
  协议读写、打补丁、`glob`（含 `.gitignore`）、`grep`，8 项检查全部通过，越界在工具层与守护进程两处都被拒。
  适配代码属于沙盒包，不在本仓。
