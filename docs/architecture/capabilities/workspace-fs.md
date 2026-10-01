# Capability: workspace-fs（工作区文件访问协议）

> 状态：✅。决策记录：[ADR 0113](../../decisions/0113-workspace-fs.md)。入口在稳定层：`WorkspaceFS` /
> `LocalWorkspaceFS` / `WorkspaceFileInfo` / `WorkspaceEntry` / `WorkspacePathError`。

## Purpose

文件类工具（`file_read` / `file_write` / `apply_patch` / `glob` / `grep`）读写的「工作区」是一个协议，不写死
本机目录。命令经 `CommandExecutor` 放进容器或远端沙盒之后，宿主把同一处文件系统以 `WorkspaceFS` 注入
文件类工具，模型看到的文件就是命令改动的那些文件。

内核只定协议与本机默认实现（ADR 0017 规则③）。路径规则、权限审批、大小上限、分页、补丁的先校验后
应用、搜索的确定性顺序与 `.gitignore` 语义都在工具里，换实现不改变它们。

## 数据契约

| 符号 | 含义 |
| --- | --- |
| `WorkspaceFS`（Protocol） | 7 个成员，见下 |
| `WorkspaceFileInfo(exists, is_directory=False, is_file=False, size=0, modified_at=0.0)` | 元数据；不存在时只有 `exists=False` |
| `WorkspaceEntry(name, is_directory, is_file, is_symlink=False)` | 目录项；类型判定不跟随符号链接 |
| `WorkspacePathError(PermissionError)` | 路径落在工作区之外 |
| `LocalWorkspaceFS(root_dir)` | 默认实现：本机目录 |

```python
class WorkspaceFS(Protocol):
    @property
    def root(self) -> str: ...
    def resolve(self, path: str) -> str: ...
    async def read_bytes(self, path: str) -> bytes: ...
    async def write_bytes(self, path: str, data: bytes, *, create_parents: bool = True) -> None: ...
    async def metadata(self, path: str) -> WorkspaceFileInfo: ...
    async def list_directory(self, path: str) -> list[WorkspaceEntry]: ...
    async def remove(self, path: str, *, recursive: bool = False) -> None: ...
```

## 行为契约

### Requirement: 路径与边界

- `root` 是工作区根的标识，出现在给模型看的工具描述与权限 target 里。
- `resolve(path)` SHALL 把相对工作区根的路径规范成规范路径，不做文件读写；落在工作区之外 SHALL 抛
  `WorkspacePathError`。规范路径 SHALL 是 `root` 本身或以 `root + "/"` 开头（搜索工具据此换算出相对路径）。
- 其余方法 SHALL 同时接受相对路径与 `resolve` 返回的规范路径，并**各自校验**不越界——实现不得假设调用方
  先调过 `resolve`。
- 失败 SHALL 以标准 `OSError` 子类表达：不存在 `FileNotFoundError`；越界或无权 `PermissionError`；其余
  `OSError`。`metadata` 对不存在的路径返回 `exists=False`，不抛异常。

#### Scenario: 越界在每个方法上都被拒绝
- **WHEN** 直接以 `../outside.txt` 调 `read_bytes` / `write_bytes` / `metadata` / `list_directory` / `remove`
- **THEN** 每个都抛 `WorkspacePathError`，工作区外的文件不受影响

### Requirement: 读写语义

- `read_bytes` 返回整个文件；分页与字节上限由工具处理。
- `write_bytes` 覆盖写入，`create_parents=True` 时自动建父目录，否则父目录不存在抛 `FileNotFoundError`。
  写入 SHOULD 是原子的（读者看到旧内容或新内容）；做不到的实现须在自己的文档里说明。
  `LocalWorkspaceFS` 写临时文件后 `rename`，是原子的。
- `list_directory` 不递归，顺序不作要求（调用方排序）。
- `remove` 删除文件；目录在 `recursive=False` 时须为空，否则抛 `OSError`。

### Requirement: 文件类工具经工作区访问文件

`make_file_read_tool` / `make_file_write_tool` / `make_apply_patch_tool` / `make_glob_tool` / `make_grep_tool`
SHALL 接受 `root_dir=`（本机目录，等价于 `workspace=LocalWorkspaceFS(root_dir)`）或 `workspace=`（注入的
`WorkspaceFS`），恰好一个；都给或都不给 `ValueError`，`workspace` 不满足协议 `TypeError`。

- 权限 target SHALL 是 `resolve` 返回的规范路径；审批先于任何读取，被拒的请求不触碰工作区。
- 工作区抛出的 `OSError` SHALL 落成工具的错误结果（`read_error` / `write_error` / `apply_io_error`），不外抛。
- `apply_patch` 的删除对目录 SHALL 在校验阶段以 `not_a_file` 拒绝（不留到应用阶段半途失败）。

#### Scenario: 工作区在别处
- **WHEN** `make_file_write_tool(workspace=<远端工作区>)` 写 `out/report.md`
- **THEN** 内容出现在该工作区里，本机目录没有任何文件产生
- **AND** 结果的 `path` 与权限 target 都是该工作区的规范路径

### Requirement: 搜索工具在非本机工作区上的行为

`glob` / `grep` 的遍历逻辑只有一份（确定性顺序、排除目录、`.gitignore`、名额、取消）。遍历在工作线程里
跑；非本机工作区的每次访问送回事件循环执行。与本机相比有两点差异，SHALL 如实体现在输出尾注里：

- 符号链接一律不跟随并计数（协议不给链接目标，无法判定是否仍在工作区内）；
- 工作区访问失败（`OSError`，含单次访问 60 秒未返回）的目录 / 文件计入 `unreadable`，搜索继续。

`grep` 对大于 `max_file_bytes` 的文件按 `metadata().size` 判定后跳过，不读入内存。

## 测试接入

- `tests/tool/test_workspace_fs.py` —— 本机实现（协议满足、边界、逐方法越界、读写 / 元数据 / 列目录 / 删除、
  不建父目录、目录删除）；内存工作区上的 `file_read` / `file_write` / `apply_patch`（先校验后应用、目录删除在
  校验期拒绝、写失败落成结果）；权限 target 与被拒不触碰工作区；五个工厂的入参互斥；`glob` / `grep` 在
  非本机工作区上的遍历、`.gitignore`、排除目录、二进制与超大文件、符号链接、访问失败、取消。
- 既有 `tests/tool/test_apply_patch.py` / `test_builtin_tools.py` / `test_glob_search.py` / `test_grep_*.py` /
  `test_search_walk.py` 未改一行——`root_dir` 路径的行为不变。

## 能力边界（如实记录）

- 协议没有按偏移读、流式读写与 `rename`：大文件整读整写；需要时再加可选能力，不改这 7 个成员。
- `LocalWorkspaceFS` 把阻塞操作放到工作线程；同一文件的并发写由调用方（工具的 `parallel_safe=False`）串行化。
- 注入的工作区若不做原子写，`file_write` / `apply_patch` 的「要么全写要么不写」只到单次 `write_bytes` 的粒度，
  取决于实现。

## R1–R5 影响

- R1：无业务概念。R2、R3、R5：无影响。
- R4：搜索工具的停止信号照常生效；`file_read` / `file_write` 的工作区访问随工具超时被放弃。
