## Context

现状（已逐处核对）：

| 环节 | 位置 | 现状 |
| --- | --- | --- |
| 模型 | `llm/image_input.py` `ImageAttachmentV1`、`llm/file_input.py` `FileAttachmentV1` | `extra="forbid"`、冻结；`content` 必填（canonical base64） |
| 用户 admission | `loop/engine_submit.py` → `loop/attachment_parts.py`；`loop/engine_gate.py` 落盘前再跑一次 `history_to_api_messages` | 解码、核对 size / sha256 / 数量 / 字节上限 / MIME / 尺寸 |
| 工具 admission | `loop/turn_tooling.py` → `admit_tool_attachments` | 同上；拒绝则该次调用判错 |
| 持久化 | `conversation/models.py` `user_message` / `function_call_output` | payload 里直接放 `model_dump()`，含 base64 |
| 冷加载 | `conversation/transcript.py` `load_history` | 只做 `ResponseItem` 形状校验，不看附件 |
| 请求构建 | `loop/prompt.py`（全部同步函数） | 每次构建重新 admission 并投影为 `ImagePart` / `FilePart` |
| 估算 | `context/budget.py` `_estimate_image_tokens` / `_estimate_file_tokens` | 策略启用时完整解码以取得宽高 / 页数 |
| 审计 | `conversation/journal/records.py` `AttachmentV1`、`item_records.py` | 记录里是完整正文，`payload_hash` 覆盖正文 |
| 压缩 | `context/strategies/multimodal_evict.py`、ADR 0147 | 淘汰时才把原件写进工作区 |

参照（只学范式，不抄代码）：

- **DeepSeek Harness** `docs/subsystems/attachment.md`：字节交给独立存储，对象 durable 之后才发布不可变的内容寻址引用；会话事件与模型可见块里只有引用和元数据；标识符是不透明字符串，消费方不得解析。
- **Google ADK** artifact service：上下文里只有轻量描述，工具按需取字节；后端可换（内存 / 对象存储）。

差异：dsh 的存储同时负责图片规范化与按路由缩放；ADK 的 artifact 带命名与版本。taifeng 只取「引用 + 已校验元数据 + 先存后记」这一核心，其余留给宿主或后续变更。

## Goals / Non-Goals

目标：transcript 与内存 history 不再承载附件正文；估算不解码；恢复可靠；一张取不回的附件不毁掉 thread；未启用时零行为变化。

非目标见 proposal。

## 数据模型

### 引用形态

```python
class ImageAttachmentRefV1(BaseModel):          # frozen, extra="forbid"
    kind: Literal["image"] = "image"
    encoding: Literal["store"] = "store"        # 与内联形态的 "base64" 区分
    ref: str                                     # 不透明引用；消费方不得解析
    media_type: ImageMediaType
    size: int                                    # 原件字节数，> 0
    sha256: str                                  # 原件摘要，64 位小写 hex
    width: int                                   # admission 时由文件头读出，> 0
    height: int
    detail: ImageDetail = "auto"

class FileAttachmentRefV1(BaseModel):           # frozen, extra="forbid"
    kind: Literal["file"] = "file"
    encoding: Literal["store"] = "store"
    ref: str
    media_type: str                              # 白名单同 FileAttachmentV1
    size: int
    sha256: str
    pages: int | None                            # PDF 页数；数不出时 None（估算取上限）
    filename: str | None = None
```

- **判别字段是 `encoding`**：`"base64"` = 内联（既有 V1），`"store"` = 引用。一个条目的 `attachments` 列表里两种形态可以并存。
- **元数据是 admission 的产物**：`width` / `height` / `pages` 只在字节通过 admission 时写入，之后被信任——估算与成本计算读它们，不再碰正文。
- `ref` 的字符集限定为 `[A-Za-z0-9._:-]`、长度 1–200：它会出现在给模型看的描述里（`attachment:<ref>`），不能含空白、分隔符或控制字符。本机实现给出 `sha256-<64 位 hex>`，但**任何消费方不得据此推导路径或假设格式**。

### 存储协议

```python
class AttachmentStore(Protocol):
    async def put(self, data: bytes, *, media_type: str, sha256: str) -> str: ...
    async def get(self, ref: str) -> bytes: ...
    async def exists(self, ref: str) -> bool: ...
    async def delete(self, ref: str) -> None: ...
```

