# -*- coding: utf-8 -*-
"""
密钥卫生测试：源码里不许留可用凭据，自检脚本得抓得到裸 Key
=========================================================
背景：app.py 曾把随项目发布的默认示例 API Key 原文写进源码（`_DEFAULT_PLACEHOLDER_KEY`），
而 prepare_submission.py 的自检特征串只认 sk- / ark- 前缀，抓不到那串裸 32 位十六进制，
于是「敏感信息 0 残留」这句话在两份交付稿里都是不成立的。本文件钉住修好后的状态：

  1. 全量扫描纳入提交包的源码/文档，逐条用自检脚本的特征串搜，0 命中；
  2. 默认 Key 的判定改成 sha256 指纹比对，比对逻辑仍然认得「裸形态」和「sk- 前缀形态」；
  3. validate_config 三态照旧：没 Key=error、还在用默认 Key=warn、自己的 Key=ok；
  4. 自检脚本新增的通用特征抓得到引号包裹的 32 位十六进制，且不误报 64 位指纹；
  5. 自检报告里的命中值已掩码，不再把凭据前 12 位打进控制台。

用替身指纹与假 Key，测试文件里不出现任何真实凭据；不联网、不调模型。

运行：  python server/test_secret_hygiene.py
"""

from __future__ import annotations

import hashlib
import os
import py_compile
import re
import shutil
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

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_secret_test_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")

import app as A  # noqa: E402
import prepare_submission as PS  # noqa: E402

_PASS = 0
_FAIL = 0

# 假凭据：只用于验证「判定逻辑」与「扫描特征」，不是任何真实 Key
FAKE_HEX32 = "a" * 32
FAKEY_KEY = "sk-" + FAKE_HEX32
FAKE_DIGESTS = frozenset({hashlib.sha256(FAKE_HEX32.encode()).hexdigest(),
                          hashlib.sha256(FAKEY_KEY.encode()).hexdigest()})
FAKE_64 = hashlib.sha256(b"not-a-credential").hexdigest()


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [OK]   %s%s" % (name, ("  <- " + extra) if extra else ""))
    else:
        _FAIL += 1
        print("  [FAIL] %s%s" % (name, ("  <- " + extra) if extra else ""))


def section(title):
    print("\n== %s ==" % title)


def is_ignored_path(rel):
    """与 prepare_submission 的排除口径一致：_ 开头的路径、示例模板、缓存目录不进提交包。"""
    parts = rel.replace("\\", "/").split("/")
    if any(part.startswith("_") for part in parts if part):
        return True
    return "__pycache__" in parts


def scan_targets():
    """纳入提交包的文本文件：server/ plugins/ dashboard/ 三个目录 + 仓库根一层文件。
    刻意不整仓递归，免得读到 data/（本机配置与用户数据，不进提交包也不该被测试打开）。"""
    out = []
    bases = [os.path.join(_ROOT, d) for d in ("server", "plugins", "dashboard")]
    bases.append(_ROOT)
    for base in bases:
        if not os.path.isdir(base):
            continue
        shallow = base == _ROOT
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith("_") and d not in ("__pycache__", ".git")]
            if shallow:
                dirs[:] = []  # 根目录只扫这一层，子目录已单独覆盖
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in (".py", ".md", ".html", ".js", ".bat", ".txt", ".json"):
                    continue
                fp = os.path.join(root, fn)
                rel = os.path.relpath(fp, _ROOT).replace("\\", "/")
                if is_ignored_path(rel):
                    continue
                if "example" in fn.lower():
                    continue
                out.append((rel, fp))
    return out


# ------------------------------------------------------------------ [1] 源码零硬编码凭据
section("[1] 纳入提交包的源码/文档：逐条用自检特征串扫，必须 0 命中")

targets = scan_targets()
check("扫描覆盖到的文件数够多（不是空跑）", len(targets) > 60, "共 %d 个文件" % len(targets))

hits = []
for rel, fp in targets:
    try:
        text = open(fp, encoding="utf-8", errors="ignore").read()
    except Exception:
        continue
    for pat in PS.SECRET_PATTERNS:
        m = pat.search(text)
        if m:
            hits.append((rel, pat.pattern))
