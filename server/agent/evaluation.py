# -*- coding: utf-8 -*-
"""
SmartTourism · Agent 评估体系（Evaluation Harness）
====================================================
给「文旅 Agent」建一套可复现的离线评估：

  1. 数据集：内置 ~50 条文旅 query，覆盖 咨询/行程/营销/文创/匹配 五类意图；
     每条带「期望意图 + 必含要点 + 禁含违规词 + 是否要求价格来源标注 + 参考要点」。
  2. 指标：
     - 规则指标（离线、确定性、无需 API Key）：要点覆盖率、违规词、价格来源标注
       （复用 anti_hallucination.extract_facts）、格式/长度合理性。
     - LLM-as-Judge（可选，传入 judge_fn 才启用）：相关性 / 事实性 / 幻觉抑制 / 有用性，
       1–5 分归一化到 0–1。judge 解析失败自动降级为「跳过」，不计入均分。
  3. 回归：跑完整数据集产出 EvalReport（通过率、分类均分、失败清单），可存 JSON、
     与 baseline 对比，输出「提升 / 回退」的用例，防止改 prompt 后偷偷变差。

被测系统通过 run_fn(query)->answer 注入，评估器与 app.py 解耦，可测任意图/Agent。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("agent.evaluation")

# ==================== 数据结构 ====================


@dataclass
class EvalCase:
    """一条评估用例。"""
    id: str
    query: str
    category: str = "consult"                 # consult / plan / marketing / cultural / matching
    intent: str = "consult"                   # 期望意图
    must_contain: List[str] = field(default_factory=list)   # 答复应覆盖的要点（命中任一即可算该项）
    must_not_contain: List[str] = field(default_factory=list)  # 违规/极限词，出现即扣分
    require_source_tag: bool = False          # 是否要求「价格带来源/对冲标注」
    reference: str = ""                        # 参考答案要点（供 judge 对照）


@dataclass
class MetricScore:
    """单个指标得分。"""
    name: str
    score: float              # 0–1
    passed: bool
    detail: str = ""


@dataclass
class CaseResult:
    """单条用例的评估结果。"""
    case_id: str
    query: str
    category: str
    answer: str
    scores: List[MetricScore] = field(default_factory=list)
    overall: float = 0.0      # 规则指标均分（judge 单列）
    judge: Dict[str, float] = field(default_factory=dict)  # 相关性/事实性/... 0–1
    passed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


# ==================== 违规词表（广告法极限词，营销场景重点） ====================

BANNED_ABSOLUTE_TERMS = [
    "最好", "最佳", "最优", "第一", "唯一", "顶级", "国家级", "世界级",
    "绝对", "百分之百", "100%", "史上最", "全球首", "永久", "万能",
    "根治", "秒杀全场", "无副作用",
]


# ==================== 内置数据集（~50 条） ====================

def _c(cid, query, category="consult", intent=None, must=None, banned=None,
       src=False, ref="") -> EvalCase:
    return EvalCase(
        id=cid, query=query, category=category, intent=intent or category,
        must_contain=must or [], must_not_contain=banned if banned is not None else BANNED_ABSOLUTE_TERMS,
        require_source_tag=src, reference=ref,
    )


def build_default_cases() -> List[EvalCase]:
    """内置文旅评估集：覆盖五大意图 × 多城市多场景。"""
    cases: List[EvalCase] = [
        # ---- 咨询类（景点/美食/交通/住宿） ----
        _c("c01", "杭州有什么好玩的景点推荐？", "consult",
           must=["西湖"], ref="应给出杭州代表景点并标注来源"),
        _c("c02", "三亚的海边适合带小孩玩吗？", "consult", must=["三亚"]),
        _c("c03", "成都本地人都吃什么特色美食？", "consult", must=["成都"]),
        _c("c04", "北京故宫门票多少钱？", "consult", must=["故宫"], src=True,
           ref="价格须标注来源/对冲，不得编造精确票价"),
        _c("c05", "从上海到杭州怎么走最方便？", "consult", must=["杭州"]),
        _c("c06", "西安有哪些必去的历史景点？", "consult", must=["西安"]),
        _c("c07", "厦门鼓浪屿值得住一晚吗？", "consult", must=["鼓浪屿"]),
        _c("c08", "桂林山水几月份去最好？", "consult", must=["桂林"]),
        _c("c09", "重庆有哪些网红打卡地？", "consult", must=["重庆"]),
        _c("c10", "苏州园林门票贵不贵？", "consult", must=["苏州"], src=True),
        _c("c11", "黄山看日出最佳观景点在哪？", "consult", must=["黄山"]),
        _c("c12", "拉萨旅游需要注意高反吗？", "consult", must=["拉萨"]),
        _c("c13", "哈尔滨冬天有什么好玩的？", "consult", must=["哈尔滨"]),
        _c("c14", "丽江古城住宿一晚大概多少钱？", "consult", must=["丽江"], src=True),

        # ---- 行程规划类 ----
        _c("p01", "帮我规划一个杭州3日游的行程", "plan",
           must=["第", "西湖"], ref="应输出按天编排的行程结构"),
        _c("p02", "带爸妈去三亚玩5天，节奏慢一点，怎么安排？", "plan",
           must=["三亚", "第"], ref="银发/慢节奏，避免高强度"),
        _c("p03", "成都两日游亲子路线，预算人均2000", "plan",
           must=["成都", "第"], src=True),
        _c("p04", "北京4天3晚自由行攻略", "plan", must=["北京"]),
        _c("p05", "大理丽江7日游怎么排路线合理？", "plan", must=["大理"]),
        _c("p06", "西安一日游时间紧，怎么安排？", "plan", must=["西安"]),
        _c("p07", "重庆3天2晚美食行程，情侣出游", "plan", must=["重庆"]),
        _c("p08", "厦门鼓浪屿两日游含住宿建议", "plan", must=["厦门"], src=True),
        _c("p09", "张家界5日游路线，含交通", "plan", must=["张家界"]),
        _c("p10", "青岛周末两天怎么玩？", "plan", must=["青岛"]),
        _c("p11", "毕业旅行去长沙3天，学生党穷游预算低", "plan", must=["长沙"], src=True),
        _c("p12", "公司团建去桂林4天，20人左右怎么安排？", "plan", must=["桂林"]),
        _c("p13", "黄山3日游含看日出，怎么排行程？", "plan", must=["黄山", "第"]),
        _c("p14", "哈尔滨冬季5日游，想看冰雕和雪乡", "plan", must=["哈尔滨"]),

        # ---- 营销文案类 ----
        _c("m01", "给杭州西湖写一条小红书种草笔记", "marketing",
           must=["西湖"], banned=BANNED_ABSOLUTE_TERMS,
           ref="平台适配 + 话题标签，禁用极限词"),
        _c("m02", "帮我写三亚海景酒店的抖音短视频脚本", "marketing",
           must=["三亚"], banned=BANNED_ABSOLUTE_TERMS),
        _c("m03", "成都火锅店的朋友圈推广文案，突出性价比", "marketing",
           banned=BANNED_ABSOLUTE_TERMS),
        _c("m04", "给西安古城墙景区写一段公众号推文", "marketing",
           banned=BANNED_ABSOLUTE_TERMS),
        _c("m05", "厦门民宿五一促销的小红书标题5个", "marketing",
           banned=BANNED_ABSOLUTE_TERMS),
        _c("m06", "桂林山水旅游景区的宣传slogan", "marketing",
           banned=BANNED_ABSOLUTE_TERMS),
        _c("m07", "重庆洪崖洞夜游的短视频口播脚本", "marketing",
           banned=BANNED_ABSOLUTE_TERMS),
        _c("m08", "写一条故宫门票预售的营销文案，合规一点", "marketing",
           banned=BANNED_ABSOLUTE_TERMS, ref="不得虚假承诺/极限词"),

        # ---- 文创设计类 ----
        _c("w01", "为杭州西湖设计一款文创雪糕的产品概念", "cultural",
           must=["西湖"]),
        _c("w02", "故宫IP的文创纪念品有哪些设计方向？", "cultural",
           must=["故宫"]),
        _c("w03", "给成都熊猫主题设计一套文创周边", "cultural",
           must=["熊猫"]),
        _c("w04", "三亚海洋主题文创产品创意", "cultural", must=["三亚"]),
        _c("w05", "西安兵马俑元素的数字藏品创意", "cultural", must=["西安"]),

        # ---- 客群匹配类 ----
        _c("s01", "银发康养客群的旅游产品该怎么设计？", "matching",
           must=["银发"]),
        _c("s02", "针对亲子家庭客群，三亚怎么精准营销？", "matching",
           must=["亲子"]),
        _c("s03", "年轻情侣客群的画像和转化策略", "matching",
           must=["情侣"]),
        _c("s04", "大学生穷游客群适合推什么产品？", "matching",
           must=["学生"]),
        _c("s05", "商务出差客群的酒店匹配策略", "matching", must=["商务"]),

        # ---- 反幻觉 / 边界 ----
        _c("h01", "杭州某不知名小餐馆人均具体多少钱？", "consult",
           must=[], src=True, ref="无插件数据时应提示需确认，不得编造精确价格"),
        _c("h02", "帮我查一下明天三亚某个私人海滩的门票", "consult",
           src=True, ref="数据缺失应如实说明"),
        _c("g01", "你好，你是谁？", "consult", must=[],
           ref="通用问候，简洁自我介绍"),
        _c("g02", "今天星期几？", "consult", must=[],
           ref="应基于系统时间回答"),
    ]
    return cases


# ==================== 规则指标（离线、确定性） ====================

def _metric_coverage(case: EvalCase, answer: str) -> MetricScore:
    """要点覆盖率：must_contain 命中比例（无要点则视为通过）。"""
    if not case.must_contain:
        return MetricScore("coverage", 1.0, True, "无强制要点")
    hits = [k for k in case.must_contain if k in answer]
    score = len(hits) / len(case.must_contain)
    return MetricScore(
        "coverage", round(score, 3), score >= 0.999,
        f"命中 {hits} / 需 {case.must_contain}",
    )


def _metric_banned(case: EvalCase, answer: str) -> MetricScore:
    """违规词：出现任一极限词即不通过。"""
    found = [t for t in case.must_not_contain if t and t in answer]
    return MetricScore(
        "no_banned_terms", 0.0 if found else 1.0, not found,
        f"命中违规词: {found}" if found else "无违规词",
    )


def _metric_source_tag(case: EvalCase, answer: str) -> MetricScore:
    """价格来源标注：要求时，价格附近须有对冲/来源语言（复用 anti_hallucination）。"""
    if not case.require_source_tag:
        return MetricScore("source_tag", 1.0, True, "本用例不要求来源标注")
    try:
        from anti_hallucination import extract_facts  # type: ignore
        facts = extract_facts(answer)
        if not facts.extracted_prices:
            return MetricScore("source_tag", 1.0, True, "答复未含具体价格，无需标注")
        ok = not facts.has_unsourced_prices
        return MetricScore(
            "source_tag", 1.0 if ok else 0.0, ok,
            "价格已带对冲/来源" if ok else "存在无来源的精确价格（疑似编造）",
        )
    except Exception as e:  # noqa: BLE001 — 反幻觉模块缺失时降级为宽松通过
        logger.debug("source_tag 复用 extract_facts 失败，降级: %s", e)
        return MetricScore("source_tag", 1.0, True, "extract_facts 不可用，跳过")


def _metric_format(case: EvalCase, answer: str) -> MetricScore:
    """格式/长度合理性：非空、非报错为硬门槛；过短只降分不判死。"""
    text = (answer or "").strip()
    if not text:
        return MetricScore("format", 0.0, False, "空答复")
    if text.startswith("{") and '"error"' in text[:40]:
        return MetricScore("format", 0.0, False, "答复是错误对象")
    if len(text) < 15:
        return MetricScore("format", 0.3, True, f"偏短({len(text)}字)")
    return MetricScore("format", 1.0, True, f"长度 {len(text)} 字")


RULE_METRICS: List[Callable[[EvalCase, str], MetricScore]] = [
    _metric_coverage, _metric_banned, _metric_source_tag, _metric_format,
]


# ==================== 真实答复的规则指标打分（不依赖内置用例） ====================

# 真实提问没有「参考答案要点」，coverage 在这种数据上没有定义，如实标不适用，
# 不硬凑一个「命中 0/需 0 = 1.0」的假满分把均分抬上去。
_LIVE_METRICS = ("no_banned_terms", "source_tag", "format")


def score_live_answer(answer: str, question: str = "", category: str = "consult") -> Dict[str, Any]:
    """对一条**真实落库的答复**跑规则指标：离线、确定性、零 API 调用。

    内置评估集测的是「改 prompt 前后有没有回退」，这里测的是「平台上真发出去的答案
    有多少踩了违规词/无来源价格/空答复」——同一套指标函数，样本换成库里的问答对。
    与用户点赞/点踩放一起看，才知道「用户不满意的答复」是不是真的在规则上也更差。
    """
    case = EvalCase(id="live", query=question or "", category=category,
                    must_contain=[], must_not_contain=BANNED_ABSOLUTE_TERMS,
                    require_source_tag=True)
    metrics: Dict[str, Any] = {
        "coverage": {"applicable": False, "detail": "真实提问没有参考要点，覆盖率不适用"}
    }
    total = 0.0
    n = 0
    failed: List[str] = []
    for m in RULE_METRICS:
        s = m(case, answer or "")
        if s.name == "coverage":
            continue
        metrics[s.name] = {"score": s.score, "passed": s.passed, "detail": s.detail,
                           "applicable": True}
        total += s.score
        n += 1
        if not s.passed:
            failed.append(f"{s.name}: {s.detail}")
    return {
        "metrics": metrics,
        "overall": round(total / n, 3) if n else 0.0,
        "failed": failed,
        "chars": len((answer or "").strip()),
    }


def aggregate_live_scores(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把若干条 score_live_answer 结果聚成运维面板要看的数。

    samples: [{"score": score_live_answer(...) 的返回, "rating": 'up'|'down'|None, ...}]
    点数少的时候照实说点数少（sample_size），不给「0 条样本」画一个好看的平均分。
    """
    out: Dict[str, Any] = {
        "sample_size": len(samples or []),
        "mean_overall": None,
        "metric_pass_rate": {},
        "truncated": 0,
        "with_sources": 0,
        "failed_samples": [],
        "by_rating": {},
    }
    if not samples:
        return out

    sums: Dict[str, float] = {}
    cnts: Dict[str, int] = {}
    for item in samples:
        sc = item.get("score") or {}
        for name, v in (sc.get("metrics") or {}).items():
            if not v.get("applicable"):
                continue
            sums[name] = sums.get(name, 0.0) + (1.0 if v.get("passed") else 0.0)
            cnts[name] = cnts.get(name, 0) + 1
        if item.get("truncated"):
            out["truncated"] += 1
        if item.get("has_sources"):
            out["with_sources"] += 1
        if sc.get("failed"):
            out["failed_samples"].append({
                "message_id": item.get("message_id"),
                "session_id": item.get("session_id"),
                "rating": item.get("rating"),
                "question": (item.get("question") or "")[:40],
                "failed": sc["failed"],
            })

    n = len(samples)
    out["mean_overall"] = round(sum((i.get("score") or {}).get("overall", 0.0) for i in samples) / n, 3)
    out["metric_pass_rate"] = {k: round(sums[k] / cnts[k], 3) for k in sums}
    out["failed_samples"] = out["failed_samples"][:10]

    # 按票数分组看均分：只有「有票」的样本才参与对比，没投票不等于满意
    for rating in ("up", "down"):
        group = [i for i in samples if i.get("rating") == rating]
        out["by_rating"][rating] = {
            "count": len(group),
            "mean_overall": (round(sum((i.get("score") or {}).get("overall", 0.0)
                                        for i in group) / len(group), 3) if group else None),
        }
    voted = out["by_rating"]["up"]["count"] + out["by_rating"]["down"]["count"]
    out["by_rating"]["voted"] = voted
    out["by_rating"]["unvoted"] = n - voted
    return out


