"""taifeng.llm.providers.sim —— 有状态 LLM conformance 模拟器。

替代旧 ``mock.py`` 开环复读机：像真实服务端一样**先审请求（协议合同）、
再记账（token / 前缀 cache）、最后按脚本作答**，使 resume / rewind / call_id
配对 / 重放复读 / 并发时序类 bug 在日常 mock 回归中直接测红。

模块分工：
- ``script``   剧本单元（SimTurn / SimExpect / SimFault / SimScriptExhausted）
- ``contract`` 协议合同校验（RequestContractValidator / SimContractViolation）
- ``server``   服务端状态机与请求侦察（SimServerState / RequestLedger）
- ``client``   ModelClient 组装（SimClient / RoutingSimClient / SimCoordinator）
- ``shape``    形状签名抽取（ShapeSignature / extract_shape —— 金样校准单一真相）
"""

from __future__ import annotations

from taifeng.llm.providers.sim.client import (
    RoutingSimClient,
    SimClient,
    SimCoordinator,
)
from taifeng.llm.providers.sim.contract import (
    RequestContractValidator,
    SimContractViolation,
)
from taifeng.llm.providers.sim.script import (
    SimExpect,
    SimFault,
    SimFinish,
    SimScriptExhausted,
    SimTurn,
)
from taifeng.llm.providers.sim.server import (
    RecordedRequest,
    RequestLedger,
    SimServerState,
)
from taifeng.llm.providers.sim.shape import (
    ShapeSignature,
    extract_shape,
    shape_class_key,
)

__all__ = [
    "RecordedRequest",
    "RequestContractValidator",
    "RequestLedger",
    "RoutingSimClient",
    "SimClient",
    "SimContractViolation",
    "SimCoordinator",
    "SimServerState",
    "SimExpect",
    "SimFault",
    "SimFinish",
    "SimScriptExhausted",
    "SimTurn",
    "ShapeSignature",
    "extract_shape",
    "shape_class_key",
]
