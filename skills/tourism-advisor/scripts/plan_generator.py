"""
文旅方案生成器 - 规划预处理脚本

用途：
  1. 被 server/app.py 导入，为 /api/plan 提供结构化行程骨架
  2. 根据用户参数生成每日主题、消费点框架、预算区间、交通策略
  3. 将骨架注入 LLM Prompt，让模型在规则约束下润色，避免自由发挥导致的信息失实

设计原则：
  - 只输出框架和约束，不编造具体价格/开放时间
  - 所有价格均为参考区间，并标注来源为"行业参考/AI推断"
  - 遇到参数缺失时返回澄清建议，供上层接口发起追问
"""

import json
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any


# 常见目的地基础信息（用于骨架生成，不含实时价格）
DESTINATION_BASE = {
    "杭州": {"tags": ["西湖", "茶文化", "江南古镇", "美食"], "default_pace": "适中"},
    "三亚": {"tags": ["海滨", "度假", "亲子", "水上运动"], "default_pace": "慢节奏"},
    "成都": {"tags": ["美食", "熊猫", "古蜀文化", "慢生活"], "default_pace": "适中"},
    "北京": {"tags": ["皇家文化", "历史古迹", "胡同", "博物馆"], "default_pace": "紧凑"},
    "大理": {"tags": ["苍山洱海", "白族文化", "旅拍", "慢生活"], "default_pace": "慢节奏"},
    "丽江": {"tags": ["古城", "纳西文化", "雪山", "民谣"], "default_pace": "慢节奏"},
    "西安": {"tags": ["秦汉唐文化", "兵马俑", "面食", "博物馆"], "default_pace": "紧凑"},
    "桂林": {"tags": ["山水", "漓江", "喀斯特", "摄影"], "default_pace": "适中"},
    "厦门": {"tags": ["海岛", "鼓浪屿", "闽南文化", "文艺"], "default_pace": "适中"},
}

# 人群策略库
CROWD_STRATEGIES = {
    "亲子家庭": {
        "景点侧重": "主题乐园、动物园、科技馆、自然景区、互动体验",
        "住宿侧重": "亲子房、加床服务、儿童乐园设施、交通便利",
        "餐饮侧重": "清淡口味、儿童餐、包间优先、食品安全",
        "节奏": "轻松慢行，每天 2-3 个景点，午休充足",
        "消费点": ["亲子套票", "儿童体验项目", "主题餐厅", "文创玩具"],
    },
    "情侣": {
        "景点侧重": "浪漫日落点、网红打卡地、温泉、古镇、艺术展",
        "住宿侧重": "大床房、景观房、浴缸、隐私好、氛围感",
        "餐饮侧重": "氛围餐厅、露台用餐、特色私房菜、下午茶",
        "节奏": "悠闲为主，注重体验感，每天 2-3 个景点",
        "消费点": ["情侣套餐", "夜游演艺", "摄影服务", "文创礼品"],
    },
    "银发康养": {
        "景点侧重": "平缓景区、文化古迹、康养基地、园林",
        "住宿侧重": "电梯、低楼层、安静、医疗配套、无障碍",
        "餐饮侧重": "清淡、软食、养生餐、正餐准时",
        "节奏": "慢节奏，每天 1-2 个景点，步行为主",
        "消费点": ["康养体验", "文化讲解", "养生餐饮", "适老化文创"],
    },
    "团建": {
        "景点侧重": "户外拓展、竞技项目、自然景区、会议室",
        "住宿侧重": "会议室、团队房、烧烤设施、大巴可达",
        "餐饮侧重": "圆桌、大包厢、烧烤自助、团餐",
        "节奏": "紧凑高效，活动为主",
        "消费点": ["团队套餐", "拓展活动", "会议室租赁", "团建周边"],
    },
    "学生穷游": {
        "景点侧重": "免费景点、博物馆、城市地标、自然风景",
        "住宿侧重": "青旅、民宿床位、交通便利、安全",
        "餐饮侧重": "本地小吃、性价比优先、分量大",
        "节奏": "紧凑高效，公共交通，每天 3-4 个景点",
        "消费点": ["学生票", "青年旅舍", "小吃街", "平价文创"],
    },
    "商务": {
        "景点侧重": "城市地标、文化展厅、高端商圈、短途景点",
        "住宿侧重": "商务酒店、会议设施、交通便利、早餐",
        "餐饮侧重": "商务宴请、效率简餐、品质稳定",
        "节奏": "高效，利用碎片时间",
        "消费点": ["商务套餐", "会议场地", "高端餐饮", "企业定制礼品"],
    },
    "朋友": {
        "景点侧重": "网红打卡、户外徒步、酒吧街、桌游密室",
        "住宿侧重": "民宿整套、多人房、公共区域大",
        "餐饮侧重": "特色餐厅、夜宵、火锅烧烤",
        "节奏": "弹性大，兼顾集体与个人兴趣",
        "消费点": ["多人套餐", "夜生活", "体验项目", "合照摄影"],
    },
}

