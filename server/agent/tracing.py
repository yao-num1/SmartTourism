# -*- coding: utf-8 -*-
"""
SmartTourism · 可观测性 / 链路追踪层
=====================================
给 Agent 运行加全链路追踪：每次请求记录一棵 span 树（LLM 调用 / 工具调用 / 节点），
含耗时、输入输出、token 用量、错误级别。

两条落地路径（默认都开）：
  1. 本地 JSONL：零配置，落盘到 data/traces/trace-*.jsonl —— 无需任何账号即可用、可离线分析；
  2. LangFuse（可选）：设置了 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 环境变量时，
     尽力把同样的 span 上报到 LangFuse（云或自部署）。**LangFuse 的任何异常都被吞掉**，
     绝不影响主链路——追踪是旁路，不能拖垮业务。

复用方式：用 wrap_call_llm / TracedToolRegistry 包一层，塞进 graph.py / multi_agent.py，
Agent 图跑起来就自动产出 trace。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("agent.tracing")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_TRACE_DIR = os.path.join(_ROOT, "data", "traces")
_MAX_STR = 2000  # 单字段落盘截断，避免 trace 文件爆炸


def _safe(obj: Any) -> Any:
    """转成可 JSON 序列化 + 截断长文本，保证落盘不炸。"""
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return obj if len(obj) <= _MAX_STR else obj[:_MAX_STR] + f"...<+{len(obj) - _MAX_STR}>"
    if isinstance(obj, dict):
        return {str(k): _safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe(v) for v in obj]
    return _safe(str(obj))


# ==================== Span 句柄 ====================

class _SpanHandle:
    """在 with 块内供调用方回填 output / usage / error。"""

    def __init__(self) -> None:
        self.output: Any = None
        self.usage: Optional[Dict[str, Any]] = None
        self.level: str = "DEFAULT"

    def set_output(self, output: Any) -> None:
        self.output = output

    def set_usage(self, usage: Dict[str, Any]) -> None:
        self.usage = usage

    def set_error(self, message: str) -> None:
        self.level = "ERROR"
        self.output = {"error": message}


class _NullSpan:
    """enabled=False 时的空句柄，吞掉所有 set_* 调用。"""
    def set_output(self, *a, **k): pass
    def set_usage(self, *a, **k): pass
    def set_error(self, *a, **k): pass


# ==================== Tracer ====================

class Tracer:
    """一次请求一个 Tracer；收集 spans，flush 时落盘（并尽力上报 LangFuse）。"""

    def __init__(
        self,
        trace_dir: Optional[str] = None,
        service_name: str = "smarttourism",
        langfuse_client: Any = None,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.service_name = service_name
        self.trace_dir = trace_dir or _DEFAULT_TRACE_DIR
        self._lf = langfuse_client
        self._spans: List[Dict[str, Any]] = []
        self._trace_id: Optional[str] = None
        self._trace_name: Optional[str] = None
        self._trace_start: Optional[float] = None
        self._trace_input: Any = None

    # ---- 生命周期 ----
    def start_trace(self, name: str, input: Any = None) -> "Tracer":
        self._trace_id = uuid.uuid4().hex
        self._trace_name = name
        self._trace_start = time.time()
        self._spans = []
        self._trace_input = input
        return self

    @contextmanager
    def span(self, name: str, kind: str = "span", input: Any = None,
             metadata: Optional[Dict[str, Any]] = None):
        """记录一个 span：with tracer.span("agent", kind="llm", input=...) as sp: ..."""
        if not self.enabled:
            yield _NullSpan()
            return

        start = time.time()
        handle = _SpanHandle()

        # LangFuse 旁路（尽力而为，异常吞掉）
        lf_obs = None
        if self._lf is not None:
            try:
                as_type = "generation" if kind == "llm" else "span"
                lf_obs = self._lf.start_observation(
                    name=name, as_type=as_type, input=_safe(input))
            except Exception as e:  # noqa: BLE001
                logger.debug("LangFuse start_observation 失败(忽略): %s", e)
                lf_obs = None

        try:
            yield handle
        finally:
            end = time.time()
            record = {
                "name": name,
                "kind": kind,
                "start": start,
                "duration_ms": round((end - start) * 1000, 2),
                "input": _safe(input),
                "output": _safe(handle.output),
                "level": handle.level,
                "metadata": _safe(metadata or {}),
            }
            if handle.usage:
                record["usage"] = _safe(handle.usage)
            self._spans.append(record)

            if lf_obs is not None:
                try:
                    lf_obs.update(output=_safe(handle.output), level=handle.level)
                    if handle.usage and kind == "llm":
                        lf_obs.update(usage_details=handle.usage)
                    lf_obs.end()
                except Exception as e:  # noqa: BLE001
                    logger.debug("LangFuse 更新 span 失败(忽略): %s", e)

    def flush(self) -> Optional[str]:
        """把本次 trace 落盘为一行 JSONL，返回文件路径（未启用则 None）。"""
        if not self.enabled or self._trace_id is None:
            return None
        end = time.time()
        record = {
            "trace_id": self._trace_id,
            "name": self._trace_name,
            "service": self.service_name,
            "start_iso": datetime.fromtimestamp(self._trace_start, tz=timezone.utc).isoformat(),
            "duration_ms": round((end - (self._trace_start or end)) * 1000, 2),
            "input": _safe(self._trace_input),
            "n_spans": len(self._spans),
            "spans": self._spans,
        }
        path = None
        try:
            os.makedirs(self.trace_dir, exist_ok=True)
            path = os.path.join(self.trace_dir, f"trace-{datetime.now():%Y%m%d}.jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.warning("trace 落盘失败: %s", e)

        if self._lf is not None:
            try:
                self._lf.flush()
            except Exception as e:  # noqa: BLE001
                logger.debug("LangFuse flush 失败(忽略): %s", e)

        # 重置，便于复用同一 Tracer 追踪下一次请求
        self._trace_id = None
        return path

    @property
    def spans(self) -> List[Dict[str, Any]]:
        return self._spans


# ==================== LangFuse 可选接入 ====================

def get_langfuse_client() -> Optional[Any]:
    """若配置了环境变量则返回 Langfuse 客户端，否则 None（本地追踪照常工作）。"""
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
    sk = os.environ.get("LANGFUSE_SECRET_KEY")
    if not pk or not sk:
        return None
    try:
        from langfuse import Langfuse  # type: ignore
        return Langfuse(
            public_key=pk, secret_key=sk,
            host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("LangFuse 客户端初始化失败，降级为仅本地追踪: %s", e)
        return None


def create_tracer(trace_dir: Optional[str] = None, use_langfuse: bool = True,
                  enabled: bool = True) -> Tracer:
    """工厂：默认自动探测 LangFuse 配置，没有就纯本地追踪。"""
    lf = get_langfuse_client() if use_langfuse else None
    return Tracer(trace_dir=trace_dir, langfuse_client=lf, enabled=enabled)


# ==================== 追踪包装器（鸭子类型，可直接替换原对象） ====================

def wrap_call_llm(call_llm_fn: Callable[..., Dict[str, Any]], tracer: Tracer,
                  name: str = "llm") -> Callable[..., Dict[str, Any]]:
    """包装 call_llm，自动记录每次 LLM 调用的耗时/用量/输出/错误。签名与原函数一致。"""

    def wrapped(messages, stream: bool = False, skill_name: str = "",
                tools: Optional[list] = None, tool_choice=None) -> Dict[str, Any]:
        with tracer.span(name, kind="llm", input={
            "n_messages": len(messages) if messages else 0,
            "skill": skill_name,
            "n_tools": len(tools or []),
            "stream": stream,
        }) as sp:
            resp = call_llm_fn(messages, stream=stream, skill_name=skill_name,
                               tools=tools, tool_choice=tool_choice)
            if isinstance(resp, dict) and "error" in resp:
                sp.set_error(str(resp["error"]))
            else:
                try:
                    msg = resp["choices"][0]["message"]
                    sp.set_output({
                        "role": msg.get("role"),
                        "chars": len(msg.get("content") or ""),
                        "tool_calls": len(msg.get("tool_calls") or []),
                    })
                except Exception:  # noqa: BLE001
                    sp.set_output({"raw_preview": str(resp)[:120]})
                if isinstance(resp, dict) and isinstance(resp.get("usage"), dict):
                    sp.set_usage(resp["usage"])
            return resp

    return wrapped


class TracedToolRegistry:
    """包装 ToolRegistry，call() 时记录工具 span。鸭子类型兼容 graph/multi_agent/function-calling。"""

    def __init__(self, registry: Any, tracer: Tracer) -> None:
        self._r = registry
        self._t = tracer

    def openai_schemas(self):
        return self._r.openai_schemas()

    def list(self):
        return self._r.list()

    def names(self):
        return self._r.names()

    def get(self, name):
        return self._r.get(name)

    def register(self, tool):
        return self._r.register(tool)

    def __contains__(self, name):
        return name in self._r

    def __len__(self):
        return len(self._r)

    def call(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        with self._t.span(f"tool:{name}", kind="tool", input=arguments) as sp:
            result = self._r.call(name, arguments)
            is_err = result.lstrip().startswith("{") and '"error"' in result[:40]
            if is_err:
                sp.set_error(result[:200])
            else:
                sp.set_output({"chars": len(result), "preview": result[:120]})
            return result
