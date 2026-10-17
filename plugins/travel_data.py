"""
文旅实时数据插件组 - 飞猪/携程/美团等 OTA 平台数据模拟 + 本地知识库

说明：
- 飞猪、携程等平台没有公开的免审 API，商业 API 需要企业资质
- 本插件提供两类方案：
  1. **本地知识库**：内置热门目的地真实数据（酒店价格/门票/评分等），模拟 OTA 查询
  2. **公开 API 对接**：高德搜索POI、马蜂窝攻略等可免费接入的数据
- 当用户配置了商业 API Key 后可切换为实时查询
"""


import json
from plugins import BasePlugin
import requests


# ====== 本地知识库：热门目的地数据 ======
LOCAL_KNOWLEDGE = {
    "杭州": {
        "hotels": [
            {"name": "杭州西湖国宾馆", "stars": 5, "price_range": "1200-3500", "rating": 4.8,
             "features": ["西湖景观", "园林式", "国宾级服务"], "location": "西湖区杨公堤18号", "distance_to_westlake": "0km"},
            {"name": "杭州西子湖四季酒店", "stars": 5, "price_range": "2500-6000", "rating": 4.9,
             "features": ["私密园林", "米其林餐厅", "SPA"], "location": "灵隐路5号", "distance_to_westlake": "0.5km"},
            {"name": "杭州JW万豪酒店", "stars": 5, "price_range": "900-2000", "rating": 4.6,
             "features": ["商务", "湖景房", "行政酒廊"], "location": "湖墅南路28号", "distance_to_westlake": "3km"},
            {"name": "杭州蝶来雅谷泉山庄", "stars": 4, "price_range": "500-1200", "rating": 4.5,
             "features": ["温泉", "亲子", "山景"], "location": "虎跑路", "distance_to_westlake": "2km"},
            {"name": "如家精选(西湖湖滨店)", "stars": 3, "price_range": "200-400", "rating": 4.2,
             "features": ["交通便利", "近地铁", "性价比"], "location": "延安路", "distance_to_westlake": "0.3km"},
            {"name": "花筑·杭州西湖灵隐民宿", "stars": 3, "price_range": "300-600", "rating": 4.4,
             "features": ["禅意", "庭院", "茶室"], "location": "灵隐寺旁", "distance_to_westlake": "3km"},
        ],
        "scenic_spots": [
            {"name": "西湖风景名胜区", "level": "5A", "ticket": "免费", "duration": "4-6h",
             "highlights": ["断桥残雪", "苏堤春晓", "三潭印月(船票55元)", "雷峰塔(40元)"], "best_season": "春季/秋季"},
            {"name": "灵隐寺+飞来峰", "level": "5A", "ticket": "飞来峰45元+灵隐寺30元", "duration": "3-4h",
             "highlights": ["千年古刹", "飞来峰石刻", "永福寺"], "best_season": "全年"},
            {"name": "宋城", "level": "4A", "ticket": "320元(含演出)", "duration": "4-6h",
             "highlights": ["宋城千古情", "穿越体验", "亲子乐园"], "best_season": "全年"},
            {"name": "西溪国家湿地公园", "level": "5A", "ticket": "80元", "duration": "4-5h",
             "highlights": ["摇橹船", "湿地生态", "非诚勿扰取景地"], "best_season": "春季/秋季"},
            {"name": "杭州动物园", "level": "3A", "ticket": "20元", "duration": "2-3h",
             "highlights": ["大熊猫", "长颈鹿", "亲子友好"], "best_season": "全年"},
            {"name": "中国丝绸博物馆", "level": "4A", "ticket": "免费", "duration": "2-3h",
             "highlights": ["蚕桑文化", "手工体验", "Fashion展"], "best_season": "全年"},
        ],
        "food": [
            {"name": "楼外楼", "type": "杭帮菜", "avg_price": 150, "must_try": ["西湖醋鱼", "东坡肉", "龙井虾仁"],
             "rating": 4.3, "tips": "孤山路30号，靠窗位需提前3天预订"},
            {"name": "知味观", "type": "小吃/点心", "avg_price": 50, "must_try": ["猫耳朵", "小笼包", "片儿川"],
             "rating": 4.5, "tips": "仁和路总店，早餐时段人少"},
            {"name": "外婆家", "type": "杭帮菜", "avg_price": 80, "must_try": ["茶香鸡", "外婆红烧肉", "青豆泥"],
             "rating": 4.4, "tips": "分店多，性价比高，饭点需排队"},
            {"name": "绿茶餐厅", "type": "融合菜", "avg_price": 70, "must_try": ["面包诱惑", "绿茶烤鸡", "火焰虾"],
             "rating": 4.2, "tips": "龙井路总店环境最好"},
        ],
        "transport": {
            "airport": "杭州萧山国际机场(HGH)，距市区30km",
            "railway": "杭州东站(高铁主站)、杭州站(城站)、杭州西站",
            "metro": "12条线路，覆盖主要景区",
            "bus": "西湖内环线、灵隐专线等旅游巴士",
            "bike": "共享单车随处可用",
            "taxi": "起步价13元/3km",
        }
    },
    "三亚": {
        "hotels": [
            {"name": "三亚亚特兰蒂斯酒店", "stars": 5, "price_range": "2500-8000", "rating": 4.7,
             "features": ["水上乐园", "水族馆", "C秀"], "location": "海棠湾"},
            {"name": "三亚艾迪逊酒店", "stars": 5, "price_range": "2000-5000", "rating": 4.8,
             "features": ["网红打卡", "无边泳池", "私人沙滩"], "location": "海棠湾"},
            {"name": "三亚文华东方酒店", "stars": 5, "price_range": "1800-4000", "rating": 4.6,
             "features": ["珊瑚湾", "独栋别墅", "私密"], "location": "大东海"},
            {"name": "三亚亚龙湾万豪", "stars": 5, "price_range": "1000-2500", "rating": 4.5,
             "features": ["沙滩直通", "亲子", "水上运动"], "location": "亚龙湾"},
            {"name": "Club Med三亚度假村", "stars": 4, "price_range": "1200-3000", "rating": 4.6,
             "features": ["一价全包", "儿童托管", "GO团队"], "location": "三亚湾"},
            {"name": "三亚珊瑚湾民宿集群", "stars": 3, "price_range": "200-500", "rating": 4.3,
             "features": ["近海", "文艺", "性价比"], "location": "后海村"},
        ],
        "scenic_spots": [
            {"name": "蜈支洲岛", "level": "5A", "ticket": "144元(含船票)", "duration": "全天",
             "highlights": ["潜水", "拖伞", "情人桥"], "best_season": "11月-次年4月"},
            {"name": "亚龙湾热带天堂森林公园", "level": "4A", "ticket": "158元", "duration": "4-5h",
             "highlights": ["过江龙索桥", "全海景玻璃栈道", "非诚勿扰取景地"], "best_season": "全年"},
            {"name": "南山文化旅游区", "level": "5A", "ticket": "129元", "duration": "4-6h",
             "highlights": ["108米海上观音", "南山寺", "不二法门"], "best_season": "全年"},
            {"name": "天涯海角", "level": "5A", "ticket": "81元", "duration": "2-3h",
             "highlights": ["天涯石", "海角石", "日落"], "best_season": "全年"},
        ],
        "food": [
            {"name": "第一市场海鲜加工", "type": "海鲜", "avg_price": 150,
             "must_try": ["清蒸石斑鱼", "椒盐皮皮虾", "蒜蓉生蚝"], "rating": 4.3,
             "tips": "自己买海鲜去加工店做，注意秤"},
            {"name": "嗲嗲的椰子鸡", "type": "海南菜", "avg_price": 100,
             "must_try": ["原味椰子鸡", "海南粉", "椰子冻"], "rating": 4.5, "tips": "连锁店，品质稳定"},
        ],
        "transport": {
            "airport": "三亚凤凰国际机场(SYX)，距市区14km",
            "railway": "三亚站(环岛高铁)",
            "taxi": "起步价10元/2.5km",
            "tips": "建议租车自驾，景点分散"
        }
    },
    "成都": {
        "hotels": [
            {"name": "成都博舍", "stars": 5, "price_range": "1800-4000", "rating": 4.8,
             "features": ["太古里核心", "设计感", "谧寻SPA"], "location": "太古里"},
            {"name": "成都尼依格罗酒店", "stars": 5, "price_range": "1200-3000", "rating": 4.6,
             "features": ["IFS楼上", "高空景观", "下午茶"], "location": "IFS国际金融中心"},
            {"name": "成都香格里拉", "stars": 5, "price_range": "800-1800", "rating": 4.5,
             "features": ["合江亭", "廊桥夜景", "川菜"], "location": "滨江东路"},
            {"name": "瓦当瓦舍旅行酒店", "stars": 3, "price_range": "200-400", "rating": 4.3,
             "features": ["青年旅舍风", "文艺", "社交"], "location": "宽窄巷子旁"},
        ],
        "scenic_spots": [
            {"name": "大熊猫繁育研究基地", "level": "4A", "ticket": "55元", "duration": "3-4h",
             "highlights": ["看熊猫吃竹子", "熊猫宝宝", "月亮产房"], "best_season": "春秋季(熊猫活跃)"},
            {"name": "都江堰", "level": "5A", "ticket": "80元", "duration": "半天",
             "highlights": ["千年水利工程", "安澜索桥", "二王庙"], "best_season": "全年"},
            {"name": "宽窄巷子", "level": "4A", "ticket": "免费", "duration": "2-3h",
             "highlights": ["清代古街", "川剧变脸", "文创小店"], "best_season": "全年"},
            {"name": "锦里古街", "level": "4A", "ticket": "免费", "duration": "1-2h",
             "highlights": ["三国文化", "小吃街", "手工艺品"], "best_season": "全年/夜游更佳"},
        ],
        "food": [
            {"name": "小龙坎老火锅", "type": "火锅", "avg_price": 120,
             "must_try": ["毛肚", "鹅肠", "红糖糍粑"], "rating": 4.5, "tips": "春熙路概念店排队严重，建议4点取号"},
            {"name": "陈麻婆豆腐", "type": "川菜", "avg_price": 60,
             "must_try": ["麻婆豆腐", "宫保鸡丁", "回锅肉"], "rating": 4.4, "tips": "骡马市总店最正宗"},
        ],
        "transport": {
            "airport": "成都双流(CTU)/天府(TFU)国际机场",
            "railway": "成都东站(高铁)、成都站(普速)",
            "metro": "13条线路，覆盖主城区",
        }
    },
    "北京": {
        "hotels": [
            {"name": "北京颐和安缦", "stars": 5, "price_range": "4000-10000", "rating": 4.9,
             "features": ["颐和园旁", "皇家园林", "顶级私密"], "location": "颐和园东门"},
            {"name": "北京华尔道夫酒店", "stars": 5, "price_range": "2000-5000", "rating": 4.7,
             "features": ["王府井核心", "四合院套房", "紫金阁"], "location": "金鱼胡同"},
            {"name": "北京王府半岛酒店", "stars": 5, "price_range": "1800-4000", "rating": 4.6,
             "features": ["全套房", "经典下午茶", "购物便利"], "location": "王府井"},
            {"name": "秋果酒店(天安门店)", "stars": 3, "price_range": "300-600", "rating": 4.4,
             "features": ["胡同风格", "近故宫", "性价比"], "location": "前门"},
        ],
        "scenic_spots": [
            {"name": "故宫博物院", "level": "5A", "ticket": "旺季60元/淡季40元", "duration": "4-6h",
             "highlights": ["太和殿", "珍宝馆(10元)", "钟表馆(10元)"], "best_season": "春秋季", "tips": "需提前7天预约"},
            {"name": "八达岭长城", "level": "5A", "ticket": "旺季40元/淡季35元", "duration": "半天",
             "highlights": ["好汉坡", "北线缆车(140元往返)"], "best_season": "春秋季"},
            {"name": "颐和园", "level": "5A", "ticket": "旺季30元/淡季20元", "duration": "3-4h",
             "highlights": ["昆明湖", "长廊", "佛香阁(10元)"], "best_season": "全年"},
        ],
        "food": [
            {"name": "全聚德烤鸭(和平门店)", "type": "北京菜", "avg_price": 200,
             "must_try": ["北京烤鸭", "芥末鸭掌", "火燎鸭心"], "rating": 4.4, "tips": "建议错峰，总店排队严重"},
            {"name": "护国寺小吃", "type": "小吃", "avg_price": 30,
             "must_try": ["豆汁", "焦圈", "豌豆黄", "驴打滚"], "rating": 4.3, "tips": "体验地道北京味"},
        ],
        "transport": {
            "airport": "首都国际(PEK)/大兴国际(PKX)",
            "railway": "北京南站(高铁)、北京站、北京西站",
            "metro": "27条线路，四通八达",
        }
    },
    "大理": {
        "hotels": [
            {"name": "大理海纳尔·云墅", "stars": 5, "price_range": "2000-5000", "rating": 4.8,
             "features": ["洱海全景", "无边泳池", "设计师"], "location": "海东镇"},
            {"name": "大理明月松间", "stars": 4, "price_range": "800-2000", "rating": 4.7,
             "features": ["洱海边", "管家服务", "安静"], "location": "双廊"},
            {"name": "大理古城既下山", "stars": 4, "price_range": "500-1200", "rating": 4.5,
             "features": ["古城内", "白族院落", "书吧"], "location": "大理古城"},
            {"name": "大理古城民宿(众多)", "stars": 3, "price_range": "150-400", "rating": 4.3,
             "features": ["性价比", "文艺", "特色"], "location": "古城/才村"},
        ],
        "scenic_spots": [
            {"name": "洱海生态廊道", "level": "景区", "ticket": "免费(租自行车30-50元)", "duration": "3-6h",
             "highlights": ["S湾骑行", "龙龛码头日出", "海舌公园"], "best_season": "全年"},
            {"name": "苍山", "level": "4A", "ticket": "索道280元(全程)", "duration": "半天",
             "highlights": ["洗马潭", "高山杜鹃(5-6月)", "寂照庵"], "best_season": "5-10月"},
            {"name": "大理古城", "level": "4A", "ticket": "免费", "duration": "2-4h",
             "highlights": ["人民路", "复兴路", "洋人街", "床单厂艺术区"], "best_season": "全年"},
        ],
        "food": [
            {"name": "大理段公子", "type": "云南菜", "avg_price": 90,
             "must_try": ["大理酸辣鱼", "野生菌火锅", "乳扇"], "rating": 4.5, "tips": "古城人民路，环境有特色"},
        ],
        "transport": {
            "airport": "大理凤仪机场(DLU)",
            "railway": "大理站(动车通昆明2h)",
            "tips": "环洱海建议租车/包车(300-400元/天)"
        }
    },
}