check("SECRET_PATTERNS 全量扫描 0 命中", not hits, str(hits[:3]))

app_src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
check("app.py 里已无 _DEFAULT_PLACEHOLDER_KEY 这个符号", "_DEFAULT_PLACEHOLDER_KEY" not in app_src)
check("app.py 里改成了指纹常量", "_DEFAULT_KEY_FINGERPRINTS" in app_src)
check("指纹常量是 64 位摘要（不含原值）",
      all(len(x) == 64 and re.fullmatch(r"[0-9a-f]{64}", x)
          for x in A._DEFAULT_KEY_FINGERPRINTS))
check("64 位指纹不会被 32 位特征误报",
      not any(p.search(repr(sorted(A._DEFAULT_KEY_FINGERPRINTS))) for p in PS.SECRET_PATTERNS))

# ------------------------------------------------------------------ [2] 判定逻辑
section("[2] _is_default_llm_key：只留摘要也要认得默认 Key 的两种形态")

_old_fp = A._DEFAULT_KEY_FINGERPRINTS
A._DEFAULT_KEY_FINGERPRINTS = FAKE_DIGESTS
try:
    check("sk- 前缀形态判为默认 Key", A._is_default_llm_key(FAKEY_KEY) is True)
    check("裸 32 位形态判为默认 Key", A._is_default_llm_key(FAKE_HEX32) is True)
    check("前后带空白仍能判出（配置里常见）", A._is_default_llm_key("  " + FAKEY_KEY + " ") is True)
    check("大写 SK- 前缀仍能剥掉", A._is_default_llm_key("SK-" + FAKE_HEX32) is True)
    check("换成自己的 Key 判为 False", A._is_default_llm_key("sk-not-the-bundled-one-000000") is False)
    check("空串 False（不当成默认）", A._is_default_llm_key("") is False)
    check("None False", A._is_default_llm_key(None) is False)
    check("非字符串（数字）不炸且 False", A._is_default_llm_key(123456) is False)
finally:
    A._DEFAULT_KEY_FINGERPRINTS = _old_fp

check("真实指纹集合里查不到假 Key（替身没污染常量）",
      A._is_default_llm_key(FAKEY_KEY) is False)

# ------------------------------------------------------------------ [3] 配置校验三态
section("[3] validate_config：没 Key / 还在用默认 Key / 自己的 Key")


def llm_check():
    res = A.validate_config()
    entry = [c for c in res["checks"] if c["name"] == "llm_api_key"]
    return (entry[0] if entry else {}), res


_saved = dict(A.API_CONFIG)
A._DEFAULT_KEY_FINGERPRINTS = FAKE_DIGESTS
try:
    A.API_CONFIG["api_key"] = ""
    e, res = llm_check()
    check("无 Key：level=error", e.get("level") == "error", str(e.get("level")))
    check("无 Key：ok=False", e.get("ok") is False)
    check("无 Key：计入 missing_required", "llm_api_key" in res["missing_required"])
    check("无 Key：complete=False", res["complete"] is False)

    A.API_CONFIG["api_key"] = FAKEY_KEY
    e, res = llm_check()
    check("默认 Key：level=warn", e.get("level") == "warn", str(e.get("level")))
    check("默认 Key：功能仍算可用 ok=True", e.get("ok") is True)
    check("默认 Key：文案提示替换", "默认" in e.get("message", ""), e.get("message", ""))
    check("默认 Key：不进 missing_required", "llm_api_key" not in res["missing_required"])
    check("默认 Key：仍进 warnings（看得见）",
          any("默认" in w for w in res["warnings"]))

    A.API_CONFIG["api_key"] = "sk-my-own-key-1234567890abcdef"
    e, res = llm_check()
    check("自己的 Key：level=ok", e.get("level") == "ok", str(e.get("level")))
    check("自己的 Key：文案是已配置", e.get("message") == "已配置", e.get("message", ""))
    check("自己的 Key：warnings 里没有默认 Key 提醒",
          not any("默认" in w for w in res["warnings"]))

    # 结构不许因为改动而缺项
    check("返回结构四键齐全",
          all(k in res for k in ("complete", "missing_required", "checks", "warnings")))
    # 传个非法类型也不能抛（校验绝不能阻断启动）
    A.API_CONFIG["api_key"] = None
    try:
        llm_check()
        check("api_key=None 时校验不抛异常", True)
    except Exception as exc:  # noqa: BLE001
        check("api_key=None 时校验不抛异常", False, repr(exc))
