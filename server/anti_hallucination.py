"""
SmartTourism · 反幻觉与冲突消解模块
====================================
用于降低大模型对话幻觉、检测用户纠正/否定信号、校验关键事实。

核心能力:
  1. 反幻觉系统提示词片段 — 注入到 build_system_prompt
  2. 用户纠正信号检测 — 识别"不对/不是/你说错了"等冲突表达
  3. 事实提取与校验 — 从 LLM 回复中提取价格/时间/地址，与插件数据比对
  4. 会话事实追踪 — 记录已确认事实，避免前后矛盾
  5. 按场景动态 temperature — 咨询类低温度，创意类高温度

作者: 智旅灵犀团队
"""

from __future__ import annotations

import re
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("anti_hallucination")
logger.setLevel(logging.INFO)


# ==================== 校验降级可观测 ====================
# 事后校验是「保险丝」不是「闸机」：它自己出问题（解析/正则/读会话事实炸）只能让这一轮不校验，
# 不能把已经花钱生成、已经落库的答复变成 500。但降级必须看得见，否则保险丝烧了一年都没人知道：
#   - 每个环节「首次」失败打一条 WARNING（同类后续降为 DEBUG，不刷屏）；
#   - 计数常驻内存，由 /api/admin/ops 的 validation 段聚合透出。
_stats_lock = threading.Lock()
_validation_stats: Dict[str, Any] = {
    "runs": 0,            # 校验执行次数
    "degraded_runs": 0,   # 至少跳过一环的次数（这一轮的结论不完整）
    "issues": 0,          # 检出问题的次数（冲突/前后矛盾/无来源价格）
    "by_stage": {},       # 各环节失败累计次数
    "last_error": {},     # 各环节最近一次错误
    "rag": {"build_failed": False, "retriever": "", "retrieval_errors": 0},
}
_warned_stages: set = set()


def _record_degraded(stage: str, exc: BaseException) -> None:
    """记一次校验降级。只存字符串，不持有异常对象（traceback 会引用着帧和连接不放）。"""
    message = str(exc)[:200]
    exc_type = type(exc).__name__
    with _stats_lock:
        _validation_stats["by_stage"][stage] = _validation_stats["by_stage"].get(stage, 0) + 1
        _validation_stats["last_error"][stage] = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "exc_type": exc_type,
            "message": message,
        }
        first = stage not in _warned_stages
        _warned_stages.add(stage)
    if first:
        logger.warning("反幻觉校验环节 %s 异常，本轮跳过该环节（首次告警，同类后续只记 DEBUG）: %s: %s",
                       stage, exc_type, message)
    else:
        logger.debug("反幻觉校验环节 %s 异常: %s: %s", stage, exc_type, message)


def _note_rag(build_failed: bool = None, retriever: str = None, retrieval_error: bool = False) -> None:
    with _stats_lock:
        if build_failed is not None:
            _validation_stats["rag"]["build_failed"] = build_failed
        if retriever is not None:
            _validation_stats["rag"]["retriever"] = retriever
        if retrieval_error:
            _validation_stats["rag"]["retrieval_errors"] += 1


def validation_stats() -> dict:
    """校验与知识库检索的降级概况，供 /api/admin/ops 观察。"""
    with _stats_lock:
        return {
            "runs": _validation_stats["runs"],
            "degraded_runs": _validation_stats["degraded_runs"],
            "issues": _validation_stats["issues"],
            "by_stage": dict(_validation_stats["by_stage"]),
            "last_error": dict(_validation_stats["last_error"]),
            "rag": dict(_validation_stats["rag"]),
        }


# ==================== 反幻觉提示词片段 ====================

