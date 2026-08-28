# Taifeng 2026.8.28.0 PyPI Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 发布 `taifeng==2026.8.28.0`，并确保运行时、MCP、distribution metadata、tag 与 PyPI 版本一致，干净环境可安装 wheel 和 sdist。

**Architecture:** `pyproject.toml` 是发行版本与依赖声明源，`taifeng.__version__` 是运行时版本，MCP stdio 从运行时版本读取。发布 workflow 在 OIDC 上传前构建一次产物，并调用仓库脚本对 wheel/sdist metadata 和双干净安装进行验证。

**Tech Stack:** Python 3.12+、pytest、uv、hatchling、GitHub Actions Trusted Publishing、PyPI JSON API

---

### Task 1: 锁定版本一致性契约

**Files:**
- Modify: `tests/llm/test_public_imports.py`
- Modify: `tests/mcp/test_mcp.py`
- Modify: `src/taifeng/__init__.py`
- Modify: `src/taifeng/mcp/stdio_client.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: 写运行时版本失败测试**

在 `tests/llm/test_public_imports.py` 增加：

```python
from importlib.metadata import version

import taifeng


def test_runtime_version_matches_distribution_metadata() -> None:
    assert taifeng.__version__ == version("taifeng")
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_public_imports.py::test_runtime_version_matches_distribution_metadata -q`

Expected: FAIL，显示 `0.0.1 != 2026.8.7.0`。

- [ ] **Step 3: 写 MCP clientInfo 失败测试**

在 `tests/mcp/test_mcp.py::test_mcp_initialize_and_list` 的 fake server request capture 中断言：

```python
assert initialize_params["clientInfo"]["version"] == taifeng.__version__
```

- [ ] **Step 4: 运行 MCP 测试并确认 RED**

Run: `PYTHONPATH=src uv run pytest tests/mcp/test_mcp.py::test_mcp_initialize_and_list -q`

Expected: FAIL，旧实现仍返回 `0.0.1`。

- [ ] **Step 5: 最小实现版本同步**

修改 `pyproject.toml` 和 `src/taifeng/__init__.py`：

```toml
version = "2026.8.28.0"
```

```python
__version__ = "2026.8.28.0"
```

修改 stdio client，让 `clientInfo.version` 使用 `taifeng.__version__`，不保留第三份 literal。

- [ ] **Step 6: 运行定向测试并确认 GREEN**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_public_imports.py tests/mcp/test_mcp.py::test_mcp_initialize_and_list -q`

Expected: 全部 PASS。

- [ ] **Step 7: 提交**

```bash
git add pyproject.toml src/taifeng/__init__.py src/taifeng/mcp/stdio_client.py \
  tests/llm/test_public_imports.py tests/mcp/test_mcp.py uv.lock
git commit -m "release: synchronize version 2026.8.28.0"
```

### Task 2: 修复干净安装缺失依赖

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/test_package_metadata.py`

- [ ] **Step 1: 写依赖声明失败测试**

```python
from importlib.metadata import requires


def test_distribution_declares_sniffio_runtime_dependency() -> None:
    dependencies = requires("taifeng") or []
    assert any(item.startswith("sniffio>=1.3") for item in dependencies)
```

- [ ] **Step 2: 运行并确认 RED**

Run: `PYTHONPATH=src uv run pytest tests/test_package_metadata.py -q`

Expected: FAIL，因为 distribution metadata 没有 `sniffio`。

- [ ] **Step 3: 增加直接依赖并刷新 lock**

在核心 dependencies 中增加：

```toml
"sniffio>=1.3", # projection backend identity 的直接运行时依赖
```

Run: `uv lock`

- [ ] **Step 4: 运行并确认 GREEN**

Run: `PYTHONPATH=src uv run pytest tests/test_package_metadata.py -q`

Expected: PASS，且 `uv sync --extra dev` 会安装 `sniffio`。

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml uv.lock tests/test_package_metadata.py
git commit -m "fix: declare sniffio runtime dependency"
```

### Task 3: 建立可复用的发行产物门禁

**Files:**
- Create: `scripts/verify_release_artifacts.py`
- Create: `tests/test_verify_release_artifacts.py`
- Modify: `.github/workflows/publish.yml`

