# -*- coding: utf-8 -*-
"""
真实 HTTP 端到端验证：LLM 重试 / 备用通道 / 全失败降级（模型层只连本机 mock）
==============================================================================
用 http.server 起一个本机 mock「模型网关」，把 /api/chat 的 stream 链路真实跑一遍：
  A 正常出话（证明改动没伤主链路）
  B1 流式建连先 500 再 200 —— 应退避重试成功，用户看不到错误
  B2 非流式（预生成思考）先 500 再 200 —— 重试不拖垮主答复
  C 主通道端口无人监听 —— 重试耗尽后切备用通道（备用密钥/模型生效），仍能出话
  D 主备都不可用 —— SSE 给出 error + error_code + [DONE]，不 500 不裸堆栈
API_CONFIG 用测试专用假值（不含真实密钥），数据库走临时文件；被测的模型层只连本机 mock。
注意：/api/chat 的插件数据收集仍会打真实高德/美团接口（都有超时且失败即降级），
所以这条用例会几十秒，且断网时仍应通过——失败的是插件而非模型层。

运行：  python server/test_llm_retry_live.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_HERE, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_live_llm_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ["SMARTTOURISM_MEMORY"] = "0"
os.environ["SMARTTOURISM_LLM_CACHE"] = "0"
os.environ["SMARTTOURISM_LLM_RETRY"] = "1"
os.environ["SMARTTOURISM_LLM_RETRY_WAIT"] = "0.01"

_PASS = _FAIL = 0


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}{(' -> ' + str(extra))[:300] if extra else ''}")


# ==================== mock 模型网关 ====================

class Mock:
    def __init__(self):
        self.state = {"fail_first": 0, "fail_scope": "all", "attempts": []}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                is_stream = bool(body.get("stream"))
                outer.state["attempts"].append({
                    "path": self.path,
                    "model": body.get("model"),
                    "auth": bool(self.headers.get("Authorization")),
                    "stream": is_stream,
                })
                scope = outer.state["fail_scope"]
                hits = (scope == "all" or (scope == "stream") == is_stream)
                if hits and outer.state["fail_first"] > 0:
                    outer.state["fail_first"] -= 1
                    payload = b'{"error":"upstream boom"}'
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                text = "好的，为你规划杭州 3 天行程：首日西湖，次日报岛，第三日灵隐。"
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for i in range(0, len(text), 6):
                        chunk = {"choices": [{"delta": {"content": text[i:i + 6]}}]}
                        self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    payload = json.dumps({"choices": [{"message": {"content": text}}]},
                                         ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def reset(self, fail_first=0, scope="all"):
        self.state["fail_first"] = fail_first
        self.state["fail_scope"] = scope
        self.state["attempts"] = []

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def dead_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


import app as A  # noqa: E402

A.API_CONFIG = {"provider": "deepseek", "base_url": "http://127.0.0.1:1/v1",
                "api_key": "TEST-ONLY-FAKE-KEY", "model": "main-model",
                "temperature": 0.3, "max_tokens": 1024}
A.PROVIDERS = dict(A.PROVIDERS)
A.PROVIDERS.setdefault("deepseek", {"model": "main-model"})
gw = Mock()
gw_fb = Mock()
DEAD = dead_port()


def call_chat(conv, msg="我要去杭州玩3天，预算2000"):
    """跑一次真实 /api/chat（stream），返回 (事件列表, 正文, 错误字典或 None)。"""
    client = A.app.test_client()
    with A.app.test_request_context():
        resp = client.post("/api/chat", json={"message": msg, "skill": "",
                                              "conversation_id": conv, "stream": True})
        assert resp.status_code == 200, resp.status_code
        raw = resp.get_data(as_text=True)
    events, err = [], None
    for line in raw.split("\n"):
        if not line.startswith("data: "):
            continue
        s = line[6:].strip()
        if s == "[DONE]":
            events.append({"done": True})
            continue
        try:
            d = json.loads(s)
        except ValueError:
            continue
        events.append(d)
        if "error" in d:
            err = d
    text = "".join(e.get("content", "") for e in events if "content" in e)
    return events, text, err


def test_all_scenarios():
    print("\n[A] 正常链路：改动后 /api/chat 流式仍能出话")
    A.API_CONFIG["base_url"] = gw.base
    A._LLM_FALLBACK = None
    gw.reset()
    events, text, err = call_chat("live-ok")
    check("无错误事件", err is None, err)
    check("正文完整", "杭州" in text and len(text) > 10, text)
    check("收到 [DONE]", any(e.get("done") for e in events))
    check("下发了 context 占用事件", any(e.get("type") == "context" for e in events))
    check("主通道只被打 1 轮（含预生成思考则 2 轮）", 1 <= len(gw.state["attempts"]) <= 2,
          len(gw.state["attempts"]))

    print("\n[B1] 流式建连先 500 再 200：退避重试后用户无感")
    gw.reset(fail_first=1, scope="stream")
    events, text, err = call_chat("live-retry-stream")
    check("流式重试后不报错", err is None, err)
    check("流式重试后仍出话", "杭州" in text, text)
    st = [a for a in gw.state["attempts"] if a["stream"]]
    check("流式请求发了 2 次", len(st) == 2, len(st))
    check("重试沿用主通道模型", {a["model"] for a in gw.state["attempts"]} == {"main-model"},
          [a["model"] for a in gw.state["attempts"]])

    print("\n[B2] 非流式（预生成思考）先 500 再 200：重试不拖垮主答复")
    gw.reset(fail_first=1, scope="nonstream")
    events, text, err = call_chat("live-retry-nonstream")
    check("非流式重试后不报错", err is None, err)
    check("非流式重试后仍出话", "杭州" in text, text)
    ns = [a for a in gw.state["attempts"] if not a["stream"]]
    check("非流式请求发了 2 次", len(ns) == 2, len(ns))

    print("\n[C] 主通道端口不可达：重试耗尽后切备用通道")
    A.API_CONFIG["base_url"] = f"http://127.0.0.1:{DEAD}/v1"
    A._LLM_FALLBACK = A._build_llm_fallback({
        "SMARTTOURISM_LLM_FALLBACK_BASE_URL": gw_fb.base,
        "SMARTTOURISM_LLM_FALLBACK_KEY": "FB-TEST-KEY",
        "SMARTTOURISM_LLM_FALLBACK_MODEL": "fb-model"})
    gw_fb.reset()
    events, text, err = call_chat("live-fallback")
    check("切备用后不报错", err is None, err)
    check("切备用后仍出话", "杭州" in text, text)
    check("备用通道被打过", len(gw_fb.state["attempts"]) >= 1, len(gw_fb.state["attempts"]))
    models = {a["model"] for a in gw_fb.state["attempts"]}
    check("备用请求使用备用模型", models == {"fb-model"}, models)
    check("备用请求带备用密钥", all(a["auth"] for a in gw_fb.state["attempts"]))

    print("\n[D] 主备都不可用：SSE 给可读错误 + [DONE]，不 500")
    A._LLM_FALLBACK = A._build_llm_fallback({"SMARTTOURISM_LLM_FALLBACK_BASE_URL":
                                             f"http://127.0.0.1:{DEAD}/v1",
                                             "SMARTTOURISM_LLM_FALLBACK_KEY": "x",
                                             "SMARTTOURISM_LLM_FALLBACK_MODEL": "fb-model"})
    events, text, err = call_chat("live-dead")
    check("返回错误事件", err is not None)
    check("错误码为 network", err and err.get("error_code") == "network", err)
    check("错误文案可读（无堆栈/主机名）",
          err and "AI 服务" in err["error"] and "127.0.0.1" not in err["error"], err)
    check("错误后仍补 [DONE]", any(e.get("done") for e in events), events[-3:])


def main():
    print("=" * 60)
    print("LLM 重试 / 备用通道 真实 HTTP 端到端验证")
    print("=" * 60)
    try:
        test_all_scenarios()
    finally:
        gw.close()
        gw_fb.close()
    total = _PASS + _FAIL
    print("\n" + "=" * 60)
    print(f"结果：{_PASS} passed, {_FAIL} failed（共 {total}）")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
