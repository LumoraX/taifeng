# ADR 0058：SKILL.md 严格加载

- 状态：Accepted
- 日期：2026-09-28
- 关联：[skill-system.md § 加载校验](../architecture/skill-system.md)；ADR 0056（inference 块校验）

## 背景

2026-09-28 能力侧 review 发现 loader 在四处静默处理出错的 SKILL.md：

1. frontmatter 缺失、YAML 语法错误或不是 mapping → `logger.warning` 后返回 None，skill 从列表里消失；
   作者只会看到「entry 找不到」或「模型从不调用它」。
2. body 超过 256KB → 截断并追加提示，模型拿着残缺指令运行。
3. 字段直接 `bool()` / `frozenset()` / `int()` 强转：`entry: "false"` 成了 True，`child_skills: style-checker`
   （漏写方括号）被拆成单字符集合，`model_invocable: "no"` 仍然可见。
4. 多目录加载时同名 skill 被 `dict.update` 静默覆盖。

## 决策

1. 1–3 一律在加载期抛 `SkillValidationError`。启动时 fail-fast；热更新时 watcher 已有「discover 失败保留旧快照」逻辑，
   不会因为改坏一个文件让运行中的服务丢失全部 skill。
2. 没有 `SKILL.md` 的子目录仍合法跳过（共享素材目录是常见布局）。
3. 多目录覆盖保留：它是 registry 注释写明的分层语义（靠后目录覆盖靠前目录）。改为 warning 日志写明两处路径。
4. 顶层未知键不报错：`frontmatter_raw` 是业务透传通道，业务键无法由内核穷举。拼写错误的防护落在有明确键集的
   嵌套块上（`inference` 未知键即报错）。
5. 类型化读取集中到 `skill/frontmatter_fields.py`，loader 不再直接强转。

## 否决的方案

- **超长 body 保留截断但发事件**：截断后的指令语义不确定，事件只能事后告知；拆文件是作者可控的正确做法。
- **多目录同名直接报错**：会破坏已写明的覆盖语义，而真实的多层 skill 目录（如系统层 + 用户层）正需要它。

## 影响

对「一直带着坏 SKILL.md 运行」的部署是行为变化：以前能启动（缺几个 skill），现在启动即报错并指明文件。
仓库内全部 examples skill 目录已逐一通过 `taifeng skill validate`。

## 验证

`tests/skill/test_strict_loading.py`（20 例）：三种坏 frontmatter、无 SKILL.md 目录仍跳过、超长 body、11 种错误类型、
必填空值、未知顶层键透传、跨目录覆盖告警。
