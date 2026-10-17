"""
文旅客源精准匹配引擎 (Matching Engine)
基于客群画像的多维度产品匹配与转化策略生成
"""

import json
import sys
from typing import Dict, List, Optional


# ======== 客群画像库 ========
CROWD_PROFILES = {
    "family_young": {
        "crowd_id": "family_young",
        "label": "幼龄亲子家庭",
        "age_range": "25-40",
        "children_age": "0-6",
        "group_size": "3-5人",
        "budget_per_person": "500-1500元",
        "trip_duration": "1-3天",
        "core_needs": ["安全", "便捷", "亲子设施", "短途"],
        "pain_points": ["孩子哭闹影响他人", "没有儿童餐", "设施不安全"],
        "decision_factors": ["儿童政策", "安全性", "亲子设施", "距离"],
        "info_channels": ["小红书", "抖音亲子", "妈妈群"],
        "keywords": ["遛娃", "亲子", "安全", "儿童", "乐园"]
    },
    "gen_z": {
        "crowd_id": "gen_z",
        "label": "Z世代年轻客群",
        "age_range": "18-28",
        "group_size": "2-4人",
        "budget_per_person": "300-800元",
        "trip_duration": "1-3天",
        "core_needs": ["出片效果", "氛围感", "性价比", "小众"],
        "pain_points": ["商业化太重", "千篇一律", "预算有限"],
        "decision_factors": ["视觉吸引力", "社交传播性", "价格"],
        "info_channels": ["抖音", "B站", "小红书"],
        "keywords": ["出片", "氛围感", "小众", "宝藏", "打卡"]
    },
    "couple": {
        "crowd_id": "couple",
        "label": "情侣蜜月客群",
        "age_range": "22-35",
        "group_size": "2人",
        "budget_per_person": "800-3000元",
        "trip_duration": "2-5天",
        "core_needs": ["浪漫氛围", "隐私", "仪式感", "纪念价值"],
        "pain_points": ["人多嘈杂", "服务不贴心", "缺乏惊喜"],
        "decision_factors": ["氛围感", "隐私性", "景观", "服务"],
        "info_channels": ["小红书", "抖音", "朋友圈"],
        "keywords": ["浪漫", "仪式感", "二人世界", "私享", "绝美"]
    },
    "senior": {
        "crowd_id": "senior",
        "label": "银发康养客群",
        "age_range": "55+",
        "group_size": "2-6人",
        "budget_per_person": "500-1500元",
        "trip_duration": "3-7天",
        "core_needs": ["舒适", "康养", "文化", "安全"],
        "pain_points": ["行程太赶", "台阶太多", "医疗不便"],
        "decision_factors": ["舒适度", "健康安全", "文化内涵"],
        "info_channels": ["微信", "今日头条", "社区推荐"],
        "keywords": ["康养", "慢生活", "文化", "品质", "养生"]
    },
    "business": {
        "crowd_id": "business",
        "label": "商务出行客群",
        "age_range": "30-50",
        "group_size": "1-5人",
        "budget": "公司报销",
        "trip_duration": "1-3天",
        "core_needs": ["效率", "品质", "便利", "商务配套"],
        "pain_points": ["交通不便", "入住繁琐", "缺少办公设施"],
        "decision_factors": ["位置", "效率", "配套", "品牌"],
        "info_channels": ["携程商旅", "飞猪", "企业协议"],
        "keywords": ["高效", "品质", "便利", "商务"]
    },
    "team_building": {
        "crowd_id": "team_building",
        "label": "团建聚会客群",
        "age_range": "22-45",
        "group_size": "10-50人",
        "budget_per_person": "300-1000元",
        "trip_duration": "1-2天",
        "core_needs": ["活动场地", "团队项目", "聚餐空间"],
        "pain_points": ["场地不够", "项目单一", "组织麻烦"],
        "decision_factors": ["承载量", "项目丰富度", "餐饮配套"],
        "info_channels": ["企业HR社群", "团建平台", "微信"],
        "keywords": ["团建", "拓展", "团队", "聚会", "轰趴"]
    }
}

