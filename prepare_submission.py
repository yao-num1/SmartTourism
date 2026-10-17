# -*- coding: utf-8 -*-
"""
一键生成「参赛提交版」清洁副本。

用途：
  1. 复制项目到目标目录（默认 ../<项目名>_提交版），剔除本地私有密钥、数据库、日志、
     生成缓存、备份与临时测试脚本，保证提交包不含敏感信息、体积可控、可直接复现。
  2. 复制完成后自动扫描目标目录，检测是否残留 API Key / Token 等敏感串并报告。

用法：
    python prepare_submission.py [目标目录]
    例：python prepare_submission.py ..\\SmartTourism_提交版

说明：
  - 真实密钥只保留在本机 data/llm_config.json / plugin_config.json / ark_config.json（已 gitignore）。
  - 提交包内仅保留 *.example.json 模板，评委按《一键部署教程》自行填入即可运行。
"""
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 目录/文件排除规则（相对路径片段或文件名）
EXCLUDE_DIRS = {
    '.git', '__pycache__', '.venv', 'venv', 'node_modules', '.workbuddy',
    'data/logs', 'data/chroma_db', 'dashboard/poster',
}
EXCLUDE_DIR_PREFIXES = ('_backup_',)
EXCLUDE_FILES = {
    'llm_config.json', 'plugin_config.json', 'ark_config.json',  # 真实密钥，仅本地
    'smarttourism.db', 'cards.json', 'inspire_pool.json', 'inspire_cards_live.json',
}
EXCLUDE_FILE_PREFIXES = ('_',)          # _*.py / _*.js / _*.txt 等临时测试/备份
EXCLUDE_EXT = {'.pyc', '.log', '.db', '.db-wal', '.db-shm'}

# 敏感串特征（用于复制后自检；只放通用特征，不放具体密钥值以免自检脚本自匹配）
SECRET_PATTERNS = [
    re.compile(r'sk-[A-Za-z0-9]{20,}'),          # OpenAI/DeepSeek 风格
    re.compile(r'ark-[A-Za-z0-9\-]{20,}'),       # 火山方舟
    re.compile(r'Bearer\s+sk-[A-Za-z0-9]{16,}'), # 请求头里带 key
    # 引号裹住的裸 32 位十六进制——历史上默认 Key 就是这个形状，前四条特征串抓不到。
    # 只认带引号的定长 32 位：sha256 是指纹不是凭据，故意不匹配 64 位。
    re.compile(r"""['\"][0-9a-f]{32}['\"]"""),
]


def _rel(path):
    return os.path.relpath(path, HERE).replace(os.sep, '/')


def _has_underscore_component(rel):
    # 任意一级目录/文件以 _ 开头（_backup、_edgeprof7、__pycache__、_xxx.py 等）都视为本地产物
    return any(part.startswith('_') for part in rel.split('/') if part)


def _skip_dir(rel):
    if rel in EXCLUDE_DIRS:
        return True
    if _has_underscore_component(rel):
        return True
    for pre in EXCLUDE_DIR_PREFIXES:
        if rel.split('/')[0].startswith(pre) or rel.startswith(pre):
            return True
    return False


def _skip_file(rel, name):
    if name in EXCLUDE_FILES:
        return True
    if _has_underscore_component(rel):
        return True
    if name.startswith(EXCLUDE_FILE_PREFIXES):
        return True
    if os.path.splitext(name)[1].lower() in EXCLUDE_EXT:
        return True
    for d in EXCLUDE_DIRS:
        if rel.startswith(d + '/'):
            return True
    for pre in EXCLUDE_DIR_PREFIXES:
        if rel.startswith(pre):
            return True
    return False


def copy_clean(dst):
    dst = os.path.abspath(dst)
    if os.path.exists(dst):
        print('[!] 目标已存在，先删除：', dst)
        shutil.rmtree(dst)
    copied = 0
    for root, dirs, files in os.walk(HERE):
        rel_root = _rel(root)
        dirs[:] = [d for d in dirs if not _skip_dir(_rel(os.path.join(root, d)))]
        if _skip_dir(rel_root) and rel_root != '.':
            continue
        for name in files:
            rel = _rel(os.path.join(root, name))
            if rel == os.path.basename(__file__):
                pass  # 本脚本本身要带上
            if _skip_file(rel, name):
                continue
            src = os.path.join(root, name)
            target = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(src, target)
            copied += 1
    # 保证 data 目录与示例模板存在
    os.makedirs(os.path.join(dst, 'data'), exist_ok=True)
    print('[OK] 已复制 %d 个文件到 %s' % (copied, dst))
    return dst


def scan_secrets(dst):
    hits = []
    for root, dirs, files in os.walk(dst):
        dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', 'node_modules')]
        for name in files:
            # 示例模板里的占位符（如 sk-xxxx）不算敏感信息
            if 'example' in name.lower():
                continue
            p = os.path.join(root, name)
            if os.path.splitext(name)[1].lower() in ('.png', '.jpg', '.jpeg', '.gif', '.ico', '.db'):
                continue
            try:
                with open(p, 'r', encoding='utf-8', errors='ignore') as f:
                    text = f.read()
            except Exception:
                continue
            for pat in SECRET_PATTERNS:
                m = pat.search(text)
                if m:
                    # 命中值本身是凭据，不能原样打进报告里，只留长度与掩码
                    v = m.group(0)
                    hits.append((os.path.relpath(p, dst), pat.pattern,
                                 '%s(%d chars)' % ('*' * min(6, len(v)), len(v))))
    return hits


def main():
    dst = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, '..', os.path.basename(HERE) + '_提交版')
    dst = copy_clean(dst)
    hits = scan_secrets(dst)
    if hits:
        print('\n[!!] 检测到疑似敏感信息残留，请处理后再提交：')
        for rel, pat, sample in hits:
            print('   - %s  匹配 %s  样例 %s' % (rel, pat, sample))
        sys.exit(2)
    print('\n[OK] 敏感信息自检通过：提交包内未检测到 API Key / Token 残留。')
    print('[OK] 请配合《参赛完善包/一键部署教程.md》与《工程交付说明.md》一并提交。')


if __name__ == '__main__':
    main()
