"""
出行费用查询插件 - 实时查询景区票价与出行交通费用

能力：
1. 景区票价：本地知识库（真实票价）+ 高德 POI biz_ext.cost（实时，覆盖更广）双源
2. 出行费用：高德路径规划 API
   - driving  → 打车费（taxi_cost）
   - transit  → 公交/地铁票价（transits[].cost）
   - walking  → 免费（距离/时长）
   - bicycling → 免费

接入方式与其它插件一致：继承 BasePlugin，注册到 registry，由 get_plugin_data
根据 trigger_keywords 自动触发，结果经 format_for_llm 喂给 LLM 智能体。
"""

import re

import requests

try:
    from . import BasePlugin
except ImportError:  # 直接运行时
    from plugins import BasePlugin


# 本地知识库票价兜底（与 travel_data.LOCAL_KNOWLEDGE 保持一致口径，避免重复依赖）
_TICKET_FALLBACK = {
    "杭州": [("西湖风景名胜区", "免费（三潭印月船票55元、雷峰塔40元）"),
             ("灵隐寺+飞来峰", "飞来峰45元 + 灵隐寺30元"),
             ("宋城", "320元（含演出）"),
             ("西溪国家湿地公园", "80元")],
    "三亚": [("蜈支洲岛", "144元（含船票）"),
             ("南山文化旅游区", "129元"),
             ("天涯海角", "81元"),
             ("亚龙湾热带天堂森林公园", "158元")],
    "成都": [("大熊猫繁育研究基地", "55元"),
             ("都江堰", "80元"),
             ("宽窄巷子", "免费"),
             ("锦里古街", "免费")],
    "北京": [("故宫博物院", "旺季60元 / 淡季40元"),
             ("八达岭长城", "旺季40元 / 淡季35元"),
             ("颐和园", "旺季30元 / 淡季20元")],
    "大理": [("苍山", "索道280元（全程）"),
             ("洱海生态廊道", "免费（租车30-50元）"),
             ("大理古城", "免费")],
}

# 打车起步价兜底（高德未返回 taxi_cost 时用于估算参考，单位：元/3km）
_TAXI_FALLBACK = {"杭州": 13, "北京": 13, "成都": 9, "三亚": 10, "大理": 10}