# ======== 产品匹配规则库 ========
MATCH_RULES = {
    "family_young": {
        "scenic": {"优先": ["主题乐园", "动物园", "海洋馆"], "可选": ["自然景区(平缓)", "科技馆"], "避免": ["高危户外", "长途徒步"]},
        "hotel": {"优先": ["亲子房型", "儿童乐园", "加床服务"], "必要设施": ["婴儿床", "儿童餐具", "恒温泳池"]},
        "dining": {"优先": ["亲子餐厅", "自助餐", "包间"], "推荐菜品": ["清淡口味", "儿童营养餐"]},
        "marketing_channel": ["小红书", "抖音亲子", "妈妈群"],
        "content_direction": ["遛娃攻略", "亲子vlog", "酒店测评"],
        "conversion_hook": ["亲子套餐", "早鸟优惠", "儿童免票"]
    },
    "gen_z": {
        "scenic": {"优先": ["网红打卡地", "艺术展", "潮流街区"], "可选": ["古镇", "创意园区"], "避免": ["传统观光团景点"]},
        "hotel": {"优先": ["设计感民宿", "青旅", "胶囊酒店"], "可选": ["性价比酒店"], "避免": ["老旧商务酒店"]},
        "dining": {"优先": ["网红店", "特色小吃", "主题餐厅"], "可选": ["夜市", "美食街"]},
        "marketing_channel": ["抖音", "B站", "小红书"],
        "content_direction": ["打卡攻略", "短视频测评", "种草图文"],
        "conversion_hook": ["限时团购", "社交裂变", "早鸟预售"]
    },
    "couple": {
        "scenic": {"优先": ["日落观景点", "温泉", "花海"], "可选": ["海岛", "酒庄"], "避免": ["亲子乐园"]},
        "hotel": {"优先": ["大床房", "景观房", "浴缸", "私汤"], "可选": ["独栋别墅", "设计师酒店"]},
        "dining": {"优先": ["氛围餐厅", "露台餐", "私房菜"], "可选": ["日料", "法餐"]},
        "marketing_channel": ["小红书", "抖音", "婚礼博主"],
        "content_direction": ["浪漫攻略", "情侣vlog", "求婚策划"],
        "conversion_hook": ["套餐预售", "节日限定", "私享定制"]
    },
    "senior": {
        "scenic": {"优先": ["文化古迹", "博物馆", "平缓自然景区"], "可选": ["植物园", "温泉"], "避免": ["高山", "长途徒步"]},
        "hotel": {"优先": ["电梯", "低层", "安静", "医疗配套"], "可选": ["温泉酒店", "康养基地"]},
        "dining": {"优先": ["清淡本地菜", "养生餐"], "可选": ["自助餐"], "避免": ["辛辣", "油炸", "生冷"]},
        "marketing_channel": ["微信", "头条", "社区推荐"],
        "content_direction": ["康养攻略", "文化解读", "慢生活分享"],
        "conversion_hook": ["康养套餐", "口碑推荐", "老客优惠"]
    },
    "business": {
        "scenic": {"可选": ["商务区周边", "休闲茶楼"], "避免": ["排队景区"]},
        "hotel": {"优先": ["商务房", "会议室", "高速WiFi"], "必要设施": ["打印", "叫醒"]},
        "dining": {"优先": ["商务简餐", "包间", "茶歇"]},
        "marketing_channel": ["携程商旅", "飞猪", "企业BD"],
        "content_direction": ["效率攻略", "商务配套"],
        "conversion_hook": ["企业协议", "会员体系", "差旅管理"]
    },
    "team_building": {
        "scenic": {"优先": ["户外拓展基地", "大型自然景区"], "可选": ["主题乐园"]},
        "hotel": {"优先": ["团队房", "会议室", "烧烤区"], "可选": ["多功能厅"]},
        "dining": {"优先": ["圆桌宴会", "烧烤自助", "火锅"]},
        "marketing_channel": ["企业HR社群", "团建平台"],
        "content_direction": ["团建方案", "活动vlog"],
        "conversion_hook": ["团建套餐", "定制方案", "早鸟优惠"]
    }
}


