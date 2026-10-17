# -*- coding: utf-8 -*-
"""
Agent 能力清单测试（app.py 的 _agent_module_status）
===================================================
这张表的用途是「不吹」：能说得上 Agent 能力的地方，必须能一眼看出哪些真在请求路径上跑、
哪些只有实现和测试。所以这条测试钉的不是文案，而是状态必须来自运行时真实信号：
  1. 已接线三项（memory / semantic_cache / rag_hybrid）的 state 随真实信号翻转，不是写死的；
  2. 未接线清单逐项要有「为什么没接」的说明，默认状态必须是 not_wired（不许含糊）；
  3. 未接线项一旦被别的代码 import 进来，状态自动翻成 imported 并计入 counts——
     表不会因为有人接了线还停留在「未接线」而变成假数据；
  4. /api/admin/ops 的 agent_modules 段与 /admin 面板都取得到。

运行：  python server/test_agent_modules.py
"""

from __future__ import annotations

import os
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_HERE, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ["SMARTTOURISM_DB"] = os.path.join(tempfile.mkdtemp(prefix="st_agentmods_test_"), "test.db")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")   # 不在测试里触发 embedder 装配

import app as A  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}{(' -> ' + str(extra))[:300] if extra else ''}")


def by_module(items):
    return {m["module"]: m for m in items}


def section(title):
    print(f"\n[{title}]")


