"""
天气插件 v3 - 中国天气网官方 API (中国气象局数据, 免Key)
接口: d1.weather.com.cn/weather_index/{city_code}.html
数据: 实时天气 + 7天预报 + 20+项生活指数 (紫外线/穿衣/感冒/洗车/旅游/运动等)

城市编码参考: https://www.weather.com.cn/
北京 101010100, 上海 101020100, 广州 101280101 等
"""

import re
import json
import requests
from plugins import BasePlugin

# ====== 中国天气网城市编码库 (code → 城市名) ======
WEATHER_CITY_CODES = {
    "北京": "101010100", "海淀": "101010200", "朝阳": "101010300",
    "上海": "101020100", "浦东": "101020600",
    "天津": "101030100",
    "重庆": "101040100",
    "哈尔滨": "101050101", "长春": "101060101", "沈阳": "101070101",
    "呼和浩特": "101080101", "包头": "101080201",
    "石家庄": "101090101", "唐山": "101090501",
    "太原": "101100101", "大同": "101100201",
    "西安": "101110101", "咸阳": "101110200",
    "济南": "101120101", "青岛": "101120201", "烟台": "101120501",
    "乌鲁木齐": "101130101",
    "拉萨": "101140101",
    "西宁": "101150101",
    "兰州": "101160101",
    "银川": "101170101",
    "郑州": "101180101", "洛阳": "101180901", "开封": "101180801",
    "南京": "101190101", "苏州": "101190401", "无锡": "101190201",
    "常州": "101191101", "扬州": "101190601", "徐州": "101190801",
    "南通": "101190501", "镇江": "101190301",
    "武汉": "101200101", "宜昌": "101200901", "襄阳": "101200201",
    "杭州": "101210101", "宁波": "101210401", "温州": "101210701",
    "绍兴": "101210501", "嘉兴": "101210301", "湖州": "101210201",
    "金华": "101210901", "台州": "101210601",
    "合肥": "101220101", "芜湖": "101220301",
    "福州": "101230101", "厦门": "101230201", "泉州": "101230501",
    "南昌": "101240101", "九江": "101240201", "景德镇": "101240801",
    "长沙": "101250101", "张家界": "101251101", "岳阳": "101251001",
    "贵阳": "101260101", "遵义": "101260201",
    "成都": "101270101", "绵阳": "101270401", "乐山": "101271401",
    "广州": "101280101", "深圳": "101280601", "珠海": "101280701",
    "东莞": "101281601", "佛山": "101280801", "惠州": "101280301",
    "昆明": "101290101", "丽江": "101291401", "大理": "101290201",
    "西双版纳": "101291601", "香格里拉": "101291301",
    "南宁": "101300101", "桂林": "101300501", "北海": "101301301",
    "海口": "101310101", "三亚": "101310201",
    "香港": "101320101", "澳门": "101330101",
    "台北": "101340101",
    # 其他常用城市继续补充...
    "秦皇岛": "101091101", "承德": "101090402",
    "大连": "101070201", "鞍山": "101070301",
    "延边": "101060301", "吉林": "101060201",
    "齐齐哈尔": "101050201", "牡丹江": "101050301",
    "无锡": "101190201", "连云港": "101191001",
    "温州": "101210701", "台州": "101210601", "衢州": "101211001",
    "黄山": "101221001", "安庆": "101220601",
    "赣州": "101240701", "上饶": "101240301",
    "威海": "101121301", "日照": "101121501",
    "洛阳": "101180901", "安阳": "101180201",
    "宜昌": "101200901", "荆州": "101200801",
    "岳阳": "101251001", "衡阳": "101250401",
    "汕头": "101280501", "湛江": "101281001",
    "三亚": "101310201", "琼海": "101310202",
    "贵阳": "101260101", "黔东南": "101260501",
    "林芝": "101140401", "日喀则": "101140201",
    "吐鲁番": "101130501", "喀什": "101130901",
    "阿拉善": "101080101", "呼伦贝尔": "101081001",
    "嘉峪关": "101161401", "张掖": "101161301",
    "天水": "101160901", "延安": "101110300",
    "伊犁": "101131001", "阿勒泰": "101131401",
}

