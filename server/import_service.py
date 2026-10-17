"""
SmartTourism · 外部内容导入服务
解析外部链接/文本内容，提取行程相关结构化信息

功能:
  - 接收 URL 或文本
  - 调用 LLM 提取目的地、天数、POI、预算
  - 输出标准 route_json 格式
  - 保存到数据库 imported_sources 表
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("import_service")
logger.setLevel(logging.DEBUG)

# ==================== 类型定义 ====================

ROUTE_JSON_TEMPLATE = {
    "destination": "",
    "days": 1,
    "budget": None,
    "pois": [],          # [{"name": "", "type": "景点/酒店/餐饮", "day": 1, "notes": ""}]
    "transport": "",     # 交通建议
    "notes": "",         # 其他备注
    "source": "",        # 来源 URL
    "extracted_at": "",  # 提取时间
}

POI_TYPE_MAP = {
    "景点": ["景点", "景区", "公园", "博物馆", "寺庙", "古镇", "海滩", "山", "湖",
             "乐园", "动物园", "植物园", "游乐园", "老街", "步行街"],
    "酒店": ["酒店", "宾馆", "民宿", "客栈", "青旅", "度假村", "旅馆"],
    "餐饮": ["餐厅", "饭店", "小吃", "美食", "夜市", "火锅", "面馆", "酒楼",
             "茶楼", "咖啡", "早餐", "海鲜", "烧烤"],
}


# ==================== URL 内容获取 ====================

def _fetch_url_content(url: str) -> Optional[str]:
    """
    尝试获取 URL 的文本内容

    Args:
        url: 网页 URL

    Returns:
        Optional[str]: 提取的文本内容，失败返回 None
    """
    try:
        import requests
        from bs4 import BeautifulSoup  # noqa: F401

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        resp.raise_for_status()

        # 尝试用 BeautifulSoup 提取正文
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, 'html.parser')

            # 移除 script/style 标签
            for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
                tag.decompose()

            text = soup.get_text(separator='\n', strip=True)
            # 清理多余空行
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            return '\n'.join(lines[:200])  # 限制 200 行
        except ImportError:
            # 无 BeautifulSoup 时返回原始文本
            return resp.text[:5000]

    except Exception as e:
        logger.warning(f"获取 URL 内容失败: {url} - {e}")
        return None


# ==================== LLM 提取 ====================

EXTRACTION_PROMPT = """你是一个旅行社的行程规划专家。请从以下内容中提取旅游行程相关的结构化信息。

内容来源: {source}

原始内容:
---
{content}
---

请提取以下信息并以 JSON 格式输出（只输出 JSON，不要其他文字）:

{{
  "destination": "目的地城市（如 杭州、三亚）",
  "days": 游玩天数（整数，如 3），
  "budget": 人均预算（整数，元，如 2000。如果无法确定则为 null），
  "pois": [
    {{
      "name": "景点/酒店/餐厅名称",
      "type": "景点 / 酒店 / 餐饮",
      "day": 第几天（整数，从 1 开始），
      "notes": "简短描述或推荐理由"
    }}
  ],
  "transport": "交通建议（如 高铁+当地包车）",
  "notes": "其他重要备注信息"
}}

