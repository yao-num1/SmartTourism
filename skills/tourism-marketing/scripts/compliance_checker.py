"""
文旅营销内容合规检查器
检测营销文案中的广告法禁用词和合规问题
"""

import re
from typing import List, Tuple


# 广告法绝对化禁用词
ABSOLUTE_BANNED_WORDS = [
    "第一", "最好", "最佳", "最优", "最大", "最高", "最低", "最便宜",
    "国家级", "世界级", "顶级", "极品", "第一品牌",
    "唯一", "独一无二", "绝无仅有",
    "万能", "全能", "100%", "百分百"
]

# 需要警惕的词（提示而非禁止）
WARNING_WORDS = [
    ("限时", "请确保有明确的起止时间"),
    ("原价", "请确保原价为真实成交价"),
    ("免费", "请明确免费的范围和条件"),
    ("永久有效", "建议改为明确的期限"),
    ("纯天然", "需要有相关认证支持")
]

# 平台字数限制
PLATFORM_LIMITS = {
    "抖音": {"title": 55, "description": 500},
    "小红书": {"title": 20, "description": 1000},
    "微信公众号": {"title": 64, "description": 20000},
    "微博": {"title": 0, "description": 140},
    "B站": {"title": 80, "description": 2000}
}


def check_absolute_words(text: str) -> List[Tuple[str, int]]:
    """检查绝对化禁用词，返回 [(禁用词, 出现次数)]"""
    results = []
    for word in ABSOLUTE_BANNED_WORDS:
        count = len(re.findall(word, text))
        if count > 0:
            results.append((word, count))
    return results


def check_warning_words(text: str) -> List[Tuple[str, str]]:
    """检查警示词，返回 [(警示词, 建议)]"""
    results = []
    for word, suggestion in WARNING_WORDS:
        if word in text:
            results.append((word, suggestion))
    return results


def check_platform_limits(text: str, platform: str) -> dict:
    """检查是否符合平台字数限制"""
    limits = PLATFORM_LIMITS.get(platform)
    if not limits:
        return {"status": "unknown", "message": f"未知平台: {platform}"}

    text_len = len(text)
    limit = limits.get("description", 1000)

    if text_len > limit:
        return {
            "status": "exceeded",
            "current": text_len,
            "limit": limit,
            "excess": text_len - limit
        }
    return {"status": "ok", "current": text_len, "limit": limit}


def check_price_format(text: str) -> List[str]:
    """检查价格标注规范"""
    issues = []
    # 检测裸价格数字（没有标注单位或说明）
    price_pattern = re.findall(r'(?<!\¥)(?<!\d)[1-9]\d{1,4}(?!\d)', text)
    if price_pattern:
        issues.append(f"检测到可能的价格数字 {'、'.join(price_pattern)}，请确认是否需标注'元'或'起'")
    return issues


def full_compliance_check(text: str, platform: str = None) -> dict:
    """执行完整合规检查"""
    result = {
        "absolute_banned": check_absolute_words(text),
        "warnings": check_warning_words(text),
        "price_issues": check_price_format(text),
        "passed": True
    }

    if platform:
        result["platform_check"] = check_platform_limits(text, platform)

    # 判断是否通过
    if result["absolute_banned"] or result["price_issues"]:
        result["passed"] = False

    return result


if __name__ == "__main__":
    # 测试示例
    test_text = """
    全球最好的温泉酒店！唯一拥有天然温泉的五星级度假村。
    原价1999元，限时特价999元，百分百满意保证！
    免费赠送早餐，纯天然温泉水，世界级服务体验。
    """

    print("=" * 50)
    print("文旅营销内容合规检查报告")
    print("=" * 50)

    result = full_compliance_check(test_text, "小红书")

    if result["absolute_banned"]:
        print("\n❌ 绝对化禁用词:")
        for word, count in result["absolute_banned"]:
            print(f"  · '{word}' 出现 {count} 次")

    if result["warnings"]:
        print("\n⚠️ 需注意的词汇:")
        for word, suggestion in result["warnings"]:
            print(f"  · '{word}': {suggestion}")

    if result.get("price_issues"):
        print("\n💰 价格标注问题:")
        for issue in result["price_issues"]:
            print(f"  · {issue}")

    if result.get("platform_check"):
        pc = result["platform_check"]
        print(f"\n📱 平台适配检查 ({'通过' if pc['status'] == 'ok' else '超限'}):")
        print(f"  字数: {pc['current']}/{pc['limit']}")

    print(f"\n{'✅ 合规检查通过' if result['passed'] else '❌ 合规检查未通过，请修改后重试'}")
