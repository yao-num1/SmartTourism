"""
平台文旅热点数据接入适配器。

说明：
- 只支持通过平台官方、已授权服务商或用户主动配置的 API 地址接入，不做网页爬取。
- 抖音、小红书的内容/热点数据权限通常需要在开放平台、商业数据产品、第三方授权服务或合作项目中申请。
- 不同账号拿到的接口路径、字段和权限可能不同，所以这里通过环境变量配置官方接口地址，
  再做统一字段归一化，供前端稳定展示。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests


REQUEST_TIMEOUT = 8

TIKHUB_DEFAULT_BASE_URL = "https://api.tikhub.dev"  # 中国大陆加速域名，主域名 api.tikhub.io 已被 GFW 屏蔽
TIKHUB_DOUYIN_BILLBOARD_ENDPOINTS = [
    "/api/v1/douyin/billboard/fetch_hot_total_search_list",
    "/api/v1/douyin/billboard/fetch_hot_total_topic_list",
    "/api/v1/douyin/billboard/fetch_hot_total_hot_word_list",
    "/api/v1/douyin/billboard/fetch_hot_challenge_list",
    "/api/v1/douyin/billboard/fetch_hot_total_video_list",
    "/api/v1/douyin/billboard/fetch_hot_city_list",
]

TOURISM_CATEGORIES: Dict[str, Dict[str, Any]] = {
    "route": {
        "label": "旅游规划攻略",
        "type": "route",
        "cat": "文创IP",
        "keywords": ["旅游", "旅行", "攻略", "路线", "行程", "游玩", "周末", "亲子游", "自驾", "避暑", "Citywalk", "citywalk", "特种兵", "打卡"],
        "color": ("#2563eb", "#60a5fa", "#eff6ff"),
    },
    "attraction": {
        "label": "景点推荐信息",
        "type": "culture",
        "cat": "博物馆",
        "keywords": ["景区", "景点", "古城", "博物馆", "公园", "寺", "山", "湖", "海", "岛", "园林", "古镇", "夜游", "演艺", "展览", "门票"],
        "color": ("#0f766e", "#5eead4", "#ecfeff"),
    },
    "food": {
        "label": "美食推荐信息",
        "type": "photo",
        "cat": "节庆",
        "keywords": ["美食", "餐厅", "小吃", "夜市", "火锅", "烧烤", "茶", "咖啡", "面", "粉", "菜", "饭", "甜品", "早茶", "夜宵"],
        "color": ("#c2410c", "#fdba74", "#fff7ed"),
    },
    "hotel": {
        "label": "住宿推荐信息",
        "type": "route",
        "cat": "文创IP",
        "keywords": ["酒店", "民宿", "客栈", "住宿", "露营", "营地", "温泉", "度假"],
        "color": ("#7c3aed", "#c4b5fd", "#f5f3ff"),
    },
    "creative": {
        "label": "文创灵感",
        "type": "culture",
        "cat": "文创IP",
        "keywords": ["文创", "非遗", "国潮", "博物馆", "礼盒", "伴手礼", "冰箱贴", "盲盒", "手作", "汉服", "簪花", "盖章"],
        "color": ("#b45309", "#facc15", "#fffbeb"),
    },
}

TOURISM_SIGNAL_WORDS = sorted(
    {word for meta in TOURISM_CATEGORIES.values() for word in meta["keywords"]} |
    {"文旅", "城市", "周边游", "本地生活", "出片", "拍照", "烟火气", "老街", "市集", "文化", "历史", "风景"},
    key=len,
    reverse=True,
)

CARTOON_IMAGE_CACHE: Dict[str, str] = {}

# 灵感卡主题分类 —— 与前端灵感库筛选 chip 一一对应（国潮/非遗/博物馆/文创IP/节庆）
INSPIRE_TOPIC_META: Dict[str, Dict[str, Any]] = {
    "国潮": {"key": "guochao", "label": "国潮", "suffix": "国潮灵感", "type": "culture", "cat": "国潮", "color": ("#b91c1c", "#fca5a5", "#fef2f2")},
    "非遗": {"key": "feiyi", "label": "非遗", "suffix": "非遗灵感", "type": "culture", "cat": "非遗", "color": ("#7c2d12", "#fdba74", "#fff7ed")},
    "博物馆": {"key": "museum", "label": "博物馆", "suffix": "博物馆灵感", "type": "culture", "cat": "博物馆", "color": ("#0f766e", "#5eead4", "#ecfeff")},
    "文创IP": {"key": "wenchuang", "label": "文创IP", "suffix": "文创IP灵感", "type": "culture", "cat": "文创IP", "color": ("#b45309", "#facc15", "#fffbeb")},
    "节庆": {"key": "festival", "label": "节庆", "suffix": "节庆灵感", "type": "route", "cat": "节庆", "color": ("#c2410c", "#fdba74", "#fff7ed")},
}

# 抖音灵感检索词池 —— (检索词, 建议分类)
# 2026-09-20 实测：抖音热点检索是"句子包含检索词"的匹配方式。
# 能稳定命中的检索词（下方全部实测 p1>0）：新中式/汉服/簪花/中式美学/中式/国风/古风、
# 非遗/竹编、博物馆/文物、冰箱贴/盲盒、中秋/国庆/烟花秀/面包节/丰收节/市集/文化节；
# 返回空、不要再用：国潮、文创、节庆、伴手礼、灯会、庙会、马面裙、漆扇、剪纸、考古、文创雪糕、IP。
# 建议分类只是线索，最终分类由抓取后的二次验证按内容判定。
INSPIRE_SEARCH_KEYWORDS: List[tuple] = [
    ("新中式", "国潮"),
    ("汉服", "国潮"),
    ("簪花", "国潮"),
    ("中式美学", "国潮"),
    ("中式", "国潮"),
    ("国风", "国潮"),
    ("古风", "国潮"),
    ("非遗", "非遗"),
    ("竹编", "非遗"),
    ("博物馆", "博物馆"),
    ("文物", "博物馆"),
    ("冰箱贴", "文创IP"),
    ("盲盒", "文创IP"),
    ("中秋", "节庆"),
    ("国庆", "节庆"),
    ("烟花秀", "节庆"),
    ("面包节", "节庆"),
    ("丰收节", "节庆"),
    ("文化节", "节庆"),
    ("市集", "节庆"),
]

# 内容主题词 —— 用于"抓取后的第二次验证"：按内容本身判定它属于哪一个灵感分类
INSPIRE_THEME_KEYWORDS: Dict[str, List[str]] = {
    "国潮": [
        "国潮", "汉服", "新中式", "马面裙", "簪花", "国风", "古风", "中式", "华服",
        "汉元素", "东方美学", "宋韵", "唐风", "妆造", "华流", "汉服妆造", "国风舞蹈",
    ],
    "非遗": [
        "非遗", "手作", "工艺", "传承人", "苏绣", "蜀绣", "漆器", "漆扇", "剪纸",
        "油纸伞", "绒花", "打铁花", "泥塑", "竹编", "扎染", "刺绣", "皮影", "木雕",
        "陶艺", "青瓷", "茶艺", "老字号", "手工",
    ],
    "博物馆": [
        "博物馆", "文博", "文物", "考古", "馆藏", "博物院", "展馆", "看展", "特展",
        "展览", "展会", "展出", "遗址", "美术馆", "镇馆", "藏品",
    ],
    "文创IP": [
        "文创", "冰箱贴", "盲盒", "雪糕", "棒棒糖", "徽章", "纪念品", "伴手礼",
        "礼盒", "盖章", "手办", "潮玩", "谷子", "联名", "周边", "玩偶", "贴纸",
        "明信片", "限定款", "打卡点", "IP形象",
    ],
    "节庆": [
        "中秋", "国庆", "春节", "元宵", "端午", "灯会", "庙会", "跨年", "市集",
        "烟花", "民俗", "丰收节", "文化节", "旅游节", "面包节", "节庆", "晚会",
        "年味", "花灯", "灯光秀", "巡游", "庆典", "嘉年华",
    ],
}

# 文创营销可用信号 —— 命中说明这条热点能被转成文创选题/包装语言/传播点
INSPIRE_MARKETING_SIGNALS: List[str] = [
    "文创", "非遗", "博物馆", "国潮", "汉服", "新中式", "簪花", "国风", "中式",
    "古风", "文物", "考古", "看展", "特展", "市集", "节庆", "晚会", "灯会",
    "烟花", "巡游", "民俗", "冰箱贴", "盲盒", "雪糕", "棒棒糖", "徽章",
    "纪念品", "伴手礼", "礼盒", "手作", "工艺", "传承", "匠人", "出片", "打卡",
    "联名", "周边", "限定", "游客", "旅游", "文旅", "景区", "古城", "古镇",
    "老街", "演出", "活动", "体验", "文化",
]

# 与文创营销无关或不宜采用的内容（新闻/负面/平台泛词），二次验证直接剔除
INSPIRE_REJECT_WORDS: List[str] = [
    "处理结果", "通报", "处罚", "事故", "遇难", "伤亡", "去世", "病逝", "疫情",
    "确诊", "股价", "跌停", "涨停", "暴涨", "暴跌", "落马", "免职", "被查",
    "辟谣", "维权", "投诉", "举报", "抄袭", "侵权", "起诉", "败诉", "离婚",
    "出轨", "绯闻", "塌房", "赌博", "诈骗", "逮捕", "判刑", "失踪", "地震",
    "洪水", "台风", "火灾", "招聘", "分数线", "放假通知", "开奖", "送别",
]

# 通用热点标题黑名单 —— 仅含平台名或泛化词、无具体景点/美食内容的热点直接过滤
GENERIC_HOTSPOT_TITLES = {
    "抖音", "热点", "热门", "未命名热点", "抖音热点", "抖音精选", "抖音课堂",
    "实时平台", "抖音热搜", "热榜", "热搜", "热门话题", "实时热点", "实时",
    "小红书", "小红书热点", "小红书热搜", "推荐", "榜单", "排行榜", "热门推荐",
    "抖音推荐", "精选", "课堂", "话题", "官方", "授权", "平台数据",
}

# 通用词粒子 —— 用于判断标题去掉这些词后是否还有实质性内容
GENERIC_TITLE_PARTICLES = {
    "抖音", "小红书", "热点", "热门", "精选", "课堂", "热搜", "热榜",
    "实时", "平台", "推荐", "榜单", "排行榜", "话题", "官方", "授权",
    "接口", "数据", "最新", "今日", "首发", "独家",
}


def _is_generic_hotspot_title(title: str) -> bool:
    """判断热点标题是否过于泛化（仅含平台名或通用词，无具体景点/美食等内容）。"""
    title = (title or "").strip()
    if not title or title in GENERIC_HOTSPOT_TITLES:
        return True
    # 去除通用词后，看是否还有实质性内容
    remaining = title
    for word in GENERIC_TITLE_PARTICLES:
        remaining = remaining.replace(word, "")
    # 去除标点、空格和特殊符号
    remaining = re.sub(r'[\s\-_|·｜/,，、。.!！?？:：;；()（）\[\]【】「」""\'\\]+', '', remaining)
    if len(remaining) < 2:
        return True
    return False


def _validate_inspire_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """抓取后的**第二次验证**：这条热点能不能用于文创营销，以及它属于哪个灵感分类。

    通过条件（全部满足）：
      1. 标题有实质内容，不是"抖音/热搜"这类平台泛词；
      2. 不命中新闻/负面/无关词（事件处理结果、事故、招聘等）；
      3. 内容命中至少一个主题词（国潮/非遗/博物馆/文创IP/节庆）；
      4. 命中文创营销可用信号，或带有文旅消费场景词 —— 否则只是一条与文创无关的热搜。

    返回 {"theme", "label", "hits", "signals", "score"}；不可用返回 None。
    """
    title = " ".join(str(item.get("title") or "").split())
    if len(title) < 6 or _is_generic_hotspot_title(title):
        return None

    text = "%s %s" % (title, item.get("summary") or "")
    if any(word in text for word in INSPIRE_REJECT_WORDS):
        return None

    theme_scores: Dict[str, int] = {}
    theme_hits: Dict[str, List[str]] = {}
    for theme, words in INSPIRE_THEME_KEYWORDS.items():
        in_title = [word for word in words if word in title]
        in_text = [word for word in words if word in text]
        if not in_text:
            continue
        # 标题命中权重更高：检索词常出现在标题里，正文命中只作补充
        theme_scores[theme] = len(in_title) * 3 + len(in_text)
        theme_hits[theme] = in_title or in_text
    if not theme_scores:
        return None

    theme = max(theme_scores, key=theme_scores.get)
    signals = [word for word in INSPIRE_MARKETING_SIGNALS if word in text]
    scene_words = ("文旅", "旅游", "景区", "古城", "古镇", "老街", "游客", "文化",
                   "博物馆", "非遗", "市集", "展览", "演出", "活动", "体验", "美食", "城市")
    has_scene = any(word in text for word in scene_words)

    score = theme_scores[theme] + len(signals)
    if theme_scores[theme] < 3 and not (signals and has_scene):
        return None
    if not signals and not has_scene:
        return None
    return {
        "theme": theme,
        "label": theme,
        "hits": theme_hits.get(theme, []),
        "signals": signals[:6],
        "score": score,
    }


def _title_fingerprint(title: str) -> str:
    """标题指纹：去掉标点与话题符号，用于跨检索词去重。"""
    text = re.sub(r"[#\s\-_|·｜/,，、。.!！?？:：;；()（）\[\]【】「」\"'~]+", "", str(title or ""))
    return text.lower()


# ── 滚动热点池 ──
# 抖音检索词能命中的文旅/文创热点总量有限（同一批热搜会被多个检索词反复命中），
# 如果每次更新都直接从"本轮抓到的那几条"里选，连续更新就会翻来覆去看到同一批卡片。
# 所以把每轮"二次验证通过"的内容并入一个滚动池，优先放出**还没展示过**的条目，
# 并给池子加 TTL：既能保证每次更新都有新卡片，又不会把过期热点拿出来充数。
INSPIRE_POOL_TTL = 12 * 3600
INSPIRE_POOL_MAX = 300
INSPIRE_POOL_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "inspire_pool.json")
_INSPIRE_POOL: Dict[str, Dict[str, Any]] = {}
_INSPIRE_POOL_LOCK = threading.Lock()
_INSPIRE_WARM_LOCK = threading.Lock()
_INSPIRE_WARMING = False


def _pool_load() -> int:
    """从磁盘恢复热点池：池子跨重启累积，保证重启后第一次更新也有新鲜内容。"""
    try:
        with open(INSPIRE_POOL_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return 0
    now = time.time()
    loaded = 0
    with _INSPIRE_POOL_LOCK:
        for fingerprint, row in (raw.get("items") or {}).items():
            if not isinstance(row, dict) or not isinstance(row.get("item"), dict):
                continue
            if now - float(row.get("added_at") or 0) > INSPIRE_POOL_TTL:
                continue
            _INSPIRE_POOL[fingerprint] = row
            loaded += 1
    return loaded


def _pool_save() -> None:
    with _INSPIRE_POOL_LOCK:
        snapshot = {"saved_at": time.time(), "items": dict(_INSPIRE_POOL)}
    try:
        os.makedirs(os.path.dirname(INSPIRE_POOL_FILE), exist_ok=True)
        tmp = "%s.tmp" % INSPIRE_POOL_FILE
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        os.replace(tmp, INSPIRE_POOL_FILE)
    except Exception as exc:
        print("[inspire] 热点池落盘失败: %s" % exc)


# 启动时恢复热点池（跨重启累积，重启后第一次"更新热点"也能拿到新鲜卡片）
_pool_load()


def _pool_merge(items: List[Dict[str, Any]]) -> int:
    """把本轮验证通过的热点并入滚动池，返回池子当前大小。"""
    now = time.time()
    with _INSPIRE_POOL_LOCK:
        for item in items:
            fingerprint = _title_fingerprint(item.get("title"))
            if not fingerprint:
                continue
            row = _INSPIRE_POOL.get(fingerprint)
            if row:
                row["item"] = item
                row["score"] = int(item.get("inspire_score") or 0)
            else:
                _INSPIRE_POOL[fingerprint] = {
                    "item": item,
                    "cat": item.get("inspire_cat") or "文创IP",
                    "score": int(item.get("inspire_score") or 0),
                    "served": 0,
                    "added_at": now,
                    "last_served_at": 0.0,
                }
        for fingerprint in [fp for fp, row in _INSPIRE_POOL.items() if now - row["added_at"] > INSPIRE_POOL_TTL]:
            _INSPIRE_POOL.pop(fingerprint, None)
        if len(_INSPIRE_POOL) > INSPIRE_POOL_MAX:
            ordered = sorted(_INSPIRE_POOL.items(), key=lambda kv: kv[1]["added_at"])
            for fingerprint, _row in ordered[:len(_INSPIRE_POOL) - INSPIRE_POOL_MAX]:
                _INSPIRE_POOL.pop(fingerprint, None)
        return len(_INSPIRE_POOL)


def _pool_pick(count: int, seed: str) -> List[Dict[str, Any]]:
    """从滚动池里按分类轮询取出 count 条，优先取"还没展示过 / 展示次数最少"的。"""
    rng = random.Random(seed)
    with _INSPIRE_POOL_LOCK:
        buckets: Dict[str, List[tuple]] = {}
        order: List[str] = []
        for fingerprint, row in _INSPIRE_POOL.items():
            cat = row["cat"]
            if cat not in buckets:
                buckets[cat] = []
                order.append(cat)
            buckets[cat].append((fingerprint, row))
        for cat in order:
            rng.shuffle(buckets[cat])
            # served 升序（没展示过的优先）→ 主题信号强的优先 → 新进池的优先
            buckets[cat].sort(key=lambda kr: (kr[1]["served"], -kr[1]["score"], -kr[1]["added_at"]))

        picked: List[Dict[str, Any]] = []
        while len(picked) < count and any(buckets[cat] for cat in order):
            for cat in order:
                if len(picked) >= count:
                    break
                if buckets[cat]:
                    _fp, row = buckets[cat].pop(0)
                    row["served"] += 1
                    row["last_served_at"] = time.time()
                    picked.append(row["item"])
        return picked


def _pool_size() -> int:
    with _INSPIRE_POOL_LOCK:
        return len(_INSPIRE_POOL)


def _env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                registry_value, _ = winreg.QueryValueEx(key, name)
                return str(registry_value).strip()
        except Exception:
            return ""
    return ""


def _first_value(data: Dict[str, Any], keys: List[str], default: str = "") -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return str(value)
    return default


def _flatten_strings(value: Any) -> List[str]:
    """从字符串、列表、字典中尽量提取可展示文本或 URL。"""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, list):
        result: List[str] = []
        for item in value:
            result.extend(_flatten_strings(item))
        return result
    if isinstance(value, dict):
        priority_keys = [
            "url",
            "uri",
            "title",
            "name",
            "word",
            "keyword",
            "desc",
            "nickname",
            "display_name",
            "cover",
            "cover_url",
            "url_list",
        ]
        result = []
        for key in priority_keys:
            if key in value:
                result.extend(_flatten_strings(value.get(key)))
        return result
    return [str(value)]


def _first_any(data: Dict[str, Any], keys: List[str], default: str = "") -> str:
    for key in keys:
        values = _flatten_strings(data.get(key))
        if values:
            return values[0]
    return default


def _extract_items(payload: Any) -> List[Dict[str, Any]]:
    """兼容常见接口返回结构：list、data.list、data.items、result.list 等。"""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    candidates = [
        payload.get("items"),
        payload.get("list"),
        payload.get("records"),
        payload.get("aweme_list"),
        payload.get("search_list"),
        payload.get("topic_list"),
        payload.get("word_list"),
        payload.get("hot_list"),
        payload.get("challenge_list"),
        payload.get("data_list"),
        payload.get("rank_list"),
        payload.get("objs"),
        payload.get("data"),
        payload.get("result"),
        payload.get("response"),
    ]

    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            nested = _extract_items(candidate)
            if nested:
                return nested
    return []


def _normalize_items(provider: str, payload: Any) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for item in _extract_items(payload):
        title = _first_any(
            item,
            [
                "title",
                "name",
                "desc",
                "description",
                "keyword",
                "key_word",
                "word",
                "word_name",
                "sentence",
                "hot_word",
                "challenge_name",
                "challenge_title",
                "event_word",
                "label",
                "video_title",
            ],
            "未命名热点",
        )
        summary = _first_any(item, ["summary", "desc", "description", "content", "subtitle", "abstract"], "")
        url = _first_any(item, ["url", "share_url", "link", "open_url", "jump_url", "schema", "share_info"], "")
        author = _first_any(item, ["author", "nickname", "user_name", "account_name", "author_info", "user"], "")
        cover = _first_any(
            item,
            [
                "cover",
                "cover_url",
                "image",
                "image_url",
                "thumbnail",
                "cover_thumb",
                "first_item_cover_url",
                "video",
                "images",
                "avatar_thumb",
            ],
            "",
        )
        heat = _first_any(
            item,
            [
                "hot_value",
                "heat",
                "hot_score",
                "score",
                "search_score",
                "rank_score",
                "view_count",
                "play_count",
                "digg_count",
                "discuss_num",
                "sentence_id",
                "rank",
            ],
            "",
        )
        region = _first_any(item, ["city_name", "city", "province", "region"], "")

        normalized.append(
            {
                "platform": provider,
                "title": title,
                "summary": summary or ("来自%s官方授权接口" % provider),
                "url": url,
                "author": author,
                "cover": cover,
                "heat": heat,
                "region": region,
            }
        )
    return normalized[:10]


def _absolute_url(base_url: str, endpoint: str) -> str:
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint
    return base_url.rstrip("/") + "/" + endpoint.lstrip("/")


def _configured_tikhub_endpoints() -> List[str]:
    raw = _env("TIKHUB_DOUYIN_ENDPOINTS")
    if not raw:
        return TIKHUB_DOUYIN_BILLBOARD_ENDPOINTS
    return [item.strip() for item in raw.split(",") if item.strip()]


def _call_tikhub_endpoint(base_url: str, api_key: str, endpoint: str) -> List[Dict[str, Any]]:
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer %s" % api_key,
    }
    url = _absolute_url(base_url, endpoint)
    response = requests.post(url, headers=headers, json={}, timeout=REQUEST_TIMEOUT)
    if response.status_code == 405:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    return _normalize_items("TikHub 抖音", payload)


def _call_tikhub_get_endpoint(base_url: str, api_key: str, endpoint: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """调用 TikHub GET 类榜单接口（需 query 参数，如城市榜 page/page_size/order/keyword）。"""
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer %s" % api_key,
    }
    url = _absolute_url(base_url, endpoint)
    response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    return _normalize_items("TikHub 抖音", payload)


def _format_heat(heat: Any) -> str:
    """把热度值格式化成易读形式：5355054 → 535.5万；非数字原样返回。"""
    raw = str(heat or "").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return raw or "—"
    if value >= 10000:
        return "%.1f万" % (value / 10000)
    return str(int(value))


def _compact_text(text: str, limit: int = 28) -> str:
    text = " ".join(str(text or "").replace("#", " ").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _score_category(text: str, meta: Dict[str, Any]) -> int:
    lower = text.lower()
    score = 0
    for word in meta["keywords"]:
        if word.lower() in lower:
            score += 3 if len(word) >= 2 else 1
    return score


def _classify_tourism_item(item: Dict[str, Any], city: str) -> Optional[Dict[str, Any]]:
    title = item.get("title", "")
    # 过滤掉通用平台热点标题（如"抖音热点""抖音精选"等无具体内容的词条）
    if _is_generic_hotspot_title(title):
        return None

    # 主题检索得到的条目：直接采用检索时归属的灵感分类，不再做文旅信号二次过滤
    # （"中秋""国庆"等节庆词、纯电商词不在 TOURISM_SIGNAL_WORDS 内，否则会被误杀）
    topic = str(item.get("inspire_cat") or "").strip()
    if topic in INSPIRE_TOPIC_META:
        meta = INSPIRE_TOPIC_META[topic]
        return {
            "key": meta["key"],
            "label": meta["label"],
            "suffix": meta["suffix"],
            "type": meta["type"],
            "cat": meta["cat"],
            "score": 6 + int(item.get("inspire_score") or 0),
            "color": meta["color"],
        }

    text = "%s %s %s" % (title, item.get("summary", ""), city)
    text_lower = text.lower()
    signal_score = sum(1 for word in TOURISM_SIGNAL_WORDS if word.lower() in text_lower)
    category_scores = {
        key: _score_category(text, meta)
        for key, meta in TOURISM_CATEGORIES.items()
    }
    category_key = max(category_scores, key=category_scores.get)
    category_score = category_scores.get(category_key, 0)

    # 城市名本身只能算弱相关，必须再命中文旅/攻略/景点/美食等语义。
    if signal_score <= 0 and category_score <= 0:
        return None

    meta = TOURISM_CATEGORIES[category_key]
    return {
        "key": category_key,
        "label": meta["label"],
        "type": meta["type"],
        "cat": meta["cat"],
        "score": signal_score + category_score,
        "color": meta["color"],
    }


def _extract_core_theme(item: Dict[str, Any], category: Dict[str, Any], city: str) -> str:
    raw_title = _compact_text(item.get("title") or item.get("summary") or "文旅热点", 24)
    if _is_generic_hotspot_title(raw_title):
        raw_title = "%s%s" % (city, category["label"])
    # 主题检索命中的热点：标题本身就是选题，分类信息由卡片名单独承载
    if category.get("suffix"):
        return raw_title
    suffix_map = {
        "route": "路线攻略",
        "attraction": "景点推荐",
        "food": "美食推荐",
        "hotel": "住宿推荐",
        "creative": "文创灵感",
    }
    suffix = suffix_map.get(category["key"], "文旅灵感")
    if suffix in raw_title or category["label"] in raw_title:
        return raw_title
    return "%s｜%s" % (raw_title, suffix)


def _cartoon_image(topic: str, category: Dict[str, Any], city: str) -> str:
    cache_key = "%s-%s-%s" % (city, category["key"], topic)
    if cache_key in CARTOON_IMAGE_CACHE:
        return CARTOON_IMAGE_CACHE[cache_key]

    primary, accent, bg = category["color"]
    title = _compact_text(topic, 14)
    label = category["label"]
    icon_map = {
        "route": "路线",
        "attraction": "景点",
        "food": "美食",
        "hotel": "住宿",
        "creative": "文创",
    }
    icon = icon_map.get(category["key"], "文旅")
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="900" height="560" viewBox="0 0 900 560">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="%s"/><stop offset="1" stop-color="#ffffff"/></linearGradient>
  <linearGradient id="card" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s"/></linearGradient>
</defs>
<rect width="900" height="560" rx="38" fill="url(#bg)"/>
<circle cx="745" cy="95" r="96" fill="%s" opacity=".22"/>
<circle cx="116" cy="445" r="130" fill="%s" opacity=".20"/>
<path d="M120 388 C260 292 326 354 450 274 C574 194 650 222 786 136" fill="none" stroke="%s" stroke-width="12" stroke-linecap="round" stroke-dasharray="2 26"/>
<g transform="translate(98 92)">
  <rect width="704" height="372" rx="34" fill="#fff" opacity=".92"/>
  <rect x="42" y="42" width="150" height="150" rx="36" fill="url(#card)"/>
  <text x="117" y="134" text-anchor="middle" font-family="Microsoft YaHei, Noto Sans SC, sans-serif" font-size="42" font-weight="800" fill="#fff">%s</text>
  <text x="226" y="86" font-family="Microsoft YaHei, Noto Sans SC, sans-serif" font-size="30" font-weight="800" fill="%s">%s</text>
  <text x="226" y="136" font-family="Microsoft YaHei, Noto Sans SC, sans-serif" font-size="48" font-weight="900" fill="#111827">%s</text>
  <text x="226" y="196" font-family="Microsoft YaHei, Noto Sans SC, sans-serif" font-size="26" fill="#475569">%s · 实时热点识别</text>
  <rect x="42" y="244" width="620" height="76" rx="22" fill="%s" opacity=".16"/>
  <text x="70" y="293" font-family="Microsoft YaHei, Noto Sans SC, sans-serif" font-size="24" fill="#334155">未匹配到可信实图，已使用卡通主题图替代</text>
</g>
</svg>""" % (bg, primary, accent, accent, primary, primary, icon, primary, label, title, city, primary)
    uri = "data:image/svg+xml;charset=utf-8," + quote(svg)
    CARTOON_IMAGE_CACHE[cache_key] = uri
    return uri


