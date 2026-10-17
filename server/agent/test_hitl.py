# -*- coding: utf-8 -*-
"""
Human-in-the-loop 单元测试
==========================
用脚本化的 Command(resume=...) 模拟「用户批准/拒绝」，确定性验证中断-恢复流程，
无需真人交互、无需数据库（commit_fn/cancel_fn 用记录型替身）。

运行：  python server/agent/test_hitl.py
"""

from __future__ import annotations

import os
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SERVER)
for p in (_SERVER, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent.hitl import (  # noqa: E402
    summarize_action, create_hitl_graph, start_confirmation, resume_confirmation,
)

_PASS = 0
_FAIL = 0


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        print(f"  ❌ {name}  {extra}")


class _Recorder:
    def __init__(self):
        self.committed = []
        self.cancelled = []

    def commit(self, action, decision):
        self.committed.append((action, decision))

    def cancel(self, action, decision):
        self.cancelled.append((action, decision))


def test_summarize_action():
    print("\n[1] summarize_action 纯函数（金额汇总 / 修改项）")
    order = summarize_action({"type": "order", "items": [
        {"name": "西湖游船票", "price": 55, "qty": 2},
        {"name": "龙井茶", "price": 120, "qty": 1}]})
    check("订单合计正确(55*2+120=230)", order["total"] == 230.0, str(order["total"]))
    check("订单标题/问题含金额", order["title"] == "下单确认" and "230.00" in order["question"], order["question"])

    mod = summarize_action({"type": "modify", "changes": ["第2天改为乌镇", "预算上调到2000"]})
    check("修改类型识别正确", mod["type"] == "modify" and mod["title"] == "行程修改确认", str(mod))
    check("修改项逐条列出", len(mod["detail"]) == 2 and "乌镇" in mod["detail"][0], str(mod["detail"]))


def test_order_approved():
    print("\n[2] 下单确认 → 用户批准 → 提交")
    rec = _Recorder()
    graph = create_hitl_graph(commit_fn=rec.commit, cancel_fn=rec.cancel)
    action = {"type": "order", "product": "西湖游船票", "price": 55, "qty": 2}

    started = start_confirmation(graph, action, thread_id="order-approve")
    check("流程在确认处暂停", started["interrupted"] is True, str(started))
    check("抛出待确认信息含金额", started["prompt"] and "110.00" in started["prompt"]["question"],
          str(started.get("prompt")))
    check("暂停时尚未提交", len(rec.committed) == 0 and len(rec.cancelled) == 0)

    done = resume_confirmation(graph, "order-approve", approved=True, note="确认无误")
    check("状态=committed", done["status"] == "committed", str(done))
    check("commit_fn 被调用一次", len(rec.committed) == 1, str(rec.committed))
    check("决策记录 approved=True", done["decision"].get("approved") is True, str(done["decision"]))


def test_order_rejected():
    print("\n[3] 下单确认 → 用户拒绝 → 取消")
    rec = _Recorder()
    graph = create_hitl_graph(commit_fn=rec.commit, cancel_fn=rec.cancel)
    action = {"type": "order", "product": "某高价套餐", "price": 9999, "qty": 1}

    started = start_confirmation(graph, action, thread_id="order-reject")
    check("流程暂停", started["interrupted"] is True)
    done = resume_confirmation(graph, "order-reject", approved=False, note="太贵了")
    check("状态=cancelled", done["status"] == "cancelled", str(done))
    check("cancel_fn 被调用、commit 未调用", len(rec.cancelled) == 1 and len(rec.committed) == 0)
    check("取消原因被带回", "太贵了" in done["result"], done["result"])


def test_modify_confirmation():
    print("\n[4] 行程修改二次确认 → 批准")
    rec = _Recorder()
    graph = create_hitl_graph(commit_fn=rec.commit, cancel_fn=rec.cancel)
    action = {"type": "modify", "target": "杭州3日游",
              "changes": ["第2天改为乌镇", "预算上调到2000"]}

    started = start_confirmation(graph, action, thread_id="modify-1")
    check("修改流程暂停", started["interrupted"] is True)
    check("确认信息复述了修改项",
          started["prompt"] and any("乌镇" in d for d in started["prompt"]["detail"]),
          str(started.get("prompt")))
    done = resume_confirmation(graph, "modify-1", approved=True)
    check("修改被确认执行", done["status"] == "committed" and len(rec.committed) == 1, str(done))


def test_state_persistence_across_pause():
    print("\n[5] checkpointer 断点续跑：暂停后原始 action 不丢失")
    rec = _Recorder()
    graph = create_hitl_graph(commit_fn=rec.commit, cancel_fn=rec.cancel)
    action = {"type": "order", "product": "宋城千古情门票", "price": 320, "qty": 3}
    start_confirmation(graph, action, thread_id="persist-1")
    done = resume_confirmation(graph, "persist-1", approved=True)
    committed_action = rec.committed[0][0]
    check("恢复后仍拿到完整原始 action",
          committed_action.get("product") == "宋城千古情门票" and committed_action.get("qty") == 3,
          str(committed_action))


if __name__ == "__main__":
    print("=" * 60)
    print("🧪 Human-in-the-loop 测试")
    print("=" * 60)
    test_summarize_action()
    test_order_approved()
    test_order_rejected()
    test_modify_confirmation()
    test_state_persistence_across_pause()
    print("\n" + "=" * 60)
    print(f"结果: {_PASS} 通过 / {_FAIL} 失败")
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)
