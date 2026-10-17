"""
SmartTourism · 智旅灵犀 - SQLite 数据库模块
基于"后端数据库设计需求说明"实现6张核心表
"""

import sqlite3
import os
import re
import uuid
import json
import logging
import threading
import functools
from datetime import datetime
from typing import Dict, Optional

DB_PATH = os.environ.get(
    'SMARTTOURISM_DB',
    os.path.join(os.path.dirname(__file__), '..', 'data', 'smarttourism.db'))

logger = logging.getLogger("database")


def get_db_path():
    return DB_PATH


def get_connection():
    """获取数据库连接，自动启用外键约束"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db():
    """初始化数据库：建表+索引（幂等，不会重复创建）"""
    conn = get_connection()
    cursor = conn.cursor()

    # ========== 1. 会话主表 ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id      TEXT PRIMARY KEY,
            user_id         TEXT NOT NULL,
            created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_active     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            history_summary TEXT NULL
        )
    """)

    # ========== 2. 消息明细表 ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            message_id  TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            role        TEXT NOT NULL CHECK(role IN ('user','agent')),
            content     TEXT NOT NULL,
            intent_type TEXT NULL CHECK(intent_type IN ('consult','plan','marketing','cultural','other')),
            timestamp   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        )
    """)

    # ========== 3. 行程方案表 ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS itineraries (
            itinerary_id TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL,
            destination  TEXT NOT NULL,
            days         INTEGER NOT NULL CHECK(days > 0),
            budget       INTEGER NULL,
            crowd_type   TEXT NULL,
            route_json   TEXT NOT NULL,
            created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        )
    """)

    # ========== 4. 营销素材表 ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS marketing_contents (
            content_id   TEXT PRIMARY KEY,
            itinerary_id TEXT NULL,
            session_id   TEXT NOT NULL,
            platform     TEXT NOT NULL CHECK(platform IN ('xiaohongshu','wechat_moments','douyin_script','other')),
            content_type TEXT NOT NULL CHECK(content_type IN ('copywriting','script','caption')),
            raw_text     TEXT NOT NULL,
            tags         TEXT NULL,
            created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (itinerary_id) REFERENCES itineraries(itinerary_id) ON DELETE SET NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        )
    """)

    # ========== 5. 外部导入记录表 ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS imported_sources (
            source_id    TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL,
            source_url   TEXT NULL,
            raw_content  TEXT NULL,
            parsed_json  TEXT NULL,
            status       TEXT NOT NULL DEFAULT 'pending'
                         CHECK(status IN ('pending','success','failed')),
            error_msg    TEXT NULL,
            created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        )
    """)

    # ========== 6. 操作日志表 ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS operation_logs (
            log_id       TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL,
            action_type  TEXT NOT NULL CHECK(action_type IN ('consult','plan','marketing','cultural','import','other')),
            skill_used   TEXT NULL,
            cost_time    INTEGER NULL,
            success_flag BOOLEAN NOT NULL,
            error_detail TEXT NULL,
            created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        )
    """)

    # ========== 7. 用户数据存储表（持久化个人中心数据） ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_store (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL DEFAULT 'default',
            category    TEXT NOT NULL CHECK(category IN ('orders','itineraries','hotels','flights')),
            data_json   TEXT NOT NULL,
            created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ========== 8. 小红书文旅灵感库 ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS xhs_notes (
            note_id             TEXT PRIMARY KEY,
            title               TEXT NOT NULL,
            source_url          TEXT NOT NULL UNIQUE,
            source_type         TEXT NOT NULL DEFAULT 'xhs_search',
            destination         TEXT NOT NULL,
            city                TEXT NOT NULL,
            province            TEXT NULL,
            theme               TEXT NOT NULL,
            crowd               TEXT NULL,
            season              TEXT NULL,
            budget_level        TEXT NULL,
            duration_suggestion TEXT NULL,
            summary             TEXT NOT NULL,
            highlights          TEXT NULL,
            pitfalls            TEXT NULL,
            tags                TEXT NULL,
            score               REAL NOT NULL DEFAULT 0,
            compliance_note     TEXT NULL,
            created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ========== 9. 全国城市基础库 ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS travel_cities (
            adcode      TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            province    TEXT NOT NULL,
            city_level  TEXT NULL,
            aliases     TEXT NULL,
            is_hot      INTEGER NOT NULL DEFAULT 0,
            source      TEXT NOT NULL DEFAULT 'seed',
            created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ========== 10. 全国文旅 POI 知识库 ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS travel_pois (
            poi_id         TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            province       TEXT NULL,
            city           TEXT NOT NULL,
            adcode         TEXT NULL,
            category       TEXT NOT NULL,
            poi_type       TEXT NULL,
            level          TEXT NULL,
            address        TEXT NULL,
            lng            REAL NULL,
            lat            REAL NULL,
            rating         REAL NULL,
            price_info     TEXT NULL,
            tags_json      TEXT NULL,
            description    TEXT NULL,
            photo_url      TEXT NULL,
            source         TEXT NOT NULL DEFAULT 'local_seed',
            source_payload TEXT NULL,
            created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (adcode) REFERENCES travel_cities(adcode) ON DELETE SET NULL
        )
    """)

    # ========== 20. 用户账号表 ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id       TEXT PRIMARY KEY,
            username      TEXT NOT NULL,
            account       TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ========== 21. 登录令牌表 ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS auth_tokens (
            token      TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME NOT NULL
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens(user_id)")

    # ========== 22. 优惠券发放表 ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS coupon_claims (
            id         TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL,
            title      TEXT NOT NULL,
            value      INTEGER NOT NULL DEFAULT 10,
            desc_text  TEXT NULL,
            source     TEXT NULL,
            status     TEXT NOT NULL DEFAULT '可用',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_coupon_user ON coupon_claims(user_id)")

    # ========== 23. 会话状态表（已确认事实 + 摘要折叠进度，重启后可恢复） ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_state (
            session_id     TEXT PRIMARY KEY,
            facts_json     TEXT NULL,
            folded_rounds  INTEGER NOT NULL DEFAULT 0,
            updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ========== 24. 长期记忆表（跨会话沉淀的用户偏好/约束/事实） ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            memory_id      TEXT PRIMARY KEY,
            user_id        TEXT NOT NULL,
            kind           TEXT NOT NULL DEFAULT 'preference'
                           CHECK(kind IN ('preference','fact','constraint','event')),
            slot           TEXT NULL,
            content        TEXT NOT NULL,
            source_session TEXT NULL,
            confidence     REAL NOT NULL DEFAULT 0.6,
            hit_count      INTEGER NOT NULL DEFAULT 0,
            embedding      TEXT NULL,
            created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at   DATETIME NULL,
            updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_user_slot ON memories(user_id, slot)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_user_updated ON memories(user_id, updated_at DESC)")

    # ========== 25. 答复反馈表（点赞/点踩，改进与评估的原始信号） ==========
    # 一人对一条答复只有一票（UNIQUE(message_id,user_id)），改票用 UPSERT 覆盖旧值，
    # 不然「先点踩再点赞」会在统计里同时算两票。
    # message_id 走外键 + ON DELETE CASCADE：删会话/清空历史时票跟着答复一起消失，
    # 不留指向已删答复的孤票——那些差评既查不到出处，又把指标永久压低。
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS message_feedback (
            feedback_id  TEXT PRIMARY KEY,
            message_id   TEXT NOT NULL,
            session_id   TEXT NOT NULL,
            user_id      TEXT NOT NULL,
            rating       TEXT NOT NULL CHECK(rating IN ('up','down')),
            reason       TEXT NULL,
            created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(message_id) REFERENCES messages(message_id) ON DELETE CASCADE
        )
    """)


    # ========== 26. 生图指纹复用表（同一张图不付第二次钱） ==========
    # 火山方舟 Seedream 按张计费，一篇博客要出 3 张图。model+size+完整提示词都一样时，
    # 上游出的就是同一张图，再调一次就是再付一次钱——演示时反复生成同一目的地、
    # 前端超时后再点一次，都属于这种重复付费。所以按指纹把已落盘的地址记下来直接复用。
    # 这是「派生缓存」不是用户数据：读不到就当没命中去正常生成，写不到也不影响这次出图，
    # 因此 DAO 加 @guarded()（见 _ark 相关函数的说明），坏了只会退化成「多花一次钱」而不是功能失败。
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ark_image_cache (
            fingerprint  TEXT PRIMARY KEY,
            img_url      TEXT NOT NULL,
            scene        TEXT NOT NULL DEFAULT '',
            size         TEXT NOT NULL DEFAULT '',
            model        TEXT NOT NULL DEFAULT '',
            reuse_count  INTEGER NOT NULL DEFAULT 0,
            created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ark_cache_scene ON ark_image_cache(scene)")


    # ========== 轻量列迁移（老库补列，幂等） ==========
    # messages.truncated：用户点「停止生成」或客户端断连导致的半截答复要如实落库标记，
    # 否则重新打开会话时，未完成的回答会被当成完整答复继续引用与上下文回填。
    # messages.sources_json：本轮真实检索到的知识库出处，历史回看时还能追溯依据。
    # sessions.title：会话标题（首条提问自动生成，可改名）。多会话列表没有标题就认不出来，
    # 而 session_id 是 uuid，用户无法据此找回上次问的内容。
    for _tbl, _col, _decl in (("messages", "truncated", "INTEGER NOT NULL DEFAULT 0"),
                              ("messages", "sources_json", "TEXT NULL"),
                              ("sessions", "title", "TEXT NULL")):
        try:
            cursor.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_decl}")
        except sqlite3.OperationalError:
            pass  # duplicate column name：已迁移过

    # ========== 创建索引 ==========
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_last_active ON sessions(last_active)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_time ON messages(session_id, timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_intent_type ON messages(intent_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_itineraries_session ON itineraries(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_itineraries_dest_date ON itineraries(destination, created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_marketing_platform ON marketing_contents(platform)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_marketing_session ON marketing_contents(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_imported_session ON imported_sources(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_imported_status ON imported_sources(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_logs_action_time ON operation_logs(action_type, created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_store_user_cat ON user_store(user_id, category)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_xhs_city ON xhs_notes(city)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_xhs_theme ON xhs_notes(theme)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_xhs_crowd ON xhs_notes(crowd)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_xhs_score ON xhs_notes(score DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_travel_cities_name ON travel_cities(name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_travel_cities_province ON travel_cities(province)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_travel_pois_city_cat ON travel_pois(city, category)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_travel_pois_name ON travel_pois(name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_travel_pois_rating ON travel_pois(rating DESC)")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_feedback_message_user "
                   "ON message_feedback(message_id, user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_feedback_session ON message_feedback(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_feedback_time ON message_feedback(updated_at DESC)")

    conn.commit()
    conn.close()
    print(f"[DB] 数据库初始化完成: {DB_PATH}")


# ==================== DAO 降级护栏 ====================
# 只给「失败可以降级」的 DAO 加护栏：隐式/后台写入（上下文摘要、会话状态、记忆命中与修剪、计数）。
# 用户主动发起的 CRUD（行程、营销内容、小红书笔记、收藏、会话创建、消息保存、各类 list/get）
# 一律不加——那类失败必须原样抛给调用方回 500，返回空值等于假装保存成功。
# 降级必须留痕：database logger 一条 WARNING（app.py 已把它接进 app.log / error.log），
# 外加 db_health() 的计数，供 /api/admin/ops 观察「库是不是在悄悄坏掉」。
_db_failures: Dict[str, int] = {}
_db_last_error: Dict[str, dict] = {}
_db_lock = threading.Lock()


def _report(label: str, exc: BaseException) -> None:
    """记一次 DAO 降级。只存字符串，不持有异常对象——traceback 会把连接和帧一起引用着不放。"""
    message = str(exc)[:200]
    exc_type = type(exc).__name__
    with _db_lock:
        _db_failures[label] = _db_failures.get(label, 0) + 1
        _db_last_error[label] = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "exc_type": exc_type,
            "message": message,
        }
    logger.warning("DAO %s 失败，已降级返回空值: %s: %s", label, exc_type, message)


def guarded(default=None, label: str = None):
    """装饰器：DAO 抛异常时记痕并返回 default（可传 list/dict 等工厂，避免多次调用共享同一对象）。"""
    def deco(fn):
        name = label or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                _report(name, exc)
                return default() if callable(default) else default
        return wrapper
    return deco


def db_health() -> dict:
    """DAO 降级情况：总失败数、按函数计数、每个函数最近一次错误。"""
    with _db_lock:
        total = sum(_db_failures.values())
        return {
            "degraded": total > 0,
            "total_failures": total,
            "by_function": dict(_db_failures),
            "last_errors": dict(_db_last_error),
        }


# ==================== DAO 操作函数 ====================

def create_session(user_id: str = "default", session_id: str = None) -> dict:
    """创建新会话，返回会话信息"""
    if session_id is None:
        session_id = str(uuid.uuid4())
    conn = get_connection()
    conn.execute(
        "INSERT INTO sessions (session_id, user_id) VALUES (?, ?)",
        (session_id, user_id)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    conn.close()
    return dict(row)


def update_session_active(session_id: str):
    """更新会话最后活跃时间"""
    conn = get_connection()
    conn.execute(
        "UPDATE sessions SET last_active = CURRENT_TIMESTAMP WHERE session_id = ?",
        (session_id,)
    )
    conn.commit()
    conn.close()


@guarded()
def update_session_summary(session_id: str, summary: str):
    """更新会话历史摘要"""
    conn = get_connection()
    conn.execute(
        "UPDATE sessions SET history_summary = ? WHERE session_id = ?",
        (summary, session_id)
    )
    conn.commit()
    conn.close()


def get_session(session_id: str) -> Optional[dict]:
    """获取会话信息"""
    conn = get_connection()
    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_user_sessions(user_id: str = "default", limit: int = 50) -> list:
    """列出用户的所有会话（按最后活跃时间倒序），带上标题、消息条数与末条摘要。

    多会话列表只有 uuid 是没法用的——用户要靠标题和「上次问到哪」认出哪一条，
    所以 title / message_count / preview 在 SQL 里一次取齐，不让前端逐条再查。
    first_message 是给加 title 列之前建的会话用的：那些老会话没标题，
    拿首条提问顶上比拿末条答复更像「这条对话在问什么」。
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT s.session_id, s.user_id, s.created_at, s.last_active, s.history_summary, s.title,
               (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.session_id) AS message_count,
               (SELECT substr(replace(replace(m.content, char(10), ' '), char(13), ' '), 1, 60)
                  FROM messages m WHERE m.session_id = s.session_id
                 ORDER BY m.rowid ASC LIMIT 1) AS first_message,
               (SELECT substr(replace(replace(m.content, char(10), ' '), char(13), ' '), 1, 60)
                  FROM messages m WHERE m.session_id = s.session_id
                 ORDER BY m.rowid DESC LIMIT 1) AS preview
          FROM sessions s
         WHERE s.user_id = ?
         ORDER BY s.last_active DESC, s.rowid DESC
         LIMIT ?
        """,
        (user_id, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def title_from_message(content: str, max_len: int = 20) -> str:
    """从首条提问生成标题：压掉换行、截断，不给 LLM 起名（一次生成 = 一次额外延迟与费用）"""
    text = re.sub(r"\s+", " ", (content or "").strip())
    if not text:
        return ""
    return text[:max_len] + ("…" if len(text) > max_len else "")


@guarded()
def set_session_title(session_id: str, title: str, only_if_empty: bool = True) -> int:
    """自动补标题（首条提问）——背景写入，失败不影响本轮答复，故加护栏"""
    if not title:
        return 0
    conn = get_connection()
    sql = "UPDATE sessions SET title = ? WHERE session_id = ?"
    params: tuple = (title, session_id)
    if only_if_empty:
        sql += " AND (title IS NULL OR title = '')"
    cur = conn.execute(sql, params)
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed


def rename_session(session_id: str, user_id: str, title: str) -> bool:
    """用户手动改名：只动自己名下的会话。属用户数据写操作，失败要报错，不加护栏"""
    title = (title or "").strip()[:60]
    if not title:
        return False
    conn = get_connection()
    try:
        cur = conn.execute("UPDATE sessions SET title = ? WHERE session_id = ? AND user_id = ?",
                           (title, session_id, user_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_session(session_id: str, user_id: str) -> bool:
    """删除整条会话（messages 等子表由外键 ON DELETE CASCADE 一起删）。

    这是不可恢复的用户数据删除，所以：只按 user_id 匹配才删，且失败必须抛出而不是返回 False。
    """
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM sessions WHERE session_id = ? AND user_id = ?",
                           (session_id, user_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def save_message(session_id: str, role: str, content: str, intent_type: str = None, message_id: str = None,
                 truncated: bool = False, sources_json: str = None) -> dict:
    """保存一条消息

    truncated=True 表示中断产生的未完整答复；sources_json 是本轮真实检索到的知识库出处
    （没有检索到就存 NULL，不能事后补一个看起来像来源的东西）。
    """
    if message_id is None:
        message_id = str(uuid.uuid4())
    conn = get_connection()
    conn.execute(
        "INSERT INTO messages (message_id, session_id, role, content, intent_type, truncated, sources_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (message_id, session_id, role, content, intent_type, 1 if truncated else 0, sources_json)
    )
    conn.commit()
    # 同时更新会话活跃时间
    conn.execute("UPDATE sessions SET last_active = CURRENT_TIMESTAMP WHERE session_id = ?", (session_id,))
    conn.commit()
    row = conn.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
    conn.close()
    return dict(row)


def get_session_messages(session_id: str, limit: int = 100, offset: int = 0) -> list:
    """获取会话的消息历史

    按 rowid 而不是 timestamp 排序：CURRENT_TIMESTAMP 只到秒，同秒写入的
    user/agent 两条消息按 timestamp 排会乱序，回填上下文和「重跑最后一轮」都会错。
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY rowid ASC LIMIT ? OFFSET ?",
        (session_id, limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_last_turn(session_id: str) -> int:
    """删掉最后一轮（最后一条 agent 消息 + 它前面的那条 user 消息），返回删除条数。

    「重新生成 / 编辑重发」用：先把这一轮撤回，再按原样或改写后的问题重跑，
    避免同一问题在历史里留下两条 agent 答复。
    末尾只有一条提问（答复没落库，比如断连或报错）时只删那一条——
    多删一条就会把上一轮的答复一起带走，那不是「撤回这一轮」。
    用户数据删除，失败必须原样抛给调用方，不加降级护栏。
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT message_id, role FROM messages WHERE session_id = ? ORDER BY rowid DESC LIMIT 2",
            (session_id,)
        ).fetchall()
        if not rows:
            return 0
        targets = [rows[0]["message_id"]]
        if rows[0]["role"] == "agent" and len(rows) > 1 and rows[1]["role"] == "user":
            targets.append(rows[1]["message_id"])
        for mid in targets:
            conn.execute("DELETE FROM messages WHERE message_id = ?", (mid,))
        conn.commit()
        return len(targets)
    finally:
        conn.close()


def get_last_user_message(session_id: str) -> Optional[str]:
    """取会话里最后一条用户消息内容（重新生成时不必让前端回传原文）"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'user' "
            "ORDER BY rowid DESC LIMIT 1",
            (session_id,)
        ).fetchone()
        return row["content"] if row else None
    finally:
        conn.close()


# ==================== 答复反馈（点赞 / 点踩） ====================

FEEDBACK_RATINGS = ("up", "down")


def save_feedback(message_id: str, session_id: str, user_id: str,
                  rating: str, reason: str = None) -> dict:
    """给一条 agent 答复记一票；同一人同一条答复改票覆盖旧值。

    用户主动发起的写操作：异常原样抛给路由，不返回空值冒充「反馈已收到」。
    归属校验写在 SQL 里（答复属于该会话、会话属于该用户、且只能是 agent 答复），
    查不到就返回 message_not_found，而不是给别人会话的答复投票。
    """
    if rating not in FEEDBACK_RATINGS:
        return {"ok": False, "code": "bad_rating", "error": "rating 只能是 up 或 down"}
    message_id = (message_id or "").strip()
    session_id = (session_id or "").strip()
    user_id = (user_id or "").strip()
    if not message_id or not session_id or not user_id:
        return {"ok": False, "code": "bad_request", "error": "缺少 message_id / session_id / user_id"}
    reason = (reason or "").strip()[:200] or None

    conn = get_connection()
    try:
        owned = conn.execute(
            "SELECT m.message_id FROM messages m "
            "JOIN sessions s ON s.session_id = m.session_id "
            "WHERE m.message_id = ? AND m.session_id = ? AND s.user_id = ? AND m.role = 'agent'",
            (message_id, session_id, user_id)
        ).fetchone()
        if not owned:
            return {"ok": False, "code": "message_not_found"}
        conn.execute(
            "INSERT INTO message_feedback (feedback_id, message_id, session_id, user_id, rating, reason) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id, user_id) DO UPDATE SET "
            "rating = excluded.rating, reason = excluded.reason, updated_at = CURRENT_TIMESTAMP",
            (str(uuid.uuid4()), message_id, session_id, user_id, rating, reason)
        )
        conn.commit()
        row = conn.execute("SELECT * FROM message_feedback WHERE message_id = ? AND user_id = ?",
                           (message_id, user_id)).fetchone()
        return {"ok": True, "feedback": dict(row)}
    finally:
        conn.close()


def delete_feedback(message_id: str, user_id: str) -> int:
    """取消某人对一条答复的票（再点一次=取消）。返回删除条数。"""
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM message_feedback WHERE message_id = ? AND user_id = ?",
                           ((message_id or "").strip(), (user_id or "").strip()))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_feedback_map(session_id: str, user_id: str) -> dict:
    """这条会话里这个人投过的票：message_id -> {rating, reason}，历史回放时标出已投状态。"""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT message_id, rating, reason FROM message_feedback "
            "WHERE session_id = ? AND user_id = ?",
            (session_id, user_id)
        ).fetchall()
        return {r["message_id"]: {"rating": r["rating"], "reason": r["reason"]} for r in rows}
    finally:
        conn.close()


