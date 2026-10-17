# -*- coding: utf-8 -*-
"""
链路追踪（Tracing）单元测试
============================
确定性验证：span 记录（耗时/用量/错误级别）、JSONL 落盘可解析、无 LangFuse 时纯本地可用、
wrap_call_llm 与 TracedToolRegistry 包装器透传原对象行为。
不依赖网络、不依赖 LangFuse 账号。

运行：  python server/agent/test_tracing.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SERVER)
for p in (_SERVER, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent.tracing import (  # noqa: E402
    Tracer, create_tracer, wrap_call_llm, TracedToolRegistry, _safe,
)
from agent.tools import Tool, ToolRegistry  # noqa: E402
from agent.graph import create_agent_graph, run_agent  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        print(f"  ❌ {name}  {extra}")


# ==================== _safe 序列化 ====================

def test_safe():
    print("\n[_safe 序列化]")
    check("保留基本类型", _safe(3) == 3 and _safe(None) is None and _safe(True) is True)
    check("长文本被截断", _safe("x" * 3000).endswith(">") and len(_safe("x" * 3000)) < 2100)
    check("dict 递归", _safe({"a": "x" * 3000})["a"].endswith(">"))
    check("非序列化对象转字符串", isinstance(_safe(object()), str))


# ==================== span 记录 ====================

def test_span_recording():
    print("\n[span 记录]")
    tmp = tempfile.mkdtemp()
    try:
        t = Tracer(trace_dir=tmp, enabled=True)
        t.start_trace("agent.run", input={"q": "杭州3日游"})
        with t.span("classify", kind="span", input="杭州3日游") as sp:
            sp.set_output({"intent": "plan"})
        with t.span("llm", kind="llm", input=[{"role": "user"}]) as sp:
            sp.set_output({"chars": 42})
            sp.set_usage({"total_tokens": 128, "prompt_tokens": 100, "completion_tokens": 28})

        check("记录了 2 个 span", len(t.spans) == 2, f"got {len(t.spans)}")
        check("span 含耗时字段", all("duration_ms" in s for s in t.spans))
        check("span kind 正确", t.spans[1]["kind"] == "llm")
        check("usage 被记录", t.spans[1]["usage"]["total_tokens"] == 128)
        check("无 usage 的 span 不含该字段", "usage" not in t.spans[0])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_span_error_level():
    print("\n[错误级别]")
    tmp = tempfile.mkdtemp()
    try:
        t = Tracer(trace_dir=tmp)
        t.start_trace("x")
        with t.span("bad", kind="span") as sp:
            sp.set_error("boom")
        check("错误 span 标记为 ERROR", t.spans[0]["level"] == "ERROR")
        check("错误信息进入 output", t.spans[0]["output"] == {"error": "boom"})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_disabled_tracer():
    print("\n[enabled=False]")
    tmp = tempfile.mkdtemp()
    try:
        t = Tracer(trace_dir=tmp, enabled=False)
        t.start_trace("x")
        with t.span("s") as sp:
            sp.set_output({"a": 1})  # _NullSpan 吞掉
            sp.set_usage({"t": 1})
            sp.set_error("ignored")
        check("禁用时不记录 span", len(t.spans) == 0)
        check("禁用时 flush 返回 None", t.flush() is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ==================== JSONL 落盘 ====================

def test_flush_jsonl():
    print("\n[JSONL 落盘]")
    tmp = tempfile.mkdtemp()
    try:
        t = Tracer(trace_dir=tmp)
        t.start_trace("agent.run", input="杭州")
        with t.span("s") as sp:
            sp.set_output("ok")
        path = t.flush()

        check("返回落盘路径", path and os.path.exists(path), str(path))
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        rec = json.loads(lines[0])
        check("单行 JSON 可解析", isinstance(rec, dict))
        check("含 trace_id / name", rec.get("trace_id") and rec.get("name") == "agent.run")
        check("n_spans 与实际一致", rec["n_spans"] == len(rec["spans"]) == 1)
        check("含 start_iso / duration_ms", "start_iso" in rec and "duration_ms" in rec)

        # flush 后可复用同一 tracer
        check("flush 后 trace_id 重置", t._trace_id is None)
        t.start_trace("second")
        with t.span("s2"):
            pass
        p2 = t.flush()
        with open(p2, "r", encoding="utf-8") as f:
            check("追加为第二行", len([ln for ln in f if ln.strip()]) == 2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ==================== wrap_call_llm ====================

def _mock_call_llm_ok(messages, stream=False, skill_name="", tools=None, tool_choice=None):
    return {
        "choices": [{"message": {"role": "assistant", "content": "杭州西湖很美"}}],
        "usage": {"total_tokens": 30, "prompt_tokens": 20, "completion_tokens": 10},
    }


def _mock_call_llm_err(messages, stream=False, skill_name="", tools=None, tool_choice=None):
    return {"error": "上游超时"}


def test_wrap_call_llm():
    print("\n[wrap_call_llm]")
    tmp = tempfile.mkdtemp()
    try:
        t = Tracer(trace_dir=tmp)
        t.start_trace("llm-call")
        wrapped = wrap_call_llm(_mock_call_llm_ok, t)
        resp = wrapped([{"role": "user", "content": "hi"}], skill_name="tourism-advisor")

        check("透传原返回值", "choices" in resp and resp["choices"][0]["message"]["content"] == "杭州西湖很美")
        check("记录了 1 个 llm span", len(t.spans) == 1 and t.spans[0]["kind"] == "llm")
        check("span 记录了 usage", t.spans[0]["usage"]["total_tokens"] == 30)
        check("span 记录了输出字符数", t.spans[0]["output"]["chars"] == 6)
        check("span input 含 skill", t.spans[0]["input"]["skill"] == "tourism-advisor")

        # 错误路径
        t.start_trace("llm-err")
        wrapped_err = wrap_call_llm(_mock_call_llm_err, t)
        r2 = wrapped_err([{"role": "user", "content": "hi"}])
        check("错误时透传 error", r2.get("error") == "上游超时")
        check("错误 span 标记 ERROR", t.spans[-1]["level"] == "ERROR")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ==================== TracedToolRegistry ====================

def test_traced_tool_registry():
    print("\n[TracedToolRegistry]")
    tmp = tempfile.mkdtemp()
    try:
        reg = ToolRegistry()
        reg.register(Tool(
            name="echo",
            description="回显",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
            handler=lambda text="": f"echo:{text}",
        ))

        t = Tracer(trace_dir=tmp)
        t.start_trace("tools")
        traced = TracedToolRegistry(reg, t)

        check("透传 openai_schemas", len(traced.openai_schemas()) == 1)
        check("透传 names", traced.names() == ["echo"])
        check("透传 __contains__", "echo" in traced and "nope" not in traced)
        check("透传 __len__", len(traced) == 1)

        result = traced.call("echo", {"text": "hello"})
        check("透传 call 返回值", result == "echo:hello")
        check("记录了 tool span", len(t.spans) == 1 and t.spans[0]["name"] == "tool:echo")
        check("tool span 记录输入", t.spans[0]["input"] == {"text": "hello"})

        # 不存在的工具 → registry.call 返回 error 字符串 → span 标记 ERROR
        err = traced.call("missing", {})
        t2 = t.spans[-1]
        check("未知工具返回错误串", "error" in err.lower())
        check("错误工具 span 标记 ERROR", t2["level"] == "ERROR")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ==================== create_tracer（无 LangFuse）====================

def test_create_tracer_local_only():
    print("\n[create_tracer 纯本地]")
    # 确保无 LangFuse 环境变量 → 纯本地追踪
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        os.environ.pop(k, None)
    tmp = tempfile.mkdtemp()
    try:
        t = create_tracer(trace_dir=tmp, use_langfuse=True)
        check("无配置时不接 LangFuse 客户端", t._lf is None)
        t.start_trace("local")
        with t.span("s"):
            pass
        path = t.flush()
        check("纯本地仍能落盘", path and os.path.exists(path))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_end_to_end_graph_tracing():
    print("\n[端到端：图 + tracer]")
    tmp = tempfile.mkdtemp()
    try:
        # 脚本化 LLM：先要求调 get_weather 工具，再给最终答复
        scripted = [
            {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "get_weather",
                              "arguments": json.dumps({"city": "杭州"}, ensure_ascii=False)}}]}}],
             "usage": {"total_tokens": 40}},
            {"choices": [{"message": {"role": "assistant", "content": "杭州今天晴，25℃。"}}],
             "usage": {"total_tokens": 25}},
        ]
        calls = {"n": 0}

        def mock_llm(messages, stream=False, skill_name="", tools=None, tool_choice=None):
            i = calls["n"]
            calls["n"] += 1
            return scripted[i] if i < len(scripted) else scripted[-1]

        reg = ToolRegistry()
        reg.register_function(
            "get_weather", "查询城市天气",
            {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
            lambda city: {"city": city, "weather": "晴", "temp_c": 25})

        t = Tracer(trace_dir=tmp)
        graph = create_agent_graph(
            call_llm_fn=wrap_call_llm(mock_llm, t),
            tool_registry=TracedToolRegistry(reg, t),
        )
        out = run_agent(graph, "杭州天气怎么样", tracer=t)

        check("图正常给出答复", out["reply"] == "杭州今天晴，25℃。", str(out["reply"]))
        check("工具被调用 1 次", out["tool_calls"] == 1, str(out["tool_calls"]))

        # 落盘的 trace 应包含 llm span、tool span、graph.invoke 根 span
        path = os.path.join(tmp, os.listdir(tmp)[0])
        with open(path, "r", encoding="utf-8") as f:
            rec = json.loads(f.readline())
        names = [s["name"] for s in rec["spans"]]
        check("trace 含根 span graph.invoke", "graph.invoke" in names, str(names))
        check("trace 含 llm span", any(s["kind"] == "llm" for s in rec["spans"]))
        check("trace 含 tool span", "tool:get_weather" in names, str(names))
        check("llm span 记录了 token 用量",
              any(s.get("usage", {}).get("total_tokens") == 40 for s in rec["spans"]))
        check("n_spans 与 spans 数量一致", rec["n_spans"] == len(rec["spans"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 60)
    print("Tracing 单元测试")
    print("=" * 60)
    test_safe()
    test_span_recording()
    test_span_error_level()
    test_disabled_tracer()
    test_flush_jsonl()
    test_wrap_call_llm()
    test_traced_tool_registry()
    test_create_tracer_local_only()
    test_end_to_end_graph_tracing()
    print("\n" + "=" * 60)
    print(f"结果: {_PASS} passed, {_FAIL} failed")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