# 预算档位映射（人均/天参考区间，不含往返大交通）
BUDGET_LEVELS = {
    "经济": {"hotel_per_night": (150, 300), "meal_per_person": (40, 80), "ticket_per_day": (50, 120), "label": "省钱优先"},
    "中等": {"hotel_per_night": (300, 600), "meal_per_person": (80, 160), "ticket_per_day": (120, 250), "label": "体验与预算平衡"},
    "舒适": {"hotel_per_night": (300, 600), "meal_per_person": (80, 160), "ticket_per_day": (120, 250), "label": "体验与预算平衡"},
    "豪华": {"hotel_per_night": (700, 1500), "meal_per_person": (180, 350), "ticket_per_day": (250, 500), "label": "品质升级"},
    "高客单": {"hotel_per_night": (700, 1500), "meal_per_person": (180, 350), "ticket_per_day": (250, 500), "label": "品质升级"},
}


def _normalize_budget(budget: Any) -> str:
    """将前端传入的预算归一化为内部档位"""
    if isinstance(budget, (int, float)):
        if budget < 400:
            return "经济"
        elif budget < 1000:
            return "中等"
        return "豪华"
    budget_str = str(budget).strip()
    for key in BUDGET_LEVELS:
        if key in budget_str:
            return key
    return "中等"


def _normalize_crowd(crowd: Any) -> str:
    """将前端传入的人群归一化为内部类型"""
    if not crowd:
        return "情侣"
    crowd_str = str(crowd)
    mapping = {
        "亲子": "亲子家庭", "带娃": "亲子家庭", "家庭": "亲子家庭",
        "情侣": "情侣", "蜜月": "情侣", "约会": "情侣", "二人": "情侣",
        "老年": "银发康养", "老人": "银发康养", "爸妈": "银发康养", "父母": "银发康养", "银发": "银发康养",
        "团建": "团建", "公司": "团建", "团队": "团建",
        "学生": "学生穷游", "大学生": "学生穷游", "穷游": "学生穷游",
        "商务": "商务", "出差": "商务",
        "朋友": "朋友", "闺蜜": "朋友", "兄弟": "朋友",
    }
    for k, v in mapping.items():
        if k in crowd_str:
            return v
    return "情侣"


def _extract_days(days: Any) -> int:
    """从各种输入中提取天数"""
    if isinstance(days, int):
        return max(1, min(days, 10))
    if isinstance(days, float):
        return max(1, min(int(days), 10))
    m = re.search(r"(\d+)", str(days))
    if m:
        return max(1, min(int(m.group(1)), 10))
    return 3