def get_crowd_profile(crowd_id: str) -> Optional[Dict]:
    """获取客群画像"""
    return CROWD_PROFILES.get(crowd_id)


def list_crowds() -> List[Dict]:
    """列出所有客群"""
    return [{"id": k, "label": v["label"], "keywords": v["keywords"]} for k, v in CROWD_PROFILES.items()]


def identify_crowd(user_description: str) -> List[Dict]:
    """基于用户描述识别可能的客群类型"""
    scores = {}
    for crowd_id, profile in CROWD_PROFILES.items():
        score = 0
        lower_desc = user_description.lower()
        for keyword in profile.get("keywords", []):
            if keyword.lower() in lower_desc:
                score += 1
        # 也检查核心需求
        for need in profile.get("core_needs", []):
            if need.lower() in lower_desc:
                score += 0.5
        if score > 0:
            scores[crowd_id] = score

    sorted_crowds = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [{"crowd_id": c, "label": CROWD_PROFILES[c]["label"], "match_score": s, "profile": CROWD_PROFILES[c]} for c, s in sorted_crowds]


def match_products(crowd_id: str, destination: str = None) -> Dict:
    """根据客群匹配产品推荐"""
    profile = get_crowd_profile(crowd_id)
    if not profile:
        return {"error": f"未找到客群: {crowd_id}", "available": [k for k in CROWD_PROFILES.keys()]}

    rules = MATCH_RULES.get(crowd_id, {})
    destination_context = f"目的地: {destination}" if destination else "通用推荐"

    return {
        "crowd": profile["label"],
        "destination": destination or "未指定",
        "profile_summary": {
            "age": profile["age_range"],
            "budget": profile.get("budget_per_person", profile.get("budget", "")),
            "needs": profile["core_needs"],
            "pain_points": profile["pain_points"]
        },
        "product_match": {
            "scenic": rules.get("scenic", {}),
            "hotel": rules.get("hotel", {}),
            "dining": rules.get("dining", {})
        },
        "marketing_strategy": {
            "channels": rules.get("marketing_channel", []),
            "content": rules.get("content_direction", []),
            "hooks": rules.get("conversion_hook", [])
        }
    }


def generate_strategy(crowd_id: str, channel: str = None) -> Dict:
    """生成定向转化策略"""
    profile = get_crowd_profile(crowd_id)
    if not profile:
        return {"error": f"未找到客群: {crowd_id}"}

    rules = MATCH_RULES.get(crowd_id, {})

    strategy = {
        "crowd": profile["label"],
        "analysis": {
            "核心需求": profile["core_needs"],
            "决策因子": profile["decision_factors"],
            "痛点": profile["pain_points"]
        },
        "channel_strategy": {
            "推荐渠道": rules.get("marketing_channel", []),
            "内容方向": rules.get("content_direction", []),
            "转化钩子": rules.get("conversion_hook", [])
        },
        "budget_guidance": {
            "参考预算": profile.get("budget_per_person", profile.get("budget", "")),
            "时长": profile["trip_duration"]
        },
        "timing": _get_timing_advice(crowd_id)
    }

    if channel:
        strategy["channel_focus"] = {
            "channel": channel,
            "content_tips": _get_channel_tips(crowd_id, channel)
        }

    return strategy


def _get_timing_advice(crowd_id: str) -> Dict:
    """获取触达时机建议"""
    timing_map = {
        "family_young": {"最佳触达": "周四-周五（计划周末游）", "提前量": "3-7天", "发布时段": "晚间20:00-22:00"},
        "gen_z": {"最佳触达": "随时（冲动消费型）", "提前量": "1-3天", "发布时段": "午间12:00/晚间20:00-23:00"},
        "couple": {"最佳触达": "节前1周", "提前量": "5-14天", "发布时段": "晚间21:00-23:00"},
        "senior": {"最佳触达": "工作日白天", "提前量": "7-30天", "发布时段": "上午9:00/下午15:00"},
        "business": {"最佳触达": "工作日", "提前量": "1-3天", "发布时段": "上午9:00-11:00"},
        "team_building": {"最佳触达": "月初/季初", "提前量": "14-30天", "发布时段": "工作日10:00-16:00"}
    }
    return timing_map.get(crowd_id, {})