- `put` **幂等**：同一内容重复写入返回同一引用，不重写已有对象。调用方传入已算好的 `sha256`，实现 SHALL 核对它与 `data` 一致，不一致抛 `AttachmentIntegrityError`。返回即表示对象已 durable。
- `get` 找不到抛 `AttachmentNotFoundError`。**完整性校验不在协议里**：由内核的解析器用引用上的 `sha256` / `size` 统一核对，后端实现不必各自重复。
- `delete` 只供宿主的回收流程调用，内核自己不调用；对不存在的引用为 noop。
- 四个方法都必须可被 anyio 取消。
- `LocalAttachmentStore(root_dir)`：对象落在 `<root>/v1/<sha256 前 2 位>/<sha256>`，写临时文件后原子替换；不存扩展名与 MIME（它们在引用上）。

### 注入

`EnginePool.create(attachment_store: AttachmentStore | None = None)`；`pool.attachments` 只读属性暴露给宿主（上传、回收）。插件经 `provide_backends` 提供时遵循 ADR 0145 的全部规则（单一提供者、与显式参数冲突报错、提供后冻结）。pool 不 `close()` 注入的存储（协议里没有 `close`；需要释放资源的后端由宿主或提供它的插件负责）。

## 流程

### 写入：先存后记

```
UserMessage(attachments=[内联 V1 | 引用])
  └─ admission（既有校验，不变）
       ├─ 内联 + 配置了存储 → put(bytes) → 换成引用（带 width/height/pages）
       ├─ 内联 + 未配置存储 → 保持内联（现状）
       └─ 引用 → exists(ref)？get 并完整 admission 一次（首次进入本 pool 的引用必须验字节）
  └─ durable append（条目里是引用）
  └─ 入队
```

- 任一 `put` 失败 → 该 submission 被拒绝，错误归类到既有的 admission 失败路径；**不回退为内联**（那是静默改变持久化形态）。
- 工具结果同理：`admit_tool_attachments` 通过后、落 fco 之前 `put`；失败则该次工具调用判错（`tool_attachment_rejected`），配对不断。
- 宿主直接传引用时，内核不信任其元数据：取回字节重新 admission，用自己读出的 `width` / `height` / `pages` 覆盖。同一引用在同一 pool 生命周期内只验一次（按 `(ref, sha256)` 记住）。

### 读取：解析阶段

`loop/prompt.py` 的构建函数全部是同步的，且被多处调用。不把它们改成异步，而是在采样前加一步：

```
pre_sample_upkeep → 压缩检查 → 【解析阶段】 → build_api_request（同步，不变） → provider
```

- 解析阶段扫描**本次请求会用到的** history 条目，收集引用形态的附件，经 `AttachmentResolver` 取回字节并核对 `sha256` 与 `size`，产出 `{ref: bytes}`；构建函数从这张表取正文，投影出的 `ImagePart` / `FilePart` 与内联路径**逐字段相同**。
- `AttachmentResolver` 每个 pool 一个，带按字节计的 LRU 缓存（`attachment_cache_bytes`，默认 64 MiB；0 = 不缓存）。并发取同一引用合并为一次后端调用。
- 被压缩驱逐的附件已不在 history 里，自然不会被解析——驱逐因此直接省掉后端读取。

### 取不回时：降级，不毁 thread

历史里的某个引用 `get` 失败（不存在 / 后端错误）或校验不过时：

- 该附件在本次请求里换成文本 `[attachment unavailable: <类型> <大小> sha256=<前 8 位> ref=<ref>: <原因>]`，与同条目的文本一起发给模型；
- 发 `attachment_unavailable` 事件（`reason` ∈ `not_found` / `integrity_mismatch` / `backend_error`）；
- turn 继续。

理由：同形态的失败在 Claude Code 上表现为「一张坏图让会话永久 400」。这里的失败是存储事故而不是输入错误，模型与宿主都被如实告知，不属于静默回退。**例外**：当前这条 submission 自己带的附件在 admission 阶段就取不到 → submission 被拒绝（输入错误必须让调用方知道，与现有「user 侧能力不足即抛错」一致）。

未注入存储却遇到引用形态 → `AttachmentStoreRequiredError`，turn 失败。这是装配错误，不降级。

### 估算

`estimate_history_tokens` 对引用形态直接用 `width` / `height` / `pages` 调 `InputCostEstimator`；`pages is None` 取 `unknown_file_token_ceiling`。内联形态走原路径。策略未启用时的常数估算不变。

### 与压缩 / ADR 0147 的衔接

| | 内联形态（未配置存储） | 引用形态 |
| --- | --- | --- |
| 驱逐时 | 配了工作区则落盘（ADR 0147） | 不写任何东西 |
| 描述里的指针 | `path=_offload/{thread}/att-….png` | `path=attachment:<ref>` |
| 模型读回 | `file_read(path)` 读工作区文件 | `file_read(path)` 识别 `attachment:` 前缀，校验该引用出现在**调用方 thread 的 transcript** 里，然后把同一个引用作为工具结果附件返回（不取字节、不复制） |
| handoff 指针块 | 不变 | 不变（`_POINTER_RE` 扩展为同时匹配 `attachment:`） |