ANTI_HALLUCINATION_PROMPT = """
**【反幻觉约束 — 必须严格遵守】**
1. 不知道就承认：如果不确定某个事实（价格、营业时间、地址、天气、政策），必须明确说"建议以官方/平台最新信息为准"，绝不允许编造具体数值。
2. 数据优先级：当"数据插件获取的真实实时数据"与你的知识冲突时，必须以插件数据为准，不得使用过时或编造的信息覆盖插件数据。
3. 禁止编造价格：所有价格必须标注为"参考区间"并注明来源（插件数据/平台参考），不得给出精确到个位的编造价格。
4. 禁止编造地址：景点、酒店、餐厅的地址必须来自插件数据或知识库；如果没有数据支撑，说明"建议在地图上搜索确认"。
5. 时间诚实：如果用户问"现在几点/今天星期几"，必须使用系统注入的时间，不得自行推断。
6. 不要伪造体验：不得编造"我去过""我体验过"等虚假个人经历。
7. 引用来源：推荐景点/酒店/餐饮时，优先使用插件返回的真实数据；如果没有插件数据，明确告知"以下是基于常识的推荐，建议出行前确认"。
"""

CONFLICT_HANDLING_PROMPT = """
**【用户纠正与冲突处理 — 必须严格遵守】**
1. 当用户说"不对""不是""你说错了""不是这个意思"等纠正性表达时，你必须：
   a. 立即停止坚持之前的回答
   b. 重新理解用户的真实意图
   c. 承认理解偏差并给出修正后的回答
2. 当用户提供的 factual 信息与你的知识冲突时，优先信任用户（用户通常更了解自己的需求和实际情况）。
3. 不要在用户纠正后仍然重复错误信息；要明确说"好的，我理解了，应该是…"并给出修正内容。
4. 如果用户连续两次纠正同一问题，主动询问"我想确认一下，您需要的是…对吗？"避免反复误解。
5. 当用户说"算了""不用了""我自己看吧"等放弃信号时，不要继续长篇回答，简洁收尾即可。
"""


# ==================== 用户纠正信号检测 ====================

# 纠正/否定信号关键词（按强度分级）
CORRECTION_SIGNALS = {
    "strong": [
        "不对", "不是这样", "你说错了", "你搞错了", "你理解错了",
        "不是这个意思", "别瞎说", "乱说", "瞎编", "编造",
        "根本没有", "不存在", "瞎扯", "胡说",
    ],
    "medium": [
        "不是", "不对吧", "应该是", "其实", "实际上",
        "我的意思是", "我想说的是", "我说的是",
        "不是问这个", "不是这个", "再想想",
        "你确定吗", "真的吗", "是这样吗",
    ],
    "weak": [
        "但是", "不过", "可是", "然而",
        "换个", "另外", "重新",
    ],
}

# 放弃信号
GIVE_UP_SIGNALS = [
    "算了", "不用了", "没事了", "我自己看", "不用了",
    "别说了", "停", "结束",
]


@dataclass
class ConflictResult:
    """冲突检测结果"""
    is_correction: bool = False          # 是否为纠正信号
    correction_strength: str = ""       # strong / medium / weak
    is_give_up: bool = False            # 是否为放弃信号
    detected_signals: List[str] = field(default_factory=list)
    suggested_action: str = ""          # 建议的处理动作


def detect_user_conflict(user_input: str) -> ConflictResult:
    """
    检测用户输入中是否包含纠正/否定/放弃信号。

    Args:
        user_input: 用户原始输入

    Returns:
        ConflictResult: 冲突检测结果
    """
    result = ConflictResult()
    text = user_input.strip()

    # 检测放弃信号
    for signal in GIVE_UP_SIGNALS:
        if signal in text:
            result.is_give_up = True
            result.detected_signals.append(signal)
            result.suggested_action = "concise_end"
            return result

    # 检测纠正信号（从强到弱）
    for strength in ["strong", "medium", "weak"]:
        signals = CORRECTION_SIGNALS.get(strength, [])
        for signal in signals:
            if signal in text:
                result.is_correction = True
                result.correction_strength = strength
                result.detected_signals.append(signal)

        if result.is_correction:
            break

    if result.is_correction:
        if result.correction_strength == "strong":
            result.suggested_action = "apologize_and_reread"
        elif result.correction_strength == "medium":
            result.suggested_action = "acknowledge_and_clarify"
        else:
            result.suggested_action = "note_and_adjust"

    return result