finally:
    A._DEFAULT_KEY_FINGERPRINTS = _old_fp
    A.API_CONFIG.clear()
    A.API_CONFIG.update(_saved)

# ------------------------------------------------------------------ [4] 自检脚本本身
section("[4] prepare_submission：新增特征要抓得到裸 Key，且不误报")

bare = [p for p in PS.SECRET_PATTERNS if "32" in p.pattern]
check("自检特征串里确实加了 32 位这一条", len(bare) == 1, str([p.pattern for p in PS.SECRET_PATTERNS]))
pat = bare[0]
check("抓得到引号包裹的 32 位十六进制（本次泄漏的形态）", bool(pat.search('"' + FAKE_HEX32 + '"')))
check("单引号形态同样抓得到", bool(pat.search("'" + FAKE_HEX32 + "'")))
check("不误报 64 位 sha256 摘要", not bool(pat.search('"' + FAKE_64 + '"')))
check("不误报 31 位（长度要正好）", not bool(pat.search('"' + "a" * 31 + '"')))
check("不误报文档里的 sk-xxxx 占位符", not bool(PS.SECRET_PATTERNS[0].search("sk-xxxx")))
check("sk- 真 Key 仍被老特征抓到", bool(PS.SECRET_PATTERNS[0].search("sk-" + "b" * 24)))
check("ark- 形态仍被老特征抓到", bool(PS.SECRET_PATTERNS[1].search("ark-" + "c" * 24)))

_probe = tempfile.mkdtemp(prefix="st_secret_probe_")
try:
    with open(os.path.join(_probe, "leaky.py"), "w", encoding="utf-8") as f:
        f.write("KEY = '%s'\n" % FAKE_HEX32)
    with open(os.path.join(_probe, "clean.py"), "w", encoding="utf-8") as f:
        f.write("DIGEST = '%s'\n" % FAKE_64)
    with open(os.path.join(_probe, "config_example.json"), "w", encoding="utf-8") as f:
        f.write('{"api_key": "sk-please-replace-me"}\n')

    ph = PS.scan_secrets(_probe)
    check("临时目录里那条泄漏被扫到", any(x[0] == "leaky.py" for x in ph), str(ph))
    check("干净文件（只有摘要）不误报", not any(x[0] == "clean.py" for x in ph))
    check("示例模板按既有规则跳过", not any("example" in x[0] for x in ph))
    masked = [x[2] for x in ph if x[0] == "leaky.py"]
    check("命中报告已掩码，不含原值",
          masked and all(FAKE_HEX32 not in m for m in masked), str(masked))
    check("命中报告保留长度便于定位", bool(masked) and "chars)" in masked[0], str(masked))
finally:
    shutil.rmtree(_probe, ignore_errors=True)

# ------------------------------------------------------------------ [5] 别改坏结构
section("[5] 静态核对：调用点接上了，横幅里不再有明文比对")

check("validate_config 调用的是函数而不是字面量",
      "elif _is_default_llm_key(llm_key):" in app_src)
check("启动横幅同样走函数", "if _is_default_llm_key(API_CONFIG" in app_src)
check("app.py 中 _is_default_llm_key 定义与引用都在",
      app_src.count("def _is_default_llm_key(") == 1 and app_src.count("_is_default_llm_key(") >= 3)
check("比对不返回原值（函数只出布尔）",
      "return any(hashlib.sha256(" in app_src)
try:
    py_compile.compile(os.path.join(_HERE, "app.py"), doraise=True)
    py_compile.compile(os.path.join(_ROOT, "prepare_submission.py"), doraise=True)
    check("app.py 与 prepare_submission.py 编译通过", True)
except Exception as exc:  # noqa: BLE001
    check("app.py 与 prepare_submission.py 编译通过", False, repr(exc))

print("\n%s\n结果: %d passed / %d failed（共 %d）"
      % ("=" * 46, _PASS, _FAIL, _PASS + _FAIL))
sys.exit(1 if _FAIL else 0)
