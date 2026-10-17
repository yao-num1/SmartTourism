# -*- coding: utf-8 -*-
"""
数据安全导出单元测试
====================
题面要求「支持本地部署、数据安全导出」，原来只有一个前端从来没调用过的 /api/export，
还把请求体里的 filename 直接拼成路径往磁盘写。这次换成 /api/export/mine：
  1. 数据按 user_id 归属过滤，别人的会话/消息/行程/反馈/记忆一条都不能出现；
  2. 服务端不落盘、文件名由服务端生成，中文名走 RFC 5987 编码；
  3. 读库失败必须照实 500，不能回一份空壳「导出成功」；
  4. 坏掉的 data_json 行不阻断整份导出，但要留痕；
  5. 前端把只存在于这台浏览器的两份留档（mkt_history / saved_guides）合并进同一文件。

用临时库（SMARTTOURISM_DB），不碰 data/smarttourism.db。

运行：  python server/test_data_export.py
"""

from __future__ import annotations

import os
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_HERE, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_export_test_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")

import app as A          # noqa: E402
import database as db    # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}{(' -> ' + str(extra)) if extra else ''}")


def section(t):
    print("\n" + t)


ME = "export_me"
OTHER = "export_other"


def seed(user_id, tag):
    """给某个用户铺一套完整数据：会话→消息→行程→营销素材→投票→本地存储→长期记忆。"""
    s = db.create_session(user_id=user_id, session_id=f"{tag}-s1")
    db.save_message(f"{tag}-s1", "user", f"{tag} 想玩三天", message_id=f"{tag}-m1")
    ans = db.save_message(f"{tag}-s1", "agent", f"{tag} 的答复正文-勿外泄",
                          message_id=f"{tag}-m2")
    db.save_itinerary(f"{tag}-s1", destination=f"{tag}城", days=3,
                      route_json='{"d1":"x"}', budget=1500, itinerary_id=f"{tag}-iti")
    db.save_marketing_content(f"{tag}-s1", platform="xiaohongshu",
                              content_type="copywriting", raw_text=f"{tag} 的笔记文案",
                              content_id=f"{tag}-mk")
    db.save_feedback(f"{tag}-m2", f"{tag}-s1", user_id, "down", "价格不对")
    db.save_user_store_item(user_id, "itineraries", {"city": f"{tag}城", "days": 3},
                            item_id=f"{tag}-us")
    db.insert_memory({"memory_id": f"{tag}-mem", "user_id": user_id, "kind": "preference",
                      "content": f"{tag} 不吃辣", "source_session": f"{tag}-s1",
                      "embedding": "0.1,0.2"})
    return s


# ==================== [1] 归属隔离 ====================
section("\n[1] 只导出自己的数据")
seed(ME, "me")
seed(OTHER, "other")
payload = db.export_user_data(ME)
blob = str(payload)
check("导出自己的会话", payload["counts"]["sessions"] == 1, payload["counts"])
check("导出自己的消息 2 条", payload["counts"]["messages"] == 2)
check("导出自己的行程 1 条", payload["counts"]["itineraries"] == 1)
check("导出自己的营销素材 1 条", payload["counts"]["marketing_contents"] == 1)
check("导出自己的投票 1 条（含点踩理由）",
      payload["counts"]["feedback"] == 1 and
      payload["data"]["feedback"][0]["reason"] == "价格不对")
check("导出自己的本地存储条目", payload["counts"]["user_store"] == 1)
check("导出自己的长期记忆", payload["counts"]["memories"] == 1)
check("别人的会话 id 不在导出里", "other-s1" not in blob)
check("别人的正文不在导出里", "other 的答复正文-勿外泄" not in blob)
check("别人的记忆不在导出里", "other 不吃辣" not in blob)
check("别人的存储条目不在导出里", "other城" not in blob)
other_payload = db.export_user_data(OTHER)
check("反过来也一样：自己的正文不在别人那份里",
      "me 的答复正文-勿外泄" not in str(other_payload))
check("陌生人导出为空而不是全表", db.export_user_data("no_such_user")["counts"]["messages"] == 0)

# ==================== [2] 坏数据不阻断，但要留痕 ====================
section("\n[2] 坏 data_json")
conn = db.get_connection()
conn.execute("INSERT INTO user_store (id, user_id, category, data_json) VALUES (?,?,?,?)",
             ("BAD1", ME, "itineraries", "{不是 JSON"))
conn.commit()
conn.close()
p2 = db.export_user_data(ME)
bad = [x for v in p2["data"]["user_store"].values() for x in v if x.get("id") == "BAD1"]
check("坏行不抛异常", bool(bad))
check("坏行标出解析失败而不是悄悄丢掉", bad and "_parse_error" in bad[0], bad)

# ==================== [3] embedding 不带走 ====================
section("\n[3] 导出内容不含无意义大字段")
check("长期记忆里的向量没被塞进导出",
      "embedding" not in str(p2["data"]["memories"]))
check("记忆正文照常可导出", p2["data"]["memories"][0]["content"] == "me 不吃辣")

