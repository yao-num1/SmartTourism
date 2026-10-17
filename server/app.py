"""
智旅灵犀 · SmartTourism — 文旅全场景智能辅助系统后端服务
=========================================================
版本: v2.2.0
基于: Flask + 国产大模型 API (DeepSeek/智谱/通义/Kimi)
架构: 插件化数据中间层 + Skill 协同 + SQLite 持久化

核心能力:
  1. AI 智能对话 — 多模型热切换、流式输出、多轮对话上下文管理
  2. 行程智能规划 — 多日行程生成、保存/修改/导出
  3. 营销内容生成 — 多平台文案/短视频脚本/活动策划/文创概念
  4. 客群精准匹配 — 客群画像库 + 智能匹配引擎 + 转化策略推荐

插件系统:
  - weather:     天气查询 (wttr.in/高德/心知)
  - travel_data: 本地知识库 + 飞猪搜索 + 高德POI
  - amap_map:    高德地图 (定位/搜索/路线)

数据存储:
  - 会话与消息: SQLite messages 表
  - 用户数据:   SQLite user_store 表
  - 插件配置:   data/plugin_config.json

作者: 智旅灵犀团队
许可证: 仅供教学与竞赛使用
"""

import json
import logging
import os
import sys
import re
import uuid
import random
import time
import threading
import traceback
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, send_file, make_response, Response, stream_with_context
from flask_cors import CORS
from logging.handlers import RotatingFileHandler
import requests
import hashlib
import secrets
from werkzeug.exceptions import HTTPException

# 确保当前目录在 sys.path 中（直接运行 app.py 时需要）
_current_dir = os.path.dirname(os.path.abspath(__file__))
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)

import database as db
import social_data
from anti_hallucination import (
    ANTI_HALLUCINATION_PROMPT,
    CONFLICT_HANDLING_PROMPT,
    detect_user_conflict,
    build_conflict_aware_prefix,
    validate_response,
    validation_stats,
    get_fact_tracker,
    get_temperature_for_skill,
    get_rag_context,
    retrieve_knowledge,
)
from context_manager import (
    manage_context,
    append_history,
    build_facts_block,
    get_summary,
    clear_summary,
    summary_cache_size,
)


# ========== 长期记忆（跨会话沉淀用户偏好/约束） ==========
# 懒加载 agent.memory：导入或构建失败即永久降级为「无长期记忆」，绝不打断对话。
MEMORY_ENABLED = os.getenv('SMARTTOURISM_MEMORY', '1') != '0'
_MEMORY_MOD = None
_MEMORY_IMPORT_FAILED = False


def _get_memory():
    global _MEMORY_MOD, _MEMORY_IMPORT_FAILED
    if not MEMORY_ENABLED or _MEMORY_IMPORT_FAILED:
        return None
    if _MEMORY_MOD is not None:
        return _MEMORY_MOD
    try:
        from agent import memory as _mem
        _MEMORY_MOD = _mem
    except Exception as e:  # noqa: BLE001
        _MEMORY_IMPORT_FAILED = True
        _MEMORY_MOD = None
        app.logger.warning("长期记忆模块不可用，本轮跳过记忆: %s", e)
    return _MEMORY_MOD


def _memory_block(user_id, query):
    """召回与本轮相关的长期记忆，生成注入块（无相关记忆/模块不可用 → 空串）。"""
    mem = _get_memory()
    if not mem or not user_id:
        return ''
    try:
        return mem.build_memory_block(user_id, query) or ''
    except Exception as e:  # noqa: BLE001
        app.logger.warning("长期记忆召回失败，跳过注入: %s", e)
        return ''


def _memory_remember(user_id, user_text, session_id):
    """答复已给出后再沉淀记忆（后台线程 + LLM 抽取，不占用响应时间）。"""
    mem = _get_memory()
    if not mem or not user_id:
        return
    try:
        mem.remember_async(user_id, user_text, session_id, call_llm)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("长期记忆写入启动失败: %s", e)


def _memory_stats(user_id='default'):
    mem = _get_memory()
    if not mem:
        return {'enabled': False, 'total': 0, 'by_slot': {}, 'embedder': 'none',
                'persisted': False, 'user_id': user_id}
    st = dict(mem.stats(user_id))
    st['enabled'] = True
    return st
# ====== 长期记忆接入点（HTTP 路由见「会话与记忆 API」一节） ======


def clean_llm_output(text: str) -> str:
    """
    后处理 LLM 回复，清理违规的排版符号。

    - 移除分隔线行（---、===、***、~~~、___、||| 等）
    - 将 Markdown 表格行转换为可读的文本格式
    - 清理多余空行
    """
    if not text:
        return text

    # 1. 移除独立的分隔线行（3个及以上连续的 -=*_~| 字符）
    text = re.sub(r'^[\s]*[-=*_~|]{3,}[\s]*$', '', text, flags=re.MULTILINE)

    # 2. 将 Markdown 表格分隔行（|---|---|）移除
    text = re.sub(r'^\|[\s:|-]+\|$', '', text, flags=re.MULTILINE)

    # 3. 将剩余的 Markdown 表格行（| 内容 | 内容 |）转为可读文本
    def _table_to_text(m):
        line = m.group(0)
        cells = [c.strip() for c in line.strip('|').split('|')]
        return '  ·  '.join(cells)

    text = re.sub(r'^\|.+\|$', _table_to_text, text, flags=re.MULTILINE)

    # 4. 清理多余空行（3个以上连续空行变为2个）
    text = re.sub(r'\n{4,}', '\n\n\n', text)

    return text.strip()

# 添加项目根目录到 Python Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = Flask(__name__, static_folder='../dashboard', static_url_path='/static')

# ========== 安全与运行环境配置 ==========
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, 'data', 'logs')
# 文件日志是「增强」不是「前提」：目录建不出来（只读盘/被占用/磁盘满）时降级为只走控制台，
# 服务照样起来——否则一个日志问题会让整个后端在 import 阶段就死掉，连 /api/health 都没有。
LOG_FILE_OK = True
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except OSError as _log_dir_err:
    LOG_FILE_OK = False
    print(f'[startup] 日志目录不可用，降级为只输出到控制台: {_log_dir_err}')

# 行程规划预处理：参数校验 + 结构化骨架生成
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'skills', 'tourism-advisor', 'scripts'))
try:
    from plan_generator import (
        validate_plan_params,
        build_plan_skeleton,
        build_plan_fallback,
    )
except Exception as _pg_err:
    print('[plan_generator] 导入失败，将使用纯 LLM 规划:', _pg_err)

    def validate_plan_params(data):
        return {"valid": True, "missing": [], "clarification": ""}

    def build_plan_skeleton(data):
        return {}

    def build_plan_fallback(reason, data):
        return {"fallback": True, "reason": reason}

APP_ENV = os.getenv('SMARTTOURISM_ENV', 'development').lower()
DEBUG_ENABLED = os.getenv('SMARTTOURISM_DEBUG', '0').lower() in ('1', 'true', 'yes', 'on')
MAX_CONTENT_LENGTH = int(os.getenv('SMARTTOURISM_MAX_CONTENT_LENGTH', str(2 * 1024 * 1024)))
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH
app.config['JSON_AS_ASCII'] = False

_allowed_origins = os.getenv(
    'SMARTTOURISM_CORS_ORIGINS',
    'http://localhost:5000,http://127.0.0.1:5000'
)
CORS(
    app,
    resources={r"/api/*": {"origins": [o.strip() for o in _allowed_origins.split(',') if o.strip()]}},
    supports_credentials=False,
    max_age=600
)

def _add_file_handler(filename: str, level: int = None):
    """挂一个滚动文件 handler；日志目录不可写时返回 None，调用方按「只有控制台」继续跑。"""
    global LOG_FILE_OK
    if not LOG_FILE_OK:
        return None
    path = os.path.join(LOG_DIR, filename)
    try:
        handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding='utf-8')
    except OSError as exc:
        LOG_FILE_OK = False
        print(f'[startup] 文件日志不可用，降级为只输出到控制台: {path} ({exc})')
        return None
    if level is not None:
        handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s [%(name)s] %(message)s'
    ))
    app.logger.addHandler(handler)
    return handler


_log_handler = _add_file_handler('app.log')
app.logger.setLevel(os.getenv('SMARTTOURISM_LOG_LEVEL', 'INFO').upper())

# ===== 运维日志：独立错误日志（WARNING+ 单独成文件，排查只看这一份干净日志）=====
_err_handler = _add_file_handler('error.log', level=logging.WARNING)

# 只有 app.logger 挂了 handler，模块内 logger.*（记忆抽取失败、摘要降级、DAO 降级）否则无处可查
for _mod_logger in ("agent.memory", "agent.semantic_cache", "anti_hallucination", "database"):
    _lg = logging.getLogger(_mod_logger)
    _lg.setLevel(logging.INFO)
    for _h in (_log_handler, _err_handler):
        if _h is not None:
            _lg.addHandler(_h)
    _lg.propagate = False

# ===== 运维指标收集器（内存态，供 /api/admin/ops 与 /admin 面板使用）=====
from ops_log import OpsStats, is_static_noise, render_admin_html  # noqa: E402
OPS = OpsStats()
# 后台线程：周期性把指标快照落盘到 data/logs/ops-YYYYMMDD.jsonl，并在近期错误率超阈值时告警
OPS.start_background(
    LOG_DIR,
    app.logger,
    interval_s=float(os.getenv('SMARTTOURISM_OPS_INTERVAL', '300')),
    alert_kwargs={
        'error_rate_threshold': float(os.getenv('SMARTTOURISM_OPS_ALERT_RATE', '0.05')),
        'min_requests': int(os.getenv('SMARTTOURISM_OPS_ALERT_MIN', '20')),
        'cooldown_s': float(os.getenv('SMARTTOURISM_OPS_ALERT_COOLDOWN', '300')),
    },
)


def _request_id() -> str:
    """获取本次请求 ID，便于前端报错与后端日志关联。"""
    return getattr(request, 'request_id', '')


def _json_error(message: str, status_code: int = 400, code: str = 'bad_request'):
    """统一 JSON 错误响应，避免把异常堆栈或敏感信息暴露给前端。"""
    response = jsonify({
        "success": False,
        "error": message,
        "code": code,
        "request_id": _request_id()
    })
    response.status_code = status_code
    return response


def get_json_payload(default=None):
    """安全读取 JSON 请求体；非法 JSON 统一抛出 400。"""
    if default is None:
        default = {}
    if not request.data:
        return default
    payload = request.get_json(silent=True)
    if payload is None:
        raise ValueError("请求体必须是合法 JSON")
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return payload


def clamp_int(value, default: int, min_value: int, max_value: int) -> int:
    """安全解析整数查询参数，避免 ValueError 触发 500。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(parsed, max_value))


@app.before_request
def attach_request_context():
    """请求入口防护：请求 ID、JSON 类型校验和基础审计日志。"""
    request.request_id = request.headers.get('X-Request-ID') or str(uuid.uuid4())
    request._ops_start = time.time()   # 运维：用于 after_request 计算耗时
    if request.path.startswith('/api/') and request.method in ('POST', 'PUT', 'PATCH'):
        if request.content_length and request.content_length > MAX_CONTENT_LENGTH:
            return _json_error("请求体过大", 413, "payload_too_large")
        if request.content_length and not request.is_json and request.path != '/api/voice/recognize':
            return _json_error("请使用 application/json 请求体", 415, "unsupported_media_type")
    # 静态资源（图片/css/js）不写访问日志，避免淹没真正有用的记录
    if is_static_noise(request.path):
        OPS.skip_static()
    else:
        app.logger.info(
            "request_id=%s method=%s path=%s remote=%s",
            request.request_id,
            request.method,
            request.path,
            request.headers.get('X-Forwarded-For', request.remote_addr)
        )


@app.after_request
def apply_security_headers(response):
    """统一安全响应头，降低常见浏览器侧风险。"""
    response.headers['X-Request-ID'] = _request_id()
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'geolocation=(self), camera=(), microphone=(self)')
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: https://webapi.amap.com https://*.amap.com https://*.autonavi.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://*.amap.com https://*.autonavi.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob: https: http://webapi.amap.com http://*.amap.com http://*.autonavi.com; "
        "worker-src 'self' blob: data:; "
        "child-src 'self' blob: data:; "
        "connect-src 'self' http://localhost:* http://127.0.0.1:* https://restapi.amap.com https://*.amap.com https://*.autonavi.com http://webapi.amap.com http://*.amap.com http://*.autonavi.com https://api.map.baidu.com https://ipapi.co https://api.ipify.org; "
        "frame-ancestors 'self';"
    )
    if request.path.startswith('/api/'):
        response.headers.setdefault('Cache-Control', 'no-store')
    # ===== 运维指标：记录状态码+耗时（静态资源不计入），并写一条完成日志 =====
    try:
        if not is_static_noise(request.path):
            _start = getattr(request, '_ops_start', None)
            _dur = (time.time() - _start) * 1000.0 if _start else 0.0
            OPS.record(request.method, request.path, response.status_code, _dur)
            app.logger.info(
                "request_id=%s done status=%s %.0fms %s %s",
                _request_id(), response.status_code, _dur, request.method, request.path
            )
    except Exception:
        pass  # 运维统计绝不影响正常响应
    return response


@app.errorhandler(ValueError)
def handle_value_error(error):
    app.logger.warning("request_id=%s bad_request=%s", _request_id(), error)
    return _json_error(str(error) or "请求参数不合法", 400, "invalid_request")


@app.errorhandler(HTTPException)
def handle_http_exception(error):
    if request.path.startswith('/api/'):
        message = error.description if error.code and error.code < 500 else "服务暂时不可用"
        return _json_error(message, error.code or 500, error.name.lower().replace(' ', '_'))
    return error


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    app.logger.error(
        "request_id=%s unhandled_error=%s\n%s",
        _request_id(),
        error,
        traceback.format_exc()
    )
    try:
        OPS.record_error(request.path, error, _request_id(), 500, type(error).__name__)
    except Exception:
        pass
    if request.path.startswith('/api/'):
        return _json_error("服务器内部错误，请稍后重试", 500, "internal_server_error")
    return "服务器内部错误，请稍后重试", 500

# ========== 插件系统 ==========
from plugins import registry
from plugins.amap_weather import AmapWeatherPlugin
from plugins.travel_data import LocalKnowledgePlugin, FliggySearchPlugin, AmapPOIPlugin, AmapMapPlugin
from plugins.meituan import MeituanTravelPlugin
from plugins.travel_cost import TravelCostPlugin

PLUGIN_CONFIG_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', 'plugin_config.json')

def load_plugin_config():
    """加载插件配置"""
    if os.path.exists(PLUGIN_CONFIG_FILE):
        with open(PLUGIN_CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_plugin_config(config: dict):
    os.makedirs(os.path.dirname(PLUGIN_CONFIG_FILE), exist_ok=True)
    with open(PLUGIN_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

# 注册所有插件
_plugin_cfg = load_plugin_config()
_amap_key = _plugin_cfg.get('amap_key', '')
registry.register(AmapWeatherPlugin(_plugin_cfg))
registry.register(LocalKnowledgePlugin())
registry.register(FliggySearchPlugin())
registry.register(MeituanTravelPlugin({"meituan_token": _plugin_cfg.get('meituan_token', '')}))
registry.register(AmapPOIPlugin({"amap_key": _amap_key}))
registry.register(AmapMapPlugin({"amap_key": _amap_key}))
registry.register(TravelCostPlugin({"amap_key": _amap_key}))

# 美团插件采集到的可预订入口（酒店预订/门票购买），get_plugin_data 执行期间累积，
# 由 chat() 读取后随响应透传给前端渲染"立即预订/立即购票"按钮。
# 必须按请求隔离：waitress 是多线程共用一个进程，挂在模块变量上会让后取走的请求
# 拿到（或清掉）别人的预订链接。
_module_bookings: list = []
_MODULE_BOOKINGS_CAP = 50


def _request_g():
    """当前请求的 flask.g；不在请求上下文里（脚本、后台线程）返回 None。"""
    try:
        from flask import g, has_request_context
        return g if has_request_context() else None
    except Exception:
        return None


def _reset_plugin_bookings() -> None:
    """开始一次插件采集前清空预订入口（只清当前请求的）。"""
    g = _request_g()
    if g is not None:
        g.plugin_bookings = []
        return
    del _module_bookings[:]


def _collect_plugin_bookings(items) -> None:
    """把本次插件采到的预订入口挂到当前请求；无请求上下文时回落模块变量。"""
    if not items:
        return
    g = _request_g()
    if g is not None:
        g.plugin_bookings = list(getattr(g, "plugin_bookings", None) or []) + list(items)
        return
    _module_bookings.extend(items)
    del _module_bookings[:-_MODULE_BOOKINGS_CAP]


def _take_plugin_bookings() -> list:
    """取出并清空本次插件执行采集到的预订入口。"""
    g = _request_g()
    if g is not None:
        items = list(getattr(g, "plugin_bookings", None) or [])
        g.plugin_bookings = []
    else:
        items = list(_module_bookings)
        del _module_bookings[:]
    # 去重
    seen, out = set(), []
    for b in items:
        u = b.get("url") if isinstance(b, dict) else None
        if u and u not in seen:
            seen.add(u)
            out.append(b)
    return out


# 景点/酒店意图与价格词的识别（用于保证美团查询明确索要票价/房价）
_MT_TICKET_INTENT = ["门票", "景区", "景点", "风景", "好玩", "去哪儿", "打卡", "游玩", "观光"]
_MT_HOTEL_INTENT = ["酒店", "住宿", "民宿", "客栈", "宾馆", "住"]
_MT_PRICE_WORDS = ["价格", "多少钱", "票价", "价位", "费用", "人均", "一晚", "起", "预算", "收费", "贵不贵"]


def _meituan_price_query(user_query: str, destination: str = None) -> str:
    """按意图补全美团查询：问景点不带价格词 → 追加索要门票价格；问酒店不带价格词 → 追加索要每晚房价。

    保证"有景点推荐/酒店推荐"时美团侧一定返回实时票价/房价，而不是只给榜单。
    """
    q = (user_query or "").strip()
    has_price = any(w in q for w in _MT_PRICE_WORDS)
    want_ticket = any(w in q for w in _MT_TICKET_INTENT)
    want_hotel = any(w in q for w in _MT_HOTEL_INTENT)
    city = (destination or "").strip()
    # 纯行程规划问句（如"杭州三天，带父母"）："预算中等"是约束不是询价，美团侧不会主动附实时价，
    # 保留用户原话上下文并追加索要票价/房价
    want_plan = bool(city) and not (want_ticket or want_hotel) and (
        bool(re.search(r'[0-9一二三四五六七八九十两半]+\s*天', q)) or any(w in q for w in ['行程', '规划', '安排', '日游']))
    if want_plan:
        return f"{q}；请推荐景点并逐一给出门票价格（成人票），并推荐酒店及每晚住宿价格"
    if has_price or not q:
        return q
    if want_ticket and want_hotel:
        return f"{city or '当地'}景点推荐及各景点门票价格（成人票），以及酒店推荐及每晚住宿价格"
    if want_ticket:
        return f"推荐{city or '当地'}必去景点，并逐一给出各景点门票价格（成人票/网络价）"
    if want_hotel:
        return f"推荐{city or '当地'}值得入住的酒店，并逐一给出每晚住宿价格（大床房/标间参考价）"
    return q


def _extract_route_places(user_query: str):
    """从自然语言中提取交通费查询的起终点，如「从西湖到灵隐寺打车多少钱」→ ('西湖','灵隐寺')。"""
    q = user_query.strip()
    m = re.search(r'(?:从)?(.+?)(?:到|去|至|打车去|坐车去)(.+?)(?:打车|公交|地铁|多少钱|费用|车费|路费|要多久|怎么走|$)', q)
    if not m:
        return None, None
    origin = m.group(1).strip(' ，。?？!！')
    dest = m.group(2).strip(' ，。?？!！')
    # 去除尾部残留的疑问词
    for w in ('打车', '公交', '地铁', '多少钱', '费用', '车费', '路费', '贵不贵', '怎么走', '要多久'):
        dest = re.sub(w + r'$', '', dest)
    origin = re.sub(r'(打车|公交|地铁|多少钱|费用|车费|路费)$', '', origin)
    if not origin or not dest:
        return None, None
    return origin, dest


def _extract_spot_name(user_query: str, city: str = ""):
    """从「XX门票多少钱」中提取景点名；提取不到返回 None（让插件按城市查全部）。"""
    q = user_query.strip()
    m = re.search(r'(.+?)(?:的门票|门票|票价|多少钱|门票价格)', q)
    if not m:
        return None
    name = m.group(1).strip(' ，。?？!！')
    # 去掉问句前缀
    name = re.sub(r'^(请问|想问|想知道|我想知道|帮我查|查一下|告诉我|那个|这个|去)', '', name)
    # 含路线词说明是「从A到B」问句，不属于景点票价
    if re.search(r'(从|到|去|打车|公交|地铁)', name):
        return None
    if not name or name in (city or ""):
        return None
    return name


def get_plugin_data(user_query: str, destination: str = None) -> str:
    """
    自动触发插件获取实时数据并返回格式化文本
    
    根据用户查询内容自动触发匹配的插件（天气、本地知识库、高德地图等），
    获取实时数据并拼接为上下文文本，供 LLM 对话使用。
    
    Args:
        user_query: 用户的原始输入文本
        destination: 目标城市/目的地，若为 None 则从 user_query 中提取
        
    Returns:
        格式化后的插件数据文本，多条数据以 '\n\n---\n\n' 分隔；
        若无插件被触发或数据为空则返回空字符串
    """
    # Step 0: 检测用户是否想知道自己的位置
    _reset_plugin_bookings()  # 每次新的插件采集前清空本请求的预订入口
    location_keywords = ['我在哪', '我的位置', '我在哪里', '我当前', '当前位置', '附近有什么', '周边有什么', '离我多远', '我附近', '我这', '我这附近']
    ask_location = any(kw in user_query for kw in location_keywords)

    # Step 0.5: 检测 GPS 坐标
    import re
    gps_match = re.search(r'[（(]\s*(\d+\.?\d*)\s*[,，]\s*(\d+\.?\d*)\s*[）)]', user_query)
    gps_lng = float(gps_match.group(1)) if gps_match else None
    gps_lat = float(gps_match.group(2)) if gps_match else None
    if gps_lng and gps_lat:
        ask_location = True  # 有精确坐标肯定是在问位置

    # Step 1: 如果没有指定 destination，从用户输入中提取城市
    if not destination:
        cities = ['杭州', '三亚', '成都', '北京', '大理', '丽江', '上海', '广州', '深圳',
                   '厦门', '桂林', '西安', '南京', '重庆', '青岛', '武汉', '长沙', '昆明',
                   '张家界', '黄山', '九寨沟', '拉萨', '哈尔滨', '苏州', '乌镇', '温州',
                   '宁波', '郑州', '合肥', '南昌', '福州', '南宁', '贵阳', '海口',
                   '济南', '太原', '沈阳', '长春', '天津', '石家庄', '呼和浩特']
        for city in cities:
            if city in user_query:
                destination = city
                break

    # Step 2: 如果用户提到了具体城市，优先使用；否则如果用户在问自己位置/周边，用 IP 定位
    amap_map = registry.get("amap_map")
    user_mentioned_city = bool(destination)  # Step 1 已提取到城市

    if not user_mentioned_city and ask_location and amap_map and amap_map.is_configured():
        try:
            ip_result = amap_map.execute({"action": "ip_location"})
            if ip_result.get("status") == "ok":
                city = ip_result.get("city", "")
                if city:
                    destination = city
        except Exception as e:
            pass

    triggered = registry.auto_trigger(user_query)
    plugin_data_parts = []

    # 出行费用查询：票价 / 交通费 专门插件，命中后跳过高德POI/本地知识库避免重复
    cost_handled = False

    # Step 2.5: 美团酒旅优先（商业数据更丰富：美食/酒店/景点/行程/票务）
    meituan = registry.get("meituan")
    meituan_handled = False
    # 已确认目的地的规划/推荐/费用类提问，即使用户措辞没命中触发词也强制走美团，
    # 否则"杭州三天，带父母，预算中等"这类问句全程拿不到实时票价/房价（2026-09-24 实测：美团一次都未被调用）
    _q0 = user_query.lower()
    meituan_worth = bool(destination) and (
        any(k in _q0 for k in ['推荐', '景点', '景区', '好玩', '攻略', '行程', '酒店', '住宿',
                               '民宿', '美食', '餐厅', '门票', '票价', '多少钱', '预算', '费用',
                               '规划', '安排', '日游'])
        or bool(re.search(r'[0-9一二三四五六七八九十两半]+\s*天', _q0))
    )
    if meituan and meituan.is_configured() and (
            any(p.name == 'meituan' for p, _c in triggered) or meituan_worth):
        try:
            mt_query = _meituan_price_query(user_query, destination)
            mt_params = {"query": mt_query, "origin_query": user_query}
            if destination:
                mt_params["city"] = destination
            mt_result = meituan.execute(mt_params)
            if mt_result.get("status") == "ok":
                formatted = meituan.format_for_llm(mt_result)
                if formatted:
                    plugin_data_parts.append(formatted)
                    meituan_handled = True
                # 缓存本次美团原文：对话结尾的推荐卡片可直接复用，无需重复调用美团
                try:
                    _seed_city = destination or str(mt_result.get("city") or "")
                    if _seed_city:
                        _reco_seed_cache[_seed_city] = {
                            "ts": time.time(),
                            "answer": str(mt_result.get("answer") or ""),
                        }
                except Exception:
                    pass
                # 采集可预订入口（酒店预订/门票购买），随响应透传给前端
                _collect_plugin_bookings(mt_result.get("bookings") or [])
            else:
                print(f"[Plugin] meituan: {mt_result.get('message', '')}")
        except Exception as e:
            print(f"[Plugin] meituan err: {e}")

    # Step 3: 优先用高德实时搜索（如果有 Key），否则用本地知识库
    has_amap = amap_map and amap_map.is_configured() if amap_map else False

    for plugin, confidence in triggered:
        try:
            params = {"destination": destination} if destination else {}
            if destination:
                params["city"] = destination

            # 美团已在 Step 2.5 统一处理，跳过避免重复调用
            if plugin.name == "meituan":
                continue

            # 出行费用查询：票价 / 交通费（打车·公交·地铁·步行）
            if plugin.name == "travel_cost":
                # 美团已提供实时票价/房价时跳过：两者数据源不同，本地费用库查不到外地城市，
                # 会注入"未查到XX门票价格"，与美团实时数据冲突并误导模型（2026-09-19 实测踩坑）
                if meituan_handled:
                    continue
                q = user_query.lower()
                is_cost_query = any(k in q for k in
                    ['票价', '门票多少钱', '门票价格', '多少钱', '费用', '花费', '开销',
                     '打车', '公交', '地铁', '车费', '路费', '预算', '贵不贵'])
                # 目的地确认后，推荐景点/酒店时也走目的地门票费用查询，保证推荐附带目的地真实票价
                is_reco_query = bool(destination) and any(k in q for k in
                    ['推荐', '景点', '景区', '好玩', '攻略', '行程', '酒店', '住宿'])
                if not is_cost_query and not is_reco_query:
                    continue
                try:
                    if any(k in q for k in ['打车', '公交', '地铁', '车费', '路费', '怎么去']):
                        origin, dest = _extract_route_places(user_query)
                        if origin and dest:
                            rp = {"action": "route", "origin": origin, "destination": dest, "mode": "driving"}
                            if destination:
                                rp["city"] = destination
                            result = plugin.execute(rp)
                            formatted = plugin.format_for_llm(result)
                            if formatted:
                                plugin_data_parts.append(formatted)
                                cost_handled = True
                    else:
                        spot = _extract_spot_name(user_query, destination)
                        tp = {"action": "ticket"}
                        if destination:
                            tp["city"] = destination
                        if spot:
                            tp["spot"] = spot
                        result = plugin.execute(tp)
                        formatted = plugin.format_for_llm(result)
                        if formatted:
                            plugin_data_parts.append(formatted)
                            cost_handled = True
                except Exception as e:
                    print(f"[Plugin] travel_cost err: {e}")
                continue

            # 高德POI：amap_map 能接管时不重复执行，避免默认关键词返回无关数据
            if plugin.name == "amap_poi":
                if meituan_handled or cost_handled:
                    continue
                will_amap_map_handle = bool(destination) and (
                    user_mentioned_city or ask_location or
                    any(kw in user_query.lower() for kw in ['酒店', '住宿', '景点', '好玩', '美食', '推荐']))
                if has_amap and will_amap_map_handle:
                    continue
                query_lower = user_query.lower()
                if any(kw in query_lower for kw in ['酒店', '住宿', '住']): params['keyword'] = '酒店'
                elif any(kw in query_lower for kw in ['景点', '景区', '好玩']): params['keyword'] = '景点'
                elif any(kw in query_lower for kw in ['美食', '好吃', '餐厅', '吃']): params['keyword'] = '餐饮'
                elif any(kw in query_lower for kw in ['购物', '逛街', '买']): params['keyword'] = '购物'
                if destination:
                    params['city'] = destination
                result = plugin.execute(params)
                formatted = plugin.format_for_llm(result)
                if formatted:
                    plugin_data_parts.append(formatted)
                continue

            # 高德地图：实时周边搜索
            if plugin.name == "amap_map" and destination:
                if meituan_handled:
                    continue
                should_search = bool(destination) and (user_mentioned_city or ask_location or
                    any(kw in user_query.lower() for kw in ['酒店', '住宿', '景点', '好玩', '美食', '推荐']))
                if not should_search:
                    continue

                query_lower = user_query.lower()
                keywords = "景点|酒店"
                if any(kw in query_lower for kw in ['酒店', '住宿', '住']): keywords = '酒店'
                elif any(kw in query_lower for kw in ['景点', '景区', '好玩']): keywords = '景点'
                elif any(kw in query_lower for kw in ['美食', '好吃', '餐厅', '吃']): keywords = '餐饮'

                params["action"] = "around"
                params["keywords"] = keywords
                params["radius"] = 5000  # 默认 5km
                params["city"] = destination
                # 如果有 GPS 精确坐标，用坐标搜索（精度最高）
                if gps_lng and gps_lat:
                    params["lng"] = gps_lng
                    params["lat"] = gps_lat
                    params["radius"] = 3000  # GPS 精度高，缩小范围到 3km
                result = plugin.execute(params)
                formatted = plugin.format_for_llm(result)
                if formatted:
                    plugin_data_parts.append(formatted)
                continue

            # 本地知识库（仅在无高德或搜索失败时）
            if plugin.name == 'local_knowledge':
                if has_amap or meituan_handled or cost_handled:
                    continue  # 有高德/美团/费用查询时跳过本地知识库
                query_lower = user_query.lower()
                if any(kw in query_lower for kw in ['酒店', '住宿', '住']): params['type'] = 'hotels'
                elif any(kw in query_lower for kw in ['景点', '景区', '门票']): params['type'] = 'spots'
                elif any(kw in query_lower for kw in ['美食', '好吃', '餐厅', '吃']): params['type'] = 'food'
                elif any(kw in query_lower for kw in ['交通', '怎么去']): params['type'] = 'transport'
                else: params['type'] = 'all'
                result = plugin.execute(params)
                formatted = plugin.format_for_llm(result)
                if formatted:
                    plugin_data_parts.append(formatted)
                continue

            result = plugin.execute(params)
            formatted = plugin.format_for_llm(result)
            if formatted:
                plugin_data_parts.append(formatted)
        except Exception as e:
            print(f"[Plugin] {plugin.name} err: {e}")

    # Step 4: 如果用户问位置，附上地图链接
    if ask_location:
        map_link = "\n\n🗺️ **查看你的实时位置和周边地图**: [点击打开地图](http://localhost:5000/map)"
        if plugin_data_parts:
            plugin_data_parts[-1] += map_link
        else:
            plugin_data_parts.append(map_link)

    return '\n\n---\n\n'.join(plugin_data_parts) if plugin_data_parts else ""

# ========== 数据库初始化 ==========
# 热路径护栏原则：DB 不可用（磁盘满、库文件被独占、表损坏）只该让「持久化」失效，
# 不能让已经花钱生成好的答复变成 500，也不能让服务在 import 阶段就起不来。
DB_READY = True
DB_ERROR = ""

try:
    db.init_db()
except Exception as _db_init_err:  # noqa: BLE001
    DB_READY = False
    DB_ERROR = str(_db_init_err)
    print(f'[startup] 数据库初始化失败，降级启动（会话与历史不再持久化）: {_db_init_err}')


def _db_ctx():
    """当前请求路径 + request_id；启动期/后台线程没有请求上下文时给占位值。"""
    try:
        return request.path, _request_id()
    except Exception:  # noqa: BLE001
        return 'startup', ''


def _db_call(label: str, fn, *args, default=None, **kwargs):
    """调用 DAO 函数，失败只记日志 + 写运维错误明细，不向上抛（返回 default）。

    对话热路径上的 DB 读写都是「锦上添花」：答不出来的原因不该是历史没存进去。
    失败仍然可观测——app.log 一条 WARNING，/api/admin/ops 的「最近错误」一条明细。
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        path, rid = _db_ctx()
        app.logger.warning("DB %s 失败，已降级（不影响本轮答复）: %s", label, exc)
        try:
            OPS.record_error(path, f"db_{label} 失败: {exc}", rid, 200, type(exc).__name__)
        except Exception:  # noqa: BLE001
            pass
        return default


def _run_validation(assistant_message: str, plugin_context: str, conversation_id: str):
    """事后反幻觉校验：它自己出问题只能让「本轮没校验」，不能把已生成、已落库的答复打成 500。

    validate_response() 内部已经分环节兜住并计数（见 validation_stats()）；这里是第二层保险，
    兜住报告构造等意料之外的异常。失败照旧留痕：app.log WARNING + /api/admin/ops 最近错误。
    """
    try:
        report = validate_response(assistant_message, plugin_context or "", conversation_id)
        if report.degraded:
            app.logger.warning("session=%s 反幻觉校验部分降级（本轮结论不完整）: %s",
                               conversation_id, report.degraded)
        return report
    except Exception as exc:  # noqa: BLE001
        path, rid = _db_ctx()
        app.logger.warning("反幻觉校验异常，本轮跳过校验（不影响已生成的答复）: %s", exc)
        try:
            OPS.record_error(path, f"validation 失败: {exc}", rid, 200, type(exc).__name__)
        except Exception:  # noqa: BLE001
            pass
        return None

# ========== 配置 ==========
# 注意：API Key 不写入源代码（敏感信息保护）。
# 读取顺序：环境变量 SMARTTOURISM_LLM_API_KEY > data/llm_config.json > 空（启动告警并走兜底）。
# data/llm_config.json 为本地私有文件（已加入 .gitignore），仓库内只提供 llm_config.example.json 模板。
LLM_CONFIG_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', 'llm_config.json')

def _load_llm_api_key():
    key = os.getenv('SMARTTOURISM_LLM_API_KEY', '').strip()
    if key:
        return key
    try:
        if os.path.exists(LLM_CONFIG_FILE):
            with open(LLM_CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f) or {}
            return str(cfg.get('api_key', '') or '').strip()
    except Exception as e:
        print(f'[Config] 读取 data/llm_config.json 失败: {e}')
    return ''

API_CONFIG = {
    "provider": "deepseek",
    "api_key": _load_llm_api_key(),
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "temperature": 0.7,
    "max_tokens": 4096
}

PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"]
    },
    "zhipu": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "models": ["glm-4-plus", "glm-4-flash", "glm-4-long"]
    },
    "qwen": {
        "name": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "models": ["qwen-max", "qwen-plus", "qwen-turbo"]
    },
    "moonshot": {
        "name": "Moonshot Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"]
    },
    "ollama": {
        "name": "本地模型 (Ollama)",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "models": ["qwen2.5:7b", "qwen2.5:14b", "deepseek-r1:8b", "glm4:9b"]
    }
}

# 上下文存储（内存缓存，持久化在 SQLite messages 表）
# 多线程共用：读写一律走下面的加锁访问器，并且只交出副本 —— 直接返回缓存里的活 list
# 会让同一会话并发的两轮互相改写（append_history 是原地 append + 原地截断）。
conversations = {}
_CONV_LOCK = threading.Lock()
CONV_CACHE_MAX = int(os.getenv('SMARTTOURISM_CONV_CACHE_MAX', '256'))


def _conv_cache_put(session_id, history):
    """写回某个会话的历史副本，并把缓存大小压回上限。"""
    with _CONV_LOCK:
        conversations[session_id] = list(history or [])
        # 会话 id 由客户端任意指定，不设上限就是无限内存；淘汰出去的下次从 SQLite 回源
        while len(conversations) > CONV_CACHE_MAX:
            conversations.pop(next(iter(conversations)), None)


def _conv_cache_drop(session_id):
    with _CONV_LOCK:
        conversations.pop(session_id, None)


def _load_conversation_history(session_id):
    """从 SQLite 加载会话历史到内存缓存，返回**副本**（可安全原地追加）。"""
    with _CONV_LOCK:
        cached = conversations.get(session_id)
    if cached is None:
        # DB 读失败不该连内存里已有的历史一起丢掉：降级为「按空历史继续」，答复照常生成
        rows = _db_call("get_session_messages", db.get_session_messages, session_id,
                        default=[], limit=40)
        _conv_cache_put(session_id,
                        [{"role": r["role"], "content": r["content"]} for r in rows])
        # 预热滚动摘要：服务重启后早期对话不会失忆（摘要落库在 sessions.history_summary）
        _db_call("restore_summary", get_summary, session_id)
    with _CONV_LOCK:
        return list(conversations.get(session_id) or [])

# 加载 Skill 定义
SKILL_DEFINITIONS = {}
SKILL_DIR = os.path.join(os.path.dirname(__file__), '..', 'skills')

for skill_name in ['tourism-advisor', 'tourism-marketing', 'smart-matching']:
    skill_path = os.path.join(SKILL_DIR, skill_name, 'SKILL.md')
    if os.path.exists(skill_path):
        with open(skill_path, 'r', encoding='utf-8') as f:
            SKILL_DEFINITIONS[skill_name] = f.read()

# ========== Skill Prompt 构建 ==========

def _dynamic_prompt_tail(extra: str = "") -> str:
    """每次都会变的值统一排到提示词末尾。

    DeepSeek 的上下文缓存按**前缀**命中，命中价远低于未命中价；把精确到分钟的时钟写在
    第一行，等于每一轮都让已缓存的前缀作废、整段按未命中重算。规则是头部逐字不动，
    只有这段变。extra 放本轮其它会变的值（如定位）。
    """
    now = datetime.now()
    weekday = ['一', '二', '三', '四', '五', '六', '日'][now.weekday()]
    return ("\n\n## 本次请求的动态信息（每次可能变化，仅这几行）\n"
            f"当前准确时间：{now.strftime('%Y年%m月%d日 %H:%M')} (星期{weekday})，北京时间 UTC+8。\n"
            + extra)


def build_system_prompt(skill_name: str = None, user_location: str = None) -> str:
    """
    构建系统提示词
    
    根据当前时间和选中的 Skill 生成 LLM 系统级提示词，
    包含角色设定、时间注入、输出格式要求以及 Skill 专属工作流程。
    
    Args:
        skill_name: Skill 名称，如 'tourism-advisor' / 'tourism-marketing' / 'smart-matching'；
                    为 None 时使用通用文旅助手提示词
        user_location: 用户当前位置描述，如 '南宁市 青秀区'；
                       若提供则注入到系统提示词中
        
    Returns:
        完整的系统提示词字符串，可直接作为 messages[0]["content"] 使用
    """
    location_rules = """
关于用户当前位置的规矩（位置取值见末尾「本次请求的动态信息」）：
- 当前位置只是用户的出发点（即地图当前定位），不是旅游目的地；目的地以用户在对话中明确说明的为准（用户消息中的"目的地"字段），绝不能把当前位置当作目的地。
- 仅当用户询问"附近"、"周边"、"离我多远"等当前位置周边问题时，才基于当前位置推荐；用户已说明目的地时，景点推荐、酒店推荐、门票与出行费用查询一律按目的地执行，不要用当前位置（出发点）查门票或费用。
- 若用户尚未说明目的地、也不是问当前位置周边，应先询问目的地，不要用当前位置猜测。
"""

    # 思考过程指令（放在最前面，最醒目）
    thinking_instruction = """**【思考过程 - 最高优先级，必须严格遵守】**
在给出正式回复之前，你必须先用 <thinking> 和 </thinking> 标签包裹你的内部推理过程。
思考过程要包含：分析用户意图、评估可用数据、制定回复策略、预判后续问题。
示例：
<thinking>
用户询问杭州亲子游，核心需求是适合带孩子的景点。
我有高德POI数据可以使用，可以按"必玩+推荐"分层推荐。
回复策略：先列必玩景点附门票和游玩时长，再给实用贴士。
</thinking>
然后输出正式回复。thinking 标签内的内容是你的内部推理，不会展示给用户。
"""

    # 静态头：与本次请求无关，逐字固定。DeepSeek 的上下文缓存按**前缀**命中，命中价远低于未命中价；
    # 原来精确到分钟的时钟排在第 4 行，等于每一轮都把已缓存的前缀作废、整段按未命中重算。
    # 所以规则是：这一段永远不动，会变的东西（时间、定位、Skill）一律排到末尾。
    base_prompt = f"""{thinking_instruction}
你是一个文旅行业的智能助手"智旅灵犀"，专精于景区、酒店、餐饮三大文旅业态。

重要：回答任何关于时间、日期、星期几的问题时，必须使用本提示词末尾「本次请求的动态信息」给出的时间。你自己不知道当前时间，必须相信我提供的时间。
{location_rules}
你的核心能力：
1. **智能咨询与规划**：为游客提供目的地信息、个性化行程规划、酒店推荐、餐饮匹配
2. **营销与文创**：生成多平台营销文案、短视频脚本、活动策划方案、文创产品创意
3. **客源精准匹配**：分析客群画像、精准匹配产品服务、制定转化策略

关键规则：
- 当用户消息中包含"以下是通过数据插件获取的真实实时数据"时，你必须严格使用这些数据
- 天气数据要直接引用真实温度和预报，不要说"无法获取实时数据"
- 景点、酒店数据要使用插件提供的真实搜索数据
- 正式回复直接以攻略/答案本身开头（如行程正文），不要开场白、复述或自述；需求分析、数据评估、策略制定、注意事项等推理内容只能写在 <thinking> 思考过程里，回复中不得出现"用户需求：""用户已确认：""我需要做的是：""注意："等元叙述
- 回复中不要输出任何预订、购票、商品链接或网址（美团/携程等）；引用插件数据时只需注明数据来源（如"数据来源：美团实时数据"）
- 插件数据未提供的字段（如具体价格）直接在回复中说明"该价格未获取到，以官方或平台实时页面为准"，不要编造，也不要输出 ¥1XX、¥2XX 这类脱敏占位价格
- 当用户问"我在哪""附近有什么"时，告诉他们已通过IP定位获取位置，并引导他们点击地图链接查看
- 如果回复中包含 [查看实时位置和周边地图] 链接，在回复末尾提醒用户点击

输出要求（必须严格遵守排版规则）：
- 使用结构化格式，善用列表和分节标题
- **绝对禁止使用 Markdown 表格**（不要用 | 竖线分隔符），改用"项目名：内容"的列表形式或 emoji 编号列表
- **绝对禁止使用 ---、===、***、~~~、___ 等任何分隔线符号**
- **绝对禁止连续使用 |||、===、--- 等字符做装饰**
- 用 **粗体标题** + • 列表来组织信息，例如：
  **🏛️ 景点推荐**
  • 西湖：门票免费，建议游玩2-3小时
  • 灵隐寺：门票30元，建议游玩1-2小时
- 每个大模块之间空一行，保持视觉层次清晰
- 回复要排版美观、层次分明、易于阅读
- 行程方案要包含：概览、景点推荐、住宿推荐(经济/舒适/豪华三档)、餐饮推荐、交通贴士、预算估算
- 营销内容要标注：适配平台、目标客群、传播策略
- 文创方案要包含：灵感来源、产品概念、设计要素、定价策略
- 客群匹配要包含：画像分析、产品匹配、转化策略
- 价格以人民币标注
{ANTI_HALLUCINATION_PROMPT}
{CONFLICT_HANDLING_PROMPT}
"""

    parts = [base_prompt]
    if skill_name and skill_name in SKILL_DEFINITIONS:
        skill_content = SKILL_DEFINITIONS[skill_name]
        # 提取核心部分（限制长度）
        lines = skill_content.split('\n')
        essential = '\n'.join(lines[:120])
        parts.append(
            f"\n\n## 当前激活的 Skill：{skill_name}\n\n"
            f"以下是该 Skill 的详细定义，请严格遵循其中的工作流程和输出模板：\n\n{essential}")

    # 动态段排在最后：只有这几行会变，前面已缓存的部分不受影响
    parts.append(_dynamic_prompt_tail(
        f"用户当前位置（出发点）：{user_location or '未获取到位置'}\n"))
    return "".join(parts)


def build_user_prompt(skill_name: str, crowd_info: dict = None, destination: str = None, user_input: str = "") -> str:
    """
    构建用户提示词
    
    将客群信息、目的地、用户原始输入组合为结构化的用户消息，
    方便 LLM 理解上下文并生成精准回复。
    
    Args:
        skill_name: 当前激活的 Skill 名称
        crowd_info: 客群画像字典，包含 label / age_range / budget_per_person / core_needs / pain_points 等字段
        destination: 目标城市或景区名称
        user_input: 用户当前输入的原始文本
        
    Returns:
        结构化的用户提示词字符串
    """
    parts = []
    
    if crowd_info:
        parts.append(f"目标客群：{crowd_info.get('label', '')}")
        parts.append(f"客群特征：年龄 {crowd_info.get('age_range', '')}，预算 {crowd_info.get('budget_per_person', '')}")
        if crowd_info.get('core_needs'):
            parts.append(f"核心需求：{'、'.join(crowd_info['core_needs'])}")
        if crowd_info.get('pain_points'):
            parts.append(f"主要痛点：{'、'.join(crowd_info['pain_points'])}")
    
    if destination:
        parts.append(f"目的地：{destination}")
    
    if user_input:
        parts.append(f"用户需求：{user_input}")
    
    return '\n'.join(parts)


# ========== 场景智能增强：画像、消费转化、文创赋能 ==========

PLAN_TAG_MAP = {
    "nature": "自然风光",
    "culture": "人文历史",
    "food": "美食探店",
    "shopping": "购物休闲",
    "photo": "拍照打卡",
    "adventure": "户外探险",
    "family": "亲子乐园",
    "night": "夜游演艺",
    "creative": "文创非遗",
    "wellness": "康养慢游",
}

PERSONA_MAP = {
    "relaxed": "松弛慢游型",
    "explorer": "探索尝鲜型",
    "photo": "拍照分享型",
    "culture": "文化深度型",
    "planner": "计划确定型",
    "spontaneous": "随性灵感型",
    "value": "性价比敏感型",
    "premium": "品质体验型",
}

RELATIONSHIP_MAP = {
    "couple": "情侣/伴侣",
    "family": "亲子家庭",
    "friends": "朋友/闺蜜",
    "senior": "银发同行",
    "business": "商务差旅",
    "team": "团建团队",
    "solo": "独自旅行",
}

CONSUMPTION_GOAL_MAP = {
    "balanced": "体验与预算平衡",
    "save": "控制预算、提高性价比",
    "experience": "愿意为特色体验付费",
    "premium": "品质升级与高客单体验",
    "souvenir": "纪念消费与文创伴手礼",
    "social": "社交分享与打卡传播",
}

PACE_MAP = {
    "slow": "轻松慢节奏",
    "medium": "适中节奏",
    "packed": "高密度特种兵",
}

MARKETING_TYPE_MAP = {
    "social": "社交媒体文案",
    "article": "公众号推文",
    "video_script": "短视频脚本",
    "poster": "海报文案",
    "event": "活动策划方案",
    "creative": "文创产品概念",
    "product": "文创产品概念",
    "package": "消费套餐设计",
    "campaign": "整合营销战役",
    "blog": "旅游推广博客",
}

MARKETING_CROWD_MAP = {
    "family": "亲子家庭",
    "couple": "情侣蜜月",
    "senior": "银发一族",
    "team": "公司团建",
    "student": "大学生/Z世代",
    "business": "商务出差",
}

MARKETING_TONE_MAP = {
    "fashion": "时尚潮流",
    "literary": "文艺清新",
    "funny": "幽默风趣",
    "professional": "专业深度",
    "warm": "温暖治愈",
    "premium": "高级品质",
    "local": "在地文化",
}

def _normalize_list(value):
    """将前端传入的列表/逗号字符串统一为干净列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[,，、\n]+", str(value))
    return [str(item).strip() for item in items if str(item).strip()]


def _label_from_map(value, label_map, default=""):
    if not value:
        return default
    return label_map.get(value, value)


def _fetch_plan_real_data(data: dict) -> str:
    """行程规划前置真实数据：美团景点门票/酒店价格 + 高德天气预报。

    任何一路取不到数据都返回明确的反馈文本（而不是静默跳过），
    由模型在方案中向用户说明「暂未获取到实时数据」。

    三路数据并行拉取（线程池），避免串行累加导致流式首字节等待 30~40 秒。
    """
    from concurrent.futures import ThreadPoolExecutor

    city = (data.get("city") or data.get("destination") or "").strip()
    try:
        days = int(data.get("days") or 3)
    except (TypeError, ValueError):
        days = 3
    parts = []

    # 1) 天气（高德实时数据）——单路执行函数
    def _do_weather():
        weather = registry.get("amap_weather")
        if weather and weather.is_configured() and city:
            try:
                w = weather.execute({"city": city, "extensions": "all"})
                return weather.format_for_llm(w) or "[天气数据不可用：高德接口无返回]"
            except Exception as e:
                return f"[天气数据不可用：{e}]"
        if not city:
            return "[天气数据不可用：未指定目的地]"
        return "[天气数据不可用：未配置高德 Web 服务 Key]"

    # 2) 美团：景点门票 + 酒店住宿价格——单路执行函数
    _bookings = []

    def _do_meituan(label, query):
        meituan = registry.get("meituan")
        if not (meituan and meituan.is_configured() and city):
            if not city:
                return "[美团实时数据不可用：未指定目的地]", None
            return "[美团实时数据不可用：未配置美团 Token]", None
        try:
            r = meituan.execute({"city": city, "query": query})
            if r.get("status") == "ok":
                formatted = meituan.format_for_llm(r)
                text = f"【美团实时数据·{label}】\n{formatted}" if formatted else ""
                return text, (r.get("bookings") or [])
            return f"[美团{label}实时数据不可用：{r.get('message', '未知错误')}。相关价格必须标注「暂未获取到实时数据」]", None
        except Exception as e:
            return f"[美团{label}实时数据不可用：{e}]", None

    # 并行拉取三路数据（天气 / 美团门票 / 美团酒店），总耗时≈最慢一路而非三路之和
    weather_text = hotel_text = ticket_text = ""
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            fw = pool.submit(_do_weather)
            ft = pool.submit(_do_meituan, "景点门票",
                             f"请推荐{city}必去的{max(days, 1)}个景点，并逐一给出各景点门票价格（成人票门市价/网络优惠价）")
            fh = pool.submit(_do_meituan, "酒店住宿",
                             f"请推荐{city}适合游客入住的酒店，并逐一给出每晚住宿价格（大床房/标间参考价）")
            weather_text = fw.result()
            ticket_text, tb = ft.result()
            hotel_text, hb = fh.result()
    except Exception as e:
        print('[plan] 并行拉取实时数据失败，降级为空数据:', e)
        weather_text, ticket_text, hotel_text = "", "", ""

    parts.append(weather_text)
    if ticket_text:
        parts.append(ticket_text)
    if hb:
        _bookings.extend(hb)
    if hotel_text:
        parts.append(hotel_text)
    if tb:
        _bookings.extend(tb)

    # 线程结果由主线程统一合并（工作线程里没有请求上下文，绝不能并发写共享容器）
    try:
        _collect_plugin_bookings(_bookings)
    except Exception:
        pass

    return "\n\n---\n\n".join([p for p in parts if p])


def build_scene_plan_messages(data: dict) -> list:
    """构建场景智能型行程规划消息，强调个性化、多偏好平衡与消费转化。"""
    city = data.get("city", "杭州")
    days = data.get("days", "3")
    budget = data.get("budget", "中等")
    tags = [_label_from_map(tag, PLAN_TAG_MAP, tag) for tag in _normalize_list(data.get("tags"))]
    relationship = _label_from_map(data.get("relationship"), RELATIONSHIP_MAP, data.get("relationship") or "自动识别")
    personas = [_label_from_map(p, PERSONA_MAP, p) for p in _normalize_list(data.get("personas"))]
    travelers = data.get("travelers", "")
    companion_preferences = data.get("companion_preferences", "")
    consumption_goal = _label_from_map(data.get("consumption_goal"), CONSUMPTION_GOAL_MAP, data.get("consumption_goal") or "体验与预算平衡")
    pace = _label_from_map(data.get("pace"), PACE_MAP, data.get("pace") or "适中节奏")
    decision_style = data.get("decision_style", "希望方案清晰，但保留可替换选项")
    conversion_focus = _normalize_list(data.get("conversion_focus")) or ["门票/联票", "特色餐饮", "住宿升级", "文创伴手礼", "夜游/演艺"]
    avoid = _normalize_list(data.get("avoid"))
    special_needs = data.get("special_needs", "")
    scenario = data.get("scenario", "")
    transport = data.get("transport_mode", "auto")
    transport_label = {
        "auto": "智能推荐（根据预算与行程自动匹配：经济/平衡偏公共交通，品质偏打车）",
        "transit": "公共交通（地铁/公交，低碳环保）",
        "taxi": "打车/网约车（便捷舒适）",
        "drive": "自驾",
        "walk": "步行与骑行",
        "mix": "公共交通为主，短途打车辅助"
    }.get(transport, "智能推荐（根据预算与行程自动匹配）")

    # 结构化骨架（由 plan_generator.py 预处理生成）
    skeleton = data.get("plan_skeleton", {})
    skeleton_text = ""
    if skeleton:
        try:
            skeleton_text = (
                "\n\n【规划预处理骨架（供参考，必须在最终方案中体现并润色）】\n"
                f"归一化参数：{json.dumps(skeleton.get('preferences', {}), ensure_ascii=False)}\n"
                f"每日主题框架：{json.dumps(skeleton.get('schedule', []), ensure_ascii=False)}\n"
                f"预算参考：{json.dumps(skeleton.get('budget_estimate', {}), ensure_ascii=False)}\n"
                f"三档消费建议：{json.dumps(skeleton.get('three_tiers', {}), ensure_ascii=False)}\n"
                f"推荐消费点：{'、'.join(skeleton.get('conversion_points', []))}\n"
                f"自动假设项：{'；'.join(skeleton.get('assumptions', []))}\n"
                f"数据来源声明：{'；'.join(skeleton.get('data_sources', []))}\n"
                "注意：骨架中的价格为参考区间，最终输出必须标注「来源：AI推断/行业参考」，并提示用户以官方平台为准。"
            )
        except Exception:
            skeleton_text = ""

    system_prompt = f"""你是“智旅灵犀”的首席文旅场景智能规划师。

你的任务不是生成普通路线，而是生成“游客满意 + 商家创收”的个性化游玩、旅居、餐饮、文创消费方案。

必须遵守以下高优先级规则：
1. 先做游客画像：识别同行关系、性格/决策风格、预算、体力节奏、兴趣冲突、消费倾向。
2. 多人同行必须做“偏好平衡”：如果双方需求不同，要说明男方/女方或不同成员分别被满足在哪里，共同满意点在哪里。
3. 每天路线必须嵌入自然消费点，但不能生硬推销：门票/联票、住宿升级、特色餐饮、夜游演艺、文创伴手礼、摄影/研学/非遗体验等。
4. 输出必须体现个性化，不允许不同客群共用同一套模板；每个推荐都要说明“为什么适合这个画像”。
5. 必须给出“基础版/体验升级版/高客单版”三档消费建议，让游客可控预算，让商家有转化空间。
6. 必须包含雨天、体力不足、预算降低、偏好冲突时的替代方案。
7. 不编造实时价格；价格用“参考区间”，并说明以现场/平台为准。
8. 交通规划规则：根据游客选择的出行方式，在每日安排的每一段移动后面标注【交通方式 · 预估时长 · 费用参考】。低碳偏好（公共交通）优先推荐地铁/公交，写出线路名与大致换乘；打车偏好给出大致费用区间；景点间距离很近（约2km内）默认建议步行或骑行，不要机械套用同一种交通方式。
9. 信息来源与可信度：
   - 本地知识库/插件数据：标注“来源：本地文旅知识库（更新时间）”或“来源：高德开放平台（实时）”
   - 模型推断的价格/开放时间：标注“来源：AI推断（建议出行前再次确认）”
   - 绝对禁止不标注来源直接输出价格或时间
10. 异常兜底：
   - 需求冲突时先指出冲突，再给出折中或分档方案
   - 信息无法确认时标注“暂未核实”并提供替代选项
   - 若本次请求包含【规划预处理骨架】但骨架中缺少实时数据，应在方案末尾提示“部分实时数据获取失败，建议出发前再次确认”

输出结构必须包含：
- 一、游客画像与需求冲突识别
- 二、假设与数据来源说明（顶部显著位置）
- 三、个性化行程总览
- 四、每日游玩+旅居+餐饮详细安排
- 五、多人偏好平衡说明
- 六、消费转化设计（消费点、适合人群、推荐理由、预计客单价、是否可替换、来源）
- 七、文创/伴手礼推荐
- 八、三档预算方案
- 九、异常与替代路线
- 十、为什么这套方案比通用攻略更适合用户
- 十一、最终地图路线

排版要求：**绝对禁止使用 Markdown 表格（| 竖线）和 ---、===、***、~~~、___ 等任何分隔线符号**；用 **粗体标题** + • 列表来组织内容；每个大模块之间空一行，保持视觉层次清晰。

完整性要求：**必须一次性完整输出全部十一个部分，直到第十一部分结束为止**；每个部分（尤其是四、每日安排和九、异常与替代路线）的每一条都要写完整，禁止写到一半停笔、禁止用"同上/略"省略；宁可在细节上精炼，也不要让任何部分残缺。

“十一、最终地图路线”是给地图插件读取的机器友好字段，必须严格遵守：
1. 先综合每天安排、交通顺路程度、体力消耗、预算、排队/绕路成本，选择一条更具性价比、最适合地图连线的主路线。
2. 只输出一行，格式必须是：地图路线：地名A → 地名B → 地名C → 地名D
3. 地图路线里只能包含真实可搜索的景点、商圈、酒店、餐厅、车站等地名；不得包含“上午、下午、总览、同行关系、兴趣偏好、消费倾向、决策风格、推荐理由、住宿、餐饮、交通”等非地名词。
4. 地名必须简短、准确，不要写时间、预算、括号说明、推荐原因、门票、动词或形容词。
5. 如果多日行程地点较多，地图路线优先选择最顺路、最有代表性、性价比最高的 5-9 个地点。
6. 不要把目的地城市名本身作为路线点，例如目的地是“南宁”时，不要输出“南宁 → 青秀山”，应直接从“青秀山”开始。
""" + _dynamic_prompt_tail()

    real_data_text = (data.get("real_data_text") or "").strip()
    real_data_section = ""
    if real_data_text:
        real_data_section = (
            "\n\n【真实数据源（必须优先采用，禁止与其中数据冲突）】\n"
            f"{real_data_text}\n"
            "使用规则：\n"
            "1) 上述美团/高德数据为真实数据：景区门票价格、酒店住宿价格、天气信息必须优先采用，"
            "并分别标注「来源：美团（实时）」「来源：高德天气（实时）」；\n"
            "2) 标注「不可用」的数据项，对应内容必须写明「暂未获取到实时数据，建议出行前在美团/高德确认」，"
            "绝对禁止编造具体价格冒充实时数据；\n"
            "3) 真实数据未覆盖的其余价格，才能使用参考区间并标注「来源：AI推断」。"
        )

    # 页内修改计划：携带用户修改意见与原方案，做增量调整而非推倒重来
    revision_request = (data.get("revision_request") or "").strip()
    previous_plan = (data.get("previous_plan") or "").strip()
    revision_section = ""
    if revision_request:
        revision_section = (
            "\n\n【本次为在原方案基础上的修改请求】\n"
            f"用户修改意见：{revision_request}\n"
            + (f"原方案全文：\n{previous_plan}\n" if previous_plan else "")
            + "要求：\n"
            "1) 保留原方案中未被意见触及的安排与输出结构，仅按意见做增量调整；\n"
            "2) 输出仍是完整方案（章节结构与首次生成一致），不要只输出差异部分；\n"
            "3) 如意见与原方案冲突，以用户本次意见为准，并在相应位置简要说明调整原因。"
        )

    user_msg = f"""请生成场景智能型文旅方案。

目的地：{city}
天数：{days}
预算档位：{budget}
同行关系：{relationship}
出行人数/成员：{travelers or '未明确，请按常见场景合理假设并注明'}
性格/画像标签：{'、'.join(personas) if personas else '未明确，请从偏好中推断'}
兴趣偏好：{'、'.join(tags) if tags else '未明确'}
多人偏好差异：{companion_preferences or '未明确，如为情侣/亲子/多人同行，请主动设计平衡点'}
消费倾向：{consumption_goal}
行程节奏：{pace}
决策风格：{decision_style}
希望重点促进的消费：{'、'.join(conversion_focus)}
不想要/避开：{'、'.join(avoid) if avoid else '无'}
特殊需求：{special_needs or '无'}
出行方式偏好：{transport_label}
具体场景补充：{scenario or '无'}
{skeleton_text}{real_data_section}{revision_section}

请特别注意：方案必须让被推荐人觉得“这是为我定制的”，同时让文旅经营者能看到门票、住宿、餐饮、文创、活动体验的转化机会。所有价格/时间类信息必须标注来源。"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg}
    ]


def build_revenue_marketing_messages(data: dict) -> list:
    """构建营销文案生成消息：按内容类型产出不同结构与风格（短文案/推文/脚本/海报/策划）。"""
    destination = data.get("destination", "")
    crowd = data.get("crowd", "")
    tone = data.get("tone", "fashion")
    extra = data.get("extra", "")
    content_type = data.get("type", "social")
    channels = _normalize_list(data.get("channels")) or ["小红书", "抖音", "微信公众号"]
    offer = data.get("offer", "")
    season = data.get("season", "")

    crowd_cn = _label_from_map(crowd, MARKETING_CROWD_MAP, crowd or "自动识别")
    tone_cn = _label_from_map(tone, MARKETING_TONE_MAP, "时尚潮流")

    # 各内容类型的输出结构、篇幅与风格要求：让产出真正贴合该类型的使用场景
    type_specs = {
        "social": {
            "name": "社交媒体种草文案",
            "length": "全文不超过 120 字",
            "structure": (
                "**噱头主标题**：不超过 14 字，制造反差/悬念/利益点，让人想点进来\n"
                "**一句传播语**：不超过 20 字，可直接当海报大字的种草 slogan\n"
                "**三个亮点**：每条不超过 16 字，用 • 开头，具体、有画面感、可验证\n"
                "**行动号召**：不超过 20 字，给出明确动作与转化入口（购票/预订/到店/收藏）\n"
                "**话题标签**：3~5 个 # 标签，贴合平台热搜语感"
            ),
            "style": "口语化、有情绪、多用 emoji 与网感表达，像朋友安利而非官方通稿。",
        },
        "article": {
            "name": "公众号推文",
            "length": "全文 600~900 字",
            "structure": (
                "**标题**：不超过 20 字，有信息量与点开欲\n"
                "**导语**：2~3 句，交代背景并抛出核心吸引点\n"
                "**三个小节**：每节一个 **小标题** + 80~150 字正文，分别讲「是什么/好在哪/怎么玩」\n"
                "**结尾引导**：2~3 句，给出关注/转发/到店/购票的行动入口"
            ),
            "style": "叙事完整、有起承转合，信息密度高，适合深度阅读与转发收藏。",
        },
        "video_script": {
            "name": "短视频分镜脚本",
            "length": "全长 15~30 秒，4~6 个分镜",
            "structure": (
                "**开头钩子**：前 3 秒的台词/画面，必须制造悬念或反差\n"
                "**分镜列表**：每镜一行，格式「镜N｜时长｜画面｜口播台词｜字幕花字」\n"
                "**结尾CTA**：最后一镜的行动号召台词与字幕"
            ),
            "style": "镜头语言具体可拍，口播短句有节奏，字幕花字点睛，避免书面长句。",
        },
        "poster": {
            "name": "海报文案",
            "length": "全文不超过 100 字",
            "structure": (
                "**主标题**：不超过 12 字，海报最大字号\n"
                "**副标题**：不超过 20 字，补充主标题信息\n"
                "**核心slogan**：不超过 16 字，记忆点\n"
                "**卖点三条**：每条不超过 10 字，用 • 开头\n"
                "**行动号召**：不超过 15 字\n"
                "**视觉建议**：一句构图 + 主色调建议"
            ),
            "style": "字字精炼、留白感强，每句都能直接排版上海报。",
        },
        "event": {
            "name": "活动策划案",
            "length": "全文 400~600 字",
            "structure": (
                "**活动名称**：不超过 16 字\n"
                "**主题slogan**：不超过 20 字\n"
                "**时间地点**：一句说明\n"
                "**环节流程**：3~5 个环节，每环节一句玩法说明\n"
                "**活动亮点**：2~3 条，用 • 开头\n"
                "**宣传与转化**：渠道 + 报名/购票/到店的转化设计"
            ),
            "style": "结构清晰、可执行，突出参与感与传播点，避免空泛口号。",
        },
    }
    spec = type_specs.get(content_type, type_specs["social"])

    system_prompt = f"""你是“智旅灵犀”的文旅营销文案主笔。

你的任务：为目的地/产品写一份**{spec['name']}**，核心是制造一个能让人停下拇指的“噱头”——反差、悬念或直击利益点。不要写客群诊断、不要写转化闭环说明、不要写合规清单。

硬性输出结构（只输出以下内容，{spec['length']}）：
{spec['structure']}

写作要求：
1. 语气贴合风格调性与目标客群：{spec['style']}避免官腔与空洞形容词堆砌。
2. 噱头必须真实可落地：不得编造价格与数据，不得使用绝对化极限词（最/第一/顶级/全网唯一），不得虚假承诺。
3. 禁止 Markdown 表格，禁止 ---、===、*** 等分隔线，禁止 # 号标题；除上述结构外，不得输出任何解释、总结、自检或多余模块。
""" + _dynamic_prompt_tail()

    user_msg = f"""请为以下对象写一份{spec['name']}（严格按系统规定的结构输出，{spec['length']}）。

目的地/产品：{destination or '未指定'}
目标客群：{crowd_cn}
风格调性：{tone_cn}
投放渠道：{'、'.join(channels)}（按首个渠道的语感微调）
季节/节点：{season or '未指定'}
优惠/套餐信息：{offer or '未指定'}
补充说明：{extra or '无'}"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg}
    ]


def build_creative_product_messages(data: dict) -> list:
    """构建文创产品核心介绍：约 200 字，聚焦「展示推销 + 读懂文化」，可直接上名片/海报。

    不再输出八大模块策划案（合规清单/产品梯度/联动销售等），那些内容冗长且不适合展示；
    只保留顾客真正会读的核心四模块。若前端勾选生成海报，则额外附一行海报提示词供生图。
    """
    destination = data.get("destination") or data.get("city") or ""
    crowd = data.get("crowd") or data.get("crowd_type") or ""
    extra = data.get("extra", "")
    want_poster = str(data.get("generate_poster", "")).lower() in ("1", "true", "yes", "on")
    seed = data.get("seed") or {}
    if not isinstance(seed, dict):
        seed = {}

    def _s(key, limit=400):
        value = seed.get(key) or ""
        if isinstance(value, (list, tuple)):
            value = "、".join([str(v) for v in value if v])
        return str(value).strip()[:limit]

    seed_name = _s("name", 80)
    seed_desc = _s("desc", 400)
    seed_culture = _s("culture", 600)
    seed_design = _s("design", 600)
    seed_audience = _s("audience", 200)

    poster_line = (
        "\n海报提示词：（单独一行，一句可用于 AI 生图的海报描述：具体产品主体 + 核心文化元素 + 画面构图 + 视觉风格 + 色调，"
        "必须与正文产品严格一致，禁止泛泛风景词）\n"
        if want_poster else ""
    )

    system_prompt = f"""你是“智旅灵犀”的文创产品策划专家。

用户要一段**能直接用于展示推销、并让顾客读懂文化**的文创核心介绍，最终会展示在营销内容框与手机数字名片上。不需要策划案、合规清单、产品梯度、价格表、联动销售方案。

硬性规则（必须全部遵守）：
1. 全文约 200 字（180~230 字）；只输出下面 4 个模块，不得增删模块、不得追加总结或自检说明。
2. 每个模块标题必须用 **粗体** 单独占一行（例如 **核心定位**），标题下正文一句话或一个短段。
3. 语气是“写给顾客看的介绍”，不要出现“我们建议”“本方案”“综上”这类策划口吻。
4. **全部内容面向顾客**：「文化内核」讲这件作品取材的真实文化/地域元素及其来历；「设计亮点」讲材质、工艺与视觉符号本身好在哪。**严禁运营/策划视角表述**（转化、爆款、卖点打法、面向游客的传播策略、可以怎么卖等）；每一句都应当能直接印在名片上给顾客看。
5. 禁止 Markdown 表格，禁止 ---、===、*** 等分隔线，禁止使用 # 号标题。
6. 不得编造价格、销量、获奖、认证、传承人姓名等无法核实的信息。
7. 「一句话传播语」必须 ≤20 字，可直接印在包装或名片标题上。

输出结构（顺序不可变）：
**核心定位**
（这件产品是什么 + 最打动人的一句卖点，≤40 字）

**文化内核**
（取材的真实文化/地域元素及其来历，约 80 字，有故事感与细节，不要一句话带过）

**设计亮点**
（材质、工艺、视觉符号好在哪，约 60 字，有具体工艺名词与使用场景）

**一句话传播语**
（≤20 字）
{poster_line}""" + _dynamic_prompt_tail()

    if seed_name or seed_desc:
        user_msg = f"""请根据下面这条文创灵感，提炼约 200 字的核心介绍。

灵感名称：{seed_name or destination or '未指定'}
灵感简介：{seed_desc or extra or '无'}
文化背景素材：{seed_culture or '无'}
设计/工艺素材：{seed_design or '无'}
目标客群素材：{seed_audience or crowd or '无'}
销售目的地/城市：{destination or '未指定'}

要求：严格按 4 个模块输出，全文约 200 字；素材不足处按该分类通行常识补充，但不得编造具体数据。"""
    else:
        user_msg = f"""请为以下目的地/文化主题构思一件文创产品，并提炼约 200 字的核心介绍。

目的地/文化主题：{destination or '未指定'}
目标客群：{crowd or '未指定，请结合文化主题推断'}
补充说明：{extra or '无'}

要求：严格按 4 个模块输出，全文约 200 字；文化元素须真实可溯源，不得编造具体数据。"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]


def extract_creative_poster_prompt(text: str) -> str:
    """从文创新产品输出中提取海报提示词。"""
    if not text:
        return ""
    # 匹配 "海报提示词：..." 或 "【海报提示词】..."
    patterns = [
        r'海报提示词[：:]\s*(.+?)(?:\n{2,}|$)',
        r'【海报提示词】\s*(.+?)(?:\n{2,}|$)',
        r'七、海报效果图生成提示词\s*[\n\r]+\s*(?:海报提示词[：:]\s*)?(.+?)(?:\n{2,}|$)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            # 保留整段提示词（多行合并为空格），避免截断导致生图主体缺失
            return " ".join(m.group(1).strip().strip('"').strip("'").split())
    return ""


def build_blog_messages(data: dict) -> list:
    """构建旅游推广博客生成消息 — 图文混排，含 AI 配图标记。"""
    destination = data.get("destination", "")
    tone = data.get("tone", "warm")
    crowd = data.get("crowd", "")
    extra = data.get("extra", "")
    tone_cn = _label_from_map(tone, MARKETING_TONE_MAP, "温暖治愈")
    crowd_cn = _label_from_map(crowd, MARKETING_CROWD_MAP, crowd or "泛旅游人群")

    system_prompt = f"""你是"智旅灵犀"的旅游推广博客撰稿人。

你的任务：撰写一篇完整的旅游推广博客文稿，图文混排，面向旅游种草和目的地推广。

博客必须包含以下结构（严格按此顺序输出）：

1. 标题：格式为"关键词｜副标题"，如"山海秘境｜奔赴这座藏在山野间的小城"，标题单独一行，不加粗体标记。

2. 海报文案两行（用于把主视觉渲染成图文并茂的推广海报，独占一行，格式严格如下）：
【海报标题：4-10个汉字，目的地名+核心卖点，如"大理·风与光的城"】
【海报推广语：10-18个汉字，一句有号召力的种草语，如"把假期交给苍山洱海"】

3. 【配图1：此处写出主视觉海报的 AI 生图提示词。要写成"电影级风景大片质感的整版旅游宣传海报设计"式提示词：先描述画面主体的电影感自然风光——大气开阔构图、黄金时刻光影、远近层次分明的景深、通透空气感与胶片色调（如"大理古城全景、青瓦白墙、苍山洱海为背景、晨光暖色调、电影级光影、旅行大片质感"），再描述海报排版——在风景主图之上做文字与图像混排：大气中文书法大标题（标题文字必须原样使用上面【海报标题】的文字）、英文小字副标题、推广语（原样使用【海报推广语】的文字）作为次级标题，并可加入装饰小标签、印章式小字等版式细节】

4. 开篇引言段落（2-3句话，营造氛围，吸引读者）

5. 🌿 游玩打卡点（列出3个景点/打卡地，每个用编号列表，含简短描述）

6. 【配图2：此处写出场景图的 AI 生图提示词，描述游客在景点活动的画面，如"游客在湖畔步道散步，远处青山倒映湖面，自然光，实景感旅行照片"】

7. ✨ 旅行小贴士（列出3条实用信息：最佳季节、建议时长、当地美食）

8. 【配图3：此处写出美食插画的 AI 生图提示词，描述当地特色美食画面，如"农家菜、湖鲜水产、竹编蒸笼盛放的乡土菜肴，俯拍，暖色光，美食摄影风格"】

9. 结语段落（1-2句话，呼应开篇，发出旅行邀请）

排版要求：
- 【配图N：...】与【海报标题：...】【海报推广语：...】标记必须独占一行，括号内的提示词要具体、可生成高质量图片
- 海报标题要短、有记忆点；海报推广语要有行动号召力，两者必须与目的地强相关、不空泛
- 不要使用 Markdown 表格、不要使用 ---、===、*** 等分隔线
- 用 emoji + 粗体标题来组织段落，如 **🌿 游玩打卡点**
- 段落之间空一行
- 语言风格：{tone_cn}，面向{crowd_cn}
- 每个配图提示词需明确画面主体、环境、色调和氛围
""" + _dynamic_prompt_tail()

    user_msg = f"""请撰写一篇旅游推广博客。

目的地/主题：{destination or '请选择一个有特色的小众旅游目的地'}
风格调性：{tone_cn}
目标读者：{crowd_cn}
补充说明：{extra or '无'}

要求：
1. 博客标题要有吸引力，体现目的地特色
2. 海报标题与推广语要像户外广告牌文案：短、准、让人想去，禁止"美丽的""漂亮的"这类空泛词
3. 三张配图的提示词要各具特色：主视觉海报以电影级风景大片质感营造氛围并做文字图像混排、场景图展示游玩体验、美食图勾起出行欲望
4. 正文要真实自然，像真实旅游博主的分享，不要出现"作为AI"等字样
5. 全文中文，约600-800字（不含配图标记）"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg}
    ]


# 加载客群数据
def load_crowd_profiles():
    """从匹配引擎加载客群画像"""
    try:
        sys.path.insert(0, os.path.join(SKILL_DIR, 'smart-matching', 'scripts'))
        from matching_engine import CROWD_PROFILES, MATCH_RULES, identify_crowd, match_products, generate_strategy, full_pipeline
        return CROWD_PROFILES, MATCH_RULES, identify_crowd, match_products, generate_strategy, full_pipeline
    except Exception as e:
        print(f"Warning: Cannot load matching engine: {e}")
        return {}, {}, None, None, None, None

CROWD_PROFILES, MATCH_RULES, identify_crowd, match_products, generate_strategy, full_pipeline = load_crowd_profiles()


# ========== LLM 调用 ==========

def _call_llm_raw(messages: list, stream: bool = False, skill_name: str = "",
             tools: list = None, tool_choice=None) -> dict:
    """
    调用大模型 API（真实网络调用，无缓存）
    
    Args:
        messages: 标准 OpenAI 格式的消息列表 [{"role":"system","content":"..."}, ...]
        stream: 是否启用流式输出
        skill_name: 当前 Skill 名称，用于动态调整 temperature
        tools: 可选，OpenAI function-calling 的 tools 数组；传入即启用工具调用
        tool_choice: 可选，"auto" / "none" / {"type":"function","function":{"name":...}}
        
    Returns:
        非流式: {"choices":[{"message":{"content":"..."}}]} 或 {"error":"...", "detail":"..."}
                启用 tools 时，message 可能含 "tool_calls" 字段
        流式: {"url":"...", "headers":{...}, "payload":{...}} 供上层流式处理用
    """
    provider = API_CONFIG["provider"]
    config = PROVIDERS.get(provider, PROVIDERS["deepseek"])
    
    # 反幻觉：按场景动态调整 temperature（低温度减少幻觉）
    effective_temp = get_temperature_for_skill(skill_name) if skill_name else API_CONFIG["temperature"]

    # 行程规划输出最长（十一个大模块），4096 tokens 会把「九、异常与替代路线」写到一半截断；
    # deepseek-chat 支持最大 8192 输出，这里按 skill 提高上限，其它供应商保持原配置。
    skill_max_tokens = {"plan": 8192}
    max_tokens = API_CONFIG["max_tokens"]
    if skill_name in skill_max_tokens and skill_max_tokens[skill_name] > max_tokens:
        if provider == "deepseek":
            max_tokens = skill_max_tokens[skill_name]
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_CONFIG['api_key']}"
    }
    
    payload = {
        "model": API_CONFIG.get("model") or config["model"],
        "messages": messages,
        "temperature": effective_temp,
        "max_tokens": max_tokens,
        "stream": stream
    }

    # Function calling：仅在显式传入 tools 时启用（向后兼容，默认不影响原有调用）
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    
    url = f"{API_CONFIG['base_url']}/chat/completions"
    
    if not stream:
        _t0 = _usage_now()
        resp, err = _llm_send(url, headers, payload, timeout=120)
        if err is not None:
            print(f"[call_llm] {err['error_code']}: {err['detail']}")
            return err
        try:
            result = resp.json()
        except ValueError as e:
            # 200 但正文不是 JSON（网关 HTML 错误页等）：以前会冒泡成 500，
            # 现在按既有错误契约返回，前端照旧走可读提示
            print(f"[call_llm] bad json: {str(e)[:120]}")
            return {"error": "AI 服务返回的内容无法解析，请稍后重试。",
                    "detail": str(e)[:200], "error_code": "unknown"}
        _msg = ((result.get("choices") or [{}])[0].get("message") or {}) if isinstance(result, dict) else {}
        _note_llm_call(skill_name, _usage_now() - _t0, messages,
                       _msg.get("content") or "", result.get("usage"))
        return result
    else:
        return {"url": url, "headers": headers, "payload": payload}


# ====== 语义缓存：同一 Skill 下关键实体完全一致且语义近似的问句直接复用答复 ======
# 复用 agent/semantic_cache（此前已建好并通过测试，但没接进主链路）。
# 带实体护栏，不会把「杭州3天」的答复错发给「杭州5天」；tools/stream 调用自动跳过。
LLM_CACHE_ENABLED = os.getenv('SMARTTOURISM_LLM_CACHE', '1') != '0'
_LLM_CACHE_TTL = float(os.getenv('SMARTTOURISM_LLM_CACHE_TTL', '900'))
_llm_cache_obj = None
_llm_cache_wrapper = None
_llm_cache_failed = False


def _get_llm_cache():
    global _llm_cache_obj, _llm_cache_wrapper, _llm_cache_failed
    if not LLM_CACHE_ENABLED or _llm_cache_failed:
        return None
    if _llm_cache_wrapper is not None:
        return _llm_cache_wrapper
    try:
        from agent.semantic_cache import SemanticCache, cached_call_llm, make_embedder
        _llm_cache_obj = SemanticCache(
            threshold=float(os.getenv('SMARTTOURISM_LLM_CACHE_SIM', '0.92')),
            ttl=_LLM_CACHE_TTL,
            embedder=make_embedder(prefer_neural=os.getenv('SMARTTOURISM_LLM_CACHE_NEURAL', '0') == '1'),
            max_entries=int(os.getenv('SMARTTOURISM_LLM_CACHE_MAX', '500')))
        _llm_cache_wrapper = cached_call_llm(_call_llm_raw, _llm_cache_obj)
    except Exception as e:  # noqa: BLE001
        _llm_cache_failed = True
        _llm_cache_obj = None
        _llm_cache_wrapper = None
        app.logger.warning("语义缓存不可用，call_llm 直接透传: %s", e)
    return _llm_cache_wrapper


def call_llm(messages: list, stream: bool = False, skill_name: str = "",
             tools: list = None, tool_choice=None) -> dict:
    """对外的唯一 LLM 入口：命中语义缓存则不再发起网络调用，否则透传给 _call_llm_raw。"""
    return _llm_dispatch(messages, stream=stream, skill_name=skill_name,
                         tools=tools, tool_choice=tool_choice)


def _llm_dispatch(messages: list, stream: bool = False, skill_name: str = "",
                  tools: list = None, tool_choice=None) -> dict:
    cached = _get_llm_cache()
    if cached is None or stream or tools:
        return _call_llm_raw(messages, stream=stream, skill_name=skill_name,
                             tools=tools, tool_choice=tool_choice)
    return cached(messages, stream=stream, skill_name=skill_name,
                  tools=tools, tool_choice=tool_choice)


def _llm_cache_stats() -> dict:
    if _llm_cache_obj is None:
        return {'enabled': LLM_CACHE_ENABLED, 'available': not _llm_cache_failed}
    st = dict(_llm_cache_obj.stats())
    st.update({'enabled': True, 'available': True, 'ttl': _LLM_CACHE_TTL})
    return st


def _usage_now() -> float:
    """计量用的时间戳。取不到就回 0.0——记账是旁路，绝不许把正在下行的答复带崩。"""
    try:
        return time.time()
    except Exception:  # noqa: BLE001
        return 0.0


# ====== 模型用量计量：每次调用的 token 与耗时都要有账，成本才讲得出数 ======
# OpenAI 兼容接口响应里带 usage 的一律记成 upstream（上游真实计量，流式末尾块也带）；
# 取不到 usage 时才按字符折算（中文 1 字≈0.75 token，其它 4 字符≈1 token），
# 并单独记成 estimated。两种来源绝不合并成「实测」——答辩里说得出哪个数是量的、
# 哪个数是估的，比报一个好看的总数有用。
# 单价不写死：由环境变量给（元/百万 token），没配就只报 token 不编造金额。
_llm_usage_lock = threading.Lock()
_LLM_USAGE = {
    "calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "upstream_calls": 0,      # 拿到上游 usage 的调用数
    "estimated_calls": 0,     # 只有折算值的调用数
    "duration_ms_total": 0,
    "duration_ms_max": 0,
    "first_token_ms_total": 0,
    "first_token_count": 0,   # 只有流式能量首字耗时
    "first_token_ms_max": 0,
    "cache_hit_tokens": 0,     # 上游报的「按命中价计的输入 token」，只有实测调用才有
    "cache_miss_tokens": 0,    # 上游报的「按未命中价计的输入 token」
    "by_skill": {},
}


def _est_tokens_from_chars(text: str) -> int:
    """把正文字符数折算成 token（只在拿不到上游 usage 时用，结果一律标 estimated）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    return int(cjk * 0.75 + (len(text) - cjk) / 4) + (1 if text.strip() else 0)


def _cache_tokens_from_usage(u: dict) -> tuple:
    """从上游 usage 里取「命中/未命中输入 token」。取不到返回 (0, 0)，不猜。

    DeepSeek 直接给 prompt_cache_hit/miss_tokens；OpenAI 系只给
    prompt_tokens_details.cached_tokens，此时未命中 = 输入总数 - 命中。
    """
    try:
        hit = int(u.get("prompt_cache_hit_tokens") or 0)
        miss = int(u.get("prompt_cache_miss_tokens") or 0)
        if hit or miss:
            return hit, miss
        det = u.get("prompt_tokens_details")
        if isinstance(det, dict):
            hit = int(det.get("cached_tokens") or 0)
            if hit:
                return hit, max(int(u.get("prompt_tokens") or 0) - hit, 0)
    except Exception:  # noqa: BLE001
        return 0, 0
    return 0, 0


def _note_llm_call(skill: str = "", seconds: float = 0.0, messages=None,
                   completion: str = "", usage=None, first_token_seconds=None) -> None:
    """记一次模型调用的账。纯旁路：任何异常都吞掉，绝不影响正在下行的答复。"""
    try:
        u = usage if isinstance(usage, dict) else {}
        pt = int(u.get("prompt_tokens") or 0)
        ct = int(u.get("completion_tokens") or 0)
        upstream = pt > 0 or ct > 0
        if not upstream:
            prompt_text = "".join(str(m.get("content") or "") for m in (messages or [])
                                  if isinstance(m, dict))
            pt = _est_tokens_from_chars(prompt_text)
            ct = _est_tokens_from_chars(completion or "")
        ms = int(max(float(seconds or 0), 0.0) * 1000)
        ft = (int(max(float(first_token_seconds), 0.0) * 1000)
              if first_token_seconds is not None else None)
        hit, miss = _cache_tokens_from_usage(u) if upstream else (0, 0)
        key = skill or "(未标注)"
        with _llm_usage_lock:
            s = _LLM_USAGE
            s["calls"] += 1
            s["prompt_tokens"] += pt
            s["completion_tokens"] += ct
            s["upstream_calls" if upstream else "estimated_calls"] += 1
            s["cache_hit_tokens"] += hit
            s["cache_miss_tokens"] += miss
            s["duration_ms_total"] += ms
            s["duration_ms_max"] = max(s["duration_ms_max"], ms)
            if ft is not None:
                s["first_token_ms_total"] += ft
                s["first_token_count"] += 1
                s["first_token_ms_max"] = max(s["first_token_ms_max"], ft)
            b = s["by_skill"].setdefault(key, {"calls": 0, "prompt_tokens": 0,
                                               "completion_tokens": 0,
                                               "duration_ms_total": 0, "upstream_calls": 0,
                                               "estimated_calls": 0})
            b["calls"] += 1
            b["prompt_tokens"] += pt
            b["completion_tokens"] += ct
            b["duration_ms_total"] += ms
            b["upstream_calls" if upstream else "estimated_calls"] += 1
    except Exception:  # noqa: BLE001
        pass


def _llm_usage_stats() -> dict:
    """把累计账本整理成运维面板要的形状（含均价折算费用；没配单价就只报 token）。"""
    with _llm_usage_lock:
        s = json.loads(json.dumps(_LLM_USAGE))
    calls = s["calls"]
    total_tokens = s["prompt_tokens"] + s["completion_tokens"]
    try:
        price_in = float(os.getenv("SMARTTOURISM_PRICE_IN_PER_M", "") or 0)
        price_out = float(os.getenv("SMARTTOURISM_PRICE_OUT_PER_M", "") or 0)
    except ValueError:
        price_in = price_out = 0.0
    priced = price_in > 0 or price_out > 0
    out = {
        "calls": calls,
        "prompt_tokens": s["prompt_tokens"],
        "completion_tokens": s["completion_tokens"],
        "total_tokens": total_tokens,
        "upstream_calls": s["upstream_calls"],
        "estimated_calls": s["estimated_calls"],
        "avg_duration_ms": round(s["duration_ms_total"] / calls) if calls else 0,
        "max_duration_ms": s["duration_ms_max"],
        "avg_first_token_ms": (round(s["first_token_ms_total"] / s["first_token_count"])
                               if s["first_token_count"] else None),
        "max_first_token_ms": s["first_token_ms_max"] if s["first_token_count"] else None,
        "stream_calls_with_first_token": s["first_token_count"],
        "cache_hit_tokens": s["cache_hit_tokens"],
        "cache_miss_tokens": s["cache_miss_tokens"],
        # 命中率按上游报的输入 token 算；上游没报缓存字段时是 None，不假装 0% 也不假装 100%
        "cache_hit_rate": (round(s["cache_hit_tokens"]
                                 / (s["cache_hit_tokens"] + s["cache_miss_tokens"]), 4)
                           if (s["cache_hit_tokens"] + s["cache_miss_tokens"]) > 0 else None),
        "cache_note": ("命中率取自上游 usage 的 prompt_cache_hit/miss_tokens，"
                       "只有实测调用才有；按前缀命中，提示词头部越稳命中越高"),
        "by_skill": s["by_skill"],
        "model": API_CONFIG.get("model") or PROVIDERS.get(
            API_CONFIG["provider"], PROVIDERS["deepseek"])["model"],
        "token_source": "上游 usage 为实测；拿不到时按字符折算，标在 estimated_calls 里",
    }
    if priced:
        _note = "按累计 token 折算；其中含 estimated 部分，金额是上限估计不是账单"
        if s["cache_hit_tokens"]:
            # 命中价远低于未命中价，这里仍整段按未命中单价乘，所以只会高估不会虚报省了多少
            _note += (f"；其中 {s['cache_hit_tokens']} 输入 token 上游按缓存命中计，"
                      "命中单价未配置，本金额仍按未命中价算，只高估不低估")
        out["cost"] = {
            "currency": os.getenv("SMARTTOURISM_PRICE_CURRENCY", "CNY"),
            "price_in_per_million": price_in,
            "price_out_per_million": price_out,
            "amount": round(s["prompt_tokens"] / 1e6 * price_in
                            + s["completion_tokens"] / 1e6 * price_out, 6),
            "note": _note,
        }
    else:
        out["cost"] = None
        out["cost_note"] = ("未配置单价（SMARTTOURISM_PRICE_IN_PER_M / "
                            "SMARTTOURISM_PRICE_OUT_PER_M），所以只报 token 不报钱")
    return out


# ========== 生图用量与复用计量（纯旁路） ==========
# Seedream 按张计费，一篇博客出 3 张，是全项目唯一按「张」花钱的地方。
# 没有台账就只能说「应该省了几张」，所以出网/成功/失败/复用都要记下来。
# 这里只记张数与耗时：单价走 SMARTTOURISM_PRICE_ARK_PER_IMAGE（元/张），
# 不配就只报张数不编价签——和 token 那套口径一致。

_ARK_USAGE_LOCK = threading.Lock()
_ARK_USAGE = {
    "requests": 0,          # 真的出网生成请求数（不含命中指纹直接复用的）
    "success": 0,           # 拿到并落盘成功（用户看到了图）
    "failed": 0,            # 请求了但没交付（失败可能已被上游计费，所以要能数出来）
    "reused": 0,            # 命中指纹缓存、一次网都没出（省下来的就是这一项）
    "duration_ms_total": 0,
    "duration_ms_max": 0,
    "by_scene": {},         # scene -> {requests,success,failed,reused,duration_ms_total}
    "fail_reasons": {},     # 粗分类 -> 次数（欠费/超时/非200/结果无效）
}

# 复用开关：默认开。想每次都要一张全新图（比如评审现场演示「真的在生成」）就设 0。
ARK_CACHE_ENABLED = os.getenv("SMARTTOURISM_ARK_IMAGE_CACHE", "1") != "0"


def _ark_fingerprint(prompt: str, scene: str, img_num, size: str,
                     model: str, negative_prompt: str = "",
                     watermark: bool = False) -> str:
    """图片内容的指纹 = 所有会影响出图结果的入参。

    只包含**决定画面**的字段：模型、尺寸、完整提示词（含场景定制段）、负面提示词、
    图序、是否带水印。刻意不含 API Key（同一个提示词换个 Key 出的还是同一张图，
    把 Key 算进去会让「欠费回落共享 Key」之后的复用失效），也不含时间戳
    （那等于给自己又造一个缓存杀手）。
    """
    raw = "\x1f".join([str(model or ""), str(size or ""), str(prompt or ""),
                       str(negative_prompt or ""), str(scene or ""), str(img_num),
                       "1" if watermark else "0"])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ark_static_url_to_path(img_url: str) -> str:
    """/static/poster/xxx.jpg -> 本地绝对路径；不在 poster 下或文件名可疑则返回 ''。

    只取 basename 再拼回去，等于把路径遍历的可能性一次掐死：库里那行被人改过、
    或者地址写法奇怪，最多是「认不出来当作没命中」，不会去读仓库外的文件。
    """
    try:
        name = os.path.basename(str(img_url or "").split("?")[0])
        if not name or not name.lower().endswith((".jpg", ".jpeg", ".png")):
            return ""
        folder = os.path.join(app.static_folder or "", "poster")
        path = os.path.normpath(os.path.join(folder, name))
        return path if os.path.isfile(path) else ""
    except Exception:  # noqa: BLE001
        return ""


_ARK_FAIL_KINDS = (
    ("欠费", ("AccountOverdue", "欠费", "overdue", "quota")),
    ("鉴权失败", ("AuthenticationError", "InvalidApiKey", "API Key")),
    ("模型未开通", ("ModelNotOpen", "NotFound")),
    ("生成超时", ("超时", "Timeout")),
    ("结果无效", ("生成结果", "下载失败", "有效图片")),
)


def _ark_fail_kind(msg: str) -> str:
    s = str(msg or "")
    for kind, keys in _ARK_FAIL_KINDS:
        if any(k in s for k in keys):
            return kind
    return "上游报错"


def _note_ark_call(scene: str = "", outcome: str = "success",
                   seconds: float = 0.0, reason: str = "") -> None:
    """记一次生图的账。outcome ∈ {success, failed, reused}。纯旁路：异常一律吞掉。"""
    try:
        kind = "reused" if outcome == "reused" else ("failed" if outcome == "failed" else "success")
        net = kind != "reused"
        ms = int(max(float(seconds or 0), 0.0) * 1000) if net else 0
        key = str(scene or "").strip() or "(未标注)"
        with _ARK_USAGE_LOCK:
            s = _ARK_USAGE
            if kind == "failed":
                s["failed"] += 1
                s["requests"] += 1
                rk = _ark_fail_kind(reason)
                s["fail_reasons"][rk] = s["fail_reasons"].get(rk, 0) + 1
            elif kind == "success":
                s["success"] += 1
                s["requests"] += 1
            else:
                s["reused"] += 1
            if net:
                s["duration_ms_total"] += ms
                s["duration_ms_max"] = max(s["duration_ms_max"], ms)
            b = s["by_scene"].setdefault(key, {"requests": 0, "success": 0,
                                               "failed": 0, "reused": 0,
                                               "duration_ms_total": 0})
            if kind != "reused":
                b["requests"] += 1
            b[kind] += 1
            if net:
                b["duration_ms_total"] += ms
    except Exception:  # noqa: BLE001
        pass


def _ark_usage_stats() -> dict:
    """把生图账本整理成运维接口/面板要的形状（含复用率；没配单价就只报张数）。"""
    with _ARK_USAGE_LOCK:
        s = json.loads(json.dumps(_ARK_USAGE))
    saved = s["reused"]
    total_attempts = s["requests"] + s["reused"]
    try:
        price = float(os.getenv("SMARTTOURISM_PRICE_ARK_PER_IMAGE", "") or 0)
    except ValueError:
        price = 0.0
    out = {
        "enabled": ARK_CACHE_ENABLED,
        "requests": s["requests"],
        "success": s["success"],
        "failed": s["failed"],
        "reused": s["reused"],
        "avg_duration_ms": (round(s["duration_ms_total"] / s["requests"])
                            if s["requests"] else 0),
        "max_duration_ms": s["duration_ms_max"],
        "fail_reasons": s["fail_reasons"],
        "by_scene": s["by_scene"],
        "reuse_rate": (round(s["reused"] / total_attempts, 4)
                       if total_attempts else None),
        "saved_requests": saved,
        "note": ("一次出图请求=一张计费图。命中指纹复用不发起网络请求，"
                 "所以复用的那一次是净省；失败仍算已发出的请求，因为上游是否计费不由我们决定"),
        "cache_persist_note": "本进程计数重启清零；库里累计值跨重启",
    }
    try:
        stats = db.ark_image_cache_stats() or {}
    except Exception:  # noqa: BLE001
        stats = {}
    out["cache_entries"] = int(stats.get("entries") or 0)
    out["cache_reuses_total"] = int(stats.get("reuses") or 0)
    if price > 0:
        out["cost"] = {
            "currency": os.getenv("SMARTTOURISM_PRICE_CURRENCY", "CNY"),
            "price_per_image": price,
            "amount": round(s["requests"] * price, 4),
            "note": f"按 {s['requests']} 次出网请求折算；复用省下的 {s['reused']} 次未计入，失败按已计费口径计（上限估计不是账单）",
        }
    else:
        out["cost"] = None
        out["cost_note"] = ("未配置单价（SMARTTOURISM_PRICE_ARK_PER_IMAGE，元/张），"
                            "所以只报张数不报钱")
    return out


def _ctx_summarizer(transcript: str) -> str:
    """上下文管理：把超出预算的旧轮次压成滚动摘要（复用同一 chat API）。

    失败返回空串 —— 调用方（context_manager）会降级为直接丢弃旧轮次，不影响主对话。
    """
    msgs = [
        {"role": "system", "content":
            "你是对话历史压缩器。把输入的多轮对话压缩成不超过150字的中文摘要，"
            "只保留：目的地/天数/预算/人群等硬约束、用户明确偏好、双方已达成的结论。"
            "不要编造输入里没有的信息，不要输出任何解释，直接输出摘要正文。"},
        {"role": "user", "content": transcript},
    ]
    result = call_llm(msgs)
    if "error" in result:
        print(f"[CTX] 摘要调用失败: {result.get('error')}")
        return ""
    return (result["choices"][0]["message"]["content"] or "").strip()


# ========== 统一错误文案：底层异常 → 用户可读提示 ==========
# 原则：对外只说「发生了什么 + 你能做什么」，堆栈/主机名/代理细节只写服务端日志。
# 返回 (用户可读提示, 错误码)，错误码供前端区分展示（提醒措辞、是否给「重试」按钮）。
def friendly_llm_error(exc) -> tuple:
    detail = str(exc) or exc.__class__.__name__
    low = detail.lower()
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in low or "timeout" in low:
        return ("AI 服务响应超时，可能是当前网络较慢，请稍后重试。", "timeout")
    if isinstance(exc, requests.exceptions.ProxyError) or "proxy" in low or "tunnel connection failed" in low:
        return ("网络连接异常：本机代理未连通外部网络，暂时无法连接 AI 服务。请关闭代理或切换网络后重试。", "network")
    if isinstance(exc, requests.exceptions.SSLError) or "ssl" in low or "certificate" in low:
        return ("网络连接异常（安全校验未通过），暂时无法连接 AI 服务，请更换网络后重试。", "network")
    if isinstance(exc, requests.exceptions.ConnectionError) or "max retries exceeded" in low \
            or "getaddrinfo" in low or "name or service not known" in low or "connection refused" in low:
        return ("无法连接到 AI 服务，请检查网络连接后重试。", "network")
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else 0
        return (_llm_http_message(status), _llm_http_code(status))
    return ("AI 服务暂时不可用，请稍后重试。", "unknown")


def _llm_http_code(status: int) -> str:
    if status in (401, 403):
        return "auth"
    if status == 402:
        return "quota"
    if status == 429:
        return "busy"
    if status >= 500:
        return "server"
    return "unknown"


def _llm_http_message(status: int) -> str:
    code = _llm_http_code(status)
    return {
        "auth": "AI 服务鉴权未通过，暂时无法生成，请联系管理员检查密钥配置。",
        "quota": "AI 服务额度不足，暂时无法生成，请联系管理员处理后重试。",
        "busy": "AI 服务当前请求较多，请稍等片刻后重试。",
        "server": "AI 服务暂时不可用，请稍后重试。",
    }.get(code, "AI 服务返回异常，请稍后重试。")


# ====== LLM 发送：瞬态故障退避重试 + 可选备用通道 ======
# 只重试「重试可能有用」的错误：超时 / 连不上 / 429 / 5xx。
# 鉴权失败、额度欠费重试只是白等 120 秒，直接返回原错误结构（前端措辞不变）。
LLM_RETRY = max(0, int(os.getenv('SMARTTOURISM_LLM_RETRY', '1')))
LLM_RETRY_WAIT = max(0.0, float(os.getenv('SMARTTOURISM_LLM_RETRY_WAIT', '1.5')))
_RETRYABLE_LLM_CODES = {"timeout", "network", "busy", "server"}

# 备用通道全部走环境变量，配置不进仓库、不含任何明文密钥；未配置时行为与改动前完全一致。
def _build_llm_fallback(env=None) -> dict:
    """从环境变量拼备用通道；未配置 base_url 返回 None。"""
    env = os.environ if env is None else env
    base = (env.get('SMARTTOURISM_LLM_FALLBACK_BASE_URL') or '').strip()
    if not base:
        return None
    return {
        "base_url": base.rstrip('/') + "/chat/completions",
        "api_key": (env.get('SMARTTOURISM_LLM_FALLBACK_KEY') or '').strip(),
        "model": (env.get('SMARTTOURISM_LLM_FALLBACK_MODEL') or '').strip(),
    }


_LLM_FALLBACK = _build_llm_fallback()


def _llm_error_dict(exc) -> dict:
    """底层异常 → call_llm 既有错误结构（error / detail / error_code）。"""
    msg, code = friendly_llm_error(exc)
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        detail = exc.response.text[:200]
    elif isinstance(exc, requests.exceptions.ConnectionError):
        detail = "connection_error"
    else:
        detail = code
    return {"error": msg, "detail": detail, "error_code": code}


def _llm_send(url, headers, payload, stream: bool = False, timeout: int = 120):
    """发一次模型请求，带退避重试与备用通道。

    返回 (response, None) 或 (None, 错误字典)；错误字典与 call_llm 原返回同构，
    所以下游（含前端 friendlyErrorMessage）不需要认识新字段。
    流式只在建连阶段重试——一旦开始吐字节再重发会把答复拼重。
    """
    plans = [(url, headers, payload)]
    if _LLM_FALLBACK:
        fb_headers = dict(headers)
        if _LLM_FALLBACK["api_key"]:
            fb_headers["Authorization"] = f"Bearer {_LLM_FALLBACK['api_key']}"
        fb_payload = dict(payload)
        if _LLM_FALLBACK["model"]:
            fb_payload["model"] = _LLM_FALLBACK["model"]
        plans.append((_LLM_FALLBACK["base_url"], fb_headers, fb_payload))

    last_err = None
    for idx, (u, h, p) in enumerate(plans):
        for attempt in range(LLM_RETRY + 1):
            resp = None
            try:
                prep = requests.Request('POST', u, headers=h, json=p).prepare()
                resp = requests.Session().send(prep, stream=stream, timeout=timeout)
                resp.raise_for_status()
                if idx:
                    app.logger.warning("主通道失败，已切换备用通道 model=%s", p.get("model"))
                return resp, None
            except Exception as exc:
                last_err = _llm_error_dict(exc)
                # 非 2xx 的响应体没消费过，显式关掉，避免 waitress 线程里连接悬着
                dead = getattr(exc, "response", None)
                if dead is not None:
                    try:
                        dead.close()
                    except Exception:
                        pass
                elif resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
                if last_err["error_code"] not in _RETRYABLE_LLM_CODES:
                    return None, last_err
                if attempt < LLM_RETRY:
                    time.sleep(LLM_RETRY_WAIT * (2 ** attempt))
                    app.logger.warning("LLM 重试 %d/%d code=%s", attempt + 1, LLM_RETRY,
                                       last_err["error_code"])
        # 这一路彻底失败，若配了备用通道则接着试下一路（不再重新退避）
        if idx == 0 and LLM_RETRY:
            app.logger.warning("主通道重试 %d 次仍失败：%s", LLM_RETRY, last_err["error_code"])
    return None, last_err


# ========== API 路由 ==========

# ╔══════════════════════════════════════════════════════════════╗
# ║  🌐 页面路由 (SPA / 移动端)                                     ║
# ╚══════════════════════════════════════════════════════════════╝

@app.route('/')
def index():
    resp = send_from_directory(app.static_folder, 'index.html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/map')
def map_page():
    return send_from_directory(app.static_folder, 'map.html')


@app.route('/m')
def mobile_page():
    return send_from_directory(app.static_folder, 'mobile.html')


# ╔══════════════════════════════════════════════════════════════╗
# ║  ⚙️  系统配置 API                                             ║
# ╚══════════════════════════════════════════════════════════════╝

@app.route('/api/config', methods=['GET'])
def get_config():
    """获取当前配置（不返回 key）"""
    return jsonify({
        "provider": API_CONFIG["provider"],
        "model": API_CONFIG.get("model") or PROVIDERS.get(API_CONFIG["provider"], {}).get("model", ""),
        "base_url": API_CONFIG["base_url"],
        "temperature": API_CONFIG["temperature"],
        "has_key": bool(API_CONFIG["api_key"]),
        "providers": {k: {"name": v["name"], "models": v.get("models", [])} for k, v in PROVIDERS.items()}
    })


@app.route('/api/config', methods=['POST'])
def save_config():
    """保存配置"""
    data = get_json_payload()
    if not data:
        return jsonify({"success": False, "error": "无效的配置数据"}), 400

    if "api_key" in data and data["api_key"]:
        api_key = str(data["api_key"]).strip()
        if len(api_key) > 512:
            raise ValueError("API Key 长度异常")
        API_CONFIG["api_key"] = api_key
    if "provider" in data:
        provider = str(data["provider"]).strip()
        if provider not in PROVIDERS:
            raise ValueError("不支持的模型供应商")
        API_CONFIG["provider"] = provider
        API_CONFIG["base_url"] = PROVIDERS[provider]["base_url"]
        API_CONFIG["model"] = PROVIDERS[provider]["model"]
    if "model" in data:
        model = str(data["model"]).strip()
        if not model or len(model) > 128:
            raise ValueError("模型名称不合法")
        API_CONFIG["model"] = model
    if "temperature" in data:
        temperature = float(data["temperature"])
        if not 0 <= temperature <= 2:
            raise ValueError("temperature 必须在 0 到 2 之间")
        API_CONFIG["temperature"] = temperature
    
    return jsonify({"success": True, "has_key": bool(API_CONFIG["api_key"])})


@app.route('/api/providers', methods=['GET'])
def list_providers():
    """列出支持的模型供应商"""
    return jsonify({k: {"name": v["name"], "models": v.get("models", [])} for k, v in PROVIDERS.items()})


@app.route('/api/crowds', methods=['GET'])
def list_crowds():
    """列出所有客群"""
    if not CROWD_PROFILES:
        return jsonify([])
    return jsonify([{"id": k, "label": v.get("label", k), "keywords": v.get("keywords", [])} for k, v in CROWD_PROFILES.items()])


@app.route('/api/crowds/<crowd_id>', methods=['GET'])
def get_crowd(crowd_id):
    """获取客群详情"""
    profile = CROWD_PROFILES.get(crowd_id)
    if not profile:
        return jsonify({"error": "客群不存在"}), 404
    return jsonify(profile)


@app.route('/api/match', methods=['POST'])
def match_crowd():
    """客群匹配"""
    data = request.json
    user_input = data.get('input', '')
    destination = data.get('destination', '')
    crowd_id = data.get('crowd_id', '')
    
    if not full_pipeline and not crowd_id:
        return jsonify({"error": "匹配引擎未加载"}), 500
    
    if user_input and full_pipeline:
        result = full_pipeline(user_input, destination)
    elif crowd_id and match_products:
        result = match_products(crowd_id, destination)
        if generate_strategy:
            strategy = generate_strategy(crowd_id)
            result["conversion_strategy"] = strategy
    else:
        return jsonify({"error": "请提供用户描述或客群ID"}), 400
    return jsonify(result)


def _local_knowledge_fallback(user_message: str, destination: str = "") -> str:
    """断网时从本地知识库（RAG + 离线POI）生成可用回答；无结果返回空串。

    策略：
      1. 优先用 RAG 引擎语义检索 skills/*/references/*.md
      2. 补充 plugins.travel_data.LOCAL_KNOWLEDGE 中的城市景点/美食/酒店
      3. 组装成可读文本，开头标注"来自本地知识库"
    """
    parts = []
    city = destination or ""

    # ---- 1. RAG 检索 ----
    try:
        from rag_engine import search_formatted
        rag_ctx = search_formatted(user_message, top_k=5)
        if rag_ctx:
            parts.append(rag_ctx)
    except Exception as e:
        print(f"[offline_fallback] RAG search failed: {e}")

    # ---- 2. 本地 POI 知识库 ----
    try:
        from plugins.travel_data import LOCAL_KNOWLEDGE
        # 如果用户提到了某个城市，就用那个城市；否则遍历所有城市
        cities_to_check = [city] if city and city in LOCAL_KNOWLEDGE else list(LOCAL_KNOWLEDGE.keys())[:3]
        poi_lines = []
        for c in cities_to_check:
            data = LOCAL_KNOWLEDGE.get(c) or {}
            for s in (data.get("scenic_spots") or [])[:3]:
                poi_lines.append(f"- {s.get('name', '')}（{c}景点）：{s.get('level', '')}，建议游玩{s.get('duration', '')}。{('、'.join(s.get('highlights', [])[:2])) if s.get('highlights') else ''}")
            for f in (data.get("food") or [])[:2]:
                poi_lines.append(f"- {f.get('name', '')}（{c}美食）：{f.get('type', '')}，人均{f.get('avg_price', '')}元。招牌：{'、'.join((f.get('must_try') or [])[:2])}")
            for h in (data.get("hotels") or [])[:2]:
                poi_lines.append(f"- {h.get('name', '')}（{c}住宿）：{'⭐' * int(h.get('stars') or 0)}，价格{h.get('price_range', '')}。特色：{'、'.join((h.get('features') or [])[:2])}")
        if poi_lines:
            parts.append("以下是本地知识库中的文旅信息：\n" + "\n".join(poi_lines))
    except Exception as e:
        print(f"[offline_fallback] LOCAL_KNOWLEDGE failed: {e}")

    if not parts:
        return ""

    header = (
        "⚠️ **当前网络不可用，AI 服务暂时无法连接。**\n"
        "以下回答来自平台本地知识库（离线数据），仅供参考：\n"
        "• 信息可能不是最新的，恢复网络后建议再次提问获取实时回答；\n"
        "• 涵盖范围以本地知识库已收录的城市和景点为主。\n\n"
        "---\n\n"
    )
    return header + "\n\n".join(parts)



@app.route('/api/chat', methods=['POST'])
def chat():
    """对话接口"""
    data = request.json
    user_message = data.get('message', '')
    skill_name = data.get('skill', '')
    crowd_id = data.get('crowd_id', '')
    destination = data.get('destination', '')
    conversation_id = data.get('conversation_id', str(uuid.uuid4()))
    stream = data.get('stream', False)

    if not API_CONFIG["api_key"]:
        return jsonify({"error": "请先配置API Key"}), 401
    
    # ====== 提取 GPS 坐标并逆地理编码 ======
    import re as _re
    gps_match = _re.search(r'[\uff08(]\s*(\d+\.?\d*)\s*[,\uff0c]\s*(\d+\.?\d*)\s*[\uff09)]', user_message)
    user_location = None
    if gps_match:
        gps_lng = float(gps_match.group(1))
        gps_lat = float(gps_match.group(2))
        # 去掉消息中的裸坐标，避免模型混淆
        user_message = user_message[:gps_match.start()] + user_message[gps_match.end():]
        user_message = user_message.strip()
        # 逆地理编码获取城市/区
        if _amap_key and gps_lng and gps_lat:
            try:
                regeo_resp = requests.get('https://restapi.amap.com/v3/geocode/regeo', params={
                    'key': _amap_key,
                    'location': f'{gps_lng},{gps_lat}',
                    'extensions': 'base'
                }, timeout=8)
                regeo_data = regeo_resp.json()
                if regeo_data.get('status') == '1':
                    rc = regeo_data.get('regeocode', {}).get('addressComponent', {})
                    city = rc.get('city', '') or rc.get('province', '')
                    district = rc.get('district', '')
                    user_location = f'{city} {district}'.strip()
                    # 当前位置只作为出发点/地图定位记住，绝不兜底成目的地（目的地以用户明确说明为准）
            except Exception as e:
                print(f'[Chat] Reverse geocode failed: {e}')
    # ====== GPS 提取完毕 ======

    # ====== 重新生成 / 编辑重发：先把最后一轮收回，再按正常流程走一遍 ======
    # 放在 GPS 提取之后：库里存的提问是不带坐标后缀的，比对才比得上。
    # 只有「前端说要撤的那一轮确实是库里最后一轮」才真撤——对不上说明上一轮根本没落库
    # （比如请求就没送达），这时删掉的是更早一轮的好答复，比不撤回更糟。
    _regenerate = bool(data.get('regenerate'))
    _edited = data.get('edit_message')
    _edited = _edited.strip()[:4000] if isinstance(_edited, str) else ''
    _rolled_back = False
    if _regenerate or _edited:
        _last_q = _db_call("get_last_user_message", db.get_last_user_message,
                           conversation_id, default=None)
        if not _last_q:
            return jsonify({"success": False, "code": "nothing_to_regenerate",
                            "error": "这一轮没有可撤回的历史，请直接重新提问"}), 400
        _of_raw = data.get('edit_of') if _edited else user_message
        _of = _of_raw.strip() if isinstance(_of_raw, str) else ''
        if _of and _of == _last_q.strip():
            _db_call("delete_last_turn", db.delete_last_turn, conversation_id, default=0)
            # 内存缓存里还留着刚撤回的那一轮，不清掉下一句就把「已经撤掉的答复」当历史喂给模型
            _conv_cache_drop(conversation_id)
            _rolled_back = True
        if _edited:
            # 库里不能留裸坐标：老客户端可能把定位后缀拼进了 edit_message
            _edited = _re.sub(r'[\uff08(]\s*\d+\.?\d*\s*[,\uff0c]\s*\d+\.?\d*\s*[\uff09)]', ' ',
                              _edited).strip()
            user_message = _edited
    # ====== 重发处理完毕 ======
    
    # 确保会话存在于DB，加载历史
    # 长期记忆按用户隔离，与登录态解析口径保持一致
    _chat_uid = _resolve_uid(data.get('user_id'))
    db_session = _db_call("get_session", db.get_session, conversation_id)
    if not db_session:
        # 按用户隔离会话：登录用户用其 user_id，否则用请求传入的 user_id 或 'default'（游客）
        _db_call("create_session", db.create_session, user_id=_chat_uid, session_id=conversation_id)
        # 首条提问就是标题：session_id 是 uuid，历史对话列表里没有标题用户认不出哪条是哪条
        _db_call("set_session_title", db.set_session_title, conversation_id,
                 db.title_from_message(user_message))
    else:
        _db_call("update_session_active", db.update_session_active, conversation_id)
    history = _db_call("load_history", _load_conversation_history, conversation_id, default=[])
    
    # ====== 反幻觉：用户纠正信号检测 ======
    conflict = detect_user_conflict(user_message)
    conflict_prefix = build_conflict_aware_prefix(conflict)
    if conflict.is_correction or conflict.is_give_up:
        app.logger.info(
            "request_id=%s session=%s conflict detected: strength=%s signals=%s",
            _request_id(), conversation_id, conflict.correction_strength,
            conflict.detected_signals,
        )
    # ====== 冲突检测完毕 ======
    
    # ====== 反幻觉：会话事实追踪 ======
    fact_tracker = get_fact_tracker()
    fact_tracker.update_facts(conversation_id, user_message)
    # ====== 事实追踪完毕 ======

    # ====== 目的地解析：只认用户明确说明的目的地 ======
    # 当前位置(user_location)仅作为出发点记住；目的地为空时复用会话中已确认的目的地，
    # 保证后续轮次推荐景点/酒店、查门票费用时仍沿用户说过的目的地；
    # 纯附近/周边类提问不注入历史目的地，交给插件按当前位置搜周边
    if not destination:
        _near_kws = ('附近', '周边', '离我', '我这', '我在哪', '我的位置', '当前位置')
        if not any(k in user_message for k in _near_kws):
            destination = str(fact_tracker.get_facts(conversation_id).get('destination') or '').strip()
    # ====== 目的地解析完毕 ======
    
    # 构建消息（传入用户位置）
    system_prompt = build_system_prompt(skill_name, user_location=user_location)
    
    crowd_info = None
    if crowd_id and crowd_id in CROWD_PROFILES:
        crowd_info = CROWD_PROFILES[crowd_id]
    
    enhanced_message = build_user_prompt(skill_name, crowd_info, destination, user_message)
    
    # 注入冲突感知前缀（如果有）
    if conflict_prefix:
        enhanced_message = conflict_prefix + enhanced_message

    def _assemble_context():
        """组装完整消息（含阻塞式实时数据采集）。

        流式路径把它推迟到 generate() 内部执行，以便在采集数据的阻塞阶段
        向前端推送真实的操作状态（如「数据收集中」），而不是干等省略号。
        返回 (messages, ctx_meta, plugin_context, plugin_bookings, rag_sources, collect_ms)。
        """
        nonlocal enhanced_message

        _t_collect = time.time()

        # ====== 插件：自动获取实时数据 ======
        _plugin_context = get_plugin_data(user_message, destination)
        _plugin_bookings = _take_plugin_bookings()  # 美团等插件采集的可预订入口（门票/酒店）
        if _plugin_context:
            enhanced_message += f"\n\n---\n**以下是通过数据插件获取的真实实时数据，你必须严格基于这些数据回答，绝对不能编造或忽略！如果数据包含天气，就用真实天气规划行程；如果数据包含酒店价格，就用真实价格推荐。**\n\n{_plugin_context}"
        # ====== 插件数据注入完毕 ======

        # ====== 反幻觉：RAG 知识库检索 ======
        # 同时拿到「注入提示词的上下文」和「可展示给用户的出处清单」：
        # 检索确实发生在主路径上，却对用户完全不可见，反幻觉就只剩一句「待核实」。
        rag_context, _rag_sources = retrieve_knowledge(user_message, top_k=3)
        if rag_context:
            enhanced_message += rag_context
        _collect_ms = int((time.time() - _t_collect) * 1000)
        # ====== RAG 注入完毕 ======

        # ====== 反幻觉：事实锚定 —— 已确认信息放当前消息开头（上下文末尾最强位置）======
        facts_block = build_facts_block(conversation_id)
        if facts_block:
            enhanced_message = facts_block + enhanced_message
        # ====== 事实锚定完毕 ======

        # ====== 长期记忆：跨会话偏好/约束召回（排在事实锚定之后，冲突时以本轮为准）======
        memory_block = _memory_block(_chat_uid, user_message)
        if memory_block:
            if facts_block and enhanced_message.startswith(facts_block):
                enhanced_message = (facts_block + memory_block
                                    + enhanced_message[len(facts_block):])
            else:
                enhanced_message = memory_block + enhanced_message
            app.logger.info("request_id=%s session=%s memory_injected chars=%d",
                            _request_id(), conversation_id, len(memory_block))
        # ====== 长期记忆注入完毕 ======

        # ====== 上下文管理：token 预算 + 滑动窗口 + 滚动摘要（替代旧的历史[-20:]硬截断）======
        _messages, _ctx_meta = manage_context(
            conversation_id, system_prompt, history, enhanced_message,
            summarizer=_ctx_summarizer)
        app.logger.info("request_id=%s session=%s ctx=%s",
                        _request_id(), conversation_id, _ctx_meta)
        # ====== 上下文组装完毕 ======
        return _messages, _ctx_meta, _plugin_context, _plugin_bookings, _rag_sources, _collect_ms

    if stream:
        return stream_response(None, conversation_id, user_message,
                               destination=destination, assemble=_assemble_context,
                               skill=skill_name)

    messages, ctx_meta, plugin_context, plugin_bookings, rag_sources, _collect_ms = _assemble_context()

    # 带实时数据（天气/票价）的答复不进语义缓存也不取缓存，避免把昨天的天气发给今天
    if plugin_context:
        result = _call_llm_raw(messages, skill_name=skill_name)
    else:
        result = call_llm(messages, skill_name=skill_name)
    
    if "error" in result:
        # 断网/超时：直接返回网络异常提示，不生成回答
        return jsonify({"success": False, "error": result["error"], "detail": result.get("detail", ""), "error_code": result.get("error_code", "")}), 500

    if (result.get("_cache") or {}).get("hit"):
        # 缓存键 = 含事实锚定/长期记忆/RAG 注入的完整当前消息，
        # 键一致意味着这些注入内容与首次生成时逐字相同 → 缓存答复可安全直接落库返回。
        app.logger.info("request_id=%s session=%s llm_cache_hit sim=%s",
                        _request_id(), conversation_id, result["_cache"].get("similarity"))
    
    assistant_message = result["choices"][0]["message"]["content"]
    # 非流式路径同样剥离思考块，否则分析内容混进 reply（2026-09-24 实测）
    assistant_message = re.sub(r'<thinking>.*?</thinking>', '', assistant_message, flags=re.DOTALL).strip()
    
    # 后处理：清理违规排版符号（分隔线、表格等）
    assistant_message = clean_llm_output(assistant_message)
    
    # 保存消息到数据库（写失败不能把已经生成好的答复变成 500）
    _db_call("save_message:user", db.save_message, conversation_id, 'user', user_message)
    
    # 保存上下文到内存（token 截断 + 摘要折叠计数由 context_manager 统一维护）
    append_history(conversation_id, history, user_message, assistant_message)
    _conv_cache_put(conversation_id, history)
    
    # 保存 agent 消息到数据库
    intent_for_db = skill_name if skill_name and skill_name in ('consult','plan','marketing','cultural','other') else 'consult'
    # 返回本轮答复的 message_id：非流式调用方（语音/接口集成）也要有点赞/点踩的落点
    _agent_row = _db_call("save_message:agent", db.save_message, conversation_id, 'agent', assistant_message,
                          intent_type=intent_for_db,
                          sources_json=(json.dumps(rag_sources, ensure_ascii=False) if rag_sources else None))
    
    # ====== 反幻觉：事后事实校验（校验自己坏了不拖累已生成的答复）======
    validation = _run_validation(assistant_message, plugin_context, conversation_id)
    if validation is not None and validation.has_issues:
        app.logger.warning(
            "request_id=%s session=%s hallucination_check=%s",
            _request_id(), conversation_id, validation.to_log_dict(),
        )
    # ====== 事实校验完毕 ======

    # ====== 长期记忆：答复已给出，后台沉淀本轮偏好（不占响应时间）======
    _memory_remember(_chat_uid, user_message, conversation_id)
    # ====== 长期记忆写入完毕 ======
    
    return jsonify({
        "success": True,
        "conversation_id": conversation_id,
        "reply": assistant_message,
        "message_id": (_agent_row or {}).get("message_id"),
        "skill_used": skill_name or "auto",
        "bookings": plugin_bookings or [],
        "sources": rag_sources or [],
        "collect_ms": _collect_ms,
        "validation": validation.to_log_dict() if validation is not None and validation.has_issues else None,
        "context": ctx_meta,
    })


def _extract_locations_from_text(text: str) -> list:
    """
    从 LLM 回复文本中提取地点名称并查询高德 POI 信息
    
    通过正则匹配 Markdown 表格、粗体、列表项中的地点候选名称，
    调用高德 POI 搜索 API 获取坐标、照片等详细信息，
    用于前端在地图上标注。
    
    Args:
        text: LLM 生成的完整回复文本（可能包含 Markdown 格式）
        
    Returns:
        地点信息列表，每个元素为 dict：
        {"name": "地点名", "category": "景点/酒店/美食/交通",
         "lng": 经度, "lat": 纬度, "photo": "照片URL", "address": "地址"}
        若未配置高德 Key 或未提取到有效地点则返回空列表
    """
    if not text or not _amap_key:
        return []

    # 地点-类别映射关键字
    category_patterns = {
        "景点": ["景区", "公园", "寺", "塔", "湖", "山", "岛", "古城", "古镇", "博物馆", "园林", "长城", "故宫", "水乡", "湾", "天涯", "南山", "宋城", "故居", "纪念馆", "广场", "步行街"],
        "酒店": ["酒店", "宾馆", "民宿", "客栈", "山庄", "度假村", "青旅"],
        "美食": ["餐厅", "酒楼", "小吃", "火锅", "面馆", "烧烤", "海鲜", "楼外楼", "全聚德", "知味观"],
        "交通": ["机场", "火车站", "站", "客运站", "地铁站"],
    }

    # 常见旅游城市列表，用于辅助判断
    known_cities = ["杭州", "三亚", "成都", "北京", "大理", "丽江", "上海", "广州", "深圳",
                    "厦门", "桂林", "西安", "南京", "重庆", "青岛", "武汉", "长沙", "昆明",
                    "张家界", "黄山", "九寨沟", "拉萨", "哈尔滨", "苏州", "乌镇", "温州"]

    # 从文本中提取已知的 POI 名称（用表格、列表、粗体等格式中出现的名称）
    candidates = set()

    # 匹配 markdown 表格中的内容
    table_cell_pattern = re.compile(r'\|\s*\*{0,2}([^*\n|]{2,20})\*{0,2}\s*\|')
    for m in table_cell_pattern.finditer(text):
        name = m.group(1).strip()
        if len(name) >= 2 and not any(c in name for c in ['¥', '元', '分', '⭐', '－', '-', 'http', '免费']):
            candidates.add(name)

    # 匹配 **粗体** 名称
    bold_pattern = re.compile(r'\*\*([^*\n]{2,30})\*\*')
    for m in bold_pattern.finditer(text):
        name = m.group(1).strip()
        if len(name) >= 2 and not any(c in name for c in ['⭐', '¥', 'http', '第', '天']):
            candidates.add(name)

    # 匹配列表项中的名称
    list_pattern = re.compile(r'^\s*[\d]+[\.\、\)]\s*\*{0,2}([^*\n\d]{2,30})\*{0,2}', re.MULTILINE)
    for m in list_pattern.finditer(text):
        name = m.group(1).strip()
        if len(name) >= 2:
            candidates.add(name)

    # 过滤：去掉纯数字、太短、明显不是地名的
    filtered = set()
    skip_words = {'住宿推荐', '美食推荐', '交通指南', '行程概览', '预算估算', '注意事项',
                  '菜品名称', '菜系', '营业时间', '推荐菜品', '贴士', '交通', '酒店',
                  '景点', '美食', '杭州', '三亚', '成都', '北京', '大理', '经济型',
                  '舒适型', '豪华型', '价格', '人均', '评分', '特色', '地址', '备注',
                  '建议', '玩法', '攻略', '概述', '说明', '方案'}
    for name in candidates:
        if name in skip_words or name in known_cities:
            continue
        if len(name) < 3:
            continue
        # 去掉以数字开头的（可能是价格）
        if name[0].isdigit():
            continue
        filtered.add(name)

    if not filtered:
        return []

    # 为每个候选地点通过高德API获取信息
    locations = []
    seen_names = set()
    seen_poi_names = set()  # 解析后的 POI 名去重（防重复卡片）
    default_city = "杭州"
    # 从文本中检测城市
    for city in known_cities:
        if city in text:
            default_city = city
            break

    for name in list(filtered)[:5]:  # 最多处理5个地点
        if name in seen_names:
            continue
        seen_names.add(name)

        # 确定类别
        category = "景点"
        for cat, keywords in category_patterns.items():
            if any(kw in name for kw in keywords):
                category = cat
                break

        # 通过高德 POI 搜索获取坐标
        try:
            search_url = "https://restapi.amap.com/v3/place/text"
            resp = requests.get(search_url, params={
                "key": _amap_key,
                "keywords": name,
                "city": default_city,
                "offset": 1,
                "extensions": "all"
            }, timeout=8)
            data = resp.json()

            if data.get("status") == "1" and data.get("pois"):
                poi = data["pois"][0]
                poi_name = poi.get("name", name) or name
                poi_type = poi.get("type", "") or ""

                # 用高德 POI 类型过滤非文旅点位：候选名（如酒店全称）常被高德错配到
                # 购物中心/超市等 POI，会在对话页渲染出与原文无关的地点卡片（2026-09-19 实测踩坑）
                if any(k in poi_type for k in ("购物", "超市", "家居", "建材", "停车场", "加油站")):
                    continue
                # 用 POI 类型校正分类，避免"猜不到关键词就默认景点"
                if any(k in poi_type for k in ("住宿服务", "宾馆酒店", "旅馆")):
                    category = "酒店"
                elif any(k in poi_type for k in ("餐饮服务", "餐厅", "快餐", "咖啡")):
                    category = "美食"
                elif any(k in poi_type for k in ("风景名胜", "公园广场", "博物馆", "纪念馆", "游乐场")):
                    category = "景点"
                # 按解析后的 POI 名去重：不同候选名可能命中同一 POI，避免重复卡片
                if poi_name in seen_poi_names:
                    continue
                seen_poi_names.add(poi_name)

                location_str = poi.get("location", "")
                lng, lat = (0.0, 0.0)
                if location_str and "," in location_str:
                    parts = location_str.split(",")
                    lng, lat = float(parts[0]), float(parts[1])

                # 提取照片
                photo_url = ""
                photos = poi.get("photos", [])
                if photos:
                    photo_info = photos[0]
                    photo_url = photo_info.get("url", "")
                    # 高德返回的 photo url 可能需要补全
                    if photo_url and not photo_url.startswith("http"):
                        photo_url = "https:" + photo_url if photo_url.startswith("//") else ""

                locations.append({
                    "name": poi_name,
                    "category": category,
                    "lng": lng,
                    "lat": lat,
                    "photo": photo_url,
                    "address": poi.get("address", ""),
                })
        except Exception:
            pass

    return locations


# ==================== 流式生成的中断控制 ====================
# 前端「停止生成」→ POST /api/chat/stop → 置位这一路的取消标志。
# 为什么不用只靠 fetch abort：abort 之后服务端要等下一次 yield 失败才察觉，
# 已经生成的半截内容既没落库也没机会收尾；显式取消让服务端自己停下、把看到的内容如实存下。
# 真正断连（关页面/切会话）另有 GeneratorExit 分支兜住，两条路径分开计数。
_stream_events = {}
_stream_lock = threading.Lock()
_stream_stats = {"cancelled": 0, "client_disconnected": 0, "active": 0}
STREAM_TTL_S = float(os.getenv('SMARTTOURISM_STREAM_TTL', '900'))


def _stream_register(sid):
    """登记一路流式生成，返回它的取消 Event；顺带淘汰超时残留（只增不减就是无界内存）。"""
    ev = threading.Event()
    now = time.time()
    with _stream_lock:
        for stale in [k for k, (_, ts) in _stream_events.items() if now - ts > STREAM_TTL_S]:
            _stream_events.pop(stale, None)
        _stream_events[sid] = (ev, now)
        _stream_stats["active"] += 1
    return ev


def _stream_unregister(sid):
    with _stream_lock:
        if _stream_events.pop(sid, None) is not None:
            _stream_stats["active"] = max(0, _stream_stats["active"] - 1)


def _stream_stop(sid):
    """置位取消标志，返回是否命中活跃的流。"""
    with _stream_lock:
        entry = _stream_events.get(sid)
    if entry is None:
        return False
    entry[0].set()
    return True


def _stream_cancelled(ev):
    return ev is not None and ev.is_set()


def stream_stats():
    with _stream_lock:
        return dict(_stream_stats)


def stream_response(messages, conversation_id, user_message, bookings=None, destination=None,
                    ctx_meta=None, assemble=None, plugin_context=None, skill=None,
                    rag_sources=None):
    """
    流式响应处理（DeepSeek 风格思考过程展示）

    模型会在回复前输出 <think>...</think> 块，后端实时解析并推送思考事件。

    阻塞式的实时数据采集（美团规划可达 30~40s）与预思考调用被推迟到 generate()
    内部执行，采集期间通过 thinking_status 事件向前端推送真实操作进度
    （如「数据收集中」），而不是让用户干等一个省略号。

    Args:
        messages: 完整的消息列表；当传入 assemble 时可为 None（由 assemble 延迟生成）
        conversation_id: 会话 ID
        user_message: 用户当前输入文本
        bookings: 插件采集的可预订入口（门票/酒店），流末以 SSE 事件下发
        ctx_meta: 上下文管理统计，随流以 SSE 事件下发（前端上下文进度条每轮更新）
        assemble: 无参回调，返回 (messages, ctx_meta, plugin_context, bookings,
                  rag_sources, collect_ms)；存在时把阻塞采集推迟到流内部执行以推送实时状态
        plugin_context: 已采集的插件数据（非 assemble 路径），供流末事实校验使用
        rag_sources: 已采集路径的知识库出处清单（非 assemble 路径用），随流下发

    Returns:
        Flask Response (text/event-stream)
    """
    def _status(msg):
        return f"data: {json.dumps({'type': 'thinking_status', 'data': {'message': msg}}, ensure_ascii=False)}\n\n"

    def generate():
        full_response = ""      # 最终回复（不含 thinking）
        thinking_text = ""       # 思考过程原始文本
        in_thinking = False      # 当前是否在 <think> 块内
        model_thinking = False   # 模型是否自己输出了 thinking
        pre_thinking_sent = False # 预生成思考是否已发送
        buffer = ""              # 缓冲区
        seen = ""                # 已流出的累计正文，供预生成思考发送决策判断（buffer 每轮会被消费）
        pending_content = ""     # 思考决策未定前暂存的正文，确保思考总是先于回复出现
        resp = None              # 上游 LLM 的流式响应，退出前必须关掉
        cancelled = False        # 用户主动停止
        _answer_id = str(uuid.uuid4())  # 本轮 agent 消息 id：[DONE] 之前就告知前端，点踩/点赞才有落点
        _rag_sources = []        # 本轮知识库检索出处（采集阶段填充）
        _collect_ms = 0          # 数据采集耗时（美团/高德/知识库）

        _sid = _request_id() or str(uuid.uuid4())
        _cancel_ev = _stream_register(_sid)
        try:
            # 第一条事件就把 stream_id 告诉前端：数据采集阶段（可达数十秒）也能点「停止」
            yield f"data: {json.dumps({'type': 'stream_id', 'data': {'request_id': _sid}}, ensure_ascii=False)}\n\n"

            # ====== 阻塞阶段 1：实时数据采集（推迟到流内部，期间推送真实状态）======
            if assemble is not None:
                yield _status('📡 数据收集中（美团 / 高德 / 知识库）...')
                msgs, meta, pctx, bks, _rag_sources, _collect_ms = assemble()
                yield _status('✅ 数据收集完成，正在连接 AI...')
            else:
                msgs, meta, pctx, bks = messages, ctx_meta, plugin_context, (bookings or [])
                _rag_sources, _collect_ms = (rag_sources or []), 0

            if _stream_cancelled(_cancel_ev):
                # 采集阶段就被叫停：一个字都没生成，整轮按「没发生」处理，不落半截问答
                with _stream_lock:
                    _stream_stats["cancelled"] += 1
                yield f"data: {json.dumps({'type': 'stopped', 'data': {'chars': 0, 'stage': 'collecting'}}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # 上下文占用每轮都下发（前端画进度条）；真正折叠过旧轮次时附带压缩提示
            if meta:
                yield f"data: {json.dumps({'type': 'context', 'data': meta}, ensure_ascii=False)}\n\n"

            # 采集耗时与知识库出处：检索一直跑在对话主路径上，用户却完全看不到依据来自哪里
            yield f"data: {json.dumps({'type': 'preparation', 'data': {'collect_ms': _collect_ms, 'rag_sources': len(_rag_sources), 'realtime': bool(pctx)}}, ensure_ascii=False)}\n\n"
            if _rag_sources:
                yield f"data: {json.dumps({'type': 'sources', 'data': _rag_sources}, ensure_ascii=False)}\n\n"

            # 本轮 agent 消息 id 在正文前就告知：答复一出来就能点赞/点踩，不必等 [DONE]。
            # 这个 id 与落库用的 message_id 同一个（见下方 save_message），对不上就是没落库，
            # 反馈接口会如实回 404，不拿一个「大概存在」的 id 让前端攒假反馈。
            yield f"data: {json.dumps({'type': 'answer_id', 'data': {'message_id': _answer_id}}, ensure_ascii=False)}\n\n"

            result = call_llm(msgs, stream=True, skill_name=skill or "")
            if isinstance(result, dict) and "url" not in result:
                # 断网/超时：直接返回网络异常提示，不生成回答
                yield f"data: {json.dumps({'error': result.get('error', '连接 AI 服务失败，请检查网络连接后重试。'), 'error_code': result.get('error_code', 'network')}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # ====== 阻塞阶段 2：预生成思考过程（如果模型自己输出 <think> 则优先用模型的）======
            yield _status('🤔 正在思考回复...')
            pre_thinking = ""
            try:
                thinking_prompt = [
                    {"role": "system", "content": (
                        "你是一个 AI 的内部推理模块。请详细分析用户问题并输出推理过程。\n"
                        "要求：\n"
                        "1. 分析用户的核心意图和需求\n"
                        "2. 评估可用信息（插件数据、历史对话、目的地知识）\n"
                        "3. 制定回复策略（推荐哪些内容、如何组织）\n"
                        "4. 考虑用户可能的后续问题\n"
                        "直接输出推理文本，不要标签，不要说「我将...」。"
                    )},
                    {"role": "user", "content": user_message}
                ]
                t_result = ({"error": "已被用户停止"} if _stream_cancelled(_cancel_ev)
                            else call_llm(thinking_prompt, stream=False))
                if "choices" in t_result:
                    pre_thinking = t_result["choices"][0]["message"]["content"].strip()
                else:
                    app.logger.warning("pre-thinking no choices: %s", str(t_result)[:200])
            except Exception as e:
                app.logger.warning("pre-thinking failed: %s", e)
            app.logger.info("pre-thinking len=%d", len(pre_thinking))

            yield _status('✍️ 正在生成回复...')
            # 流式同样走 _llm_send：失败可在建连阶段重试并切备用通道；已开始下行后再断流不进重试
            _usage_t0 = _usage_now()
            resp, conn_err = _llm_send(result["url"], result["headers"], result["payload"],
                                       stream=True, timeout=180)
            if conn_err is not None:
                yield f"data: {json.dumps({'error': conn_err['error'], 'error_code': conn_err.get('error_code', 'network')}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return
            resp.encoding = 'utf-8'
            _usage_first = None
            _usage_chunk = None

            for line in resp.iter_lines(decode_unicode=True):
                if _stream_cancelled(_cancel_ev):
                    # 用户点了停止：马上停止读上游，已流出的正文照常收尾（落库标 truncated）
                    cancelled = True
                    with _stream_lock:
                        _stream_stats["cancelled"] += 1
                    app.logger.info("request_id=%s session=%s 用户停止生成，已输出 %d 字",
                                    _sid, conversation_id, len(full_response))
                    break
                if line:
                    if line.startswith('data: '):
                        data_str = line[6:]
                        if data_str == '[DONE]':
                            break
                        try:
                            chunk = json.loads(data_str)
                            # 开了 include_usage 的供应商会在最后发一个 choices 为空、只带 usage 的块，
                            # 直接取 [0] 会 IndexError 把整条流打断，所以先兜住空列表再取用量
                            if chunk.get('usage'):
                                _usage_chunk = chunk['usage']
                            _choices = chunk.get('choices') or [{}]
                            delta = _choices[0].get('delta', {})
                            content = delta.get('content', '')
                            if not content:
                                continue
                            if _usage_first is None:
                                _usage_first = _usage_now() - _usage_t0

                            # 将新内容加入缓冲区
                            buffer += content
                            seen += content

                            # 预生成思考的发送决策：等累计输出足够判断模型是否自己输出 <thinking> 再定，
                            # 避免首 chunk 只是 '<think' 残 tag 就误发预生成、随后模型自己的思考漏进回复正文
                            if pre_thinking and not pre_thinking_sent and not model_thinking and not in_thinking:
                                stripped = seen.strip()
                                if stripped.startswith('<thinking>') or len(stripped) >= 12:
                                    pre_thinking_sent = True
                                    if not stripped.startswith('<thinking>'):
                                        yield f"data: {json.dumps({'type': 'thinking_status', 'data': {'message': '正在分析问题...'}}, ensure_ascii=False)}\n\n"
                                        for t_line in pre_thinking.split('\n'):
                                            t_line = t_line.strip()
                                            if t_line:
                                                yield f"data: {json.dumps({'type': 'thinking', 'data': {'content': t_line + '\n'}}, ensure_ascii=False)}\n\n"
                                                time.sleep(0.08)
                                        yield f"data: {json.dumps({'type': 'thinking_status', 'data': {'message': '分析完成，正在生成回复...'}}, ensure_ascii=False)}\n\n"
                                        # 思考已下发，把决策期间暂存的正文一次性flush，保证思考先于回复
                                        if pending_content:
                                            full_response += pending_content
                                            yield f"data: {json.dumps({'content': pending_content}, ensure_ascii=False)}\n\n"
                                            pending_content = ""

                            # ====== 缓冲式解析 <think> 标签 ======
                            if in_thinking:
                                model_thinking = True
                                end_tag = '</thinking>'
                                if end_tag in buffer:
                                    idx = buffer.index(end_tag)
                                    if idx > 0:
                                        thinking_text += buffer[:idx]
                                        yield f"data: {json.dumps({'type': 'thinking', 'data': {'content': buffer[:idx]}}, ensure_ascii=False)}\n\n"
                                    in_thinking = False
                                    yield f"data: {json.dumps({'type': 'thinking_status', 'data': {'message': '思考完成，正在生成回复...'}}, ensure_ascii=False)}\n\n"
                                    buffer = buffer[idx + len(end_tag):]
                                else:
                                    safe_len = len(buffer) - len(end_tag) + 1
                                    if safe_len > 0:
                                        safe = buffer[:safe_len]
                                        thinking_text += safe
                                        yield f"data: {json.dumps({'type': 'thinking', 'data': {'content': safe}}, ensure_ascii=False)}\n\n"
                                        buffer = buffer[safe_len:]
                                    continue

                            start_tag = '<thinking>'
                            # 即使预生成思考已发送，模型自己的 <thinking> 也必须收进思考面板，
                            # 否则分析内容会原样漏进回复正文（2026-09-24 实测）
                            if start_tag in buffer:
                                model_thinking = True
                                idx = buffer.index(start_tag)
                                if idx > 0:
                                    before = buffer[:idx]
                                    full_response += before
                                    yield f"data: {json.dumps({'content': before}, ensure_ascii=False)}\n\n"
                                in_thinking = True
                                yield f"data: {json.dumps({'type': 'thinking_status', 'data': {'message': '正在分析问题...'}}, ensure_ascii=False)}\n\n"
                                buffer = buffer[idx + len(start_tag):]
                                end_tag = '</thinking>'
                                if end_tag in buffer:
                                    end_idx = buffer.index(end_tag)
                                    if end_idx > 0:
                                        thinking_text += buffer[:end_idx]
                                        yield f"data: {json.dumps({'type': 'thinking', 'data': {'content': buffer[:end_idx]}}, ensure_ascii=False)}\n\n"
                                    in_thinking = False
                                    yield f"data: {json.dumps({'type': 'thinking_status', 'data': {'message': '思考完成，正在生成回复...'}}, ensure_ascii=False)}\n\n"
                                    buffer = buffer[end_idx + len(end_tag):]
                                else:
                                    safe_len = len(buffer) - len(end_tag) + 1
                                    if safe_len > 0:
                                        safe = buffer[:safe_len]
                                        thinking_text += safe
                                        yield f"data: {json.dumps({'type': 'thinking', 'data': {'content': safe}}, ensure_ascii=False)}\n\n"
                                        buffer = buffer[safe_len:]
                                    continue

                            # 正常输出（保留末尾可能的不完整标签，含跨 chunk 的 '</thinking>'）
                            if not in_thinking:
                                safe = buffer
                                held = False
                                for tag in ('<thinking>', '</thinking>'):
                                    for i in range(len(tag), 0, -1):
                                        if buffer.endswith(tag[:i]):
                                            safe = buffer[:-i]
                                            buffer = buffer[-i:]
                                            held = True
                                            break
                                    if held:
                                        break
                                if not held:
                                    buffer = ""
                                if safe:
                                    # 清除可能残留的 thinking 标签
                                    clean = re.sub(r'</?thinking>', '', safe)
                                    if pre_thinking and not pre_thinking_sent and not model_thinking:
                                        # 思考决策未定：暂存正文，避免回复先于思考跳出
                                        pending_content += clean
                                    else:
                                        full_response += clean
                                        yield f"data: {json.dumps({'content': clean}, ensure_ascii=False)}\n\n"

                        except json.JSONDecodeError:
                            continue

            # 流结束后处理剩余缓冲区
            if buffer:
                clean = re.sub(r'</?thinking>', '', buffer)
                if in_thinking:
                    thinking_text += clean
                    yield f"data: {json.dumps({'type': 'thinking', 'data': {'content': clean}}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'thinking_status', 'data': {'message': '思考完成，正在生成回复...'}}, ensure_ascii=False)}\n\n"
                else:
                    if pre_thinking and not pre_thinking_sent and not model_thinking:
                        pending_content += clean
                    else:
                        full_response += clean
                        if clean:
                            yield f"data: {json.dumps({'content': clean}, ensure_ascii=False)}\n\n"

            # 思考决策未定流就结束（极短回复）：补发暂存正文，避免内容丢失
            if pending_content:
                full_response += pending_content
                yield f"data: {json.dumps({'content': pending_content}, ensure_ascii=False)}\n\n"
                pending_content = ""

            # 兜底：如果模型输出到结束时仍在 thinking（没闭合），强制关闭
            if in_thinking and thinking_text:
                yield f"data: {json.dumps({'type': 'thinking_status', 'data': {'message': '思考完成，正在生成回复...'}}, ensure_ascii=False)}\n\n"

            # 保存上下文（去除 thinking 标签）——先算出来，中断时才能把「已生成的那一半」如实存下
            clean_response = re.sub(r'<thinking>.*?</thinking>', '', full_response, flags=re.DOTALL).strip()
            # 后处理：清理违规排版符号
            clean_response = clean_llm_output(clean_response)

            # 记账：中断也是真实消耗，照样记；首字耗时含建连，才是用户体感的等待
            _note_llm_call(skill or "", _usage_now() - _usage_t0, msgs, clean_response,
                           _usage_chunk, _usage_first)

            # 中断时不再从半截正文里猜地点/预订入口：正则会在断句处误抓，渲染出对不上的卡片
            if not cancelled:
                # 提取地点信息并发送（附带目的地城市，供前端查询美团实时价格）
                locations = _extract_locations_from_text(full_response)
                if locations:
                    for _loc in locations:
                        _loc["city"] = destination or ""
                    yield f"data: {json.dumps({'type': 'locations', 'data': locations}, ensure_ascii=False)}\n\n"

                # 发送可预订入口（门票购买/酒店预订），前端渲染操作按钮
                if bks:
                    yield f"data: {json.dumps({'type': 'bookings', 'data': bks}, ensure_ascii=False)}\n\n"

            if cancelled:
                yield f"data: {json.dumps({'type': 'stopped', 'data': {'chars': len(clean_response), 'message': '已停止生成，以上内容可能不完整'}}, ensure_ascii=False)}\n\n"

            # 发送 [DONE] 标记
            yield f"data: [DONE]\n\n"

            # 发送 done 事件（兼容旧前端）
            _has_thinking = bool(thinking_text) or pre_thinking_sent
            yield f"data: {json.dumps({'done': True, 'has_thinking': _has_thinking, 'cancelled': cancelled}, ensure_ascii=False)}\n\n"

            if clean_response or not cancelled:
                history = _load_conversation_history(conversation_id)
                append_history(conversation_id, history, user_message, clean_response)
                _conv_cache_put(conversation_id, history)
                # 历史已随 [DONE] 之后的这段落库；失败以前是静默 pass，现在降级但留痕（app.log + /api/admin/ops）
                _db_call("save_message:user", db.save_message, conversation_id, 'user', user_message)
                # 中断的答复带 truncated 落库：重新打开会话时前端要标出「未完整」
                _db_call("save_message:agent", db.save_message, conversation_id, 'agent', clean_response,
                         message_id=_answer_id, truncated=cancelled,
                         sources_json=(json.dumps(_rag_sources, ensure_ascii=False)
                                       if _rag_sources else None))

            if not cancelled:
                # ====== 反幻觉：流式输出后事实校验 ======
                # 以前整块是 try/except: pass——校验坏掉了完全没人知道。现在降级但留痕。
                try:
                    _plugin_ctx = pctx if pctx else get_plugin_data(user_message, None)
                except Exception as _pctx_err:  # noqa: BLE001
                    app.logger.warning("流式校验前获取插件数据失败，本轮按「无插件上下文」校验: %s",
                                       _pctx_err)
                    _plugin_ctx = ""
                validation = _run_validation(clean_response, _plugin_ctx, conversation_id)
                if validation is not None and validation.has_issues:
                    app.logger.warning(
                        "request_id=%s session=%s stream_hallucination_check=%s",
                        _request_id(), conversation_id, validation.to_log_dict(),
                    )
                    # 通过 SSE 发送校验警告（前端可选展示）
                    yield f"data: {json.dumps({'type': 'validation', 'data': validation.to_log_dict()}, ensure_ascii=False)}\n\n"
                # ====== 流式校验完毕 ======

                # ====== 长期记忆：流式答复已给出，后台沉淀本轮偏好 ======
                try:
                    _memory_remember(_chat_uid, user_message, conversation_id)
                except Exception:
                    pass
                # ====== 长期记忆写入完毕 ======
            else:
                app.logger.info("request_id=%s session=%s 本轮已中断，跳过事实校验与记忆沉淀",
                                _sid, conversation_id)

        except GeneratorExit:
            # 客户端真的断连（关页面/切会话）：werkzeug 关闭本生成器。上游流交给 finally 关，
            # 已流出的内容不强行落库（不知道它断在哪一步），只留计数。
            with _stream_lock:
                _stream_stats["client_disconnected"] += 1
            app.logger.info("request_id=%s session=%s 客户端断连，流式生成已释放",
                            _sid, conversation_id)
            raise
        except requests.exceptions.Timeout:
            # 断网/超时：直接返回网络异常提示，不生成回答
            yield f"data: {json.dumps({'error': 'AI 服务响应超时，可能是当前网络较慢，请稍后重试。', 'error_code': 'timeout'}, ensure_ascii=False)}\n\n"
        except requests.exceptions.ConnectionError:
            # 断网：直接返回网络异常提示，不生成回答
            yield f"data: {json.dumps({'error': '连接 AI 服务失败，请检查网络连接后重试。', 'error_code': 'network'}, ensure_ascii=False)}\n\n"
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else 0
            yield f"data: {json.dumps({'error': _llm_http_message(status), 'error_code': _llm_http_code(status)}, ensure_ascii=False)}\n\n"
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[Stream Error]\n{tb}")
            _msg, _code = friendly_llm_error(e)
            yield f"data: {json.dumps({'error': _msg, 'error_code': _code}, ensure_ascii=False)}\n\n"
        finally:
            # 上游流必须关：只靠 break 的话 requests 还挂着这条连接，客户端早就走了，
            # waitress 那 8 条线程会被「没人看的回答」占住。取消登记也一并清掉，避免注册表只增不减。
            if resp is not None:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
            _stream_unregister(_sid)

    return Response(stream_with_context(generate()), mimetype='text/event-stream; charset=utf-8',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/chat/stop', methods=['POST'])
def chat_stop():
    """停止正在进行的流式生成：置位该路的取消标志，由服务端自己停下并收尾落库。

    只回 200 不报 404：前端点停止时这一路很可能刚刚自然结束，把「没得停」当成错误
    会让界面弹出一个假的失败提示。
    """
    payload = get_json_payload({}) or {}
    sid = str(payload.get('request_id') or '')
    found = _stream_stop(sid) if sid else False
    if not found:
        app.logger.info("停止请求未命中活跃流（可能已自然结束）: stream_id=%s", sid[:36])
    return jsonify({"success": True, "found": found, "streams": stream_stats()})


@app.route('/api/plan', methods=['POST'])
def plan():
    """场景智能型行程规划接口 — 画像驱动、多偏好平衡、消费转化、异常兜底"""
    if not API_CONFIG["api_key"]:
        return jsonify({"error": "请先配置API Key"}), 401

    data = request.json or {}

    # 1) 参数完整性校验与澄清
    validation = validate_plan_params(data)
    if not validation["valid"]:
        return jsonify({
            "success": True,
            "reply": validation["clarification"],
            "need_clarification": True,
            "missing": validation["missing"],
        }), 200

    # 2) 结构化骨架预处理（AI + 规则混合优势）
    try:
        skeleton = build_plan_skeleton(data)
        data["plan_skeleton"] = skeleton
    except Exception as e:
        print('[plan] 骨架生成失败:', e)
        skeleton = build_plan_fallback(str(e), data)
        data["plan_skeleton"] = skeleton

    # 2.5) 拉取真实数据源（美团景点/酒店/门票 + 高德天气），供模型优先采用
    try:
        data["real_data_text"] = _fetch_plan_real_data(data)
    except Exception as e:
        print('[plan] 真实数据获取失败:', e)
        data["real_data_text"] = ""

    stream = data.get('stream', True)
    messages = build_scene_plan_messages(data)

    if stream:
        return _stream_llm_response(messages, skill_name="plan")

    try:
        result = call_llm(messages, skill_name="plan")
    except Exception as e:
        print('[plan] LLM 调用异常:', e)
        fallback = build_plan_fallback(f"LLM 调用异常: {e}", data)
        return jsonify({"success": True, "reply": fallback["minimal_plan"]["note"], "fallback": True}), 200

    if "error" in result:
        fallback = build_plan_fallback(result["error"], data)
        return jsonify({"success": True, "reply": fallback["minimal_plan"]["note"], "fallback": True}), 200

    return jsonify({
        "success": True,
        "reply": clean_llm_output(result["choices"][0]["message"]["content"]),
        "plan_skeleton": skeleton,
    })

@app.route('/api/marketing', methods=['POST'])
def marketing():
    """创收增效型营销生成接口 — 内容、渠道、转化、合规闭环"""
    if not API_CONFIG["api_key"]:
        return jsonify({"error": "请先配置API Key"}), 401

    data = request.json or {}
    stream = data.get('stream', True)
    if data.get('type') == 'blog':
        messages = build_blog_messages(data)
    else:
        messages = build_revenue_marketing_messages(data)

    if stream:
        return _stream_llm_response(messages, usage_label="marketing")

    result = call_llm(messages)
    if "error" in result:
        return jsonify({"success": False, "error": result["error"]}), 500
    return jsonify({"success": True, "reply": clean_llm_output(result["choices"][0]["message"]["content"])})


# ===== 文创数字名片（实物印码即媒介：每个文创一个可分享 H5 名片页） =====
CARDS_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', 'cards.json')
_CARDS_MAX_ITEMS = 500


def _load_cards() -> dict:
    if os.path.exists(CARDS_FILE):
        try:
            with open(CARDS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _save_cards(cards: dict):
    os.makedirs(os.path.dirname(CARDS_FILE), exist_ok=True)
    with open(CARDS_FILE, 'w', encoding='utf-8') as f:
        json.dump(cards, f, ensure_ascii=False, indent=2)


def _clip(value, limit):
    """名片字段统一截断，防恶意塞爆存储"""
    if not isinstance(value, str):
        value = str(value) if value is not None else ''
    return value[:limit]


def _warm_card_image(raw_img: str):
    """名片主图若指向灵感卡懒加载地址，先在后台把 AI 生图任务排进线程池。

    数字名片要被顾客扫码打开，不能等 40 多秒；由服务端提前触发生成，
    顾客扫码时通常已命中磁盘缓存（接口会直接返回 JPEG）。
    """
    if not raw_img or not raw_img.startswith('/api/inspire/image'):
        return
    try:
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(raw_img).query)
        topic = (query.get('t') or [''])[0].strip()
        cat = (query.get('c') or ['文创IP'])[0].strip() or '文创IP'
        city = (query.get('y') or ['杭州'])[0].strip() or '杭州'
        if topic:
            social_data.ensure_inspire_image(topic, cat, city)
    except Exception as exc:
        print('[cards] 名片配图预热失败:', exc)


@app.route('/api/cards', methods=['POST'])
def create_card():
    """保存一张文创数字名片，返回短 id 与访问路径"""
    data = request.get_json(force=True, silent=True) or {}
    name = _clip(data.get('name'), 80).strip()
    if not name:
        return jsonify({"success": False, "error": "缺少产品名称"}), 400

    cards = _load_cards()
    if len(cards) >= _CARDS_MAX_ITEMS:
        return jsonify({"success": False, "error": "名片库已满，请联系管理员清理"}), 507

    img = _clip(data.get('img'), 600)
    _warm_card_image(img)

    cid = 'c' + uuid.uuid4().hex[:10]
    cards[cid] = {
        "name": name,
        "brand": _clip(data.get('brand'), 60),
        "cat": _clip(data.get('cat'), 20),
        "img": img,
        "img_fallback": _clip(data.get('img_fallback'), 600),
        "price": _clip(data.get('price'), 30),
        "sales": _clip(data.get('sales'), 30),
        "tags": [_clip(t, 20) for t in (data.get('tags') or [])[:8]],
        "highlights": [_clip(t, 60) for t in (data.get('highlights') or [])[:6]],
        "desc": _clip(data.get('desc'), 2000),
        "design": _clip(data.get('design'), 2000),
        "culture": _clip(data.get('culture'), 3000),
        "audience": _clip(data.get('audience'), 300),
        "story": _clip(data.get('story'), 4000),
        "poster": _clip(data.get('poster'), 600),
        "booking_url": _clip(data.get('booking_url'), 600),
        "source_url": _clip(data.get('source_url'), 600),
        "source": _clip(data.get('source'), 20) or "inspire",
        "user_id": _resolve_uid(data.get('user_id')),
        "created_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    try:
        _save_cards(cards)
    except Exception as e:
        app.logger.error(f"保存名片失败: {e}")
        return jsonify({"success": False, "error": "保存失败"}), 500
    return jsonify({"success": True, "id": cid, "url": f"/static/digital-card.html?id={cid}"})


@app.route('/api/cards/<cid>', methods=['GET'])
def get_card(cid: str):
    """读取名片数据；id 只含 [a-z0-9]，校验不合法直接 404"""
    if not re.fullmatch(r'c[0-9a-f]{10}', cid or ''):
        return jsonify({"success": False, "error": "not found"}), 404
    card = _load_cards().get(cid)
    if not card:
        return jsonify({"success": False, "error": "not found"}), 404
    return jsonify({"success": True, "card": card})


@app.route('/digital-card.html')
def digital_card_page():
    return send_from_directory(app.static_folder, 'digital-card.html')


@app.route('/api/network-info', methods=['GET'])
def network_info():
    """返回本机局域网 IPv4 列表，供数字名片把 localhost 二维码换成局域网地址"""
    import socket
    ips = []
    try:
        # 方法1：UDP 探测出网网卡 IP（不实际发包，无需联网成功）
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith('127.'):
                ips.append(ip)
        finally:
            s.close()
    except Exception:
        pass
    try:
        # 方法2：主机名解析兜底（某些机器方法1拿不到）
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip and not ip.startswith('127.') and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return jsonify({"success": True, "lan_ips": ips})


@app.route('/api/creative', methods=['POST'])
def creative_empower():
    """文创产品创意赋能独立接口 — 文化元素、IP合规、产品梯度、海报联动"""
    if not API_CONFIG["api_key"]:
        return jsonify({"error": "请先配置API Key"}), 401

    data = request.json or {}
    destination = data.get("destination") or data.get("city") or ""

    # 1) 参数校验：文创必须有目的地或文化主题
    if not destination and not data.get("culture_theme"):
        return jsonify({
            "success": True,
            "reply": (
                "为了设计有在地文化依据的文创产品，我需要先确认：\n"
                "1. 目的地或景区名称（如：西湖、故宫、敦煌）\n"
                "2. 目标客群（如：亲子、情侣、Z世代、商务礼品）\n"
                "3. 主要销售渠道（可选）：景区门店 / 线上电商 / 酒店 / 研学\n\n"
                "请回复目的地和客群，我会为您提炼约 200 字的核心介绍（定位、文化内核、设计亮点、传播语），并可配套海报提示词。"
            ),
            "need_clarification": True,
            "missing": ["目的地或文化主题"],
        }), 200

    # 2) 使用独立的文创产品 Prompt（不再复用营销文案 Prompt）
    messages = build_creative_product_messages(data)
    stream = data.get('stream', True)

    # 如果需要联动生成海报，则使用非流式返回以便携带 poster_url
    if data.get("generate_poster"):
        stream = False

    if stream:
        return _stream_llm_response(messages, usage_label="creative_product")

    try:
        result = call_llm(messages)
    except Exception as e:
        print('[creative] LLM 调用异常:', e)
        return jsonify({"success": False, "error": f"生成失败: {e}"}), 500

    if "error" in result:
        return jsonify({"success": False, "error": result["error"]}), 500

    reply = clean_llm_output(result["choices"][0]["message"]["content"])

    # 3) 提取海报提示词，如前端请求则联动生成海报
    poster_prompt = extract_creative_poster_prompt(reply)
    poster_url = ""
    if poster_prompt and data.get("generate_poster") and ARK_CONFIG.get("api_key"):
        # 锚定主题：把目的地/文化主题显式拼入生图提示词，保证海报与正文描述的产品一致
        anchor_theme = destination or data.get("culture_theme") or "文旅"
        # 海报只承载"噱头"：产品名 + 一句传播语，不照搬正文卖点/价格/段落（与文创数字名片一致）
        anchored_prompt = ("文创产品概念海报，主题：%s。%s "
                           "版式硬性要求：画面上只允许出现产品名与一句不超过 20 字的传播语（噱头 slogan）作为大字标题，"
                           "不得排布正文段落、卖点清单、价格、二维码或多余文字。") % (anchor_theme, poster_prompt)
        success, img_result = _generate_ark_image(anchored_prompt)
        if success:
            poster_url = img_result
        else:
            print('[creative] 海报生成联动失败:', img_result)

    response = {"success": True, "reply": reply}
    if poster_prompt:
        response["poster_prompt"] = poster_prompt
    if poster_url:
        response["poster_url"] = poster_url
    return jsonify(response)


@app.route('/api/social/hotspots', methods=['GET'])
def social_hotspots():
    """通过官方/授权渠道获取抖音、小红书文旅热点数据。"""
    keyword = request.args.get('keyword', '文旅')
    city = request.args.get('city', '杭州')
    return jsonify(social_data.get_social_hotspots(keyword=keyword, city=city))


@app.route('/api/market/hot-inspirations', methods=['GET', 'POST'])
def market_hot_inspirations():
    """聚合市场热点，实时更新社区攻略与文创产品灵感。"""
    payload = request.json or {} if request.method == 'POST' else {}
    keyword = (payload.get('keyword') or request.args.get('keyword') or '文旅').strip()
    city = (payload.get('city') or request.args.get('city') or '杭州').strip()
    limit = min(int(payload.get('limit') or request.args.get('limit') or 10), 12)
    persist = str(payload.get('persist', request.args.get('persist', '1'))).lower() not in ('0', 'false', 'no')
    refresh_token = (payload.get('refresh_token') or request.args.get('refresh_token') or '').strip()

    data = social_data.get_market_hot_inspirations(keyword=keyword, city=city, limit=limit, refresh_token=refresh_token)
    saved_notes = []
    if persist:
        for note in data.get('xhs_notes', []):
            try:
                saved_notes.append(db.upsert_xhs_note(note))
            except Exception as exc:
                print("[market_hot_inspirations] upsert note failed:", exc)
    data["saved_notes"] = saved_notes
    data["persisted"] = persist
    return jsonify(data)


# ╔══════════════════════════════════════════════════════════════╗
# ║  🎨  灵感卡片 AI 配图（懒生成 + 磁盘缓存）                      ║
# ╚══════════════════════════════════════════════════════════════╝

_INSPIRE_PLACEHOLDER_THEME = {
    "国潮": ("#b91c1c", "#fca5a5", "#fef2f2"),
    "非遗": ("#7c2d12", "#fdba74", "#fff7ed"),
    "博物馆": ("#0f766e", "#5eead4", "#ecfeff"),
    "文创IP": ("#b45309", "#facc15", "#fffbeb"),
    "节庆": ("#c2410c", "#fdba74", "#fff7ed"),
}


@app.route('/api/inspire/image', methods=['GET'])
def inspire_image():
    """灵感卡片配图：按主题懒生成 AI 概念图并落盘缓存。

    - 已生成：直接返回 JPEG，并带 `X-Inspire-Image: ready`
    - 未生成：`?fast=1` 时立刻返回占位图 + `X-Inspire-Image: pending`（前端稍后重试），
      否则阻塞等待生成完成（最长 150 秒），保证 <img> 直接引用也能拿到图。
    """
    topic = (request.args.get('t') or '').strip()
    cat = (request.args.get('c') or '文创IP').strip()
    city = (request.args.get('y') or '杭州').strip()
    fast = str(request.args.get('fast') or '').lower() in ('1', 'true', 'yes')

    if topic:
        slug = social_data.inspire_image_slug(topic, cat, city, cat)
        ready = social_data.inspire_image_ready(slug)
        if not ready:
            social_data.ensure_inspire_image(topic, cat, city)
            if not fast:
                ready = social_data.wait_inspire_image(slug, timeout=150)
        if ready:
            resp = make_response(send_file(social_data.inspire_image_path(slug), mimetype='image/jpeg'))
            resp.headers['Cache-Control'] = 'public, max-age=604800'
            resp.headers['X-Inspire-Image'] = 'ready'
            return resp

    primary, accent, bg = _INSPIRE_PLACEHOLDER_THEME.get(cat, ("#8b3a2b", "#d7a94c", "#f6ead2"))
    label = (cat or '文创')[:6]
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300" width="400" height="300">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s"/></linearGradient></defs>
<rect width="400" height="300" fill="%s"/>
<circle cx="330" cy="60" r="90" fill="%s" opacity=".18"/>
<circle cx="70" cy="250" r="110" fill="%s" opacity=".14"/>
<text x="200" y="146" text-anchor="middle" font-family="Microsoft YaHei,Noto Sans SC,sans-serif"
 font-size="34" font-weight="800" fill="%s" opacity=".85">%s</text>
<text x="200" y="190" text-anchor="middle" font-family="Microsoft YaHei,Noto Sans SC,sans-serif"
 font-size="17" fill="#ffffff" opacity=".9">AI 概念图生成中…</text>
</svg>""" % (accent, primary, bg, primary, accent, primary, label)
    resp = make_response(svg)
    resp.mimetype = 'image/svg+xml'
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['X-Inspire-Image'] = 'pending'
    return resp


# ╔══════════════════════════════════════════════════════════════╗
# ║  🖼️  首页灵感橱窗：每天联网生成，内容当日缓存                    ║
# ╚══════════════════════════════════════════════════════════════╝

_PORTAL_SHOWCASE_CLS = {"scenic", "culture", "food", "night", "hotel", "market"}
_PORTAL_SHOWCASE_PAGES = {"plan", "marketing", "chat"}
_PORTAL_SHOWCASE_IMAGE = {
    "scenic": "/static/images/hot/scenic.png",
    "culture": "/static/images/hot/culture.png",
    "food": "/static/images/hot/food.png",
    "night": "/static/images/hot/night.png",
    "hotel": "/static/images/hot/hotel.png",
    "market": "/static/images/hot/market.png",
}
_PORTAL_SHOWCASE_FALLBACK = [
    {"tag": "热门景点", "icon": "🏞️", "cls": "scenic", "title": "西湖宋韵线", "sub": "晨游、茶事、非遗讲解", "desc": "以湖区漫游、茶事体验、宋韵文化讲解为核心，适合第一次到访杭州的家庭、情侣与研学客群。",
     "reasons": ["清晨与傍晚人少、光线好，避开白天团队客流。", "湖游加茶事与非遗讲解，一条线看全杭州底色。", "适合首访与亲子，约两小时步行强度低。"], "actionText": "生成这条路线", "actionPage": "plan"},
    {"tag": "文创作品", "icon": "🏺", "cls": "culture", "title": "良渚玉鸟礼盒", "sub": "博物馆联名伴手礼", "desc": "以良渚文化符号为灵感，适合做博物馆文创、城市礼物、研学纪念品和景区联名产品展示。",
     "reasons": ["观展前后是伴手礼需求最集中的时段。", "玉鸟纹样落在器物上，每件都有可讲的文化出处。", "研学纪念与城市礼物都合适，便于组合陈列。"], "actionText": "生成文创营销文案", "actionPage": "marketing"},
    {"tag": "热门餐食", "icon": "🍜", "cls": "food", "title": "杭帮风味宴", "sub": "东坡肉、龙井虾仁", "desc": "围绕东坡肉、龙井虾仁、时令小点等菜品组合，适合与路线、酒店、夜游套餐打包推荐。",
     "reasons": ["午晚两个饭点能自然接进行程，不用额外绕路。", "东坡肉配龙井虾仁与时令小点，一桌吃出本帮味。", "团餐、亲子、情侣都能组桌，可与路线打包。"], "actionText": "生成餐食推广内容", "actionPage": "marketing"},
    {"tag": "夜游活动", "icon": "🌉", "cls": "night", "title": "运河灯影游", "sub": "夜航、市集、沉浸演艺", "desc": "以夜航、灯光、市集和沉浸式演艺为核心，适合提升游客夜间停留时长和二次消费。",
     "reasons": ["入夜后灯光与演艺集中，是白天之外的增量时段。", "夜航、市集、沉浸演艺串成一条水岸动线。", "适合情侣与年轻客群，约三小时不占白天。"], "actionText": "规划夜游路线", "actionPage": "plan"},
    {"tag": "精选住宿", "icon": "🏨", "cls": "hotel", "title": "江南庭院民宿", "sub": "亲子与情侣客群优选", "desc": "适合亲子、情侣和轻奢度假客群，可与深度游、茶文化体验、摄影路线组合售卖。",
     "reasons": ["周末与节假日房态紧，宜提前确认再排行程。", "庭院式建筑有地方特色，不是连锁酒店的模板房。", "亲子与轻奢度假优先，可接深度游与摄影路线。"], "actionText": "咨询住宿推荐", "actionPage": "chat"},
    {"tag": "非遗市集", "icon": "🧧", "cls": "market", "title": "宋式雅集", "sub": "手作体验与国风打卡", "desc": "融合手作体验、国风服饰、在地小吃和互动演艺，适合做周末活动和社交平台传播。",
     "reasons": ["周末与节庆场次密集，白天夜场都能承接客流。", "手作、国风服饰、在地小吃与演艺都可参与。", "适合活动策划与短视频种草，素材密度高。"], "actionText": "生成活动策划", "actionPage": "marketing"},
]
# 当日缓存：date=已生成日期，data=结果，last_attempt=上次生成尝试时间戳（失败重试限流）
_portal_showcase_cache = {"date": "", "data": None, "last_attempt": 0.0}


def _portal_season_label(now):
    month = now.month
    if month in (3, 4, 5):
        return "春季"
    if month in (6, 7, 8):
        return "夏季"
    if month in (9, 10):
        return "初秋"
    if month == 11:
        return "深秋"
    return "冬季"


def _portal_fetch_online_context(city):
    """尽力而为地聚合联网上下文：高德实时天气 + 抖音/小红书文旅热点标题。"""
    lines = []
    try:
        weather = registry.get("amap_weather")
        if weather and weather.is_configured():
            w = weather.execute({"city": city, "extensions": "base"})
            text = weather.format_for_llm(w)
            if text:
                lines.append("【实时天气】\n" + text)
    except Exception as exc:
        print("[hot_showcase] weather context failed:", exc)
    try:
        hot = social_data.get_social_hotspots(keyword="文旅", city=city)
        titles = []
        for item in hot.get("items", [])[:8]:
            title = str(item.get("title") or item.get("keyword") or "").strip()
            if title:
                titles.append("- " + title)
        if titles:
            lines.append("【社媒文旅热点】\n" + "\n".join(titles))
    except Exception as exc:
        print("[hot_showcase] hotspot context failed:", exc)
    return lines


def _portal_clean_reasons(raw_reasons, desc):
    """推荐原因清洗：去空、去重、去掉照抄 desc 的那条，最多留 3 条。

    模型可能给字符串、给 5 条或给重复内容；宁缺不凑——缺的那条不给，
    也绝不拿平台话术或 desc 复制来补数。
    """
    if isinstance(raw_reasons, str):
        raw_reasons = [raw_reasons]
    if not isinstance(raw_reasons, list):
        return []
    desc_text = str(desc or "").strip()
    out = []
    for item in raw_reasons:
        text = str(item or "").strip()[:60]
        if not text or text == desc_text or text in out:
            continue
        out.append(text)
        if len(out) >= 3:
            break
    return out


def _portal_generate_cards(city):
    """调用大模型 + 联网上下文生成当日 6 张灵感卡片；失败返回 None。"""
    now = datetime.now()
    weekday_cn = "一二三四五六日"[now.weekday()]
    context = _portal_fetch_online_context(city)
    context_text = "\n\n".join(context) if context else \
        f"[联网数据暂不可用，请基于{city}的经典文旅资源与当季特点生成]"
    user_prompt = (
        f"请为文旅平台首页的「热门灵感橱窗」生成今天（{now.strftime('%Y年%m月%d日')} 星期{weekday_cn}，"
        f"{_portal_season_label(now)}）的 6 张推荐卡片。\n\n"
        "要求：\n"
        "1. 覆盖 6 个类别：热门景点、文创作品、热门餐食、夜游活动、精选住宿、非遗市集，每类一张。\n"
        f"2. 优先结合下方联网数据中的实时热点与当季特点，因地制宜（城市：{city}），避免空泛。\n"
        "3. title 不超过 8 个字；sub 不超过 14 个字（用顿号分隔 2~3 个关键词）；desc 为 40~60 字推荐语。\n"
        "4. reasons 是「推荐原因」数组，恰好 3 条、每条不超过 30 字，三条分别讲三个维度：\n"
        "   ①时机——为什么是现在去（当季物候、天气、节庆、实时热点）；\n"
        "   ②体验——这一处本身玩什么、有什么独特或稀缺之处；\n"
        "   ③客群与实用——适合谁、怎么玩省力（时长、动线、组合方式、价位区间，不编具体价格）。\n"
        "   三条只能写这一处资源本身，不要写成平台功能、内容更新频率或操作引导，彼此不得重复，\n"
        "   也不要照抄 desc 的句子。\n"
        '5. icon 为单个 emoji；cls 从 ["scenic","culture","food","night","hotel","market"] 中按类别选择。\n'
        '6. actionPage 从 ["plan","marketing","chat"] 中选择：景点/夜游用 plan，文创/餐食/市集用 marketing，住宿用 chat。\n\n'
        "联网参考数据：\n" + context_text + "\n\n"
        "只输出 JSON 数组，格式：\n"
        '[{"tag":"热门景点","icon":"🏞️","cls":"scenic","title":"...","sub":"...",'
        '"desc":"...","reasons":["时机…","体验…","客群与实用…"],'
        '"actionText":"生成这条路线","actionPage":"plan"}, ...]'
    )
    resp = call_llm([
        {"role": "system", "content": "你是资深文旅内容策划专家。输出必须是合法 JSON 数组，不要输出任何解释文字或 Markdown 代码块。"},
        {"role": "user", "content": user_prompt},
    ])
    content = ""
    if isinstance(resp, dict) and resp.get("choices"):
        try:
            content = resp["choices"][0]["message"]["content"] or ""
        except Exception:
            content = ""
    if not content:
        return None
    start = content.find("[")
    end = content.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        raw_cards = json.loads(content[start:end + 1])
    except Exception:
        return None
    if not isinstance(raw_cards, list):
        return None
    cards = []
    seen_cls = set()
    for raw in raw_cards:
        if not isinstance(raw, dict):
            continue
        cls = str(raw.get("cls") or "").strip()
        if cls not in _PORTAL_SHOWCASE_CLS or cls in seen_cls:
            continue
        title = str(raw.get("title") or "").strip()[:16]
        if not title:
            continue
        seen_cls.add(cls)
        desc = str(raw.get("desc") or "").strip()[:120]
        cards.append({
            "id": f"{cls}-{now.strftime('%Y%m%d')}-{len(cards)}",
            "tag": str(raw.get("tag") or "今日推荐").strip()[:10],
            "icon": (str(raw.get("icon") or "🏞️").strip()[:4]) or "🏞️",
            "cls": cls,
            "title": title,
            "sub": str(raw.get("sub") or "").strip()[:14],
            "desc": desc,
            "reasons": _portal_clean_reasons(raw.get("reasons"), desc),
            "actionText": str(raw.get("actionText") or "去看看").strip()[:12],
            "actionPage": raw.get("actionPage") if raw.get("actionPage") in _PORTAL_SHOWCASE_PAGES else "plan",
        })
        if len(cards) >= 6:
            break
    return cards or None


def _portal_fallback_cards():
    """离线兜底：按日期轮换默认卡片，保证内容每天仍有变化。"""
    now = datetime.now()
    offset = now.timetuple().tm_yday % len(_PORTAL_SHOWCASE_FALLBACK)
    rotated = _PORTAL_SHOWCASE_FALLBACK[offset:] + _PORTAL_SHOWCASE_FALLBACK[:offset]
    cards = []
    for idx, card in enumerate(rotated):
        card = dict(card)
        card["id"] = f"{card['cls']}-{now.strftime('%Y%m%d')}-{idx}"
        cards.append(card)
    return cards


@app.route('/api/portal/hot-showcase', methods=['GET'])
def portal_hot_showcase():
    """首页「热门灵感橱窗」：每天联网生成一次，当日所有访客共享同一份内容。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if _portal_showcase_cache["date"] == today and _portal_showcase_cache["data"]:
        return jsonify(_portal_showcase_cache["data"])

    # 生成失败时限流：10 分钟内不重复调用大模型，直接返回兜底
    now_ts = datetime.now().timestamp()
    if now_ts - _portal_showcase_cache["last_attempt"] < 600:
        data = {
            "success": True, "date": today, "mode": "daily_fallback",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "cards": _portal_fallback_cards(),
        }
        return jsonify(data)
    _portal_showcase_cache["last_attempt"] = now_ts

    city = (request.args.get('city') or '杭州').strip() or '杭州'
    cards = _portal_generate_cards(city)
    mode = "ai_online"
    if not cards:
        print("[hot_showcase] AI 生成失败，使用按日轮换的兜底卡片")
        cards = _portal_fallback_cards()
        mode = "daily_fallback"
    else:
        # 只有真正联网生成成功的结果才做当日缓存
        data = {
            "success": True, "date": today, "city": city, "mode": mode,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "cards": cards,
        }
        _portal_showcase_cache["date"] = today
        _portal_showcase_cache["data"] = data
        return jsonify(data)
    data = {
        "success": True, "date": today, "city": city, "mode": mode,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cards": cards,
    }
    return jsonify(data)


def _ops_hot_action(note: dict) -> str:
    """按热点内容特征映射到最合适的落地功能：路线类→行程规划，风景/目的地类→推广博客，其余→营销内容。"""
    blob = " ".join(str(note.get(k) or "") for k in ("title", "theme", "summary", "crowd"))
    if any(k in blob for k in ("路线", "行程", "攻略", "一日游", "半日", "Citywalk", "citywalk", "漫步", "骑行", "串联")):
        return "plan"
    if any(k in blob for k in ("文创", "非遗", "市集", "伴手礼", "礼物", "美食", "餐", "小吃", "宴",
                               "夜游", "夜航", "灯", "演艺", "演出")):
        return "marketing"
    return "blog"


@app.route('/api/ops/dashboard', methods=['GET'])
def ops_dashboard():
    """首页今日运营看板：聚合真实业务数据、授权热点、灵感库和票务产品。"""
    city = request.args.get('city', '杭州').strip() or '杭州'
    keyword = request.args.get('keyword', '文旅').strip() or '文旅'
    limit = min(int(request.args.get('limit', 3)), 6)

    stats = db.get_stats()
    hot_data = social_data.get_market_hot_inspirations(
        keyword=keyword,
        city=city,
        limit=max(limit, 3),
        refresh_token=datetime.now().strftime("%Y%m%d%H")
    )
    notes = db.list_xhs_notes(city=city, limit=30)
    products = _get_ticket_products(city) or _get_ticket_products()

    trends = []
    for idx, note in enumerate((hot_data.get('xhs_notes') or [])[:limit]):
        score = int(float(note.get('score') or max(70, 96 - idx * 7)))
        trends.append({
            "id": note.get("note_id") or f"trend-{idx}",
            "type": "trend",
            "rank": idx + 1,
            "title": note.get("title") or "文旅热点",
            "meta": " / ".join([v for v in [note.get("crowd"), note.get("theme"), note.get("duration_suggestion")] if v]) or city,
            "score": score,
            "summary": note.get("summary") or "",
            "source": note.get("source_type") or "market_hot",
            "url": note.get("source_url") or "",
            "highlights": note.get("highlights") or [],
            "pitfalls": note.get("pitfalls") or [],
            "destination": str(note.get("destination") or note.get("city") or city).strip() or city,
            "action": _ops_hot_action(note),
            "fill": {
                "destination": str(note.get("destination") or note.get("city") or city).strip() or city,
                "extra": note.get("title") or "",
            },
        })

    if not trends:
        for idx, note in enumerate(notes[:limit]):
            score = int(float(note.get('score') or max(70, 92 - idx * 6)))
            trends.append({
                "id": note.get("note_id") or f"note-{idx}",
                "type": "trend",
                "rank": idx + 1,
                "title": note.get("title") or "社区灵感",
                "meta": " / ".join([v for v in [note.get("crowd"), note.get("theme"), note.get("duration_suggestion")] if v]) or city,
                "score": score,
                "summary": note.get("summary") or "",
                "source": note.get("source_type") or "xhs_notes",
                "url": note.get("source_url") or "",
                "highlights": note.get("highlights") or [],
                "pitfalls": note.get("pitfalls") or [],
                "destination": str(note.get("destination") or note.get("city") or city).strip() or city,
                "action": _ops_hot_action(note),
                "fill": {
                    "destination": str(note.get("destination") or note.get("city") or city).strip() or city,
                    "extra": note.get("title") or "",
                },
            })

    product_count = len(products)
    avg_price = round(sum(float(p.get('price') or 0) for p in products[:20]) / max(1, min(len(products), 20)))
    content_opportunity = len(notes) + len(hot_data.get('xhs_notes') or []) + len(hot_data.get('creative_products') or [])
    destination_heat = min(99, max(65, 72 + len(trends) * 6 + min(12, product_count // 3)))
    conversion_rate = min(68, max(18, 24 + int(stats.get('total_itineraries', 0)) * 2 + min(12, product_count // 4)))

    kpis = [
        {
            "id": "destination_heat",
            "label": "目的地热度",
            "value": str(destination_heat),
            "trend": f"来自 {len(trends)} 条热点",
            "detail": f"综合授权热点、社区灵感、票务产品数量计算。当前城市：{city}。",
            "source": "market_hot + xhs_notes + tickets",
        },
        {
            "id": "conversion_rate",
            "label": "咨询转化",
            "value": f"{conversion_rate}%",
            "trend": f"{product_count} 个可售产品",
            "detail": f"基于本地票务/酒店产品、行程生成记录和营销内容记录估算可转化空间。当前均价约 CNY {avg_price}。",
            "source": "tickets/products + stats",
        },
        {
            "id": "content_opportunity",
            "label": "内容机会",
            "value": str(content_opportunity),
            "trend": "可生成选题",
            "detail": "由平台灵感库、市场热点和文创产品灵感共同形成，可用于短视频、小红书、公众号和路线攻略。",
            "source": "market/hot-inspirations + xhs_notes",
        },
    ]

    top_trend = trends[0] if trends else {"title": f"{city}文旅内容", "summary": "可结合景点、美食、路线和文创做运营内容。"}
    top_products = products[:3]

    # ===== 热点机会 TOP10：授权热点 + 社区灵感按评分排序，点击可一键跳转生成 =====
    hot_pool = []
    _seen_hot = set()
    for note in list(hot_data.get('xhs_notes') or []) + list(notes):
        t = (note.get('title') or '').strip()
        if not t or t in _seen_hot:
            continue
        _seen_hot.add(t)
        hot_pool.append(note)
    hot_pool.sort(key=lambda n: float(n.get('score') or 0), reverse=True)
    rankings_hot = []
    for idx, note in enumerate(hot_pool[:10]):
        dest = str(note.get('destination') or note.get('city') or city).strip() or city
        rankings_hot.append({
            "id": f"hot-{idx}",
            "type": "rank",
            "rank": idx + 1,
            "title": note.get('title') or '热点机会',
            "meta": " / ".join([v for v in [dest, note.get('theme'), note.get('crowd')] if v]) or dest,
            "score": int(float(note.get('score') or 70)),
            "summary": note.get('summary') or '',
            "detail": (note.get('summary') or '可结合景点、美食、路线与文创做运营内容。') + " 点击后自动跳转到对应功能并一键生成内容。",
            "highlights": (note.get('highlights') or [])[:3],
            "pitfalls": (note.get('pitfalls') or [])[:2],
            "url": note.get('source_url') or '',
            "source": note.get('source_type') or 'market_hot',
            "destination": dest,
            "action": _ops_hot_action(note),
            "fill": {"destination": dest, "extra": note.get('title') or ''},
        })

    # ===== 今日建议任务：直接由 TOP 热点派生，点击跳转对应功能一键落地 =====
    hot_top = rankings_hot or [dict(top_trend, fill={"destination": city, "extra": top_trend.get('title', '')})]
    tasks = []
    if len(hot_top) > 0:
        n0 = hot_top[0]
        tasks.append({
            "id": "task_content",
            "type": "task",
            "icon": "文",
            "title": f"把热点「{n0['title']}」写成种草文案",
            "desc": f"热度 {n0.get('score', '')} · 一键生成小红书/抖音内容。",
            "detail": n0.get('summary') or n0.get('detail') or "将热点拆成标题、开头钩子、路线信息和转化 CTA。",
            "action": "marketing",
            "fill": {"destination": (n0.get('fill') or {}).get('destination') or city, "extra": n0['title']},
        })
    if len(hot_top) > 1:
        n1 = hot_top[1]
        tasks.append({
            "id": "task_route",
            "type": "task",
            "icon": "线",
            "title": f"为热点「{n1['title']}」排一条可执行路线",
            "desc": f"热度 {n1.get('score', '')} · 一键生成行程（含交通/停留/避坑）。",
            "detail": n1.get('summary') or n1.get('detail') or "把热点景点、美食与住宿组合成可执行路线。",
            "action": "plan",
            "fill": {"destination": (n1.get('fill') or {}).get('destination') or city},
        })
    if len(hot_top) > 2:
        n2 = hot_top[2]
        tasks.append({
            "id": "task_blog",
            "type": "task",
            "icon": "博",
            "title": f"围绕热点「{n2['title']}」生成图文推广博客",
            "desc": f"热度 {n2.get('score', '')} · 一键产出标题+正文+三张配图。",
            "detail": n2.get('summary') or n2.get('detail') or "生成图文并茂的旅游推广博客。",
            "action": "blog",
            "fill": {"destination": (n2.get('fill') or {}).get('destination') or city, "extra": n2['title']},
        })
    if len(tasks) < 3:
        tasks.append({
            "id": "task_product",
            "type": "task",
            "icon": "转",
            "title": "联动可售产品",
            "desc": "匹配门票、酒店或文创伴手礼形成转化闭环。",
            "detail": "当前可售产品示例：" + ("、".join(p.get('name', '') for p in top_products) if top_products else "暂无产品"),
            "action": "chat",
            "fill": {"extra": top_products[0].get('name', '') if top_products else ''},
        })

    # ===== TOP10 排行榜：目的地热度 / 咨询转化 / 内容机会 =====
    # 目的地热度：按热点与社区灵感来源城市聚合，出现次数 + 平均分为依据
    heat_map = {}
    for note in list(hot_data.get('xhs_notes') or []) + list(notes):
        dest = str(note.get('destination') or note.get('city') or '').strip()
        if not dest:
            continue
        score = float(note.get('score') or 70)
        entry = heat_map.setdefault(dest, {"count": 0, "score_sum": 0.0, "sample": ""})
        entry["count"] += 1
        entry["score_sum"] += score
        if not entry["sample"]:
            entry["sample"] = note.get('title') or ''
    heat_items = sorted(heat_map.items(), key=lambda kv: (kv[1]["count"], kv[1]["score_sum"]), reverse=True)[:10]
    rankings_heat = []
    for idx, (dest, info) in enumerate(heat_items):
        avg = info["score_sum"] / max(1, info["count"])
        score = int(max(60, min(99, avg + info["count"] * 2)))
        rankings_heat.append({
            "id": f"heat-{idx}",
            "type": "rank",
            "rank": idx + 1,
            "title": dest,
            "meta": f"{info['count']} 条热点 / 社区内容",
            "score": score,
            "detail": f"「{dest}」近期在授权热点与社区灵感中出现 {info['count']} 次，代表内容如「{info['sample']}」。热度越高，越值得优先做行程与营销内容。",
            "highlights": [f"近端热点 {info['count']} 条，均分 {avg:.0f}", "点击「进入相关功能」自动带目的地跳转行程规划"],
            "source": "market_hot + xhs_notes",
            "action": "plan",
            "fill": {"destination": dest},
        })

    # 咨询转化：可售产品按客单价排序，高客单优先做打包转化
    conv_products = sorted([p for p in products if float(p.get('price') or 0) > 0],
                           key=lambda p: float(p.get('price') or 0), reverse=True)[:10]
    max_price = max([float(p.get('price') or 0) for p in conv_products] or [1])
    rankings_conversion = []
    for idx, p in enumerate(conv_products):
        price = float(p.get('price') or 0)
        rankings_conversion.append({
            "id": f"conv-{idx}",
            "type": "rank",
            "rank": idx + 1,
            "title": p.get('name') or '可售产品',
            "meta": f"¥{int(price)} · {p.get('type') or '票务'} · {p.get('city') or city}",
            "score": int(40 + 55 * price / max_price),
            "detail": f"{p.get('name')}（{p.get('desc') or ''}）：当前可售产品，客单价 ¥{int(price)}。适合与「{p.get('city') or city}」行程打包，在推荐卡片中直达转化。",
            "highlights": (p.get('tags') or [])[:3] + ["点击「进入相关功能」自动填充营销表单"],
            "source": p.get('source') or 'tickets',
            "action": "marketing",
            "fill": {"destination": p.get('city') or city, "extra": p.get('name') or ''},
        })

    # 内容机会：社区灵感 + 市场热点按评分排序，均可一键转营销内容
    content_pool = list(notes) + list(hot_data.get('xhs_notes') or [])
    content_pool.sort(key=lambda n: float(n.get('score') or 0), reverse=True)
    rankings_content = []
    for idx, note in enumerate(content_pool[:10]):
        dest = str(note.get('destination') or note.get('city') or city).strip() or city
        rankings_content.append({
            "id": f"content-{idx}",
            "type": "rank",
            "rank": idx + 1,
            "title": note.get('title') or '内容选题',
            "meta": " / ".join([v for v in [dest, note.get('theme'), note.get('crowd')] if v]) or dest,
            "score": int(float(note.get('score') or 70)),
            "detail": (note.get('summary') or '可转为短视频、小红书或公众号选题。') + " 点击「进入相关功能」自动把选题填入营销生成表单。",
            "highlights": (note.get('highlights') or [])[:3],
            "pitfalls": (note.get('pitfalls') or [])[:2],
            "url": note.get('source_url') or '',
            "source": note.get('source_type') or 'xhs_notes',
            "action": "marketing",
            "fill": {"destination": dest, "extra": note.get('title') or ''},
        })

    return jsonify({
        "success": True,
        "city": city,
        "keyword": keyword,
        "updated_at": datetime.now().isoformat(timespec="minutes"),
        "source_note": "聚合真实接口与本地业务数据；外部热点仅使用官方/授权接口或已配置服务。",
        "kpis": kpis,
        "trends": trends[:limit],
        "tasks": tasks,
        "rankings": {
            "hot": rankings_hot,
            "heat": rankings_heat,
            "conversion": rankings_conversion,
            "content": rankings_content,
        },
        "brief": {
            "id": "brief_today",
            "title": f"今日重点：把「{top_trend['title']}」转成可预订路线",
            "desc": "建议联动景点门票、餐饮体验、文创伴手礼与社媒内容。",
            "detail": top_trend.get("summary") or "优先选择热度高、可拍照、可转化、交通可达的内容。",
            "action": "marketing",
        },
        "raw_counts": {
            "notes": len(notes),
            "ticket_products": product_count,
            "itineraries": stats.get("total_itineraries", 0),
            "marketing": stats.get("total_marketing", 0),
        }
    })


@app.route('/api/xhs/notes', methods=['GET'])
def xhs_notes():
    """查询文旅平台灵感库。仅返回链接、摘要、标签等合规元数据。"""
    keyword = request.args.get('keyword', '').strip() or None
    city = request.args.get('city', '').strip() or None
    theme = request.args.get('theme', '').strip() or None
    crowd = request.args.get('crowd', '').strip() or None
    source_type = request.args.get('source_type', '').strip() or None
    limit = min(int(request.args.get('limit', 50)), 100)
    return jsonify({
        "success": True,
        "items": db.list_xhs_notes(keyword=keyword, city=city, theme=theme, crowd=crowd, source_type=source_type, limit=limit),
        "facets": db.get_xhs_note_facets()
    })


@app.route('/api/xhs/notes/<note_id>', methods=['GET'])
def xhs_note_detail(note_id):
    """查询单条平台灵感详情。"""
    note = db.get_xhs_note(note_id)
    if not note:
        return jsonify({"success": False, "error": "灵感内容不存在"}), 404
    return jsonify({"success": True, "item": note})


@app.route('/api/xhs/notes', methods=['POST'])
def create_xhs_note():
    """导入一条平台灵感元数据。"""
    data = request.json or {}
    required = ['title', 'source_url', 'destination', 'city', 'theme', 'summary']
    missing = [k for k in required if not data.get(k)]
    if missing:
        return jsonify({"success": False, "error": "缺少字段: " + ",".join(missing)}), 400
    note = db.upsert_xhs_note(data)
    return jsonify({"success": True, "item": note})


def _stream_llm_response(messages: list, skill_name: str = "", usage_label: str = ""):
    """
    通用流式 LLM 响应辅助函数
    
    以 SSE 格式逐块转发 LLM 的流式输出，适用于行程规划、营销生成等独立接口。
    与 stream_response 的区别：不保存对话历史，不提取地点信息。
    
    Args:
        messages: 标准 OpenAI 格式的消息列表
        skill_name: 当前 Skill 名称，用于动态调整 temperature / max_tokens
        usage_label: 只写进用量台账的标签，不参与 temperature——营销/文创这些接口
            没有 Skill 名，不标就全落在「未标注」里，成本表按能力分不开。
        
    Returns:
        成功: Flask Response 对象 (mimetype='text/event-stream')
        失败: JSON 错误响应
    """
    result = call_llm(messages, stream=True, skill_name=skill_name)
    if isinstance(result, dict) and "error" in result:
        # 携带 error_code 供前端判断是「网络类」还是「服务类」，措辞与重试入口据此变化
        return jsonify({
            "success": False,
            "error": result["error"],
            "error_code": result.get("error_code") or "server",
        }), 500

    provider = API_CONFIG["provider"]
    config = PROVIDERS.get(provider, PROVIDERS["deepseek"])

    def generate():
        try:
            _usage_t0 = _usage_now()
            _usage_first = None
            _usage_chunk = None
            _out_chars = []
            resp, conn_err = _llm_send(result["url"], result["headers"], result["payload"],
                                       stream=True, timeout=180)
            if conn_err is not None:
                yield f'data: {json.dumps({"error": conn_err["error"], "error_code": conn_err.get("error_code", "network")}, ensure_ascii=False)}\n\n'
                yield 'data: [DONE]\n\n'
                return
            resp.encoding = 'utf-8'
            for line in resp.iter_lines(decode_unicode=True):
                if line and line.startswith('data: '):
                    data_str = line[6:]
                    if data_str.strip() == '[DONE]':
                        yield 'data: [DONE]\n\n'
                        break
                    try:
                        chunk_data = json.loads(data_str)
                        if chunk_data.get('usage'):
                            _usage_chunk = chunk_data['usage']
                        _choices = chunk_data.get('choices') or [{}]
                        delta = _choices[0].get('delta', {})
                        content = delta.get('content', '')
                        if content:
                            if _usage_first is None:
                                _usage_first = _usage_now() - _usage_t0
                            _out_chars.append(content)
                            yield f'data: {json.dumps({"content": content}, ensure_ascii=False)}\n\n'
                    except json.JSONDecodeError:
                        pass
            _note_llm_call(skill_name or usage_label, _usage_now() - _usage_t0, messages,
                           "".join(_out_chars), _usage_chunk, _usage_first)
        except Exception as e:
            import traceback
            print("[stream_llm_error]\n%s" % traceback.format_exc())
            _msg, _code = friendly_llm_error(e)
            yield f'data: {json.dumps({"error": _msg, "error_code": _code}, ensure_ascii=False)}\n\n'

    return Response(generate(), mimetype='text/event-stream; charset=utf-8')


@app.route('/api/conversations/<conv_id>', methods=['DELETE'])
def clear_conversation(conv_id):
    """清除对话历史（内存+SQLite+会话摘要+已确认事实）"""
    _conv_cache_drop(conv_id)
    # 从 SQLite 删除该会话的消息历史（保留会话记录）
    conn = db.get_connection()
    conn.execute("DELETE FROM messages WHERE session_id = ?", (conv_id,))
    conn.commit()
    conn.close()
    # 会话级记忆一并清掉，否则历史删了、摘要和事实还会继续影响后续回答
    clear_summary(conv_id)
    try:
        get_fact_tracker().clear(conv_id)
    except Exception:
        pass
    return jsonify({"success": True})


@app.route('/api/memory', methods=['GET', 'DELETE'])
def long_term_memories_api():
    """长期记忆的透明可控入口：GET 查看当前用户记住了什么，DELETE 全部清除。"""
    mem = _get_memory()
    if request.method == 'GET':
        uid = _resolve_uid(request.args.get('user_id'))
        items = mem.list_memories(uid, kind=request.args.get('kind')) if mem else []
        return jsonify({'success': True, 'user_id': uid, 'total': len(items),
                        'items': items, 'stats': _memory_stats(uid)})
    uid = _resolve_uid(request.args.get('user_id')
                       or (request.get_json(silent=True) or {}).get('user_id'))
    n = mem.forget(user_id=uid) if mem else 0
    app.logger.info("user=%s 清空长期记忆 %d 条", uid, n)
    return jsonify({'success': True, 'deleted': n, 'user_id': uid})


@app.route('/api/memory/recall', methods=['POST'])
def recall_long_term_memories():
    """调试用：看某一问句会召回哪些长期记忆及其打分因子。"""
    payload = request.get_json(silent=True) or {}
    uid = _resolve_uid(payload.get('user_id'))
    mem = _get_memory()
    items = mem.recall(uid, payload.get('query') or '', touch=False) if mem else []
    return jsonify({'success': True, 'user_id': uid, 'items': items})


@app.route('/api/memory/<memory_id>', methods=['DELETE'])
def delete_long_term_memory(memory_id):
    """删除单条长期记忆（用户说「别记这个」时前端调用）。"""
    mem = _get_memory()
    n = mem.forget(memory_id=memory_id) if mem else 0
    return jsonify({'success': n > 0, 'deleted': n})


@app.route('/api/export/mine', methods=['GET'])
def export_my_data():
    """导出当前账号自己的全部业务数据（题面要求的「数据安全导出」）。

    取代原来的 /api/export：那个接口把请求体里的 filename 直接拼进路径往 data/exports
    写文件（`filename="../../x"` 就能写到目录外），而且前端从来没有调用过它。这里
    不落盘、文件名由服务端生成、内容只按 user_id 过滤，读完直接以附件回给浏览器——
    「导出」不该在服务器上留一份用户没打算留下的副本。
    """
    uid = _resolve_uid(request.args.get('user_id'))
    payload = db.export_user_data(uid)
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    fname = f"智旅灵犀-我的数据-{uid}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    resp = Response(body, mimetype='application/json; charset=utf-8')
    from urllib.parse import quote
    # 中文名走 RFC 5987：filename 参数只留 ASCII 兜底名，真实名用百分号编码，
    # 不把任何未经编码的字符串拼进响应头
    resp.headers['Content-Disposition'] = (
        'attachment; filename="smarttourism-my-data.json"; '
        "filename*=UTF-8''" + quote(fname))
    resp.headers['Cache-Control'] = 'no-store'
    c = payload['counts']
    app.logger.info("user=%s 数据导出：会话 %d / 消息 %d / 行程 %d / 反馈 %d",
                    uid, c['sessions'], c['messages'], c['itineraries'], c['feedback'])
    return resp


@app.route('/api/plugins', methods=['GET'])
def list_plugins():
    """列出所有插件及其状态"""
    plugins = []
    for p in registry.list_all():
        plugins.append({
            "name": p.name,
            "display_name": p.display_name,
            "description": p.description,
            "version": p.version,
            "configured": p.is_configured(),
            "required_config": p.required_config,
            "trigger_keywords": p.trigger_keywords[:10],
        })
    return jsonify(plugins)


@app.route('/api/plugins/<plugin_name>/trigger', methods=['POST'])
def trigger_plugin(plugin_name: str):
    """手动触发指定插件"""
    plugin = registry.get(plugin_name)
    if not plugin:
        return jsonify({"error": f"插件 {plugin_name} 不存在"}), 404
    if not plugin.is_configured():
        return jsonify({"error": f"插件 {plugin_name} 未配置", "required": plugin.required_config}), 400

    params = request.json or {}
    result = plugin.execute(params)
    formatted = plugin.format_for_llm(result)
    return jsonify({"success": True, "data": result, "formatted": formatted})


@app.route('/api/map/regeo', methods=['POST'])
def map_regeo():
    """逆地理编码：坐标转地址"""
    plugin = registry.get("amap_map")
    if not plugin or not plugin.is_configured():
        return jsonify({"error": "地图插件未配置"}), 400
    params = request.json or {}
    params["action"] = "regeo"
    result = plugin.execute(params)
    return jsonify(result)


@app.route('/api/map/around', methods=['POST'])
def map_around():
    """周边搜索"""
    plugin = registry.get("amap_map")
    if not plugin or not plugin.is_configured():
        return jsonify({"error": "地图插件未配置"}), 400
    params = request.json or {}
    params["action"] = "around"
    result = plugin.execute(params)
    return jsonify(result)


@app.route('/api/map/direction', methods=['POST'])
def map_direction():
    """路线规划"""
    plugin = registry.get("amap_map")
    if not plugin or not plugin.is_configured():
        return jsonify({"error": "地图插件未配置"}), 400
    params = request.json or {}
    params["action"] = "direction"
    result = plugin.execute(params)
    return jsonify(result)


@app.route('/api/map/ip-location', methods=['GET'])
def map_ip_location():
    """IP定位"""
    plugin = registry.get("amap_map")
    if not plugin or not plugin.is_configured():
        return jsonify({"error": "地图插件未配置"}), 400
    result = plugin.execute({"action": "ip_location"})
    return jsonify(result)


@app.route('/api/my-location', methods=['GET'])
def my_location():
    """通过后端代理获取用户真实IP位置，避免前端CORS限制"""
    if not _amap_key:
        return jsonify({"error": "高德Key未配置"}), 500

    # 获取用户真实IP（优先 X-Forwarded-For，其次 remote_addr）
    user_ip = request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
    if not user_ip:
        user_ip = request.headers.get('X-Real-IP', '')
    if not user_ip:
        user_ip = request.remote_addr or ''

    # 如果是本地请求，先检测公网IP
    ip_param = ''
    if user_ip and user_ip not in ('127.0.0.1', '::1', 'localhost'):
        ip_param = user_ip
    else:
        # 本地开发环境：通过外部服务检测服务器公网IP
        try:
            import requests as _requests
            r = _requests.get('https://whois.pconline.com.cn/ipJson.jsp?json=true', timeout=5,
                              headers={'User-Agent': 'Mozilla/5.0'})
            r.encoding = 'gbk'
            d = r.json()
            if d.get('ip'):
                ip_param = d['ip']
                # 如果外部服务直接返回了位置信息，直接用
                if d.get('city'):
                    city = d['city']
                    province = d.get('pro', '')
                    # 用城市名做地理编码，获取坐标
                    geo_lng, geo_lat = 0, 0
                    try:
                        geo_resp = _requests.get('https://restapi.amap.com/v3/geocode/geo', params={
                            'key': _amap_key, 'address': city
                        }, timeout=5)
                        geo_data = geo_resp.json()
                        if geo_data.get('status') == '1' and geo_data.get('geocodes'):
                            loc = geo_data['geocodes'][0]['location'].split(',')
                            geo_lng = float(loc[0])
                            geo_lat = float(loc[1])
                    except:
                        pass
                    return jsonify({
                        "status": "ok",
                        "city": city,
                        "province": province,
                        "district": "",
                        "address": d.get('addr', ''),
                        "lng": geo_lng,
                        "lat": geo_lat,
                        "ip": ip_param,
                        "source": "pconline-ip"
                    })
        except Exception as e:
            print("[Location] Public IP detection failed: %s" % e)

    try:
        import requests as _requests
        params = {"key": _amap_key}
        if ip_param:
            params["ip"] = ip_param

        resp = _requests.get("https://restapi.amap.com/v3/ip", params=params, timeout=10)
        data = resp.json()

        if data.get("status") == "1":
            city = data.get("city", "")
            province = data.get("province", "")
            rectangle = data.get("rectangle", "")
            lng, lat = 0, 0

            # 解析 rectangle 获取中心坐标
            if rectangle and ";" in rectangle:
                parts = rectangle.split(";")
                if len(parts) >= 2:
                    p1 = parts[0].split(",")
                    p2 = parts[1].split(",")
                    if len(p1) == 2 and len(p2) == 2:
                        lng = (float(p1[0]) + float(p2[0])) / 2
                        lat = (float(p1[1]) + float(p2[1])) / 2

            # 如果有坐标，做一次逆地理编码获取更精确的区/街道信息
            district = ""
            address = ""
            if lng and lat:
                try:
                    regeo_resp = _requests.get("https://restapi.amap.com/v3/geocode/regeo", params={
                        "key": _amap_key,
                        "location": f"{lng},{lat}",
                        "extensions": "base"
                    }, timeout=8)
                    regeo_data = regeo_resp.json()
                    if regeo_data.get("status") == "1":
                        rc = regeo_data.get("regeocode", {}).get("addressComponent", {})
                        district = rc.get("district", "")
                        address = regeo_data.get("regeocode", {}).get("formatted_address", "")
                        if not city:
                            city = rc.get("city", "") or rc.get("province", "")
                except:
                    pass

            return jsonify({
                "status": "ok",
                "city": city if isinstance(city, str) else "",
                "province": province if isinstance(province, str) else "",
                "district": district,
                "address": address,
                "lng": round(lng, 6),
                "lat": round(lat, 6),
                "ip": ip_param or "(server)",
                "source": "backend-ip"
            })

        return jsonify({"error": data.get("info", "IP定位失败"), "status": "error"})

    except Exception as e:
        return jsonify({"error": str(e), "status": "error"}), 500


@app.route('/api/map/key', methods=['GET'])
def map_key():
    """获取高德 JS API Key 与安全码（前端地图使用）"""
    config = load_plugin_config()
    js_key = config.get('amap_js_key', config.get('amap_key', ''))
    security = config.get('amap_security_code', '')
    return jsonify({"key": js_key, "security": security})


# ===== 美团单项实时价格（景点门票 / 酒店房价 / 餐饮人均），供推荐卡片懒加载 =====
_meituan_price_cache: dict = {}

_MT_PRICE_PATTERNS = [
    re.compile(r'([0-9]{1,5})\s*元\s*到\s*([0-9]{1,5})\s*元'),          # 15元到50元（区间优先）
    re.compile(r'[¥￥]\s*([0-9][0-9Xx]{0,4})\s*起'),                     # ￥1XX起 / ¥90起
    re.compile(r'[¥￥]\s*([0-9][0-9Xx]{0,4})'),                          # ￥220 / ¥1XX
    re.compile(r'([0-9]{1,5})\s*元\s*[/／]\s*人'),                       # 60元/人
    re.compile(r'([0-9]{1,5})\s*元\s*起'),                               # 35元起
    re.compile(r'(人均|门票|票价|价格|成人票|学生票|儿童票|优惠票|联票|售价|起价)[^0-9¥￥]{0,8}([0-9]{1,5})\s*元'),  # 成人票50元 / 门票45元
]

# 返回 (price, is_range) 的 pattern 下标：区间 pattern 需要合并两个 group
_MT_RANGE_IDX = 0
_MT_CTX_IDX = 5  # 上下文词 pattern 的价格在 group(2)


def _extract_meituan_price(answer: str, name: str = "") -> str:
    """从美团单项查询的返回文本中提取价格，优先取与目标名称同处的行。

    覆盖格式：￥1XX起 / ¥90 / 15元到50元（区间）/ 成人票50元 / 60元/人 / 35元起 / 门票45元。
    """
    if not answer:
        return ""
    lines = answer.split("\n")
    preferred = lines
    if name:
        key = name[:8]
        hit = [ln for ln in lines if key in ln]
        if hit:
            preferred = hit + [ln for ln in lines if ln not in hit]
    for pool in (preferred, lines):
        for pi, rx in enumerate(_MT_PRICE_PATTERNS):
            for ln in pool:
                m = rx.search(ln)
                if not m:
                    continue
                if pi == _MT_RANGE_IDX:
                    return "%s-%s" % (m.group(1), m.group(2))
                if pi == _MT_CTX_IDX:
                    return m.group(2)
                return m.group(1)
    return ""


@app.route('/api/meituan/price', methods=['POST'])
def meituan_item_price():
    """查询单个 POI 的美团实时价格（门票/房价/人均）。

    请求: {"city": "温州", "name": "江心屿", "category": "景点"}
    响应: {"status": "ok", "price": "35", "url": "...", "source": "美团"}
    结果按 (city, name) 缓存，避免卡片重复请求。
    """
    body = request.get_json(force=True, silent=True) or {}
    city = (body.get('city') or '').strip()
    name = (body.get('name') or '').strip()
    category = (body.get('category') or '').strip()
    if not name:
        return jsonify({"status": "error", "message": "缺少 name 参数"})

    # 非文旅条目（购物中心/商超/车站等）直接跳过价格查询：这类条目在美团侧必然查不到
    # 票价，而景点分支会串行重试两次（每次 11-24s），前端卡片会长时间停在"美团价查询中…"
    _NON_TRAVEL_WORDS = ("购物中心", "商场", "商城", "超市", "步行街", "停车场",
                         "地铁站", "火车站", "客运站", "机场", "小区", "写字楼")
    _TRAVEL_WORDS = ("景区", "景点", "乐园", "公园", "博物馆", "纪念馆", "古镇", "古城",
                     "酒店", "民宿", "客栈", "餐厅", "小吃", "火锅", "度假")
    if (any(w in name for w in _NON_TRAVEL_WORDS)
            and not any(w in name for w in _TRAVEL_WORDS)):
        return jsonify({"status": "skip", "message": "非文旅条目，跳过价格查询"})

    cache_key = (city, name)
    if cache_key in _meituan_price_cache:
        return jsonify(_meituan_price_cache[cache_key])

    meituan = registry.get("meituan")
    if not (meituan and meituan.is_configured()):
        return jsonify({"status": "not_configured", "message": "未配置美团 Token"})

    if '酒' in category or '宿' in category:
        queries = [f"{city}{name}酒店每晚住宿价格多少钱，怎么在线预订"]
    elif '餐' in category or '食' in category or '美食' in category:
        queries = [f"{city}{name}人均消费多少钱"]
    else:  # 景点/其他：按门票查询；美团 AI 有时省略价格，换问法重试一次
        queries = [f"{city}{name}门票多少钱一张", f"{city}{name}景区门票价格"]

    price, url, last_err = "", "", ""
    for query in queries:
        try:
            r = meituan.execute({"city": city or "北京", "query": query})
        except Exception as e:
            last_err = str(e)[:120]
            continue
        if r.get("status") != "ok":
            last_err = r.get("message", "美团查询失败")
            continue
        answer = str(r.get("answer") or "")
        if not price:
            price = _extract_meituan_price(answer, name)
        if not url:
            for b in (r.get("bookings") or []):
                if b.get("url"):
                    url = b["url"]
                    break
        if price:
            break

    if not price and last_err:
        return jsonify({"status": "error", "message": last_err})

    result = {"status": "ok", "price": price, "url": url, "source": "美团"}
    _meituan_price_cache[cache_key] = result
    return jsonify(result)


# ===== 灵感橱窗详情：美团真实口碑评价（懒加载，供首页卡片详情弹窗使用）=====
_portal_reviews_cache: dict = {}   # (city, name) -> {"reviews": [...], "rating": "", "source": "美团"}

# 分类 → 美团问法（明确索要"游客/食客/住客真实评价"，避免只返回榜单或价格）
_PORTAL_REVIEW_QUERY = {
    "scenic": "{city}{name}景区游客真实评价怎么样，有哪些口碑亮点和游玩体验反馈",
    "culture": "{city}{name}游客真实评价怎么样，有哪些口碑亮点和体验反馈",
    "food": "{city}{name}餐厅食客真实评价怎么样，有哪些口碑亮点和用餐体验反馈",
    "night": "{city}{name}游客真实评价怎么样，有哪些口碑亮点和夜游体验反馈",
    "hotel": "{city}{name}酒店住客真实评价怎么样，有哪些口碑亮点和入住体验反馈",
    "market": "{city}{name}游客真实评价怎么样，有哪些口碑亮点和逛集体验反馈",
}


@app.route('/api/portal/hot-reviews', methods=['POST'])
def portal_hot_reviews():
    """查询灵感橱窗卡对应 POI 的美团真实口碑评价（抽取原文评价句，非模型编造）。

    请求: {"city": "杭州", "name": "西湖宋韵线", "category": "scenic"}
    响应: {"status": "ok", "reviews": ["环境…", "服务…"], "rating": "4.8", "source": "美团"}
    结果按 (city, name) 缓存，避免弹窗重复消耗美团额度。
    """
    body = request.get_json(force=True, silent=True) or {}
    city = (body.get('city') or '杭州').strip() or '杭州'
    name = (body.get('name') or '').strip()
    category = (body.get('category') or 'scenic').strip() or 'scenic'
    if not name:
        return jsonify({"status": "error", "message": "缺少 name 参数"})

    cache_key = (city, name)
    if cache_key in _portal_reviews_cache:
        cached = dict(_portal_reviews_cache[cache_key])
        cached["status"] = "ok"
        return jsonify(cached)

    meituan = registry.get("meituan")
    if not (meituan and meituan.is_configured()):
        return jsonify({"status": "not_configured", "message": "未配置美团 Token"})

    tpl = _PORTAL_REVIEW_QUERY.get(category, _PORTAL_REVIEW_QUERY["scenic"])
    query = tpl.format(city=city, name=name)
    try:
        r = meituan.execute({"city": city, "query": query})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)[:120]})
    if r.get("status") != "ok":
        return jsonify({"status": "error", "message": r.get("message", "美团查询失败")})

    answer = str(r.get("answer") or "")
    plain = _reco_plain(answer)
    reviews = _reco_reviews(plain, category)[:4]
    rm = _RECO_RATING_RE.search(plain)
    rating = rm.group(1) if rm else ""

    result = {"reviews": reviews, "rating": rating, "source": "美团"}
    # 只在真正抽到评价句时缓存，避免把一次失败的空结果长期锁死
    if reviews:
        _portal_reviews_cache[cache_key] = result
    payload = dict(result)
    payload["status"] = "ok"
    return jsonify(payload)


# ╔══════════════════════════════════════════════════════════════╗
# ║  🧭 对话结尾推荐卡片（景点 → 美食 → 酒店）                       ║
# ║  数据源优先级：美团实时数据 > 高德 POI > 本地知识库              ║
# ╚══════════════════════════════════════════════════════════════╝
# 卡片字段：描述 / 价格 / 地址 / 联系电话 / 评分与评价要点 / 图片 / 美团购票链接
# 图片优先取美团实拍图，取不到时用高德 POI 图补齐；两者都没有则 image_missing=True，
# 由前端明确提示用户"暂无图片，可前往美团/高德查看实景图"，绝不编造图片。

_RECO_GROUP_ORDER = [
    # (内部键, 展示名, 美团查询模板)
    ("scenic", "景点", "{city}有哪些必去的景点，逐一给出门票价格、地址和联系电话"),
    ("food", "美食", "{city}有哪些必吃的当地特色餐厅，逐一给出餐厅名称、人均价格、地址、联系电话和评分"),
    ("hotel", "酒店", "{city}有哪些性价比高的酒店推荐，给出每晚价格、地址电话、评分和预订链接"),
]
_RECO_TYPE_LABEL = {"scenic": "景点", "food": "美食", "hotel": "酒店"}
_RECO_PER_GROUP = 3          # 每组最多 3 张卡片，聚焦而非堆量
_RECO_TTL = 1800             # 同一城市卡片缓存 30 分钟，避免重复消耗美团额度

_reco_cards_cache: dict = {}  # city -> {"ts": float, "payload": dict}
_reco_mt_cache: dict = {}     # (city, key) -> {"ts": float, "answer": str}
_reco_seed_cache: dict = {}   # city -> {"ts": float, "answer": str} 对话中已取得的美团原文，优先复用
_reco_amap_lock = threading.Lock()  # 高德 POI 调用节流锁，避免个人 Key 触发 QPS 限制
_reco_amap_last = [0.0]             # 上次高德调用时间戳

# 非文旅 POI 直接排除（历史踩坑：酒店全称被高德错配到购物中心，渲染出无关卡片）
_RECO_NON_TRAVEL = ("购物中心", "商场", "商城", "超市", "步行街", "停车场", "加油站",
                    "地铁站", "火车站", "客运站", "机场", "小区", "写字楼", "公司企业", "银行")
# 房型链接不是独立商户入口，不作为卡片主体
_RECO_ROOM_RE = re.compile(r'(大床|双床|单人房|双人房|标间|榻榻米|投影|房\]|房$|间$|套$)')
# 美团美食接口只给"主题式小标题"（如"地道温州菜""特色小吃"），不含商户名，
# 这类泛称不能当卡片名称，需放行给高德补充真实商户（2026-09-19 四次实测确认）
# 判定方式：只有带品牌特征（分隔符/数字/门店后缀）的才算真实商户名
_RECO_BRAND_RE = re.compile(
    r'[·•・（）()0-9]|(店|馆|楼|家|轩|庄|居|苑|阁|铺|坊|斋|屋|舍|记|号|行|'
    r'酒店|大酒家|私房菜|菜馆|烧|烤|面|粉|粥|茶|咖)$')
_RECO_BLOCK_RE = re.compile(r'^\s*#{1,4}\s*(.+?)\s*$', re.MULTILINE)
# 纯修饰性词汇：一个名称若被这些词完全覆盖（如"经典老字号"），就只是泛称而非商户名
_RECO_GENERIC_TOKENS = ("经典", "特色", "地道", "正宗", "本地", "当地", "人气", "网红", "热门",
                        "老字号", "招牌", "精选", "必吃", "好吃", "传统", "古法", "口碑", "推荐",
                        "新晋", "宝藏", "风味", "家常", "私房", "各类", "主打", "系列", "合集",
                        "小吃", "美食", "餐厅", "菜", "店", "馆")
# 位置无关的泛称标记：名称里只要带这些词，就多半是美团的主题小标题而非商户名
# （"桂林菜老字号"结尾命中"号"会被品牌特征误判为真实商户，靠这条拦住）
_RECO_GENERIC_PHRASE_RE = re.compile(
    r'(老字号|老店|特色小吃|地道|当地特色|本地特色|人气推荐|必吃|美食合集|各类美食|各种美食)')
# 酒店卡片必须能指认到具体住宿主体：带住宿业态后缀，或带"××店"式分店括注。
# 美团酒店区块里夹着"栈桥周边·近海实惠""五四广场·观影体验""古城内·经济实惠商务"
# 这类"区域·卖点"小标题，它们不是商户名，混进卡片会渲染出无地址/无电话的空壳
# （2026-09-19 青岛/丽江实测复现）
_RECO_LODGE_RE = re.compile(
    r'(酒店|宾馆|旅馆|客栈|民宿|美宿|公寓|山庄|度假村|青旅|招待所|饭店|大厦|公馆|'
    r'house|hotel|inn|hostel)', re.I)
_RECO_BRANCH_RE = re.compile(r'[（(][^）)]{1,20}店[）)]')
_RECO_ORDINAL_RE = re.compile(r'^[一二三四五六七八九十0-9]+\s*[、.．)）]\s*')
_RECO_LINK_RE = re.compile(r'\[([^\]\n]{1,60})\]\((https?://[^)\s]+)\)')
_RECO_IMAGE_RE = re.compile(r'!\[[^\]]*\]\((https?://[^)\s]+)\)')
_RECO_ADDR_RE = re.compile(r'(?:地址[位于]*|位于|坐落于)[:：]?\s*([^\n，。,；;、]{4,40})')
_RECO_TEL_RE = re.compile(r'(?:联系电话|预定电话|电话|Tel)[为是:：]*\s*([0-9][0-9\-—~、,，\s]{5,40})')
_RECO_RATING_RE = re.compile(r'评分[^\d]{0,4}([0-9](?:\.[0-9])?)\s*分')
_RECO_DESC_RE = re.compile(r'\*{0,2}(?:特色亮点|推荐理由|亮点|特色|简介)\*{0,2}[:：]\s*([^\n]+)')
_RECO_CITY_LIST = ['杭州', '三亚', '成都', '北京', '大理', '丽江', '上海', '广州', '深圳',
                   '厦门', '桂林', '西安', '南京', '重庆', '青岛', '武汉', '长沙', '昆明',
                   '张家界', '黄山', '九寨沟', '拉萨', '哈尔滨', '苏州', '乌镇', '温州',
                   '宁波', '郑州', '合肥', '南昌', '福州', '南宁', '贵阳', '海口',
                   '济南', '太原', '沈阳', '长春', '天津', '石家庄', '呼和浩特']
# 高德 POI 分类码（前 6 位）：风景名胜 / 餐饮服务 / 住宿服务
_RECO_AMAP_TYPES = {"scenic": "110000", "food": "050000", "hotel": "100000"}

# 高德 POI 的 type 字段是「多值」的：多值之间用 `|` 分隔，单值内部用 `;` 分层级。
# 例："餐饮服务;中餐厅;中餐厅|住宿服务;住宿服务相关;住宿服务相关"（民宿同时挂了两个大类）。
# 历史 bug：只取 `split(";")[0]` 会把上面这种 POI 判成"餐饮服务"而放进美食组，
# 于是"杭州临安坡心橼舍民宿""玉龙白沙玉湖人家客栈"被当成餐厅推荐（2026-09-19 实测复现）。
# 故改为拆出全部主类型逐一校验：必须命中本类目，且不得命中互斥类目。
_RECO_TYPE_MUST = {"scenic": ("风景名胜", "公园广场", "科教文化服务", "体育休闲服务"),
                   "food": ("餐饮服务",), "hotel": ("住宿服务",)}
# 互斥类目只用于"景点/美食"：酒店内设餐厅会同时挂餐饮标签，故 hotel 组不加互斥，
# 纯餐厅靠上面的 MUST（缺"住宿服务"主类型）和下面的名称否决词拦掉
_RECO_TYPE_BAN = {"scenic": ("住宿服务",), "food": ("住宿服务",), "hotel": ()}
# 名称兜底：名称已明确指向其它业态时直接否决，防止 POI 分类标注本身有误。
# 注意"饭店"在各地兼指餐厅与旅馆，歧义太大，不放进任一方的否决词
_RECO_NAME_BAN = {"scenic": ("民宿", "客栈", "公寓", "宾馆", "酒店", "旅馆", "招待所", "青旅"),
                  "food": ("民宿", "客栈", "公寓", "宾馆", "酒店", "旅馆", "招待所", "青旅",
                           "景区", "风景区", "公园", "广场"),
                  "hotel": ("餐厅", "菜馆", "小吃", "烧烤", "咖啡", "茶楼", "火锅", "面馆", "酒楼")}
# 高德景点类目下会混入社区小广场、村级活动场所等非旅游 POI，数据贫瘠（无评分无图无电话）
# 时优先度下调，让字段更完整的景点排到前面
_RECO_SCENIC_WEAK_RE = re.compile(r'(广场|小区|社区|活动中心|村文化|便民|服务站|停车场|菜市场)')


def _reco_poi_heads(ptype: str) -> list:
    """拆出高德 POI 的全部主类型段（type 以 `|` 分多值、以 `;` 分层级）"""
    heads = []
    for seg in (ptype or "").split("|"):
        head = seg.split(";")[0].strip()
        if head:
            heads.append(head)
    return heads


def _reco_type_ok(name: str, ptype: str, key: str) -> bool:
    """校验高德 POI 是否真属于目标类目：主类型须命中本类目，且不得命中互斥类目"""
    if any(b in (name or "") for b in _RECO_NAME_BAN.get(key, ())):
        return False
    heads = _reco_poi_heads(ptype)
    if not heads:
        return False
    if not any(any(t in h for t in _RECO_TYPE_MUST.get(key, ())) for h in heads):
        return False
    if any(any(b in h for b in _RECO_TYPE_BAN.get(key, ())) for h in heads):
        return False
    return True


def _reco_richness(item: dict) -> int:
    """卡片字段完整度打分，用于同组内排序（评分/图片/电话/地址越多越靠前）"""
    return (2 if item.get("rating") else 0) + (2 if item.get("image") else 0) \
        + (1 if item.get("phone") else 0) + (1 if item.get("address") else 0)


def _reco_strip_tips(text: str) -> str:
    """去掉美团文案里的引导语/小贴士（不是卡片内容，混进描述会污染评价字段）"""
    keep = []
    for ln in (text or "").split("\n"):
        s = ln.strip()
        if s.startswith("🔍") or "小贴士" in s or "随时告诉小团" in s or "都可以随时" in s:
            continue
        keep.append(ln)
    return "\n".join(keep)


def _reco_plain(text: str) -> str:
    """去掉 Markdown 标记，得到可读纯文本（用于抽取地址/电话/价格/评价句）"""
    if not text:
        return ""
    t = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', text)          # 图片
    t = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', t)          # 链接 → 保留标题文字
    t = re.sub(r'</?[a-zA-Z]{1,8}>', ' ', t)                # 残留的 </ka> 等标签
    t = re.sub(r'\[([^\]\n]{0,40})(?=\n|$)', r'\1', t)      # 被截断的 [ 链接
    t = t.replace('**', '').replace('\\', '')
    t = re.sub(r'[ \t]{2,}', ' ', t)
    return t.strip()


def _reco_split_blocks(answer: str) -> list:
    """把美团 Markdown 按标题切成区块，返回 [(标题, 正文)]"""
    blocks = []
    answer = _reco_strip_tips(answer)
    matches = list(_RECO_BLOCK_RE.finditer(answer or ""))
    for i, m in enumerate(matches):
        title = _RECO_ORDINAL_RE.sub('', _reco_plain(m.group(1))).strip(' ·-—')
        end = matches[i + 1].start() if i + 1 < len(matches) else len(answer)
        body = (answer[m.end():end] or "").strip()
        if title and body:
            blocks.append((title, body))
    return blocks


def _reco_classify(blob: str) -> str:
    """按内容特征判断区块属于哪个类目（用于复用对话里已取得的美团原文）"""
    if re.search(r'起\s*/\s*晚|[¥￥][0-9Xx]{2,5}\s*起|美团(舒适|经济|高档|豪华)型|房型', blob):
        return "hotel"
    if "门票" in blob or "团购链接" in blob or "景区门票" in blob:
        return "scenic"
    if any(k in blob for k in ("人均", "餐厅", "必吃", "海鲜", "火锅", "小吃", "菜馆", "大排档")):
        return "food"
    return ""


def _reco_block_links(body: str) -> list:
    """提取区块内的商户链接 [(标题, 链接)]，排除图片与房型链接"""
    out = []
    for m in _RECO_LINK_RE.finditer(body or ""):
        title, url = m.group(1).strip(), m.group(2)
        if not re.search(r'(dpurl\.cn|meituan\.com|dianping\.com)', url):
            continue
        if re.search(r'\.(jpg|jpeg|png|webp|gif)', url, re.I):
            continue
        if re.search(r'(meituan\.net|dpfile\.com|autonavi\.com)', url, re.I):
            continue
        out.append((_reco_plain(title).strip(), url))
    return out


def _reco_generic_only(name: str) -> bool:
    """名称是否完全由修饰性词汇构成（剥离全部泛称词后不剩任何专名成分）"""
    s = name or ""
    for t in _RECO_GENERIC_TOKENS:
        s = s.replace(t, "")
    return not s.strip(' ·-—•、')


def _reco_has_lodge_identity(name: str) -> bool:
    """名称能否指认到具体住宿主体（有住宿业态后缀或"××店"式分店括注）"""
    n = name or ""
    return bool(_RECO_LODGE_RE.search(n) or _RECO_BRANCH_RE.search(n))


def _reco_is_generic_name(name: str, key: str) -> bool:
    """判断名称是否只是类目泛称而非真实商户名（美团各区块的"主题式小标题"常见）"""
    n = _RECO_ORDINAL_RE.sub('', (name or '').strip()).strip(' ·-—')
    if not n:
        return True
    # 先排除纯修饰语堆砌的假名（"经典老字号""特色小吃"），否则会因结尾"号/店"被误判成真实商户
    if _reco_generic_only(n) or _RECO_GENERIC_PHRASE_RE.search(n):
        return True
    if key == "hotel":
        # 酒店组多一道硬判据：指认不到具体住宿主体的（"区域·卖点"式标题）一律当泛称，
        # 交给高德按 POI 补真实商户
        return not _reco_has_lodge_identity(n)
    if key != "food":
        return False
    if _RECO_BRAND_RE.search(n):        # 带品牌/门店特征，判定为真实商户名
        return False
    if any(c in n for c in '与和及、'):
        return True
    return len(n) < 7


def _reco_block_name(title: str, links: list, key: str) -> str:
    """确定卡片名称：美团商旅类区块标题常是主题词，真实商户名在链接标题里"""
    for t, _u in links:
        if not t or len(t) > 40 or _RECO_ROOM_RE.search(t):
            continue
        if any(w in t for w in _RECO_NON_TRAVEL):
            continue
        return t
    return title


def _reco_money(text: str, key: str) -> str:
    """从文本中提取价格：酒店取『￥xx起/晚』，餐饮取『人均 xx』，景点取门票价"""
    if not text:
        return ""
    if key == "hotel":
        m = re.search(r'[¥￥]\s*([0-9][0-9Xx]{0,4})\s*起', text)
        if m:
            return m.group(1)
    if key == "food":
        m = re.search(r'人均[^\d]{0,6}([0-9]{1,4})', text)
        if m:
            return m.group(1)
        m = re.search(r'[¥￥]\s*([0-9]{1,4})\s*[/／]?\s*人', text)
        if m:
            return m.group(1)
    m = re.search(r'成人[票]?[^\d]{0,6}([0-9]{1,4})\s*元', text)
    if m:
        return m.group(1)
    m = re.search(r'([0-9]{1,5})\s*元', text)
    if m:
        return m.group(1)
    m = re.search(r'[¥￥]\s*([0-9][0-9Xx]{0,4})', text)
    if m:
        return m.group(1)
    return ""


def _reco_price_label(key: str, price: str, raw: str = "") -> str:
    if price:
        if key == "hotel":
            return "¥%s 起/晚" % price
        if key == "food":
            return "人均 ¥%s" % price
        return "¥%s 起" % price
    if raw and "免费" in raw:
        return "免费"
    return ""


def _reco_reviews(plain: str, key: str, desc: str = "") -> list:
    """从数据源原文里抽取评价要点句（真实文本片段，非模型编造）"""
    out = []
    skip = ('地址', '电话', '门票价格', '团购链接', '起/晚', '房型', '人均', '小贴士', '『')
    for sent in re.split(r'[。！!；;\n]', plain or ""):
        s = sent.strip(' 　*·-—|[]')
        s = re.sub(r'^[^\s：:]{2,7}[：:]\s*', '', s).strip()   # 去掉"特色亮点："等字段前缀
        if len(s) < 6 or len(s) > 60:
            continue
        if any(k in s for k in skip):
            continue
        # 描述句已单独展示，不再重复进评价列表
        if desc and (s in desc or desc in s or s[:12] == desc[:12]):
            continue
        if any(s[:14] == o[:14] for o in out):
            continue
        if any(k in s for k in ('评分', '必吃榜', '当地人推荐', '推荐', '性价比', '环境', '服务',
                                '味道', '适合', '不错', '宽敞', '安静', '特色', '免费', '便捷',
                                '方便', '划算', '近')):
            out.append(s)
        if len(out) >= 3:
            break
    return out


def _reco_sentences(body: str) -> list:
    """抽取区块内的描述性句子，过滤图片行/房型行/字段行"""
    out = []
    junk = ('地址', '电话', '门票价格', '团购链接', '起/晚', '房型', '人均',
            '㎡', '入住', '限时免费取消', '立即确认', '床', '订', '开业')
    for para in (body or "").split("\n"):
        if not para.strip() or "![" in para:
            continue
        cand = _reco_plain(para)
        if any(k in cand for k in junk):
            continue
        for s in re.split(r'[。！!；;]', cand):
            s = s.strip(' 　*·-—|[]')
            if 8 <= len(s) <= 60:
                out.append(s)
    return out


def _reco_parse_meituan(answer: str, key: str) -> list:
    """把美团返回的 Markdown 解析成卡片条目"""
    items = []
    for title, body in _reco_split_blocks(answer):
        blob = title + "\n" + body
        cls = _reco_classify(blob)
        if cls and cls != key:
            continue
        plain = _reco_plain(body)
        links = _reco_block_links(body)
        name = _reco_block_name(title, links, key).strip()
        if len(name) < 2 or any(w in name for w in _RECO_NON_TRAVEL):
            continue
        if _reco_is_generic_name(name, key):
            continue
        if any(i["name"] == name for i in items):
            continue

        images = [u for u in _RECO_IMAGE_RE.findall(body)
                  if re.search(r'(meituan\.net|dpfile\.com|meituan\.com)', u, re.I)]
        dm = _RECO_DESC_RE.search(plain)
        if dm:
            desc = dm.group(1).strip()
            review_pool = plain
        else:
            # 酒店/餐饮类无『特色亮点』字段：首句作为描述（位置/定位），其余句子作为评价要点
            sents = _reco_sentences(body)
            desc = sents[0] if sents else ""
            review_pool = "。".join(sents[1:])

        am = _RECO_ADDR_RE.search(plain)
        tm2 = _RECO_TEL_RE.search(plain)
        rm = _RECO_RATING_RE.search(plain)
        rating = rm.group(1) if rm else ""
        addr = am.group(1).strip() if am else ""
        tel = tm2.group(1).strip(' 、,，') if tm2 else ""
        price = _reco_money(plain, key)
        # 信息量门槛：既无价格/地址/电话/链接/图片，又没有评分支撑的条目直接丢弃，
        # 避免把美团文案里的主题小标题（如"特色小吃"）渲染成推荐卡片
        if not any([price, addr, tel, links, images]) and not (rating and len(desc) >= 10):
            continue
        items.append({
            "name": name,
            "type": _RECO_TYPE_LABEL[key],
            "group": key,
            "desc": desc[:120],
            "price": price,
            "price_label": "",
            "address": addr,
            "phone": tel,
            "rating": rating,
            "reviews": _reco_reviews(review_pool, key, desc),
            "image": images[0] if images else "",
            "image_missing": not images,
            "image_from": "美团" if images else "",
            "link": (links[0][1] if links else ""),
            "sources": ["美团"],
            "amap_done": False,
        })
    return items


def _reco_amap_get(params: dict) -> dict:
    """高德 POI 接口统一入口：串行节流 + 失败重试。

    个人 Key 有 QPS 限制，卡片生成会并发补全多条 POI（餐饮兜底 + 地址/电话/图片补齐），
    不加节流会被高德拒绝返回（CUQPS_HAS_EXCEEDED_THE_LIMIT），表现为整组卡片"暂无数据"。
    """
    for attempt in range(3):
        with _reco_amap_lock:
            gap = time.time() - _reco_amap_last[0]
            if gap < 0.16:
                time.sleep(0.16 - gap)
            _reco_amap_last[0] = time.time()
            try:
                return requests.get("https://restapi.amap.com/v3/place/text",
                                    params=params, timeout=8).json()
            except Exception:
                pass
        time.sleep(0.25 * (attempt + 1))
    return {}


def _reco_amap_poi(name: str, city: str, key: str = "") -> dict:
    """高德 POI 检索单个地点，带名称相似度校验与多写法重试，避免错配到无关 POI

    key 为所属类目时，额外用 _reco_type_ok 校验业态，防止补全时把民宿当餐厅之类的错配。
    """
    if not _amap_key or not name:
        return {}
    import difflib

    def _query(kw):
        data = _reco_amap_get({"key": _amap_key, "keywords": kw, "city": city or "",
                               "offset": 3, "extensions": "all"})
        return (data.get("pois") or []) if data.get("status") == "1" else []

    # 商户全称常带分店括注或 · 分隔，直接检索容易失配，按"由全到简"依次尝试
    variants = [name]
    base = re.sub(r'[（(][^）)]*[）)]', '', name).strip()
    if base and base != name:
        variants.append(base)
    for part in re.split(r'[·•|｜]', base or name):
        part = part.strip()
        if len(part) >= 3 and part not in variants:
            variants.append(part)

    best, best_score = {}, 0.0
    for kw in variants:
        for poi in _query(kw)[:3]:
            pname = poi.get("name", "") or ""
            ptype = poi.get("type", "") or ""
            if any(k in ptype for k in ("购物", "超市", "家居", "建材", "停车场", "加油站")):
                continue
            if key and not _reco_type_ok(pname, ptype, key):
                continue
            score = difflib.SequenceMatcher(None, name, pname).ratio()
            if name in pname or pname in name:
                score = max(score, 0.75)
            if base and (base in pname or pname in base):
                score = max(score, 0.7)
            biz = poi.get("biz_ext") or {}
            rich = sum(1 for v in (poi.get("address"), poi.get("tel"), poi.get("photos"),
                                   biz.get("rating") if isinstance(biz.get("rating"), str) else "")
                       if v)
            score += rich * 0.02        # 同分时字段更全的 POI 优先（评分/电话/图片）
            if score > best_score:
                best, best_score = poi, score
        if best_score >= 0.75 and isinstance((best.get("biz_ext") or {}).get("rating"), str) \
                and (best.get("biz_ext") or {}).get("rating"):
            break                        # 已命中高置信且带评分的 POI，不再换词重试
    return best if best_score >= 0.5 else {}


def _reco_amap_fill(item: dict, city: str, poi: dict) -> None:
    """用高德 POI 补齐缺失字段并写入坐标（只填空缺，不覆盖美团已有数据）"""
    if not poi:
        return
    biz = poi.get("biz_ext") or {}

    def _one(v):
        if isinstance(v, list):
            return str(v[0]) if v else ""
        return str(v or "")

    if not item.get("address"):
        item["address"] = _one(poi.get("address"))
    if not item.get("phone"):
        item["phone"] = _one(poi.get("tel")).strip(' 、,，')
    if not item.get("rating"):
        item["rating"] = _one(biz.get("rating"))
    if not item.get("price"):
        cost = _one(biz.get("cost"))
        if cost:
            item["price"] = cost.split(".")[0]
    if not item.get("image"):
        photos = poi.get("photos") or []
        url = (photos[0].get("url") or "") if photos else ""
        if not url.startswith("http"):
            url = ("https:" + url) if url.startswith("//") else ""
        if url:
            item["image"] = url
            item["image_from"] = "高德"
    loc = poi.get("location", "") or ""
    if "," in loc:
        try:
            item["lng"], item["lat"] = float(loc.split(",")[0]), float(loc.split(",")[1])
        except Exception:
            pass
    item["amap_done"] = True


def _reco_amap_items(city: str, key: str, exclude: list) -> list:
    """高德 POI 按类目检索，作为美团之外的补充来源"""
    if not _amap_key:
        return []
    try:
        data = _reco_amap_get({"key": _amap_key, "types": _RECO_AMAP_TYPES.get(key, ""),
                               "city": city, "citylimit": "true", "offset": 8,
                               "extensions": "all", "sortrule": "weight"})
    except Exception:
        return []
    if data.get("status") != "1":
        return []
    out = []
    for poi in (data.get("pois") or []):
        name = (poi.get("name") or "").strip()
        ptype = poi.get("type") or ""
        if not name or len(name) < 2:
            continue
        # 高德按大类码检索时会混入近邻业态（民宿/客栈同时挂"餐饮服务"标签），
        # 见 _RECO_TYPE_MUST 处的说明：必须按全部主类型校验，只认真正的本类目 POI
        if not _reco_type_ok(name, ptype, key):
            continue
        if any(k in name for k in _RECO_NON_TRAVEL) or any(k in ptype for k in ("购物", "超市", "家居", "建材")):
            continue
        if any(name in e or e in name for e in exclude) or any(i["name"] == name for i in out):
            continue
        biz = poi.get("biz_ext") or {}

        def _one(v):
            if isinstance(v, list):
                return str(v[0]) if v else ""
            return str(v or "")

        area = _one(poi.get("adname"))
        biz_area = poi.get("business_area") or ""
        if biz_area and biz_area not in area:
            area = (area + biz_area)
        tail = ptype.split(";")[-1] if ptype else ""
        item = {
            "name": name,
            "type": _RECO_TYPE_LABEL[key],
            "group": key,
            "desc": "位于%s的%s" % (area, tail) if (area and tail) else (area or tail),
            "price": "",
            "price_label": "",
            "address": _one(poi.get("address")),
            "phone": _one(poi.get("tel")).strip(' 、,，'),
            "rating": _one(biz.get("rating")),
            "reviews": [],
            "image": "",
            "image_missing": True,
            "image_from": "",
            "link": "",
            "sources": ["高德"],
            "amap_done": True,
        }
        if key == "food":
            cost = _one(biz.get("cost"))
            if cost:
                item["price"] = cost.split(".")[0]
        _reco_amap_fill(item, city, poi)
        out.append(item)
    # 高德按综合权重返回，字段贫瘠的 POI（社区小广场、村级活动场所之类）常排在前列，
    # 按字段完整度重排，让有评分/图片/电话的条目优先进入卡片
    def _rank(it):
        score = _reco_richness(it)
        if key == "scenic" and _RECO_SCENIC_WEAK_RE.search(it.get("name") or ""):
            score -= 3
        return -score

    out.sort(key=_rank)
    return out


def _reco_local_items(city: str, key: str, exclude: list) -> list:
    """本地知识库兜底（仅覆盖少量城市，数据来源已在库内标注）"""
    try:
        from plugins.travel_data import LOCAL_KNOWLEDGE
    except Exception:
        return []
    data = LOCAL_KNOWLEDGE.get(city)
    if not data:
        return []
    out = []

    def _push(name, desc, price, raw, rating, address, highlights):
        name = (name or "").strip()
        if not name or any(name in e or e in name for e in exclude):
            return
        if any(i["name"] == name for i in out):
            return
        out.append({
            "name": name, "type": _RECO_TYPE_LABEL[key], "group": key,
            "desc": desc[:120], "price": price, "price_label": "", "address": address,
            "phone": "", "rating": str(rating or ""), "reviews": highlights[:3],
            "image": "", "image_missing": True, "image_from": "", "link": "",
            "sources": ["本地知识库"], "amap_done": False,
        })

    if key == "scenic":
        for s in data.get("scenic_spots", []):
            ticket = s.get("ticket", "") or ""
            hl = [x for x in (s.get("highlights") or []) if "元" not in x][:3]
            _push(s.get("name"), "%s · 建议游玩 %s · %s" % (s.get("level", ""), s.get("duration", ""), "、".join(hl)),
                  _reco_money(ticket, "scenic"), ticket, "", "", hl)
    elif key == "food":
        for f in data.get("food", []):
            _push(f.get("name"), "%s · 招牌菜：%s" % (f.get("type", ""), "、".join((f.get("must_try") or [])[:3])),
                  str(f.get("avg_price") or ""), "", f.get("rating"), "", (f.get("must_try") or []))
    else:
        for h in data.get("hotels", []):
            nums = re.findall(r'\d+', h.get("price_range", "") or "")
            _push(h.get("name"), "%s · %s" % ("⭐" * int(h.get("stars") or 0), "、".join((h.get("features") or [])[:3])),
                  (nums[0] if nums else ""), h.get("price_range", ""), h.get("rating"),
                  h.get("location", ""), (h.get("features") or [])[:3])
    return out


def _reco_booking_link(city: str, name: str, link: str) -> tuple:
    """返回 (链接, 类型)。有美团商户深链就直连，否则回落到美团搜索页"""
    from urllib.parse import quote
    if link and re.search(r'(dpurl\.cn|meituan\.com|dianping\.com)', link) \
            and not re.search(r'\.(jpg|jpeg|png|webp)', link, re.I):
        return link, "deeplink"
    return "https://i.meituan.com/search/?w=" + quote((city or "") + " " + name), "search"


def _reco_finalize(item: dict, city: str) -> dict:
    """补齐高德字段 → 生成价格文案/购票链接/导航链接，并清理内部字段"""
    if not item.get("amap_done"):
        _reco_amap_fill(item, city,
                        _reco_amap_poi(item.get("name", ""), city, item.get("group", "")))
    url, kind = _reco_booking_link(city, item.get("name", ""), item.get("link", ""))
    lng, lat = item.get("lng"), item.get("lat")
    nav = ""
    if lng and lat:
        from urllib.parse import quote
        nav = ("https://uri.amap.com/marker?position=%s,%s&name=%s&src=smarttourism"
               % (lng, lat, quote(item.get("name", ""))))
    sources = [s for s in (item.get("sources") or []) if s]
    item["price_label"] = _reco_price_label(item.get("group", ""), item.get("price", ""))
    item["booking_url"] = url
    item["booking_kind"] = kind
    item["nav_url"] = nav
    item["image_missing"] = not bool(item.get("image"))
    item["source"] = sources[0] if sources else ""
    item["source_detail"] = " + ".join(sources)
    if item.get("image_from") and item["image_from"] not in item["source_detail"]:
        item["source_detail"] += "（图片来自%s）" % item["image_from"]
    for k in ("amap_done", "link", "sources", "lng", "lat"):
        item.pop(k, None)
    return item


def _reco_seed_blocks(answer: str, key: str) -> str:
    """从对话中已取得的美团原文里，按类目切出可复用的区块"""
    out = []
    for title, body in _reco_split_blocks(answer):
        if _reco_classify(title + "\n" + body) == key:
            out.append("## " + title + "\n" + body)
    return "\n\n".join(out)


def _reco_meituan_answer(city: str, key: str, tpl: str) -> str:
    """取美团原文：同城缓存 → 对话中已取得的插件原文 → 实时查询"""
    ck = (city, key)
    now = time.time()
    hit = _reco_mt_cache.get(ck)
    if hit and now - hit["ts"] < _RECO_TTL:
        return hit["answer"]
    seed = _reco_seed_cache.get(city)
    if seed and now - seed["ts"] < 600:
        blocks = _reco_seed_blocks(seed["answer"], key)
        if blocks:
            _reco_mt_cache[ck] = {"ts": now, "answer": blocks}
            return blocks
    meituan = registry.get("meituan")
    if not (meituan and meituan.is_configured()):
        return ""
    try:
        r = meituan.execute({"city": city or "北京", "query": tpl.format(city=city)})
    except Exception as e:
        print(f"[Reco] meituan {key} err: {e}")
        return ""
    if r.get("status") != "ok":
        print(f"[Reco] meituan {key}: {r.get('message', '')}")
        return ""
    answer = str(r.get("answer") or "")
    _reco_mt_cache[ck] = {"ts": now, "answer": answer}
    return answer


def _reco_detect_city(text: str) -> str:
    for c in _RECO_CITY_LIST:
        if c in (text or ""):
            return c
    return ""


def _reco_build_cards(city: str) -> dict:
    """组装推荐卡片：景点 → 美食 → 酒店，每类最多 3 张"""
    now = time.time()
    cached = _reco_cards_cache.get(city)
    if cached and now - cached["ts"] < _RECO_TTL:
        return cached["payload"]

    from concurrent.futures import ThreadPoolExecutor
    answers = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {key: pool.submit(_reco_meituan_answer, city, key, tpl)
                for key, _label, tpl in _RECO_GROUP_ORDER}
        for key, fut in futs.items():
            try:
                answers[key] = fut.result(timeout=45)
            except Exception as e:
                answers[key] = ""
                print(f"[Reco] meituan {key} timeout: {e}")

    groups, notices, source_total = [], [], {"美团": 0, "高德": 0, "本地知识库": 0}
    for key, label, _tpl in _RECO_GROUP_ORDER:
        items = _reco_parse_meituan(answers.get(key, ""), key)
        if len(items) < _RECO_PER_GROUP:
            items.extend(_reco_amap_items(city, key, [i["name"] for i in items]))
        if len(items) < _RECO_PER_GROUP:
            items.extend(_reco_local_items(city, key, [i["name"] for i in items]))
        items = items[:_RECO_PER_GROUP]
        if not items:
            notices.append("「%s」暂未获取到可用数据" % label)
            continue
        with ThreadPoolExecutor(max_workers=min(6, len(items))) as pool:
            items = list(pool.map(lambda it: _reco_finalize(it, city), items))
        for it in items:
            source_total[it.get("source", "")] = source_total.get(it.get("source", ""), 0) + 1
        srcs = {it.get("source", "") for it in items}
        if srcs == {"美团"}:
            note = "美团实时数据"
        elif "美团" in srcs:
            note = "美团实时数据 + 高德地图补齐"
        elif "高德" in srcs:
            note = "高德地图商户数据（价格/评分来自高德，购票跳转美团）"
        else:
            note = "本地知识库（非实时，仅供行程参考）"
        groups.append({"key": key, "label": label, "items": items, "source_note": note})

    payload = {
        "success": True,
        "city": city,
        "groups": groups,
        "notices": notices,
        "source_summary": source_total,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "disclaimer": "价格与库存来自美团实时查询，以下单页面为准；地址/电话/图片部分由高德地图补齐。",
    }
    _reco_cards_cache[city] = {"ts": now, "payload": payload}
    return payload


@app.route('/api/recommend/cards', methods=['POST'])
def recommend_cards():
    """对话结尾推荐卡片接口：景点 → 美食 → 酒店（美团 > 高德 > 本地知识库）

    请求: {"city": "温州", "content": "AI回复全文（可选，用于兜底识别城市）"}
    响应: {"success": true, "city": "温州", "groups": [{"key","label","items":[...]}]}
    """
    body = request.get_json(force=True, silent=True) or {}
    city = (body.get('city') or '').strip()
    if not city:
        city = _reco_detect_city(str(body.get('content') or body.get('message') or ''))
    if not city:
        return jsonify({"success": False, "error": "缺少城市信息", "groups": []})
    try:
        return jsonify(_reco_build_cards(city))
    except Exception as e:
        app.logger.warning("recommend_cards failed: %s", traceback.format_exc()[-500:])
        return jsonify({"success": False, "error": str(e)[:200], "groups": []})


@app.route('/api/map/geocode', methods=['POST'])
def map_geocode():
    """地理编码代理 — 使用 Web 服务 Key 调用高德地理编码 API"""
    data = request.json or {}
    address = data.get('address', '')
    city = data.get('city', '')
    if not address:
        return jsonify({"status": "error", "message": "address 参数必填"}), 400
    if not _amap_key:
        return jsonify({"status": "error", "message": "高德 Web 服务 Key 未配置"}), 500
    try:
        resp = requests.get('https://restapi.amap.com/v3/geocode/geo', params={
            'key': _amap_key,
            'address': address,
            'city': city
        }, timeout=8)
        d = resp.json()
        if d.get('status') == '1' and d.get('geocodes') and len(d['geocodes']) > 0:
            loc = d['geocodes'][0]['location'].split(',')
            return jsonify({
                'status': 'ok',
                'lng': float(loc[0]),
                'lat': float(loc[1]),
                'formatted_address': d['geocodes'][0].get('formatted_address', ''),
                'level': d['geocodes'][0].get('level', '')
            })
        return jsonify({'status': 'error', 'message': d.get('info', '地理编码失败')}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/plugins/config', methods=['POST'])
def save_plugin_config_api():
    """保存插件配置（如高德Key等）"""
    data = request.json
    if not data:
        return jsonify({"success": False, "error": "无效数据"}), 400

    config = load_plugin_config()
    for key, value in data.items():
        config[key] = value
    save_plugin_config(config)

    # 更新已注册插件的配置
    for plugin in registry.list_all():
        if hasattr(plugin, 'amap_key'):
            plugin.amap_key = config.get('amap_key', '')
            plugin.config['amap_key'] = config.get('amap_key', '')
        if hasattr(plugin, 'token'):
            plugin.token = config.get('meituan_token', '')
            plugin.config['meituan_token'] = config.get('meituan_token', '')

    masked = {}
    for k, v in config.items():
        if 'key' in k.lower() or 'secret' in k.lower():
            masked[k] = v[:4] + '****' + v[-4:] if len(v) > 8 else '****'
        else:
            masked[k] = v
    return jsonify({"success": True, "config": masked})


@app.route('/api/health', methods=['GET'])
def health():
    """健康检查"""
    try:
        cfg = validate_config()
        config_section = {
            "complete": cfg["complete"],
            "missing_required": cfg["missing_required"],
            "warnings": cfg["warnings"],
        }
    except Exception:
        config_section = {"complete": None, "missing_required": [], "warnings": []}
    return jsonify({
        "status": "ok",
        "service": "SmartTourism",
        "version": "v2.2.0",
        "environment": APP_ENV,
        "debug": DEBUG_ENABLED,
        "skills_loaded": list(SKILL_DEFINITIONS.keys()),
        "crowds_loaded": list(CROWD_PROFILES.keys()) if CROWD_PROFILES else [],
        "plugins_loaded": len(registry.list_all()),
        "plugins_configured": len(registry.list_configured()),
        "has_api_key": bool(API_CONFIG["api_key"]),
        "has_amap_key": bool((_amap_key or "").strip()),
        "provider": PROVIDERS.get(API_CONFIG["provider"], {}).get("name", "未配置"),
        "config": config_section,
        "timestamp": datetime.now().isoformat()
    })


# ========== 运维概览（便于后台发现问题与维护）==========

def _ops_allowed() -> bool:
    """运维接口访问控制：仅限本机回环，或设置了 SMARTTOURISM_ADMIN_TOKEN 且携带正确 token。"""
    ip = request.remote_addr or ''
    if ip in ('127.0.0.1', '::1') or ip.startswith('127.'):
        return True
    token = os.getenv('SMARTTOURISM_ADMIN_TOKEN', '')
    return bool(token) and request.args.get('token') == token


# ==================== Agent 能力清单：已接线 / 建好未接线，如实分开 ====================
# 写这张表是为了「不吹」：说得出 Agent 能力的地方，得能一眼看出哪些真在请求路径上跑，
# 哪些只有实现和测试、/api/chat 还没走它。已接线项的状态取自运行时真实信号（feature flag +
# 最近一次装配结果），不是手写的常量；未接线项用 sys.modules 兜底——将来谁把它接上了，
# 这里会自动从「未接线」翻成「已被导入」，表不会悄悄过期成假广告。
_AGENT_UNWIRED = [
    ("graph", "LangGraph StateGraph 编排（含 SQLite checkpointer）",
     "/api/chat 仍走 app.py 的 ReAct 单链路；接进来要多 2-3 次模型调用，延迟与成本待产品决策"),
    ("multi_agent", "规划 / 执行 / 审校 多 Agent 协作", "依赖同一编排层，与 graph 一起决策"),
    ("hitl", "Human-in-the-loop 中断确认", "当前的二次确认在前端做，未接 LangGraph interrupt"),
    ("tools", "Tool / Function-Calling 抽象层",
     "只被 graph 与 mcp_server 使用；两者未接线，整条工具链因此不在请求路径上"),
    ("mcp_server", "标准 MCP Server（对外暴露工具）", "可独立运行与测试，未接进对话请求"),
    ("model_router", "按任务难度路由模型 + 成本核算",
     "现在统一用配置里的主模型；换路由会同时改变计费与质量表现，待决策"),
    ("guardrails", "输入/输出护栏（含提示注入检测）",
     "反幻觉靠提示词约束 + 事后校验，该模块本身未进请求路径"),
    ("evaluation", "回复质量自动评估",
     "内置用例集仍是离线跑批；规则指标已额外读库里的真实答复出质量指标"
     "（见 /api/admin/ops 的 answer_quality），不进 /api/chat 请求路径"),
    ("tracing", "调用链路追踪", "已由 request_id + app.log + /api/admin/ops 覆盖主要排查需求"),
    ("structured_output", "Pydantic 结构化输出", "行程骨架走本地参数校验，未走该模块"),
    ("graphrag", "知识图谱检索", "知识库检索走 agent.rag_hybrid（BM25+向量），图谱版未接入"),
]


def _agent_module_status() -> dict:
    """逐项说明每个 Agent 模块在「本进程的真实请求路径」里处于什么状态。"""
    import sys as _sys

    wired = []

    # 长期记忆：请求里按需用 _get_memory() 装配，这里读同样的信号，不重复触发 import
    if _MEMORY_MOD is not None:
        state, detail = "active", "每轮召回注入 + 答复后后台沉淀"
    elif not MEMORY_ENABLED:
        state, detail = "disabled", "SMARTTOURISM_MEMORY=0 显式关闭"
    elif _MEMORY_IMPORT_FAILED:
        state, detail = "unavailable", "模块导入失败（详见 app.log 的「长期记忆模块不可用」）"
    else:
        state, detail = "wired_idle", "已接线，本进程还没有请求触发装配"
    wired.append({"module": "memory", "capability": "跨会话长期记忆（偏好/约束沉淀与召回）",
                  "state": state, "detail": detail})

    # 语义缓存：装配很重（要建 embedder），这里只看现状，绝不为观测去触发它
    if _llm_cache_wrapper is not None:
        state, detail = "active", "call_llm 命中缓存则不再发起网络调用"
    elif not LLM_CACHE_ENABLED:
        state, detail = "disabled", "SMARTTOURISM_LLM_CACHE=0 显式关闭"
    elif _llm_cache_failed:
        state, detail = "unavailable", "缓存不可用，call_llm 已改为直接透传"
    else:
        state, detail = "wired_idle", "已接线，尚未有请求触发装配"
    wired.append({"module": "semantic_cache", "capability": "语义相似度缓存（省 Token 省时间）",
                  "state": state, "detail": detail})

    rag = validation_stats().get("rag", {})
    if rag.get("build_failed"):
        state, detail = "degraded", "检索器构建失败，本轮起永久跳过知识库检索"
    elif rag.get("retriever"):
        state, detail = "active", f"检索器就绪（embedder={rag['retriever']}）"
    else:
        state, detail = "wired_idle", "已接线，还没有请求触发构建"
    wired.append({"module": "rag_hybrid", "capability": "本地知识库混合检索（BM25+向量）",
                  "state": state, "detail": detail})

    unwired = []
    for name, capability, reason in _AGENT_UNWIRED:
        loaded = f"agent.{name}" in _sys.modules
        unwired.append({"module": name, "capability": capability,
                        "state": "imported" if loaded else "not_wired",
                        "detail": reason})
    return {
        "wired": wired,
        "unwired": unwired,
        "counts": {"wired": len(wired), "unwired": len(unwired),
                   "imported_but_unwired": sum(1 for u in unwired if u["state"] == "imported")},
    }


def _answer_quality_stats(sample_limit: int = 30) -> dict:
    """答复反馈 + 把规则指标跑在**库里真实发出去的答复**上，两边对照着看。

    内置评估集回答「改提示词后有没有回退」，这里回答「用户这几天看到的答复里有多少
    踩了违规词 / 报了没来源的价格 / 是半截的」。纯离线、零 API 调用，只在运维面板
    请求时算一次，样本限定最近 sample_limit 条，不整库扫。
    库里没数据就照实 sample_size=0——不拿「平均分 1.0」冒充「质量很好」。
    """
    rows = db.recent_agent_answers(limit=sample_limit)
    try:
        from agent import evaluation as _ev
    except Exception as e:  # noqa: BLE001
        return {"error": f"评估模块不可用: {e}", "sample_size": len(rows),
                "feedback": db.feedback_stats()}

    samples = []
    for r in rows:
        samples.append({
            "message_id": r.get("message_id"),
            "session_id": r.get("session_id"),
            "question": r.get("question"),
            "truncated": r.get("truncated"),
            "has_sources": r.get("has_sources"),
            "rating": r.get("rating"),
            "score": _ev.score_live_answer(r.get("answer") or "", r.get("question") or "",
                                           r.get("intent_type") or "consult"),
        })
    out = _ev.aggregate_live_scores(samples)
    out["feedback"] = db.feedback_stats()
    out["sample_source"] = f"最近 {len(rows)} 条已落库 agent 答复"
    return out


@app.route('/api/admin/ops', methods=['GET'])
def admin_ops():
    """运维指标 JSON：请求量/错误率/各接口耗时/最近错误 + 记忆与缓存运行态。"""
    if not _ops_allowed():
        return _json_error("运维接口仅限本机或管理员访问", 403, "forbidden")
    snap = dict(OPS.snapshot())
    try:
        snap['llm_cache'] = _llm_cache_stats()
    except Exception as e:  # noqa: BLE001
        snap['llm_cache'] = {'error': str(e)}
    try:
        mem = _get_memory()
        snap['long_term_memory'] = ({'enabled': False} if mem is None
                                    else {'enabled': True, 'default_user': mem.stats('default')})
    except Exception as e:  # noqa: BLE001
        snap['long_term_memory'] = {'error': str(e)}
    try:
        snap['session_memory'] = {
            'cached_conversations': len(conversations),
            'summaries_in_memory': summary_cache_size(),
        }
    except Exception:
        pass
    try:
        # DAO 降级计数：库在悄悄坏掉时（磁盘满/被独占）这里先看出来，而不是等对话开始丢历史
        snap['database'] = {'ready': DB_READY, 'path': db.get_db_path(),
                            'init_error': DB_ERROR, **db.db_health()}
    except Exception as e:  # noqa: BLE001
        snap['database'] = {'error': str(e)}
    try:
        # 反幻觉校验降级计数：校验是保险丝，烧了不该让答复消失，但必须在这里看得见
        snap['validation'] = validation_stats()
    except Exception as e:  # noqa: BLE001
        snap['validation'] = {'error': str(e)}
    try:
        # 流式中断分开看：用户主动「停止生成」与客户端断连（关页面/切会话）是两种信号
        snap['chat_streams'] = stream_stats()
    except Exception as e:  # noqa: BLE001
        snap['chat_streams'] = {'error': str(e)}
    try:
        # 答复反馈 + 真实答复的规则指标：用户怎么说，和规则怎么判，放在一起对照
        snap['answer_quality'] = _answer_quality_stats()
    except Exception as e:  # noqa: BLE001
        snap['answer_quality'] = {'error': str(e)}
    snap['degradation'] = {'log_file_ok': LOG_FILE_OK}
    try:
        # 模型用量与耗时：答辩要报运行成本，数得是从真实调用里累出来的
        snap['llm_usage'] = _llm_usage_stats()
    except Exception as e:  # noqa: BLE001
        snap['llm_usage'] = {'error': str(e)}
    try:
        # 生图用量与复用率：Seedream 按张计费，省下的张数也要是能报数的
        snap['image_usage'] = _ark_usage_stats()
    except Exception as e:  # noqa: BLE001
        snap['image_usage'] = {'error': str(e)}
    try:
        # Agent 能力清单：已接线的看真实状态，建好未接线的照实标「未接线」
        snap['agent_modules'] = _agent_module_status()
    except Exception as e:  # noqa: BLE001
        snap['agent_modules'] = {'error': str(e)}
    return jsonify(snap)


@app.route('/admin', methods=['GET'])
def admin_page():
    """运维概览面板（零依赖内联页面，每 5 秒自刷新）。"""
    if not _ops_allowed():
        return _json_error("运维接口仅限本机或管理员访问", 403, "forbidden")
    return Response(render_admin_html(), mimetype='text/html')


# ╔══════════════════════════════════════════════════════════════╗
# ║  💬 AI 对话 API                                              ║
# ╚══════════════════════════════════════════════════════════════╝

# ========== 会话 API ==========

@app.route('/api/sessions', methods=['POST'])
def create_session_api():
    """创建新会话"""
    data = request.json or {}
    user_id = data.get('user_id', 'default')
    session = db.create_session(user_id=user_id)
    return jsonify({"success": True, "session": session})


@app.route('/api/sessions', methods=['GET'])
def list_sessions_api():
    """列出用户的所有会话（含标题/条数/末条摘要，供历史对话列表使用）

    user_id 走 _resolve_uid：登录用户以令牌为准，避免带上显式参数就看到别人的会话列表。
    """
    user_id = _resolve_uid(request.args.get('user_id'))
    limit = request.args.get('limit', 50, type=int)
    limit = max(1, min(limit, 200))
    sessions = db.list_user_sessions(user_id=user_id, limit=limit)
    return jsonify({"success": True, "sessions": sessions, "user_id": user_id})


@app.route('/api/sessions/<session_id>', methods=['GET'])
def get_session_api(session_id):
    """获取会话信息"""
    session = db.get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"success": True, "session": session})


@app.route('/api/sessions/<session_id>/messages', methods=['GET'])
def get_session_messages_api(session_id):
    """拉取会话历史消息（附带当前用户在这些答复上投过的票，回放时反馈状态不丢）"""
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    messages = db.get_session_messages(session_id, limit=limit, offset=offset)
    # 票读不出来不影响历史本身（历史才是主角），但降级要留痕，不能悄悄变成「你没投过票」
    votes = _db_call("get_feedback_map", db.get_feedback_map, session_id, _resolve_uid(), default={}) or {}
    for m in messages:
        v = votes.get(m.get("message_id"))
        if v:
            m["feedback_rating"] = v["rating"]
            m["feedback_reason"] = v["reason"]
    return jsonify({"success": True, "messages": messages})


@app.route('/api/sessions/<session_id>/summary', methods=['PUT'])
def update_session_summary_api(session_id):
    """更新会话历史摘要"""
    data = request.json or {}
    summary = data.get('summary', '')
    db.update_session_summary(session_id, summary)
    return jsonify({"success": True})


@app.route('/api/sessions/<session_id>/title', methods=['PUT'])
def rename_session_api(session_id):
    """会话改名。只允许动自己名下的会话；属用户数据写操作，DB 故障要如实报错而不是假装成功"""
    title = ((request.get_json(silent=True) or {}).get('title') or '').strip()
    if not title:
        return jsonify({"success": False, "error": "标题不能为空"}), 400
    ok = db.rename_session(session_id, _resolve_uid(), title[:60])
    if not ok:
        return jsonify({"success": False, "error": "会话不存在或不属于当前用户"}), 404
    return jsonify({"success": True, "title": title[:60]})


@app.route('/api/sessions/<session_id>', methods=['DELETE'])
def delete_session_api(session_id):
    """删除整条会话（含其消息）。不可恢复，所以按 user_id 匹配才删；
    保留会话只清空内容请走 /api/conversations/<id>。"""
    ok = db.delete_session(session_id, _resolve_uid())
    if not ok:
        return jsonify({"success": False, "error": "会话不存在或不属于当前用户"}), 404
    _conv_cache_drop(session_id)
    clear_summary(session_id)
    try:
        get_fact_tracker().clear(session_id)
    except Exception:
        pass
    app.logger.info("session deleted: %s", session_id)
    return jsonify({"success": True})


# ========== 答复反馈 API ==========

@app.route('/api/feedback', methods=['POST'])
def feedback_api():
    """给某条 agent 答复点赞/点踩；rating 传空或 cancel 表示取消这一票。

    用户主动提交的写操作：DB 出问题要如实回 500，不能回「感谢反馈」把没记下的票说成记下了。
    message_id 对不上真实答复（本轮没落库、会话已删、或前端拿了个假 id）回 404，
    前端据此提示「这条答复没有落库，反馈收不下」，而不是本地假装成功。
    """
    data = request.get_json(silent=True) or {}
    user_id = _resolve_uid(data.get('user_id'))
    message_id = (data.get('message_id') or '').strip()
    session_id = (data.get('conversation_id') or data.get('session_id') or '').strip()
    rating = str(data.get('rating') or '').strip().lower()
    if not message_id or not session_id:
        return jsonify({"success": False, "error": "缺少 message_id / conversation_id"}), 400

    if rating in ('', 'cancel', 'none'):
        removed = db.delete_feedback(message_id, user_id)
        return jsonify({"success": True, "rating": None, "removed": removed})

    res = db.save_feedback(message_id, session_id, user_id, rating, data.get('reason'))
    if not res.get("ok"):
        code = res.get("code")
        if code == "message_not_found":
            return jsonify({"success": False, "code": code,
                            "error": "这条答复没有落库（可能已删除、被中断或生成失败），反馈没有记下"}), 404
        return jsonify({"success": False, "code": code, "error": res.get("error", "反馈提交失败")}), 400
    fb = res["feedback"]
    return jsonify({"success": True, "message_id": fb["message_id"],
                    "rating": fb["rating"], "reason": fb["reason"]})


# ╔══════════════════════════════════════════════════════════════╗
# ║  🗺️  行程方案 API                                            ║
# ╚══════════════════════════════════════════════════════════════╝
# ========== 行程方案 API ==========

@app.route('/api/itineraries', methods=['POST'])
def create_itinerary_api():
    """创建行程方案"""
    data = request.json or {}
    required = ['session_id', 'destination', 'days', 'route_json']
    for field in required:
        if field not in data:
            return jsonify({"error": f"缺少必填字段: {field}"}), 400
    itinerary = db.save_itinerary(
        session_id=data['session_id'],
        destination=data['destination'],
        days=data['days'],
        route_json=json.dumps(data['route_json'], ensure_ascii=False) if isinstance(data['route_json'], dict) else data['route_json'],
        budget=data.get('budget'),
        crowd_type=data.get('crowd_type')
    )
    return jsonify({"success": True, "itinerary": itinerary})


@app.route('/api/itineraries/<itinerary_id>', methods=['GET'])
def get_itinerary_api(itinerary_id):
    """获取行程详情"""
    itinerary = db.get_itinerary(itinerary_id)
    if not itinerary:
        return jsonify({"error": "行程不存在"}), 404
    return jsonify({"success": True, "itinerary": itinerary})


@app.route('/api/itineraries/<itinerary_id>', methods=['PUT'])
def update_itinerary_api(itinerary_id):
    """更新行程（前端拖拽回存）"""
    data = request.json or {}
    if 'route_json' not in data:
        return jsonify({"error": "缺少 route_json 字段"}), 400
    route_json = json.dumps(data['route_json'], ensure_ascii=False) if isinstance(data['route_json'], dict) else data['route_json']
    itinerary = db.update_itinerary(itinerary_id, route_json)
    if not itinerary:
        return jsonify({"error": "行程不存在"}), 404
    return jsonify({"success": True, "itinerary": itinerary})


@app.route('/api/sessions/<session_id>/itineraries', methods=['GET'])
def list_session_itineraries_api(session_id):
    """列出会话的所有行程"""
    itineraries = db.list_session_itineraries(session_id)
    return jsonify({"success": True, "itineraries": itineraries})


# ╔══════════════════════════════════════════════════════════════╗
# ║  📝 营销素材 API                                             ║
# ╚══════════════════════════════════════════════════════════════╝

# ========== 营销素材 API ==========

@app.route('/api/contents', methods=['POST'])
def create_content_api():
    """创建营销素材"""
    data = request.json or {}
    required = ['session_id', 'platform', 'content_type', 'raw_text']
    for field in required:
        if field not in data:
            return jsonify({"error": f"缺少必填字段: {field}"}), 400
    content = db.save_marketing_content(
        session_id=data['session_id'],
        platform=data['platform'],
        content_type=data['content_type'],
        raw_text=data['raw_text'],
        itinerary_id=data.get('itinerary_id'),
        tags=json.dumps(data['tags'], ensure_ascii=False) if isinstance(data.get('tags'), list) else data.get('tags')
    )
    return jsonify({"success": True, "content": content})


@app.route('/api/contents/<content_id>', methods=['GET'])
def get_content_api(content_id):
    """获取营销素材详情"""
    content = db.get_marketing_content(content_id)
    if not content:
        return jsonify({"error": "素材不存在"}), 404
    return jsonify({"success": True, "content": content})


# ╔══════════════════════════════════════════════════════════════╗
# ║  📥 外部导入 API                                             ║
# ╚══════════════════════════════════════════════════════════════╝

# ========== 外部导入 API ==========

@app.route('/api/import', methods=['POST'])
def import_source_api():
    """导入外部内容（链接或文本）"""
    data = request.json or {}
    if 'session_id' not in data:
        return jsonify({"error": "缺少 session_id"}), 400
    source = db.save_imported_source(
        session_id=data['session_id'],
        source_url=data.get('source_url'),
        raw_content=data.get('raw_content')
    )
    return jsonify({"success": True, "source": source})


@app.route('/api/import/<source_id>', methods=['PUT'])
def update_import_api(source_id):
    """更新导入记录状态"""
    data = request.json or {}
    db.update_imported_source(
        source_id=source_id,
        parsed_json=json.dumps(data['parsed_json'], ensure_ascii=False) if isinstance(data.get('parsed_json'), dict) else data.get('parsed_json'),
        status=data.get('status'),
        error_msg=data.get('error_msg')
    )
    return jsonify({"success": True})


# ╔══════════════════════════════════════════════════════════════╗
# ║  📊 统计看板 API                                             ║
# ╚══════════════════════════════════════════════════════════════╝

# ========== 统计看板 API ==========

@app.route('/api/stats', methods=['GET'])
def stats_api():
    """获取统计看板数据"""
    stats = db.get_stats()
    return jsonify({"success": True, "stats": stats})


# ╔══════════════════════════════════════════════════════════════╗
# ║  🗺️  地图增强 API                                            ║
# ╚══════════════════════════════════════════════════════════════╝

# ========== 地图增强 API ==========

@app.route('/api/map/direction-proxy', methods=['POST'])
def map_direction_proxy():
    """路线规划代理 - 调用高德 Web Service API direction 接口"""
    data = request.json or {}
    origin = data.get("origin", "")
    dest = data.get("destination", "")
    waypoints = data.get("waypoints", [])
    mode = data.get("mode", "driving")
    city = data.get("city", "")

    if not origin or not dest:
        return jsonify({"error": "origin 和 destination 参数必填，格式: lng,lat"}), 400

    if not _amap_key:
        return jsonify({"error": "高德地图 API Key 未配置"}), 500

    mode_url_map = {
        "driving": "https://restapi.amap.com/v3/direction/driving",
        "walking": "https://restapi.amap.com/v3/direction/walking",
        "transit": "https://restapi.amap.com/v3/direction/transit/integrated",
        "bicycling": "https://restapi.amap.com/v4/direction/bicycling",
    }

    url = mode_url_map.get(mode, mode_url_map["driving"])

    params = {
        "key": _amap_key,
        "origin": origin,
        "destination": dest,
        "extensions": "all",
    }
    # transit 模式必须传 city 参数 (起点城市)
    if mode == "transit" and city:
        params["city"] = city
        params["cityd"] = data.get("cityd", city)  # 终点城市，默认同起点
    # driving 模式支持途经点
    if mode == "driving" and waypoints and isinstance(waypoints, list) and len(waypoints) > 0:
        params["waypoints"] = ";".join(waypoints)

    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()

        if data.get("status") == "1" and data.get("errcode") in (None, "", "0"):
            route = data.get("route", {})
            return jsonify({
                "status": "ok",
                "mode": mode,
                "origin": route.get("origin", ""),
                "destination": route.get("destination", ""),
                "paths": route.get("paths", []),
                "taxi_cost": route.get("taxi_cost", ""),
            })

        # 如果 AMAP 返回错误，尝试用 AMAP error info
        return jsonify({
            "status": "error",
            "message": data.get("info", "路线规划失败"),
            "detail": data
        }), 500

    except requests.exceptions.RequestException as e:
        # Fallback: 返回模拟的直线路线
        try:
            orig_parts = origin.split(",")
            dest_parts = dest.split(",")
            olng, olat = float(orig_parts[0]), float(orig_parts[1])
            dlng, dlat = float(dest_parts[0]), float(dest_parts[1])

            # 计算直线距离（Haversine 简化）
            from math import radians, cos, sin, sqrt, atan2
            R = 6371000
            dlat_r = radians(dlat - olat)
            dlon_r = radians(dlng - olng)
            a = sin(dlat_r / 2) ** 2 + cos(radians(olat)) * cos(radians(dlat)) * sin(dlon_r / 2) ** 2
            c = 2 * atan2(sqrt(a), sqrt(1 - a))
            distance = R * c
            duration = distance / 1.4 / 60  # 假设 1.4 m/s 步行速度，单位分钟

            return jsonify({
                "status": "ok",
                "mode": mode,
                "fallback": True,
                "origin": origin,
                "destination": dest,
                "paths": [{
                    "distance": str(int(distance)),
                    "duration": str(int(duration * 60)),
                    "steps": [{
                        "instruction": f"从起点直接前往终点（直线距离约 {distance:.0f}m，实际API不可用时的占位路线）",
                        "road": "直行",
                        "distance": str(int(distance)),
                        "duration": str(int(duration * 60)),
                    }]
                }],
            })
        except Exception:
            return jsonify({
                "status": "error",
                "message": f"路线规划请求失败: {str(e)}",
            }), 500


@app.route('/api/map/poi-search', methods=['POST'])
def map_poi_search():
    """POI 搜索（含照片）- 调用高德 POI 搜索 API"""
    data = request.json or {}
    keywords = data.get("keywords", "")
    city = data.get("city", "")
    types = data.get("types", "")

    if not keywords:
        return jsonify({"error": "keywords 参数必填"}), 400
    if not _amap_key:
        return jsonify({"error": "高德地图 API Key 未配置"}), 500

    params = {
        "key": _amap_key,
        "keywords": keywords,
        "offset": 20,
        "extensions": "all",
    }
    if city:
        params["city"] = city
        params["citylimit"] = "true"
    if types:
        params["types"] = types

    try:
        resp = requests.get("https://restapi.amap.com/v3/place/text", params=params, timeout=10)
        data = resp.json()

        if data.get("status") == "1":
            pois = []
            for p in data.get("pois", []):
                location_str = p.get("location", "")
                lng, lat = (0.0, 0.0)
                if location_str and "," in location_str:
                    parts = location_str.split(",")
                    lng, lat = float(parts[0]), float(parts[1])

                # 提取照片
                photo_url = ""
                photos = p.get("photos", [])
                if photos:
                    photo_info = photos[0]
                    photo_url = photo_info.get("url", "")
                    if photo_url and not photo_url.startswith("http"):
                        photo_url = "https:" + photo_url if photo_url.startswith("//") else ""

                # 提取评分
                biz_ext = p.get("biz_ext", {})
                rating = biz_ext.get("rating", "") if biz_ext else ""
                if isinstance(rating, list):
                    rating = rating[0] if rating else ""

                pois.append({
                    "id": p.get("id", ""),
                    "name": p.get("name", ""),
                    "location": {"lng": lng, "lat": lat},
                    "address": p.get("address", ""),
                    "type": p.get("type", ""),
                    "photo": photo_url,
                    "rating": rating,
                    "tel": p.get("tel", "") or "",
                    "business_area": p.get("business_area", ""),
                    "pname": p.get("pname", "") or "",
                    "cityname": p.get("cityname", "") or "",
                    "adname": p.get("adname", "") or "",
                })

            return jsonify({
                "status": "ok",
                "count": data.get("count", "0"),
                "pois": pois,
            })

        return jsonify({
            "status": "error",
            "message": data.get("info", "POI 搜索失败"),
        }), 500

    except requests.exceptions.RequestException as e:
        return jsonify({
            "status": "error",
            "message": f"POI 搜索请求失败: {str(e)}",
        }), 500


@app.route('/api/map/poi-photos', methods=['POST'])
def map_poi_photos():
    """获取 POI 详情（含照片）- 调用高德 POI 详情 API"""
    data = request.json or {}
    poi_id = data.get("poi_id", "")

    if not poi_id:
        return jsonify({"error": "poi_id 参数必填"}), 400
    if not _amap_key:
        return jsonify({"error": "高德地图 API Key 未配置"}), 500

    try:
        # 高德 POI 详情接口（通过搜索 ID 并加 extensions=all 获取详情）
        resp = requests.get("https://restapi.amap.com/v3/place/detail", params={
            "key": _amap_key,
            "id": poi_id,
            "extensions": "all",
        }, timeout=10)
        data = resp.json()

        if data.get("status") == "1" and data.get("pois"):
            poi = data["pois"][0]
            location_str = poi.get("location", "")
            lng, lat = (0.0, 0.0)
            if location_str and "," in location_str:
                parts = location_str.split(",")
                lng, lat = float(parts[0]), float(parts[1])

            # 提取照片列表
            photo_list = []
            for photo in poi.get("photos", []):
                url = photo.get("url", "")
                if url and not url.startswith("http"):
                    url = "https:" + url if url.startswith("//") else url
                photo_list.append({
                    "url": url,
                    "title": photo.get("title", ""),
                })

            # 提取评分
            biz_ext = poi.get("biz_ext", {}) if poi.get("biz_ext") else {}
            deep_info = poi.get("deep_info", {}) if poi.get("deep_info") else {}
            rating = biz_ext.get("rating", "") if biz_ext else ""
            if isinstance(rating, list):
                rating = rating[0] if rating else ""

            return jsonify({
                "status": "ok",
                "poi": {
                    "id": poi.get("id", ""),
                    "name": poi.get("name", ""),
                    "location": {"lng": lng, "lat": lat},
                    "address": poi.get("address", ""),
                    "type": poi.get("type", ""),
                    "pname": poi.get("pname", "") or "",
                    "cityname": poi.get("cityname", "") or "",
                    "adname": poi.get("adname", "") or "",
                    "photos": photo_list,
                    "rating": rating,
                    "cost": biz_ext.get("cost", ""),
                    "tel": poi.get("tel", "") or "",
                    "business_area": poi.get("business_area", ""),
                    "website": poi.get("website", "") or "",
                    "email": poi.get("email", "") or "",
                    "opentime": deep_info.get("opentime", "") or "",
                },
            })

        return jsonify({
            "status": "error",
            "message": data.get("info", "POI 详情获取失败"),
        }), 500

    except requests.exceptions.RequestException as e:
        return jsonify({
            "status": "error",
            "message": f"POI 详情请求失败: {str(e)}",
        }), 500


@app.route('/api/travel/cities', methods=['GET'])
def travel_cities():
    """查询本地全国城市基础库"""
    keyword = (request.args.get("keyword", "") or "").strip()
    province = (request.args.get("province", "") or "").strip()
    hot_only = request.args.get("hot", "") in ("1", "true", "yes")
    limit = min(int(request.args.get("limit", 200) or 200), 500)

    sql = "SELECT adcode, name, province, city_level, aliases, is_hot, source FROM travel_cities WHERE 1=1"
    params = []
    if keyword:
        sql += " AND (name LIKE ? OR aliases LIKE ? OR province LIKE ?)"
        like = f"%{keyword}%"
        params.extend([like, like, like])
    if province:
        sql += " AND province LIKE ?"
        params.append(f"%{province}%")
    if hot_only:
        sql += " AND is_hot = 1"
    sql += " ORDER BY is_hot DESC, province, name LIMIT ?"
    params.append(limit)

    conn = db.get_connection()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify({
        "success": True,
        "count": len(rows),
        "cities": [dict(r) for r in rows]
    })


@app.route('/api/travel/pois', methods=['GET'])
def travel_pois():
    """查询本地全国文旅 POI 知识库"""
    keyword = (request.args.get("keyword", "") or "").strip()
    city = (request.args.get("city", "") or "").strip()
    province = (request.args.get("province", "") or "").strip()
    category = (request.args.get("category", "") or "").strip()
    limit = min(int(request.args.get("limit", 50) or 50), 200)

    sql = """
        SELECT poi_id, name, province, city, adcode, category, poi_type, level, address,
               lng, lat, rating, price_info, tags_json, description, photo_url, source
        FROM travel_pois
        WHERE 1=1
    """
    params = []
    if keyword:
        sql += " AND (name LIKE ? OR description LIKE ? OR tags_json LIKE ?)"
        like = f"%{keyword}%"
        params.extend([like, like, like])
    if city:
        sql += " AND city LIKE ?"
        params.append(f"%{city}%")
    if province:
        sql += " AND province LIKE ?"
        params.append(f"%{province}%")
    if category:
        sql += " AND category = ?"
        params.append(category)
    sql += " ORDER BY COALESCE(rating, 0) DESC, city, category, name LIMIT ?"
    params.append(limit)

    conn = db.get_connection()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    pois = []
    for row in rows:
        item = dict(row)
        try:
            item["tags"] = json.loads(item.pop("tags_json") or "[]")
        except Exception:
            item["tags"] = []
        pois.append(item)
    return jsonify({
        "success": True,
        "count": len(pois),
        "pois": pois
    })



# ╔══════════════════════════════════════════════════════════════╗
# ║  🎫 购票实时数据接口 (DeepTrip 2.0 light Skill)               ║
# ╚══════════════════════════════════════════════════════════════╝

# ========== 购票实时数据接口 (DeepTrip 2.0 light Skill) ==========
# 数据来源：本地知识库(酒店/景区真实价格) — 可替换为飞猪/携程 API

def _parse_ticket_price(ticket_str: str) -> float:
    """
    从票价字符串中解析最低价格
    
    Args:
        ticket_str: 票价字符串，如 "成人票150元，儿童票80元"
        
    Returns:
        解析出的最低价格（float），若无法解析则返回 0.0
    """
    if not ticket_str:
        return 0.0
    nums = re.findall(r'(\d+)\s*元', ticket_str)
    if nums:
        return float(min(int(n) for n in nums))
    return 0.0


def _build_ticket_products(city: str = '') -> list:
    """
    从本地知识库构建票务产品列表
    
    遍历本地知识库中的景点门票和酒店价格信息，
    生成标准化的票务产品数据结构。
    
    Args:
        city: 目标城市名称，为空时加载所有城市的票务产品
        
    Returns:
        票务产品列表，每个产品包含 id / name / type / price / city / tags 等字段
    """
    from plugins.travel_data import LOCAL_KNOWLEDGE
    products = []
    pid = 0
    cities_to_search = [city] if city and city in LOCAL_KNOWLEDGE else list(LOCAL_KNOWLEDGE.keys())
    for c in cities_to_search:
        data = LOCAL_KNOWLEDGE[c]
        for spot in data.get('scenic_spots', []):
            price = _parse_ticket_price(spot.get('ticket', ''))
            if price <= 0:
                continue
            pid += 1
            products.append({
                'id': f'tk{pid:03d}', 'name': spot['name'], 'type': '景点',
                'price': price, 'orig_price': price,
                'city': c, 'tags': spot.get('highlights', [])[:3],
                'desc': f"{spot.get('level', '')} · {spot.get('duration', '')}",
                'stock': 999, 'source': '本地知识库'
            })
        for hotel in data.get('hotels', []):
            nums = re.findall(r'(\d+)', hotel.get('price_range', ''))
            if not nums:
                continue
            low, high = float(nums[0]), float(nums[1]) if len(nums) >= 2 else float(nums[0])
            if low <= 0:
                continue
            pid += 1
            products.append({
                'id': f'tk{pid:03d}', 'name': hotel['name'], 'type': '酒店',
                'price': low, 'orig_price': high,
                'city': c, 'tags': hotel.get('features', [])[:3],
                'desc': f"{'⭐' * hotel['stars']} · {hotel.get('rating', '')}分",
                'stock': 50, 'source': '本地知识库'
            })
    return products


_TICKET_CACHE = {}

def _get_ticket_products(city: str = '') -> list:
    """
    获取票务产品列表（带缓存）
    
    基于 _build_ticket_products 构建产品列表并缓存结果，
    避免每次请求都重新遍历本地知识库。
    
    Args:
        city: 目标城市名称，为空时加载全部城市产品
        
    Returns:
        票务产品列表（从缓存读取）
    """
    ck = city or '__all__'
    if ck not in _TICKET_CACHE:
        _TICKET_CACHE[ck] = _build_ticket_products(city)
    return _TICKET_CACHE[ck]


@app.route('/api/tickets/products', methods=['GET'])
def ticket_products():
    keyword = request.args.get('keyword', '')
    city = request.args.get('city', '')  # 明确指定城市
    target = city or keyword
    products = _get_ticket_products(target)
    if keyword and not city:
        # 关键词二次过滤（名称/标签）
        kw = keyword.lower()
        products = [p for p in products if kw in p['city'].lower() or kw in p['name'].lower() or any(kw in t.lower() for t in p.get('tags', []))]
    return jsonify({"success": True, "products": products, "source": "本地知识库(飞猪/携程真实价格)"})


@app.route('/api/tickets/order', methods=['POST'])
def ticket_order():
    """门票 / 酒店购买下单 — 需登录"""
    user = _get_current_user()
    if not user:
        return jsonify({"success": False, "error": "请先登录后再购买"}), 401
    data = request.json or {}
    product_id = data.get('product_id', '')
    quantity = int(data.get('quantity', 1))
    contact = data.get('contact', {})
    
    # 从知识库产品列表中查找
    all_products = _get_ticket_products()
    product = next((p for p in all_products if p['id'] == product_id), None)
    if not product:
        return jsonify({"success": False, "error": "产品不存在"}), 404
    
    total = product['price'] * quantity
    order_id = 'ORD' + datetime.now().strftime('%Y%m%d%H%M%S') + str(random.randint(100, 999))
    
    return jsonify({
        "success": True,
        "order": {
            "order_id": order_id,
            "product_name": product['name'],
            "quantity": quantity,
            "unit_price": product['price'],
            "total": round(total, 2),
            "contact": contact,
            "created_at": datetime.now().isoformat(),
            "source": "本地知识库(可升级为飞猪/携程API)"
        }
    })


# ╔══════════════════════════════════════════════════════════════╗
# ║  👤 个人中心 / 订单存储                                       ║
# ╚══════════════════════════════════════════════════════════════╝

# ========== 个人中心 / 订单存储 ==========

@app.route('/api/user/profile', methods=['GET'])
def user_profile():
    uid = _resolve_uid(request.args.get('user_id'))
    import database as db
    data = db.get_user_store_items(uid)
    return jsonify({'success': True, 'data': data})

@app.route('/api/user/order', methods=['POST'])
def save_user_order():
    data = request.get_json(silent=True) or {}
    uid = _resolve_uid(data.get('user_id'))
    import database as db
    item = {
        'type': data.get('type', 'ticket'),
        'title': data.get('title', ''),
        'city': data.get('city', ''),
        'price': data.get('price', 0),
        'count': data.get('count', 1),
        'date': data.get('date', ''),
        'status': data.get('status', 'confirmed'),
        'order_id': data.get('order_id', '')
    }
    saved = db.save_user_store_item(uid, 'orders', item)
    return jsonify({'success': True, 'order': {'id': saved['id'], 'created_at': saved['created_at']}})

@app.route('/api/user/itinerary', methods=['POST'])
def save_user_itinerary():
    data = request.get_json(silent=True) or {}
    uid = _resolve_uid(data.get('user_id'))
    import database as db
    item = {
        'title': data.get('title', ''),
        'city': data.get('city', ''),
        'days': data.get('days', 1),
        'spots': data.get('spots', []),
        'budget': data.get('budget', ''),
        'date': data.get('date', ''),
        'content': data.get('content', ''),
        'tags': data.get('tags', [])
    }
    saved = db.save_user_store_item(uid, 'itineraries', item)
    return jsonify({'success': True, 'itinerary': {'id': saved['id'], 'created_at': saved['created_at']}})

@app.route('/api/user/booking', methods=['POST'])
def save_user_booking():
    data = request.get_json(silent=True) or {}
    uid = _resolve_uid(data.get('user_id'))
    import database as db
    btype = data.get('booking_type', 'hotel')
    plural_map = {'hotel': 'hotels', 'flight': 'flights', 'itinerary': 'itineraries', 'ticket': 'orders'}
    category = plural_map.get(btype, btype + 's')
    item = {
        'type': btype,
        'title': data.get('title', ''),
        'city': data.get('city', ''),
        'price': data.get('price', 0),
        'date': data.get('date', ''),
        'days': data.get('days', 0),
        'detail': data.get('detail', ''),
        'content': data.get('content', ''),
        'status': data.get('status', 'confirmed')
    }
    saved = db.save_user_store_item(uid, category, item)
    return jsonify({'success': True, 'booking': {'id': saved['id'], 'created_at': saved['created_at']}})

@app.route('/api/user/clear', methods=['POST'])
def clear_user_data():
    data = request.get_json(silent=True) or {}
    uid = _resolve_uid(data.get('user_id') or request.args.get('user_id'))
    category = data.get('category') or request.args.get('category') or ''
    import database as db
    db.clear_user_store(uid, category if category else None)
    return jsonify({'success': True})


@app.route('/api/user/item', methods=['POST'])
def delete_user_item():
    """删除个人中心的单条记录（目前只有行程走服务端存储）。

    取代原来「clear + 整份重发」的做法：那种全量重建在任一次请求失败时会把用户
    别的记录一起清掉。这里只删一条，并且按 user_id 校验所有权，删不到就回 404——
    让前端明白区分「删掉了」和「没删掉」，不做假成功。
    """
    data = request.get_json(silent=True) or {}
    uid = _resolve_uid(data.get('user_id'))
    item_id = str(data.get('id') or '').strip()
    if not item_id:
        return jsonify({'success': False, 'error': '缺少要删除的记录 id'}), 400
    import database as db
    deleted = db.delete_user_store_item(item_id, uid)
    if not deleted:
        return jsonify({'success': False, 'error': '记录不存在，或不属于当前账号'}), 404
    return jsonify({'success': True, 'deleted': item_id})

# ====== 语音识别（本地 faster-whisper，离线可用，无需 API Key）=======
_whisper_model = None

def _get_whisper_model():
    """懒加载本地语音识别模型；首次调用会下载模型（约150MB，自动缓存到本地）"""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    try:
        import os as _os
        _os.environ.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel('base', device='cpu', compute_type='int8')
    except Exception as e:
        print('[语音识别] 模型加载失败:', e)
        _whisper_model = False
    return _whisper_model

@app.route('/api/voice/recognize', methods=['POST'])
def voice_recognize():
    """语音识别接口：接收浏览器录制的音频（form-data: audio），返回识别文字"""
    model = _get_whisper_model()
    if not model:
        return jsonify({"error": "语音识别模型未就绪，请确认已安装 faster-whisper"}), 500
    f = request.files.get('audio')
    if not f:
        return jsonify({"error": "缺少音频文件"}), 400
    import tempfile as _tf
    tmp_path = None
    try:
        with _tf.NamedTemporaryFile(suffix='.webm', delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        segments, info = model.transcribe(tmp_path, language='zh', vad_filter=True)
        text = ''.join(seg.text for seg in segments).strip()
        if not text:
            return jsonify({"error": "没有识别到语音，请靠近麦克风重试"}), 422
        return jsonify({"text": text, "language": info.language})
    except Exception as e:
        print('[语音识别] 识别失败:', e)
        return jsonify({"error": "识别失败: %s" % str(e)}), 500
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ====== 景区特色产品（并入文创灵感库，仅供参考）======
# 每个产品除基础信息外，附带 design/culture/audience/inspire/slogan，
# 供「扫码前介绍」与「扫码后详情（亮点/文化/设计/适合谁/深入了解）」展示
SHOP_PRODUCTS = [
    {"id": "sp01", "name": "西湖油纸伞·断桥残雪款", "scenic": "杭州西湖", "price": 88, "tag": "国潮", "emoji": "☂️", "img": "/static/images/shop/sp01.png", "desc": "手绘断桥雪景，撑开即见江南",
     "design": "伞面手绘断桥残雪水墨，竹骨八十四档古法穿线，开合顺滑有韧性。",
     "culture": "取西湖十景之「断桥残雪」，与杭州油纸伞制作技艺结合，伞下即是江南意象。",
     "audience": "喜爱国风美学、旅拍打卡的年轻游客与汉服爱好者。",
     "inspire": ["雪景水墨+留白的视觉语言，适合冬季限定营销", "「撑开即见江南」的场景化文案角度", "与西湖游船/汉服体验联动的套票玩法"],
     "slogan": "一把伞，撑开整个江南的雪。"},
    {"id": "sp02", "name": "龙井茶韵伴手礼盒", "scenic": "杭州西湖", "price": 168, "tag": "伴手礼", "emoji": "🍵", "img": "/static/images/shop/sp02.png", "desc": "明前龙井+青瓷杯，茶香入礼",
     "design": "明前龙井配青瓷品茗杯，礼盒以茶山等高线为纹样，开盒有层次。",
     "culture": "西湖龙井为绿茶之冠，青瓷呼应宋代茶事美学，一盏见杭州。",
     "audience": "送长辈/商务伴手、爱茶人士与注重礼赠质感的人群。",
     "inspire": ["茶山+青瓷的高级感配色，适合礼赠场景", "「把杭州的春天装进盒子」节令营销", "与茶楼/民宿下午茶的联名体验"],
     "slogan": "把杭州的春天，装进一只礼盒。"},
    {"id": "sp03", "name": "灵隐禅意香囊·护佑符", "scenic": "杭州灵隐寺", "price": 39, "tag": "非遗", "emoji": "🪷", "img": "/static/images/shop/sp03.png", "desc": "古法香粉，祈愿平安",
     "design": "古法香粉入囊，外绣祥云护佑纹，配挂绳可随身可车载。",
     "culture": "取灵隐禅意与端午佩香习俗，香囊自古是祈愿平安的随身之物。",
     "audience": "求平安顺遂的年轻人、考生家庭与喜爱禅意小物的用户。",
     "inspire": ["禅意留白+祈福文案的情绪营销", "「随身的好运气」场景种草", "与寺庙咖啡/素斋的组合打卡"],
     "slogan": "一缕香，一份随身的安心。"},
    {"id": "sp04", "name": "故宫神兽冰箱贴盲盒", "scenic": "北京故宫", "price": 45, "tag": "潮玩", "emoji": "🧸", "img": "/static/images/shop/sp04.png", "desc": "故宫猫+龙九子随机款",
     "design": "故宫猫与龙九子Q版造型，磁吸冰箱贴，随机盲盒款带隐藏。",
     "culture": "取材故宫屋脊神兽与宫猫文化，把镇宅瑞兽变成桌面小陪伴。",
     "audience": "盲盒收藏者、学生党与喜欢萌系国潮的年轻人。",
     "inspire": ["盲盒+隐藏款的收集欲玩法", "瑞兽拟人化的萌系视觉", "「把故宫带回家」的低门槛伴手礼定位"],
     "slogan": "抽一只瑞兽，镇守你的冰箱。"},
    {"id": "sp05", "name": "敦煌飞天丝巾", "scenic": "甘肃敦煌", "price": 129, "tag": "国潮", "emoji": "🧣", "img": "/static/images/shop/sp05.png", "desc": "莫高窟壁画复刻，真丝斜纹",
     "design": "莫高窟飞天壁画复刻于真丝斜纹，飘带线条流畅，垂坠有光。",
     "culture": "飞天是敦煌艺术的象征，丝路乐舞与矿物彩在丝巾上重生。",
     "audience": "喜爱国风穿搭、艺术联名与旅拍配饰的女性用户。",
     "inspire": ["壁画矿物色的高级配色方案", "「把千年飞天披在肩上」故事化文案", "与汉服/舞蹈演出的联动造型"],
     "slogan": "千年飞天，落在肩头。"},
    {"id": "sp06", "name": "故宫脊兽书签·鎏金款", "scenic": "北京故宫", "price": 59, "tag": "文创", "emoji": "📖", "img": "/static/images/shop/sp06.png", "desc": "太和殿十脊兽全套",
     "design": "太和殿十脊兽金属蚀刻鎏金，一套十枚，夹书如瑞兽列队。",
     "culture": "脊兽是古建筑等级与祈福符号，十兽依次寓意消灾纳福。",
     "audience": "阅读爱好者、学生与喜欢成套收集的文创玩家。",
     "inspire": ["成套收集的系列化营销", "金属鎏金的质感视觉", "「一页一瑞兽」的阅读陪伴文案"],
     "slogan": "十兽列队，守你每一页书。"},
    {"id": "sp07", "name": "熊猫基地毛绒玩偶·花叶", "scenic": "成都熊猫基地", "price": 99, "tag": "潮玩", "emoji": "🐼", "img": "/static/images/shop/sp07.png", "desc": "国宝明星同款，手感蓬松",
     "design": "按明星熊猫「花叶」原型打版，绒毛蓬松可抱，关节可摆造型。",
     "culture": "大熊猫是成都与中国的萌系名片，玩偶承载基地探望记忆。",
     "audience": "亲子家庭、熊猫粉丝与喜欢治愈系玩偶的年轻人。",
     "inspire": ["萌系IP的拟人故事营销", "「抱走一只顶流」情绪种草", "与基地门票/研学的亲子套票"],
     "slogan": "把顶流花花，抱回家。"},
    {"id": "sp08", "name": "川剧脸谱书立·变脸", "scenic": "成都宽窄巷子", "price": 78, "tag": "非遗", "emoji": "🎭", "img": "/static/images/shop/sp08.png", "desc": "手工彩绘，川味十足",
     "design": "手工彩绘川剧脸谱做书立，一对可夹书，色彩浓烈对称。",
     "culture": "川剧变脸是国家级非遗，脸谱色彩对应忠奸性格谱式。",
     "audience": "戏剧爱好者、书房布置人群与喜欢浓烈国潮的用户。",
     "inspire": ["脸谱撞色的强视觉海报", "「一书一脸谱」的文化科普内容", "与川剧演出/茶馆的联票玩法"],
     "slogan": "一张脸谱，夹住一段川味故事。"},
    {"id": "sp09", "name": "苏州园林纸雕灯·拙政园", "scenic": "苏州拙政园", "price": 148, "tag": "文创", "emoji": "🏮", "img": "/static/images/shop/sp09.png", "desc": "镂空纸雕，点亮园林夜色",
     "design": "多层镂空纸雕叠出拙政园亭台，内置暖光，点亮即成夜景。",
     "culture": "苏州园林是文人写意山水的立体呈现，纸雕再现移步换景。",
     "audience": "家居氛围控、送礼人群与喜爱东方美学的用户。",
     "inspire": ["光影层次的氛围感视觉", "「把园林夜色留在桌面」场景营销", "与夜游园林/民宿的联动陈列"],
     "slogan": "一盏灯，亮起一座园林的夜。"},
    {"id": "sp10", "name": "苏绣团扇·双面绣", "scenic": "苏州平江路", "price": 108, "tag": "非遗", "emoji": "🪭", "img": "/static/images/shop/sp10.png", "desc": "双面绣艺，一扇两面",
     "design": "苏绣双面绣工艺，一扇两面同图异色，檀木扇柄丝穗坠角。",
     "culture": "苏绣为四大名绣之首，双面绣是其绝技，团扇承闺阁雅趣。",
     "audience": "喜爱国风配饰、婚礼/演出道具与收藏绣品的人群。",
     "inspire": ["双面异色的工艺稀缺性卖点", "「一扇两面」的对比视觉", "与汉服婚礼/雅集的体验联动"],
     "slogan": "一面是画，另一面也是。"},
    {"id": "sp11", "name": "西安兵马俑迷你手办", "scenic": "西安兵马俑", "price": 66, "tag": "潮玩", "emoji": "🗿", "img": "/static/images/shop/sp11.png", "desc": "Q版俑阵，桌面摆件",
     "design": "Q版俑阵迷你手办，跪射/将军/军吏多形态，可排兵布阵。",
     "culture": "兵马俑是秦帝国军阵的地下缩影，手办让千年军阵走上桌面。",
     "audience": "历史爱好者、桌面摆件玩家与学生群体。",
     "inspire": ["Q版反差萌的历史科普内容", "「桌面排兵布阵」互动玩法", "与博物馆研学的套票营销"],
     "slogan": "一支迷你军团，驻守你的书桌。"},
    {"id": "sp12", "name": "大唐不夜城鱼灯", "scenic": "西安大唐不夜城", "price": 58, "tag": "国潮", "emoji": "🐟", "img": "/static/images/shop/sp12.png", "desc": "非遗鱼灯，夜晚出片神器",
     "design": "非遗鱼灯造型手提灯，夜晚点亮通透出片，可提可挂。",
     "culture": "鱼灯寓意年年有余，是不夜城夜游的标志性手持道具。",
     "audience": "夜游打卡、汉服出行与喜欢氛围感道具的年轻人。",
     "inspire": ["夜景点灯的高出片率视觉", "「提一盏鱼灯游长安」场景种草", "与不夜城夜游/灯会的联动"],
     "slogan": "提一盏鱼灯，走进大唐的夜。"},
    {"id": "sp13", "name": "桂林山水丝巾", "scenic": "桂林漓江", "price": 119, "tag": "伴手礼", "emoji": "⛰️", "img": "/static/images/shop/sp13.png", "desc": "漓江烟雨绘于真丝",
     "design": "漓江烟雨山水绘于真丝，青绿渐变，围系飘逸。",
     "culture": "桂林山水甲天下，丝巾把喀斯特峰林与江雾披在身上。",
     "audience": "旅拍配饰、送女性长辈与喜爱山水意境的人群。",
     "inspire": ["青绿山水的意境配色", "「把漓江披在肩上」文案角度", "与漓江游船/竹筏的旅拍联动"],
     "slogan": "一方丝巾，藏尽漓江烟雨。"},
    {"id": "sp14", "name": "武夷山大红袍茶饼·典藏", "scenic": "福建武夷山", "price": 188, "tag": "伴手礼", "emoji": "🫖", "img": "/static/images/shop/sp14.png", "desc": "岩韵醇厚，礼盒典藏",
     "design": "大红袍压制成饼，浮雕山水亭台纹，木盒典藏配茶刀。",
     "culture": "武夷岩茶「岩骨花香」，大红袍为乌龙之冠，茶饼利于陈藏。",
     "audience": "茶客、商务礼赠与喜爱收藏陈茶的人群。",
     "inspire": ["岩韵+陈藏的时间价值叙事", "「一饼藏山水」的典藏视觉", "与茶山游/茶席体验的联动"],
     "slogan": "岩韵醇厚，一饼藏尽武夷山水。"},
    {"id": "sp15", "name": "丽江东巴纸笔记本", "scenic": "丽江古城", "price": 46, "tag": "非遗", "emoji": "📓", "img": "/static/images/shop/sp15.png", "desc": "千年东巴纸，纳西文字页",
     "design": "千年东巴纸做内页，封面压纳西象形文字，纸质粗粝耐写。",
     "culture": "东巴纸是纳西族手工纸活化石，防蛀耐存，文字如画。",
     "audience": "手帐爱好者、文艺青年与喜爱少数民族文化的人群。",
     "inspire": ["象形文字的图形化视觉", "「写在千年纸上」的文化叙事", "与古城手作体验坊的联动"],
     "slogan": "在千年的纸上，写今天的事。"},
    {"id": "sp16", "name": "莫高窟藻井丝巾", "scenic": "甘肃敦煌", "price": 139, "tag": "国潮", "emoji": "🌀", "img": "/static/images/shop/sp16.png", "desc": "藻井纹样，异域瑰丽",
     "design": "莫高窟藻井中心纹样复刻，对称瑰丽，真丝方巾百搭。",
     "culture": "藻井是窟顶的华盖纹样，汇聚丝路多元装饰母题。",
     "audience": "艺术联名爱好者、配饰穿搭与收藏纹样的人群。",
     "inspire": ["藻井对称纹样的华丽视觉", "「窟顶华盖披上身」故事文案", "与敦煌特展/讲解的联动"],
     "slogan": "一方藻井，罩住丝路千年。"},
    {"id": "sp17", "name": "广州塔灯光冰箱贴", "scenic": "广州塔", "price": 32, "tag": "文创", "emoji": "🗼", "img": "/static/images/shop/sp17.png", "desc": "夜灯款，磁吸可爱",
     "design": "广州塔「小蛮腰」造型磁吸冰箱贴，内置夜灯款可点亮。",
     "culture": "广州塔是城市新地标，灯光款还原珠江夜色的璀璨。",
     "audience": "城市纪念收藏者、年轻游客与喜欢小夜灯的人群。",
     "inspire": ["夜灯点亮的反差展示", "「把小蛮腰带回家」地标营销", "与珠江夜游的联动纪念"],
     "slogan": "点亮小蛮腰，留住珠江夜。"},
    {"id": "sp18", "name": "三亚贝壳风铃·南海", "scenic": "三亚湾", "price": 55, "tag": "伴手礼", "emoji": "🐚", "img": "/static/images/shop/sp18.png", "desc": "海风轻拂，叮咚入梦",
     "design": "南海天然贝壳串制风铃，海风轻拂叮咚作响，附挂绳。",
     "culture": "贝壳是海的信物，风铃把海浪声与度假记忆挂在檐下。",
     "audience": "度假纪念、家居装饰与喜爱海洋元素的人群。",
     "inspire": ["海风声音的感官营销", "「把海风挂在家」场景种草", "与海岛游/民宿的联动陈列"],
     "slogan": "一串风铃，响起整片南海的风。"},
]

@app.route('/api/shop/products', methods=['GET'])
def shop_products():
    """景区特色礼物商品列表，支持关键词 / 分类过滤"""
    keyword = (request.args.get('keyword') or '').strip().lower()
    cat = (request.args.get('cat') or '').strip()
    items = SHOP_PRODUCTS
    if keyword:
        items = [x for x in items if keyword in (x['name'] + x['scenic'] + x['tag'] + x['desc']).lower()]
    if cat:
        items = [x for x in items if x['tag'] == cat]
    return jsonify({"success": True, "products": items})

@app.route('/api/shop/coupons', methods=['GET'])
def shop_coupons():
    """查询我的优惠券（登录用户 / 默认游客）"""
    user = _get_current_user()
    uid = user['user_id'] if user else 'default'
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM coupon_claims WHERE user_id = ? ORDER BY created_at DESC", (uid,)).fetchall()
        coupons = [dict(r) for r in rows]
    finally:
        conn.close()
    return jsonify({"success": True, "coupons": coupons})

@app.route('/api/shop/coupon/claim', methods=['POST'])
def shop_coupon_claim():
    """购买成功后发放礼物 10% 优惠券"""
    user = _get_current_user()
    uid = user['user_id'] if user else 'default'
    data = get_json_payload() or {}
    cid = 'CPN' + datetime.now().strftime('%Y%m%d%H%M%S') + str(random.randint(100, 999))
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO coupon_claims (id, user_id, title, value, desc_text, source, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, uid, "礼物 10% 优惠券", 10, "购物礼品全场通用 10% 立减",
             str(data.get('source', '景区礼物')), "可用"))
        conn.commit()
        row = conn.execute("SELECT * FROM coupon_claims WHERE id = ?", (cid,)).fetchone()
    finally:
        conn.close()
    return jsonify({"success": True, "coupon": dict(row) if row else None})

@app.route('/api/shop/search_goods', methods=['POST'])
def shop_search_goods():
    """方案A：调用本地大模型（Ollama）动态生成景区特色商品（虚拟演示数据）"""
    data = get_json_payload() or {}
    keyword = str(data.get('keyword', '')).strip()
    if not keyword:
        return jsonify({"success": False, "error": "请输入景区或城市名称"}), 400
    if len(keyword) > 30:
        return jsonify({"success": False, "error": "关键词过长"}), 400

    system_prompt = (
        "你是资深文旅特产商品策划专家。请为指定景区/城市策划 5-8 件有代表性的特色伴手礼、文创商品。\n"
        "必须严格返回 JSON 对象，格式：{\"products\":[{\"name\":\"商品名\",\"price\":整数价格(元,30-300),\"desc\":\"一句话卖点\",\"scenic\":\"所属景区\",\"tag\":\"分类\"}]}\n"
        "tag 只能取：国潮、非遗、文创、潮玩、伴手礼。\n"
        "商品名、desc、scenic 必须全部使用简体中文，不得混用英文。禁止输出 JSON 以外的任何文字、注释或 Markdown。"
    )
    user_prompt = f"为「{keyword}」策划特色礼物商品。"
    try:
        resp = requests.post(
            "http://localhost:11434/v1/chat/completions",
            json={
                "model": "deepseek-r1:7b",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.6,
                "max_tokens": 1500,
                "stream": False
            },
            timeout=150
        )
    except requests.exceptions.ConnectionError:
        return jsonify({"success": False, "error": "本地 AI 服务（Ollama）未启动，请先启动 Ollama 后再搜好物"}), 503
    except requests.exceptions.Timeout:
        return jsonify({"success": False, "error": "AI 生成超时，请稍后重试"}), 502
    if resp.status_code != 200:
        return jsonify({"success": False, "error": f"本地 AI 返回 {resp.status_code}: {resp.text[:200]}"}), 502
    try:
        content = ((resp.json().get('choices') or [{}])[0].get('message') or {}).get('content', '').strip()
        if content.startswith('```'):
            content = re.sub(r'^```\w*\n?|\n?```$', '', content)
        raw = json.loads(content)
    except Exception as e:
        return jsonify({"success": False, "error": f"AI 返回内容解析失败：{e}"}), 502

    tag_emoji = {"国潮": "🏮", "非遗": "🪭", "文创": "📖", "潮玩": "🧸", "伴手礼": "🎁"}
    products_raw = raw.get('products') if isinstance(raw, dict) else raw
    products = []
    ts = int(time.time())
    if isinstance(products_raw, list):
        for i, g in enumerate(products_raw):
            if not isinstance(g, dict):
                continue
            name = str(g.get('name', '')).strip()
            if not name:
                continue
            try:
                price = int(float(g.get('price', 58)))
            except Exception:
                price = 58
            price = max(9, min(price, 999))
            tag = str(g.get('tag', '文创')).strip()
            if tag not in tag_emoji:
                tag = '文创'
            products.append({
                "id": f"ai{ts}{i}",
                "name": name,
                "scenic": str(g.get('scenic') or keyword),
                "price": price,
                "tag": tag,
                "emoji": tag_emoji.get(tag, '🎁'),
                "desc": str(g.get('desc', '')).strip() or f"{keyword}特色好物",
                "ai": True
            })
    if not products:
        return jsonify({"success": False, "error": "模型未返回有效商品"}), 502
    return jsonify({"success": True, "ai": True, "keyword": keyword, "products": products})

# ====== 用户注册 / 登录 / 会话 ======
TOKEN_TTL_DAYS = 7

def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return f"{salt}${digest.hex()}"

def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, hex_digest = stored.split('$', 1)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
        return secrets.compare_digest(digest.hex(), hex_digest)
    except Exception:
        return False

def _issue_token(user_id: str) -> str:
    token = secrets.token_hex(24)
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM auth_tokens WHERE user_id = ? OR expires_at <= datetime('now')", (user_id,))
        conn.execute(
            "INSERT INTO auth_tokens (token, user_id, expires_at) VALUES (?, ?, datetime('now', '+7 days'))",
            (token, user_id))
        conn.commit()
    finally:
        conn.close()
    return token

def _get_current_user():
    """从 Authorization: Bearer <token> 解析当前用户，未登录返回 None"""
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    try:
        conn = db.get_connection()
        row = conn.execute(
            "SELECT u.user_id, u.username, u.account FROM auth_tokens t JOIN users u ON u.user_id = t.user_id "
            "WHERE t.token = ? AND t.expires_at > datetime('now')", (token,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None

def _resolve_uid(explicit=None):
    """按用户隔离数据：优先用登录令牌解析用户，未登录回退到显式 user_id 或 'default'（游客）"""
    user = _get_current_user()
    if user and user.get('user_id'):
        return user['user_id']
    if explicit:
        return str(explicit)
    uid = request.args.get('user_id')
    if not uid:
        try:
            payload = request.get_json(silent=True) or {}
            uid = payload.get('user_id')
        except Exception:
            uid = None
    return str(uid) if uid else 'default'

# ====== 图片验证码（纯 SVG 生成，无需 Pillow）======
_CAPTCHA_STORE = {}          # captcha_id -> (text_lower, expiry_ts)
_CAPTCHA_LOCK = threading.Lock()
_CAPTCHA_TTL = 300           # 5 分钟有效
_CAPTCHA_CHARS = 'ABCDEFGHJKLMNPQRSTUVWXY345678'  # 去掉 0/O、1/I 等易混淆字符

def _captcha_cleanup():
    """清理过期验证码"""
    now = time.time()
    with _CAPTCHA_LOCK:
        for k in [k for k, v in _CAPTCHA_STORE.items() if v[1] < now]:
            _CAPTCHA_STORE.pop(k, None)

def _make_captcha_svg(text: str) -> str:
    """把验证码文本渲染成带干扰线/噪点/旋转的 SVG 图片"""
    W, H = 120, 40
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
             f'<rect width="{W}" height="{H}" fill="#f4efe4"/>']
    for _ in range(4):  # 干扰线
        x1, y1, x2, y2 = (random.randint(0, W), random.randint(0, H), random.randint(0, W), random.randint(0, H))
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="rgba(120,110,90,0.35)" stroke-width="1"/>')
    for _ in range(36):  # 噪点
        cx, cy = random.randint(0, W), random.randint(0, H)
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="0.8" fill="rgba(120,110,90,0.45)"/>')
    n = max(len(text), 1)
    for i, ch in enumerate(text):
        x = 12 + i * (W - 24) // n + random.randint(-2, 2)
        y = random.randint(27, 31)
        rot = random.randint(-22, 22)
        color = '#%02x%02x%02x' % (random.randint(30, 110), random.randint(30, 90), random.randint(60, 140))
        parts.append(f'<text x="{x}" y="{y}" font-family="Georgia,serif" font-size="26" font-weight="bold" '
                     f'fill="{color}" transform="rotate({rot} {x} {y})">{ch}</text>')
    parts.append('</svg>')
    return ''.join(parts)

@app.route('/api/auth/captcha', methods=['GET'])
def auth_captcha():
    """生成图片验证码：返回 captcha_id + 原始 SVG（前端自行转 data URI 显示）"""
    _captcha_cleanup()
    text = ''.join(random.choice(_CAPTCHA_CHARS) for _ in range(4))
    cid = secrets.token_hex(8)
    with _CAPTCHA_LOCK:
        _CAPTCHA_STORE[cid] = (text.lower(), time.time() + _CAPTCHA_TTL)
    return jsonify({"success": True, "captcha_id": cid, "svg": _make_captcha_svg(text)})

def _verify_captcha(captcha_id, code) -> bool:
    """一次性校验验证码（不区分大小写），校验后立即失效"""
    if not captcha_id or not code:
        return False
    with _CAPTCHA_LOCK:
        item = _CAPTCHA_STORE.pop(str(captcha_id), None)
    if not item:
        return False
    text, expiry = item
    if time.time() > expiry:
        return False
    return secrets.compare_digest(text, str(code).strip().lower())

@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    """用户注册：用户名 + 账号 + 密码 + 图片验证码，注册成功自动登录"""
    data = get_json_payload() or {}
    username = str(data.get('username', '')).strip()
    account = str(data.get('account', '')).strip()
    password = str(data.get('password', ''))
    if not _verify_captcha(data.get('captcha_id', ''), data.get('captcha', '') or data.get('captcha_code', '')):
        return jsonify({"success": False, "error": "验证码错误或已失效", "captcha_error": True}), 400
    if not username:
        return jsonify({"success": False, "error": "请输入用户名"}), 400
    if len(username) > 20:
        return jsonify({"success": False, "error": "用户名最长20个字符"}), 400
    if not re.match(r'^[A-Za-z0-9_@.\-]{3,32}$', account):
        return jsonify({"success": False, "error": "账号需为3-32位字母/数字/下划线"}), 400
    if len(password) < 6:
        return jsonify({"success": False, "error": "密码至少6位"}), 400
    conn = db.get_connection()
    try:
        dup = conn.execute("SELECT user_id FROM users WHERE account = ?", (account,)).fetchone()
        if dup:
            return jsonify({"success": False, "error": "该账号已被注册"}), 409
        user_id = 'U' + str(uuid.uuid4())[:12]
        conn.execute("INSERT INTO users (user_id, username, account, password_hash) VALUES (?,?,?,?)",
                     (user_id, username, account, _hash_password(password)))
        conn.commit()
    finally:
        conn.close()
    token = _issue_token(user_id)
    return jsonify({"success": True, "token": token, "username": username, "user_id": user_id})

@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    """用户登录：账号 + 密码 + 图片验证码"""
    data = get_json_payload() or {}
    account = str(data.get('account', '')).strip()
    password = str(data.get('password', ''))
    if not _verify_captcha(data.get('captcha_id', ''), data.get('captcha', '') or data.get('captcha_code', '')):
        return jsonify({"success": False, "error": "验证码错误或已失效", "captcha_error": True}), 400
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT user_id, username, password_hash FROM users WHERE account = ?", (account,)).fetchone()
    finally:
        conn.close()
    if not row or not _verify_password(password, row['password_hash']):
        return jsonify({"success": False, "error": "账号或密码错误"}), 401
    token = _issue_token(row['user_id'])
    return jsonify({"success": True, "token": token, "username": row['username'], "user_id": row['user_id']})

@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    """退出登录"""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        token = auth[7:].strip()
        try:
            conn = db.get_connection()
            conn.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))
            conn.commit()
            conn.close()
        except Exception:
            pass
    return jsonify({"success": True})

@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    """获取当前登录用户"""
    user = _get_current_user()
    if not user:
        return jsonify({"success": False, "authenticated": False}), 200
    return jsonify({"success": True, "authenticated": True, "username": user['username'], "user_id": user['user_id']})

# ====== 文创海报生成（火山方舟 Seedream 文生图）=======
# 接口：POST https://ark.cn-beijing.volces.com/api/v3/images/generations
# 请求体固定字段：sequential_image_generation=disabled（单图）、response_format=url、
# stream=false；model / size / watermark 由 data/ark_config.json 提供，缺省即官方示例值。
ARK_API_URL = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
ARK_MODEL_DEFAULT = "doubao-seedream-4-5-251128"
ARK_SIZE_DEFAULT = "2K"
ARK_CONFIG_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', 'ark_config.json')
ARK_CONFIG = {"api_key": ""}
ARK_PERSIST_KEYS = ('model', 'watermark', 'poster_size', 'image_size', 'blog_api_key', 'blog_size', 'blog_model')

# 生图场景 → 专属 Key / 尺寸字段。旅游博客配图（主视觉海报·场景图·美食图）走 blog_*，
# 其余场景（文创海报、灵感卡配图）沿用共享的 api_key / poster_size。
ARK_SCENE_KEYS = {
    'blog': ('blog_api_key', 'blog_size'),
}

def _load_ark_config():
    """从 data/ark_config.json 读取火山方舟配置（api_key/model/watermark/size，重启不丢失）"""
    global ARK_CONFIG
    try:
        if os.path.exists(ARK_CONFIG_FILE):
            with open(ARK_CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                if cfg.get('api_key'):
                    ARK_CONFIG['api_key'] = str(cfg['api_key']).strip()
                for k in ARK_PERSIST_KEYS:
                    if k in cfg:
                        ARK_CONFIG[k] = cfg[k]
    except Exception as e:
        print('[ARK] 配置加载失败:', e)

def _save_ark_config():
    """写回 ark_config.json：连同 model / size / watermark 一起保存。

    注意：历史实现只写 api_key，前端保存一次 Key 就会把 model/size/watermark 从配置文件里
    抹掉（重启后退回代码缺省值）。这里改为读旧文件 → 合并 → 整体落盘，未知字段（如 _comment）保留。
    """
    os.makedirs(os.path.dirname(ARK_CONFIG_FILE), exist_ok=True)
    data = {}
    if os.path.exists(ARK_CONFIG_FILE):
        try:
            with open(ARK_CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
    data['api_key'] = ARK_CONFIG.get('api_key', '')
    data['model'] = ARK_CONFIG.get('model') or ARK_MODEL_DEFAULT
    data['watermark'] = bool(ARK_CONFIG.get('watermark', False))
    data['poster_size'] = ARK_CONFIG.get('poster_size') or ARK_SIZE_DEFAULT
    data['image_size'] = ARK_CONFIG.get('image_size') or ARK_SIZE_DEFAULT
    # 场景专属 Key 原样保留（历史实现只写共享字段，会被整体覆盖丢失）
    for k in ('blog_api_key', 'blog_size'):
        if ARK_CONFIG.get(k):
            data[k] = ARK_CONFIG[k]
    with open(ARK_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

_load_ark_config()

# ========== 启动期配置校验（不硬崩溃，只告警 + 在 /api/health 暴露完整度）==========

# 内置示例 Key 的指纹（sha256），源码里不留原文：
# 提交包/仓库里的 app.py 一旦带上可用凭据，「敏感信息 0 残留」就是句空话，所以只留摘要做比对。
_DEFAULT_KEY_FINGERPRINTS = frozenset({
    '1925b2191655083716a70d06b17f120e246fe346f1d3731018bb6711a3ae9db3',  # 裸 32 位形态
    '30d206bc331505a53bc02907c7f776cff02357652070e815ab3c58866b33de72',  # sk- 前缀形态
})


def _is_default_llm_key(key) -> bool:
    """配置里的 Key 是否还是随项目发布的那把示例 Key。"""
    s = str(key or '').strip()
    if not s:
        return False
    forms = {s, s[3:] if s.lower().startswith('sk-') else 'sk-' + s}
    return any(hashlib.sha256(f.encode('utf-8')).hexdigest() in _DEFAULT_KEY_FINGERPRINTS for f in forms)


def validate_config() -> dict:
    """核对运行所需的关键配置，返回结构化结果（供 /api/health 与启动告警使用）。

    只读、无副作用、不抛异常。required 项缺失代表核心功能不可用；
    optional 项缺失只降级对应功能（如生图）。
    """
    checks = []

    # 降级状态必须在 /api/health 看得见：否则「历史没存进去」「日志没落盘」会静默发生
    if DB_READY:
        checks.append({"name": "database", "level": "ok", "ok": True,
                       "message": f"会话库可用（{db.get_db_path()}）"})
    else:
        checks.append({"name": "database", "level": "warn", "ok": False,
                       "message": f"数据库初始化失败，会话与历史不再持久化，对话仍可正常回答：{DB_ERROR[:120]}"})

    if LOG_FILE_OK:
        checks.append({"name": "log_file", "level": "ok", "ok": True,
                       "message": "文件日志写入 data/logs 正常"})
    else:
        checks.append({"name": "log_file", "level": "warn", "ok": False,
                       "message": "文件日志不可用，已降级为只输出到控制台，排查请看 stdout"})

    llm_key = (API_CONFIG.get("api_key") or "").strip()
    if not llm_key:
        checks.append({"name": "llm_api_key", "level": "error", "ok": False,
                       "message": "未配置大模型 API Key，对话/行程/营销等 AI 功能不可用。请在设置页填写或配置 data/llm_config.json"})
    elif _is_default_llm_key(llm_key):
        checks.append({"name": "llm_api_key", "level": "warn", "ok": True,
                       "message": "正在使用内置的默认示例 API Key，建议尽快替换为自己的 Key"})
    else:
        checks.append({"name": "llm_api_key", "level": "ok", "ok": True, "message": "已配置"})

    provider = API_CONFIG.get("provider") or ""
    if provider not in PROVIDERS:
        checks.append({"name": "llm_provider", "level": "warn", "ok": False,
                       "message": f"未知的模型供应商 '{provider}'，将回退到默认配置"})
    else:
        checks.append({"name": "llm_provider", "level": "ok", "ok": True, "message": PROVIDERS[provider].get("name", provider)})

    if (_amap_key or "").strip():
        checks.append({"name": "amap_key", "level": "ok", "ok": True, "message": "已配置高德 Key，地图/POI/路线可用"})
    else:
        checks.append({"name": "amap_key", "level": "error", "ok": False,
                       "message": "未配置高德地图 Key，地图展示、POI 检索与路线规划将不可用。请在 data/plugin_config.json 填写 amap_key"})

    configured = len(registry.list_configured())
    total = len(registry.list_all())
    checks.append({"name": "plugins", "level": "ok" if configured else "warn",
                   "ok": configured > 0, "message": f"已配置 {configured}/{total} 个数据插件"})

    if (ARK_CONFIG.get("api_key") or "").strip():
        checks.append({"name": "ark_image_key", "level": "ok", "ok": True, "message": "已配置火山方舟生图 Key"})
    else:
        checks.append({"name": "ark_image_key", "level": "warn", "ok": True,
                       "message": "未配置火山方舟生图 Key，营销配图/海报生成功能不可用（其余功能不受影响）"})

    missing_required = [c["name"] for c in checks if c["level"] == "error"]
    warnings = [c["message"] for c in checks if c["level"] in ("error", "warn")]
    return {
        "complete": not missing_required,
        "missing_required": missing_required,
        "checks": checks,
        "warnings": warnings,
    }


def _log_config_validation() -> None:
    """启动时把配置校验结果写入日志（error→WARNING，warn→WARNING，齐全→INFO）。"""
    try:
        result = validate_config()
    except Exception as e:  # 校验本身绝不能阻断启动
        app.logger.warning("配置校验执行失败（已忽略）: %s", e)
        return
    if result["complete"] and not result["warnings"]:
        app.logger.info("启动配置校验通过：所有关键配置齐全")
        return
    for c in result["checks"]:
        if c["level"] == "error":
            app.logger.warning("配置缺失[%s]: %s", c["name"], c["message"])
        elif c["level"] == "warn":
            app.logger.warning("配置提醒[%s]: %s", c["name"], c["message"])


_log_config_validation()


@app.route('/api/ark/config', methods=['GET'])
def get_ark_config():
    """查询火山方舟生图配置（不返回明文 Key，便于运维核对当前生效的模型/尺寸）"""
    return jsonify({
        "has_key": bool(ARK_CONFIG.get('api_key')),
        "has_blog_key": bool(ARK_CONFIG.get('blog_api_key')),
        "endpoint": ARK_API_URL,
        "model": ARK_CONFIG.get('model') or ARK_MODEL_DEFAULT,
        "poster_size": ARK_CONFIG.get('poster_size') or ARK_SIZE_DEFAULT,
        "image_size": ARK_CONFIG.get('image_size') or ARK_SIZE_DEFAULT,
        "blog_size": ARK_CONFIG.get('blog_size') or ARK_CONFIG.get('poster_size') or ARK_SIZE_DEFAULT,
        "blog_model": ARK_CONFIG.get('blog_model') or ARK_CONFIG.get('model') or ARK_MODEL_DEFAULT,
        "watermark": bool(ARK_CONFIG.get('watermark', False)),
        "last_error": ARK_CONFIG.get('last_error', ''),
    })

@app.route('/api/ark/config', methods=['POST'])
def save_ark_config():
    """保存 / 清除火山方舟 API Key"""
    data = get_json_payload() or {}
    api_key = str(data.get('api_key', '')).strip()
    if api_key:
        if len(api_key) > 512:
            return jsonify({"success": False, "error": "API Key 长度异常"}), 400
        ARK_CONFIG['api_key'] = api_key
    else:
        ARK_CONFIG['api_key'] = ''
    _save_ark_config()
    return jsonify({"success": True, "has_key": bool(ARK_CONFIG['api_key'])})

def _generate_ark_image(prompt: str, scene: str = '', title: str = '', slogan: str = '', img_num: int = 1) -> tuple:
    """
    调用火山方舟 Seedream 文生图（/api/v3/images/generations）生成文创海报 / 产品图 / 博客配图。

    scene='blog' 时走旅游博客专属 Key（data/ark_config.json 的 blog_api_key）与专属模型
    （blog_model，缺省回落全局 model），未配置 Key 则回落到共享 api_key；其它场景一律用共享 api_key。

    title / slogan（可选，仅 blog 场景生效）：海报上要渲染的文字。传入后配图升级为
    「图文并茂的推广海报」——画面 + 标题大字 + 推广语，文字内容由 LLM/前端从博客主题提炼。

    img_num（仅 blog 场景生效）：1=主视觉风景、2=旅行场景、3=美食。三张均可做成带标题字的
    整版图文推广海报，但主视觉画面按 img_num 锁定各自主体（风光/人文场景/美食特写），
    保证三张海报视觉各异、互不雷同。

    返回：
        (success: bool, result: str)
        success=True 时 result 为本地可访问图片 URL
        success=False 时 result 为错误信息
    """
    scene = str(scene or '').strip().lower()
    title = str(title or '').strip()
    slogan = str(slogan or '').strip()
    try:
        img_num = int(img_num)
    except (TypeError, ValueError):
        img_num = 1
    key_field, size_field = ARK_SCENE_KEYS.get(scene, ('api_key', None))
    api_key = str(ARK_CONFIG.get(key_field) or '').strip()
    if not api_key and scene in ARK_SCENE_KEYS:
        # 场景专属 Key 未配置 → 回落共享 Key，保证功能不中断
        api_key = str(ARK_CONFIG.get('api_key') or '').strip()
        size_field = None
    if not api_key:
        return False, "请先在营销页配置火山方舟图片 API Key"
    if not prompt:
        return False, "请输入图片需求"

    if scene == 'blog':
        size = ARK_CONFIG.get('blog_size') or ARK_CONFIG.get('poster_size') or ARK_SIZE_DEFAULT
        model = ARK_CONFIG.get('blog_model') or ARK_CONFIG.get('model') or ARK_MODEL_DEFAULT
        # 三张配图统一做成同款整版宣传海报，仅按图序定制电影质感措辞：1=风景、2=旅行场景、3=美食
        _cinematic = {
            1: ("电影级风景大片", "电影感自然风光——大气开阔的构图、黄金时刻光影、远近层次分明的景深、通透空气感与胶片色调"),
            2: ("电影级旅行场景大片", "电影感旅行场景——人物与自然环境交融、自然光线、真实纪实质感、景深层次分明、胶片色调"),
            3: ("电影级美食大片", "电影感美食特写——诱人摆盘、暖色调灯光、食材质感细腻、浅景深虚化背景、胶片色调"),
        }
        cine_label, cine_desc = _cinematic.get(img_num, _cinematic[1])
        # 三张均为图文推广海报，但主视觉画面必须各自锁定本图序主体，避免海报之间雷同
        _subject = {
            1: "目的地标志性风光的全景大片（山川/湖海/古城天际线等，画面中不出现菜品食物）",
            2: "游客在景点游玩、漫步、互动的人文旅行场景（人物为主体，背景为目的地风光）",
            3: "当地特色美食的诱人特写（菜品/小吃/宴席为主体，占画面主要位置）",
        }
        subject = _subject.get(img_num, _subject[1])
        if title or slogan:
            # 整版旅游宣传海报设计：书法大标题 + 推广语 + 自由版式装饰（复刻旧版 5.0 pro 2560x1440 的质感）。
            # 不限制其它文字——装饰标签/印章/英文小字正是海报设计感的来源，只要求准确清晰。
            text_req = ''
            if title:
                text_req += f'画面主视觉位置以大气有力的中文书法字体显示主标题「{title}」，标题附近搭配优雅的英文小字副标题，'
            if slogan:
                text_req += f'版式中点缀一行中文推广语「{slogan}」作为次级标题，'
            full_prompt = (f"专业旅游目的地宣传海报（横版16:9整版设计），海报主视觉画面必须是{subject}，"
                           f"以{cine_label}质感呈现：{prompt}。"
                           f"画面强调{cine_desc}，如同一帧旅行电影的海报画面；{text_req}"
                           f"在画面主图之上做文字与图像混排的海报版式，可自然加入与目的地相关的装饰小标签、印章式小字、景点名标注，错落有层次，"
                           f"呈现高级旅游杂志与户外广告的设计质感；"
                           f"电影级真实感旅游摄影，构图完整有层次，光影通透，色调高级协调，高清细节丰富，"
                           f"所有文字必须准确清晰无错别字，不要出现水印、二维码")
        else:
            # 场景图/美食图：纯电影感实景大图，不做海报排版、不加大字标题，避免与主视觉海报雷同
            full_prompt = (f"旅游推广博客配图（{cine_label}实景大图，画面不含任何标题或海报排版文字）：{prompt}。"
                           f"{cine_desc}，电影级真实感旅游摄影，构图完整有层次，光影通透，高清细节丰富，"
                           f"如出现小字标注需准确清晰，不要出现水印logo和多余留白")
    else:
        full_prompt = f"文创产品/海报设计：{prompt}。商业插画风格，构图完整精致，色彩协调，高清细节丰富，适合文创营销展示"
        size = ARK_CONFIG.get('poster_size') or ARK_SIZE_DEFAULT
        model = ARK_CONFIG.get('model') or ARK_MODEL_DEFAULT

    # 生图的账：只有真的出过网的请求才计 requests/failed——没 Key、没提示词这种
    # 前置拦截根本没花钱，记进去会把「省了多少」算虚。
    _ark_state = {"net": False, "t0": 0.0}

    def _fail(msg):
        ARK_CONFIG['last_error'] = msg
        try:
            if _ark_state["net"]:
                _note_ark_call(scene, "failed", _usage_now() - _ark_state["t0"], msg)
        except Exception:  # noqa: BLE001
            pass
        return False, msg

    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "prompt": full_prompt,
            "sequential_image_generation": "disabled",
            "size": size,
            "response_format": "url",
            "stream": False,
            "watermark": bool(ARK_CONFIG.get("watermark", False))
        }
        if scene == 'blog':
            # 负面提示词：旧版（出好看海报时期）一直在用，能压住乱码文字与畸形
            payload["negative_prompt"] = "模糊，扭曲变形，文字乱码，错误文字，水印，logo，低清晰度，杂乱，畸形"
        # 指纹复用：同样的模型+尺寸+提示词+负面提示词，上游出的就是同一张图，
        # 再调一次就是再付一次钱（博客一篇 3 张、演示时反复点同一目的地最容易踩到）。
        # 必须**本地文件还在**才算命中——图被清过就当没命中去正常生成，
        # 绝不把一个打不开的地址当成成功返回给前端。
        fp = _ark_fingerprint(full_prompt, scene, img_num,
                              str(payload.get("model") or ""), str(payload.get("size") or ""),
                              str(payload.get("negative_prompt") or ""),
                              bool(payload.get("watermark")))
        if ARK_CACHE_ENABLED:
            hit = db.lookup_ark_image(fp) or {}
            local = _ark_static_url_to_path(str(hit.get("img_url") or ""))
            if local:
                _note_ark_call(scene, "reused")
                ARK_CONFIG['last_error'] = ''
                return True, f"/static/poster/{os.path.basename(local)}"
        _ark_state["net"] = True
        _ark_state["t0"] = _usage_now()
        resp = requests.post(ARK_API_URL, headers=headers, json=payload, timeout=150)
        if resp.status_code == 400 and 'sequential_image_generation' in resp.text:
            # Seedream 5.0+ 不再支持该字段（4.x 必须带）——去掉后自动重试一次，新旧模型自适应
            print('[ARK] 模型不支持 sequential_image_generation，去掉该参数重试')
            payload.pop('sequential_image_generation', None)
            resp = requests.post(ARK_API_URL, headers=headers, json=payload, timeout=150)
        if (resp.status_code == 403 and 'AccountOverdueError' in resp.text
                and scene in ARK_SCENE_KEYS):
            # 场景专属 Key 账户欠费 → 自动回落共享 Key + 全局模型，保证功能可用
            shared_key = str(ARK_CONFIG.get('api_key') or '').strip()
            if shared_key and shared_key != api_key:
                print('[ARK] 场景专属 Key 账户欠费，回落共享 Key + 全局模型重试')
                api_key = shared_key
                payload['model'] = str(ARK_CONFIG.get('model') or ARK_MODEL_DEFAULT)
                if 'sequential_image_generation' not in payload:
                    payload['sequential_image_generation'] = 'disabled'
                headers['Authorization'] = f'Bearer {api_key}'
                resp = requests.post(ARK_API_URL, headers=headers, json=payload, timeout=150)
                if resp.status_code == 400 and 'sequential_image_generation' in resp.text:
                    payload.pop('sequential_image_generation', None)
                    resp = requests.post(ARK_API_URL, headers=headers, json=payload, timeout=150)
        if resp.status_code != 200:
            # 欠费等常见错误转成可直接行动的中文提示，避免把原始 JSON 抛给前端
            if 'AccountOverdueError' in resp.text:
                return _fail("火山方舟账户欠费（AccountOverdueError），请到控制台充值后重试")
            if 'AuthenticationError' in resp.text or 'InvalidApiKey' in resp.text:
                return _fail("火山方舟 API Key 无效，请在营销页重新配置")
            if 'ModelNotOpen' in resp.text or 'NotFound' in resp.text:
                return _fail("火山方舟未开通该 Seedream 模型，请在控制台开通后重试")
            return _fail(f"火山方舟返回 {resp.status_code}: {resp.text[:300]}")
        result = resp.json()
        items = result.get('data') or []
        if not items or not items[0].get('url'):
            return _fail("生成结果为空")
        temp_url = items[0]['url']
        img_resp = requests.get(temp_url, timeout=60)
        if img_resp.status_code != 200:
            return _fail("图片下载失败")
        # 校验确为真实图片：过小或非 JPEG/PNG 魔术字节说明拿到的是占位/损坏内容，不落盘
        blob = img_resp.content
        if len(blob) < 5120 or not (blob[:4] == b'\x89PNG' or blob[:3] == b'\xff\xd8\xff'):
            return _fail("生成结果不是有效图片（可能仍在生成中），请稍后重试")
        folder = os.path.join(app.static_folder, 'poster')
        os.makedirs(folder, exist_ok=True)
        # Seedream 返回的是 JPEG，按真实格式落盘（历史实现存成 .png，MIME 与实际内容不符）
        ext = 'png' if blob[:4] == b'\x89PNG' else 'jpg'
        # 并发生成（博客三张配图同时出图）可能在同一秒完成，仅用秒级时间戳会撞名互相覆盖，
        # 导致两个图位显示同一张图；追加 uuid 保证文件名唯一。
        filename = f"poster_{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
        save_path = os.path.join(folder, filename)
        with open(save_path, 'wb') as f:
            f.write(blob)
        ARK_CONFIG['last_error'] = ''
        # 只缓存**校验通过且已落盘**的图：把占位图/坏图缓存下来会被无限复用，比多花一次钱糟得多。
        # 写入用最终实际发出的参数算指纹——欠费回落换过模型后，图是那个模型出的，
        # 记到原模型的指纹下就等于给下一次发了张对不上号的图。
        fp_write = _ark_fingerprint(full_prompt, scene, img_num,
                                    str(payload.get("model") or model),
                                    str(payload.get("size") or size),
                                    str(payload.get("negative_prompt") or ""),
                                    bool(payload.get("watermark")))
        db.remember_ark_image(fp_write, f"/static/poster/{filename}", scene,
                              str(payload.get("size") or size),
                              str(payload.get("model") or model))
        _note_ark_call(scene, "success", _usage_now() - _ark_state["t0"])
        return True, f"/static/poster/{filename}"
    except requests.exceptions.Timeout:
        return _fail("图片生成超时，请稍后重试")
    except Exception as e:
        print('[ARK] 生成失败:', e)
        return _fail(f"生成失败: {e}")


@app.route('/api/ark/generate', methods=['POST'])
def ark_generate():
    """调用火山方舟 Seedream 生成文创海报 / 产品图 / 博客配图，下载到本地 static 后返回可访问地址。

    blog 场景可额外传 title / slogan / img_num：三张配图均做成带标题字与推广语的整版图文推广
    海报；img_num（1=风光/2=场景/3=美食）锁定各自主视觉主体，保证三张海报互不雷同。
    """
    data = get_json_payload() or {}
    prompt = str(data.get('prompt', '')).strip()
    scene = str(data.get('scene', '')).strip().lower()
    title = str(data.get('title', '')).strip()[:24]
    slogan = str(data.get('slogan', '')).strip()[:40]
    img_num = data.get('img_num', 1)
    success, result = _generate_ark_image(prompt, scene, title, slogan, img_num)
    if success:
        return jsonify({"success": True, "img_url": result, "scene": scene or 'default'})
    return jsonify({"success": False, "error": result}), 401 if "API Key" in result else 502

if __name__ == '__main__':
    print("=" * 60)
    print("智旅灵犀 SmartTourism Server v2.2.0")
    print("=" * 60)
    print(f"项目路径: {os.path.dirname(os.path.abspath(__file__))}")
    print(f"已加载 {len(SKILL_DEFINITIONS)} 个 Skill: {', '.join(SKILL_DEFINITIONS.keys())}")
    print(f"已加载 {len(CROWD_PROFILES)} 个客群画像")
    print(f"数据库路径: {db.get_db_path()}")
    print(f"服务地址: http://localhost:5000")
    print(f"API 接口: http://localhost:5000/api/")
    print(f"运行环境: {APP_ENV} | Debug: {'ON' if DEBUG_ENABLED else 'OFF'}")
    print(f"日志目录: {LOG_DIR}")
    print("=" * 60)

    # 配置安全检查
    if _is_default_llm_key(API_CONFIG.get("api_key")):
        print("警告: 使用的是默认 API Key，建议通过前端设置页面更换为自己的 Key")

    app.run(
        host=os.getenv('SMARTTOURISM_HOST', '0.0.0.0'),
        port=int(os.getenv('SMARTTOURISM_PORT', '5050')),
        debug=DEBUG_ENABLED,
        threaded=True
    )
