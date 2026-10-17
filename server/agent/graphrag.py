# -*- coding: utf-8 -*-
"""
SmartTourism · GraphRAG / 文旅知识图谱
========================================
把本地知识库（5 城市 × 酒店/景点/美食/交通）构建成一张**异质知识图谱**（networkx），
用「实体链接 → 子图检索 → 多跳推理 → 社区摘要」的方式为 LLM 提供结构化事实上下文。

相比纯向量 RAG，图谱擅长**多跳/关系型**问题：
  「西湖附近评分高的酒店」「杭州 5A 景点适合哪个季节」「住 XX 酒店周边有什么吃的」——
  这些需要顺着 城市→景点→酒店 的边遍历，而不是靠语义相似度碰运气。

组件：
  - build_knowledge_graph(local_knowledge) → nx.Graph（节点带 type/city/属性，边带 rel）
  - GraphRAG：实体链接、ego 子图检索、事实上下文文本、最短路径、社区摘要、统计

设计原则：纯离线、零新依赖（networkx 已在环境）；图谱构建与检索全部确定性可测。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx

logger = logging.getLogger("agent.graphrag")

# 景点名常见后缀，用于生成「近」关系的别名匹配
_SPOT_SUFFIX = ("风景名胜区", "风景区", "景区", "古镇", "公园", "博物院", "博物馆", "度假区")


def _core_name(name: str) -> str:
    """去掉景点常见后缀，得到可用于模糊匹配的核心名（西湖风景名胜区 → 西湖）。"""
    for suf in _SPOT_SUFFIX:
        if name.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return name


def _nid(kind: str, city: str, name: str) -> str:
    return f"{kind}:{city}:{name}"


def _default_knowledge() -> Dict[str, Any]:
    try:
        from plugins.travel_data import LOCAL_KNOWLEDGE  # type: ignore
        return LOCAL_KNOWLEDGE
    except Exception:  # noqa: BLE001
        try:
            import os
            import sys
            _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            if _root not in sys.path:
                sys.path.insert(0, _root)
            from plugins.travel_data import LOCAL_KNOWLEDGE  # type: ignore
            return LOCAL_KNOWLEDGE
        except Exception as e:  # noqa: BLE001
            logger.warning("无法加载 LOCAL_KNOWLEDGE，使用空图谱: %s", e)
            return {}


def build_knowledge_graph(local_knowledge: Optional[Dict[str, Any]] = None) -> nx.Graph:
    """从本地知识库构建异质知识图谱。"""
    lk = local_knowledge if local_knowledge is not None else _default_knowledge()
    G = nx.Graph()

    for city, data in (lk or {}).items():
        if not isinstance(data, dict):
            continue
        G.add_node(_nid("city", city, city), type="city", city=city, name=city)

        spots = data.get("scenic_spots", []) or []
        spot_index: List[Tuple[str, str]] = []  # (node_id, core_name)
        for s in spots:
            name = s.get("name", "")
            if not name:
                continue
            nid = _nid("scenic", city, name)
            G.add_node(nid, type="scenic", city=city, name=name,
                       level=s.get("level", ""), ticket=s.get("ticket", ""),
                       duration=s.get("duration", ""), best_season=s.get("best_season", ""),
                       highlights=s.get("highlights", []))
            G.add_edge(_nid("city", city, city), nid, rel="has_scenic")
            spot_index.append((nid, _core_name(name)))

        for h in data.get("hotels", []) or []:
            name = h.get("name", "")
            if not name:
                continue
            nid = _nid("hotel", city, name)
            G.add_node(nid, type="hotel", city=city, name=name,
                       stars=h.get("stars"), price_range=h.get("price_range", ""),
                       rating=h.get("rating"), features=h.get("features", []),
                       location=h.get("location", ""))
            G.add_edge(_nid("city", city, city), nid, rel="has_hotel")

            # 「近」关系：酒店位置/特色里出现景点核心名，或有到西湖距离字段
            hay = f"{h.get('location', '')} {' '.join(h.get('features', []) or [])}"
            dist = h.get("distance_to_westlake")
            for spot_id, core in spot_index:
                if core and (core in hay or core in name):
                    G.add_edge(nid, spot_id, rel="near")
                elif dist and core == "西湖":
                    G.add_edge(nid, spot_id, rel="near", distance=dist)

        for f in data.get("food", []) or []:
            name = f.get("name", "")
            if not name:
                continue
            nid = _nid("food", city, name)
            G.add_node(nid, type="food", city=city, name=name,
                       cuisine=f.get("type", ""), avg_price=f.get("avg_price"),
                       rating=f.get("rating"), must_try=f.get("must_try", []),
                       tips=f.get("tips", ""))
            G.add_edge(_nid("city", city, city), nid, rel="has_food")
            # 美食与同城景点的弱关联（同在城市社区内，便于「景点周边吃什么」多跳）

    return G


class GraphRAG:
    """知识图谱检索增强。"""

    def __init__(self, graph: Optional[nx.Graph] = None,
                 local_knowledge: Optional[Dict[str, Any]] = None) -> None:
        self.G = graph if graph is not None else build_knowledge_graph(local_knowledge)
        # 名称 → 节点 id（长名优先，便于最长匹配）
        self._name_index: List[Tuple[str, str]] = sorted(
            [(str(d.get("name", "")), n) for n, d in self.G.nodes(data=True) if d.get("name")],
            key=lambda x: len(x[0]), reverse=True,
        )

    # ---- 基础 ----
    def stats(self) -> Dict[str, Any]:
        types: Dict[str, int] = {}
        for _, d in self.G.nodes(data=True):
            types[d.get("type", "?")] = types.get(d.get("type", "?"), 0) + 1
        return {"nodes": self.G.number_of_nodes(), "edges": self.G.number_of_edges(),
                "node_types": types}

    def node(self, nid: str) -> Optional[Dict[str, Any]]:
        return dict(self.G.nodes[nid]) if nid in self.G else None

    # ---- 实体链接 ----
    def link_entities(self, text: str) -> List[str]:
        """从查询文本里链接到图谱节点（最长匹配、去重、不重叠消费）。"""
        if not text:
            return []
        found: List[str] = []
        consumed = [False] * len(text)
        for name, nid in self._name_index:
            if not name:
                continue
            start = 0
            while True:
                i = text.find(name, start)
                if i < 0:
                    break
                if not any(consumed[i:i + len(name)]):
                    for k in range(i, i + len(name)):
                        consumed[k] = True
                    if nid not in found:
                        found.append(nid)
                start = i + len(name)
        # 别名核心名兜底（如「西湖」链接到「西湖风景名胜区」）
        for nid, d in self.G.nodes(data=True):
            if d.get("type") == "scenic" and nid not in found:
                core = _core_name(str(d.get("name", "")))
                if core and core != d.get("name") and core in text:
                    found.append(nid)
        return found

    # ---- 子图检索 ----
    def ego_nodes(self, seeds: List[str], max_hops: int = 1) -> List[str]:
        """seeds 的 max_hops 跳邻域节点集合（含 seeds）。"""
        collected = set(seeds)
        frontier = list(seeds)
        for _ in range(max(0, max_hops)):
            nxt = []
            for n in frontier:
                if n in self.G:
                    for nb in self.G.neighbors(n):
                        if nb not in collected:
                            collected.add(nb)
                            nxt.append(nb)
            frontier = nxt
        return list(collected)

    def subgraph(self, query: str, max_hops: int = 1) -> nx.Graph:
        seeds = self.link_entities(query)
        nodes = self.ego_nodes(seeds, max_hops) if seeds else []
        return self.G.subgraph(nodes).copy()

    def _score_node(self, nid: str, seeds: List[str]) -> float:
        d = self.G.nodes[nid]
        score = 0.0
        if nid in seeds:
            score += 3.0
        score += min(self.G.degree(nid), 6) * 0.2
        r = d.get("rating")
        if isinstance(r, (int, float)):
            score += float(r) * 0.3
        if d.get("type") == "scenic" and d.get("level"):
            score += 0.5
        return score

    def retrieve(self, query: str, max_hops: int = 1, top_k: int = 10) -> List[Dict[str, Any]]:
        """返回与查询相关的 top_k 节点（带属性 + 与种子的关系）。"""
        seeds = self.link_entities(query)
        if not seeds:
            return []
        nodes = self.ego_nodes(seeds, max_hops)
        ranked = sorted(nodes, key=lambda n: self._score_node(n, seeds), reverse=True)[:top_k]
        out: List[Dict[str, Any]] = []
        for nid in ranked:
            d = dict(self.G.nodes[nid])
            d["id"] = nid
            # 记录它连到哪些边（关系）
            rels = []
            for nb in self.G.neighbors(nid):
                ed = self.G.edges[nid, nb]
                rels.append({"rel": ed.get("rel", "related"),
                             "other": self.G.nodes[nb].get("name", nb)})
            d["relations"] = rels[:6]
            out.append(d)
        return out

    def context_for_llm(self, query: str, max_hops: int = 1, top_k: int = 10) -> str:
        """把子图事实渲染成给 LLM 的结构化上下文（GraphRAG 的检索产物）。"""
        nodes = self.retrieve(query, max_hops=max_hops, top_k=top_k)
        if not nodes:
            return ""
        lines: List[str] = ["【知识图谱检索结果】"]
        for d in nodes:
            t = d.get("type")
            name = d.get("name")
            city = d.get("city", "")
            if t == "city":
                lines.append(f"- 城市：{name}")
            elif t == "scenic":
                extra = "，".join(filter(None, [
                    d.get("level"), f"门票{d.get('ticket')}" if d.get("ticket") else "",
                    f"游玩{d.get('duration')}" if d.get("duration") else "",
                    f"最佳季节{d.get('best_season')}" if d.get("best_season") else ""]))
                lines.append(f"- [{city}] 景点：{name}（{extra}）" if extra else f"- [{city}] 景点：{name}")
            elif t == "hotel":
                extra = "，".join(filter(None, [
                    f"{d.get('stars')}星" if d.get("stars") else "",
                    f"评分{d.get('rating')}" if d.get("rating") else "",
                    f"价格区间{d.get('price_range')}" if d.get("price_range") else "",
                    d.get("location", "")]))
                lines.append(f"- [{city}] 酒店：{name}（{extra}）")
            elif t == "food":
                extra = "，".join(filter(None, [
                    d.get("cuisine", ""),
                    f"人均{d.get('avg_price')}" if d.get("avg_price") else "",
                    f"评分{d.get('rating')}" if d.get("rating") else ""]))
                lines.append(f"- [{city}] 美食：{name}（{extra}）")
            near = [r["other"] for r in d.get("relations", []) if r["rel"] == "near"]
            if near:
                lines.append(f"    临近：{'、'.join(near[:4])}")
        return "\n".join(lines)

    # ---- 多跳 / 路径 ----
    def shortest_path(self, name_a: str, name_b: str) -> Optional[List[str]]:
        """两个实体名之间的最短关系路径（返回显示名序列）。"""
        def find(name):
            for nm, nid in self._name_index:
                if nm == name:
                    return nid
            seeds = self.link_entities(name)
            return seeds[0] if seeds else None
        a, b = find(name_a), find(name_b)
        if not a or not b or a not in self.G or b not in self.G:
            return None
        try:
            path = nx.shortest_path(self.G, a, b)
        except nx.NetworkXNoPath:
            return None
        return [self.G.nodes[n].get("name", n) for n in path]

    # ---- 社区 ----
    def communities(self, method: str = "modularity") -> List[List[str]]:
        """社区划分：modularity（模块度）或 city（按城市，确定性）。返回节点 id 列表的列表。"""
        if self.G.number_of_nodes() == 0:
            return []
        if method == "city":
            buckets: Dict[str, List[str]] = {}
            for nid, d in self.G.nodes(data=True):
                buckets.setdefault(str(d.get("city", "?")), []).append(nid)
            return list(buckets.values())
        try:
            from networkx.algorithms.community import greedy_modularity_communities
            comms = greedy_modularity_communities(self.G)
            return [sorted(c) for c in comms]
        except Exception as e:  # noqa: BLE001
            logger.debug("模块度社区失败，回退按城市: %s", e)
            return self.communities(method="city")

    def community_summary(self, method: str = "modularity") -> List[Dict[str, Any]]:
        """每个社区的规模、主导城市、类型分布——可用于「全局摘要型」问答。"""
        out: List[Dict[str, Any]] = []
        for comm in self.communities(method):
            types: Dict[str, int] = {}
            cities: Dict[str, int] = {}
            for nid in comm:
                d = self.G.nodes[nid]
                types[d.get("type", "?")] = types.get(d.get("type", "?"), 0) + 1
                c = d.get("city", "?")
                cities[c] = cities.get(c, 0) + 1
            dominant = max(cities, key=cities.get) if cities else "?"
            out.append({"size": len(comm), "dominant_city": dominant,
                        "types": types, "sample": [self.G.nodes[n].get("name", n) for n in comm[:6]]})
        out.sort(key=lambda x: x["size"], reverse=True)
        return out