def feedback_stats(recent_days: int = 7) -> dict:
    """票数概览：总数/好差/近 N 天分布（UTC 日期，与消息时间戳同一口径）/点踩理由 TOP。"""
    conn = get_connection()
    try:
        counts = {r["rating"]: r["c"] for r in conn.execute(
            "SELECT rating, COUNT(*) AS c FROM message_feedback GROUP BY rating").fetchall()}
        total = sum(counts.values())
        by_day = [{"date": r["d"], "up": r["up"], "down": r["down"]} for r in conn.execute(
            "SELECT date(updated_at) AS d, "
            "SUM(CASE WHEN rating = 'up' THEN 1 ELSE 0 END) AS up, "
            "SUM(CASE WHEN rating = 'down' THEN 1 ELSE 0 END) AS down "
            "FROM message_feedback GROUP BY d ORDER BY d DESC LIMIT ?",
            (max(1, min(int(recent_days or 7), 60)),)
        ).fetchall()]
        reasons = [{"reason": r["reason"], "count": r["c"]} for r in conn.execute(
            "SELECT reason, COUNT(*) AS c FROM message_feedback "
            "WHERE rating = 'down' AND reason IS NOT NULL AND reason != '' "
            "GROUP BY reason ORDER BY c DESC LIMIT 5").fetchall()]
        return {
            "total": total,
            "up": counts.get("up", 0),
            "down": counts.get("down", 0),
            "up_rate": round(counts.get("up", 0) / total, 3) if total else None,
            "sessions": conn.execute(
                "SELECT COUNT(DISTINCT session_id) AS c FROM message_feedback").fetchone()["c"],
            "by_day": list(reversed(by_day)),
            "top_down_reasons": reasons,
        }
    finally:
        conn.close()