# 天气代码映射
WEATHER_CODES = {
    "00": "晴", "01": "多云", "02": "阴", "03": "阵雨",
    "04": "雷阵雨", "05": "雷阵雨伴有冰雹", "06": "雨夹雪", "07": "小雨",
    "08": "中雨", "09": "大雨", "10": "暴雨", "11": "大暴雨",
    "12": "特大暴雨", "13": "阵雪", "14": "小雪", "15": "中雪",
    "16": "大雪", "17": "暴雪", "18": "雾", "19": "冻雨",
    "20": "沙尘暴", "21": "小到中雨", "22": "中到大雨", "23": "大到暴雨",
    "24": "暴雨到大暴雨", "25": "大暴雨到特大暴雨",
    "26": "小到中雪", "27": "中到大雪", "28": "大到暴雪",
    "29": "浮尘", "30": "扬沙", "31": "强沙尘暴", "53": "霾", "99": "无",
}

WIND_DIRS = {"0": "无持续风向", "1": "东北风", "2": "东风", "3": "东南风",
             "4": "南风", "5": "西南风", "6": "西风", "7": "西北风",
             "8": "北风", "9": "旋转风"}


def find_city_code(city_name: str) -> str:
    """根据城市名查找天气网编码"""
    if city_name in WEATHER_CITY_CODES:
        return WEATHER_CITY_CODES[city_name]
    # 模糊匹配
    for key, code in WEATHER_CITY_CODES.items():
        if city_name in key or key in city_name:
            return code
    # 默认北京
    return "101010100"


def parse_code(code: str) -> str:
    """天气代码 → 中文描述"""
    return WEATHER_CODES.get(code, code)