def build_conflict_aware_prefix(conflict: ConflictResult) -> str:
    """
    根据冲突检测结果，构建需要注入到用户消息前缀的引导文本。

    Args:
        conflict: 冲突检测结果

    Returns:
        str: 注入到用户消息前的引导文本（可为空）
    """
    if conflict.is_give_up:
        return "【系统提示：用户表示不需要继续，请简洁收尾，不要长篇回复。】\n"

    if not conflict.is_correction:
        return ""

    if conflict.correction_strength == "strong":
        return (
            "【系统提示：用户明确指出你之前的回答有误。你必须："
            "1) 承认理解偏差并道歉；"
            "2) 重新分析用户的真实需求；"
            "3) 给出修正后的回答，不要重复之前的错误内容。】\n"
        )
    elif conflict.correction_strength == "medium":
        return (
            "【系统提示：用户对你的回答表达了纠正意图。请："
            "1) 确认你理解了用户的真实意思；"
            "2) 给出修正后的回答。】\n"
        )
    else:
        return (
            "【系统提示：用户可能想补充或调整需求，请留意并适当修正回答方向。】\n"
        )


# ==================== 事实提取与校验 ====================

@dataclass
class FactCheckResult:
    """事实校验结果"""
    extracted_prices: List[str] = field(default_factory=list)
    extracted_times: List[str] = field(default_factory=list)
    extracted_addresses: List[str] = field(default_factory=list)
    has_unsourced_prices: bool = False       # 是否存在无来源的价格
    has_hedging_language: bool = False       # 是否已使用对冲语言（"参考""约""以实际为准"）
    confidence_issues: List[str] = field(default_factory=list)


def extract_facts(text: str) -> FactCheckResult:
    """
    从 LLM 回复中提取关键事实（价格、时间、地址），用于后续校验。

    Args:
        text: LLM 回复文本

    Returns:
        FactCheckResult: 提取的事实和置信度问题
    """
    result = FactCheckResult()

    if not text:
        return result

    # 提取价格：¥xxx / xxx元 / xxx块 / xxx-xxx元
    price_patterns = [
        r'¥\s*(\d+(?:[.-～~]\d+)?)\s*(?:元|块)?',
        r'(\d+(?:\s*[-～~]\s*\d+)?)\s*(?:元|块)',
        r'人均\s*(\d+(?:\s*[-～~]\s*\d+)?)',
        r'门票\s*(\d+(?:\s*[-～~]\s*\d+)?)',
    ]
    for pattern in price_patterns:
        matches = re.findall(pattern, text)
        result.extracted_prices.extend(matches)

    # 提取时间表达：HH:MM / 上午/下午X点 / X:00-X:00
    time_patterns = [
        r'(\d{1,2}:\d{2}(?:\s*[-～~]\s*\d{1,2}:\d{2})?)',
        r'(上午|下午|早上|晚上)\s*(\d{1,2})\s*点',
        r'(\d{1,2})\s*[-～~]\s*(\d{1,2})\s*点',
    ]
    for pattern in time_patterns:
        matches = re.findall(pattern, text)
        if matches:
            result.extracted_times.extend([str(m) for m in matches])

    # 提取地址：XX省XX市 / XX市XX区 / XX路XX号
    address_patterns = [
        r'([\u4e00-\u9fa5]{2,6}(?:省|自治区))[\u4e00-\u9fa5]{2,8}(?:市|区|县)',
        r'([\u4e00-\u9fa5]{2,8}(?:市|区|县))[\u4e00-\u9fa5]{2,10}(?:路|街|大道|巷)',
        r'([\u4e00-\u9fa5]{2,15}(?:路|街|大道|巷)\d*号?)',
    ]
    for pattern in address_patterns:
        matches = re.findall(pattern, text)
        result.extracted_addresses.extend(matches)

    # 检测对冲语言
    hedging_keywords = [
        "参考", "约", "大约", "左右", "以实际为准", "以现场为准",
        "以平台为准", "建议确认", "仅供参考", "参考区间", "参考价格",
    ]
    result.has_hedging_language = any(kw in text for kw in hedging_keywords)

    # 检测无来源的精确价格
    if result.extracted_prices and not result.has_hedging_language:
        # 检查价格附近是否有"参考""约"等对冲词
        for price in result.extracted_prices:
            # 在原文中找该价格前后 20 字符的范围
            price_str = str(price)
            idx = text.find(price_str)
            if idx >= 0:
                context_start = max(0, idx - 20)
                context_end = min(len(text), idx + len(price_str) + 20)
                context = text[context_start:context_end]
                if not any(kw in context for kw in hedging_keywords):
                    result.has_unsourced_prices = True
                    result.confidence_issues.append(
                        f"价格 '{price_str}' 附近未发现对冲语言，可能被用户当作精确价格"
                    )
                    break

    return result


