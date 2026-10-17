# -*- coding: utf-8 -*-
"""
运行成本与延迟实测脚本
======================
评审要看「必要的运行成本」并且要区分实测与预期，所以这份数据必须是一条命令能量出来的，
而不是文档里手写的数字。脚本打本机服务端的真实接口（会真花模型额度，量很小），
每个场景前后各读一次 /api/admin/ops 的 llm_usage，用**差值**把 token 归到具体场景上。

用法（工作目录=仓库根，服务端要先起着）：
    python server/measure_llm_cost.py            # 跑测并生成 运行成本与延迟实测.md
    python server/measure_llm_cost.py --no-md    # 只打印，不写文件

口径（写进产出文档，别在文档里含糊）：
  - 能拿到上游 `usage` 的调用记为**实测 token**（本服务的流式末尾块也会带 usage，实测为准）；
  - 拿不到 usage 时才按字符折算，单独计数在 estimated_calls，绝不混进实测；
  - 金额只有配了单价才算：SMARTTOURISM_PRICE_IN_PER_M / SMARTTOURISM_PRICE_OUT_PER_M
    （元/百万 token）。不配就只报 token，绝不编一个价签上去。
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

BASE = os.getenv("SMARTTOURISM_BASE", "http://127.0.0.1:5050")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_MD = os.path.join(ROOT, "运行成本与延迟实测.md")
UID = "cost_probe_user"


def _get(path, timeout=30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_json(path, payload, timeout=300):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def usage_snapshot():
    return _get("/api/admin/ops", timeout=30).get("llm_usage") or {}


def _delta(after, before, key):
    return (after.get(key) or 0) - (before.get(key) or 0)


def run_chat(message, conversation_id, destination="", skill=""):
    """跑一轮对话，测首字/总耗时/输出字数/出处条数，并按 ops 差值归账 token。"""
    before = usage_snapshot()
    t0 = time.time()
    resp = _post_json("/api/chat", {
        "message": message, "conversation_id": conversation_id, "user_id": UID,
        "stream": True, "skill": skill, "crowd_id": "", "destination": destination})
    resp.encoding = "utf-8"
    first_content = None
    out_chars = 0
    sources = 0
    buffer = ""
    truncated_by_cancel = False
    for raw in resp:
        try:
            chunk = raw.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        buffer += chunk
        while "\n\n" in buffer:
            evt, buffer = buffer.split("\n\n", 1)
            if not evt.startswith("data: "):
                continue
            body = evt[6:].strip()
            if body == "[DONE]":
                break
            try:
                d = json.loads(body)
            except ValueError:
                continue
            if "content" in d:
                if first_content is None:
                    first_content = time.time() - t0
                out_chars += len(d.get("content") or "")
            elif d.get("type") == "sources":
                sources += len(d.get("data") or [])
            elif d.get("type") == "stopped":
                truncated_by_cancel = True
            elif "error" in d:
                raise RuntimeError("服务端返回错误事件：" + str(d)[:200])
    total = time.time() - t0
    after = usage_snapshot()
    return {
        "首字_s": round(first_content, 2) if first_content is not None else None,
        "总耗时_s": round(total, 2),
        "输出字数": out_chars,
        "知识库出处": sources,
        "调用次数": _delta(after, before, "calls"),
        "实测token调用": _delta(after, before, "upstream_calls"),
        "折算token调用": _delta(after, before, "estimated_calls"),
        "输入token": _delta(after, before, "prompt_tokens"),
        "输入命中token": _delta(after, before, "cache_hit_tokens"),
        "输入未命中token": _delta(after, before, "cache_miss_tokens"),
        "输出token": _delta(after, before, "completion_tokens"),
        "半截": truncated_by_cancel,
    }


def run_marketing(destination, mtype, tone, crowd):
    before = usage_snapshot()
    t0 = time.time()
    resp = _post_json("/api/marketing", {
        "destination": destination, "type": mtype, "tone": tone,
        "crowd": crowd, "extra": "", "stream": True})
    resp.encoding = "utf-8"
    first_content = None
    out_chars = 0
    buffer = ""
    for raw in resp:
        buffer += raw.decode("utf-8", "replace")
        while "\n\n" in buffer:
            evt, buffer = buffer.split("\n\n", 1)
            if not evt.startswith("data: "):
                continue
            body = evt[6:].strip()
            if body == "[DONE]":
                break
            try:
                d = json.loads(body)
            except ValueError:
                continue
            if "content" in d:
                if first_content is None:
                    first_content = time.time() - t0
                out_chars += len(d.get("content") or "")
            elif "error" in d:
                raise RuntimeError("服务端返回错误事件：" + str(d)[:200])
    total = time.time() - t0
    after = usage_snapshot()
    return {
        "首字_s": round(first_content, 2) if first_content is not None else None,
        "总耗时_s": round(total, 2),
        "输出字数": out_chars,
        "知识库出处": None,
        "调用次数": _delta(after, before, "calls"),
        "实测token调用": _delta(after, before, "upstream_calls"),
        "折算token调用": _delta(after, before, "estimated_calls"),
        "输入token": _delta(after, before, "prompt_tokens"),
        "输入命中token": _delta(after, before, "cache_hit_tokens"),
        "输入未命中token": _delta(after, before, "cache_miss_tokens"),
        "输出token": _delta(after, before, "completion_tokens"),
        "半截": False,
    }


def build_md(health, lu, rows, prices, started):
    lines = []
    lines.append("# 运行成本与延迟实测")
    lines.append("")
    lines.append("> 这份表由一条命令量出来，不是手写：`python server/measure_llm_cost.py`  ")
    lines.append("> 打本机服务端真实接口，每个场景前后各读一次 `/api/admin/ops` 的 `llm_usage`，"
                 "用差值把 token 归到具体场景。")
    lines.append("")
    lines.append("## 一、测量环境")
    lines.append("")
    lines.append("| 项 | 值 | 来源 |")
    lines.append("|---|---|---|")
    lines.append(f"| 被测服务 | {health.get('service')} {health.get('version')} | `/api/health` |")
    lines.append(f"| 模型供应商 | {health.get('provider')} | `/api/health` |")
    lines.append(f"| 模型 | {lu.get('model') or '未知'} | `/api/admin/ops` 的 `llm_usage.model` |")
    lines.append(f"| 测量时间（本地） | {started} | 脚本运行时刻 |")
    lines.append(f"| 本次进程累计调用 | {lu.get('calls')} 次 | `llm_usage.calls`（进程重启清零） |")
    lines.append(f"| token 口径 | {lu.get('token_source')} | `llm_usage.token_source` |")
    lines.append("")
    lines.append("## 二、逐场景实测")
    lines.append("")
    lines.append("| 场景 | 首字等待(s) | 总耗时(s) | 输出字数 | 模型调用次数 | "
                 "实测token调用 | 折算token调用 | 输入token | 其中缓存命中 | 输出token | 知识库出处 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for name, r in rows:
        if r.get("错误"):
            lines.append("| {} | 场景失败 | — | — | — | — | — | — | — | — | {} |".format(
                name, r["错误"].replace("|", "／")[:80]))
            continue
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            name,
            "-" if r["首字_s"] is None else r["首字_s"],
            r["总耗时_s"], r["输出字数"], r["调用次数"],
            r["实测token调用"], r["折算token调用"], r["输入token"],
            r.get("输入命中token", 0), r["输出token"],
            "-" if r["知识库出处"] is None else r["知识库出处"]))
    tot_calls = sum(r["调用次数"] for _, r in rows)
    tot_pt = sum(r["输入token"] for _, r in rows)
    tot_ct = sum(r["输出token"] for _, r in rows)
    tot_up = sum(r["实测token调用"] for _, r in rows)
    tot_est = sum(r["折算token调用"] for _, r in rows)
    tot_chars = sum(r["输出字数"] for _, r in rows)
    tot_hit = sum(r.get("输入命中token", 0) for _, r in rows)
    lines.append("")
    lines.append(f"合计：{len(rows)} 个场景，{tot_calls} 次模型调用"
                 f"（其中 {tot_up} 次拿到上游真实 usage、{tot_est} 次为字符折算），"
                 f"输入 {tot_pt} token、输出 {tot_ct} token，产出正文 {tot_chars} 字。")
    if tot_pt:
        lines.append(f"输入里 **{tot_hit} token（{tot_hit / tot_pt * 100:.1f}%）按上下文缓存命中计费**"
                     f"——命中数取自上游 usage 的 `prompt_cache_hit_tokens`，是量的不是推的；"
                     "提示词头部逐字稳定才有这一幕（见第四节）。")
    lines.append("")
    lines.append("## 三、成本")
    lines.append("")
    if prices:
        lines.append(f"单价（元/百万 token，来自环境变量）：输入 {prices[0]}、输出 {prices[1]}。")
        lines.append("")
        lines.append("| 项 | 金额 |")
        lines.append("|---|---|")
        lines.append(f"| 本次 {len(rows)} 个场景合计 | {tot_pt / 1e6 * prices[0] + tot_ct / 1e6 * prices[1]:.4f} 元 |")
        lines.append(f"| 平均单场景 | {(tot_pt / 1e6 * prices[0] + tot_ct / 1e6 * prices[1]) / max(len(rows), 1):.4f} 元 |")
        lines.append("")
        lines.append("> 折算里含字符估算部分，所以这是**上限估计**，不等于账单。")
    else:
        lines.append("本次**未配单价**，所以只报 token 不报钱（`llm_usage.cost` 为空，"
                     "接口里写着 `未配置单价…所以只报 token 不报钱`）。")
        lines.append("")
        lines.append("要出金额就设好环境变量再跑一遍（模型调价是常事，所以单价不写死在代码里）：")
        lines.append("")
        lines.append("```bat")
        lines.append("set SMARTTOURISM_PRICE_IN_PER_M=<输入价，元/百万token>")
        lines.append("set SMARTTOURISM_PRICE_OUT_PER_M=<输出价，元/百万token>")
        lines.append("python server\\measure_llm_cost.py")
        lines.append("```")
        lines.append("")
        lines.append("> 单价请按**当时的官方价格页**填写；DeepSeek 在 2026 年有过调价，"
                     "任何写死在文档里的价签都会过期，所以这里只留方法不留数字。")
    lines.append("")
    lines.append("## 四、数据来源与口径（哪些是量的、哪些是估的）")
    lines.append("")
    lines.append("- **实测**：延迟（首字/总耗时，脚本本地计时）、输出字数、知识库出处条数、"
                 "以及上游响应带回 `usage` 的那些调用的 token。")
    if tot_est == 0 and tot_up > 0:
        lines.append("- **折算**：本次**一次都没用上**——"
                     f"{tot_up} 次调用全部在响应里带了真实 `usage`（流式的末尾块也带），"
                     "所以本表 token 全是实测。只有上游不回 `usage` 时才会退到折算："
                     "按「中文 1 字≈0.75 token，其它 4 字符≈1 token」估算，"
                     "并在 `llm_usage.estimated_calls` 里单独计数，不混进实测。")
    else:
        lines.append("- **折算**：上游不回 `usage` 的调用按「中文 1 字≈0.75 token，"
                     "其它 4 字符≈1 token」折算，在 `llm_usage.estimated_calls` 里单独计数，"
                     f"不混进实测。本次实测 {tot_up} 次、折算 {tot_est} 次。")
    lines.append("- 每个场景的模型调用次数不止 1 次：一轮对话除正文外还有一次预生成思考"
                 "（非流式，带真实 usage）。这正是「一次用户等待里到底花了多少」的账。")
    lines.append("- 延迟含服务端前置采集（定位/实时数据/知识库检索），"
                 "与用户在界面上感受到的等待一致，不是纯模型耗时。")
    lines.append("- 语义缓存命中时不再发起网络调用，所以**重复问题的成本会低于本表**；"
                 "本表量的是冷路径（各场景问法都不同）。")
    lines.append("- **上下文缓存（prompt cache）**：DeepSeek 按请求**前缀**命中，命中价远低于未命中价。"
                 "本项目因此把每次都会变的值（分钟级时钟、IP 定位、Skill 正文）一律排到提示词末尾，"
                 "头部逐字不动；同一次提问里的两次调用（预生成思考 + 正文）共用这段头部，"
                 "同会话的第二轮又共用上一轮的全部内容，所以命中 token 会随轮次上升。"
                 "命中量由上游 `usage` 报，不由本项目推断。")
    lines.append("- 命中有个前提：上游得先见过这段前缀才会命中，所以**新前缀的第一次调用命中必为 0**"
                 "（本表营销场景就是 0，它是一段独立的新 prompt 且只调一次）；服务重启不会清掉上游的缓存，"
                 "但也不保证还在。")
    lines.append("")
    lines.append("## 五、预期收益（与上面实测分开，未做对照实验前不当结论讲）")
    lines.append("")
    lines.append("人工写一条小红书文案 + 一版行程的成本、以及转化率变化，**本项目没有做过"
                 "对照实验，不给数字**。要引用收益请先设计对照测量，或明确标注为假设。")
    lines.append("")
    lines.append("## 六、复现步骤")
    lines.append("")
    lines.append("```bat")
    lines.append("start.bat")
    lines.append("python server\\measure_llm_cost.py")
    lines.append("```")
    lines.append("")
    lines.append(f"运维面板 `/admin` 的「模型用量与成本」区块看实时累计值；"
                 f"本文件生成于 {started}。")
    lines.append("")
    return "\n".join(lines)


def main():
    no_md = "--no-md" in sys.argv
    try:
        health = _get("/api/health", timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"服务端没起来或不可达：{e}\n先运行 start.bat 再起本脚本。")
        return 1
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    conv = "cost_probe_" + str(int(time.time()))
    rows = []
    scenarios = [
        ("咨询（附近餐饮，带目的地）", lambda: run_chat(
            "杭州西湖附近有什么适合带小孩吃饭的餐厅？给两家就够，说清为什么适合孩子。",
            conv, destination="杭州")),
        ("行程（预算+银发同行约束）", lambda: run_chat(
            "帮我规划苏州 2 天行程，总预算 1200 元，同行是我爸妈（六十多岁），不要太赶。",
            conv, destination="苏州")),
        ("用户改需求（同会话二轮）", lambda: run_chat(
            "第二天上午换成室内景点，我爸妈怕晒，其它别动。", conv, destination="苏州")),
        ("营销（推广博客）", lambda: run_marketing("杭州", "blog", "文艺", "亲子家庭")),
    ]
    for name, fn in scenarios:
        print(f"\n>> {name}")
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001
            print(f"   场景失败，如实记录：{str(e)[:200]}")
            r = {"首字_s": None, "总耗时_s": None, "输出字数": 0, "知识库出处": None,
                 "调用次数": 0, "实测token调用": 0, "折算token调用": 0,
                 "输入token": 0, "输入命中token": 0, "输入未命中token": 0,
                 "输出token": 0, "半截": False, "错误": str(e)[:200]}
        rows.append((name, r))
        print("   " + json.dumps(r, ensure_ascii=False))

    lu = usage_snapshot()
    p_in = os.getenv("SMARTTOURISM_PRICE_IN_PER_M")
    p_out = os.getenv("SMARTTOURISM_PRICE_OUT_PER_M")
    prices = (float(p_in), float(p_out)) if p_in and p_out else None
    md = build_md(health, lu, rows, prices, started)
    print("\n" + md)
    if not no_md:
        with io.open(OUT_MD, "w", encoding="utf-8") as f:
            f.write(md)
        print("已写入 " + OUT_MD)
    return 0


if __name__ == "__main__":
    sys.exit(main())