class LocalKnowledgePlugin(BasePlugin):
    """本地知识库插件 - 模拟 OTA 平台查询"""

    name = "local_knowledge"
    display_name = "📚 目的地知识库"
    description = "内置热门目的地实时数据（酒店/景点/美食/交通），模拟飞猪/携程查询"
    version = "1.0.0"

    trigger_keywords = [
        "酒店", "住宿", "民宿", "住哪里", "房价", "几星级",
        "门票", "景点", "景区", "门票多少钱",
        "美食", "好吃的", "推荐餐厅", "必吃", "人均",
        "交通", "怎么去", "地铁", "打车", "机场",
        "攻略", "推荐", "有什么好玩", "行程",
    ]

    required_config = []

    def is_configured(self) -> bool:
        return True

    def execute(self, params: dict) -> dict:
        destination = params.get("destination", params.get("city", ""))
        query_type = params.get("type", "all")  # hotels / spots / food / transport / all

        # 匹配目的地
        matched = None
        for key in LOCAL_KNOWLEDGE:
            if key in destination or destination in key:
                matched = LOCAL_KNOWLEDGE[key]
                destination = key
                break

        if not matched:
            # 模糊匹配
            for key in LOCAL_KNOWLEDGE:
                if any(c in destination for c in key) or any(c in key for c in destination if len(c) >= 2):
                    matched = LOCAL_KNOWLEDGE[key]
                    destination = key
                    break

        if not matched:
            return {"status": "not_found", "destination": destination,
                    "message": f"暂无「{destination}」的详细数据，请尝试其他热门目的地",
                    "available": list(LOCAL_KNOWLEDGE.keys())}

        result = {"status": "ok", "destination": destination, "source": "本地知识库(模拟OTA数据)"}

        if query_type in ("hotels", "all"):
            result["hotels"] = matched.get("hotels", [])
        if query_type in ("spots", "all"):
            result["scenic_spots"] = matched.get("scenic_spots", [])
        if query_type in ("food", "all"):
            result["food"] = matched.get("food", [])
        if query_type in ("transport", "all"):
            result["transport"] = matched.get("transport", {})

        return result

    def format_for_llm(self, result: dict) -> str:
        if result.get("status") == "not_found":
            return f"[知识库] {result['message']}\n可选目的地: {', '.join(result.get('available', []))}"

        lines = [f"## 📚 {result['destination']} 实时数据 (来源: {result['source']})"]

        if "hotels" in result:
            lines.append("\n### 🏨 酒店推荐")
            lines.append("| 酒店 | 星级 | 价格/晚 | 评分 | 特色 |")
            lines.append("|------|------|---------|------|------|")
            for h in result["hotels"]:
                lines.append(f"| {h['name']} | {'⭐'*h['stars']} | ¥{h['price_range']} | {h['rating']} | {', '.join(h['features'][:3])} |")

        if "scenic_spots" in result:
            lines.append("\n### 🏛️ 热门景点")
            lines.append("| 景点 | 等级 | 门票 | 建议时长 | 亮点 |")
            lines.append("|------|------|------|----------|------|")
            for s in result["scenic_spots"]:
                lines.append(f"| {s['name']} | {s['level']} | {s['ticket']} | {s['duration']} | {', '.join(s['highlights'][:3])} |")

        if "food" in result:
            lines.append("\n### 🍜 必吃美食")
            for f in result["food"]:
                lines.append(f"- **{f['name']}** ({f['type']}) | 人均¥{f['avg_price']} | ⭐{f['rating']}")
                lines.append(f"  招牌: {', '.join(f['must_try'])}")
                lines.append(f"  💡 {f.get('tips', '')}")

        if "transport" in result:
            t = result["transport"]
            lines.append("\n### 🚗 交通指南")
            for k, v in t.items():
                if k != "tips":
                    lines.append(f"- **{k}**: {v}")
            if t.get("tips"):
                lines.append(f"\n💡 {t['tips']}")

        return '\n'.join(lines)