def validate_against_plugin_data(
    llm_response: str,
    plugin_context: str,
) -> List[str]:
    """
    将 LLM 回复中的关键事实与插件数据进行比对，返回冲突列表。

    Args:
        llm_response: LLM 回复文本
        plugin_context: 注入给 LLM 的插件数据文本

    Returns:
        List[str]: 冲突描述列表（空列表表示无冲突）
    """
    conflicts: List[str] = []

    if not llm_response or not plugin_context:
        return conflicts

    # 提取插件数据中的价格
    plugin_prices = set()
    for m in re.finditer(r'(\d+(?:\.\d+)?)\s*元', plugin_context):
        plugin_prices.add(m.group(1))

    # 提取 LLM 回复中的价格
    llm_prices = set()
    for m in re.finditer(r'(\d+(?:\.\d+)?)\s*元', llm_response):
        llm_prices.add(m.group(1))

    # 如果 LLM 回复中有精确价格，但插件数据中没有该价格，标记为潜在冲突
    if llm_prices and plugin_prices:
        unsourced = llm_prices - plugin_prices
        if unsourced and len(unsourced) > len(plugin_prices) * 0.5:
            conflicts.append(
                f"LLM 回复中出现了 {len(unsourced)} 个插件数据中不存在的价格，可能为编造"
            )

    # 检查 LLM 是否声称"无法获取实时数据"但插件数据实际存在
    denial_patterns = [
        "无法获取实时数据", "无法获取实时", "暂时无法获取",
        "目前无法确认", "建议自行查询",
    ]
    for pattern in denial_patterns:
        if pattern in llm_response and plugin_context.strip():
            conflicts.append(
                f"LLM 声称 '{pattern}'，但插件数据实际已提供相关信息"
            )
            break

    return conflicts


# ==================== 会话事实追踪 ====================