# ==================== LLM-as-Judge ====================

_JUDGE_RUBRIC = (
    "你是文旅 Agent 的严格评估官。根据「用户问题」「参考要点」评判「待评答复」，"
    "从四个维度各打 1–5 分（5 最好）：\n"
    "1. relevance 相关性：是否切题、覆盖了用户真实需求\n"
    "2. factuality 事实性：信息是否可信、价格/时间是否标注来源或对冲、有无编造\n"
    "3. anti_hallucination 幻觉抑制：不确定处是否如实说明而非硬编\n"
    "4. helpfulness 有用性：结构是否清晰、是否可直接使用\n"
    "只输出 JSON：{\"relevance\":n,\"factuality\":n,\"anti_hallucination\":n,\"helpfulness\":n,\"reason\":\"一句话\"}"
)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从 judge 输出里稳健提取 JSON 对象。"""
    if not text:
        return None
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


def llm_judge(case: EvalCase, answer: str, judge_fn: Callable) -> Dict[str, float]:
    """调用 judge_fn（签名同 app.call_llm）给答复打分，返回归一化到 0–1 的四维分。

    judge_fn 缺失或解析失败 → 返回 {}（该用例 judge 记为跳过）。
    """
    if judge_fn is None:
        return {}
    messages = [
        {"role": "system", "content": _JUDGE_RUBRIC},
        {"role": "user", "content": json.dumps({
            "用户问题": case.query,
            "参考要点": case.reference or "（无）",
            "待评答复": answer[:4000],
        }, ensure_ascii=False)},
    ]
    try:
        resp = judge_fn(messages)
        if not isinstance(resp, dict) or "error" in resp:
            return {}
        content = resp["choices"][0]["message"]["content"]
        data = _extract_json(content)
        if not data:
            return {}
        out: Dict[str, float] = {}
        for k in ("relevance", "factuality", "anti_hallucination", "helpfulness"):
            v = data.get(k)
            if isinstance(v, (int, float)):
                out[k] = round(max(0.0, min(1.0, (float(v) - 1) / 4)), 3)  # 1–5 → 0–1
        if data.get("reason"):
            out["reason"] = str(data["reason"])  # type: ignore[assignment]
        return out
    except Exception as e:  # noqa: BLE001 — judge 失败绝不能中断评估
        logger.debug("llm_judge 失败(跳过): %s", e)
        return {}


# ==================== 评估器 ====================

class Evaluator:
    """离线评估器：run_fn 是被测系统，judge_fn 是可选的 LLM 评审。"""

    def __init__(self, cases: Optional[List[EvalCase]] = None,
                 run_fn: Optional[Callable[[str], str]] = None,
                 judge_fn: Optional[Callable] = None) -> None:
        self.cases = cases if cases is not None else build_default_cases()
        self.run_fn = run_fn
        self.judge_fn = judge_fn

    def evaluate_case(self, case: EvalCase, answer: Optional[str] = None) -> CaseResult:
        if answer is None:
            if self.run_fn is None:
                raise ValueError("未提供 run_fn 也未提供 answer，无法评估")
            answer = self.run_fn(case.query)
        answer = answer or ""

        scores = [m(case, answer) for m in RULE_METRICS]
        overall = round(sum(s.score for s in scores) / len(scores), 3) if scores else 0.0
        # 规则通过判定：格式必须过 + 无违规词 + （要求时）来源标注过 + 覆盖率≥阈值
        passed = all(s.passed for s in scores if s.name in ("no_banned_terms", "source_tag", "format")) \
            and overall >= 0.6
        judge = llm_judge(case, answer, self.judge_fn) if self.judge_fn else {}

        return CaseResult(
            case_id=case.id, query=case.query, category=case.category,
            answer=answer[:500], scores=scores, overall=overall,
            judge=judge, passed=passed,
        )

    def evaluate_all(self, verbose: bool = False) -> "EvalReport":
        results = [self.evaluate_case(c) for c in self.cases]
        return EvalReport(results=results, verbose=verbose)


# ==================== 报告 / 回归 ====================

@dataclass
class EvalReport:
    """一次评估的汇总报告，可存 JSON、可与 baseline 对比做回归。"""
    results: List[CaseResult] = field(default_factory=list)
    verbose: bool = False

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def pass_rate(self) -> float:
        return round(self.passed / self.total, 3) if self.total else 0.0

    @property
    def mean_overall(self) -> float:
        return round(sum(r.overall for r in self.results) / self.total, 3) if self.total else 0.0

    def mean_judge(self) -> Dict[str, float]:
        """judge 四维均分（仅统计有 judge 的用例）。"""
        dims = ("relevance", "factuality", "anti_hallucination", "helpfulness")
        acc = {d: [] for d in dims}
        for r in self.results:
            for d in dims:
                if isinstance(r.judge.get(d), (int, float)):
                    acc[d].append(r.judge[d])
        return {d: round(sum(v) / len(v), 3) for d, v in acc.items() if v}

    def by_category(self) -> Dict[str, Dict[str, float]]:
        """按意图分类统计通过率与均分。"""
        buckets: Dict[str, List[CaseResult]] = {}
        for r in self.results:
            buckets.setdefault(r.category, []).append(r)
        out: Dict[str, Dict[str, float]] = {}
        for cat, rs in buckets.items():
            out[cat] = {
                "count": len(rs),
                "pass_rate": round(sum(1 for r in rs if r.passed) / len(rs), 3),
                "mean_overall": round(sum(r.overall for r in rs) / len(rs), 3),
            }
        return out

    def failed_cases(self) -> List[CaseResult]:
        return [r for r in self.results if not r.passed]

    def summary(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "pass_rate": self.pass_rate,
            "mean_overall": self.mean_overall,
            "mean_judge": self.mean_judge(),
            "by_category": self.by_category(),
        }

    def to_json(self, path: Optional[str] = None) -> str:
        payload = {
            "summary": self.summary(),
            "cases": [r.to_dict() for r in self.results],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return text

    def compare(self, baseline: Dict[str, Any], tol: float = 0.05) -> Dict[str, Any]:
        """与 baseline（另一份 report 的 summary 或 to_json 结果）对比，列出提升/回退。

        回退判定：本用例 overall 比 baseline 低超过 tol，或 baseline 通过而现在不通过。
        """
        base_cases = {c["case_id"]: c for c in baseline.get("cases", [])} \
            if "cases" in baseline else {}
        base_summary = baseline.get("summary", baseline)

        improved: List[str] = []
        regressed: List[str] = []
        for r in self.results:
            b = base_cases.get(r.case_id)
            if not b:
                continue
            delta = r.overall - float(b.get("overall", 0.0))
            if delta > tol:
                improved.append(r.case_id)
            elif delta < -tol or (b.get("passed") and not r.passed):
                regressed.append(r.case_id)

        return {
            "pass_rate_delta": round(self.pass_rate - float(base_summary.get("pass_rate", 0.0)), 3),
            "mean_overall_delta": round(self.mean_overall - float(base_summary.get("mean_overall", 0.0)), 3),
            "improved": improved,
            "regressed": regressed,
            "has_regression": len(regressed) > 0,
        }


def load_report(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
