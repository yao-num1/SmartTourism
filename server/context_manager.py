# -*- coding: utf-8 -*-
"""
对话上下文管理模块
==================

解决三个问题（依据见下）：
1. **上下文过长**：按 token 预算选择历史窗口（滑动窗口），超预算的旧轮次不再硬塞给模型；
2. **截断丢信息**：被预算丢弃的旧轮次先压缩成滚动摘要，放在消息列表前部（摘要 + 近轮原文的混合记忆）；
3. **幻觉/漂移**：把会话里已确认的事实（目的地/天数/预算/人群）每轮重新锚定到用户消息开头，
   防止模型在长上下文中偏离用户已确认的约束（context poisoning）。

参考：
- Liu et al. 2023《Lost in the Middle》:关键信息放上下文首尾，埋在中间会丢失
  → 摘要放 system 之后（前部），事实锚定块放当前用户消息（末尾，最强位置）；
- LangChain ConversationSummaryBufferMemory:滚动摘要 + 近轮缓冲；
- MemGPT (Packer et al. 2023):分层记忆，摘要可再压缩；
- Anthropic 提示工程指南:显式重申约束、要求承认不确定。

本模块不依赖第三方库（无 tiktoken/向量库），token 估算为保守近似：
CJK 字符 ≈ 1 token，其余按词 ≈ len/4，宁可高估不超发。
"""

import os
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---- 可调参数（环境变量覆盖）----
# 输入总预算（system + 摘要 + 历史 + 当前用户消息），deepseek-chat 上下文 64K，
# 预留输出 max_tokens(最大 8192) 与插件数据尖峰，默认 24K 是保守值
CONTEXT_BUDGET_TOKENS = int(os.environ.get('CTX_BUDGET', '24000'))
# 无论预算多紧都保留的最近完整轮数（1 轮 = user + assistant 两条）
CONTEXT_RECENT_ROUNDS = int(os.environ.get('CTX_RECENT_ROUNDS', '4'))
# 内存中最多缓存的消息条数（与 app.py 历史一致），超出从头部丢弃
IN_MEMORY_CAP = int(os.environ.get('CTX_MEM_CAP', '40'))
# 摘要长度上限（字），超过就优先保留硬约束
SUMMARY_MAX_CHARS = int(os.environ.get('CTX_SUMMARY_MAX', '300'))
# 单次送入压缩器的原文上限（字）。超出先分段压缩再合并 —— 分级压缩，避免一次性长文喂给模型
SUMMARY_INPUT_CHARS = int(os.environ.get('CTX_SUMMARY_INPUT', '6000'))
# 压缩触发线：占用达到「预算 × 该比例」就开始折叠旧轮次，不等塞满。
# 提前压的原因：插件实时数据/RAG 会在单轮造成 token 尖峰，等撑到 100% 再压，
# 本轮已经超了；留 25% 余量给尖峰，同时让进度条的「到线即压」有实际意义。
COMPRESS_TRIGGER = min(1.0, max(0.1, float(os.environ.get('CTX_COMPRESS_TRIGGER', '0.75'))))
# 是否把摘要/事实写进 SQLite（关闭则纯内存，单测可设 CTX_PERSIST=0）
PERSIST_ENABLED = os.environ.get('CTX_PERSIST', '1') != '0'