def _get_channel_tips(crowd_id: str, channel: str) -> str:
    """获取特定渠道的内容建议"""
    tips_map = {
        "family_young": {"小红书": "封面用萌娃+目的地实拍图，标题带'遛娃'/'亲子宝藏'标签，正文突出安全性和儿童友好设施", "抖音": "15-30秒亲子vlog，黄金3秒用孩子笑脸/互动瞬间，BGM用温馨轻快音乐"},
        "gen_z": {"小红书": "大片感封面+多图拼贴，正文用emoji分段，带#出片 #氛围感，强调视觉冲击", "抖音": "快节奏15秒打卡，运镜+转场，BGM用潮流音乐，强行动号召收尾", "B站": "深度测评+真实vlog，3-5分钟，强调性价比和真实体验"},
        "couple": {"小红书": "浪漫氛围感封面，双人合照，正文带仪式感描述，#浪漫旅行 #情侣出行", "抖音": "甜蜜氛围30秒，日落/星空场景+BGM，叙事感文案"},
        "senior": {"微信": "图文推送，大字排版，突出康养文化价值，附联系方式", "头条": "深度图文，文化解读为主，配合实景图片"}
    }
    return tips_map.get(crowd_id, {}).get(channel, f"在{channel}上发布{get_crowd_profile(crowd_id)['label']}相关内容")


def full_pipeline(user_description: str, destination: str = None) -> Dict:
    """一键式全流程匹配"""
    # Step 1: 客群识别
    crowds = identify_crowd(user_description)
    if not crowds:
        return {"error": "未能从描述中识别出明确客群类型", "suggestion": "请补充更多客群特征描述"}

    # Step 2: 取最佳匹配客群
    best_match = crowds[0]
    crowd_id = best_match["crowd_id"]

    # Step 3: 产品匹配
    product_match = match_products(crowd_id, destination)

    # Step 4: 转化策略
    strategy = generate_strategy(crowd_id)

    return {
        "user_description": user_description,
        "destination": destination,
        "identified_crowds": crowds,
        "best_match": {
            "crowd_id": crowd_id,
            "label": best_match["label"],
            "confidence": best_match["match_score"]
        },
        "product_recommendations": product_match.get("product_match", {}),
        "conversion_strategy": strategy
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='文旅客源精准匹配引擎')
    parser.add_argument('--action', choices=['profile', 'list', 'identify', 'match', 'strategy', 'pipeline'],
                        default='pipeline', help='执行操作')
    parser.add_argument('--crowd', help='客群ID')
    parser.add_argument('--input', help='用户描述文本')
    parser.add_argument('--destination', help='目的地')
    parser.add_argument('--channel', help='目标渠道')

    args = parser.parse_args()

    if args.action == 'list':
        result = list_crowds()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.action == 'profile':
        if not args.crowd:
            print(json.dumps({"error": "请指定 --crowd"}, ensure_ascii=False))
            sys.exit(1)
        result = get_crowd_profile(args.crowd)
        print(json.dumps(result, ensure_ascii=False, indent=2) if result else "未找到客群")

    elif args.action == 'identify':
        if not args.input:
            print(json.dumps({"error": "请指定 --input 描述文本"}, ensure_ascii=False))
            sys.exit(1)
        result = identify_crowd(args.input)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.action == 'match':
        if not args.crowd:
            print(json.dumps({"error": "请指定 --crowd"}, ensure_ascii=False))
            sys.exit(1)
        result = match_products(args.crowd, args.destination)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.action == 'strategy':
        if not args.crowd:
            print(json.dumps({"error": "请指定 --crowd"}, ensure_ascii=False))
            sys.exit(1)
        result = generate_strategy(args.crowd, args.channel)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.action == 'pipeline':
        if not args.input:
            print(json.dumps({"error": "请指定 --input 描述文本"}, ensure_ascii=False))
            sys.exit(1)
        result = full_pipeline(args.input, args.destination)
        print(json.dumps(result, ensure_ascii=False, indent=2))