def recent_agent_answers(limit: int = 50) -> list:
    """最近的 agent 答复，带上紧邻它前面那条提问（按 rowid 相邻配对，不看时间戳）。

    给「答复质量评估」当样本：指标要跑在真实问答上，不能只跑内置用例集。
    末尾形状对不上（比如答复前不是提问）时 question 为 None，不硬凑一个问题。
    """
    limit = max(1, min(int(limit or 50), 500))
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT rowid AS rid, message_id, session_id, role, content, intent_type, "
            "truncated, sources_json, timestamp FROM messages ORDER BY rowid DESC LIMIT ?",
            (limit * 2 + 2,)
        ).fetchall()
        out = []
        for i, r in enumerate(rows):
            if r["role"] != "agent":
                continue
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            question = nxt["content"] if (nxt is not None and nxt["role"] == "user") else None
            fb = conn.execute("SELECT rating, reason FROM message_feedback WHERE message_id = ? "
                              "ORDER BY updated_at DESC LIMIT 1", (r["message_id"],)).fetchone()
            out.append({
                "message_id": r["message_id"],
                "session_id": r["session_id"],
                "question": question,
                "answer": r["content"],
                "intent_type": r["intent_type"],
                "truncated": bool(r["truncated"]),
                "has_sources": bool(r["sources_json"]),
                "rating": fb["rating"] if fb else None,
                "feedback_reason": fb["reason"] if fb else None,
                "timestamp": r["timestamp"],
            })
            if len(out) >= limit:
                break
        return out
    finally:
        conn.close()


