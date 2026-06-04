"""step_pipeline web demo —— 业务编排多步流水线 + 步级级联重试（浏览器可点）。

与 ``examples/web_ui``（chat 范式）不同：这里是**流水线范式** —— 每步一张卡片，展示
「输入 → 输出 / 状态」，每步一个 🔄 重试按钮（级联重跑下游），某步弹表单时就地填写续跑。

同步模型（极简）：start / retry / resume 三个动作各自把流水线跑到「下一个挂起点或全部
完成」再返回全量快照，前端据快照重渲染步卡。复用真实 LLM（_provider_bootstrap）。

运行：
    cd taifeng
    PYTHONPATH=src uv run python examples/step_pipeline/server.py
    # 浏览器 http://localhost:8766
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse
    from pydantic import BaseModel
except ImportError:  # pragma: no cover
    print("缺 fastapi —— 装：uv pip install -e \".[dev,litellm]\"", file=sys.stderr)
    raise

import taifeng

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"
sys.path.insert(0, str(EXAMPLES_DIR))
sys.path.insert(0, str(HERE))

from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)
from pipeline import Pipeline  # noqa: E402

from taifeng.tool.builtins.request_user_input import (  # noqa: E402
    make_request_user_input_tool,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("step_pipeline")

# 固定 3 步流水线（演示）；换成你的真实步骤只需改这里 + skills_dir
STEPS: list[tuple[str, str]] = [
    ("intake", "步骤1·信息采集"),
    ("risk", "步骤2·风险评估"),
    ("plan", "步骤3·干预计划"),
]
SKILLS_DIR = HERE / "skills"

_pool: taifeng.EnginePool | None = None
_pipe: Pipeline | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    load_dotenv_files()
    try:
        client, meta = build_model_client(require_api_key=False)
        logger.info("model client 就绪 provider=%s model=%s", meta["provider"], meta["model"])
    except ProviderBootstrapError as exc:
        logger.error("model client 构造失败：%s", exc)
        raise
    _pool = await taifeng.EnginePool.create(
        skills_dir=SKILLS_DIR,
        storage_dir=HERE / ".runs",
        model_client=client,
        compressors=[],
        extra_tools=[make_request_user_input_tool()],
        max_iterations=20,
    )
    try:
        yield
    finally:
        if _pool is not None:
            await _pool.close()


app = FastAPI(title="Step Pipeline Demo", lifespan=lifespan)


class StartReq(BaseModel):
    seed: str


class ResumeReq(BaseModel):
    request_id: str
    payload: dict[str, Any]


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(HERE / "static" / "index.html")


@app.post("/api/start")
async def start(req: StartReq) -> dict[str, Any]:
    """新建流水线并从第 1 步顺跑到挂起/完成。"""
    global _pipe
    if _pool is None:
        raise HTTPException(503, "pool 未就绪")
    if not req.seed.strip():
        raise HTTPException(400, "seed 不能为空")
    # 每次 start 用新 base_session，避免与上轮 thread 冲突（用 seed 长度 + 计数做区分）
    import time
    base = f"pipe_{int(time.monotonic() * 1000)}"
    _pipe = Pipeline(_pool, STEPS, req.seed.strip(), base)
    await _pipe.run_from(0)
    return {"steps": _pipe.snapshot()}


@app.post("/api/retry/{index}")
async def retry(index: int) -> dict[str, Any]:
    """重试第 index 步 → 级联重跑 index..N。"""
    if _pipe is None:
        raise HTTPException(409, "流水线未启动")
    if not 0 <= index < len(_pipe.steps):
        raise HTTPException(400, f"step index 越界: {index}")
    await _pipe.retry(index)
    return {"steps": _pipe.snapshot()}


@app.post("/api/resume/{index}")
async def resume(index: int, req: ResumeReq) -> dict[str, Any]:
    """提交第 index 步的表单 → 续跑该步并往后顺跑。"""
    if _pipe is None:
        raise HTTPException(409, "流水线未启动")
    if not 0 <= index < len(_pipe.steps):
        raise HTTPException(400, f"step index 越界: {index}")
    await _pipe.resume_step(index, req.request_id, req.payload)
    return {"steps": _pipe.snapshot()}


@app.get("/api/state")
async def state() -> dict[str, Any]:
    return {"steps": _pipe.snapshot() if _pipe else []}


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="warning")


if __name__ == "__main__":
    main()
