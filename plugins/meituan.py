"""
美团技术服务合作中心 - 酒旅 Skill 插件

通过美团官方 AI Hub 提供的自然语言查询接口，获取美食、酒店、景点、
门票、行程规划、火车票/机票等酒旅数据。

接入文档: https://developer.meituan.com/zh/v2/dev/doc （AI Hub 文档中心）
Skill 管理: https://developer.meituan.com/zh/v2/dev/aiHub/skillManage
请求格式与官方 ht-ai Skill 包（@meituan-travel/ht-ai）逐字段对齐：
  POST https://mcp-open-cater.meituan.com/v1/api/voyage/openapi/query
  body: {"city": ..., "query": ..., "originQuery": ...可选..., "channel": "default"}
  header: Authorization: <token>（裸 Token，不加 Bearer），超时 120s
"""

import os
import re
import threading
import time
from urllib.parse import quote

import requests

try:
    from . import BasePlugin
except ImportError:  # 直接运行时
    from plugins import BasePlugin


# ====== 结果缓存：TTL 命中 + 接口失败时回退旧数据（stale-if-error）======
# 美团单次查询实测 11-44s，是整条对话链路里最慢的一环；同城同问在几分钟内结果几乎不变，
# 没必要每个请求都干等一次。TTL 内的命中直接当实时数据用；接口超时/繁忙/报错时回退到
# 最近一次成功结果——但必须在提示词里标明「这是多久之前的数据」，宁可给用户一份
# 标注了时间的旧票价，也不能让模型把旧价当实时价报出去。
CACHE_TTL = int(os.getenv("MEITUAN_CACHE_TTL", "300"))                # 5 分钟内算「实时」
CACHE_STALE_MAX = int(os.getenv("MEITUAN_CACHE_STALE_MAX", "86400"))  # 接口挂了最多回退到 24h 前
CACHE_MAX_ENTRIES = int(os.getenv("MEITUAN_CACHE_MAX_ENTRIES", "200"))


def _fmt_age(seconds: int) -> str:
    """把秒数说成人话，用于提示词与降级日志。"""
    s = max(0, int(seconds or 0))
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分钟"
    if s < 86400:
        return f"{s // 3600} 小时"
    return f"{s // 86400} 天"


# 结果中的图片链接（非预订入口）
_IMG_URL_RE = re.compile(r'(meituan\.net|dpfile\.com|meituan\.com/img).*(jpg|jpeg|png|webp)', re.I)
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^)\s]+)\)')

_TICKET_WORDS = ["门票", "景区", "景点", "演出", "游船", "乐园", "入园", "票务", "观光", "索道"]
_HOTEL_WORDS = ["酒店", "住宿", "民宿", "客栈", "宾馆", "房型", "度假村", "预订", "入住"]


def _clean_title(text: str) -> str:
    text = re.sub(r'\*+', '', text)
    text = text.replace('\\(', '(').replace('\\)', ')')
    return text.strip()


_PRICE_RE = re.compile(r'[¥￥]\s*(\d{1,5})(?:\s*[-~—]\s*(\d{1,5}))?|(\d{1,5})\s*元(?:\s*[-~—]\s*(\d{1,5})\s*元)?')
# 美团对价格脱敏显示为 ￥1XX起/晚，需连 X 一起捕获
_PRICE_LINE_RES = [
    re.compile(r'[¥￥]\s*([0-9][0-9Xx]{0,4})\s*起'),
    re.compile(r'[¥￥]\s*([0-9][0-9Xx]{0,4})'),
    re.compile(r'([0-9]{1,5})\s*元'),
]
# 房型链接（如"观景双床房"）：不是独立酒店/门票入口，不应生成预订卡片
_ROOM_TITLE_RE = re.compile(r'(房|床|间|套)\]?$')
# 美团过载/限流时的兜底文案特征。注意："排队/休息一下/稍后再试"这类词在正常答复里
# 也会出现（如"避免现场排队""午间回酒店休息一下"），不能作 busy 判据，
# 否则会把含真实票价/房价的合法数据误丢弃（2026-09-24 实测踩坑）
_BUSY_RE = re.compile(r'(忙不过来|系统繁忙|第一时间回复)')