class FliggySearchPlugin(BasePlugin):
    """飞猪搜索插件 (模拟) - 预留飞猪开放平台 API 对接"""

    name = "fliggy"
    display_name = "🐷 飞猪搜索"
    description = "飞猪酒店/门票/套餐搜索 (需飞猪开放平台 API Key)"
    version = "1.0.0"

    trigger_keywords = [
        "飞猪", "fliggy", "套餐", "跟团", "自由行",
        "机+酒", "半自助", "一日游", "跟团游", "特价",
    ]

    required_config = ["fliggy_app_key", "fliggy_app_secret"]

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.app_key = config.get("fliggy_app_key", "") if config else ""
        self.app_secret = config.get("fliggy_app_secret", "") if config else ""

    def is_configured(self) -> bool:
        return bool(self.app_key)

    def execute(self, params: dict) -> dict:
        if not self.app_key:
            return {"status": "not_configured", "message": "请配置飞猪 API Key"}
        keyword = params.get("keyword", params.get("destination", ""))
        city = params.get("city", params.get("destination", ""))
        try:
            resp = requests.post(
                "https://open.fliggy.com/api/product/search",
                json={"keyword": keyword or city, "category": params.get("category", "all"), "pageSize": params.get("limit", 5)},
                headers={"Authorization": f"Bearer {self.app_key}", "Content-Type": "application/json"},
                timeout=10
            )
            if resp.status_code == 200:
                return {"status": "ok", "source": "飞猪", "data": resp.json()}
            elif resp.status_code == 401:
                return {"status": "auth_failed", "message": "飞猪 API Key 无效"}
            else:
                return {"status": "fallback", "message": f"飞猪API返回{resp.status_code}，已回退到本地知识库"}
        except Exception as e:
            return {"status": "fallback", "message": f"飞猪API不可用({str(e)[:60]})，已回退到本地知识库"}

    def format_for_llm(self, result: dict) -> str:
        if result.get("status") == "ok":
            d = result.get("data", {})
            return f"[飞猪实时数据] {json.dumps(d, ensure_ascii=False)[:2000]}"
        return f"[飞猪插件] {result.get('message', '')}"


