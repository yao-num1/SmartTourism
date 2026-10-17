#!/usr/bin/env python
"""一键测试运行器：发现并执行 server/ 与 plugins/ 下所有 test_*.py，汇总通过/失败。

每个测试文件都是可独立运行的脚本（python test_x.py，退出码 0=全通过）。
本运行器逐个以子进程执行，捕获输出与退出码，最后给出总览；任一失败则以非零码退出。

用法:
    python run_tests.py            # 运行全部
    python run_tests.py -v         # 打印每个测试文件的完整输出
    python run_tests.py tools rag  # 只运行文件名包含这些关键字的测试
"""

import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
SEARCH_DIRS = ["server", "plugins"]
PER_TEST_TIMEOUT = 300

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_RESULT_RE = re.compile(r"(\d+)\s*(?:passed|通过)[^\n]*?(\d+)\s*(?:failed|失败)")


def discover(keywords):
    files = []
    for d in SEARCH_DIRS:
        base = os.path.join(ROOT, d)
        for dp, dns, fns in os.walk(base):
            if "__pycache__" in dp:
                continue
            for fn in sorted(fns):
                if fn.startswith("test_") and fn.endswith(".py"):
                    full = os.path.join(dp, fn)
                    if keywords and not any(k in fn.lower() for k in keywords):
                        continue
                    files.append(full)
    return sorted(files)


def run_one(path, verbose):
    rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
    start = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, path],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PER_TEST_TIMEOUT,
        )
        rc, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return rel, "TIMEOUT", 0, 0, PER_TEST_TIMEOUT, ""
    dur = time.time() - start
    combined = out + ("\n" + err if err.strip() else "")
    matches = _RESULT_RE.findall(combined)
    if matches:
        passed, failed = int(matches[-1][0]), int(matches[-1][1])
    else:
        passed = 0 if rc != 0 else 1
        failed = 1 if rc != 0 else 0
    status = "PASS" if rc == 0 else "FAIL"
    return rel, status, passed, failed, dur, (combined if verbose else "")


def main():
    args = sys.argv[1:]
    verbose = "-v" in args or "--verbose" in args
    keywords = [a.lower() for a in args if not a.startswith("-")]

    files = discover(keywords)
    if not files:
        print("未发现匹配的测试文件。")
        return 0

    print("=" * 64)
    print(f"SmartTourism 测试运行器 — 发现 {len(files)} 个测试文件")
    print("=" * 64)

    total_pass = total_fail = 0
    failures = []
    for path in files:
        rel, status, p, f, dur, dump = run_one(path, verbose)
        total_pass += p
        total_fail += f
        icon = "✅" if status == "PASS" else ("⏱️" if status == "TIMEOUT" else "❌")
        print(f"{icon} {status:<7} {rel:<44} {p:>3} passed / {f:>2} failed  ({dur:.1f}s)")
        if status != "PASS":
            failures.append((rel, status, dump))
        if verbose and dump:
            print("-" * 64)
            print(dump.strip())
            print("-" * 64)

    print("=" * 64)
    print(f"文件: {len(files)}  总计: {total_pass} passed / {total_fail} failed")
    if failures:
        print("失败的测试文件:")
        for rel, status, _ in failures:
            print(f"  - {rel} ({status})")
        print("=" * 64)
        return 1
    print("全部通过 🎉")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