class WeatherPlugin(BasePlugin):
    name = "weather"
    display_name = "🌤️ 心知天气"
    description = "心知天气优先，fallback到中国天气网免Key"
    version = "3.1.0"

    trigger_keywords = [
        "天气", "气温", "下雨", "晴天", "阴天", "温度",
        "湿度", "风力", "紫外线", "空气质量", "穿衣",
        "带伞", "防晒", "降温", "升温", "雨季",
        "天气怎么样", "会不会下雨", "穿什么衣服",
        "weather", "temperature", "rain", "多少度",
        "热不热", "冷不冷", "刮风", "下雪", "雾霾",
    ]

    required_config = []  # 心知Key可选，无Key时自动fallback到中国天气网

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.cache_ttl = 600  # 缓存 10 分钟
        # 从配置中读取心知 Key（公钥或私钥）
        self.seniverse_public_key = (config or {}).get('seniverse_public_key', '')
        self.seniverse_key = (config or {}).get('seniverse_key', '')

    def is_configured(self) -> bool:
        return bool(self.seniverse_public_key or self.seniverse_key)

    def execute(self, params: dict) -> dict:
        city = params.get("city", params.get("destination", "北京"))
        
        # 优先尝试心知天气
        if self.seniverse_public_key or self.seniverse_key:
            result = self._fetch_seniverse(city)
            if result and "error" not in result:
                return result
        
        # Fallback: 中国天气网
        code = find_city_code(city)
        return self.get_cached_or_fetch(
            f"weathercn:{code}",
            lambda: self._fetch_weather_cn(code, city),
            ttl=self.cache_ttl
        )

    def _fetch_seniverse(self, city: str) -> dict:
        """从心知天气获取数据（支持公钥/私钥）"""
        try:
            key = self.seniverse_key or self.seniverse_public_key
            # 心知天气 v4 实时天气 API
            url = f"https://api.seniverse.com/v4/weather/now.json"
            params = {
                "key": key,
                "location": city,
                "language": "zh-Hans",
                "unit": "c"
            }
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()
            
            if data.get("status_code") and data["status_code"] != 0:
                # 公钥可能不支持 v4，尝试 v3
                return self._fetch_seniverse_v3(city, key)
            
            results = data.get("results", [])
            if not results:
                return {"error": f"未找到城市: {city}"}
            
            r = results[0]
            loc = r.get("location", {})
            now = r.get("now", {})
            
            return {
                "source": f"心知天气 (seniverse.com)",
                "city": loc.get("name", city),
                "code": loc.get("id", ""),
                "now": {
                    "temperature": now.get("temperature", ""),
                    "weather": now.get("text", ""),
                    "weather_code": now.get("code", ""),
                    "humidity": now.get("humidity", ""),
                    "wind_direction": now.get("wind_direction", ""),
                    "wind_speed": now.get("wind_speed", ""),
                    "wind_scale": now.get("wind_scale", ""),
                    "feels_like": now.get("feels_like", ""),
                    "visibility": now.get("visibility", ""),
                    "pressure": now.get("pressure", ""),
                    "time": now.get("last_update", ""),
                },
                "daily": self._fetch_seniverse_daily(city, key),
                "life": {},
            }
        except Exception as e:
            return {"error": f"心知天气获取失败: {str(e)}"}

    def _fetch_seniverse_v3(self, city: str, key: str) -> dict:
        """心知天气 v3 API（公钥友好）"""
        try:
            url = f"https://api.seniverse.com/v3/weather/now.json"
            params = {
                "key": key,
                "location": city,
                "language": "zh-Hans",
                "unit": "c"
            }
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()
            
            if "error" in data:
                return {"error": data.get("error", "API错误")}
            
            results = data.get("results", [])
            if not results:
                return {"error": f"未找到城市: {city}"}
            
            r = results[0]
            loc = r.get("location", {})
            now = r.get("now", {})
            
            return {
                "source": f"心知天气 v3 (seniverse.com)",
                "city": loc.get("name", city),
                "code": loc.get("id", ""),
                "now": {
                    "temperature": now.get("temperature", ""),
                    "weather": now.get("text", ""),
                    "weather_code": now.get("code", ""),
                    "time": now.get("last_update", ""),
                },
                "daily": [],
                "life": {},
            }
        except Exception as e:
            return {"error": f"心知天气 v3 获取失败: {str(e)}"}

    def _fetch_seniverse_daily(self, city: str, key: str) -> list:
        """心知天气 3 天预报"""
        try:
            url = f"https://api.seniverse.com/v4/weather/daily.json"
            params = {
                "key": key,
                "location": city,
                "language": "zh-Hans",
                "unit": "c",
                "start": 0,
                "days": 3
            }
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()
            
            if data.get("status_code") and data["status_code"] != 0:
                return []
            
            results = data.get("results", [])
            if not results:
                return []
            
            daily_list = results[0].get("daily", [])
            return [{
                "date": d.get("date", "")[5:],  # MM-DD
                "day_name": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][0] if d.get("date") else "",
                "weather_day": d.get("text_day", ""),
                "weather_night": d.get("text_night", ""),
                "high": d.get("high", ""),
                "low": d.get("low", ""),
                "wind_day": d.get("wind_direction", ""),
                "wind_speed": d.get("wind_speed", ""),
            } for d in daily_list]
        except Exception:
            return []

    def _fetch_weather_cn(self, code: str, city: str) -> dict:
        """从中国天气网获取数据"""
        try:
            url = f"http://d1.weather.com.cn/weather_index/{code}.html"
            headers = {
                "Referer": "https://www.weather.com.cn/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            resp = requests.get(url, headers=headers, timeout=10)
            resp.encoding = 'utf-8'
            text = resp.text

            if not text or "dataSK" not in text:
                return {"error": f"无法获取{city}({code})天气数据"}

            # 提取 JS 变量
            sk = self._extract_js_var(text, "dataSK")
            fc_data = self._extract_js_var(text, "fc")
            zs = self._extract_js_var(text, "dataZS")
            city_dz = self._extract_js_var(text, "cityDZ")

            # 解析实时天气
            now = {}
            if sk:
                now = {
                    "temperature": sk.get("temp", ""),
                    "feels_like": sk.get("tempf", ""),
                    "weather": sk.get("weather", ""),
                    "wind_direction": sk.get("WD", ""),
                    "wind_speed": sk.get("WS", ""),
                    "humidity": sk.get("SD", ""),
                    "pressure": sk.get("qy", ""),
                    "visibility": sk.get("njd", ""),
                    "aqi": sk.get("aqi", ""),
                    "aqi_pm25": sk.get("aqi_pm25", ""),
                    "rain": sk.get("rain", "0"),
                    "time": sk.get("time", ""),
                }

            # 解析7天预报
            daily = []
            if fc_data and "f" in fc_data:
                for f in fc_data["f"]:
                    daily.append({
                        "date": f.get("fi", ""),
                        "day_name": f.get("fj", ""),
                        "weather_day": parse_code(f.get("fa", "")),
                        "weather_night": parse_code(f.get("fb", "")),
                        "high": f.get("fc", ""),
                        "low": f.get("fd", ""),
                        "wind_day": WIND_DIRS.get(f.get("fe", ""), f.get("fe", "")),
                        "wind_night": WIND_DIRS.get(f.get("ff", ""), f.get("ff", "")),
                        "wind_speed": f.get("fg", ""),
                    })

            # 解析生活指数
            life = {}
            if zs and "zs" in zs:
                z = zs["zs"]
                # dataZS 中的字段名是拼音缩写
                life_map = {
                    "穿衣": ("ct_name", "ct_hint", "ct_des_s"),
                    "紫外线": ("uv_name", "uv_hint", "uv_des_s"),
                    "感冒": ("gm_name", "gm_hint", "gm_des_s"),
                    "运动": ("yd_name", "yd_hint", "yd_des_s"),
                    "旅游": ("tr_name", "tr_hint", "tr_des_s"),
                    "洗车": ("xc_name", "xc_hint", "xc_des_s"),
                    "防晒": ("fs_name", "fs_hint", "fs_des_s"),
                    "雨伞": ("ys_name", "ys_hint", "ys_des_s"),
                    "舒适度": ("co_name", "co_hint", "co_des_s"),
                    "晾晒": ("ls_name", "ls_hint", "ls_des_s"),
                    "交通": ("jt_name", "jt_hint", "jt_des_s"),
                }
                for label, (name_k, hint_k, des_k) in life_map.items():
                    life[label] = {
                        "name": z.get(name_k, ""),
                        "hint": z.get(hint_k, ""),
                        "desc": z.get(des_k, ""),
                    }

            # 城市信息
            city_name = city
            if city_dz and "weatherinfo" in city_dz:
                city_name = city_dz["weatherinfo"].get("city", city)

            return {
                "source": "中国天气网 (weather.com.cn) · 中国气象局数据",
                "city": city_name,
                "code": code,
                "now": now,
                "daily": daily,
                "life": life,
            }

        except Exception as e:
            return {"error": f"获取天气失败: {str(e)}"}

    def _extract_js_var(self, text: str, var_name: str) -> dict:
        """从 JS 文本中提取 var xxx = {...}; 的对象（支持嵌套花括号）"""
        idx = text.find(f'var {var_name} =')
        if idx == -1:
            return None
        # 找第一个 {
        start = text.find('{', idx)
        if start == -1:
            return None
        # 括号深度匹配
        depth = 0
        end = start
        for i in range(start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            return None

    def _life_item(self, zs: dict, key: str) -> dict:
        """解析生活指数项"""
        name_key = f"{key}_name"
        hint_key = f"{key}_hint"
        des_key = f"{key}_des_s"
        return {
            "name": zs.get(name_key, ""),
            "hint": zs.get(hint_key, ""),
            "desc": zs.get(des_key, ""),
        }

    def format_for_llm(self, result: dict) -> str:
        if "error" in result:
            return f"[天气数据暂时不可用: {result.get('error')}]"

        city = result.get("city", "")
        src = result.get("source", "")
        now = result.get("now", {})
        daily = result.get("daily", [])
        life = result.get("life", {})

        lines = [f"## 🌤️ {city} 实时天气", f"*数据来源: {src}*"]

        # 实时
        if now:
            lines.append(f"\n**当前实况** ({now.get('time','')}):")
            lines.append(f"- 天气: **{now.get('weather','?')}**")
            
            temp = now.get('temperature', '?')
            feels = now.get('feels_like', '')
            temp_str = f"温度: **{temp}°C**"
            if feels:
                temp_str += f" | 体感: {feels}°C"
            lines.append(f"- {temp_str}")
            
            humidity = now.get('humidity', '')
            pressure = now.get('pressure', '')
            if humidity or pressure:
                parts = []
                if humidity: parts.append(f"湿度: {humidity}")
                if pressure: parts.append(f"气压: {pressure}hPa")
                lines.append(f"- {' | '.join(parts)}")
            
            wind = now.get('wind_direction', '') or ''
            wind_s = now.get('wind_speed', '') or now.get('wind_scale', '') or ''
            if wind or wind_s:
                lines.append(f"- 风向: {wind} | 风力: {wind_s}")
            
            vis = now.get('visibility', '')
            if vis and vis != '?': lines.append(f"- 能见度: {vis}")
            aqi = now.get('aqi', '')
            if aqi: lines.append(f"- AQI: {aqi}")
            rain = now.get('rain', '')
            if rain and rain != '0': lines.append(f"- ⚠️ 降水量: {rain}mm")

        # 预报
        if daily:
            lines.append(f"\n**{len(daily)}天预报:**\n")
            lines.append("| 日期 | 天气 | 高温 | 低温 | 风向 |")
            lines.append("|------|------|------|------|------|")
            for d in daily:
                w = d.get("weather_day", "")
                if d.get("weather_night") and d["weather_night"] != w:
                    w += "转" + d["weather_night"]
                lines.append(f"| {d.get('date','')} | {w} | {d.get('high','')}°C | {d.get('low','')}°C | {d.get('wind_day','')} |")

        # 生活指数（仅中国天气网有）
        if life:
            lines.append(f"\n**生活建议:**")
            for key, item in life.items():
                if item.get("hint"):
                    lines.append(f"- **{key}**: {item['hint']} - {item.get('desc','')}")

        return '\n'.join(lines)