# ==================== [4] 路由：附件下载、不写盘 ====================
section("\n[4] /api/export/mine 的行为")
client = A.app.test_client()
resp = client.get(f"/api/export/mine?user_id={ME}")
check("状态 200", resp.status_code == 200, resp.status_code)
check("是 JSON 附件", "application/json" in resp.headers.get("Content-Type", ""))
dispo = resp.headers.get("Content-Disposition", "")
check("声明为 attachment 下载", dispo.startswith("attachment"), dispo)
check('带 ASCII 兜底文件名', 'filename="smarttourism-my-data.json"' in dispo, dispo)
check("中文名走 RFC 5987 编码", "filename*=UTF-8''" in dispo and "%E6%99%BA" in dispo, dispo)
check("响应不缓存", resp.headers.get("Cache-Control") == "no-store")
body = resp.get_json()
check("响应体带 counts 与 data", {"counts", "data", "user_id", "notice"} <= set(body.keys()))
check("user_id 回显请求的账号", body["user_id"] == ME)
check("说明里点明本地留档不在这份里", "localStorage" in body["notice"])
exports_dir = os.path.join(_ROOT, "data", "exports")
files_before = set(os.listdir(exports_dir)) if os.path.isdir(exports_dir) else set()
client.get(f"/api/export/mine?user_id={ME}")
files_after = set(os.listdir(exports_dir)) if os.path.isdir(exports_dir) else set()
check("导出不在服务器落文件", files_before == files_after, files_after - files_before)

# ==================== [5] 读库失败不许演成功 ====================
section("\n[5] 库坏了要照实报错")
_real_get_conn = db.get_connection


def _broken():
    raise RuntimeError("database is locked")


db.get_connection = _broken
try:
    r5 = client.get(f"/api/export/mine?user_id={ME}")
    check("读不出来就回 500 而不是空壳成功", r5.status_code == 500, r5.status_code)
except Exception as e:  # noqa: BLE001
    check("读不出来就回 500 而不是空壳成功", False, e)
finally:
    db.get_connection = _real_get_conn
check("恢复后仍能导出", client.get(f"/api/export/mine?user_id={ME}").status_code == 200)

# ==================== [6] 旧接口与路径穿越一起消失 ====================
section("\n[6] 旧 /api/export 已下线")
src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
check("不再有 /api/export 路由", "@app.route('/api/export'" not in src)
check("不再有 data/exports 写盘", "data', 'exports'" not in src and "export_dir" not in src)
check("文件名不再取自请求体", "data.get('filename'" not in src)
r6 = client.post("/api/export", json={"filename": "../../evil", "content": "x"})
check("打旧接口是 404，不再写文件", r6.status_code == 404, r6.status_code)
evil = os.path.join(_ROOT, "evil.js")
check("没有穿越到上层目录写出文件", not os.path.exists(evil))

# 用户主动读取：按 DAO 口径不加护栏（加了异常变 None 就是「导出空文件却说是成功」）
_db_src = open(os.path.join(_HERE, "database.py"), encoding="utf-8").read()
_start = _db_src.find("def export_user_data")
dao_src = _db_src[_start:_db_src.find("\n# ====================", _start)]
check("导出 DAO 找得到", _start > 0 and len(dao_src) > 200)
_above = _db_src[:_start].rstrip()
check("导出 DAO 头上没有 @guarded 装饰器（失败必须抛）",
      not _above.endswith("@guarded()") and "@guarded" not in _above[-60:], _above[-60:])
check("导出函数体内也不套护栏",
      "guarded(" not in dao_src.replace("@guarded()", ""))
check("子表按属于该用户的 session_id 过滤，不接受外部 session_id",
      "WHERE user_id = ? ORDER BY last_active DESC" in dao_src and
      "WHERE session_id IN (" in dao_src)

# ==================== [7] 前端：入口 + 合并本地留档 ====================
section("\n[7] 界面侧")
html = open(os.path.join(_ROOT, "dashboard", "index.html"), encoding="utf-8").read()
fn = html[html.find("async function exportMyData()"):]
fn = fn[:fn.find("\n// Profile tab switching")]
check("个人中心有导出入口（不是隐藏接口）", 'id="pf-export-btn"' in html)
check("点击绑定到真实函数", "expBtn.addEventListener('click', exportMyData)" in html)
check("请求带当前账号与令牌", "encodeURIComponent(currentUid())" in fn and
      "AUTH_STATE.token" in fn)
check("合并这台浏览器的营销留档", "mkt_history: uGet('mkt_history', [])" in fn)
check("合并这台浏览器保存的攻略", "saved_guides: uGet('saved_guides', [])" in fn)
check("合并说明写进文件", "browser_local_note" in fn)
check("HTTP 非 2xx 不算成功，并读出后端错误文案",
      "if (!resp.ok)" in fn and "resp.status" in fn)
check("连不上服务器给可读提示", "无法连接服务器" in fn)
check("下载走 Blob，不依赖服务端文件路径",
      "new Blob(" in fn and "URL.createObjectURL" in fn)
check("成功后报真实条数而不是空话",
      "已导出：会话 " in fn and "payload.counts" in fn)
check("失败提示与恢复按钮在 finally 里", "finally" in fn and "btn.disabled = false" in fn)
check("导出函数在 __st 里可被验证", "exportMyData}" in html.split("window.__st=")[1][:400])

print("\n" + "=" * 64)
print(f"结果: {_PASS} passed / {_FAIL} failed")
print("=" * 64)
sys.exit(0 if _FAIL == 0 else 1)
