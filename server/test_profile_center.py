# -*- coding: utf-8 -*-
"""
个人中心（Profile）测试
======================
个人中心原来四栏：我的订单 / 我的行程 / 酒店预订 / 机票预订。核对下来只有行程有真实链路
（规划页「保存到个人行程」→ /api/user/booking → user_store），其余三栏是空壳：平台没有购票
与机票能力（美团插件只返回酒店/门票预订入口），sm_tkt_orders 更是没有任何代码写过。四栏底下
还都挂着「+ 手动添加」，等于让用户手工编数据。

现在的口径：
  1. 只留三个栏，且每个栏都对应一条真实链路——
     行程（服务端 user_store）/ 营销内容（本地 mkt_history，营销页生成时自动留档）/
     我保存的攻略（本地 saved_guides，社区点收藏时记录）；
  2. 「+ 手动添加」全部去掉，界面不再提供编数据的入口；
  3. 删除只删「自己这一条」：新增 /api/user/item 按 user_id 校验所有权，
     取代原先 clear+整份重发（任一次请求失败就会把用户别的记录清掉）；删不到就回 404，
     不做假成功；
  4. 行程正文是模型生成/用户输入拼出的 markdown，详情弹窗必须先转义再套标签（存储型 XSS）；
  5. 顺带修：窄屏把登录/设置按钮连同 .nav-status 一起藏了、用户名竖排、加载失败不吭声。

用临时库（SMARTTOURISM_DB），不发真实网络请求，也不碰用户现有数据。

运行：  python server/test_profile_center.py
"""

from __future__ import annotations

import os
import re
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

# DB_PATH 在 database 模块加载时求值，必须先设好再 import app
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_profile_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")

import app as A          # noqa: E402
import database as db    # noqa: E402

DASHBOARD = os.path.join(_ROOT, "dashboard", "index.html")
_PASS = 0
_FAIL = 0

# 空壳栏位的文案：只要还在页面里，就说明界面还在承诺平台做不到的事。
# 「酒店预订」不在此列——对话里的美团酒店入口是真的，假的只是个人中心那个从没写过数据的栏位。
FAKE_CLAIMS = ("我的订单", "机票预订", "在对话中购票后",
               "机票预订记录将出现在这里", "在对话中预订酒店后")


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}{(' -> ' + str(extra))[:300] if extra else ''}")


def section(title):
    print("\n" + title)


def _slice(text, start_marker, end_marker):
    """取一段函数体文本，用于做「顺序」断言（比如转义必须先于套标签）。"""
    i = text.find(start_marker)
    j = text.find(end_marker, i + 1)
    if i < 0:
        return ""
    return text[i:j if j > i else len(text)]


