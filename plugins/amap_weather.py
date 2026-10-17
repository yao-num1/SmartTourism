"""
高德天气插件 - 统一使用高德开放平台 Web 服务 API Key
接口: https://restapi.amap.com/v3/weather/weatherInfo
支持: 实况天气 (base) + 4天预报 (all)
"""

from plugins import BasePlugin
import requests
import json


class AmapWeatherPlugin(BasePlugin):
    name = "amap_weather"
    display_name = "🌤️ 高德天气"
    description = "通过高德开放平台 Web 服务 API Key 获取实况+预报天气"
    version = "1.0.0"

    trigger_keywords = [
        "天气", "气温", "下雨", "晴天", "阴天", "温度",
        "湿度", "风力", "带伞", "防晒", "降温", "升温",
        "天气怎么样", "会不会下雨", "穿什么衣服",
        "热不热", "冷不冷", "刮风", "下雪", "雾霾",
        "多少度", "紫外线", "雨季",
    ]

    required_config = ["amap_key"]

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.amap_key = (config or {}).get("amap_key", "")
        self.cache_ttl = 600

    def is_configured(self) -> bool:
        return bool(self.amap_key)

    def execute(self, params: dict) -> dict:
        city = params.get("city", params.get("destination", "北京"))
        extensions = params.get("extensions", "all")

        return self.get_cached_or_fetch(
            f"amap_weather:{city}:{extensions}",
            lambda: self._fetch(city, extensions),
            ttl=self.cache_ttl
        )

    def _fetch(self, city_name: str, extensions: str = "all") -> dict:
        """调用高德天气 API"""
        try:
            # 高德天气 API 需要 adcode，先用城市名尝试（自动兼容 adcode）
            url = "https://restapi.amap.com/v3/weather/weatherInfo"
            params = {
                "key": self.amap_key,
                "city": city_name,
                "extensions": extensions,
                "output": "JSON"
            }
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()

            if data.get("status") != "1" or data.get("infocode") != "10000":
                return {"error": f"高德天气查询失败: {data.get('info', '未知错误')}"}

            result = {
                "source": "高德开放平台 (lbs.amap.com)",
                "city": city_name,
            }

            # 实况天气
            lives = data.get("lives", [])
            if lives:
                live = lives[0]
                result["now"] = {
                    "province": live.get("province", ""),
                    "city": live.get("city", ""),
                    "weather": live.get("weather", ""),
                    "temperature": live.get("temperature", ""),
                    "winddirection": live.get("winddirection", ""),
                    "windpower": live.get("windpower", ""),
                    "humidity": live.get("humidity", ""),
                    "reporttime": live.get("reporttime", ""),
                }

            # 预报天气
            forecasts = data.get("forecasts", [])
            if forecasts:
                fc = forecasts[0]
                result["province"] = fc.get("province", "")
                result["city"] = fc.get("city", "")
                result["adcode"] = fc.get("adcode", "")
                result["reporttime"] = fc.get("reporttime", "")
                result["daily"] = []

                for cast in fc.get("casts", []):
                    result["daily"].append({
                        "date": cast.get("date", ""),
                        "week": cast.get("week", ""),
                        "weather_day": cast.get("dayweather", ""),
                        "weather_night": cast.get("nightweather", ""),
                        "high": cast.get("daytemp", "") + "°C",
                        "low": cast.get("nighttemp", "") + "°C",
                        "wind_day": cast.get("daywind", ""),
                        "wind_night": cast.get("nightwind", ""),
                        "wind_power_day": cast.get("daypower", ""),
                        "wind_power_night": cast.get("nightpower", ""),
                    })

            return result
        except Exception as e:
            return {"error": f"高德天气获取失败: {str(e)}"}

    def format_for_llm(self, result: dict) -> str:
        if "error" in result:
            return f"[天气数据暂时不可用: {result.get('error')}]"

        now = result.get("now", {})
        daily = result.get("daily", [])
        province = result.get("province", "") or now.get("province", "")
        city = result.get("city", "") or now.get("city", "")

        lines = [f"## 🌤️ {province} {city} 天气（高德）"]

        # 实况
        if now:
            lines.append(f"\n**当前实况** ({now.get('reporttime','')}):")
            lines.append(f"- 天气: **{now.get('weather','?')}**")
            lines.append(f"- 温度: **{now.get('temperature','?')}°C**")
            lines.append(f"- 湿度: {now.get('humidity','?')}%")
            wind_dir = now.get('winddirection', '')
            wind_pow = now.get('windpower', '')
            if wind_dir or wind_pow:
                lines.append(f"- 风向: {wind_dir} | 风力: {wind_pow}级")

        # 预报
        if daily:
            lines.append(f"\n**{len(daily)}天预报:**\n")
            lines.append("| 日期 | 星期 | 白天天气 | 夜间天气 | 高温 | 低温 | 白天风向 |")
            lines.append("|------|------|----------|----------|------|------|----------|")
            for d in daily:
                wd = d.get('weather_day', '')
                lines.append(
                    f"| {d.get('date','')} | {d.get('week','')} | {wd} | "
                    f"{d.get('weather_night','')} | {d.get('high','')} | {d.get('low','')} | "
                    f"{d.get('wind_day','')} {d.get('wind_power_day','')}级 |"
                )

        return '\n'.join(lines)
