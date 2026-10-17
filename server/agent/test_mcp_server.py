# -*- coding: utf-8 -*-
"""
MCP Server 单元测试 + 真实协议端到端测试
========================================
  - 单测 _list_tools_impl / _call_tool_impl（脱离传输层）
  - 端到端：用 MCP 内存版 client-server，让真实 ClientSession 连接本 server，
    完成 initialize → list_tools → call_tool 全流程，验证确实说的是标准 MCP 协议。

运行：  python server/agent/test_mcp_server.py
"""

from __future__ import annotations

import asyncio
import os
import sys

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

from agent.tools import ToolRegistry, build_registry_from_plugins  # noqa: E402
from agent.mcp_server import build_server, _list_tools_impl, _call_tool_impl  # noqa: E402

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


class _FakePluginRegistry:
    def __init__(self, plugins):
        self._plugins = plugins

    def list_all(self):
        return self._plugins


async def test_impl_functions():
    print("\n[1] 核心 impl 函数（脱离传输层）")
    reg = ToolRegistry()
    reg.register_function(
        "echo", "回显消息",
        {"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]},
        lambda msg: f"echo:{msg}")

    tools = await _list_tools_impl(reg)
    check("list_tools 返回 1 个工具", len(tools) == 1, str(len(tools)))
    check("工具名/ schema 正确",
          tools[0].name == "echo" and tools[0].inputSchema["properties"]["msg"]["type"] == "string",
          str(tools[0]))

    content = await _call_tool_impl(reg, "echo", {"msg": "hi"})
    check("call_tool 执行正确", content[0].text == "echo:hi", content[0].text)

    content_err = await _call_tool_impl(reg, "not_exist", {})
    check("未知工具返回可读错误而非抛异常", "未知工具" in content_err[0].text, content_err[0].text)


async def test_real_mcp_protocol():
    print("\n[2] 真实 MCP 协议端到端（内存版 client↔server）")
    try:
        from mcp.shared.memory import create_connected_server_and_client_session
    except Exception as e:  # noqa: BLE001
        check("导入内存版 client-server 助手", False, str(e))
        return
    try:
        from plugins.travel_data import LocalKnowledgePlugin
    except Exception as e:  # noqa: BLE001
        check("导入 LocalKnowledgePlugin", False, str(e))
        return

    reg = build_registry_from_plugins(_FakePluginRegistry([LocalKnowledgePlugin()]))
    server = build_server(reg)

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()

        listed = await client.list_tools()
        names = [t.name for t in listed.tools]
        check("客户端发现了 local_knowledge 工具", "local_knowledge" in names, str(names))
        lk = next((t for t in listed.tools if t.name == "local_knowledge"), None)
        check("工具带合法 inputSchema",
              lk is not None and lk.inputSchema.get("type") == "object"
              and "city" in lk.inputSchema.get("properties", {}),
              str(lk.inputSchema if lk else None))

        result = await client.call_tool("local_knowledge", {"city": "杭州", "type": "spots"})
        text = "".join(getattr(c, "text", "") for c in result.content)
        check("call_tool 未报错", not getattr(result, "isError", False), str(result)[:120])
        check("返回真实杭州景点数据", "杭州" in text and len(text) > 20, text[:100])
        print(f"     ↳ 返回预览: {text[:90].replace(chr(10), ' ')}...")


if __name__ == "__main__":
    print("=" * 60)
    print("🧪 MCP Server 测试")
    print("=" * 60)
    asyncio.run(test_impl_functions())
    asyncio.run(test_real_mcp_protocol())
    print("\n" + "=" * 60)
    print(f"结果: {_PASS} 通过 / {_FAIL} 失败")
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)
