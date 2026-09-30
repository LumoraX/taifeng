# ADR 0073：apply_patch 按路径申请 file_write；`ApplyPatch` 别名并入 `FileWrite`

- 状态：Accepted（Amends #0028）
- 日期：2026-09-30
- 关联：[permission-gate 契约](../architecture/capabilities/permission-gate.md)；
  [tool-builtins-extended § apply_patch](../architecture/capabilities/tool-builtins-extended.md)；ADR 0017 / 0022 / 0028

## 背景

ADR 0028 把效果模型定为权限正典，并把一处过渡例外记入 backlog：`apply_patch` 仍以
`scope="tool_use", target="apply_patch"` 申请权限，请求里**不带路径**。后果是按路径写的禁令对它无效——
`{"deny": ["FileWrite(/srv/app/secrets/*)"]}` 能拦住 `file_write`，拦不住同样能改写、删除该目录文件的 `apply_patch`。
同一个效果（写文件）因为换了一个工具就绕开规则，正是效果模型要消除的漏洞。

## 决策

1. **按路径发 `file_write`**：`apply_patch` 对每个**不同的**解析后绝对路径各发一条
   `PermissionRequest(scope="file_write", target=<绝对路径>)`。edit / create / delete 都归 `file_write`——
   删除是写效果，不新增 scope。`metadata` 带 `tool="apply_patch"`、该路径上的 `patch_kinds`、`patch_count`
   与 thread / call / submission 标识，供 Prompter 展示和业务策略收紧。
2. **执行顺序：解析 → 审批 → 内容校验 → 应用**。路径解析是纯计算；审批先于任何文件内容读取，
   被拒的请求不能从 `old_text_not_found` 之类的报错里探知目标文件内容。越出沙盒的路径在审批前就失败，
   不向审批人展示。
3. **原子性延伸到权限**：任一路径被拒 → 整组不执行、0 文件被改，返回 `permission_denied` 并带 `denied_path`。
   遇拒即停，不再为其余路径继续打扰审批人。
4. **同一路径只审批一次**：同一文件上的多条 patch 合并成一条请求。
5. **`ApplyPatch(p)` 并入 `FileWrite(p)`**：别名保留（既有规则串仍可解析），scope 改为 `file_write`，
   payload 按绝对路径匹配。

## 替代方案

- **整组一条请求、target 拼接全部路径**：规则匹配对象不再是单个规范化路径，`FileWrite(/x/*)` 无法命中拼接串；否决。
- **edit 额外发 `file_read`**：edit 确实读文件，但读到的内容不回给模型（只用于定位 `old_text`），
  对外效果只有写；多发一条只会让 `ask` 模式下每个文件问两次；否决。
- **保留 `tool_use` 请求并额外发 `file_write`**：同一动作两套审批，业务要写两份规则才能放行；否决。
- **删除 `ApplyPatch` 别名**：既有 `ApplyPatch(*)` 规则会在加载期抛 `unknown_permission_syntax`；
  保留为同义别名代价为零；否决。

## 后果

- **BREAKING（行为）**：
  - 按路径写的 `FileWrite` 规则开始对 `apply_patch` 生效——此前意外放行的补丁可能被拦住，属预期；
  - `ApplyPatch(apply_patch)`（把 payload 当工具名写）不再命中任何请求，需改为路径模式或 `ApplyPatch(*)`；
    `ApplyPatch(*)` 语义从「允许 / 询问 apply_patch 这个工具」变为「允许 / 询问任意路径的写入」，
    同时也会匹配 `file_write` 工具的请求；
  - Style B 规则 `{"scope": "tool_use", "target_pattern": "apply_patch"}` 不再命中，需改为 `file_write` + 路径；
  - 多文件补丁在 `ask` 模式下按文件数询问（可复用授权 ADR 0022 减少重复询问）。
- 校验报错顺序变化：路径 / 形状错误先于内容错误报告（例如第 1 条 `old_text` 不存在、第 2 条越出沙盒时，
  现在先报第 2 条）。
- R1–R5：R1 无业务概念；R2 不涉及压缩与 prompt 前缀；R3 沿用既有 `permission_*` 事件；R4 审批沿用
  `PermissionPolicy.check` 的取消语义；R5 不改持久化形状。