class SessionFactTracker:
    """
    会话级别事实追踪器（内存缓存 + SQLite 落库）。

    记录每个会话中已确认的关键事实（如目的地、出行时间、预算），
    用于检测 LLM 回复是否与已确认事实矛盾。
    事实写入 session_state.facts_json，服务重启后按会话恢复，不再"重启即失忆"。
    """

    def __init__(self):
        self._facts: Dict[str, Dict[str, Any]] = {}
        self._hydrated: set = set()

    # ---- 持久化：database 不可用时静默降级为纯内存 ----
    @staticmethod
    def _db():
        import os
        if os.environ.get('CTX_PERSIST', '1') == '0':
            return None
        try:
            import database
            return database
        except Exception:
            return None

    def _hydrate(self, session_id: str):
        """内存里没有这个会话时，先从 DB 读回此前确认的事实。"""
        if session_id in self._facts or session_id in self._hydrated:
            return
        self._hydrated.add(session_id)
        d = self._db()
        if not d:
            return
        try:
            row = d.get_session_state(session_id) or {}
            raw = row.get('facts_json')
        except Exception:
            return
        if not raw:
            return
        try:
            import json
            loaded = json.loads(raw)
        except Exception:
            return
        if isinstance(loaded, dict) and loaded:
            self._facts.setdefault(session_id, {}).update(loaded)

    def _persist(self, session_id: str):
        d = self._db()
        if not d:
            return
        try:
            import json
            d.save_session_state(session_id,
                                 facts_json=json.dumps(self._facts.get(session_id, {}),
                                                       ensure_ascii=False))
        except Exception as e:
            logger.warning("会话事实持久化失败（不影响本轮对话）: %s", e)

    def update_facts(self, session_id: str, user_input: str):
        """从用户输入中提取并更新已确认事实"""
        self._hydrate(session_id)
        if session_id not in self._facts:
            self._facts[session_id] = {}

        facts = self._facts[session_id]

        # 提取目的地
        cities = [
            '杭州', '三亚', '成都', '北京', '大理', '丽江', '上海', '广州',
            '深圳', '厦门', '桂林', '西安', '南京', '重庆', '青岛', '武汉',
            '长沙', '昆明', '张家界', '黄山', '九寨沟', '拉萨', '哈尔滨',
            '苏州', '乌镇', '温州', '宁波', '南宁', '海口', '珠海',
        ]
        for city in cities:
            if city in user_input:
                facts["destination"] = city
                break

        # 提取天数
        day_match = re.search(r'(\d+)\s*天', user_input)
        if day_match:
            facts["days"] = int(day_match.group(1))

        # 提取预算（两种说法都要认：「3000元预算」「预算3000元」「人均800」）
        budget_match = re.search(r'(\d+)\s*(元|块|万|k|K)?\s*(预算|人均)', user_input)
        if not budget_match:
            budget_match = re.search(r'(?:预算|人均)[^\d]{0,4}(\d+)\s*(元|块|万|k|K)?', user_input)
        if budget_match:
            amount = int(budget_match.group(1))
            unit = budget_match.group(2)
            if unit in ('万',):
                amount *= 10000
            elif unit in ('k', 'K'):
                amount *= 1000
            facts["budget"] = amount

        # 提取人群类型
        crowd_patterns = {
            '亲子': r'亲子|带娃|小孩|孩子|宝宝',
            '情侣': r'情侣|蜜月|约会|二人',
            '老年': r'老人|爸妈|父母|银发|退休',
            '团建': r'团建|公司|团队',
            '学生': r'学生|毕业|穷游',
            '商务': r'商务|出差|会议',
        }
        for crowd_type, pattern in crowd_patterns.items():
            if re.search(pattern, user_input):
                facts["crowd_type"] = crowd_type
                break

        self._persist(session_id)

    def get_facts(self, session_id: str) -> Dict[str, Any]:
        """获取会话的已确认事实（内存没有则从 DB 恢复）"""
        self._hydrate(session_id)
        return self._facts.get(session_id, {})

    def check_consistency(self, session_id: str, llm_response: str) -> List[str]:
        """
        检查 LLM 回复是否与已确认事实矛盾。

        Args:
            session_id: 会话 ID
            llm_response: LLM 回复文本

        Returns:
            List[str]: 矛盾描述列表
        """
        facts = self.get_facts(session_id)
        issues: List[str] = []

        if not facts:
            return issues

        # 检查目的地一致性
        confirmed_dest = facts.get("destination")
        if confirmed_dest:
            # 如果 LLM 回复中出现了其他城市作为主要推荐目的地
            other_cities = [c for c in [
                '杭州', '三亚', '成都', '北京', '大理', '上海', '广州',
                '厦门', '桂林', '西安', '南京', '重庆', '青岛', '武汉',
                '长沙', '昆明', '苏州', '温州', '南宁', '海口',
            ] if c != confirmed_dest and c in llm_response]
            # 如果回复中大量提及其他城市但未提及确认目的地
            if other_cities and confirmed_dest not in llm_response:
                issues.append(
                    f"用户已确认目的地为 '{confirmed_dest}'，但回复中未提及该目的地，"
                    f"却提到了 {other_cities[:3]}"
                )

        # 检查天数一致性
        confirmed_days = facts.get("days")
        if confirmed_days:
            # 查找 LLM 回复中的天数
            day_mentions = re.findall(r'第\s*(\d+)\s*天', llm_response)
            if day_mentions:
                max_day = max(int(d) for d in day_mentions)
                if max_day > confirmed_days + 1:
                    issues.append(
                        f"用户确认 {confirmed_days} 天行程，但回复中出现第 {max_day} 天"
                    )

        return issues

    def clear(self, session_id: str):
        """清除会话事实记录（内存 + DB），并允许后续重新从 DB 读取干净状态"""
        self._facts.pop(session_id, None)
        self._hydrated.discard(session_id)
        d = self._db()
        if d:
            try:
                d.save_session_state(session_id, facts_json="{}")
            except Exception:
                pass


# 全局单例
_fact_tracker = SessionFactTracker()