要求:
- POI 名称要精确
- 如果内容中没有明确提到景点/酒店/餐厅，pois 可为空数组
- 天数默认 1
- 请确保 JSON 格式有效
"""


def extract_with_llm(
    content: str,
    call_llm_fn: Callable,
    source_url: str = "",
) -> Dict[str, Any]:
    """
    使用 LLM 从文本内容中提取结构化行程信息

    Args:
        content: 文本内容
        call_llm_fn: LLM 调用函数
        source_url: 来源 URL（用于标记）

    Returns:
        Dict: 标准 route_json 格式的结构化数据
    """
    # 截断过长内容
    truncated = content[:8000] if len(content) > 8000 else content

    source_label = source_url or "用户提供的文本"
    prompt = EXTRACTION_PROMPT.format(source=source_label, content=truncated)

    messages = [{"role": "user", "content": prompt}]

    try:
        resp = call_llm_fn(messages)
        if "error" in resp:
            logger.error(f"LLM 调用失败: {resp['error']}")
            return _fallback_extraction(content, source_url)

        response_text = resp["choices"][0]["message"]["content"]

        # 提取 JSON（可能包裹在 ```json ``` 中或混杂其他文字）
        json_str = _extract_json(response_text)
        if not json_str:
            logger.warning("LLM 回复中未找到 JSON，使用 fallback")
            return _fallback_extraction(content, source_url)

        data = json.loads(json_str)

        # 标准化输出
        route_json = dict(ROUTE_JSON_TEMPLATE)
        route_json.update({
            "destination": data.get("destination", ""),
            "days": int(data.get("days", 1)) or 1,
            "budget": int(data["budget"]) if data.get("budget") else None,
            "pois": data.get("pois", []),
            "transport": data.get("transport", ""),
            "notes": data.get("notes", ""),
            "source": source_url,
            "extracted_at": datetime.now().isoformat(),
        })

        return route_json

    except json.JSONDecodeError as e:
        logger.warning(f"JSON 解析失败: {e}")
        return _fallback_extraction(content, source_url)
    except Exception as e:
        logger.exception(f"LLM 提取失败: {e}")
        return _fallback_extraction(content, source_url)


def _extract_json(text: str) -> Optional[str]:
    """从文本中提取 JSON 字符串"""
    # 尝试 ```json ... ```
    match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if match:
        return match.group(1).strip()

    # 尝试纯 JSON 对象
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        # 简单验证：尝试解析
        candidate = match.group()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    return None


def _fallback_extraction(content: str, source_url: str = "") -> Dict[str, Any]:
    """
    规则兜底：当 LLM 提取失败时，用正则从文本中提取关键信息

    Returns:
        Dict: route_json
    """
    route_json = dict(ROUTE_JSON_TEMPLATE)
    route_json["source"] = source_url
    route_json["extracted_at"] = datetime.now().isoformat()

    # 城市列表
    cities = [
        '杭州', '三亚', '成都', '北京', '大理', '丽江', '上海', '广州', '深圳',
        '厦门', '桂林', '西安', '南京', '重庆', '青岛', '武汉', '长沙', '昆明',
        '张家界', '黄山', '九寨沟', '拉萨', '哈尔滨', '苏州', '乌镇', '温州',
    ]

    for city in cities:
        if city in content:
            route_json["destination"] = city
            break

    # 天数
    day_match = re.search(r'(\d+)\s*天', content)
    if day_match:
        route_json["days"] = int(day_match.group(1))

    # 预算
    budget_match = re.search(r'(\d+)\s*(元|块|万)?\s*(预算|人均|花费)', content)
    if budget_match:
        amount = int(budget_match.group(1))
        unit = budget_match.group(2)
        if unit == '万':
            amount *= 10000
        route_json["budget"] = amount

    # POI 提取
    pois = []
    for poi_type, keywords in POI_TYPE_MAP.items():
        for keyword in keywords:
            # 查找 "XX[keyword]" 模式
            pattern = rf'([^\s，,。.；;！!？?\n]{{2,10}}{re.escape(keyword)})'
            for match in re.finditer(pattern, content):
                name = match.group(1).strip()
                if name not in {p["name"] for p in pois}:
                    pois.append({
                        "name": name,
                        "type": poi_type,
                        "day": 1,
                        "notes": f"从原文正则提取: {keyword}",
                    })
            if len(pois) >= 20:
                break
        if len(pois) >= 20:
            break

    route_json["pois"] = pois[:20]
    route_json["notes"] = "（由规则兜底提取，建议人工核对）"

    return route_json


# ==================== 主服务接口 ====================

class ImportService:
    """
    外部内容导入服务

    使用示例:
        service = ImportService(call_llm_fn)
        result = service.import_content(
            content="北京3日游攻略...",
            source_url="https://example.com/beijing-guide",
            session_id="abc123",
        )
    """

    def __init__(
        self,
        call_llm_fn: Callable,
        save_to_db_fn: Optional[Callable] = None,
        update_db_fn: Optional[Callable] = None,
    ):
        """
        Args:
            call_llm_fn: LLM 调用函数 (app.call_llm)
            save_to_db_fn: 保存到数据库的函数 (database.save_imported_source)
            update_db_fn: 更新数据库的函数 (database.update_imported_source)
        """
        self.call_llm = call_llm_fn
        self.save_to_db = save_to_db_fn
        self.update_db = update_db_fn

    def import_content(
        self,
        content: str,
        session_id: str,
        source_url: str = "",
        source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        导入文本内容，解析为行程草稿

        Args:
            content: 文本内容
            session_id: 会话 ID
            source_url: 来源 URL
            source_id: 数据库记录 ID（不传则自动生成）

        Returns:
            Dict: {
                "success": bool,
                "source_id": str,
                "route_json": dict,
                "error": str | None,
            }
        """
        if source_id is None:
            source_id = str(uuid.uuid4())

        # 1) 保存原始内容到数据库
        if self.save_to_db:
            try:
                self.save_to_db(
                    session_id=session_id,
                    source_url=source_url,
                    raw_content=content,
                    source_id=source_id,
                )
            except Exception as e:
                logger.warning(f"保存到数据库失败: {e}")
                # 继续处理，不中断

        # 2) 调用 LLM 提取结构化信息
        route_json = extract_with_llm(content, self.call_llm, source_url)

        # 3) 更新数据库中的解析结果
        if self.update_db:
            try:
                self.update_db(
                    source_id=source_id,
                    parsed_json=json.dumps(route_json, ensure_ascii=False),
                    status="success",
                )
            except Exception as e:
                logger.warning(f"更新数据库失败: {e}")

        return {
            "success": True,
            "source_id": source_id,
            "route_json": route_json,
            "error": None,
        }

    def import_url(
        self,
        url: str,
        session_id: str,
    ) -> Dict[str, Any]:
        """
        从 URL 导入内容

        Args:
            url: 网页 URL
            session_id: 会话 ID

        Returns:
            Dict: 同 import_content()
        """
        # 获取 URL 内容
        content = _fetch_url_content(url)
        if not content:
            return {
                "success": False,
                "source_id": None,
                "route_json": None,
                "error": f"无法获取 URL 内容: {url}。请确认链接可访问，或尝试直接粘贴文本。",
            }

        return self.import_content(
            content=content,
            session_id=session_id,
            source_url=url,
        )