def _extract_price_near(answer: str, title: str, url: str = None):
    """在链接所在行内提取价格（¥xx / ￥1XX起 / xx元），返回如 "90"、"1XX"、"120-180"，无则 None。

    只扫描链接所在行，避免把评分/距离等无关数字误当价格。
    """
    if not answer:
        return None
    line = ""
    if url:
        for ln in answer.split("\n"):
            if url in ln:
                line = ln
                break
    if not line and title:
        idx = answer.find(title)
        if idx >= 0:
            line = answer[idx: answer.find("\n", idx) if answer.find("\n", idx) > 0 else len(answer)]
    if not line:
        return None
    # 优先匹配美团脱敏价（￥1XX起），再匹配区间价（¥120-180），最后才是 xx元
    for rx in _PRICE_LINE_RES[:2]:
        m = rx.search(line)
        if m:
            return m.group(1)
    m = _PRICE_RE.search(line)
    if m and m.group(1):
        return m.group(1) if not m.group(2) else "%s-%s" % (m.group(1), m.group(2))
    m = _PRICE_LINE_RES[2].search(line)
    if m:
        return m.group(1)
    return None


class MeituanTravelPlugin(BasePlugin):
    """美团酒旅自然语言查询插件（美食/酒店/景点/行程/票务）"""

    name = "meituan"
    display_name = "🍜 美团酒旅"
    description = "美团官方酒旅数据：美食餐厅、酒店、景点门票、行程规划、火车票机票"
    version = "1.4.0"

    trigger_keywords = [
        "美团", "美食", "好吃的", "餐厅", "必吃", "小吃", "特色菜",
        "酒店", "住宿", "民宿", "景点", "门票", "景区",
        "行程", "攻略", "规划", "一日游", "几日游",
        "火车票", "机票", "高铁", "动车",
    ]

    required_config = ["meituan_token"]

    def __init__(self, config: dict = None):
        super().__init__(config)
        if config:
            self.token = config.get("meituan_token", "")
        # 插件在注册中心是单例、waitress 8 线程共用，缓存读写必须加锁
        self._result_cache = {}
        self._cache_lock = threading.Lock()

    # ==================== 结果缓存 ====================

    @staticmethod
    def _cache_key(city: str, query: str, channel: str) -> str:
        """origin_query 不进 key：它只是用户原话（供统计与预订卡片判意图），不改变美团返回内容，
        进 key 会让每条措辞不同的提问都 miss，缓存等于白做。"""
        return "%s|%s|%s" % (city, channel, query)

    def _cache_get(self, key: str):
        """返回 (answer, 距上次抓取的秒数)；无记录返回 None。"""
        with self._cache_lock:
            item = self._result_cache.get(key)
            if not item:
                return None
            return item["answer"], time.time() - item["ts"]

    def _cache_put(self, key: str, answer):
        with self._cache_lock:
            self._result_cache[key] = {"answer": answer, "ts": time.time()}
            if len(self._result_cache) > CACHE_MAX_ENTRIES:
                oldest = min(self._result_cache, key=lambda k: self._result_cache[k]["ts"])
                self._result_cache.pop(oldest, None)

    def cache_size(self) -> int:
        with self._cache_lock:
            return len(self._result_cache)

    def _from_cache(self, answer, city, query, origin_query, age, stale, reason=""):
        """用缓存原文拼一个与实时成功路径同构的结果。

        bookings 每次重算而不是跟着缓存：它的 type 判定要看 origin_query 的意图，
        而 origin_query 不在缓存 key 里。
        """
        out = {
            "status": "ok", "source": "美团酒旅", "city": city, "answer": answer,
            "bookings": self.extract_bookings(answer, query, city, origin_query),
            "cached": True, "stale": bool(stale), "age_seconds": int(age),
        }
        if reason:
            out["degraded_reason"] = reason
        return out

    def is_configured(self) -> bool:
        return bool(getattr(self, "token", ""))

    def should_trigger(self, user_query: str) -> float:
        """命中任一关键词即触发（美团接口本身是自然语言后端，覆盖面广）"""
        q = user_query.lower()
        hits = sum(1 for kw in self.trigger_keywords if kw in q)
        if hits == 0:
            return 0.0
        return min(0.5 + 0.1 * hits, 1.0)

    def execute(self, params: dict) -> dict:
        if not getattr(self, "token", ""):
            return {"status": "not_configured", "message": "请配置美团 Token（开发者控制台创建）"}

        query = (params.get("query") or "").strip()
        if not query:
            return {"status": "error", "message": "请提供自然语言查询内容 query"}
        city = (params.get("city") or params.get("destination") or "").strip() or "北京"
        # 官方 Skill 规范：用户原始查询通过独立字段 originQuery 上报（仅用于统计，不影响结果），
        # 不要拼进 query 文本，避免干扰美团侧的理解。
        origin_query = (params.get("origin_query") or "").strip()

        body = {"city": city, "query": query, "channel": params.get("channel") or "default"}
        if origin_query:
            body["originQuery"] = origin_query

        key = self._cache_key(city, query, body["channel"])
        hit = self._cache_get(key)
        if hit and hit[1] < CACHE_TTL:
            print(f"[Plugin] meituan 命中缓存（{_fmt_age(hit[1])}前抓取）: {query[:40]}")
            return self._from_cache(hit[0], city, query, origin_query, hit[1], stale=False)

        def _fail(message: str, status: str = "error") -> dict:
            """这次没拿到有效数据：有旧结果就回退（标 stale），没有才照原样报错。"""
            old = self._cache_get(key)
            if old and old[1] <= CACHE_STALE_MAX:
                print(f"[Plugin] meituan 回退缓存（{_fmt_age(old[1])}前抓取）: {message}")
                return self._from_cache(old[0], city, query, origin_query, old[1],
                                        stale=True, reason=message)
            return {"status": status, "message": message}

        try:
            resp = requests.post(
                "https://mcp-open-cater.meituan.com/v1/api/voyage/openapi/query",
                headers={
                    "Authorization": self.token,  # 注意：直接放 token，不加 Bearer
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=60,  # 实测普通查询 11-24s（2026-09-19），行程规划类查询 36-44s（2026-09-24），30s 会全部超时降级
            )
            data = resp.json()
        except requests.exceptions.Timeout:
            return _fail("美团接口超时，请稍后重试")
        except Exception as e:
            return _fail(f"美团接口请求失败: {e}")

        if resp.status_code == 403:
            return _fail("美团 Token 校验失败(403)，请检查 meituan_token 是否有效")
        if resp.status_code != 200 or data.get("code") not in (0, None):
            return _fail(f"美团接口返回异常(HTTP {resp.status_code}): {str(data)[:200]}")

        result_data = data.get("data") or ""
        if data.get("result") != "SUCCESS" or not result_data:
            return _fail(f"美团未返回有效结果: {data.get('msg') or data.get('errorInfo') or '空结果'}")

        # 美团限流/过载时返回「小团忙不过来」式兜底文案，HTTP 200 但无数据——必须识别，
        # 否则会把无效内容当真实数据塞给模型（2026-09-18 实测踩坑）
        answer_text = str(result_data)
        if _BUSY_RE.search(answer_text) or len(answer_text.strip()) < 60:
            return _fail("美团接口当前繁忙，请稍后重试", status="busy")

        self._cache_put(key, result_data)
        bookings = self.extract_bookings(answer_text, query, city, origin_query)
        return {"status": "ok", "source": "美团酒旅", "city": city, "answer": result_data, "bookings": bookings}

    def extract_bookings(self, answer: str, query: str, city: str, origin_query: str = "") -> list:
        """从美团返回的 Markdown 中提取可预订入口；门票类无链接时构造美团官方搜索入口。

        同时从链接标题附近的文本提取价格（¥xx / xx元 / xx起），挂到条目 price 字段，
        前端预订卡片可直接展示「¥xx 起」。
        返回: [{"type": "hotel"|"ticket", "title": str, "url": str, "price": str|None}]
        """
        bookings = []
        seen = set()
        for title, url in _MD_LINK_RE.findall(answer or ""):
            if _IMG_URL_RE.search(url) or "图片" in title:
                continue
            if "dpurl.cn" not in url and "meituan.com" not in url and "dianping.com" not in url:
                continue
            clean = _clean_title(title)
            if not clean or len(clean) > 40 or url in seen:
                continue
            # 过滤房型链接（"观景双床房""倾橙 舒适投影大床房"等）：不是独立的酒店/门票预订入口
            if (_ROOM_TITLE_RE.search(clean) or re.search(r'(大床|双床|单人间|双人间|榻榻米|投影)', clean)) \
                    and not any(w in clean for w in _HOTEL_WORDS) and "美宿" not in clean:
                continue
            # 标题含住宿词优先判酒店（如"如家精选酒店(西湖景区店)"含"景区"但明显是酒店）
            if any(w in clean for w in _HOTEL_WORDS):
                btype = "hotel"
            elif any(w in clean for w in ("门票", "演出", "游船", "乐园", "票务", "索道", "入园")):
                btype = "ticket"
            else:
                # 标题无业态词时按用户原话判意图：改写后的 query 必带景点/门票/酒店词，
                # 会把菜品链接误判成门票卡片（2026-09-24 实测）
                intent_q = origin_query or query
                if any(w in intent_q for w in _TICKET_WORDS):
                    btype = "ticket"
                elif any(w in intent_q for w in _HOTEL_WORDS):
                    btype = "hotel"
                else:
                    btype = "item"
            seen.add(url)
            bookings.append({
                "type": btype,
                "title": clean,
                "url": url,
                "price": _extract_price_near(answer, clean, url),
            })

        # 门票/酒店类查询：美团 answer 未带链接时，兜底给美团官方入口。
        # 注意：meituan.com/s/ 搜索页有反爬（301→/error/404），实测仅首页可直达，故统一回首页。
        if not bookings:
            if any(w in query for w in _TICKET_WORDS):
                # 兜底关键词：优先用用户原话（originQuery），其次从改写后的查询提取地点/景点名
                raw = (origin_query or "").strip() or query
                kw = re.split(
                    r'(门票|景区|景点|好玩|怎么|如何|什么|多少|价格|票价|酒店|住宿|推荐|值得|必去|附近|有|在|，|。|\?|？|!|！|\s)+',
                    raw,
                )[0].strip('，。 ') or city
                bookings.append({
                    "type": "ticket",
                    "title": "%s·美团在线购票" % kw[:20],
                    "url": "https://www.meituan.com/",
                    "price": None,
                })
            elif any(w in query for w in _HOTEL_WORDS):
                bookings.append({
                    "type": "hotel",
                    "title": "%s酒店·美团在线预订" % city,
                    "url": "https://www.meituan.com/",
                    "price": None,
                })
        return bookings[:6]

    def format_for_llm(self, result: dict) -> str:
        if result.get("status") != "ok":
            msg = result.get("message", "")
            return f"[美团酒旅] 查询失败: {msg}" if msg else ""
        # 去掉 </answer> 之类的闭合标签，只保留正文
        answer = result.get("answer", "").replace("</answer>", "").strip()
        # 剥掉预订/购票链接：回复只告知数据来源，不向用户输出美团链接（2026-09-24 需求）
        answer = _MD_LINK_RE.sub(lambda m: m.group(1), answer)
        # 美团尾部的券包营销话术对用户无价值，一并过滤
        answer = '\n'.join(ln for ln in answer.split('\n')
                           if not any(w in ln for w in ('🎁', '省钱券包', '专属优惠')))
        # 数据新鲜度必须写进提示词：回退数据若还顶着「实时数据」的标题，
        # 模型会把几十分钟前的票价当实时价报给用户——比拿不到数据更糟。
        age = _fmt_age(result.get("age_seconds"))
        if result.get("stale"):
            title = f"## 🍜 美团数据（{result.get('city', '')}·{age}前缓存）"
            suffix = (
                f"⚠️ 美团接口本次不可用（{str(result.get('degraded_reason') or '未知原因')[:60]}），"
                f"以上是 {age}前抓取的缓存数据，价格与库存可能已变动。"
                f"回复时必须先说明这是 {age}前的数据、仅供参考，不要当成实时报价；"
                f"不要输出任何预订/购票链接或网址"
            )
            return f"{title}\n{answer}\n\n{suffix}"
        age_note = f"（本次命中缓存，抓取于 {age}前）" if result.get("cached") else ""
        return (
            f"## 🍜 美团实时数据 ({result.get('city', '')})\n"
            f"{answer}\n\n"
            f"（以上来自美团真实商家数据{age_note}，价格/库存以美团 App 实时页面为准；"
            f"回复中引用时只需注明数据来源为「美团实时数据」，不要输出任何预订/购票链接或网址）"
        )