def get_fact_tracker() -> SessionFactTracker:
    """获取全局事实追踪器实例"""
    return _fact_tracker


# ==================== 动态 Temperature ====================

def get_temperature_for_skill(skill_name: str = "") -> float:
    """
    根据当前 Skill / 意图类型返回推荐 temperature。

    原则:
      - 事实咨询/行程规划: 低温度（0.3-0.5），减少幻觉
      - 营销文案/文创设计: 高温度（0.7-0.9），保留创意
      - 通用对话: 中等温度（0.5-0.6）

    Args:
        skill_name: Skill 名称

    Returns:
        float: 推荐 temperature
    """
    temp_map = {
        "tourism-advisor": 0.4,      # 咨询与规划 — 事实性强，低温度
        "smart-matching": 0.3,       # 客群匹配 — 结构化输出，更低温度
        "tourism-marketing": 0.75,   # 营销与文创 — 创意性强，高温度
        "": 0.5,                     # 通用对话
    }
    return temp_map.get(skill_name, 0.5)


# ==================== 综合校验 ====================

@dataclass
class ValidationReport:
    """综合校验报告"""
    fact_check: Optional[FactCheckResult] = None
    plugin_conflicts: List[str] = field(default_factory=list)
    consistency_issues: List[str] = field(default_factory=list)
    overall_status: str = "pass"  # pass / warning / error
    degraded: List[str] = field(default_factory=list)  # 本轮被跳过的环节（结论不完整）

    @property
    def has_issues(self) -> bool:
        return bool(self.plugin_conflicts or self.consistency_issues or
                    (self.fact_check and self.fact_check.has_unsourced_prices))

    def to_log_dict(self) -> dict:
        """转为日志友好的字典"""
        return {
            "status": self.overall_status,
            "unsourced_prices": self.fact_check.has_unsourced_prices if self.fact_check else False,
            "plugin_conflicts": len(self.plugin_conflicts),
            "consistency_issues": len(self.consistency_issues),
            "degraded": list(self.degraded),
            "details": {
                "plugin_conflicts": self.plugin_conflicts,
                "consistency_issues": self.consistency_issues,
                "confidence_issues": self.fact_check.confidence_issues if self.fact_check else [],
            },
        }


def validate_response(
    llm_response: str,
    plugin_context: str = "",
    session_id: str = "",
) -> ValidationReport:
    """
    对 LLM 回复进行综合校验。

    Args:
        llm_response: LLM 回复文本
        plugin_context: 注入的插件数据
        session_id: 会话 ID

    Returns:
        ValidationReport: 校验报告

    本函数**不向上抛异常**：三个子环节各自兜住，失败的环节记进 report.degraded
    与 validation_stats()。答复此时已经生成并落库，校验自己坏了不该把请求打成 500。
    """
    report = ValidationReport()
    degraded: List[str] = []

    def _stage(name: str, fn, default):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            _record_degraded(name, exc)
            degraded.append(name)
            return default

    # 事实提取
    report.fact_check = _stage("fact_check", lambda: extract_facts(llm_response), None)

    # 插件数据比对
    report.plugin_conflicts = _stage(
        "plugin_conflict", lambda: validate_against_plugin_data(llm_response, plugin_context), [])

    # 会话事实一致性
    if session_id:
        report.consistency_issues = _stage(
            "consistency", lambda: get_fact_tracker().check_consistency(session_id, llm_response), [])

    report.degraded = degraded

    # 综合状态
    if report.plugin_conflicts or report.consistency_issues:
        report.overall_status = "warning"
    elif report.fact_check and report.fact_check.has_unsourced_prices:
        report.overall_status = "warning"
    else:
        report.overall_status = "pass"

    with _stats_lock:
        _validation_stats["runs"] += 1
        if degraded:
            _validation_stats["degraded_runs"] += 1
        if report.has_issues:
            _validation_stats["issues"] += 1

    if report.has_issues:
        logger.warning(
            "response_validation session=%s report=%s",
            session_id,
            report.to_log_dict(),
        )

    return report


# ==================== RAG 事实增强 ====================

# 离线混合检索器单例（bge-small-zh 已本地缓存；缺失时自动回退 Mock 嵌入）
_HYBRID_RETRIEVER = None
_HYBRID_BUILD_FAILED = False


