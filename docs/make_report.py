"""一条命令把作品报告 HTML 打成 PDF（本机 Edge 无头打印，不联网、不装依赖）。

用法:
    python docs/make_report.py            # 生成 docs/智旅灵犀_作品报告.pdf
    python docs/make_report.py --out X.pdf

为什么用 Edge 而不是 python 库排版：版式（分页、表格、SVG 图）已经在 HTML/CSS 里定好，
浏览器打印是唯一能一比一还原的路径；改数字只需重跑本命令。
"""

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, "智旅灵犀_作品报告.html")
DEFAULT_PDF = os.path.join(HERE, "智旅灵犀_作品报告.pdf")

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def find_edge():
    for p in EDGE_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", default=HTML)
    ap.add_argument("--out", default=DEFAULT_PDF)
    args = ap.parse_args()

    edge = find_edge()
    if not edge:
        print("[make_report] 未找到 Microsoft Edge。请手动用浏览器打开 HTML 后 Ctrl+P，")
        print("              选「另存为 PDF」、纸张 A4、边距「无」、勾掉页眉页脚。")
        return 1
    if not os.path.exists(args.html):
        print(f"[make_report] 源稿不存在: {args.html}")
        return 1

    out = os.path.abspath(args.out)
    if os.path.exists(out):
        os.remove(out)

    # 独立 user-data-dir：避免复用已运行的 Edge 实例导致命令直接返回却不产出文件
    profile = os.path.join(os.environ.get("TEMP", "."), "edge_report_profile")
    cmd = [
        edge,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        f"--user-data-dir={profile}",
        "--no-pdf-header-footer",
        "--virtual-time-budget=8000",
        f"--print-to-pdf={out}",
        "file:///" + args.html.replace("\\", "/"),
    ]
    print("[make_report] Edge:", edge)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"[make_report] 打印失败 rc={proc.returncode}")
        print((proc.stderr or proc.stdout or "")[:1200])
        return 1

    deadline = time.time() + 30
    while not os.path.exists(out) and time.time() < deadline:
        time.sleep(0.4)
    if not os.path.exists(out):
        print("[make_report] 命令返回 0 但没写出 PDF，请检查是否有 Edge 进程残留。")
        return 1

    kb = os.path.getsize(out) / 1024
    pages = "?"
    try:
        from pypdf import PdfReader
        pages = str(len(PdfReader(out).pages))
    except Exception:
        pass
    print(f"[make_report] 已生成 {out}  ({kb:.0f} KB, {pages} 页)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