def main():
    print("=" * 60)
    print("Agent 能力清单（已接线 / 建好未接线）测试")
    print("=" * 60)

    st = A._agent_module_status()
    section("1] 结构：三段齐全，已接线集合不许悄悄扩大")
    check("含 wired / unwired / counts", {"wired", "unwired", "counts"} <= set(st), list(st.keys()))
    check("已接线只有三项（新增接线要同时改状态来源）",
          {m["module"] for m in st["wired"]} == {"memory", "semantic_cache", "rag_hybrid"},
          [m["module"] for m in st["wired"]])
    check("counts.wired 与实际一致", st["counts"]["wired"] == len(st["wired"]), st["counts"])
    check("counts.unwired 与实际一致", st["counts"]["unwired"] == len(st["unwired"]), st["counts"])

    section("2] 已接线项：状态来自真实信号（改信号，状态就翻）")
    wired = by_module(st["wired"])
    mem = wired["memory"]
    check("未装配且开关开着时不谎报运行中", mem["state"] != "active", mem)
    saved_mod, saved_enabled = A._MEMORY_MOD, A.MEMORY_ENABLED
    try:
        A._MEMORY_MOD = object()
        check("装配成功 → active", by_module(A._agent_module_status()["wired"])["memory"]["state"] == "active")
        A._MEMORY_MOD = None
        A.MEMORY_ENABLED = False
        m2 = by_module(A._agent_module_status()["wired"])["memory"]
        check("环境变量关闭 → disabled 且写明是哪个开关",
              m2["state"] == "disabled" and "SMARTTOURISM_MEMORY" in m2["detail"], m2)
        A.MEMORY_ENABLED = True
        A._MEMORY_IMPORT_FAILED = True
        m3 = by_module(A._agent_module_status()["wired"])["memory"]
        check("导入失败 → unavailable（不是 active）", m3["state"] == "unavailable", m3)
    finally:
        A._MEMORY_MOD, A.MEMORY_ENABLED = saved_mod, saved_enabled
        A._MEMORY_IMPORT_FAILED = False
    st2 = A._agent_module_status()
    check("还原后回到真实状态", by_module(st2["wired"])["memory"]["state"] in
          ("wired_idle", "active", "disabled", "unavailable"),
          by_module(st2["wired"])["memory"])

    cache = by_module(st["wired"])["semantic_cache"]
    check("语义缓存本测试进程为 disabled（SMARTTOURISM_LLM_CACHE=0）",
          cache["state"] == "disabled", cache)
    rag = by_module(st["wired"])["rag_hybrid"]
    check("知识库检索状态取自 validation_stats",
          rag["state"] in ("wired_idle", "active", "degraded"), rag)

    section("3] 未接线清单：逐项有能力和理由，默认状态照实写未接线")
    names = [m["module"] for m in st["unwired"]]
    expected = ["graph", "multi_agent", "hitl", "tools", "mcp_server", "model_router",
                "guardrails", "evaluation", "tracing", "structured_output", "graphrag"]
    check("清单覆盖 11 个未接线模块", sorted(names) == sorted(expected), names)
    check("每项都写了能力一句话", all(m["capability"] for m in st["unwired"]))
    check("每项都解释了为什么没接", all(len(m["detail"]) >= 6 for m in st["unwired"]),
          [m["module"] for m in st["unwired"] if len(m["detail"]) < 6])
    check("没有一项自称 active",
          all(m["state"] in ("not_wired", "imported") for m in st["unwired"]))
    check("本测试进程里这些模块确实没被导入 → 全部 not_wired",
          all(m["state"] == "not_wired" for m in st["unwired"]),
          [(m["module"], m["state"]) for m in st["unwired"] if m["state"] != "not_wired"])

    section("4] /api/admin/ops 与 /admin 面板都取得到")
    body = A.app.test_client().get("/api/admin/ops").get_json()
    am = body.get("agent_modules") or {}
    check("ops 返回 agent_modules 段", bool(am.get("wired")) and bool(am.get("unwired")), list(body.keys()))
    check("ops 段与函数输出一致", am["counts"] == A._agent_module_status()["counts"], am.get("counts"))
    page = A.app.test_client().get("/admin").get_data(as_text=True)
    check("面板含 Agent 能力清单区块", "Agent 能力清单" in page and 'id="agents"' in page)

    section("5] 离线运行记录的能力/理由与运维口径逐字一致")
    # Agent模块离线运行记录.md 会进答辩材料，措辞若与 _AGENT_UNWIRED 各说一套就是自我加分。
    # 用 ast 取字典字面量而不是 import：import 会改 SMARTTOURISM_DB、关掉 logging 并写文件。
    import ast
    demo_path = os.path.join(_HERE, "agent_offline_demo.py")
    demo_src = open(demo_path, encoding="utf-8").read()
    demo_info = {}
    for node in ast.walk(ast.parse(demo_src)):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "MODULE_INFO" for t in node.targets):
            demo_info = ast.literal_eval(node.value)
    check("离线记录覆盖同样 11 个模块",
          sorted(demo_info) == sorted(m[0] for m in A._AGENT_UNWIRED), sorted(demo_info))
    for m in A._AGENT_UNWIRED:
        got = demo_info.get(m[0]) or ("", "")
        check(f"{m[0]}：能力描述与 app.py 一致", got[0] == m[1], (got[0], m[1]))
        check(f"{m[0]}：未接线理由与 app.py 一致", got[1] == m[2], (got[1], m[2]))
    check("离线记录声明了「模型输出为桩」", "模型输出为桩" in demo_src)
    check("离线生成器不读密钥配置、不发网络请求",
          "requests." not in demo_src and "urllib.request" not in demo_src
          and "api_key" not in demo_src)

    section("6] 自动翻转：模块一旦被 import，状态不再停留在未接线")
    import agent.model_router  # noqa: F401,E402  放在最后，前面的断言依赖它尚未被导入
    flipped = by_module(A._agent_module_status()["unwired"])["model_router"]
    check("import 后 → imported", flipped["state"] == "imported", flipped)
    check("counts.imported_but_unwired 跟上",
          A._agent_module_status()["counts"]["imported_but_unwired"] >= 1,
          A._agent_module_status()["counts"])

    total = _PASS + _FAIL
    print("\n" + "=" * 60)
    print(f"结果：{_PASS} passed, {_FAIL} failed（共 {total}）")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
