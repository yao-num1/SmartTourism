# -*- coding: utf-8 -*-
"""
GraphRAG / 知识图谱 单元测试
============================
基于真实本地知识库（5 城市）验证：图谱构建、实体链接、ego 子图检索、
多跳「近」关系、最短路径、跨城市隔离、社区划分与摘要、LLM 上下文渲染。
纯离线、确定性。

运行：  python server/agent/test_graphrag.py
"""

from __future__ import annotations

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
for p in (_SERVER, _ROOT, os.path.join(_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent.graphrag import build_knowledge_graph, GraphRAG, _core_name  # noqa: E402

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


def test_core_name():
    print("\n[核心名提取]")
    check("去后缀", _core_name("西湖风景名胜区") == "西湖")
    check("无后缀保持", _core_name("楼外楼") == "楼外楼")
    check("不误删过短名", _core_name("景区") == "景区")


def test_build_graph():
    print("\n[图谱构建]")
    G = build_knowledge_graph()
    st_types = {}
    for _, d in G.nodes(data=True):
        st_types[d.get("type")] = st_types.get(d.get("type"), 0) + 1
    check("节点数 > 50", G.number_of_nodes() > 50, str(G.number_of_nodes()))
    check("含四类节点", {"city", "scenic", "hotel", "food"} <= set(st_types), str(st_types))
    check("5 个城市节点", st_types.get("city") == 5, str(st_types.get("city")))
    check("有边", G.number_of_edges() > 50)
    # near 关系存在（杭州酒店靠西湖）
    near = [(u, v) for u, v, d in G.edges(data=True) if d.get("rel") == "near"]
    check("存在 near 关系边", len(near) > 0, str(len(near)))


def test_entity_linking():
    print("\n[实体链接]")
    gr = GraphRAG()
    seeds = gr.link_entities("杭州西湖好玩吗")
    names = [gr.G.nodes[s].get("name") for s in seeds]
    check("链接到城市杭州", "杭州" in names, str(names))
    check("别名链接到西湖景点", any("西湖" in str(n) for n in names), str(names))
    check("空文本无链接", gr.link_entities("") == [])


def test_retrieve_and_context():
    print("\n[子图检索 + 上下文]")
    gr = GraphRAG()
    nodes = gr.retrieve("杭州西湖附近的酒店", max_hops=1, top_k=10)
    check("检索到节点", len(nodes) > 0)
    types = {n.get("type") for n in nodes}
    check("含酒店节点", "hotel" in types, str(types))
    check("全部属于杭州", all(n.get("city") == "杭州" for n in nodes),
          str({n.get("city") for n in nodes}))

    ctx = gr.context_for_llm("杭州西湖景点推荐", max_hops=1, top_k=8)
    check("上下文非空", len(ctx) > 0)
    check("上下文含知识图谱标题", "知识图谱" in ctx)
    check("上下文含西湖", "西湖" in ctx)


def test_multi_hop_near():
    print("\n[多跳：景点↔酒店 近关系]")
    gr = GraphRAG()
    # 西湖国宾馆 与 西湖风景名胜区 应通过 near 直接相连
    path = gr.shortest_path("西湖风景名胜区", "杭州西湖国宾馆")
    check("存在路径", path is not None, str(path))
    if path:
        check("路径为直接相邻(1跳)", len(path) == 2, str(path))


def test_shortest_path_via_city():
    print("\n[多跳：跨类型经城市相连]")
    gr = GraphRAG()
    # 某景点 与 某美食 之间应经「杭州」城市节点相连（2跳）
    path = gr.shortest_path("西湖风景名胜区", "楼外楼")
    check("景点↔美食 有路径", path is not None, str(path))
    if path:
        check("经过城市杭州", "杭州" in path, str(path))
    check("不存在实体 → None", gr.shortest_path("西湖风景名胜区", "不存在的地方XYZ") is None)


def test_cross_city_isolation():
    print("\n[跨城市隔离]")
    gr = GraphRAG()
    nodes = gr.retrieve("三亚的海边和酒店", max_hops=1, top_k=20)
    cities = {n.get("city") for n in nodes}
    check("只返回三亚节点", cities == {"三亚"}, str(cities))


def test_communities():
    print("\n[社区划分]")
    gr = GraphRAG()
    city_comms = gr.communities(method="city")
    check("按城市 = 5 个社区", len(city_comms) == 5, str(len(city_comms)))
    mod_comms = gr.communities(method="modularity")
    check("模块度社区非空", len(mod_comms) > 0)

    summary = gr.community_summary(method="city")
    check("摘要条数=社区数", len(summary) == len(city_comms))
    check("摘要含主导城市", all("dominant_city" in s for s in summary))
    check("摘要含规模", all(s["size"] > 0 for s in summary))
    dom = {s["dominant_city"] for s in summary}
    check("覆盖 5 个城市", len(dom) == 5, str(dom))


def test_stats_and_node():
    print("\n[统计与节点访问]")
    gr = GraphRAG()
    st = gr.stats()
    check("stats 含 nodes/edges", "nodes" in st and "edges" in st)
    check("node_types 含 hotel", "hotel" in st["node_types"])
    check("未知节点返回 None", gr.node("hotel:火星:不存在") is None)


def test_empty_graph():
    print("\n[空图谱不崩溃]")
    gr = GraphRAG(build_knowledge_graph({}))
    check("空图 stats=0", gr.stats()["nodes"] == 0)
    check("空图检索为空", gr.retrieve("杭州西湖") == [])
    check("空图上下文为空", gr.context_for_llm("杭州") == "")
    check("空图社区为空", gr.communities() == [])


def main():
    print("=" * 60)
    print("GraphRAG 单元测试")
    print("=" * 60)
    test_core_name()
    test_build_graph()
    test_entity_linking()
    test_retrieve_and_context()
    test_multi_hop_near()
    test_shortest_path_via_city()
    test_cross_city_isolation()
    test_communities()
    test_stats_and_node()
    test_empty_graph()
    print("\n" + "=" * 60)
    print(f"结果: {_PASS} passed, {_FAIL} failed")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