def estimate_tokens(text: str) -> int:
    """保守估算 token 数：CJK ≈ 1 token/字，其余 ≈ 4 字符/token。宁可高估。"""
    if not text:
        return 0
    cjk = len(re.findall(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\u2014-\u201f]', text))
    other = len(text) - cjk
    return cjk + max(1, other // 4)


def _group_rounds(history: List[dict]) -> Tuple[List[Tuple[dict, dict]], List[dict]]:
    """把相邻的 (user, assistant) 配成完整轮，返回 (完整轮列表, 未配对尾巴)。"""
    rounds: List[Tuple[dict, dict]] = []
    tail: List[dict] = []
    i = 0
    n = len(history)
    while i < n:
        if (i + 1 < n and history[i].get('role') == 'user'
                and history[i + 1].get('role') in ('assistant', 'agent')):
            rounds.append((history[i], history[i + 1]))
            i += 2
        else:
            tail.append(history[i])
            i += 1
    return rounds, tail


# ---- 滚动摘要存储（内存为主，SQLite 落库：重启后可恢复）----
_summary_store: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()
_hydrated: set = set()


def _store() -> Dict[str, Dict[str, Any]]:
    return _summary_store


def _db():
    """惰性拿 database；拿不到（单测 / 无 DB 环境）返回 None，持久化自动降级为纯内存。"""
    if not PERSIST_ENABLED:
        return None
    try:
        import database
        return database
    except Exception:
        return None


def _hydrate(conv_id: str):
    """内存里没有该会话摘要时，从 DB 读回 sessions.history_summary + session_state.folded_rounds。

    只在每个 conv_id 上首次访问时查库（_hydrated 标记），后续走内存。
    """
    if conv_id in _hydrated:
        return
    _hydrated.add(conv_id)
    with _lock:
        if conv_id in _store():
            return
    d = _db()
    if not d:
        return
    try:
        sess = d.get_session(conv_id) or {}
        text = (sess.get('history_summary') or '').strip()
        folded = int((d.get_session_state(conv_id) or {}).get('folded_rounds') or 0)
    except Exception:
        return
    if text or folded:
        with _lock:
            _store().setdefault(conv_id, {'text': text, 'folded': folded, 'restored': True})


def _persist(conv_id: str):
    """把摘要与折叠轮数写回 DB（失败静默：持久化不该打断对话）。"""
    d = _db()
    if not d:
        return
    with _lock:
        entry = _store().get(conv_id)
    if not entry:
        return
    try:
        d.update_session_summary(conv_id, entry.get('text', ''))
        d.save_session_state(conv_id, folded_rounds=int(entry.get('folded', 0) or 0))
    except Exception as e:
        print(f'[CTX] 摘要持久化失败（不影响本轮对话）: {e}')


def get_summary(conv_id: str) -> str:
    _hydrate(conv_id)
    with _lock:
        return _store().get(conv_id, {}).get('text', '')


def get_summary_meta(conv_id: str) -> Dict[str, Any]:
    _hydrate(conv_id)
    with _lock:
        entry = dict(_store().get(conv_id, {}))
    return {'text': entry.get('text', ''), 'folded': entry.get('folded', 0),
            'tier': entry.get('tier', 'none'), 'src_chars': entry.get('src_chars', 0),
            'restored': entry.get('restored', False)}


def summary_cache_size() -> int:
    """内存里缓存了多少个会话的滚动摘要（观测用）。"""
    with _lock:
        return len(_summary_store)


def clear_summary(conv_id: str):
    with _lock:
        _store().pop(conv_id, None)
    _hydrated.add(conv_id)  # 已清空，别再回读 DB 把它捞回来
    d = _db()
    if d:
        try:
            d.update_session_summary(conv_id, '')
            d.save_session_state(conv_id, folded_rounds=0)
        except Exception:
            pass


# ---- 压缩工具：硬约束保底 + 句子级裁剪 + 抽取式降级 ----

# 摘要可以粗糙，但硬约束不能丢：这些词后面紧跟的短语会被原样保留
_CONSTRAINT_HEADS = ('目的地', '城市', '天数', '预算', '人均', '人群', '亲子', '老人', '孩子',
                     '宝宝', '情侣', '团建', '忌口', '不吃', '不要', '不去', '不想', '避免',
                     '高铁', '火车', '飞机', '自驾', '大巴', '住宿', '酒店', '已确认', '改成',
                     '换成', '重新')
_CONSTRAINT_RE = re.compile(
    r'(' + '|'.join(_CONSTRAINT_HEADS) + r')[^。；;!！?？，,、\n]{0,32}')


def extract_constraints(text: str, max_items: int = 6) -> List[str]:
    """从原文里抠出硬约束短语（去重、保序）。这是压缩的安全网：模型漏了由这里兜住。"""
    if not text:
        return []
    out: List[str] = []
    for m in _CONSTRAINT_RE.finditer(text):
        frag = re.sub(r'\s+', '', m.group(0))
        if len(frag) < 2 or frag in out:
            continue
        out.append(frag)
        if len(out) >= max_items:
            break
    return out


def _sentences(text: str) -> List[str]:
    parts = re.split(r'(?<=[。；;！!？?\n])', text)
    return [p.strip() for p in parts if p and p.strip()]


def sentence_trim(text: str, limit: int) -> str:
    """按句子裁剪到 limit 字：优先留首句和含硬约束的句子，从中间丢次要句子。

    替代原先的 text[:limit] 硬截断 —— 截断会切出半句话，且可能把出现在后面的
    预算/忌口这类信息直接抹掉。
    """
    if len(text) <= limit:
        return text
    sents = _sentences(text)
    if not sents:
        return text[:limit]
    keep = [sents[0]]
    dropped_middle: List[str] = []
    for s in sents[1:-1]:
        (keep if _CONSTRAINT_RE.search(s) else dropped_middle).append(s)
    if sents[-1] not in keep:
        dropped_middle.append(sents[-1])
    # 预算还够就把被丢的句子按原顺序尽量补回
    used = sum(len(s) for s in keep)
    for s in dropped_middle:
        if used + len(s) <= limit:
            keep.append(s)
            used += len(s)
    merged = ''.join(keep)
    if len(merged) > limit:
        merged = sentence_trim_safe_cut(merged, limit)
    return merged


def sentence_trim_safe_cut(text: str, limit: int) -> str:
    """单句本身就超长时的兜底：留头 2/3 + 尾 1/3，中间用省略号。"""
    if len(text) <= limit:
        return text
    head = int(limit * 0.66)
    tail = limit - head - 1
    return text[:head] + '…' + (text[-tail:] if tail > 0 else '')


def _heuristic_summary(dropped: List[Tuple[dict, dict]], old_text: str) -> str:
    """无压缩模型（单测 / LLM 不可用）时的抽取式降级：约束保底 + 每轮用户话轮首句。

    原来这条路径会直接丢弃旧轮次（信息归零）；降级为抽取式至少保住主线。
    """
    lines: List[str] = []
    if old_text:
        lines.append(old_text)
    for u, a in dropped:
        us = (u.get('content', '') or '').strip()
        head_sent = (_sentences(us) or [us[:40]])[0]
        lines.append(head_sent[:60])
    text = '；'.join(dict.fromkeys(x for x in lines if x))
    cons = extract_constraints('\n'.join(
        (u.get('content', '') or '') + '\n' + (a.get('content', '') or '') for u, a in dropped))
    kept = [c for c in cons if c not in text]
    if kept:
        text = ('【硬约束】' + '、'.join(kept) + '。' + text)
    return sentence_trim(text, SUMMARY_MAX_CHARS)


def _summarize_once(payload: str, summarizer: Callable[[str], str]) -> str:
    try:
        return (summarizer(payload) or '').strip()
    except Exception as e:
        print(f'[CTX] 摘要生成失败: {e}')
        return ''


def _fold_new_rounds(conv_id: str, dropped: List[Tuple[dict, dict]],
                     summarizer: Optional[Callable[[str], str]]) -> bool:
    """把新被预算丢弃的完整轮折叠进滚动摘要。返回是否真的产生了/更新了摘要。

    分级压缩（tier 写入 meta，便于观测压缩强度）：
      none      —— 原文直接压一次即可；
      chunked   —— 原文超长，先分段压再合并，合并后再裁；
      degraded  —— 没有可用压缩模型/压缩失败，走抽取式降级（约束保底）。
    """
    if not dropped:
        return False
    transcript = '\n'.join(
        f"用户：{u.get('content', '')[:400]}\n助手：{a.get('content', '')[:600]}"
        for u, a in dropped)
    with _lock:
        entry = _store().setdefault(conv_id, {'text': '', 'folded': 0})
    old = entry['text']
    src_chars = sum(len(u.get('content', '') or '') + len(a.get('content', '') or '')
                    for u, a in dropped)
    tier = 'none'
    new_text = ''

    if summarizer:
        if len(transcript) <= SUMMARY_INPUT_CHARS:
            payload = (f"【已有摘要】{old}\n" if old else '') + \
                      ("【新超出预算的旧轮次】\n" + transcript)
            new_text = _summarize_once(payload, summarizer)
        else:
            # 分段压缩：每段各自压到 SUMMARY_INPUT_CHARS 以内，再整体压一次
            tier = 'chunked'
            chunks = [transcript[i:i + SUMMARY_INPUT_CHARS]
                      for i in range(0, len(transcript), SUMMARY_INPUT_CHARS)]
            partial = '；'.join(filter(None, (_summarize_once(c, summarizer) or c[:120]
                                              for c in chunks[:4])))
            new_text = _summarize_once(
                f"【已有摘要】{old}\n【分段摘要，请合并去重】{partial}", summarizer) or partial

    if not new_text:
        # 压缩模型缺失或失败 → 抽取式降级，不再像旧实现那样整段丢弃
        tier = 'degraded'
        new_text = _heuristic_summary(dropped, old)

    # 硬约束保底 + 句子级裁剪（原来的 new_text[:SUMMARY_MAX_CHARS] 会切出半句并丢信息）
    if new_text:
        cons = extract_constraints(transcript)
        kept = [c for c in cons if c not in new_text]
        if kept:
            new_text = '【硬约束】' + '、'.join(kept) + '。' + new_text
        new_text = sentence_trim(new_text, SUMMARY_MAX_CHARS)

    with _lock:
        entry['text'] = new_text
        entry['tier'] = tier
        entry['src_chars'] = int(entry.get('src_chars', 0)) + src_chars
        entry['folded'] = entry.get('folded', 0) + len(dropped)
    _persist(conv_id)
    return bool(new_text)


def append_history(conv_id: str, history: List[dict],
                   user_msg: str, assistant_msg: str) -> List[dict]:
    """追加一轮并做内存截断；若头部被丢弃的是已折叠过的轮次，同步校正 folded 计数。"""
    history.append({"role": "user", "content": user_msg})
    history.append({"role": "assistant", "content": assistant_msg})
    overflow = max(0, len(history) - IN_MEMORY_CAP)
    if overflow:
        del history[:overflow]
        with _lock:
            entry = _store().get(conv_id)
            if entry:
                # 头部按 2 条 = 1 轮近似校正（历史恒为配对追加，截断恒为偶数）
                entry['folded'] = max(0, entry.get('folded', 0) - overflow // 2)
    return history


def build_facts_block(conv_id: str) -> str:
    """从事实追踪器取已确认事实，生成锚定块（放在当前用户消息开头，即上下文末尾最强位置）。"""
    try:
        from anti_hallucination import get_fact_tracker
        facts = get_fact_tracker().get_facts(conv_id) or {}
    except Exception:
        return ''
    if not facts:
        return ''
    labels = {'destination': '目的地', 'days': '天数', 'budget': '预算(元)',
              'crowd_type': '人群'}
    parts = [f"{labels[k]}：{v}" for k, v in facts.items() if k in labels and v]
    if not parts:
        return ''
    return ("【本会话已确认信息（用户此前明确说过，回答必须与此一致，冲突时优先采用）】"
            + '；'.join(parts) + "\n\n")


def manage_context(conv_id: str, system_prompt: str, history: List[dict],
                   current_user_content: str,
                   summarizer: Optional[Callable[[str], str]] = None
                   ) -> Tuple[List[dict], Dict[str, Any]]:
    """按预算组装 messages。

    策略（对齐 Lost in the Middle）：
      1. system 永远保留；
      2. 滚动摘要（如有）放第 2 条 —— 前部位置，模型看得见；
      3. 历史从最新往最旧贪心装入触发线（预算 × COMPRESS_TRIGGER，默认 75%）；
         最近 CONTEXT_RECENT_ROUNDS 轮无论多紧都保留；
      4. 装不下的旧轮次折叠进滚动摘要（下次调用生效），本轮直接不出现在 messages；
      5. 当前用户消息（含事实锚定块/插件数据/RAG）永远保留。

    返回 (messages, meta)，meta 供日志与调试：
      {rounds_total, rounds_included, rounds_dropped, rounds_in_summary, summary_used,
       summary_chars, summary_src_chars, compress_ratio, compress_tier,
       est_input_tokens, budget}
    """
    _hydrate(conv_id)
    rounds, _tail = _group_rounds(history)
    total_rounds = len(rounds)
    recent = rounds[-CONTEXT_RECENT_ROUNDS:]
    older = rounds[:-CONTEXT_RECENT_ROUNDS] if total_rounds > CONTEXT_RECENT_ROUNDS else []

    # 历史预算 = 触发线 - system - 当前用户消息（这两块永不清退）
    trigger_tokens = int(CONTEXT_BUDGET_TOKENS * COMPRESS_TRIGGER)
    fixed = estimate_tokens(system_prompt) + estimate_tokens(current_user_content)
    budget_hist = max(0, trigger_tokens - fixed)

    # 摘要本身占预算；先预留摘要 token，再装历史
    with _lock:
        entry = _store().get(conv_id)
        summary_text = (entry or {}).get('text', '')
        folded_prev = min((entry or {}).get('folded', 0), len(older))
    summary_tokens = estimate_tokens(summary_text)

    # 已在摘要里的最旧 N 轮：不进 messages，也不重复折叠（folded 单调递增）
    already_folded = older[:folded_prev]
    candidates = older[folded_prev:]

    def _pair_tokens(pair: Tuple[dict, dict]) -> int:
        return estimate_tokens(pair[0].get('content', '')) + estimate_tokens(pair[1].get('content', ''))

    # 最近轮必保（即使超预算也保留，宁可超一点不让本轮对话断裂）
    selected: List[Tuple[dict, dict]] = list(recent)
    used = sum(_pair_tokens(p) for p in selected)

    # 从未折叠的旧轮里，从最新往前贪心装
    dropped_now: List[Tuple[dict, dict]] = []
    for pair in reversed(candidates):
        t = _pair_tokens(pair)
        if used + t + summary_tokens <= budget_hist:
            selected.insert(0, pair)
            used += t
        else:
            dropped_now.insert(0, pair)  # 保持时间顺序

    summary_used = bool(summary_text)
    if dropped_now:
        # 新被丢弃的轮次折叠进摘要；folded 计数在 _fold_new_rounds 内累加
        if _fold_new_rounds(conv_id, dropped_now, summarizer):
            summary_used = True
            summary_text = get_summary(conv_id)

    messages: List[dict] = [{"role": "system", "content": system_prompt}]
    if summary_text:
        messages.append({
            "role": "system",
            "content": ("【对话早期摘要】以下是本对话早期轮次的压缩摘要，"
                        "细节可能缺失；与近期消息冲突时以近期消息为准。\n" + summary_text)
        })
    for u, a in selected:
        messages.append({"role": "user", "content": u.get('content', '')})
        messages.append({"role": "assistant", "content": a.get('content', '')})
    messages.append({"role": "user", "content": current_user_content})

    est = (estimate_tokens(system_prompt)
           + (estimate_tokens(summary_text) if summary_text else 0)
           + used
           + estimate_tokens(current_user_content))
    final = get_summary_meta(conv_id)
    src_chars = int(final.get('src_chars') or 0)
    meta = {
        "rounds_total": total_rounds,
        "rounds_included": len(selected),
        "rounds_dropped": len(dropped_now),
        "rounds_in_summary": folded_prev + len(dropped_now),
        "summary_used": summary_used,
        "summary_restored": bool(final.get('restored')),
        # 压缩观测：原文字数 → 摘要字数，ratio 越大压得越狠
        "summary_chars": len(final.get('text') or ''),
        "summary_src_chars": src_chars,
        "compress_ratio": round(src_chars / max(1, len(final.get('text') or '')), 2) if src_chars else 0,
        "compress_tier": final.get('tier', 'none'),
        "est_input_tokens": est,
        "budget": CONTEXT_BUDGET_TOKENS,
        # 进度条用：占用比例 + 自动压缩触发线（前端据此画刻度）
        "pct": round(est / CONTEXT_BUDGET_TOKENS, 4) if CONTEXT_BUDGET_TOKENS else 0.0,
        "trigger": COMPRESS_TRIGGER,
        "trigger_tokens": trigger_tokens,
    }
    return messages, meta