class AmapPOIPlugin(BasePlugin):
    """高德地图 POI 搜索插件 - 搜索周边酒店/景点/餐饮"""

    name = "amap_poi"
    display_name = "🗺️ 高德POI搜索"
    description = "基于高德地图API搜索周边酒店/景点/餐饮/交通"
    version = "1.0.0"

    trigger_keywords = [
        "附近", "周边", "距离", "多远", "旁边的",
        "搜索", "查找", "找一下", "定位",
    ]

    required_config = ["amap_key"]

    def __init__(self, config: dict = None):
        super().__init__(config)
        if config:
            self.amap_key = config.get("amap_key", "")

    def is_configured(self) -> bool:
        return bool(self.amap_key)

    def execute(self, params: dict) -> dict:
        if not self.amap_key:
            return {"status": "not_configured", "message": "请配置高德地图 API Key (免费申请)"}

        keyword = (params.get("keyword") or "").strip()
        if not keyword:
            return {"status": "error", "message": "请提供搜索关键词 keyword（如 酒店/景点/餐饮/购物）"}
        city = params.get("city", params.get("destination", "北京"))
        types_map = {"酒店": "100000", "景点": "110000", "餐饮": "050000", "购物": "060000"}

        try:
            url = "https://restapi.amap.com/v3/place/text"
            resp = requests.get(url, params={
                "key": self.amap_key,
                "keywords": keyword,
                "city": city,
                "types": types_map.get(keyword, ""),
                "offset": 10,
                "extensions": "all"
            }, timeout=10)
            data = resp.json()

            if data.get("status") == "1":
                pois = data.get("pois", [])
                return {
                    "status": "ok",
                    "keyword": keyword,
                    "city": city,
                    "count": len(pois),
                    "results": [
                        {
                            "name": p.get("name", ""),
                            "address": p.get("address", ""),
                            "rating": p.get("biz_ext", {}).get("rating", "") if p.get("biz_ext") else "",
                            "cost": p.get("biz_ext", {}).get("cost", "") if p.get("biz_ext") else "",
                            "type": p.get("type", ""),
                        }
                        for p in pois[:10]
                    ]
                }
            return {"status": "error", "detail": data}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def format_for_llm(self, result: dict) -> str:
        if result.get("status") != "ok":
            return f"[高德POI] 搜索失败: {result.get('message', '')}"

        lines = [f"## 🗺️ {result['city']} - \"{result['keyword']}\" 搜索结果 ({result['count']}条)"]
        for i, r in enumerate(result.get("results", []), 1):
            rating = f" ⭐{r['rating']}" if r.get("rating") else ""
            cost = f" ¥{r['cost']}" if r.get("cost") else ""
            lines.append(f"{i}. **{r['name']}**{rating}{cost} - {r['address']}")
        return '\n'.join(lines)