def save_itinerary(session_id: str, destination: str, days: int, route_json: str,
                   budget: int = None, crowd_type: str = None, itinerary_id: str = None) -> dict:
    """保存行程方案"""
    if itinerary_id is None:
        itinerary_id = str(uuid.uuid4())
    conn = get_connection()
    conn.execute(
        "INSERT INTO itineraries (itinerary_id, session_id, destination, days, budget, crowd_type, route_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (itinerary_id, session_id, destination, days, budget, crowd_type, route_json)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM itineraries WHERE itinerary_id = ?", (itinerary_id,)).fetchone()
    conn.close()
    return dict(row)


def get_itinerary(itinerary_id: str) -> Optional[dict]:
    """获取行程详情"""
    conn = get_connection()
    row = conn.execute("SELECT * FROM itineraries WHERE itinerary_id = ?", (itinerary_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_itinerary(itinerary_id: str, route_json: str) -> Optional[dict]:
    """更新行程（前端拖拽后回存）"""
    conn = get_connection()
    conn.execute(
        "UPDATE itineraries SET route_json = ? WHERE itinerary_id = ?",
        (route_json, itinerary_id)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM itineraries WHERE itinerary_id = ?", (itinerary_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_session_itineraries(session_id: str) -> list:
    """列出某会话的所有行程"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM itineraries WHERE session_id = ? ORDER BY created_at DESC",
        (session_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_marketing_content(session_id: str, platform: str, content_type: str, raw_text: str,
                           itinerary_id: str = None, tags: str = None, content_id: str = None) -> dict:
    """保存营销素材"""
    if content_id is None:
        content_id = str(uuid.uuid4())
    conn = get_connection()
    conn.execute(
        "INSERT INTO marketing_contents (content_id, itinerary_id, session_id, platform, content_type, raw_text, tags) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (content_id, itinerary_id, session_id, platform, content_type, raw_text, tags)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM marketing_contents WHERE content_id = ?", (content_id,)).fetchone()
    conn.close()
    return dict(row)


def get_marketing_content(content_id: str) -> Optional[dict]:
    """获取营销素材详情"""
    conn = get_connection()
    row = conn.execute("SELECT * FROM marketing_contents WHERE content_id = ?", (content_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_imported_source(session_id: str, source_url: str = None, raw_content: str = None,
                         source_id: str = None) -> dict:
    """保存外部导入记录"""
    if source_id is None:
        source_id = str(uuid.uuid4())
    conn = get_connection()
    conn.execute(
        "INSERT INTO imported_sources (source_id, session_id, source_url, raw_content, status) "
        "VALUES (?, ?, ?, ?, 'pending')",
        (source_id, session_id, source_url, raw_content)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM imported_sources WHERE source_id = ?", (source_id,)).fetchone()
    conn.close()
    return dict(row)


def update_imported_source(source_id: str, parsed_json: str = None, status: str = None, error_msg: str = None):
    """更新导入记录状态"""
    conn = get_connection()
    fields = []
    values = []
    if parsed_json is not None:
        fields.append("parsed_json = ?")
        values.append(parsed_json)
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if error_msg is not None:
        fields.append("error_msg = ?")
        values.append(error_msg)
    if not fields:
        conn.close()
        return
    values.append(source_id)
    conn.execute(f"UPDATE imported_sources SET {', '.join(fields)} WHERE source_id = ?", values)
    conn.commit()
    conn.close()


def save_operation_log(session_id: str, action_type: str, success_flag: bool,
                       skill_used: str = None, cost_time: int = None, error_detail: str = None,
                       log_id: str = None) -> dict:
    """保存操作日志"""
    if log_id is None:
        log_id = str(uuid.uuid4())
    conn = get_connection()
    conn.execute(
        "INSERT INTO operation_logs (log_id, session_id, action_type, skill_used, cost_time, success_flag, error_detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (log_id, session_id, action_type, skill_used, cost_time, bool(success_flag), error_detail)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM operation_logs WHERE log_id = ?", (log_id,)).fetchone()
    conn.close()
    return dict(row)


def get_stats() -> dict:
    """获取统计看板数据（P2功能）"""
    conn = get_connection()
    stats = {}

    # 总会话数
    row = conn.execute("SELECT COUNT(*) as cnt FROM sessions").fetchone()
    stats['total_sessions'] = row['cnt']

    # 总消息数
    row = conn.execute("SELECT COUNT(*) as cnt FROM messages").fetchone()
    stats['total_messages'] = row['cnt']

    # 总行程数
    row = conn.execute("SELECT COUNT(*) as cnt FROM itineraries").fetchone()
    stats['total_itineraries'] = row['cnt']

    # 总营销素材数
    row = conn.execute("SELECT COUNT(*) as cnt FROM marketing_contents").fetchone()
    stats['total_marketing'] = row['cnt']

    # 按意图统计消息
    rows = conn.execute(
        "SELECT intent_type, COUNT(*) as cnt FROM messages WHERE intent_type IS NOT NULL "
        "GROUP BY intent_type ORDER BY cnt DESC"
    ).fetchall()
    stats['intent_distribution'] = {r['intent_type']: r['cnt'] for r in rows}

    # 按平台统计营销素材
    rows = conn.execute(
        "SELECT platform, COUNT(*) as cnt FROM marketing_contents GROUP BY platform ORDER BY cnt DESC"
    ).fetchall()
    stats['platform_distribution'] = {r['platform']: r['cnt'] for r in rows}

    # 操作成功率
    row = conn.execute(
        "SELECT COUNT(*) as total, SUM(CASE WHEN success_flag THEN 1 ELSE 0 END) as success "
        "FROM operation_logs"
    ).fetchone()
    if row['total'] > 0:
        stats['operation_success_rate'] = round(row['success'] / row['total'] * 100, 1)
    else:
        stats['operation_success_rate'] = 0

    conn.close()
    return stats


def upsert_xhs_note(note: dict) -> dict:
    """新增或更新小红书灵感元数据。仅保存链接、摘要与标签，不搬运原文正文。"""
    note_id = note.get('note_id') or str(uuid.uuid4())
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO xhs_notes (
            note_id, title, source_url, source_type, destination, city, province,
            theme, crowd, season, budget_level, duration_suggestion, summary,
            highlights, pitfalls, tags, score, compliance_note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_url) DO UPDATE SET
            title = excluded.title,
            source_type = excluded.source_type,
            destination = excluded.destination,
            city = excluded.city,
            province = excluded.province,
            theme = excluded.theme,
            crowd = excluded.crowd,
            season = excluded.season,
            budget_level = excluded.budget_level,
            duration_suggestion = excluded.duration_suggestion,
            summary = excluded.summary,
            highlights = excluded.highlights,
            pitfalls = excluded.pitfalls,
            tags = excluded.tags,
            score = excluded.score,
            compliance_note = excluded.compliance_note,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            note_id,
            note['title'],
            note['source_url'],
            note.get('source_type', 'xhs_search'),
            note['destination'],
            note['city'],
            note.get('province'),
            note['theme'],
            note.get('crowd'),
            note.get('season'),
            note.get('budget_level'),
            note.get('duration_suggestion'),
            note['summary'],
            json.dumps(note.get('highlights', []), ensure_ascii=False),
            json.dumps(note.get('pitfalls', []), ensure_ascii=False),
            json.dumps(note.get('tags', []), ensure_ascii=False),
            float(note.get('score', 0)),
            note.get('compliance_note', '仅保存公开入口、人工摘要和筛选标签，完整内容跳转原平台查看。')
        )
    )
    conn.commit()
    row = conn.execute("SELECT * FROM xhs_notes WHERE source_url = ?", (note['source_url'],)).fetchone()
    conn.close()
    return _decode_xhs_note(dict(row)) if row else {}


def list_xhs_notes(keyword: str = None, city: str = None, theme: str = None,
                   crowd: str = None, source_type: str = None, limit: int = 50) -> list:
    """按关键词、城市、主题、客群和平台来源筛选文旅灵感元数据。"""
    conn = get_connection()
    clauses = []
    values = []
    if keyword:
        like = f"%{keyword}%"
        clauses.append("(title LIKE ? OR destination LIKE ? OR summary LIKE ? OR tags LIKE ?)")
        values.extend([like, like, like, like])
    if city and city != '全部':
        clauses.append("city = ?")
        values.append(city)
    if theme and theme != '全部':
        clauses.append("theme = ?")
        values.append(theme)
    if crowd and crowd != '全部':
        clauses.append("(crowd = ? OR crowd LIKE ?)")
        values.extend([crowd, f"%{crowd}%"])
    if source_type and source_type != '全部':
        clauses.append("source_type = ?")
        values.append(source_type)
    # 排除通用平台热点标题（如"抖音热点""抖音精选"等无具体景点/美食内容的词条）
    generic_titles = [
        '抖音', '热点', '热门', '未命名热点', '抖音热点', '抖音精选', '抖音课堂',
        '实时平台', '抖音热搜', '热榜', '热搜', '热门话题', '实时热点', '实时',
        '小红书', '小红书热点', '小红书热搜', '推荐', '榜单', '排行榜', '热门推荐',
        '抖音推荐', '精选', '课堂', '话题', '官方', '授权', '平台数据',
    ]
    placeholders = ",".join("?" for _ in generic_titles)
    clauses.append(f"title NOT IN ({placeholders})")
    values.extend(generic_titles)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    values.append(limit)
    rows = conn.execute(
        f"SELECT * FROM xhs_notes{where} ORDER BY score DESC, updated_at DESC LIMIT ?",
        values
    ).fetchall()
    conn.close()
    return [_decode_xhs_note(dict(r)) for r in rows]


def get_xhs_note(note_id: str) -> Optional[dict]:
    """获取单条平台灵感元数据。"""
    conn = get_connection()
    row = conn.execute("SELECT * FROM xhs_notes WHERE note_id = ?", (note_id,)).fetchone()
    conn.close()
    return _decode_xhs_note(dict(row)) if row else None


def get_xhs_note_facets() -> dict:
    """获取灵感库筛选项。"""
    conn = get_connection()
    result = {}
    for field in ['city', 'theme', 'crowd', 'season', 'budget_level', 'source_type']:
        rows = conn.execute(
            f"SELECT DISTINCT {field} AS value FROM xhs_notes WHERE {field} IS NOT NULL AND {field} != '' ORDER BY value"
        ).fetchall()
        result[field] = [r['value'] for r in rows]
    row = conn.execute("SELECT COUNT(*) AS cnt FROM xhs_notes").fetchone()
    result['total'] = row['cnt'] if row else 0
    conn.close()
    return result


def _decode_xhs_note(row: dict) -> dict:
    """解码 JSON 字段，便于 API 直接返回。"""
    for key in ['highlights', 'pitfalls', 'tags']:
        value = row.get(key)
        if isinstance(value, str):
            try:
                row[key] = json.loads(value) if value else []
            except json.JSONDecodeError:
                row[key] = []
    return row


def save_user_store_item(user_id: str, category: str, item: dict, item_id: str = None) -> dict:
    """保存一条用户数据到持久化存储"""
    if item_id is None:
        import random
        item_id = f'{category[:3].upper()}{datetime.now().strftime("%Y%m%d%H%M%S")}{random.randint(100,999)}'
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO user_store (id, user_id, category, data_json) VALUES (?, ?, ?, ?)",
            (item_id, user_id, category, json.dumps(item, ensure_ascii=False))
        )
        conn.commit()
        row = conn.execute("SELECT * FROM user_store WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_store_items(user_id: str, category: str = None, limit: int = 100) -> dict:
    """获取用户存储数据，返回按 category 分组的结果"""
    conn = get_connection()
    if category:
        rows = conn.execute(
            "SELECT * FROM user_store WHERE user_id = ? AND category = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, category, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM user_store WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
    conn.close()

    result = {'orders': [], 'itineraries': [], 'hotels': [], 'flights': [], 'coupons': []}
    for row in rows:
        item = json.loads(row['data_json'])
        item['id'] = row['id']
        item['created_at'] = row['created_at']
        cat = row['category']
        if cat in result:
            result[cat].append(item)
    return result


def clear_user_store(user_id: str, category: str = None):
    """清除用户存储数据"""
    conn = get_connection()
    if category:
        conn.execute("DELETE FROM user_store WHERE user_id = ? AND category = ?", (user_id, category))
    else:
        conn.execute("DELETE FROM user_store WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def delete_user_store_item(item_id: str, user_id: str) -> int:
    """删除单条存储数据，只删属于该用户的行，返回受影响行数。

    必须带上 user_id：id 是「类别前缀+时间戳+随机数」这种可猜的短号，
    只按 id 删等于任何人拿到号就能删别人的行程。返回 rowcount 是为了让调用方
    能区分「真删掉了」和「这条不是你的/早就不在了」，不能删不动还回成功。
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM user_store WHERE id = ? AND user_id = ?", (item_id, user_id)
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def export_user_data(user_id: str) -> dict:
    """把某个用户名下的全部业务数据聚合成一份可下载的 JSON（数据可携带/安全导出）。

    三条硬约束：
    1. 每张表都按 user_id 或「该用户的 session_id」过滤，绝不返回别人的行——会话 id 是
       可猜的 uuid 片段，跨用户串数据是最容易犯的错，所以子表一律先从 sessions 取回
       属于该用户的 session_id 集合，再用 IN (?) 过滤，不接受调用方直接给 session_id。
    2. 这是用户主动发起的读取，按 DAO 口径**不加 @guarded()**：库读不出来就必须抛，
       让路由回 500；返回一份空壳「导出成功」等于把用户的数据弄丢了还说导出了。
    3. 只做读，不写盘、不落文件——导出内容直接回给浏览器下载。
    """
    conn = get_connection()
    try:
        sessions = [dict(r) for r in conn.execute(
            "SELECT session_id, created_at, last_active, history_summary FROM sessions "
            "WHERE user_id = ? ORDER BY last_active DESC", (user_id,)
        ).fetchall()]
        sids = [s['session_id'] for s in sessions]

        def _by_session(table, order):
            if not sids:
                return []
            marks = ",".join("?" * len(sids))
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM {table} WHERE session_id IN ({marks}) ORDER BY {order}",
                sids
            ).fetchall()]

        messages = _by_session('messages', 'timestamp ASC')
        itineraries = _by_session('itineraries', 'created_at ASC')
        marketing = _by_session('marketing_contents', 'created_at ASC')
        feedback = [dict(r) for r in conn.execute(
            "SELECT * FROM message_feedback WHERE user_id = ? ORDER BY created_at ASC",
            (user_id,)
        ).fetchall()]
        store_rows = conn.execute(
            "SELECT id, category, data_json, created_at FROM user_store WHERE user_id = ? "
            "ORDER BY created_at DESC", (user_id,)
        ).fetchall()
        user_store = {}
        for r in store_rows:
            try:
                item = json.loads(r['data_json'])
            except (TypeError, ValueError):
                # 单行坏数据不该整份导出失败，但也不能装作它不存在
                item = {"_raw": str(r['data_json']), "_parse_error": "data_json 不是合法 JSON"}
            item['id'] = r['id']
            item['created_at'] = r['created_at']
            user_store.setdefault(r['category'], []).append(item)
        # embedding 是给人工智能检索用的向量，导出里没有可读价值且体积大，不带走
        memories = [dict(r) for r in conn.execute(
            "SELECT memory_id, kind, slot, content, source_session, confidence, "
            "hit_count, created_at, last_used_at, updated_at "
            "FROM memories WHERE user_id = ? ORDER BY created_at ASC", (user_id,)
        ).fetchall()]
    finally:
        conn.close()

    counts = {
        'sessions': len(sessions), 'messages': len(messages),
        'itineraries': len(itineraries), 'marketing_contents': len(marketing),
        'feedback': len(feedback),
        'user_store': sum(len(v) for v in user_store.values()),
        'memories': len(memories),
    }
    return {
        'app': '智旅灵犀 SmartTourism',
        'exported_at': datetime.now().isoformat(timespec='seconds'),
        'user_id': user_id,
        'counts': counts,
        'data': {
            'sessions': sessions,
            'messages': messages,
            'itineraries': itineraries,
            'marketing_contents': marketing,
            'feedback': feedback,
            'user_store': user_store,
            'memories': memories,
        },
        'notice': ('本文件只包含当前账号在本机数据库里的记录。营销内容留档与我保存的攻略存在'
                   '浏览器本地（localStorage），换浏览器或清缓存后不在这里，导出时由前端另行合并。'),
    }


# ==================== 会话状态（记忆持久化打底） ====================

@guarded()
def save_session_state(session_id: str, facts_json: str = None, folded_rounds: int = None):
    """写入/更新会话状态（None 字段保持原值）。session_id 不存在时自动补一行，避免外键与孤儿会话问题。"""
    conn = get_connection()
    conn.execute("INSERT OR IGNORE INTO session_state(session_id, facts_json, folded_rounds) VALUES(?,?,0)",
                 (session_id, facts_json))
    if facts_json is not None:
        conn.execute("UPDATE session_state SET facts_json=?, updated_at=CURRENT_TIMESTAMP WHERE session_id=?",
                     (facts_json, session_id))
    if folded_rounds is not None:
        conn.execute("UPDATE session_state SET folded_rounds=?, updated_at=CURRENT_TIMESTAMP WHERE session_id=?",
                     (folded_rounds, session_id))
    conn.commit()
    conn.close()


@guarded()
def get_session_state(session_id: str) -> Optional[dict]:
    """读取会话状态；不存在返回 None"""
    conn = get_connection()
    row = conn.execute("SELECT * FROM session_state WHERE session_id = ?", (session_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ==================== 长期记忆 ====================

def insert_memory(memory: dict) -> dict:
    """新增一条长期记忆。memory 需含 memory_id/user_id/kind/content，其余取默认值。"""
    conn = get_connection()
    conn.execute("""
        INSERT INTO memories (memory_id, user_id, kind, slot, content, source_session,
                              confidence, hit_count, embedding)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        memory['memory_id'], memory['user_id'], memory.get('kind', 'preference'),
        memory.get('slot'), memory['content'], memory.get('source_session'),
        memory.get('confidence', 0.6), memory.get('hit_count', 0), memory.get('embedding'),
    ))
    conn.commit()
    conn.close()
    return memory


def update_memory(memory_id: str, content: str = None, embedding: str = None,
                  confidence: float = None, kind: str = None, slot: str = None):
    """更新既有记忆（强化/改写），并刷新 updated_at 使时间衰减重新计时"""
    conn = get_connection()
    sets, args = [], []
    for col, val in (('content', content), ('embedding', embedding),
                     ('confidence', confidence), ('kind', kind), ('slot', slot)):
        if val is not None:
            sets.append(f"{col} = ?")
            args.append(val)
    if sets:
        sets.append("updated_at = CURRENT_TIMESTAMP")
        conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE memory_id = ?", (*args, memory_id))
    conn.commit()
    conn.close()


@guarded()
def bump_memory_hit(memory_id: str, score: float = None):
    """记忆被召回一次：命中数 +1，可选同步提升置信度（上限 1.0）"""
    conn = get_connection()
    if score is None:
        conn.execute("UPDATE memories SET hit_count = hit_count + 1, last_used_at = CURRENT_TIMESTAMP"
                     " WHERE memory_id = ?", (memory_id,))
    else:
        conn.execute("UPDATE memories SET hit_count = hit_count + 1, last_used_at = CURRENT_TIMESTAMP,"
                     " confidence = MIN(1.0, MAX(confidence, ?)) WHERE memory_id = ?",
                     (score, memory_id))
    conn.commit()
    conn.close()


def list_memories(user_id: str = "default", kind: str = None, slot: str = None,
                  limit: int = 200) -> list:
    conn = get_connection()
    sql = "SELECT * FROM memories WHERE user_id = ?"
    args = [user_id]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if slot:
        sql += " AND slot = ?"
        args.append(slot)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, tuple(args)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_memory_by_id(memory_id: str) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM memories WHERE memory_id = ?", (memory_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_memory(memory_id: str = None, user_id: str = None) -> int:
    """按 id 删除单条；或按 user_id 清空（两者至少给一个，防止误删全库）"""
    conn = get_connection()
    if memory_id:
        cur = conn.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,))
    elif user_id:
        cur = conn.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
    else:
        conn.close()
        return 0
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


@guarded(default=0)
def prune_memories(user_id: str, keep: int = 200) -> int:
    """按 置信度×新旧 综合分保留最近 keep 条，其余删除（防止长期记忆无限膨胀）"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT memory_id FROM memories WHERE user_id = ?
        ORDER BY confidence DESC, updated_at DESC LIMIT -1 OFFSET ?
    """, (user_id, keep)).fetchall()
    ids = [r['memory_id'] for r in rows]
    if ids:
        conn.executemany("DELETE FROM memories WHERE memory_id = ?", [(i,) for i in ids])
        conn.commit()
    conn.close()
    return len(ids)


@guarded(default=0)
def memory_count(user_id: str) -> int:
    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) AS c FROM memories WHERE user_id = ?", (user_id,)).fetchone()['c']
    conn.close()
    return n


# ==================== 生图指纹复用（省的是真金白银） ====================

def _ark_hit(row) -> dict:
    return {"img_url": row['img_url'], "scene": row['scene'],
            "size": row['size'], "model": row['model'],
            "reuse_count": int(row['reuse_count'] or 0)}


@guarded(default=None, label='lookup_ark_image')
def lookup_ark_image(fingerprint: str) -> Optional[dict]:
    """按指纹查已经生成过的图，命中就把 reuse_count +1。

    加 @guarded() 是有意的：这张表是派生缓存，不是用户数据。它读不出来时正确做法是
    「当没命中、正常去生成一张」，而不是把用户的出图请求整个失败掉。
    最坏结果只是多花一次生图钱，不是功能坏掉。
    """
    conn = get_connection()
    row = conn.execute("SELECT * FROM ark_image_cache WHERE fingerprint = ?",
                       (fingerprint,)).fetchone()
    if not row:
        conn.close()
        return None
    conn.execute("UPDATE ark_image_cache SET reuse_count = reuse_count + 1 "
                 "WHERE fingerprint = ?", (fingerprint,))
    conn.commit()
    out = _ark_hit(row)
    conn.close()
    return out


@guarded(default=False, label='remember_ark_image')
def remember_ark_image(fingerprint: str, img_url: str, scene: str = "",
                       size: str = "", model: str = "") -> bool:
    """把出好并校验通过的图片地址按指纹记下来（reuse_count 归零重新起算）。

    同样是派生写入：写不到就这次不缓存，下一次重新生成，绝不影响本次返回给用户的图。
    """
    conn = get_connection()
    conn.execute("""
        INSERT INTO ark_image_cache (fingerprint, img_url, scene, size, model, reuse_count)
        VALUES (?,?,?,?,?,0)
        ON CONFLICT(fingerprint) DO UPDATE SET
            img_url=excluded.img_url, scene=excluded.scene,
            size=excluded.size, model=excluded.model, reuse_count=0
    """, (fingerprint, img_url, scene or '', size or '', model or ''))
    conn.commit()
    conn.close()
    return True


@guarded(default=dict, label='ark_image_cache_stats')
def ark_image_cache_stats() -> dict:
    """缓存全局统计：可复用指纹数、累计复用次数（跨进程持久，重启不清零）。"""
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS entries, COALESCE(SUM(reuse_count), 0) AS reuses "
        "FROM ark_image_cache").fetchone()
    conn.close()
    return {"entries": int(row['entries'] or 0), "reuses": int(row['reuses'] or 0)}


if __name__ == '__main__':
    init_db()
    print("数据库表创建完成。")