def _get_hybrid_retriever():
    """懒加载离线混合检索器。构建失败则永久降级为 None，绝不联网阻塞对话。"""
    global _HYBRID_RETRIEVER, _HYBRID_BUILD_FAILED
    if _HYBRID_RETRIEVER is not None or _HYBRID_BUILD_FAILED:
        return _HYBRID_RETRIEVER
    try:
        import os
        # 强制离线：只用本地缓存的模型，缺失就快速失败回退 Mock，杜绝下载挂起
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        from agent.rag_hybrid import build_default_retriever
        _HYBRID_RETRIEVER = build_default_retriever(prefer_neural=True, use_reranker=False)
        _note_rag(build_failed=False,
                  retriever=str(getattr(_HYBRID_RETRIEVER.embedder, "name", "?")))
        logger.info(
            "RAG 混合检索器就绪: %d 篇文档, embedder=%s",
            len(_HYBRID_RETRIEVER.documents),
            getattr(_HYBRID_RETRIEVER.embedder, "name", "?"),
        )
    except Exception as e:  # noqa: BLE001
        _HYBRID_BUILD_FAILED = True
        _HYBRID_RETRIEVER = None
        _note_rag(build_failed=True)
        _record_degraded("rag_build", e)
        logger.warning("RAG 混合检索器构建失败，跳过知识库检索: %s", e)
    return _HYBRID_RETRIEVER


def retrieve_knowledge(user_input: str, top_k: int = 3) -> Tuple[str, List[dict]]:
    """
    检索本地知识库，同时返回「注入提示词的上下文」和「可展示给用户的出处清单」。

    出处清单只描述**这次真的检索到了什么**：检索器不可用、无结果、或检索抛异常时
    返回空列表——绝不拿「应该有来源」当来源，那样等于给用户一个假的安心感。

    Returns:
        (context_str, sources)。sources 每项：
        {"index", "name", "city", "type", "source", "score", "snippet"}
    """
    if not user_input or not user_input.strip():
        return "", []
    retriever = _get_hybrid_retriever()
    if retriever is None:
        return "", []
    try:
        docs = retriever.retrieve(user_input, top_k=top_k)
        if not docs:
            return "", []
        parts = ["以下是本地知识库中相关的文旅资料：\n"]
        sources = []
        for i, d in enumerate(docs, 1):
            meta = d.metadata or {}
            source = meta.get("source", "本地知识库")
            parts.append(f"### 参考资料 {i} (相关度: {d.score:.1%}, 来源: {source})")
            parts.append(d.text[:600])
            parts.append("")
            sources.append({
                "index": i,
                "name": meta.get("name") or source,
                "city": meta.get("city", "") or "",
                "type": meta.get("type", "") or "",
                "source": source,
                "score": round(float(d.score), 4),
                "snippet": re.sub(r"\s+", " ", d.text[:120]).strip(),
            })
        body = "\n".join(parts)
        return (f"\n\n---\n**以下是从本地知识库检索到的参考资料，请优先参考：**\n\n{body}",
                sources)
    except Exception as e:  # noqa: BLE001
        # 原来是 logger.debug——INFO 级别下完全看不见，知识库悄悄失效没人知道
        _record_degraded("rag_retrieve", e)
        _note_rag(retrieval_error=True)
        return "", []


def get_rag_context(user_input: str, top_k: int = 3) -> str:
    """
    从离线混合检索（bge-small-zh 向量 + BM25 + RRF 融合 + 词面重排）检索本地知识库，
    作为事实依据注入对话。检索器不可用或无结果时返回空字符串（优雅降级）。

    注: 旧版 rag_engine 基于 chromadb 默认 ONNX 模型，查询时会联网下载模型；
    墙内下载挂起会阻塞 /api/chat，故改用纯离线的 agent.rag_hybrid。
    需要同时拿到出处清单时调用 retrieve_knowledge()。

    Args:
        user_input: 用户输入
        top_k: 检索数量

    Returns:
        str: 格式化的知识库上下文（可为空）
    """
    return retrieve_knowledge(user_input, top_k=top_k)[0]