class AmapMapPlugin(BasePlugin):
    """高德地图综合插件 - 定位/逆地理编码/周边搜索/路线规划"""

    name = "amap_map"
    display_name = "🗺️ 高德地图"
    description = "GPS定位、逆地理编码、周边景点酒店搜索、驾车/步行/公交路线规划"
    version = "1.0.0"

    trigger_keywords = [
        "定位", "我在哪", "位置", "导航", "路线", "怎么走",
        "周边", "附近", "多远", "距离", "驾车", "步行",
        "公交", "地铁", "骑车", "酒店", "景点", "餐饮",
        "好吃", "好玩", "推荐", "我去", "去玩",
        "我在哪里", "离我", "旁边", "我这", "我附近",
    ]

    required_config = ["amap_key"]

    def __init__(self, config: dict = None):
        super().__init__(config)
        if config:
            self.amap_key = config.get("amap_key", "")

    def is_configured(self) -> bool:
        return bool(self.amap_key)

    def execute(self, params: dict) -> dict:
        action = params.get("action", "regeo")
        if not self.amap_key:
            return {"error": "请先配置高德地图 API Key"}

        if action == "regeo":
            return self._regeo(params)
        elif action == "around":
            return self._around(params)
        elif action == "direction":
            return self._direction(params)
        elif action == "ip_location":
            return self._ip_location()
        return {"error": f"未知操作: {action}"}

    def _regeo(self, params):
        """逆地理编码：坐标 → 地址"""
        lng = params.get("lng", "")
        lat = params.get("lat", "")
        if not lng or not lat:
            return {"error": "请提供经纬度 lng, lat"}
        try:
            resp = requests.get("https://restapi.amap.com/v3/geocode/regeo", params={
                "key": self.amap_key, "location": f"{lng},{lat}",
                "extensions": "all", "radius": 1000
            }, timeout=10)
            data = resp.json()
            if data.get("status") == "1":
                regeo = data.get("regeocode", {})
                addr = regeo.get("addressComponent", {})
                pois = regeo.get("pois", [])[:5]
                roads = regeo.get("roads", [])[:3]
                return {
                    "status": "ok",
                    "formatted_address": regeo.get("formatted_address", ""),
                    "province": addr.get("province", ""),
                    "city": addr.get("city", []) if isinstance(addr.get("city"), list) else addr.get("city", ""),
                    "district": addr.get("district", ""),
                    "township": addr.get("township", ""),
                    "street": addr.get("streetNumber", {}).get("street", ""),
                    "nearby_pois": [{"name": p.get("name", ""), "address": p.get("address", ""), "distance": p.get("distance", "")} for p in pois],
                    "nearby_roads": [{"name": r.get("name", ""), "distance": r.get("distance", "")} for r in roads],
                }
            return {"error": "逆地理编码失败"}
        except Exception as e:
            return {"error": str(e)}

    def _around(self, params):
        """周边搜索"""
        lng = params.get("lng", "")
        lat = params.get("lat", "")
        city = params.get("city", params.get("destination", ""))
        keywords = params.get("keywords", "景点|酒店|餐饮")
        radius = params.get("radius", 3000)

        if (not lng or not lat) and not city:
            return {"error": "请提供经纬度 lng, lat 或城市名 city"}

        try:
            base_url = "https://restapi.amap.com/v3/place/around" if (lng and lat) else "https://restapi.amap.com/v3/place/text"
            req_params = {
                "key": self.amap_key, "keywords": keywords,
                "offset": 15, "extensions": "all"
            }
            if lng and lat:
                req_params["location"] = f"{lng},{lat}"
                req_params["radius"] = radius
            else:
                req_params["city"] = city

            resp = requests.get(base_url, params=req_params, timeout=10)
            data = resp.json()

            if data.get("status") == "1":
                pois = data.get("pois", [])
                return {
                    "status": "ok", "count": data.get("count", "0"),
                    "results": [{
                        "name": p.get("name", ""),
                        "address": p.get("address", ""),
                        "location": p.get("location", ""),
                        "distance": p.get("distance", ""),
                        "type": p.get("type", ""),
                        "rating": p.get("biz_ext", {}).get("rating", "") if p.get("biz_ext") else "",
                        "cost": p.get("biz_ext", {}).get("cost", "") if p.get("biz_ext") else "",
                        "tel": p.get("tel", ""),
                    } for p in pois[:15]]
                }
            return {"error": "周边搜索失败"}
        except Exception as e:
            return {"error": str(e)}

    def _direction(self, params):
        """路线规划"""
        origin = params.get("origin", "")  # "lng,lat"
        destination = params.get("destination", "")
        mode = params.get("mode", "driving")  # driving/walking/transit/bicycling
        if not origin or not destination:
            return {"error": "请提供起点 origin 和终点 destination（格式: lng,lat）"}

        mode_map = {
            "driving": "https://restapi.amap.com/v3/direction/driving",
            "walking": "https://restapi.amap.com/v3/direction/walking",
            "transit": "https://restapi.amap.com/v3/direction/transit/integrated",
            "bicycling": "https://restapi.amap.com/v4/direction/bicycling",
        }
        url = mode_map.get(mode, mode_map["driving"])
        try:
            resp = requests.get(url, params={
                "key": self.amap_key, "origin": origin,
                "destination": destination, "extensions": "all"
            }, timeout=10)
            data = resp.json()
            if data.get("status") == "1":
                route = data.get("route", {})
                paths = route.get("paths", [])
                if paths:
                    p = paths[0]
                    steps = []
                    for s in p.get("steps", [])[:5]:
                        steps.append({
                            "instruction": s.get("instruction", ""),
                            "road": s.get("road", ""),
                            "distance": s.get("distance", ""),
                            "duration": s.get("duration", ""),
                        })
                    return {
                        "status": "ok",
                        "mode": mode,
                        "origin": route.get("origin", ""),
                        "destination": route.get("destination", ""),
                        "distance": p.get("distance", ""),
                        "duration": p.get("duration", ""),
                        "taxi_cost": data.get("route", {}).get("taxi_cost", ""),
                        "steps": steps,
                    }
            return {"error": f"路线规划失败: {data.get('info','')}"}
        except Exception as e:
            return {"error": str(e)}

    def _ip_location(self):
        """IP 定位"""
        try:
            resp = requests.get("https://restapi.amap.com/v3/ip", params={
                "key": self.amap_key
            }, timeout=10)
            data = resp.json()
            if data.get("status") == "1":
                return {
                    "status": "ok",
                    "province": data.get("province", ""),
                    "city": data.get("city", ""),
                    "adcode": data.get("adcode", ""),
                    "rectangle": data.get("rectangle", ""),
                }
            # 如果 Key 类型不对（JS API Key vs Web服务 Key），给出提示
            if data.get("infocode") == "10009":
                return {"error": "高德Key类型不匹配：请使用Web服务类型Key（非JS API Key）"}
            return {"error": f"IP定位失败: {data.get('info','')}"}
        except Exception as e:
            return {"error": str(e)}

    def format_for_llm(self, result: dict) -> str:
        if "error" in result:
            return f"[地图] {result.get('error')}"
        if "province" in result:
            # IP 定位结果
            return f"[📍 定位] {result.get('province','')} {result.get('city','')}"
        if "results" in result:
            lines = [f"## 🗺️ 周边实时搜索结果 ({result.get('count','?')}条)"]
            for i, r in enumerate(result.get('results', [])[:10], 1):
                dist = f" {r.get('distance','')}m" if r.get('distance') else ""
                rating = f" ⭐{r['rating']}" if r.get('rating') else ""
                lines.append(f"{i}. **{r.get('name','')}**{rating}{dist} - {r.get('address','')}")
            return '\n'.join(lines)
        if "distance" in result:
            # 路线结果
            return f"[🧭 路线] 距离{float(result['distance'])/1000:.1f}km | 约{int(result['duration'])/60:.0f}分钟"
        return ""  # 地图数据主要通过前端渲染