def _load_ark_cfg() -> Dict[str, Any]:
    """读取火山方舟配置（与 server/app.py 共用 data/ark_config.json）。"""
    path = os.path.join(os.path.dirname(__file__), "..", "data", "ark_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


AI_IMAGE_CACHE: Dict[str, str] = {}

# ── 灵感卡 AI 配图：懒生成 + 磁盘缓存 + 线程池并发 ──
# 实测单张 Seedream 约 45s，但并发不受限（4 张并行总耗时仍约 50s），所以用线程池把
# "请求图片"与"等待生成"解耦：接口先返回占位图，前端轮询到 ready 再换成真实图片，
# 避免一次请求里串行等 10 张图（那样要 7 分钟以上）。
INSPIRE_IMAGE_WORKERS = 12
INSPIRE_IMAGE_DIR = os.path.join(os.path.dirname(__file__), "..", "dashboard", "poster", "inspire")
_inspire_image_pool = ThreadPoolExecutor(max_workers=INSPIRE_IMAGE_WORKERS)
_inspire_image_events: Dict[str, threading.Event] = {}
_inspire_image_guard = threading.Lock()

# 生成失败的 slug（欠费/限流/网络异常等）：冷却期内不再重试，并让接口直接告诉前端
# 「这张图暂时拿不到」，前端立刻回退兜底图，而不是傻等 2 分钟。
INSPIRE_IMAGE_FAIL_COOLDOWN = 600
_INSPIRE_IMAGE_FAILED: Dict[str, float] = {}


def _mark_inspire_image_failed(slug: str) -> None:
    with _inspire_image_guard:
        _INSPIRE_IMAGE_FAILED[slug] = time.time()


def inspire_image_failed(slug: str) -> bool:
    stamp = _INSPIRE_IMAGE_FAILED.get(slug)
    if not stamp:
        return False
    return (time.time() - stamp) <= INSPIRE_IMAGE_FAIL_COOLDOWN


def inspire_image_slug(title: str, theme: str, city: str, cat: str = "") -> str:
    """灵感配图的稳定 id：同一条热点 + 同一分类始终得到同一张缓存图。"""
    raw = "%s|%s|%s|%s" % (city, theme, title, cat or theme)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def inspire_image_path(slug: str) -> str:
    return os.path.join(INSPIRE_IMAGE_DIR, "%s.jpg" % slug)


def inspire_image_ready(slug: str) -> bool:
    path = inspire_image_path(slug)
    return os.path.exists(path) and os.path.getsize(path) > 10000


def inspire_image_url(topic: str, cat: str, city: str) -> str:
    """灵感卡片统一使用 AI 生图：返回懒加载地址，由 /api/inspire/image 按需生成。"""
    return "/api/inspire/image?t=%s&c=%s&y=%s" % (
        quote(str(topic or "")), quote(str(cat or "文创IP")), quote(str(city or "杭州")),
    )


def _inspire_image_job(slug: str, title: str, desc: str, theme: str, city: str, cat: str,
                       event: threading.Event) -> None:
    try:
        if not _ai_product_image(title, desc, theme, city, cat):
            _mark_inspire_image_failed(slug)
    except Exception as exc:
        print("[inspire] AI 概念图生成失败 %s: %s" % (slug, exc))
        _mark_inspire_image_failed(slug)
    finally:
        event.set()


def ensure_inspire_image(topic: str, cat: str, city: str, desc: str = "") -> str:
    """把某张灵感卡的 AI 概念图排进后台线程池；返回预期的文件路径。"""
    topic = (topic or "").strip()
    if not topic:
        return ""
    slug = inspire_image_slug(topic, cat, city, cat)
    path = inspire_image_path(slug)
    if inspire_image_ready(slug):
        return path
    if inspire_image_failed(slug):
        return path
    with _inspire_image_guard:
        event = _inspire_image_events.get(slug)
        if event is None:
            event = threading.Event()
            _inspire_image_events[slug] = event
            _inspire_image_pool.submit(_inspire_image_job, slug, topic, desc, cat, city, cat, event)
    return path


def wait_inspire_image(slug: str, timeout: float = 150.0) -> bool:
    """等待某张灵感图生成完成；超时或未登记则返回当前是否已就绪。"""
    if inspire_image_ready(slug):
        return True
    with _inspire_image_guard:
        event = _inspire_image_events.get(slug)
    if event is None:
        return inspire_image_ready(slug)
    event.wait(timeout)
    return inspire_image_ready(slug)


def _ai_product_image(title: str, desc: str, theme: str, city: str, cat: str) -> str:
    """按产品描述调用火山方舟 Seedream 生成文创概念图，落盘缓存；失败返回空串。

    图文一致性策略：prompt 由产品标题/主题/卖点直接构建，不含任何无关图库素材。
    """
    slug = inspire_image_slug(title, theme, city, cat)
    if slug in AI_IMAGE_CACHE:
        return AI_IMAGE_CACHE[slug]

    cfg = _load_ark_cfg()
    api_key = (cfg.get("api_key") or "").strip()
    if not api_key:
        return ""

    filename = "%s.jpg" % slug
    filepath = os.path.join(INSPIRE_IMAGE_DIR, filename)
    public_url = "/static/poster/inspire/%s" % filename
    if os.path.exists(filepath) and os.path.getsize(filepath) > 10000:
        AI_IMAGE_CACHE[slug] = public_url
        return public_url

    topic = _compact_text(title, 30)
    detail = _compact_text(desc, 60)
    prompt = (
        "文创产品概念效果图，主题：%s（%s·%s）。"
        "画面主体必须是一件以“%s”为设计灵感的文创实物产品（如冰箱贴、礼盒、徽章、手作、盲盒、文具等，按主题选一最合适品类），"
        "产品清晰居中，材质与工艺质感真实，融入%s地域文化元素；"
        "浅景深棚拍背景，柔和商业摄影布光，高级审美，色彩协调，构图完整。"
        "参考卖点：%s。"
        "严格要求：画面中不要出现任何文字、字母、水印、logo；不要出现人物正脸；不要拼接多图。"
        % (topic, city, theme, topic, city, detail)
    )
    try:
        resp = requests.post(
            "https://ark.cn-beijing.volces.com/api/v3/images/generations",
            headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % api_key},
            json={
                "model": cfg.get("model") or "doubao-seedream-4-5-251128",
                "prompt": prompt,
                "sequential_image_generation": "disabled",
                "size": cfg.get("image_size") or "2K",
                "response_format": "url",
                "stream": False,
                "watermark": bool(cfg.get("watermark", False)),
            },
            timeout=120,
        )
        if resp.status_code != 200:
            print("[inspire] AI概念图生成失败 HTTP %s: %s" % (resp.status_code, resp.text[:200]))
            return ""
        data = resp.json()
        url = (data.get("data") or [{}])[0].get("url", "")
        if not url:
            return ""
        img = requests.get(url, timeout=60)
        if img.status_code != 200 or len(img.content) < 10000:
            return ""
        os.makedirs(INSPIRE_IMAGE_DIR, exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(img.content)
        AI_IMAGE_CACHE[slug] = public_url
        return public_url
    except Exception as exc:
        print("[inspire] AI概念图生成异常: %s" % exc)
        return ""


def _select_contextual_image(item: Dict[str, Any], category: Dict[str, Any], topic: str, city: str) -> str:
    """灵感卡配图统一走 AI 生图：返回懒加载地址，图片由 /api/inspire/image 生成。

    不再使用"未匹配到可信实图，已使用卡通主题图替代"这类占位图，卡片视觉风格统一。
    """
    return inspire_image_url(topic, category.get("cat") or "文创IP", city)


def _build_card_summary(item: Dict[str, Any], category: Dict[str, Any], topic: str, city: str) -> str:
    heat = item.get("heat") or "实时热度"
    if category["key"] == "route":
        return "抖音热点识别到“%s”适合转化为%s行程攻略，可围绕路线顺序、交通方式、停留时长和预算取舍做成卡片。" % (topic, city)
    if category["key"] == "attraction":
        return "抖音热点识别到“%s”与景点/文化空间相关，可沉淀为景点亮点、历史背景、拍照机位和避坑提醒。" % topic
    if category["key"] == "food":
        return "抖音热点识别到“%s”与美食消费相关，可转化为口味特点、推荐菜、排队建议和周边游玩组合。" % topic
    if category["key"] == "hotel":
        return "抖音热点识别到“%s”与住宿/度假相关，可整理为位置、价格、适合人群和周边动线建议。" % topic
    # 灵感卡简介：只讲这条热点本身能带来什么灵感，不做"非在售/参考定价"一类免责声明
    # （免责提示由价格位下方的"非实物 · 仅供参考"标签单独承载）
    theme = category.get("cat") or category.get("label") or "文创"
    return (
        "“%s”是抖音当期热门内容，热度 %s，属于%s方向。"
        "它可以作为一条文创选题：从热点里提炼可视化的符号、场景和情绪，"
        "转成能带得走、能展示、能被分享的小产品与包装主题。"
        % (topic, _format_heat(heat), theme)
    )


# 动态热点卡的产品化建议：按分类给出可落地的品类 / 工艺 / 渠道常识，
# 只写行业通用做法，不编造具体城市或机构的事实（反幻觉约定）。
_INSPIRE_PRODUCT_IDEAS: Dict[str, Dict[str, str]] = {
    "博物馆": {
        "forms": "冰箱贴、金属徽章、纹样书签、迷你摆件",
        "craft": "用金属压铸或滴胶工艺还原主视觉，配色直接取热点画面里的建筑与文物色彩",
        "channel": "博物馆出口文创店、景区文创柜台，配「打卡同款」主题陈列",
    },
    "非遗": {
        "forms": "手作体验材料包、联名饰品、节令伴手礼盒",
        "craft": "与在地手艺人做小批量联名，把手工痕迹本身当卖点讲",
        "channel": "非遗工坊体验课、景区快闪店、民宿前台展售",
    },
    "国潮": {
        "forms": "盲盒、徽章、帆布袋、贴纸包",
        "craft": "走潮流插画 + 高饱和配色的系列款，做成可收集的组合",
        "channel": "市集摊位、潮玩店寄售、抖音 / 小红书店铺带货",
    },
    "节庆": {
        "forms": "限定礼盒、主题包装食品、节庆饰品",
        "craft": "包装突出节日氛围与在地符号，走限时限量策略",
        "channel": "节庆市集、景区门店节日档期、伴手礼专区",
    },
    "文创IP": {
        "forms": "冰箱贴、明信片、徽章、小型摆件",
        "craft": "选热点里最出片的一个视觉符号做主视觉，小件起量控成本",
        "channel": "景区文创店、市集摊位、内容平台直播带货",
    },
}
_INSPIRE_IDEA_DEFAULT = "文创IP"


def _hot_product_idea(cat: str) -> Dict[str, str]:
    return _INSPIRE_PRODUCT_IDEAS.get(cat) or _INSPIRE_PRODUCT_IDEAS[_INSPIRE_IDEA_DEFAULT]


# ===== 顾客视角文案（数字名片正面内容）=====
# 名片是印在包装上给**顾客**扫的，所以「背后的文化故事 / 设计与工艺 / 亮点速览」
# 必须是介绍作品本身的文字，不能用“给顾客讲…”“转化”“爆款”这类运营口吻。
# 只讲该品类普适且可核实的文化背景与工艺做法，不编造具体年代、奖项、传承人。
_INSPIRE_PRODUCT_CULTURE: Dict[str, str] = {
    "博物馆": (
        "{region}的博物馆文创，大多是从展柜里的一件实物出发的——器物的造型、纹样的走向、"
        "釉色与砖色的层次，都是能落到一件小物上的设计语言。它们原本属于某个具体年代，"
        "被做成可以随身带走的样子之后，历史就不再隔着玻璃：别在包上、贴在冰箱上，"
        "等于把一段地方记忆收进了自己的日常。博物馆文创最动人的地方也在这里——"
        "文化不必被讲解，被带走就够了。"
    ),
    "非遗": (
        "{region}这类手艺的价值，从来不在“老”，而在“手上真做过”。一针一线、一编一织，"
        "匠人手上留下的细微痕迹，让同一款作品也会有微小的差别，这正是机器做不出来的部分。"
        "把非遗从工坊带进日常，靠的不是复古，而是好用：戴得上身、放得上桌、送得出手，"
        "手艺才被更多人记住，也才有机会再传一代。"
    ),
    "国潮": (
        "国潮好看的地方，是把中国人熟悉的东西重新排了一遍版：朱红、靛蓝、石绿这些传统色，"
        "配上利落的比例与留白，看着既眼熟又不显老气。它的文化根基是传统纹样与器物美学——"
        "云纹、回纹、缠枝、青花，本来就有很强的图形感，重新组合之后，正好接上今天"
        "「想要一点文化底色，但不想要隆重感」的心情。"
    ),
    "节庆": (
        "节令之所以让人觉得特别，是因为它一年只在这几天发生：人们用吃食、装饰与赠礼，"
        "把普通日子和值得记住的日子分开。这类作品的文化根基就来自这套节令习俗，"
        "{region}的日常里也有自己的一套过法。做成一件可以反复使用的小物，"
        "节日过去也不会闲置，倒成了下一次节气的提醒。"
    ),
    "文创IP": (
        "{region}被记住，往往不是因为宏大叙事，而是因为一个具体的小符号：一处建筑的轮廓、"
        "一枚纹样、一句本地人一听就懂的话。真正耐看的文创都建立在这种“一眼认出”的熟悉感上——"
        "本地人会心一笑，外地人会追问一句“这是什么”。像「{hook}」这类内容能火，"
        "靠的也是这种具体感；把它做成一件能带走的小物，热度才不会过期。"
    ),
}

_INSPIRE_PRODUCT_DESIGN: Dict[str, str] = {
    "博物馆": (
        "主视觉取自画面里最有辨识度的建筑轮廓与文物纹样，形态首选冰箱贴、金属徽章、"
        "纹样书签或迷你摆件。工艺上用金属压铸或滴胶还原立体起伏，表面做哑光或轻微做旧，"
        "颜色按实物的砖色、釉色与纹样配色取色，尽量不加滤镜——看起来像把一件小展品"
        "从展柜里请了出来。"
    ),
    "非遗": (
        "以在地手作技艺为工艺基础，与手艺人合作限量出品，手工留下的细微痕迹本身就是"
        "识别特征，不做过度统一。视觉上收敛配色、突出材质本身，常见形态是手作体验材料包、"
        "联名饰品与节令伴手礼盒——摸得到的纹理，就是它最直接的工艺说明。"
    ),
    "国潮": (
        "视觉走潮流插画加高饱和配色的系列款，同一组传统符号做成可收集的组合：盲盒、徽章、"
        "帆布袋、贴纸包。工艺以印刷与后道处理为主，靠色彩叠加、纸张与油墨质感拉开层次，"
        "一套摆在一起才完整，单件拿出来也好看。"
    ),
    "节庆": (
        "第一眼是包装：以节令符号与在地纹样做限定视觉，材质上用纸盒搭配烫金、烫银这类"
        "仪式感更强的工艺，配限时限量的编号。内里的食品或饰品都挑得实用——能吃完、"
        "或用得住，不会拆完包装就闲置。"
    ),
    "文创IP": (
        "选画面里最出片的一个视觉符号做主视觉，做成冰箱贴、明信片、徽章或小型摆件这类小件。"
        "工艺以金属压铸、滴胶与丝印为主，细节重点放在边缘倒角、色块层次与手感上——"
        "小巧不占地，实物比图片更耐看。"
    ),
}

# 亮点速览：名片正面给顾客看的 3~4 句卖点（不是给运营者的建议）
_INSPIRE_SELLING_POINTS: Dict[str, List[str]] = {
    "博物馆": ["取材自{region}的馆藏纹样与器物色彩", "小件轻便，行李不占地方，回去能一直用", "配色对着实物取色，摆出来就是一件小展品"],
    "非遗": ["在地手作技艺制作，每件都有细微差别", "戴得上身、放得上桌，日常真的用得到", "送人自带话题，比普通伴手礼更有来历"],
    "国潮": ["传统纹样重新配色，眼熟但不老气", "可以收集的系列款，越凑越好看", "不挑场合，日常穿搭就能带出门"],
    "节庆": ["{region}限定主题，只有这一季才有", "包装有仪式感，拆开就想拍照", "过了节也不闲置，还能接着用"],
    "文创IP": ["从{region}最出片的符号提炼而来", "一眼认出是哪座城市，拍照很上镜", "小件好携带，是省地方的旅行纪念"],
}


def _hot_culture_text(cat: str, region: str, hook: str) -> str:
    """顾客视角的文化故事（100~150 字），讲作品取材的真实文化背景。"""
    tpl = _INSPIRE_PRODUCT_CULTURE.get(cat) or _INSPIRE_PRODUCT_CULTURE[_INSPIRE_IDEA_DEFAULT]
    return tpl.replace("{region}", region or "当地").replace("{hook}", hook or "").strip()


def _hot_design_text(cat: str) -> str:
    """顾客视角的设计与工艺（100~150 字），讲材质、工艺与视觉符号本身。"""
    return _INSPIRE_PRODUCT_DESIGN.get(cat) or _INSPIRE_PRODUCT_DESIGN[_INSPIRE_IDEA_DEFAULT]


def _hot_selling_points(cat: str, region: str) -> List[str]:
    """顾客视角的亮点速览（名片正面用），与运营向的 `inspire` 建议分开。"""
    tpl = _INSPIRE_SELLING_POINTS.get(cat) or _INSPIRE_SELLING_POINTS[_INSPIRE_IDEA_DEFAULT]
    return [t.replace("{region}", region or "当地") for t in tpl]


def _build_official_cards(item: Dict[str, Any], idx: int, city: str, keyword: str, refresh_token: str) -> Optional[Dict[str, Dict[str, Any]]]:
    category = _classify_tourism_item(item, city)
    if not category:
        return None

    topic = _extract_core_theme(item, category, city)
    image = _select_contextual_image(item, category, topic, city)
    summary = _build_card_summary(item, category, topic, city)
    heat = item.get("heat") or (3600 + idx * 520)
    token = re_slug("%s-%s-%s" % (refresh_token, topic, idx))[:34]
    source_url = item.get("url") or "https://www.douyin.com/search/%s" % quote(topic)
    # 社区卡：内容本身带真实封面就用真实封面，否则用同一张 AI 概念图兜底
    cover = str(item.get("cover") or "").strip()
    cover_ok = cover.startswith(("http://", "https://", "data:image"))
    # 热点发生地（如热点本身带城市，就用热点城市；否则退回当前选择城市）
    region = str(item.get("region") or city or "当地")

    community = {
        "id": "douyin-hot-post-%s" % token,
        "type": category["type"],
        "title": topic,
        "author": "抖音热点识别",
        "tag": category["label"],
        "views": int(float(str(heat).replace("万", "0000"))) if str(heat).replace(".", "", 1).isdigit() else 3600 + idx * 680,
        "favs": 420 + idx * 86,
        "image": cover if cover_ok else image,
        "content": summary,
        "source": "official",
        "source_url": source_url,
        "category": category["key"],
        "image_mode": "real" if cover_ok else "ai",
    }
    note = {
        "note_id": "douyin_%s" % token,
        "title": topic,
        "source_url": source_url,
        "source_type": "official_hot",
        "destination": city,
        "city": city,
        "province": "",
        "theme": category["label"],
        "crowd": "大众游客",
        "season": "实时热点",
        "budget_level": "按实际产品组合估算",
        "duration_suggestion": "适合半日到两日内容组合",
        "summary": summary,
        "highlights": [
            "产品方向：把热点「%s」转成%s方向的可售产品（%s）" % (topic, category["cat"], _hot_product_idea(category["cat"])["forms"]),
            "文案钩子：主标题直接沿用热点原话「%s」，再补「%s限定」制造在地稀缺感" % (_compact_text(str(item.get("title") or topic), 24), region),
            "热度 %s 处于讨论上升期：适合在热度期内快速上新或跟进内容" % _format_heat(heat),
        ],
        "pitfalls": ["热点内容需人工复核后再用于正式营销文案", "不要复制平台用户原文或评论"],
        "tags": [region, category["cat"], item.get("region") or city, "抖音热点"],
        "score": 99 - idx,
        "compliance_note": "仅保存公开入口、摘要和标签；完整内容跳转原平台查看。",
    }
    idea = _hot_product_idea(category["cat"])
    hook = _compact_text(str(item.get("title") or topic), 24)
    product = {
        "id": "douyin_hot_product_%s" % token,
        "name": "%s·%s" % (_compact_text(topic, 16), category["cat"]),
        "brand": "抖音热点 · %s" % region,
        "cat": category["cat"],
        "img": image,
        "image_mode": "ai",
        "price": None,
        "priceNote": "非实物 · 仅供参考",
        "sales": "抖音热度 %s" % _format_heat(heat),
        "badge": "hot" if idx < 2 else "new",
        "badgeText": "热点灵感",
        "tags": [category["cat"], region, "抖音热点"],
        "desc": summary,
        "design": _hot_design_text(category["cat"]),
        "culture": _hot_culture_text(category["cat"], region, hook),
        "audience": "被「%s」内容种草的年轻游客与本地周末客群；喜欢收集%s的文创爱好者；到%s旅行想带走一件「有梗又有故事」伴手礼的人。" % (topic, idea["forms"].split("、")[0], region),
        # 名片正面给顾客看的卖点（与下面的运营向 inspire 建议分开，避免运营口吻进名片）
        "selling_points": _hot_selling_points(category["cat"], region),
        "inspire": [
            "产品方向：把「%s」做成%s方向的可售产品，首推%s" % (topic, category["cat"], idea["forms"]),
            "工艺与成本：%s；先小批量试款，跑得动再加单" % idea["craft"],
            "销售场景：%s" % idea["channel"],
            "文案钩子：主标题直接用热点原话「%s」，配「%s限定」稀缺词；当前热度%s，热度期内上新转化最好" % (hook, region, _format_heat(heat)),
        ],
        "dynamic": True,
    }
    return {"community": community, "note": note, "product": product}


def _search_plan(refresh_token: str, city: str) -> List[tuple]:
    """按 refresh_token 生成这一轮的检索词计划。

    规则：每个分类的检索词按种子随机挑一半（不再是固定取首词），再按分类轮询交错排列 ——
    这样既保证五类都能覆盖到，又保证每次更新用的是**不同的检索词组合**，
    从根上解决"第二次更新没有新内容"。
    """
    seed = "%s|%s" % (refresh_token or "", city or "")
    groups: Dict[str, List[tuple]] = {}
    order: List[str] = []
    for kw, cat in INSPIRE_SEARCH_KEYWORDS:
        if cat not in groups:
            groups[cat] = []
            order.append(cat)
        groups[cat].append((kw, cat))

    picked: Dict[str, List[tuple]] = {}
    for cat in order:
        words = groups[cat][:]
        take = max(1, (len(words) + 1) // 2)          # 两类词取 1，七类词取 4
        rng = random.Random("%s|%s" % (seed, cat))
        rng.shuffle(words)
        picked[cat] = words[:take]

    plan: List[tuple] = []
    cursor = 0
    while any(picked[cat] for cat in order):
        cat = order[cursor % len(order)]
        cursor += 1
        if picked[cat]:
            plan.append(picked[cat].pop(0))
    return plan


TIKHUB_HOT_ENDPOINT = "/api/v1/douyin/billboard/fetch_hot_city_list"


def _harvest_hot_rows(base_url: str, api_key: str, keywords: List[str], pages: List[int],
                      budget: int, seen: Optional[set] = None) -> tuple:
    """按检索词逐个抓抖音热点原始条目，返回 (条目列表, 错误列表, 实际请求数)。"""
    items: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen = seen if seen is not None else set()
    requests_made = 0
    for kw in keywords:
        if requests_made >= budget:
            break
        for page in pages:
            if requests_made >= budget:
                break
            requests_made += 1
            try:
                rows = _call_tikhub_get_endpoint(
                    base_url, api_key, TIKHUB_HOT_ENDPOINT,
                    {"page": page, "page_size": 10, "order": "desc", "keyword": kw},
                )
            except Exception as exc:
                errors.append("keyword=%s(page%s)：%s" % (kw, page, exc))
                continue
            for row in rows:
                fingerprint = _title_fingerprint(row.get("title"))
                if not fingerprint or fingerprint in seen:
                    continue
                seen.add(fingerprint)
                row["inspire_keyword"] = kw
                row["source_endpoint"] = "hot_search"
                items.append(row)
    return items, errors, requests_made


def _validate_batch(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对抓到的原始热点逐条做文创可用性二次验证，并把分类结论写回条目。"""
    validated: List[Dict[str, Any]] = []
    for item in items:
        verdict = _validate_inspire_item(item)
        if not verdict:
            continue
        item["inspire_cat"] = verdict["theme"]
        item["inspire_score"] = verdict["score"]
        item["inspire_hits"] = verdict["hits"]
        item["inspire_signals"] = verdict["signals"]
        validated.append(item)
    return validated


def _warm_pool_async(seed: str, city: str, used_keywords: List[str]) -> None:
    """后台补池：把本轮没用到的检索词再抓一遍，让下一次"更新热点"有新鲜内容。

    只在后台跑，不影响接口响应时间；同时用标志位保证同一时刻只有一个补池任务。
    """
    global _INSPIRE_WARMING
    with _INSPIRE_WARM_LOCK:
        if _INSPIRE_WARMING:
            return
        _INSPIRE_WARMING = True

    def worker() -> None:
        global _INSPIRE_WARMING
        try:
            api_key = _env("TIKHUB_API_KEY") or _env("TIKHUB_DOUYIN_API_KEY")
            base_url = _env("TIKHUB_DOUYIN_BASE_URL") or _env("TIKHUB_BASE_URL") or TIKHUB_DEFAULT_BASE_URL
            if not api_key:
                return
            plan = [kw for kw, _hint in _search_plan("%s|warm" % seed, city) if kw not in used_keywords]
            if not plan:
                return
            rows, _errors, _n = _harvest_hot_rows(base_url, api_key, plan, [1], budget=12)
            validated = _validate_batch(rows)
            if validated:
                _pool_merge(validated)
                _pool_save()
                print("[inspire] 后台补池：新增验证通过 %d 条，池内共 %d 条" % (len(validated), _pool_size()))
        except Exception as exc:
            print("[inspire] 后台补池失败: %s" % exc)
        finally:
            with _INSPIRE_WARM_LOCK:
                _INSPIRE_WARMING = False

    threading.Thread(target=worker, name="inspire-pool-warm", daemon=True).start()


def fetch_tikhub_douyin_hotspots(keyword: str, city: str, target: int = 10,
                                 refresh_token: str = "") -> Dict[str, Any]:
    """按"国潮/非遗/博物馆/文创IP/节庆"主题词池抓抖音热点，再做第二次验证。

    与旧实现的三点关键区别：
      1. **检索词按 refresh_token 轮换**：每轮从每个分类里换一批词、并轮换翻页页码，
         所以每次点"更新热点"拿到的是一批**不同**的热点 —— 解决"第二次更新就没有新内容"；
      2. **抓取后二次验证**：`_validate_inspire_item` 逐条判断能否用于文创营销
         （剔除新闻/负面/与文创无关的热搜），并按**内容**重新判定它属于哪个灵感分类；
      3. **分类均衡取样**：按分类轮询取卡，保证五类都有代表，不会整类被挤掉。

    实测（2026-09-20）：抖音热点检索是"句子包含检索词"的匹配方式，
    "国潮/文创/节庆/伴手礼/灯会"等宽泛词返回空，所以词池用的是实测有命中的近义词。
    """
    api_key = _env("TIKHUB_API_KEY") or _env("TIKHUB_DOUYIN_API_KEY")
    base_url = _env("TIKHUB_DOUYIN_BASE_URL") or _env("TIKHUB_BASE_URL") or TIKHUB_DEFAULT_BASE_URL

    if not api_key:
        return _unconfigured(
            "TikHub 抖音",
            ["TIKHUB_API_KEY"],
            "未配置 TikHub API Key。配置后将优先调用 TikHub 抖音榜单接口获取实时热点，失败时回退到本地市场趋势包。",
        )

    target = max(4, int(target or 10))
    harvest_goal = target * 3           # 二次验证会淘汰一部分，先多抓一些
    request_budget = 10                 # 控制单次更新的接口调用量（约 15s）

    plan = _search_plan(refresh_token, city)
    plan_keywords = [kw for kw, _hint in plan]
    seen_fingerprints: set = set()

    items, errors, requests_made = _harvest_hot_rows(
        base_url, api_key, plan_keywords, [1], request_budget, seen_fingerprints)

    # 翻页补充：实测第 2 页有 25%~60% 是新内容，页码按 refresh_token 轮换
    if len(items) < harvest_goal and requests_made < request_budget:
        deep_seed = "%s|%s|deep" % (refresh_token or "", city or "")
        deep_page = 2 + int(hashlib.md5(deep_seed.encode("utf-8")).hexdigest()[:4], 16) % 3   # 2 / 3 / 4
        extra_items, extra_errors, extra_requests = _harvest_hot_rows(
            base_url, api_key, plan_keywords, [deep_page],
            request_budget - requests_made, seen_fingerprints)
        items.extend(extra_items)
        errors.extend(extra_errors)
        requests_made += extra_requests

    # ── 第二次验证：只留下能用于文创营销的内容，并按内容重新判定分类 ──
    validated = _validate_batch(items)
    rejected = len(items) - len(validated)

    # 并入滚动池：池子里"还没展示过"的会优先放出来，保证连续更新看到的是新卡片
    pool_size = _pool_merge(validated) if validated else _pool_size()
    if validated:
        _pool_save()
    picked = _pool_pick(target + 2, "%s|%s|pool" % (refresh_token or "", city or "")) if pool_size else []

    # 后台继续补池，供下一次"更新热点"使用（不阻塞本次响应）
    _warm_pool_async(refresh_token or city, city, plan_keywords)

    if picked:
        return {
            "provider": "TikHub 抖音",
            "status": "ok" if not errors else "partial",
            "message": "抖音热点检索命中 %d 条，二次验证通过 %d 条，本轮放出 %d 条（滚动池共 %d 条）"
                       % (len(items), len(validated), len(picked), pool_size),
            "items": picked,
            "errors": errors[:3],
            "trace": {
                "raw": len(items),
                "validated": len(validated),
                "rejected": rejected,
                "pool": pool_size,
                "requests": requests_made,
                "keywords": [kw for kw, _ in plan],
            },
        }

    return {
        "provider": "TikHub 抖音",
        "status": "error",
        "message": "本轮抖音热点均未通过文创可用性二次验证（共 %d 条原始热点被剔除），已回退本地市场趋势包" % len(items)
                   if items else "TikHub 抖音接口调用失败，已回退本地市场趋势包：%s" % ("；".join(errors[:3]) or "未返回可用热点"),
        "items": [],
        "errors": errors[:5],
        "trace": {"raw": len(items), "rejected": rejected, "requests": requests_made},
    }


def _call_official_endpoint(
    provider: str,
    endpoint: str,
    token: str,
    keyword: str,
    city: str,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    if extra_headers:
        headers.update(extra_headers)

    params = {"keyword": keyword, "city": city, "scene": "culture_tourism"}
    response = requests.get(endpoint, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    return {
        "provider": provider,
        "status": "ok",
        "message": "已通过官方/授权接口返回数据",
        "items": _normalize_items(provider, payload),
    }


def _unconfigured(provider: str, required: List[str], message: str) -> Dict[str, Any]:
    return {
        "provider": provider,
        "status": "unconfigured",
        "message": message,
        "required_env": required,
        "items": [],
    }


def fetch_douyin_hotspots(keyword: str, city: str, target: int = 10, refresh_token: str = "") -> Dict[str, Any]:
    tikhub = fetch_tikhub_douyin_hotspots(keyword, city, target, refresh_token)
    if tikhub.get("items"):
        return tikhub

    endpoint = _env("DOUYIN_HOTSPOT_ENDPOINT")
    token = _env("DOUYIN_ACCESS_TOKEN") or _env("DOUYIN_CLIENT_TOKEN")
    client_key = _env("DOUYIN_CLIENT_KEY")

    if not endpoint:
        tikhub["provider"] = "抖音"
        tikhub["required_env"] = ["TIKHUB_API_KEY", "或 DOUYIN_HOTSPOT_ENDPOINT + DOUYIN_ACCESS_TOKEN/DOUYIN_CLIENT_TOKEN"]
        return tikhub

    headers = {}
    if client_key:
        headers["X-Client-Key"] = client_key

    try:
        result = _call_official_endpoint("抖音", endpoint, token, keyword, city, headers)
        result["message"] = "TikHub 未返回可用数据，已通过兼容的抖音官方/授权接口返回数据"
        return result
    except Exception as exc:
        return {
            "provider": "抖音",
            "status": "error",
            "message": "抖音官方接口调用失败：%s" % exc,
            "items": [],
        }


def fetch_xiaohongshu_hotspots(keyword: str, city: str) -> Dict[str, Any]:
    endpoint = _env("XHS_HOTSPOT_ENDPOINT")
    token = _env("XHS_ACCESS_TOKEN")
    app_id = _env("XHS_APP_ID")

    if not endpoint:
        return _unconfigured(
            "小红书",
            ["XHS_HOTSPOT_ENDPOINT", "XHS_ACCESS_TOKEN"],
            "未配置小红书官方/授权热点接口。请通过小红书官方商业合作、品牌合作或已授权数据服务获取接口与授权令牌后再接入。",
        )

    headers = {}
    if app_id:
        headers["X-App-Id"] = app_id

    try:
        return _call_official_endpoint("小红书", endpoint, token, keyword, city, headers)
    except Exception as exc:
        return {
            "provider": "小红书",
            "status": "error",
            "message": "小红书官方接口调用失败：%s" % exc,
            "items": [],
        }


def get_social_hotspots(keyword: str = "文旅", city: str = "杭州", target: int = 10,
                        refresh_token: str = "") -> Dict[str, Any]:
    keyword = (keyword or "文旅").strip()
    city = (city or "杭州").strip()
    sources = [
        fetch_douyin_hotspots(keyword, city, target, refresh_token),
        fetch_xiaohongshu_hotspots(keyword, city),
    ]

    items: List[Dict[str, Any]] = []
    for source in sources:
        items.extend(source.get("items", []))

    return {
        "official_only": False,
        "api_provider_policy": "official_or_authorized_api_only",
        "keyword": keyword,
        "city": city,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sources": sources,
        "items": items[:40],
    }


MARKET_TREND_PACKS: List[Dict[str, Any]] = [
    {
        "title": "国潮文博冰箱贴持续升温，年轻用户偏好“可展示的小文物”",
        "city": "北京",
        "theme": "文创非遗",
        "crowd": "Z世代",
        "tags": ["国潮", "博物馆", "冰箱贴", "打卡"],
        "image": "",
        "summary": "近期文博类小件文创更容易形成社交传播，尤其是冰箱贴、金属书签、徽章等低门槛产品，适合和城市打卡路线绑定。",
        "highlights": ["低客单价便于冲动消费", "适合社媒晒图与收藏展示", "可和博物馆夜场、盖章路线联动"],
        "pitfalls": ["避免只做图案搬运", "需要讲清楚文物元素来源"],
        "score": 96,
    },
    {
        "title": "非遗手作体验从“看展”转向“带走作品”",
        "city": "苏州",
        "theme": "非遗体验",
        "crowd": "亲子家庭",
        "tags": ["非遗", "手作", "研学", "亲子"],
        "image": "",
        "summary": "亲子和研学客群更关注参与感，苏绣、扎染、拓印、香囊等体验型内容适合进入社区攻略和文创营销脚本。",
        "highlights": ["让用户亲手完成作品", "适合半日研学产品", "可沉淀为短视频教程内容"],
        "pitfalls": ["控制单次体验时长", "材料包要有清晰成品预期"],
        "score": 92,
    },
    {
        "title": "城市礼物礼盒化，地域特产正在升级为审美消费",
        "city": "杭州",
        "theme": "城市礼物",
        "crowd": "商务出行",
        "tags": ["城市礼物", "茶礼", "伴手礼", "宋韵"],
        "image": "",
        "summary": "茶、糕点、香氛等地域特产更适合用城市美学包装升级，形成节庆送礼和商务伴手礼场景。",
        "highlights": ["地域符号可视化", "适合节庆档期", "可和酒店前台、景区商店联动"],
        "pitfalls": ["避免过度包装", "产品故事要和真实产地关联"],
        "score": 89,
    },
    {
        "title": "夜游内容带动拍照机位、餐饮和文创联动消费",
        "city": "西安",
        "theme": "夜游打卡",
        "crowd": "情侣伴侣",
        "tags": ["夜游", "汉服", "发簪", "拍照"],
        "image": "",
        "summary": "夜游目的地正在从“看灯”升级为“妆造+拍照+餐饮+文创”的组合消费，适合社区攻略做完整线路推荐。",
        "highlights": ["拍照机位可驱动浏览", "汉服妆造带动配饰消费", "夜间餐饮可承接转化"],
        "pitfalls": ["注意排队与人流控制", "路线不要安排过密"],
        "score": 94,
    },
    {
        "title": "考古盲盒和互动式文创仍具传播潜力",
        "city": "郑州",
        "theme": "文博互动",
        "crowd": "大学生",
        "tags": ["考古", "盲盒", "互动", "文博"],
        "image": "https://www.chnmus.net/webfile/sitesources/hnsbwy/upload/202009/20200921171823114.jpg",
        "summary": "互动式文创把购买动作变成可分享的过程，适合通过开箱、挖掘、复刻等体验增强用户停留和二次传播。",
        "highlights": ["适合开箱短视频", "亲子与大学生客群都能接受", "文物知识可自然植入"],
        "pitfalls": ["盲盒机制要避免过度博彩化", "复刻件需要清晰标注来源"],
        "score": 91,
    },
    {
        "title": "节气限定与传统节日文创适合做滚动上新",
        "city": "全国",
        "theme": "节庆营销",
        "crowd": "大众游客",
        "tags": ["节气", "中秋", "茶宠", "限定"],
        "image": "https://upload.wikimedia.org/wikipedia/commons/8/80/Tuerye_-_Beijing_Rabbit_God.jpg",
        "summary": "节气、花朝、中秋、端午等节点能给文创产品提供持续上新理由，适合自动生成不同档期的营销素材。",
        "highlights": ["节点明确便于排期", "限定款利于制造紧迫感", "可和茶空间、民宿、夜市结合"],
        "pitfalls": ["避免每个节日都同质化换包装", "需要提前规划库存和预售"],
        "score": 88,
    },
    {
        "title": "城市 Citywalk 攻略从景点清单转向慢游路线",
        "city": "上海",
        "theme": "旅游规划攻略",
        "crowd": "年轻游客",
        "tags": ["Citywalk", "路线", "街区", "拍照"],
        "image": "",
        "summary": "用户更关注不赶路、可拍照、有咖啡和小店停留点的路线，适合把老街、展馆、餐饮和文创店串联成半日攻略。",
        "highlights": ["路线顺序清晰", "适合半日慢游", "可联动街区文创小店"],
        "pitfalls": ["避免路线过密", "标注步行距离和休息点"],
        "score": 93,
    },
    {
        "title": "地方小吃合集成为短途游决策入口",
        "city": "成都",
        "theme": "美食推荐信息",
        "crowd": "周末短途游客",
        "tags": ["美食", "小吃", "夜市", "烟火气"],
        "image": "",
        "summary": "游客常用美食作为目的地选择入口，适合把小吃街、老字号和附近景点组合成“吃完顺路玩”的攻略卡片。",
        "highlights": ["突出味道和排队建议", "适合夜游路线", "可联动伴手礼"],
        "pitfalls": ["避免只列店名", "需说明口味和交通"],
        "score": 95,
    },
    {
        "title": "博物馆夜场和临展正在带动周边游",
        "city": "南京",
        "theme": "景点推荐信息",
        "crowd": "文化兴趣用户",
        "tags": ["博物馆", "夜游", "展览", "文创"],
        "image": "",
        "summary": "夜场、临展和盖章路线能延长游客停留时间，适合生成展览亮点、预约提醒和周边餐饮动线。",
        "highlights": ["适合文化打卡", "可带动文创消费", "适合雨天备选路线"],
        "pitfalls": ["提醒预约和闭馆时间", "避免忽略交通返程"],
        "score": 90,
    },
    {
        "title": "亲子住宿更关注亲水、公园和研学资源",
        "city": "广州",
        "theme": "住宿推荐信息",
        "crowd": "亲子家庭",
        "tags": ["酒店", "亲子", "公园", "研学"],
        "image": "",
        "summary": "亲子家庭选择住宿时更关注周边公园、博物馆和餐饮便利度，适合把酒店推荐和一日研学路线合并展示。",
        "highlights": ["适合周末亲子游", "住宿和景点一起推荐", "减少换乘成本"],
        "pitfalls": ["标注亲子设施", "说明噪音和早餐情况"],
        "score": 87,
    },
    {
        "title": "山水徒步路线需要补充交通和体力分级",
        "city": "桂林",
        "theme": "旅游规划攻略",
        "crowd": "户外游客",
        "tags": ["徒步", "山水", "交通", "路线"],
        "image": "",
        "summary": "自然风景类攻略不能只展示美图，应补充交通、体力等级、补给点和返程时间，减少用户实际出行落差。",
        "highlights": ["路线可执行", "适合户外内容卡片", "可联动装备文创"],
        "pitfalls": ["注意天气风险", "避免夸大难度或轻松程度"],
        "score": 86,
    },
    {
        "title": "老街咖啡和在地小店适合做轻量景点推荐",
        "city": "杭州",
        "theme": "景点推荐信息",
        "crowd": "情侣和闺蜜游客",
        "tags": ["老街", "咖啡", "拍照", "小店"],
        "image": "",
        "summary": "老街小店、咖啡馆和手作店能形成轻量目的地，适合做“拍照机位+下午茶+文创小物”的短路线。",
        "highlights": ["适合拍照分享", "客单价低", "可带动街区消费"],
        "pitfalls": ["避免过度滤镜化", "补充营业时间和人流情况"],
        "score": 89,
    },
]


def _trend_window(limit: int = 5, seed: str = "") -> List[Dict[str, Any]]:
    seed_score = sum(ord(ch) for ch in str(seed or ""))
    today_index = (datetime.now().timetuple().tm_yday + seed_score) % len(MARKET_TREND_PACKS)
    ordered = MARKET_TREND_PACKS[today_index:] + MARKET_TREND_PACKS[:today_index]
    return ordered[:limit]


def _note_from_trend(trend: Dict[str, Any], idx: int) -> Dict[str, Any]:
    slug = re_slug("%s-%s" % (trend["city"], trend["title"]))
    return {
        "note_id": "market_%s" % slug[:28],
        "title": trend["title"],
        "source_url": "https://example.com/market-hot/%s" % slug,
        "source_type": "market_hot",
        "destination": trend["city"],
        "city": trend["city"] if trend["city"] != "全国" else "杭州",
        "province": "",
        "theme": trend["theme"],
        "crowd": trend["crowd"],
        "season": "实时热点",
        "budget_level": "按产品组合浮动",
        "duration_suggestion": "适合半日到一日内容组合",
        "summary": trend["summary"],
        "highlights": trend["highlights"],
        "pitfalls": trend["pitfalls"],
        "tags": trend["tags"],
        "score": trend["score"] - idx,
        "compliance_note": "市场趋势模拟数据：未复制平台正文、图片或评论，可在配置官方/授权接口后替换为实时平台数据。",
    }


def _community_from_trend(trend: Dict[str, Any], idx: int) -> Dict[str, Any]:
    category = _classify_tourism_item({"title": trend["title"], "summary": trend["summary"]}, trend["city"]) or {
        "key": "creative",
        "label": trend["theme"],
        "type": "culture",
        "cat": "文创IP",
        "color": TOURISM_CATEGORIES["creative"]["color"],
    }
    image = _cartoon_image(trend["title"], category, trend["city"])
    return {
        "id": "market-post-%s" % idx,
        "type": category["type"],
        "title": trend["title"],
        "author": "市场热点观察",
        "tag": trend["theme"],
        "views": int(trend["score"]) * 37,
        "favs": int(trend["score"]) * 9,
        "image": image,
        "content": trend["summary"],
        "source": "market_hot",
        "image_mode": "cartoon",
    }


def _product_from_trend(trend: Dict[str, Any], idx: int) -> Dict[str, Any]:
    cat = "文创IP"
    if "非遗" in trend["theme"]:
        cat = "非遗"
    elif "节庆" in trend["theme"] or "城市礼物" in trend["theme"]:
        cat = "节庆"
    elif "文博" in trend["theme"] or "博物馆" in trend["tags"]:
        cat = "博物馆"
    category = _classify_tourism_item({"title": trend["title"], "summary": trend["summary"]}, trend["city"]) or {
        "key": "creative",
        "label": trend["theme"],
        "type": "culture",
        "cat": cat,
        "color": TOURISM_CATEGORIES["creative"]["color"],
    }
    # 配图统一走 AI 生图（懒加载，由 /api/inspire/image 生成并缓存），不再出现卡通占位图
    image = inspire_image_url(trend["title"], cat, trend["city"])
    image_mode = "ai"
    return {
        "id": "hot_%s_%s" % (datetime.now().strftime("%m%d"), idx),
        "name": trend["tags"][0] + "趋势灵感款",
        "brand": trend["city"] + "热门文旅",
        "cat": cat,
        "img": image,
        "image_mode": image_mode,
        "price": None,
        "priceNote": "非实物 · 仅供参考",
        "sales": "趋势热度 %s" % trend["score"],
        "badge": "hot" if idx < 2 else "new",
        "badgeText": "趋势观察",
        "tags": trend["tags"][:3],
        "desc": trend["summary"],
        "design": _hot_design_text(cat),
        "culture": _hot_culture_text(cat, trend["city"], trend["title"]),
        "audience": trend["crowd"],
        "selling_points": _hot_selling_points(cat, trend["city"]),
        "inspire": trend["highlights"][:4],
        "dynamic": True,
    }


def re_slug(text: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in text)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "hot"


def get_market_hot_inspirations(keyword: str = "文旅", city: str = "杭州", limit: int = 10, refresh_token: str = "") -> Dict[str, Any]:
    """聚合市场热点：优先用抖音真实热点产出 limit 张灵感卡片，不足时用本地趋势包补位。"""
    limit = max(4, min(int(limit or 10), 12))
    refresh_token = str(refresh_token or datetime.now().strftime("%Y%m%d%H%M"))
    official = get_social_hotspots(keyword=keyword, city=city, target=limit, refresh_token=refresh_token)

    # 官方热点卡片（抖音主题检索 + 二次验证通过的内容，按主题分类）
    official_cards: List[Dict[str, Dict[str, Any]]] = []
    official_items = official.get("items", [])
    if official_items:
        # 检索阶段已按 refresh_token 打散并做过分类均衡，这里再轻量轮换一次增加变化
        offset = int(hashlib.md5(("%s|rotate" % refresh_token).encode("utf-8")).hexdigest()[:6], 16) % max(1, len(official_items))
        rotated_items = official_items[offset:] + official_items[:offset]

        # 先按灵感分类分桶，再轮询取卡：保证国潮/非遗/博物馆/文创IP/节庆各有代表，
        # 避免"旋转 + 顺序截断"导致某一类被整类挤掉
        buckets: Dict[str, List[Dict[str, Dict[str, Any]]]] = {}
        cat_order: List[str] = []
        for idx, item in enumerate(rotated_items):
            cards = _build_official_cards(item, idx, city, keyword, refresh_token)
            if not cards:
                continue
            cat = cards["product"].get("cat") or "文创IP"
            if cat not in buckets:
                buckets[cat] = []
                cat_order.append(cat)
            buckets[cat].append(cards)

        while len(official_cards) < limit and any(buckets.values()):
            for cat in cat_order:
                if len(official_cards) >= limit:
                    break
                if buckets[cat]:
                    official_cards.append(buckets[cat].pop(0))

    # 趋势包只做补位：官方热点凑满 limit 张时不再混入趋势卡，保持数据来源纯净
    need = max(0, limit - len(official_cards))
    trend_fill = max(need, 3)
    trends = _trend_window(trend_fill, refresh_token)

    trend_posts = [_community_from_trend(item, idx) for idx, item in enumerate(trends)]
    trend_notes = [_note_from_trend(item, idx) for idx, item in enumerate(trends)]
    trend_products = [_product_from_trend(item, idx) for idx, item in enumerate(trends[:need])]

    community_posts = [cards["community"] for cards in official_cards] + trend_posts
    xhs_notes = [cards["note"] for cards in official_cards] + trend_notes
    creative_products = [cards["product"] for cards in official_cards] + trend_products

    return {
        "success": True,
        "keyword": keyword,
        "city": city,
        "refresh_token": refresh_token,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "official" if official_cards else "market_fallback",
        "sources": official.get("sources", []),
        "community_posts": community_posts[:limit + 4],
        "xhs_notes": xhs_notes[:limit + 4],
        "creative_products": creative_products[:limit + 4],
    }