# ==================== 辅助函数 ====================

def format_route_summary(route_json: Dict[str, Any]) -> str:
    """
    将 route_json 格式化为人类可读的摘要

    Args:
        route_json: 结构化行程数据

    Returns:
        str: Markdown 格式的摘要
    """
    destination = route_json.get("destination", "未知目的地")
    days = route_json.get("days", 1)
    budget = route_json.get("budget")
    pois = route_json.get("pois", [])
    transport = route_json.get("transport", "")
    notes = route_json.get("notes", "")
    source = route_json.get("source", "")

    lines = [
        f"## 📋 行程草稿预览",
        f"",
        f"| 项目 | 内容 |",
        f"|------|------|",
        f"| 🎯 **目的地** | {destination} |",
        f"| 📅 **天数** | {days} 天 |",
        f"| 💰 **预算** | {f'{budget} 元/人' if budget else '未指定'} |",
    ]

    if transport:
        lines.append(f"| 🚗 **交通** | {transport} |")
    if source:
        lines.append(f"| 🔗 **来源** | {source[:60]} |")

    lines.append("")

    if pois:
        lines.append("### 🗺️ POI 列表")
        lines.append("")
        # 按天分组
        by_day: Dict[int, List[Dict]] = {}
        for poi in pois:
            day = poi.get("day", 1)
            by_day.setdefault(day, []).append(poi)

        for day in sorted(by_day.keys()):
            lines.append(f"**第 {day} 天**")
            for poi in by_day[day]:
                emoji_map = {"景点": "🏞️", "酒店": "🏨", "餐饮": "🍽️"}
                emoji = emoji_map.get(poi.get("type", ""), "📍")
                name = poi.get("name", "")
                notes_text = poi.get("notes", "")
                lines.append(f"- {emoji} **{name}** ({poi.get('type', '')})"
                             + (f" - {notes_text}" if notes_text else ""))
            lines.append("")

    if notes:
        lines.append(f"### 📝 备注")
        lines.append(f"{notes}")
        lines.append("")

    lines.append("---")
    lines.append("*此草稿由 AI 自动生成，你可以继续编辑或确认后使用。*")

    return "\n".join(lines)


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("🧪 导入服务测试")
    print("=" * 60)

    # 模拟文本
    sample_text = """
    杭州3日游攻略
    人均预算: 2000元

    第一天：
    - 上午到达杭州，入住西湖边的如家酒店
    - 中午去楼外楼吃西湖醋鱼
    - 下午游览西湖十景：断桥残雪、苏堤春晓
    - 晚上逛河坊街夜市

    第二天：
    - 上午去灵隐寺
    - 中午在灵隐寺素斋馆用餐
    - 下午游览飞来峰
    - 晚上看印象西湖演出

    第三天：
    - 上午去龙井村品茶
    - 中午吃农家菜
    - 下午返程

    交通建议: 杭州东站高铁到达，市区公交+打车
    """

    print("\n📥 测试 fallback 提取（无 LLM 时）...")
    result = _fallback_extraction(sample_text)

    print(f"  目的地: {result['destination']}")
    print(f"  天数: {result['days']}")
    print(f"  预算: {result['budget']}")
    print(f"  POI 数量: {len(result['pois'])}")
    for poi in result['pois'][:5]:
        print(f"    - {poi['name']} ({poi['type']})")

    # 测试格式化
    print("\n📝 格式化摘要:")
    print(format_route_summary(result)[:500])

    print("\n✅ 测试完成")