- [ ] **Step 1: 写 artifact metadata 解析失败测试**

测试构造 wheel/sdist fixture，要求 verifier 拒绝版本不一致或缺少 `Requires-Dist: sniffio>=1.3` 的产物，并接受完整 metadata。

- [ ] **Step 2: 运行并确认 RED**

Run: `PYTHONPATH=src uv run pytest tests/test_verify_release_artifacts.py -q`

Expected: ERROR/FAIL，因为 verifier 尚不存在。

- [ ] **Step 3: 实现最小 verifier**

脚本接受 `--dist-dir` 与 `--expected-version`，读取 wheel `*.dist-info/METADATA` 与 sdist `PKG-INFO`，校验：

```text
Name: taifeng
Version: 2026.8.28.0
Requires-Dist: sniffio>=1.3
```

随后分别用 `uv venv` 创建两个临时环境，用 `uv pip install --python <python> <artifact>` 安装 wheel/sdist，并执行：

```python
import importlib.metadata
import sniffio
import taifeng

assert taifeng.__version__ == importlib.metadata.version("taifeng") == expected
```

- [ ] **Step 4: 运行 verifier 测试并确认 GREEN**

Run: `PYTHONPATH=src uv run pytest tests/test_verify_release_artifacts.py -q`

Expected: PASS。

- [ ] **Step 5: 加固 Trusted Publishing workflow**

在上传前运行：

```yaml
- name: 校验锁文件
  run: uv lock --check
- name: 运行版本一致性测试
  run: uv run --extra dev pytest tests/llm/test_public_imports.py tests/mcp/test_mcp.py::test_mcp_initialize_and_list tests/test_package_metadata.py -q
- name: 校验发行产物
  run: uv run scripts/verify_release_artifacts.py --dist-dir dist --expected-version "${GITHUB_REF_NAME#v}"
```

- [ ] **Step 6: 提交**

```bash
git add scripts/verify_release_artifacts.py tests/test_verify_release_artifacts.py .github/workflows/publish.yml
git commit -m "ci: verify PyPI artifacts before upload"
```

### Task 4: 本地发行候选验证

**Files:**
- Verify only

- [ ] **Step 1: 全量测试**

Run: `PYTHONPATH=src uv run pytest tests/ -q`

Expected: 全部 PASS。

- [ ] **Step 2: Ruff 与 diff 检查**

Run: `uv run ruff check scripts/verify_release_artifacts.py tests/test_verify_release_artifacts.py tests/test_package_metadata.py`

Run: `git diff --check main...HEAD`

Expected: 全部退出 0。

- [ ] **Step 3: 构建到空目录并执行双产物 smoke**

Run: `dist_dir=$(mktemp -d); uv build --out-dir "$dist_dir"; uv run scripts/verify_release_artifacts.py --dist-dir "$dist_dir" --expected-version 2026.8.28.0`

Expected: wheel/sdist metadata 与两个干净安装全部 PASS。

### Task 5: 集成 main 与发布

**Files:**
- Git refs and external PyPI state

- [ ] **Step 1: 合并前检查目标版本即时唯一性**

查询 `https://pypi.org/pypi/taifeng/2026.8.28.0/json`，并检查 local/origin tag；PyPI 应为 404，tag 应不存在。

- [ ] **Step 2: fast-forward 合并 main 并重复全量测试与 artifact smoke**

Expected: main 干净，测试与两个产物安装均 PASS。

- [ ] **Step 3: 创建并推送 annotated tag**

```bash
git tag -a v2026.8.28.0 -m "Release 2026.8.28.0"
git push origin main v2026.8.28.0
```

- [ ] **Step 4: 监控 Trusted Publishing workflow**

用 `gh run list` 找到 tag 对应 workflow，并用 `gh run watch --exit-status` 等待成功终态。

- [ ] **Step 5: PyPI 最终验收**

查询 PyPI JSON 到版本可见，并在全新临时环境执行：

```bash
uv pip install --python <temp-python> 'taifeng==2026.8.28.0'
```

然后验证 `taifeng.__version__`、distribution metadata、`sniffio` import 和 Codex provider public import。
