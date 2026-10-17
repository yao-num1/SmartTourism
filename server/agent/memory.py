# -*- coding: utf-8 -*-
"""
SmartTourism · 长期记忆模块（Long-term Memory）
================================================

会话级上下文（context_manager）管的是「这一次聊了什么」，本模块管的是
「关于这个用户，我们长期知道什么」——跨会话沉淀、语义召回、注入提示词。

流程（每轮对话）：
  1. 写入：用户话轮 → 抽取候选记忆（规则抽取 + 可选 LLM 抽取）→ 归一化 →
     相似去重 / 同槽位覆盖 → 落 memories 表（带向量、置信度、命中数）；
  2. 读取：当前问题 → 语义召回 top-k（相似度 × 置信度 × 时间衰减 × 命中强化）→
     拼成「用户长期记忆」块注入提示词，并显式声明「与本轮说法冲突时以本轮为准」。

设计约束（对齐项目现状）：
  - 零新增重依赖：向量复用 agent.semantic_cache.make_embedder（TF-IDF 离线可用，
    有 sentence-transformers 时自动升级 bge-small-zh）；存储复用 server/database.py；
  - 全链路可降级：DB / 嵌入 / LLM 任一不可用都不该打断对话，只是记忆变弱；
  - 记忆是「参考」不是「事实源」：注入块明确降级语义，避免旧偏好压过用户新说法。

运行/测试：  python server/agent/test_memory.py
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("agent.memory")

# ---- 可调参数 ----
RECALL_TOP_K = int(os.environ.get("MEM_TOP_K", "5"))
RECALL_MIN_SCORE = float(os.environ.get("MEM_MIN_SCORE", "0.42"))
DEDUP_SIM = float(os.environ.get("MEM_DEDUP_SIM", "0.88"))
MAX_MEMORIES_PER_USER = int(os.environ.get("MEM_MAX", "200"))
TIME_DECAY_DAYS = float(os.environ.get("MEM_DECAY_DAYS", "90"))
# 相似度/置信度/时间衰减/命中 四因子权重（和为 1）
W_SIM, W_CONF, W_DECAY, W_HIT = 0.55, 0.20, 0.15, 0.10

KINDS = ("preference", "fact", "constraint", "event")
KIND_LABEL = {"preference": "偏好", "fact": "事实", "constraint": "约束", "event": "事件"}

# 同槽位「后者覆盖前者」的槽位（数值/单一事实类）；其余槽位（忌口、排斥项、风格）是累加的
OVERRIDE_SLOTS = {"budget", "days", "crowd", "transport", "lodging", "style", "pace"}

MEMORY_CUE_RE = re.compile(
    r"喜欢|偏好|倾向|想要|希望|习惯|一般|通常|每次|最爱|讨厌|不喜欢|不想|不吃|不喝|不要|不接受|"
    r"不要|避开|恐高|晕车|晕船|轮椅|婴儿车|预算|人均|带娃|带父母|同行|情侣|亲子|团建|"
    r"民宿|酒店|青旅|露营|高铁|火车|飞机|自驾|大巴|特种兵|深度游|休闲|慢节奏|躺平|打卡|citywalk|逛吃")


@dataclass
class MemoryCandidate:
    """一条待写入的候选记忆。"""
    content: str
    kind: str = "preference"
    slot: Optional[str] = None
    confidence: float = 0.6
    meta: Dict[str, Any] = field(default_factory=dict)


# ==================== 向量编解码（存 JSON，避免引入 scipy 依赖） ====================

def encode_vector(vec: Any) -> Optional[str]:
    """把嵌入向量序列化成 JSON。支持 scipy 稀疏行向量与稠密序列（numpy/list）。"""
    if vec is None:
        return None
    try:
        if hasattr(vec, "tocsr"):  # scipy sparse
            coo = vec.tocsr().tocoo()
            pairs = sorted(zip([int(c) for c in coo.col], [float(x) for x in coo.data]),
                           key=lambda p: p[0])[:8192]
            if not pairs:
                return None
            return json.dumps({"k": "s", "n": int(vec.shape[1]),
                               "i": [p[0] for p in pairs],
                               "v": [round(p[1], 5) for p in pairs]})
        values = vec.tolist() if hasattr(vec, "tolist") else list(vec)
        flat = [float(x) for x in values]
        if not flat:
            return None
        return json.dumps({"k": "d", "n": len(flat), "v": [round(x, 5) for x in flat][:4096]})
    except Exception as e:  # noqa: BLE001
        logger.debug("向量编码失败: %s", e)
        return None


def _as_dict(payload: dict) -> Dict[int, float]:
    if payload.get("k") == "s":
        return dict(zip(payload.get("i") or [], payload.get("v") or []))
    return {i: v for i, v in enumerate(payload.get("v") or [])}


def vector_dot(a_json: Optional[str], b_json: Optional[str]) -> Optional[float]:
    """两个已 L2 归一化的向量点积 == 余弦相似度（TF-IDF 与 bge 都归一化过）。"""
    if not a_json or not b_json:
        return None
    try:
        a = json.loads(a_json)
        b = json.loads(b_json)
        if a.get("k") == "d" and b.get("k") == "d":
            va, vb = a.get("v") or [], b.get("v") or []
            return sum(x * y for x, y in zip(va, vb))
        da, db = _as_dict(a), _as_dict(b)
        if len(da) > len(db):
            da, db = db, da
        return sum(v * db.get(i, 0.0) for i, v in da.items())
    except Exception:  # noqa: BLE001
        return None


def lexical_sim(a: str, b: str) -> float:
    """字符 bigram Jaccard —— 没有向量时的兜底相似度。"""
    def grams(s: str) -> set:
        s = re.sub(r"\s+", "", s or "")
        return {s[i:i + 2] for i in range(len(s) - 1)} or {s} if s else set()
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


# ---- 槽位话题词：字向量对短文本很弱（「推荐点吃的」和「不吃辣」余弦几乎为 0），
# 但按记忆槽位召回是长期记忆的本职，所以给「问题命中该槽位的话题词」一个固定相关度。
SLOT_TOPICS = {
    "diet": ("吃", "餐", "菜", "食", "口味", "忌口", "美食", "逛吃"),
    "lodging": ("住", "酒店", "民宿", "房", "住宿", "订房"),
    "transport": ("交通", "高铁", "飞机", "火车", "自驾", "大巴", "路上", "到达", "怎么去"),
    "days": ("天", "日程", "时长", "假期", "几天", "行程"),
    "budget": ("预算", "花费", "钱", "贵", "便宜", "费用", "人均", "多少"),
    "style": ("节奏", "安排", "规划", "玩", "逛", "累", "轻松", "行程"),
    "crowd": ("带", "孩子", "宝宝", "父母", "老人", "同行", "一家", "情侣"),
    "accessibility": ("轮椅", "行动", "方便", "无障碍", "恐高", "晕车", "晕船", "推车"),
    "avoid": ("推荐", "安排", "去处", "景点", "行程"),
    "destination": ("去", "目的地", "城市", "玩", "旅行"),
}
TOPIC_BONUS = float(os.environ.get("MEM_TOPIC_BONUS", "0.75"))


def _topic_hit(query: str, slot: Optional[str]) -> bool:
    words = SLOT_TOPICS.get(slot or "")
    if not words:
        return False
    return any(w in query for w in words)


# ==================== 嵌入器（单例，可注入替换） ====================

_embedder: Any = None
_embed_lock = threading.Lock()


def get_embedder():
    """复用 semantic_cache 的嵌入工厂：离线走 TF-IDF，装了模型自动升级 bge-small-zh。"""
    global _embedder
    with _embed_lock:
        if _embedder is None:
            try:
                try:
                    from agent.semantic_cache import make_embedder
                except ImportError:
                    from semantic_cache import make_embedder
                _embedder = make_embedder(
                    prefer_neural=os.environ.get("MEM_NEURAL", "0") == "1")
            except Exception as e:  # noqa: BLE001
                logger.warning("嵌入器不可用，退化为词面相似度: %s", e)
                _embedder = False
        return _embedder or None


def set_embedder(emb: Any) -> None:
    """注入自定义嵌入器（单测用），传 None 恢复自动选择。"""
    global _embedder
    with _embed_lock:
        _embedder = emb if emb is not None else None


def _embed_json(text: str) -> Optional[str]:
    emb = get_embedder()
    if not emb:
        return None
    try:
        return encode_vector(emb.embed(text))
    except Exception:
        return None


def _sim(query: str, q_json: Optional[str], row: dict, use_topic: bool = True) -> float:
    """查询与一条记忆的相关度：字向量余弦、词面 Jaccard、槽位话题命中，三者取最大。

    取最大而非加权：三种信号互补（向量管近义、词面管短文本、话题管「问吃的就该想起忌口」），
    任一命中即认为相关，避免弱嵌入器把该记住的约束漏掉。
    use_topic=False 用于判重 —— 「两条忌口」不该因为都含「吃」被判成同一条。
    """
    content = row.get("content") or ""
    best = lexical_sim(query, content)
    cached = row.get("embedding")
    if q_json and cached:
        got = vector_dot(q_json, cached)
        if got is not None:
            best = max(best, got)
    if use_topic and _topic_hit(query, row.get("slot")):
        best = max(best, TOPIC_BONUS)
    return max(0.0, min(1.0, best))


# ==================== DB 访问（database 不可用时整体降级为「无记忆」） ====================

def _db():
    if os.environ.get("MEM_PERSIST", "1") == "0":
        return None
    try:
        import database
        return database
    except Exception:
        return None


def normalize_content(text: str) -> str:
    """记忆正文归一化：去空白/标点/大小写差异，便于「同义写法」判重。"""
    if not text:
        return ""
    s = re.sub(r"[\s，。、；：!！?？,.;:]+", "", (text or "")).lower()
    return s[:120]


def _days_since(ts: Optional[str]) -> float:
    if not ts:
        return 0.0
    try:
        dt = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
        return max(0.0, (datetime.now() - dt).total_seconds() / 86400.0)
    except Exception:
        return 0.0


# ==================== 规则抽取（离线、零成本、可测） ====================

_CITIES = list(dict.fromkeys([
    '杭州', '三亚', '成都', '北京', '大理', '丽江', '上海', '广州', '深圳', '厦门', '桂林',
    '西安', '南京', '重庆', '青岛', '武汉', '长沙', '昆明', '张家界', '黄山', '九寨沟',
    '拉萨', '哈尔滨', '苏州', '乌镇', '温州', '宁波', '南宁', '海口', '珠海', '敦煌',
    '阳朔', '北海', '长白山', '福州', '洛阳', '开封', '张掖', '伊犁', '呼伦贝尔',
]))
_CROWD = [('亲子', r'亲子|带娃|小孩|孩子|宝宝'), ('情侣', r'情侣|蜜月|约会|二人|对象'),
          ('老年', r'老人|爸妈|父母|银发|退休'), ('团建', r'团建|公司|团队'),
          ('学生', r'学生|毕业|穷游'), ('商务', r'商务|出差|会议')]

_POS_HEADS = r'(?:喜欢|最爱|偏好|向往|想去|钟情|惦记|打算去|考虑)'
_NEG_HEADS = r'(?:不想去|不喜欢|讨厌|排斥|别推荐|避开)'


def _city_near(pattern_head: str, text: str, guard_negation: bool = True) -> List[str]:
    """在情绪动词附近（±8 字窗口）找城市名。

    guard_negation=True 时跳过「不喜欢/别推荐」这类被否定词修饰的正向动词 —— 否则
    「不喜欢杭州」会同时命中正向和负向，记成自相矛盾的两条记忆。
    """
    out: List[str] = []
    for m in re.finditer(pattern_head, text):
        head = text[max(0, m.start() - 1):m.start()]
        if guard_negation and head in ("不", "别", "没", "无"):
            continue
        window = text[m.end():m.end() + 8] + text[max(0, m.start() - 6):m.start()]
        for city in _CITIES:
            if city in window and city not in out:
                out.append(city)
    return out


def extract_rule_based(text: str) -> List[MemoryCandidate]:
    """从一句用户话轮里抽取长期偏好/约束。全部是确定性正则，离线可测、零调用成本。"""
    out: List[MemoryCandidate] = []
    if not text or len(text) < 4:
        return out
    add = out.append

    # 喜欢 / 想去的目的地
    for city in _city_near(_POS_HEADS, text):
        add(MemoryCandidate(f"想去{city}", "preference", "destination", 0.7))
    # 明确排斥的目的地（这里的动词本身就是否定式，不需要再屏蔽前导否定词）
    for city in _city_near(_NEG_HEADS, text, guard_negation=False):
        add(MemoryCandidate(f"排斥{city}", "constraint", "avoid", 0.75))

    # 预算（只沉淀「长期倾向」级别的表述，避免把一次性的询价当永久事实）
    m = re.search(r'(?:预算|人均)[^\d]{0,4}(\d+)\s*(万|k|K|元|块)?', text)
    if not m:
        m = re.search(r'(\d+)\s*(万|k|K|元|块)?\s*(?:预算|人均)', text)
    if m:
        amount = int(m.group(1))
        unit = m.group(2) or ""
        if unit == "万":
            amount *= 10000
        elif unit in ("k", "K"):
            amount *= 1000
        add(MemoryCandidate(f"人均预算约{amount}元", "preference", "budget",
                           0.6 if re.search(r'一般|通常|大概|左右', text) else 0.7))

    # 天数上限/偏好
    m = re.search(r'(?:最多|不超过|控制在|别超过)\s*(\d+)\s*天', text)
    if m:
        add(MemoryCandidate(f"行程最多{m.group(1)}天", "constraint", "days", 0.7))

    # 人群构成
    for label, pat in _CROWD:
        if re.search(pat, text):
            add(MemoryCandidate(f"出行人群常为{label}", "fact", "crowd", 0.55))
            break

    # 忌口 / 饮食约束
    for m in re.finditer(r'(不吃|不喝|忌口|过敏|不能吃|不习惯)([^，。；;!！?？\n]{1,8})', text):
        item = re.sub(r'\s', '', m.group(2))[:8]
        if item:
            add(MemoryCandidate(f"{m.group(1)}{item}", "constraint", "diet", 0.8))

    # 交通偏好（先判否定式，避免「不坐飞机」被当成正向偏好）
    for mode in ("高铁", "火车", "飞机", "自驾", "大巴", "轮渡", "骑行"):
        neg = re.search(rf'(不坐|不开|不想坐|不想开|排斥|怕)[^，。；!\n]{{0,4}}{mode}', text)
        pos = (re.search(rf'(尽量|优先|习惯|喜欢|只坐|一般坐|都坐|坐|乘)[^，。；!\n]{{0,4}}{mode}', text)
               or re.search(rf'{mode}[^，。；!\n]{{0,4}}(方便|便宜|快|舒服|优先)', text))
        if neg:
            add(MemoryCandidate(f"不坐{mode}", "constraint", "transport", 0.75))
        elif pos:
            add(MemoryCandidate(f"偏好{mode}出行", "preference", "transport", 0.6))

    # 住宿偏好
    for stay in ("民宿", "青旅", "露营", "度假酒店", "星级酒店", "连锁酒店", "海景房"):
        if stay in text and re.search(r'住|订|选|偏好|喜欢|要', text):
            neg = re.search(rf'(?:不|别|不想)[^，。；!\n]{{0,3}}{stay}', text)
            add(MemoryCandidate((f"不住{stay}" if neg else f"住宿偏好{stay}"),
                                "constraint" if neg else "preference", "lodging", 0.65))
            break

    # 玩法风格 / 节奏
    style_map = [("特种兵式高强度", '特种兵|赶场|多打卡'), ("休闲慢节奏", '休闲|慢游|躺平|不赶|慢节奏|松弛'),
                 ("深度文化游", '深度游|人文|博物馆|历史|古镇'), ("自然风光徒步", '徒步|登山|自然风光|海景|湖边'),
                 ("美食逛吃", '逛吃|美食|小吃|夜市'), ("亲子游乐", '乐园|动物园|遛娃|游乐')]
    for label, pat in style_map:
        if re.search(pat, text) and re.search(r'喜欢|偏好|想|倾向|更爱|习惯|不要|不想', text):
            neg = bool(re.search(rf'(不喜欢|不想|别|避免|讨厌)[^，。；!\n]{{0,6}}(?:{pat})', text))
            add(MemoryCandidate((f"不喜欢{label}" if neg else f"偏好{label}"),
                                "constraint" if neg else "preference", "style", 0.6))
            break

    # 无障碍 / 特殊需求（安全相关，置信度给高）
    for need, pat in [("恐高", r'恐高'), ("晕车", r'晕车'), ("晕船", r'晕船'),
                      ("需轮椅无障碍", r'轮椅|行动不便|腿脚不便'), ("需推婴儿车", r'婴儿车|推车')]:
        if re.search(pat, text):
            add(MemoryCandidate(need, "constraint", "accessibility", 0.85))

    return out


# ==================== LLM 抽取（可选，抽取自由表述里的偏好） ====================

_EXTRACT_PROMPT = (
    "你是记忆抽取器。从用户这句话里提炼「长期有效」的个人偏好、约束或事实，"
    "忽略一次性的问询。只输出 JSON 数组，每项形如 "
    '{"kind":"preference|fact|constraint|event","slot":"destination|budget|days|crowd|diet|'
    'transport|lodging|style|pace|accessibility|avoid|other","content":"不超过20字的中文陈述"}。'
    "没有可提炼的长期信息就输出 []。不要输出任何解释。")


def extract_with_llm(text: str, llm_fn: Callable[[List[Dict[str, Any]]], Dict[str, Any]]
                     ) -> List[MemoryCandidate]:
    """用大模型补充规则漏掉的偏好。llm_fn 即 app.call_llm(messages)；失败返回空列表。"""
    if not llm_fn or not text or len(text) < 6:
        return []
    msgs = [{"role": "system", "content": _EXTRACT_PROMPT},
            {"role": "user", "content": text[:600]}]
    try:
        result = llm_fn(msgs)
    except Exception as e:  # noqa: BLE001
        logger.debug("LLM 记忆抽取调用异常: %s", e)
        return []
    if not isinstance(result, dict) or "error" in result:
        return []
    try:
        raw = result["choices"][0]["message"]["content"] or ""
    except Exception:
        return []
    return parse_llm_candidates(raw)


def parse_llm_candidates(raw: str) -> List[MemoryCandidate]:
    """解析 LLM 输出的 JSON 数组（容忍 ```json 围栏和前后废话）。"""
    if not raw:
        return []
    m = re.search(r"\[.*\]", raw, flags=re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except Exception:
        return []
    out: List[MemoryCandidate] = []
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        content = re.sub(r"\s+", "", str(it.get("content") or ""))[:40]
        if len(content) < 2:
            continue
        kind = it.get("kind") if it.get("kind") in KINDS else "preference"
        slot = str(it.get("slot") or "other")[:24]
        out.append(MemoryCandidate(content, kind, slot, 0.6))
    return out


# ==================== 写入：去重 / 覆盖 / 落库 ====================

def remember(user_id: str, candidates: List[MemoryCandidate],
             session_id: Optional[str] = None) -> Dict[str, int]:
    """把候选记忆写入长期库。返回 {"added": n, "updated": m, "skipped": k}。"""
    d = _db()
    stats = {"added": 0, "updated": 0, "skipped": 0}
    if not d or not candidates:
        stats["skipped"] = len(candidates)
        return stats

    existing = d.list_memories(user_id=user_id, limit=MAX_MEMORIES_PER_USER * 2)
    for cand in candidates:
        content = (cand.content or "").strip()
        if len(content) < 2:
            stats["skipped"] += 1
            continue
        norm = normalize_content(content)
        emb_json = _embed_json(content)
        same_slot = [r for r in existing if (r.get("slot") or "other") == (cand.slot or "other")]

        # 1) 判等：同槽位 + 归一化文本相同或向量高度相似 → 强化既有记忆，不新增副本
        dup = None
        for r in same_slot:
            if normalize_content(r.get("content", "")) == norm:
                dup = r
                break
            sim = (_sim(content, emb_json, r, use_topic=False) if emb_json
                   else lexical_sim(content, r.get("content", "")))
            if sim >= DEDUP_SIM:
                dup = r
                break
        if dup:
            new_conf = min(1.0, float(dup.get("confidence") or 0.5) + 0.1)
            d.update_memory(dup["memory_id"], content=content,
                            embedding=emb_json, confidence=new_conf, kind=cand.kind)
            dup["content"], dup["confidence"] = content, new_conf
            stats["updated"] += 1
            continue

        # 2) 同槽位冲突：单一事实类槽位（预算/天数/交通…）以最新说法为准，其余并存
        if cand.slot in OVERRIDE_SLOTS and same_slot:
            old = same_slot[0]
            d.update_memory(old["memory_id"], content=content, embedding=emb_json,
                            confidence=max(cand.confidence, 0.6), kind=cand.kind)
            old["content"] = content
            stats["updated"] += 1
            continue

        mid = "mem_" + uuid.uuid4().hex[:16]
        d.insert_memory({
            "memory_id": mid, "user_id": user_id, "kind": cand.kind if cand.kind in KINDS else "preference",
            "slot": cand.slot or "other", "content": content, "source_session": session_id,
            "confidence": cand.confidence, "embedding": emb_json,
        })
        existing.append({"memory_id": mid, "slot": cand.slot or "other",
                         "content": content, "confidence": cand.confidence,
                         "embedding": emb_json, "hit_count": 0,
                         "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        stats["added"] += 1

    if stats["added"]:
        try:
            d.prune_memories(user_id, keep=MAX_MEMORIES_PER_USER)
        except Exception:
            pass
    return stats


# ==================== 读取：召回 + 注入块 ====================

def score_memory(query: str, q_json: Optional[str], row: dict) -> Tuple[float, Dict[str, float]]:
    sim = _sim(query, q_json, row)
    conf = float(row.get("confidence") or 0.5)
    decay = math.exp(-_days_since(row.get("updated_at")) / max(1.0, TIME_DECAY_DAYS))
    hit = min(1.0, int(row.get("hit_count") or 0) / 10.0)
    score = W_SIM * sim + W_CONF * conf + W_DECAY * decay + W_HIT * hit
    return score, {"sim": round(sim, 3), "conf": round(conf, 3),
                   "decay": round(decay, 3), "hit": round(hit, 3)}


def recall(user_id: str, query: str, top_k: int = RECALL_TOP_K,
           min_score: float = RECALL_MIN_SCORE, touch: bool = True) -> List[Dict[str, Any]]:
    """语义召回与当前问题相关的长期记忆，按综合分排序。"""
    d = _db()
    if not d or not query or not query.strip():
        return []
    try:
        rows = d.list_memories(user_id=user_id, limit=MAX_MEMORIES_PER_USER * 2)
    except Exception as e:  # noqa: BLE001
        logger.warning("记忆读取失败（本轮不注入长期记忆）: %s", e)
        return []
    q_json = _embed_json(query)
    scored: List[Tuple[float, Dict[str, float], dict]] = []
    for r in rows:
        sc, parts = score_memory(query, q_json, r)
        if sc >= min_score:
            scored.append((sc, parts, r))
    scored.sort(key=lambda x: (-x[0], x[2].get("updated_at") or ""))
    picked = scored[:top_k]
    out = [{"memory_id": r["memory_id"], "kind": r["kind"], "slot": r["slot"],
            "content": r["content"], "score": round(sc, 4), "factors": parts}
           for sc, parts, r in picked]
    if touch and out:
        for item in out:
            try:
                d.bump_memory_hit(item["memory_id"], item["score"])
            except Exception:
                pass
    return out


def build_memory_block(user_id: str, query: str, top_k: int = RECALL_TOP_K) -> str:
    """生成注入提示词的长期记忆块；无相关记忆返回空串。"""
    items = recall(user_id, query, top_k=top_k)
    if not items:
        return ""
    lines: List[str] = []
    for it in items:
        lines.append(f"- [{KIND_LABEL.get(it['kind'], '偏好')}] {it['content']}")
    return ("【用户长期记忆（来自历史会话沉淀，仅作偏好参考；"
            "与本轮用户明确说法冲突时，一律以本轮为准，不要生硬套用）】\n"
            + "\n".join(lines) + "\n\n")


def should_extract(text: str) -> bool:
    """该话轮值得跑一次 LLM 抽取吗（省 token 的启发式闸门）。"""
    if not text or len(text) < 8:
        return False
    return bool(MEMORY_CUE_RE.search(text))


def extract_and_remember(user_id: str, text: str, session_id: Optional[str] = None,
                         llm_fn: Optional[Callable] = None) -> Dict[str, int]:
    """规则抽取（总是）+ LLM 抽取（闸门放行时）→ 合并写入。"""
    cands = extract_rule_based(text)
    if llm_fn and should_extract(text):
        seen = {normalize_content(c.content) for c in cands}
        for c in extract_with_llm(text, llm_fn):
            if normalize_content(c.content) not in seen:
                cands.append(c)
                seen.add(normalize_content(c.content))
    return remember(user_id, cands, session_id)


# ==================== 管理 / 观测 ====================

def list_memories(user_id: str = "default", kind: Optional[str] = None) -> List[Dict[str, Any]]:
    d = _db()
    if not d:
        return []
    try:
        rows = d.list_memories(user_id=user_id, kind=kind)
    except Exception:
        return []
    return [{"memory_id": r["memory_id"], "kind": r["kind"], "slot": r["slot"],
             "content": r["content"], "confidence": r["confidence"],
             "hit_count": r["hit_count"], "created_at": r["created_at"],
             "updated_at": r["updated_at"], "last_used_at": r["last_used_at"]} for r in rows]


def forget(memory_id: Optional[str] = None, user_id: Optional[str] = None) -> int:
    d = _db()
    if not d:
        return 0
    try:
        return d.delete_memory(memory_id=memory_id, user_id=user_id)
    except Exception:
        return 0


def stats(user_id: str = "default") -> Dict[str, Any]:
    d = _db()
    emb = get_embedder()
    rows = list_memories(user_id)
    by_slot: Dict[str, int] = {}
    for r in rows:
        by_slot[r["slot"] or "other"] = by_slot.get(r["slot"] or "other", 0) + 1
    return {"user_id": user_id, "total": len(rows),
            "embedder": getattr(emb, "name", "lexical"),
            "by_slot": by_slot,
            "avg_confidence": round(sum(float(r["confidence"] or 0) for r in rows) / len(rows), 3)
            if rows else 0.0,
            "persisted": bool(d)}


def warmup() -> None:
    """预热嵌入器（首次加载模型可能几百毫秒，别把它算进首屏延迟）。"""
    get_embedder()


# 后台写入线程引用，避免被 GC（daemon 线程，进程退出即结束，不阻塞关机）
_bg_threads: List[threading.Thread] = []


def remember_async(user_id: str, text: str, session_id: Optional[str] = None,
                   llm_fn: Optional[Callable] = None) -> None:
    """答复已经给出后再抽取记忆：放后台线程，绝不拖慢对话响应。"""
    def _run():
        try:
            r = extract_and_remember(user_id, text, session_id, llm_fn)
            if r.get("added") or r.get("updated"):
                logger.info("长期记忆沉淀 user=%s +%d ~%d", user_id,
                            r.get("added", 0), r.get("updated", 0))
        except Exception as e:  # noqa: BLE001
            logger.warning("长期记忆写入失败（不影响对话）: %s", e)

    t = threading.Thread(target=_run, name="memory-write", daemon=True)
    _bg_threads.append(t)
    if len(_bg_threads) > 50:  # 只留最近引用，防止无限增长
        del _bg_threads[:-50]
    t.start()


def wait_bg(timeout: float = 5.0) -> bool:
    """等待后台写入线程收尾（单测/优雅退出用）。返回是否全部完成。"""
    deadline = time.time() + timeout
    for t in list(_bg_threads):
        t.join(max(0.01, deadline - time.time()))
        if time.time() > deadline:
            return False
    return True
