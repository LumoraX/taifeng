# Taifeng 2026.8.28.0 PyPI 发布设计

## 目标

将当前 `main` 的 Codex 图片输入能力发布为 PyPI 包 `taifeng==2026.8.28.0`，并保证源码运行时版本、distribution metadata、Git tag 与 PyPI 版本完全一致。

## 发布阻断项

1. `2026.8.7.0` 已存在于 PyPI，不能重复上传。
2. `pyproject.toml` 为 `2026.8.7.0`，但 `taifeng.__version__` 与 MCP stdio
   `clientInfo.version` 仍硬编码为 `0.0.1`。
3. `conversation/journal/materialization.py` 直接导入 `sniffio`，但核心依赖未声明；干净环境会在导入 `taifeng` 时失败。

## 变更

- 将 `pyproject.toml` 与 `taifeng.__version__` 同步为 `2026.8.28.0`，MCP stdio
  `clientInfo.version` 改为读取统一运行时版本。
- 将 `sniffio>=1.3` 声明为核心直接依赖。
- 增加版本一致性测试，校验运行时版本、distribution metadata 与 MCP clientInfo 一致。
- 加固 `publish.yml`：上传前执行锁文件校验、版本一致性测试、产物 metadata 检查以及 wheel/sdist
  双路径干净安装 smoke。
- 不修改 LLM、loop、conversation 的行为与协议，不刷新真实 LLM 台账。

## 验证

1. TDD：版本一致性测试先在旧值上失败，修改后通过。
2. `uv lock` 与全量 pytest 通过。
3. `uv build` 构建到全新临时目录，检查 wheel `METADATA` 与 sdist `PKG-INFO` 的版本和
   `Requires-Dist: sniffio>=1.3`。
4. 分别在两个全新临时虚拟环境中安装 wheel 与 sdist，验证 `import taifeng`、`import sniffio`
   以及 `taifeng.__version__ == importlib.metadata.version("taifeng") == "2026.8.28.0"`。
5. 在 workflow 中重复版本测试和两个产物的安装 smoke，成功后才允许 `uv publish`。
6. 合并 main 后重复全量 pytest 和构建安装冒烟。

## 发布与验收

- 打 tag 前即时查询 PyPI JSON，并检查 local/origin 的 `v2026.8.28.0`；任一已存在即停止并选择新版本。
- 创建并推送 annotated tag `v2026.8.28.0`，触发 `.github/workflows/publish.yml` 的 Trusted Publishing。
- 监控 GitHub Actions 到成功终态。
- 查询 PyPI JSON API，并在全新虚拟环境从正式 PyPI 安装精确版本进行最终验证。
- GitHub Actions 成功、PyPI 可见和 PyPI 安装成功分别报告；不得用 tag 或本地构建替代发布证据。

## 失败处理

- 构建、测试或安装失败：不打 tag。
- workflow 失败：保留 tag 与日志，修复后使用新版本号，不覆盖已发布文件。
- PyPI 尚未可见：轮询有限时间并如实报告传播状态，不重复上传同一版本。