class TravelCostPlugin(BasePlugin):
    """出行费用查询插件（票价 + 打车/公交/地铁/步行费用）"""

    name = "travel_cost"
    display_name = "💰 出行费用查询"
    description = "实时查询景区门票价格与出行交通费用（打车/公交/地铁/步行）"
    version = "1.0.0"

    trigger_keywords = [
        "票价", "门票多少钱", "门票价格", "门票", "多少钱", "费用", "花费", "开销",
        "打车", "打车费", "打的", "出租", "滴滴", "公交", "地铁", "车费", "路费",
        "预算", "贵不贵", "划算", "划算吗", "怎么收费", "收费",
    ]

    required_config = ["amap_key"]

    def __init__(self, config: dict = None):
        super().__init__(config)
        if config:
            self.amap_key = config.get("amap_key", "")

    def is_configured(self) -> bool:
        # 票价查询有本地知识库兜底，即便无高德 Key 也能工作；交通费需高德 Key
        return True

    def should_trigger(self, user_query: str) -> float:
        q = user_query.lower()
        hits = sum(1 for kw in self.trigger_keywords if kw in q)
        if hits == 0:
            return 0.0
        return min(0.55 + 0.12 * hits, 1.0)

    def execute(self, params: dict) -> dict:
        action = params.get("action") or self._infer_action(params)
        if action == "route":
            return self._route_cost(params)
        return self._ticket_price(params)

    def _infer_action(self, params: dict) -> str:
        q = (params.get("query") or "").lower()
        if any(k in q for k in ("打车", "公交", "地铁", "车费", "路费", "从", "到", "怎么去")):
            return "route"
        return "ticket"

    # ---------- 景区票价 ----------
    def _ticket_price(self, params: dict) -> dict:
        city = (params.get("city") or params.get("destination") or "").strip()
        spot = (params.get("spot") or params.get("keyword") or "").strip()

        # 1) 本地知识库（真实票价，快且稳）
        local_spots = []
        for key, items in _TICKET_FALLBACK.items():
            if key in city or city in key or (spot and (key in spot or spot in key)):
                local_spots = [{"name": n, "price": p, "source": "本地知识库"} for n, p in items]
                if key in city or city in key:
                    break

        # 2) 高德 POI 实时票价（biz_ext.cost），覆盖更广的城市/景点
        amap_spots = self._amap_ticket(city or spot, spot)
        merged = {}
        for s in local_spots + amap_spots:
            merged.setdefault(s["name"], s)

        if not merged:
            return {"status": "not_found", "city": city,
                    "message": f"暂未查到「{city or spot}」的门票价格，可尝试更具体的目的地"}

        return {"status": "ok", "city": city or spot, "spots": list(merged.values())[:8]}

    def _amap_ticket(self, city: str, spot: str) -> list:
        if not self.amap_key:
            return []
        try:
            resp = requests.get("https://restapi.amap.com/v3/place/text", params={
                "key": self.amap_key,
                "keywords": spot or (city + " 景区"),
                "city": city or "",
                "types": "110000",  # 风景名胜
                "offset": 8,
                "extensions": "all",
            }, timeout=10)
            data = resp.json()
            if data.get("status") != "1":
                return []
            spots = []
            for p in data.get("pois", []):
                cost = (p.get("biz_ext") or {}).get("cost", "")
                name = p.get("name", "")
                if cost and name:
                    spots.append({"name": name, "price": f"{cost}元", "source": "高德POI"})
            return spots
        except Exception:
            return []

    # ---------- 出行交通费用 ----------
    def _route_cost(self, params: dict) -> dict:
        origin = (params.get("origin") or "").strip()
        destination = (params.get("destination") or "").strip()
        city = (params.get("city") or "").strip()
        mode = params.get("mode", "driving")

        if not self.amap_key:
            return {"status": "not_configured",
                    "message": "查询交通费用需要配置高德 Web 服务 Key（免费申请）"}

        # 地名 → 坐标
        if origin and re.match(r'^\d+\.?\d*,\d+\.?\d*$', origin):
            origin_ll = origin
        else:
            origin_ll = self._geocode(origin, city) if origin else ""
        if destination and re.match(r'^\d+\.?\d*,\d+\.?\d*$', destination):
            dest_ll = destination
        else:
            dest_ll = self._geocode(destination, city) if destination else ""

        if not origin_ll or not dest_ll:
            return {"status": "error",
                    "message": f"无法定位起终点（起点「{origin}」终点「{destination}」），请提供更明确的地名或坐标"}

        result = self._direction(origin_ll, dest_ll, mode)
        if result.get("status") != "ok":
            return result

        return {"status": "ok", "origin": origin, "destination": destination,
                "mode": mode, **result}

    def _geocode(self, place: str, city: str = "") -> str:
        try:
            params = {"key": self.amap_key, "address": place}
            if city:
                params["city"] = city
            resp = requests.get("https://restapi.amap.com/v3/geocode/geo",
                                params=params, timeout=10)
            data = resp.json()
            geocodes = data.get("geocodes") or []
            if data.get("status") == "1" and geocodes:
                return geocodes[0].get("location", "")
            return ""
        except Exception:
            return ""

    def _direction(self, origin_ll: str, dest_ll: str, mode: str) -> dict:
        mode_map = {
            "driving": "https://restapi.amap.com/v3/direction/driving",
            "walking": "https://restapi.amap.com/v3/direction/walking",
            "transit": "https://restapi.amap.com/v3/direction/transit/integrated",
            "bicycling": "https://restapi.amap.com/v4/direction/bicycling",
        }
        url = mode_map.get(mode, mode_map["driving"])
        try:
            resp = requests.get(url, params={
                "key": self.amap_key, "origin": origin_ll,
                "destination": dest_ll, "extensions": "all",
            }, timeout=10)
            data = resp.json()
            if data.get("status") != "1":
                return {"status": "error", "message": f"路线规划失败: {data.get('info', '')}"}
            route = data.get("route", {})

            # 公交/地铁：数据在 route.transits[]（含票价 cost）
            if mode == "transit":
                transits = route.get("transits") or []
                if not transits:
                    return {"status": "error", "message": "未获取到公交/地铁路线"}
                t = transits[0]
                dist = round(int(t.get("distance", 0)) / 1000, 1)
                dur = round(int(t.get("duration", 0)) / 60, 0)
                cost = t.get("cost", "")
                return {"status": "ok", "distance": dist, "duration": dur,
                        "transit_cost": cost}

            # 驾车/步行/骑行：数据在 route.paths[]
            paths = route.get("paths") or []
            if not paths:
                return {"status": "error", "message": "未获取到路线"}
            p = paths[0]
            distance_km = round(int(p.get("distance", 0)) / 1000, 1)
            duration_min = round(int(p.get("duration", 0)) / 60, 0)

            if mode == "driving":
                taxi_cost = route.get("taxi_cost", "")
                return {"status": "ok", "distance": distance_km,
                        "duration": duration_min, "taxi_cost": taxi_cost or ""}
            # walking / bicycling 免费
            return {"status": "ok", "distance": distance_km,
                    "duration": duration_min, "cost": 0}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def format_for_llm(self, result: dict) -> str:
        if result.get("status") == "not_found":
            return f"[💰 费用查询] {result.get('message', '')}"
        if result.get("status") == "error":
            return f"[💰 费用查询] {result.get('message', '')}"
        if result.get("status") == "not_configured":
            return f"[💰 费用查询] {result.get('message', '')}"

        # 票价结果
        if "spots" in result:
            lines = [f"## 💰 景区门票价格（{result.get('city', '')}）"]
            for s in result["spots"]:
                lines.append(f"- **{s['name']}**：{s['price']}（{s['source']}）")
            lines.append("（票价以景区官方/在线平台实时为准，旺季或有浮动）")
            return '\n'.join(lines)

        # 交通费用结果
        if "distance" in result:
            mode_cn = {"driving": "打车/驾车", "transit": "公交/地铁", "walking": "步行", "bicycling": "骑行"}.get(result.get("mode"), result.get("mode"))
            lines = [f"## 💰 出行费用（{result.get('origin', '起点')} → {result.get('destination', '终点')}，{mode_cn}）"]
            lines.append(f"- 距离：约 **{result['distance']} km**，时长：约 **{result['duration']} 分钟**")
            if result.get("taxi_cost"):
                lines.append(f"- 打车费用：约 **{result['taxi_cost']} 元**（高德估算，实际以计费为准）")
            if result.get("transit_cost") is not None and result.get("transit_cost") != "":
                lines.append(f"- 公交/地铁票价：约 **{result['transit_cost']} 元**")
            if result.get("cost") == 0:
                lines.append("- 费用：**免费**")
            lines.append("（费用为高德实时估算，实际以平台计费为准）")
            return '\n'.join(lines)

        return "[💰 费用查询] 无结果"