def main():
    print("=" * 64)
    print("个人中心 · 栏目真实性 / 删除所有权 / 本地留档")
    print("=" * 64)

    # ---------- [1] DAO：删除必须带 user_id ----------
    section("\n[1] delete_user_store_item 只删自己的行")
    owner = "pf_owner_1"
    other = "pf_other_1"
    it = db.save_user_store_item(owner, "itineraries",
                                 {"title": "杭州 3天行程", "city": "杭州", "days": 3,
                                  "content": "## 第一天\n西湖", "status": "confirmed"})
    hotel = db.save_user_store_item(other, "hotels", {"title": "如家精选酒店", "city": "杭州"})
    check("行程已落库", bool(it and it.get("id")), it)

    wrong = db.delete_user_store_item(hotel["id"], owner)
    check("拿别人的 id 删不动（返回 0 行）", wrong == 0, wrong)
    still = db.get_user_store_items(other)
    check("别人的记录还在", len(still["hotels"]) == 1, still["hotels"])

    mine = db.get_user_store_items(owner)["itineraries"]
    check("自己的 id 删得掉，返回受影响行数", db.delete_user_store_item(mine[0]["id"], owner) == 1)
    check("删完确实查不到了", db.get_user_store_items(owner)["itineraries"] == [])
    check("删不存在的 id 返回 0 而不是报错", db.delete_user_store_item("NOPE0000000000", owner) == 0)

    # ---------- [2] 路由：/api/user/item ----------
    section("\n[2] POST /api/user/item 的所有权与失败口径")
    client = A.app.test_client()

    keep = db.save_user_store_item("pf_route_a", "itineraries",
                                   {"title": "宁波 2天行程", "city": "宁波", "days": 2, "content": "x"})
    foreign = db.save_user_store_item("pf_route_b", "itineraries",
                                      {"title": "温岭 1天行程", "city": "温岭", "days": 1, "content": "y"})

    r = client.post("/api/user/item", json={"user_id": "pf_route_a"})
    check("缺 id 直接 400", r.status_code == 400 and r.get_json()["success"] is False, r.get_json())

    r = client.post("/api/user/item", json={"id": foreign["id"], "user_id": "pf_route_a"})
    body = r.get_json()
    check("删别人的记录 404，不报成功", r.status_code == 404 and body["success"] is False, body)
    check("404 时给了人话错误", bool(str(body.get("error", ""))), body)
    check("别人的那条仍在库里", len(db.get_user_store_items("pf_route_b")["itineraries"]) == 1)

    r = client.post("/api/user/item", json={"id": keep["id"], "user_id": "pf_route_a"})
    check("删自己的记录 200", r.status_code == 200 and r.get_json()["success"] is True, r.get_json())
    check("返回删掉的 id", r.get_json().get("deleted") == keep["id"], r.get_json())
    check("删后 profile 不再返回它",
          db.get_user_store_items("pf_route_a")["itineraries"] == [])

    r = client.post("/api/user/item", json={"id": "GHOST0000000", "user_id": "pf_route_a"})
    check("id 不存在也是 404", r.status_code == 404, r.status_code)

    # 登录令牌优先于显式 user_id：带着 A 的 token 不能删 B 的记录
    r = client.get("/api/user/profile?user_id=" + "pf_route_b")
    prof = r.get_json()
    check("profile 接口仍返回行程列表", prof["success"] is True and "itineraries" in prof["data"], prof)

    # ---------- [3] 页面：空壳栏位与手动添加入口都没了 ----------
    section("\n[3] dashboard：只留三个真实栏位")
    html = open(DASHBOARD, encoding="utf-8").read()

    gone = ("data-ptab=\"orders\"", "data-ptab=\"hotels\"", "data-ptab=\"flights\"",
            "ps-orders", "ps-hotels", "ps-flights",
            "profile-orders-list", "profile-hotels-list", "profile-flights-list",
            "addProfileItem", "pf-add-btn", "sm_tkt_orders", "saveProfileToServer",
            "category=orders", "/api/user/order")
    for token in gone:
        check("页面不再出现 " + token, token not in html, token)

    for word in FAKE_CLAIMS:
        check("不再有假承诺文案「" + word + "」", word not in html, word)

    check("保留 我的行程", "data-ptab=\"itineraries\"" in html and "id=\"ps-itineraries\"" in html)
    check("新增 我的营销内容", "data-ptab=\"marketing\"" in html and "id=\"ps-marketing\"" in html)
    check("新增 我保存的攻略", "data-ptab=\"guides\"" in html and "id=\"ps-guides\"" in html)
    check("三个栏位各有列表容器",
          all(x in html for x in ("profile-itineraries-list", "profile-marketing-list", "profile-guides-list")))

    # ---------- [4] 空态提示说的是真实产出路径 ----------
    section("\n[4] 空态提示指向真正会产生数据的动作")
    empty_block = _slice(html, "const PROFILE_EMPTY = {", "};")
    check("行程提示指向「保存到个人行程」", "保存到个人行程" in empty_block, empty_block)
    check("营销提示指向营销页并说明上限 30 条", "在「营销」页生成文案后会自动留档" in empty_block
          and "30" in empty_block, empty_block)
    check("攻略提示指向社区收藏", "在「社区」点「收藏这篇攻略」" in empty_block, empty_block)
    check("提示里的入口名与导航一致",
          all(x in html for x in ("data-page=\"plan\"", "data-page=\"marketing\"", "data-page=\"community\"")))

    # ---------- [5] 删除：只删这一条，失败不假装成功 ----------
    section("\n[5] 删除链路")
    check("前端调 /api/user/item", "/api/user/item" in html)
    check("删除带上 id 与当前用户",
          re.search(r"body:\s*JSON\.stringify\(\{\s*id:\s*item\.id,\s*user_id:\s*currentUid\(\)\s*\}\)", html) is not None)
    check("删失败就提示并留在界面上（没有悄悄 splice）",
          "删除失败，请稍后重试" in html and "无法连接服务器" in html)
    check("删除成功后重新拉列表而不是本地假删", "await loadProfile();" in html)
    del_body = _slice(html, "async function deleteProfileItem(type, idx) {", "\n// Profile tab switching")
    check("没有服务端 id 的行程只清界面那条", "profileData.itineraries = list.filter" in del_body, del_body[:400])
    check("营销删除写回本地 mkt_history", "uSet('mkt_history', rest)" in del_body)
    check("攻略取消保存写回 saved_guides 并退还收藏数",
          "uSet('saved_guides', rest)" in del_body and "_bumpCommunityFav(item.id, -1)" in del_body)
    check("攻略栏的按钮写「取消保存」而不是「删除」",
          "type === 'guides' ? '取消保存'" in html)

    # ---------- [6] 详情弹窗：转义先于 markdown 套标签 ----------
    section("\n[6] 详情弹窗不留存储型 XSS")
    md_body = _slice(html, "function _pfMarkdown(text) {", "\n\nfunction viewProfileDetail")
    check("先转义 & < >",
          0 < md_body.find("replace(/&/g") < md_body.find("replace(/### "), md_body[:300])
    check("转义在套标签之前完成", md_body.find("&lt;") < md_body.find("<h4>"), md_body[:300])
    detail_body = _slice(html, "function viewProfileDetail(type, idx) {", "\nasync function deleteProfileItem")
    check("正文只经 _pfMarkdown 输出", "_pfMarkdown(text)" in detail_body)
    check("详情弹窗不再把 item.content 直接拼进 innerHTML",
          "item.content +" not in detail_body and "/### (.*)/" not in detail_body, detail_body[:300])
    check("标题/副标题走 escapeHtml",
          "escapeHtml(title)" in detail_body and "escapeHtml(sub)" in detail_body)
    check("旧的未转义函数已删除", "function viewItineraryDetail" not in html)

    # ---------- [7] 本地两份留档：读的是真数据源 ----------
    section("\n[7] 营销内容与攻略的数据源")
    local_body = _slice(html, "function _readLocalArchives() {", "\nasync function loadProfile")
    check("营销内容读 mkt_history", "uGet('mkt_history', [])" in local_body, local_body)
    check("攻略读 saved_guides", "uGet('saved_guides', [])" in local_body, local_body)
    load_body = _slice(html, "async function loadProfile() {", "\nfunction renderAllProfileLists")
    check("进个人中心会重读本地留档，删掉的东西不会复活", "_readLocalArchives();" in load_body, load_body[:300])
    check("首屏也先渲染本地留档，不是一句空等",
          "_readLocalArchives();\n  renderAllProfileLists();" in html)
    check("行程读服务端 data.itineraries", "data.data.itineraries" in load_body.replace(" ", ""))
    check("加载失败会 toast，而不是只 console.error", "showToast('行程列表加载失败" in load_body)
    check("HTTP 非 2xx 也算失败", "if (!resp.ok || !data.success) throw" in load_body)

    check("收藏记录的是收藏当时的快照（含正文与时间）",
          all(x in html for x in ("function _toggleSavedGuide(post)", "content: String(post.content",
                                  "ts: Date.now()")))
    check("同一篇不会重复保存", re.search(r"list\.findIndex\(function\s*\(g\)\s*\{\s*return String\(g\.id\)", html) is not None)
    check("收藏做成开关：保存 +1 / 取消 -1", "_bumpCommunityFav(id, saved ? 1 : -1)" in html)
    check("社区按钮按保存态换文案", "isGuideSaved(post.id)" in html and "★ 已保存" in html)
    check("弹窗收藏按钮有 id 可回写文案", "id=\"cdm-fav-btn\"" in html)
    check("营销栏用真实类型名", "MKT_TYPE_LABELS" in html and "video_script: '短视频'" in html)

    # ---------- [8] 顺带修的界面问题 ----------
    section("\n[8] 窄屏与用户名显示")
    mq = _slice(html, "@media(max-width:620px){", "}\n")
    check("620px 下不再整块藏掉 .nav-status", ".nav-status{display:none}" not in mq, mq[:200])
    check("改为只藏状态文字，登录/设置留在屏幕上", ".nav-status-text{display:none}" in mq, mq[:200])
    check("窄屏给登录按钮留出位置", ".nav-user-btn{max-width:88px" in mq, mq[:260])
    check("用户名不换行竖排", re.search(r"\.pa-name\{[^}]*white-space:nowrap", html) is not None)
    check("卡片「查看详情」按钮文字不再是低透明度色",
          re.search(r"\.pf-card-view\{[^}]*color:var\(--accent\)", html) is not None)

    # ---------- [9] 调试出口不再指向已删除的函数 ----------
    section("\n[9] window.__st 出口自洽")
    st = _slice(html, "window.__st={", "};")
    check("__st 不再导出 addProfileItem", "addProfileItem" not in st, st[:200])
    for fn in ("deleteProfileItem", "viewProfileDetail"):
        check("__st 导出的 " + fn + " 有定义", ("function " + fn + "(") in html, fn)

    print("\n" + "=" * 64)
    print(f"结果: {_PASS} passed / {_FAIL} failed")
    print("=" * 64)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
