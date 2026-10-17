# -*- coding: utf-8 -*-
"""
生图成本（指纹复用 + 计量）测试
==============================
Seedream 按张计费，一篇博客出 3 张图，是全项目唯一按「张」花钱的地方。
这里要证明两件事：**同一张图确实只付一次钱**，以及**省下的张数量得出来**。
覆盖：
  1. 指纹只认影响画面的入参（模型/尺寸/提示词/负面提示词/场景/图序/水印），
     不含 API Key、不含时间戳；
  2. 第二次同样请求**一次网都不出**，直接回同一张已落盘的图；
  3. 换任意一个画面参数 → 是新图，照常出网；
  4. 图被清理过 → 当没命中重新生成，绝不返回打不开的地址（死链）；
  5. 只有校验通过并落盘成功的图才进缓存；坏图/失败不缓存（否则错误被永久复用）；
  6. 计费口径：没 Key、没提示词这类前置拦截**不算出网请求**（不虚报省了多少）；
     失败算已发出的请求，并归类原因（欠费/超时/结果无效…）；
  7. 台账是旁路：喂垃圾参数、库坏了都不能抛异常，也不能影响出图；
  8. 开关 SMARTTOURISM_ARK_IMAGE_CACHE=0 时退回「每次都真生成」；
  9. 没配单价只报张数；配了才算钱，且写明是上限估计；
 10. 前端超时必须**不早于**服务端出图预算（否则用户看不到图却已经付费）；
 11. 没有给上游请求体加任何新字段（省钱不许改动出图行为）；
 12. /api/admin/ops 暴露 image_usage，运维面板渲染「命中复用 / 复用率」。

用临时库（SMARTTOURISM_DB）+ 临时 static 目录，全程离线（requests 被替身接管），
不碰 data/ 与 dashboard/static 下任何真实数据。

运行：  python server/test_image_cost.py
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

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_arkcost_test_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")
os.environ.pop("SMARTTOURISM_ARK_IMAGE_CACHE", None)
os.environ.pop("SMARTTOURISM_PRICE_ARK_PER_IMAGE", None)

import app as A  # noqa: E402
import requests  # noqa: BLE001  (仅取 exceptions 类型)

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


# ---------- 替身：ARK 的出图请求与临时图下载 ----------

GOOD_JPEG = b"\xff\xd8\xff" + b"jpgdata" * 1200          # > 5120 字节，魔术字节对
GOOD_PNG = b"\x89PNG\r\n\x1a\n" + b"pngdata" * 1200
BAD_TINY = b"\xff\xd8\xff" + b"x" * 100                   # 太小 → 判定为无效图
BAD_TEXT = b"not an image at all, long enough here" * 300


class FakeResp:
    def __init__(self, status=200, body=None, text="", content=b"", url_body=None):
        self.status_code = status
        self._body = body if body is not None else {
            "data": [{"url": url_body or "https://ark-temp.example/seedream.jpg"}]}
        self.text = text
        self.content = content
        self.encoding = "utf-8"

    def json(self):
        return self._body


class ArkShim:
    """记下发出去的请求；post/get 各按脚本给结果。"""

    def __init__(self, posts=None, gets=None):
        self.posts = list(posts or [])
        self.gets = list(gets or [])
        self.sent = []          # 每次 post 的 payload
        self.gets_called = 0
        self.exceptions = requests.exceptions

    def post(self, url, headers=None, json=None, timeout=None):
        self.sent.append(dict(json or {}))
        if not self.posts:
            raise AssertionError("不该再出网了：指纹该命中复用（post 被多调了一次）")
        kind, val = self.posts.pop(0)
        if kind == "raise":
            raise val
        return val

    def get(self, url, timeout=None, headers=None):
        self.gets_called += 1
        if not self.gets:
            return FakeResp(status=200, content=GOOD_JPEG)
        kind, val = self.gets.pop(0)
        if kind == "raise":
            raise val
        return val


def snap():
    return {k: v for k, v in A._ARK_USAGE.items() if k != "by_scene"}


STATIC = tempfile.mkdtemp(prefix="st_ark_static_")
A.app.static_folder = STATIC
# 测试内不碰真实 Key：只在内存里放一个假 Key，也不打印它的内容
A.ARK_CONFIG = {"api_key": "test-only-key", "model": "test-model", "poster_size": "2K"}


def run(scene="blog", prompt="西湖秋色", img_num=1, title="", slogan="",
        shim=None):
    """跑一次真实出图函数，返回 (success, url/错误)。shim 由调用方持有以便查请求次数。"""
    shim = shim if shim is not None else ArkShim(posts=[("ok", FakeResp())])
    old = A.requests
    A.requests = shim
    try:
        return A._generate_ark_image(prompt, scene=scene, title=title,
                                     slogan=slogan, img_num=img_num)
    finally:
        A.requests = old


# ==================== [1] 指纹口径 ====================
section("\n[1] 指纹只认影响画面的入参")
base = A._ark_fingerprint("p", "blog", 1, "2K", "m")
check("同样入参指纹稳定", base == A._ark_fingerprint("p", "blog", 1, "2K", "m"))
for name, args in (("提示词", ("p2", "blog", 1, "2K", "m")),
                   ("场景", ("p", "poster", 1, "2K", "m")),
                   ("图序", ("p", "blog", 2, "2K", "m")),
                   ("尺寸", ("p", "blog", 1, "1K", "m")),
                   ("模型", ("p", "blog", 1, "2K", "m2"))):
    check(f"换{name} → 不同指纹（会重新生成）", base != A._ark_fingerprint(*args))
check("换负面提示词 → 不同指纹",
      base != A._ark_fingerprint("p", "blog", 1, "2K", "m", "模糊"))
check("带不带水印 → 不同指纹（水印会改画面）",
      A._ark_fingerprint("p", "b", 1, "2K", "m", "", False)
      != A._ark_fingerprint("p", "b", 1, "2K", "m", "", True))
check("指纹不含时间戳这种东西（否则自己就成了缓存杀手）",
      A._ark_fingerprint("p", "blog", 1, "2K", "m")
      == A._ark_fingerprint("p", "blog", 1, "2K", "m"))

# ==================== [2] 第二次一次网都不出 ====================
section("\n[2] 同一张图不付第二次钱")
before = snap()
ok1, url1 = run(prompt="三张里的一张-风光")
mid = snap()
check("第一次真的出网了", ok1 is True and mid["requests"] - before["requests"] == 1,
      (ok1, url1, mid["requests"]))
check("落盘文件存在", os.path.isfile(os.path.join(STATIC, "poster",
                                                  os.path.basename(url1))), url1)
shim2 = ArkShim(posts=[])          # 空脚本：再 post 就抛，用来证明「一次网都没出」
b2 = snap()
ok2, url2 = run(prompt="三张里的一张-风光", shim=shim2)
a2 = snap()
check("第二次直接复用，没有出网", ok2 is True and url2 == url1 and not shim2.sent)
check("复用计数 +1，出网计数不动",
      a2["reused"] - b2["reused"] == 1 and a2["requests"] == b2["requests"],
      (a2["reused"], b2["reused"]))
check("库里累计复用跨重启也认（持久计数已 +1）",
      (A.db.ark_image_cache_stats() or {}).get("reuses", 0) >= 1,
      A.db.ark_image_cache_stats())

# ==================== [3] 换参数就是另一张图 ====================
section("\n[3] 换了画面参数就该重新生成")
b3 = snap()
ok3, url3 = run(prompt="三张里的一张-风光", img_num=2)
check("同提示词不同图序 → 新图新地址", ok3 and url3 != url1)
check("确实出网了", snap()["requests"] - b3["requests"] == 1)
b3b = snap()
ok3c, url3c = run(scene="poster", prompt="三张里的一张-风光", img_num=2)
check("不同场景（提示词模板也不同）→ 再出一张",
      ok3c and url3c != url3 and snap()["requests"] - b3b["requests"] == 1)

# ==================== [4] 图被清理过不能给死链 ====================
section("\n[4] 文件没了就当没命中")
gone = os.path.join(STATIC, "poster", os.path.basename(url1))
os.remove(gone)
b4 = snap()
ok4, url4 = run(prompt="三张里的一张-风光")
check("复用不成立时重新生成而不是返回死链",
      ok4 and url4 != url1 and os.path.isfile(os.path.join(STATIC, "poster",
                                                           os.path.basename(url4))))
check("重新生成算一次出网（不假装省了）", snap()["requests"] - b4["requests"] == 1)
b4b = snap()
ok4c, url4c = run(prompt="三张里的一张-风光", shim=ArkShim(posts=[]))
check("新图落盘后又能复用了", ok4c and url4c == url4 and snap()["reused"] > b4b["reused"])

# ==================== [5] 坏图/失败不进缓存 ====================
section("\n[5] 只有校验通过的真图才缓存")
b5 = snap()
entries_before = (A.db.ark_image_cache_stats() or {}).get("entries", 0)
shim_bad = ArkShim(posts=[("ok", FakeResp())], gets=[("ok", FakeResp(content=BAD_TINY))])
ok5, err5 = run(prompt="坏图测试", img_num=3, shim=shim_bad)
check("太小的图判为无效并如实报错", ok5 is False and "有效图片" in str(err5), err5)
check("失败计入 requests+failed（可能已被上游计费）",
      snap()["failed"] > b5["failed"] and snap()["requests"] > b5["requests"])
shim_bad2 = ArkShim(posts=[], gets=[])
ok5b, err5b = run(prompt="坏图测试", img_num=3, shim=shim_bad2)
check("坏图没有被缓存下来（否则错误会被永久复用）",
      ok5b is False and shim_bad2.sent, (ok5b, str(err5b)[:60]))
check("失败原因归到「结果无效」",
      (snap()["fail_reasons"] or {}).get("结果无效", 0) >= 1 or
      A._ark_fail_kind("生成结果不是有效图片") == "结果无效")

# ==================== [6] 前置拦截不算出网请求 ====================
section("\n[6] 没花钱的不记账")
b6 = snap()
old_cfg = A.ARK_CONFIG
A.ARK_CONFIG = {"api_key": "", "model": "test-model"}
ok6, err6 = run(prompt="没有 Key")
A.ARK_CONFIG = {"api_key": "k", "model": "test-model"}
ok6b, err6b = run(prompt="")
A.ARK_CONFIG = old_cfg
check("没 Key / 没提示词直接失败", ok6 is False and ok6b is False)
check("这两种前置拦截不计出网请求（不虚报省钱）",
      snap()["requests"] == b6["requests"] and snap()["failed"] == b6["failed"],
      (snap()["requests"], b6["requests"]))

# 上游非 200 → 计一次请求 + 失败分类
shim_402 = ArkShim(posts=[("ok", FakeResp(status=403, text="AccountOverdueError",
                                          body={}))])
b6c = snap()
ok6c, err6c = run(prompt="欠费场景", scene="poster", shim=shim_402)
check("上游报错计为已出网 + 失败", ok6c is False
      and snap()["requests"] > b6c["requests"] and snap()["failed"] > b6c["failed"])
check("欠费归到「欠费」这一类", A._ark_fail_kind(err6c) == "欠费", err6c)
check("呆分类不会漏（未知错误也有归属）",
      A._ark_fail_kind("完全没见过的报错") == "上游报错"
      and A._ark_fail_kind("图片生成超时") == "生成超时")

# ==================== [7] 旁路与路径安全 ====================
section("\n[7] 记账是旁路，缓存不许变成漏洞")
A._note_ark_call(None, None, None, None)
A._note_ark_call("blog", "reused", -5, None)
check("喂垃圾参数不抛异常", True)
check("负数耗时不会被记成负数", A._ARK_USAGE["duration_ms_total"] >= 0)
_positive = A._ark_static_url_to_path(url4)
check("已存在的图能解析回本地路径（命中复用的前提）",
      _positive.endswith(os.path.basename(url4)) and os.path.isfile(_positive), _positive)
trav = A._ark_static_url_to_path("/static/poster/../../../../etc/passwd")
check("路径遍历的库内容当作没命中（不读仓库外的文件）", trav == "", trav)
check("非图片地址不当命中", A._ark_static_url_to_path("/static/poster/x.txt") == "")
check("不存在的文件名当作没命中", A._ark_static_url_to_path("/static/poster/nope.jpg") == "")
# 缓存表坏掉时必须退化成「没命中、正常生成」，而不是把出图请求整个失败掉。
# 注意要打穿到 get_connection（DAO 的 @guarded() 就在那一层兜住），
# 直接把整个 DAO 换成会抛异常的 lambda 等于把护栏本身拆掉，测的不是真实路径。
_conn = A.db.get_connection


def _boom():
    raise RuntimeError("库炸了")


A.db.get_connection = _boom
try:
    check("表读不出来时 lookup 降级返回 None（护栏生效）",
          A.db.lookup_ark_image("any") is None)
    check("统计降级为空 dict", A.db.ark_image_cache_stats() == {})
    ok7, url7 = run(prompt="库坏了还要出图", img_num=1, title="T", slogan="S")
    check("缓存表坏了退化成正常生成，不影响出图", ok7 is True, url7)
finally:
    A.db.get_connection = _conn

# ==================== [8] 开关 ====================
section("\n[8] SMARTTOURISM_ARK_IMAGE_CACHE=0 时不复用")
A.ARK_CACHE_ENABLED = False
b8 = snap()
ok8, url8 = run(prompt="三张里的一张-风光", shim=ArkShim(posts=[("ok", FakeResp())]))
check("关掉开关就每次真出网", snap()["requests"] - b8["requests"] == 1 and ok8)
A.ARK_CACHE_ENABLED = True

# ==================== [9] 张数与钱的口径 ====================
section("\n[9] 没配单价只报张数")
os.environ.pop("SMARTTOURISM_PRICE_ARK_PER_IMAGE", None)
st9 = A._ark_usage_stats()
for k in ("requests", "success", "failed", "reused", "by_scene", "reuse_rate",
          "cache_entries", "cache_reuses_total", "note", "enabled"):
    check(f"image_usage 带 {k}", k in st9)
check("没配单价时 cost 为空", st9.get("cost") is None)
check("没配单价时说明为什么没钱", "单价" in (st9.get("cost_note") or ""))
check("口径写明复用=净省、失败仍算已出网",
      "复用" in st9["note"] and "失败" in st9["note"])
check("写明哪部分是跨重启的", "重启" in st9["cache_persist_note"])
os.environ["SMARTTOURISM_PRICE_ARK_PER_IMAGE"] = "0.2"
c9 = A._ark_usage_stats()
check("配了单价才算钱", isinstance(c9.get("cost"), dict) and c9["cost"]["amount"] > 0)
check("钱按出网张数算，复用不算钱",
      c9["cost"]["amount"] == round(c9["requests"] * 0.2, 4), c9["cost"])
check("钱注明是上限估计", "上限估计" in c9["cost"]["note"])
os.environ.pop("SMARTTOURISM_PRICE_ARK_PER_IMAGE", None)
check("复用率不会大于 1", (st9["reuse_rate"] or 0) <= 1.0)

# ==================== [10] 上游请求体没被改动 ====================
section("\n[10] 省钱不许改动画行为")
shim10 = ArkShim(posts=[("ok", FakeResp())])
run(prompt="检查请求体", img_num=1, title="标题", slogan="口号", shim=shim10)
allowed = {"model", "prompt", "sequential_image_generation", "size",
           "response_format", "stream", "watermark", "negative_prompt"}
check("没有往上游塞 fingerprint/cache 之类新字段",
      all(set(p.keys()) <= allowed for p in shim10.sent),
      [sorted(p.keys()) for p in shim10.sent])
_src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
check("提示词本身没为省钱被改短（水印开关仍按配置传）",
      '"watermark": bool(ARK_CONFIG.get("watermark", False))' in _src)

# ==================== [11] 超时错位：付了钱得让用户看到图 ====================
section("\n[11] 前端超时不早于服务端出图预算")
_dash = open(os.path.join(_ROOT, "dashboard", "index.html"), encoding="utf-8").read()
_seg = _dash[_dash.find("async function generateBlogImage"):_dash.find(
    "async function generateBlogImage") + 5000]
_srv_gen = 150      # app.py: requests.post(..., timeout=150)
_srv_dl = 60        # app.py: requests.get(temp_url, timeout=60)
import re as _re  # noqa: E402
_m = _re.search(r"arkTimer\s*=\s*setTimeout\(.*?\},\s*(\d+)\s*\)", _seg, _re.S)
check("博客配图仍有客户端超时（不是把超时删掉了事）", bool(_m), _seg[:200])
if _m:
    client_ms = int(_m.group(1))
    check(f"客户端 {client_ms}ms ≥ 服务端预算 {(_srv_gen + _srv_dl) * 1000}ms",
          client_ms >= (_srv_gen + _srv_dl) * 1000, client_ms)
check("服务端出图预算确实是 150s + 60s",
      "requests.post(ARK_API_URL, headers=headers, json=payload, timeout=150)" in _src
      and "requests.get(temp_url, timeout=60)" in _src)

# ==================== [12] 接口与面板 ====================
section("\n[12] /api/admin/ops 与运维面板")
client = A.app.test_client()
ops = client.get("/api/admin/ops").get_json() or {}
iu = ops.get("image_usage") or {}
check("ops 里有 image_usage 段", isinstance(iu, dict) and "requests" in iu,
      list(iu.keys())[:6])
check("ops 报的是真实累计（本测试已经出网若干次）", iu.get("requests", 0) >= 1)
check("库里指纹条数与复用总数也透出",
      "cache_entries" in iu and "cache_reuses_total" in iu)
_ops = open(os.path.join(_HERE, "ops_log.py"), encoding="utf-8").read()
check("面板渲染生图区块", 'id="imgusage"' in _ops and "image_usage" in _ops)
check("面板把复用讲成「0 成本」", "命中复用" in _ops and "出图请求（计费）" in _ops)
check("面板展示跨重启的累计复用", "累计复用" in _ops and "cache_reuses_total" in _ops)
check("面板写明开关怎么关", "SMARTTOURISM_ARK_IMAGE_CACHE" in _ops)
check("面板按场景分组看得到", "by_scene" in _ops)
check("失败原因分布也上墙", "fail_reasons" in _ops and "失败原因分布" in _ops)

print(f"\n{'=' * 46}\n结果: {_PASS} passed / {_FAIL} failed"
      f"（共 {_PASS + _FAIL}）")
sys.exit(1 if _FAIL else 0)