对模型而言只有一条规则：**描述里给了 `path=`，就用 `file_read` 读它**。后端是工作区还是存储，模型不需要知道。

`file_read` 的引用归属校验是必要的：引用是内容寻址的，本身不可猜，但模型可能从别处（如工具输出里）看到别的 thread 的引用。只放行本 thread transcript 里出现过的引用。

## Decisions

1. **默认不启用**。`attachment_store=None` 时一切照旧。启用后新写入的附件是引用；同一 thread 里新旧形态并存。先在实验层发布，跑过一个发布周期再考虑默认启用。
2. **判别用 `encoding` 而不是新的 `kind`**。`kind` 已被压缩视图、估算、驱逐等十余处用来区分图片与文件；引用是「怎么存」而不是「是什么」。
3. **元数据进引用**（宽高、页数）。代价是引用形态比 dsh 的多几个字段；收益是估算从「解码全部附件」变成读几个整数。
4. **完整性校验放在解析器而不是协议实现**。后端作者只需实现存取，不会因为漏做校验而留下静默损坏。
5. **不改构建函数的同步签名**，改为前置解析。改异步会波及 `history_to_api_messages` 的全部调用点（admission 复核、压缩视图、回放），风险与收益不成比例。
6. **取不回降级而不是失败**，但仅限历史附件；见上。
7. **不做引用计数**。thread 之间、恢复前后共享对象，thread 删除时内核不删对象。提供 `collect_attachment_refs(items) -> set[str]`，宿主可据此做标记-清除。
8. **审计模式本期互斥**。`audit=` 与 `attachment_store=` 同时给出 → 构造期 `ValueError`。Journal 记录形状变更涉及 hash 链与回放，单列为 `attachment-store-journal`。
9. **冷加载时发现引用即检查装配**。`load_history` 本来就要读完整个 thread；读到任一引用形态的附件而 pool 未注入存储时，在第一次采样前就报 `AttachmentStoreRequiredError`，而不是等到某一轮构建请求才失败。不改 `__meta__` 首行（transcript 追加写，首行不可回改）。

### 否决的方案

- **把 `WorkspaceFS` 直接当附件存储**（沿用 ADR 0147 的 `_offload/`）：工作区是模型可写的，附件原件必须不可变；工作区也可能是每个 thread 一个沙盒，做不到跨 thread 共享。
- **provider Files API 作为主存储**（Anthropic / OpenAI `file_id`）：thread 被锁在一个 provider 上，换模型即失效；保留期由对方决定。可以作为解析阶段之上的上传缓存，不能是真相源。
- **在 `MessageStore` 协议上加 blob 方法**：已有的宿主实现全部要改；附件的生命周期（跨 thread 共享、独立回收）也与消息不同。
- **启用时改写既有 transcript 把内联附件外置**：违反 R5 追加写；双读已经让旧 thread 可用。
- **引用上不放元数据、估算时按需取字节**：等于把现在的解码成本换成后端读取成本，远端后端下更糟。

## Risks

- **恢复依赖两个存储的一致性**：transcript 在、附件存储丢了 → 历史附件全部降级为占位文本。缓解：`attachment_unavailable` 事件、元信息标记的启动期检查；宿主须把两者放进同一备份策略（写入契约文档）。
- **宿主传入伪造元数据的引用**：已由「首次进入 pool 的引用重新 admission」覆盖；需要测试守住。
- **解析器缓存占用内存**：默认 64 MiB 上限；大图场景可调或关。
- **旧版本读到引用形态的 thread**：旧版本不认识 `encoding="store"`，构建请求时失败。无法在旧版本上修复；发布说明里写明「启用后该 thread 不可被旧版本恢复」。
- **`file_read` 的 `attachment:` 前缀与真实文件名冲突**：工作区路径解析在前缀判断之后；名为 `attachment:xxx` 的真实文件读不到。可接受（冒号在多数平台的文件名里本就受限），写入契约。

## Open Questions

1. `collect_attachment_refs` 是否足以支撑宿主的回收，还是需要在 `ThreadDirectory` 层提供「全量引用集」？等第一个要做回收的宿主提出需求再定。
2. 图片是否在 `put` 之前做规范化（去 EXIF、统一格式）？本期不做——`sha256` 是原件的摘要，规范化会改变它，并且影响回放摘要。
3. 文档中 `tool-image-attachment.md` 的「strict SessionJournal 不支持」一行与代码（`loop/audit_tool.py` 已支持工具图片落账）不一致，需在本变更的文档任务里一并更正。