def validate_plan_params(data: dict) -> Dict[str, Any]:
    """
    校验规划参数完整性。

    返回：
      {
        "valid": bool,
        "missing": [缺失字段],
        "clarification": "澄清话术"
      }
    """
    city = data.get("city") or data.get("destination")
    days = data.get("days")
    relationship = data.get("relationship") or data.get("crowd_type") or data.get("crowd")
    budget = data.get("budget")

    missing = []
    if not city:
        missing.append("目的地")
    if days is None or _extract_days(days) is None:
        missing.append("出行天数")
    if not relationship:
        missing.append("同行关系")

    if missing:
        clarification = (
            "为了让行程方案真正贴合您的需求，我还需要确认以下信息：\n"
            + "\n".join(f"{i+1}. {item}" for i, item in enumerate(missing))
        )
        if not budget:
            clarification += "\n（可选）人均预算：这将帮助我推荐经济/舒适/豪华三档方案。"
        clarification += "\n\n您可以先回复任意几项，我会基于已有信息先给一版草案，并标注所有假设条件。"
        return {"valid": False, "missing": missing, "clarification": clarification}

    return {"valid": True, "missing": [], "clarification": ""}


def build_plan_skeleton(data: dict) -> Dict[str, Any]:
    """
    根据用户输入生成结构化行程骨架，供 LLM 在约束下润色。

    返回字段：
      - preferences: 归一化后的用户参数
      - schedule: 每日主题与时段框架
      - budget_estimate: 三档预算估算
      - recommendation_strategy: 人群策略
      - conversion_points: 消费转化点建议
      - assumptions: 自动假设项（用于在输出顶部声明）
      - data_sources: 数据来源声明
    """
    city = data.get("city") or data.get("destination") or ""
    days = _extract_days(data.get("days", 3))
    budget_level = _normalize_budget(data.get("budget", "中等"))
    crowd_type = _normalize_crowd(data.get("relationship") or data.get("crowd_type") or data.get("crowd"))
    preferences = data.get("tags") or data.get("preferences") or []
    if isinstance(preferences, str):
        preferences = [p.strip() for p in preferences.split(",") if p.strip()]
    transport = data.get("transport_mode", "auto")
    num_people = 2
    travelers = data.get("travelers", "")
    if travelers:
        m = re.search(r"(\d+)", str(travelers))
        if m:
            num_people = max(1, min(int(m.group(1)), 20))

    # 自动假设声明
    assumptions = []
    if not data.get("budget"):
        assumptions.append(f"人均预算档位={budget_level}")
    if not data.get("tags") and not data.get("preferences"):
        base_tags = DESTINATION_BASE.get(city, {}).get("tags", ["自然风光", "美食"])
        preferences = base_tags[:3]
        assumptions.append(f"兴趣偏好={', '.join(preferences)}")
    if transport == "auto":
        assumptions.append("出行方式=智能推荐（根据预算与场景匹配）")

    strategy = CROWD_STRATEGIES.get(crowd_type, CROWD_STRATEGIES["情侣"])

    # 生成每日主题框架
    base_themes = ["抵达与初探", "核心体验", "深度文化", "休闲放松", "周边探索", "返程采购"]
    schedule = []
    for day in range(1, days + 1):
        theme = base_themes[(day - 1) % len(base_themes)]
        schedule.append({
            "day": day,
            "date": (datetime.now() + timedelta(days=day - 1)).strftime("%Y-%m-%d"),
            "theme": f"第{day}天 · {theme}",
            "morning": {"time": "09:00-12:00", "focus": strategy["景点侧重"].split("、")[0], "activity_type": "景点/体验"},
            "afternoon": {"time": "13:30-17:30", "focus": strategy["景点侧重"].split("、")[min(1, len(strategy["景点侧重"].split("、"))-1)], "activity_type": "景点/体验"},
            "evening": {"time": "18:30-21:00", "focus": "特色餐饮+夜游/休闲", "activity_type": "餐饮/夜游"},
            "transport": "公共交通为主，短途打车辅助" if transport == "auto" else transport,
            "satisfaction_goal": f"满足{crowd_type}核心需求：{strategy['景点侧重'].split('、')[0]}",
            "conversion_goal": strategy["消费点"][day % len(strategy["消费点"])],
        })

    # 三档预算估算
    factor = BUDGET_LEVELS.get(budget_level, BUDGET_LEVELS["中等"])
    def _range_total(low_key, high_key, multiplier):
        low = factor[low_key][0] * multiplier
        high = factor[high_key][1] * multiplier
        return f"{low}-{high}"

    budget_estimate = {
        "住宿": _range_total("hotel_per_night", "hotel_per_night", max(days - 1, 1)),
        "餐饮": _range_total("meal_per_person", "meal_per_person", num_people * days),
        "门票娱乐": _range_total("ticket_per_day", "ticket_per_day", num_people * days),
        "市内交通": "按实际出行方式估算，约占总花费 10%-20%",
        "说明": "以上为人均/全程参考区间，不含往返大交通；实际以官方平台或现场为准。"
    }

    # 三档消费升级建议
    three_tiers = {
        "基础版": {
            "住宿": f"经济型酒店/民宿（参考 {BUDGET_LEVELS['经济']['hotel_per_night'][0]}-{BUDGET_LEVELS['经济']['hotel_per_night'][1]} 元/晚）",
            "餐饮": f"本地小吃+快餐（参考 {BUDGET_LEVELS['经济']['meal_per_person'][0]}-{BUDGET_LEVELS['经济']['meal_per_person'][1]} 元/人/餐）",
            "体验": "免费/低价景点为主",
        },
        "体验升级版": {
            "住宿": f"舒适型酒店/精品民宿（参考 {BUDGET_LEVELS['中等']['hotel_per_night'][0]}-{BUDGET_LEVELS['中等']['hotel_per_night'][1]} 元/晚）",
            "餐饮": f"特色餐厅+下午茶（参考 {BUDGET_LEVELS['中等']['meal_per_person'][0]}-{BUDGET_LEVELS['中等']['meal_per_person'][1]} 元/人/餐）",
            "体验": "联票+1-2 个深度体验项目",
        },
        "高客单版": {
            "住宿": f"高端酒店/度假村（参考 {BUDGET_LEVELS['豪华']['hotel_per_night'][0]}-{BUDGET_LEVELS['豪华']['hotel_per_night'][1]} 元/晚）",
            "餐饮": f"景观餐厅+商务宴请（参考 {BUDGET_LEVELS['豪华']['meal_per_person'][0]}-{BUDGET_LEVELS['豪华']['meal_per_person'][1]} 元/人/餐）",
            "体验": "私导/夜游/文创礼盒/摄影服务",
        },
    }

    return {
        "preferences": {
            "destination": city,
            "days": days,
            "budget_level": budget_level,
            "crowd_type": crowd_type,
            "num_people": num_people,
            "preferences": preferences,
            "transport_mode": transport,
        },
        "schedule": schedule,
        "budget_estimate": budget_estimate,
        "three_tiers": three_tiers,
        "recommendation_strategy": strategy,
        "conversion_points": strategy["消费点"],
        "assumptions": assumptions,
        "data_sources": [
            "人群策略与消费点：基于 Skill 内置规则库（参考行业运营经验）",
            "预算区间：基于行业参考均值（AI 推断，实际以酒店/餐厅/景区官方为准）",
            "实时天气/POI：由天气插件、高德 POI 插件提供（调用成功时标注实时）",
        ],
    }


def build_plan_fallback(reason: str, data: dict) -> Dict[str, Any]:
    """
    当插件异常或 LLM 失败时，返回最小可用行程框架。
    """
    city = data.get("city") or data.get("destination") or "目的地未指定"
    days = _extract_days(data.get("days", 2))
    return {
        "fallback": True,
        "reason": reason,
        "minimal_plan": {
            "destination": city,
            "days": days,
            "note": "当前生成遇到波动，已为您简化为一版核心行程框架。",
            "days_outline": [
                {"day": d, "theme": f"第{d}天 · 核心体验待补充", "key_spots": []}
                for d in range(1, days + 1)
            ],
            "suggestion": "请补充目的地、天数、同行关系后，我会重新生成完整方案。",
        },
    }


if __name__ == "__main__":
    # 本地自测
    sample_input = {
        "destination": "杭州",
        "days": 3,
        "budget": "中等",
        "crowd_type": "情侣",
        "preferences": ["自然风光", "美食", "文化"],
        "transport_mode": "auto",
        "travelers": "2人",
    }
    print(json.dumps(build_plan_skeleton(sample_input), ensure_ascii=False, indent=2))
