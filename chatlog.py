"""独立的聊天记录库：归档插件 webhook 推送的原始聊天消息。

与 `analytics.db` 刻意分开：
  - `analytics.db` 存业务流水（events / llm_usage / api_calls）与配置表，需要长期保留；
  - `chatlog.db` 只存消息原文，按月保留，可整体备份 / 删除而不影响业务统计。

配置（config.yaml 的 `chat_log` 段，**运行时读取**，改完最多 30 秒生效）：
    chat_log:
      enabled: true
      retention_months: 3        # 1~12；按月折算 30 天，超出范围自动钳制
      db_path: "./data/chatlog.db"  # 相对项目根；改路径需重启（环境变量 WXBOT_CHAT_DB 优先）
      purge_interval_hours: 6    # 定时清理间隔（小时）

模块加载时确定 DB 路径，此后不再切换 —— 运行中改 db_path 会把旧库「凭空清空」。
"""
import os
import threading
import time

import yaml

import db_util

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "chatlog.db")


def _config_path():
    p = os.environ.get("WXBOT_CONFIG")
    if p and os.path.isfile(p):
        return p
    return os.path.join(BASE_DIR, "data", "config.yaml")


def _initial_db_path():
    env = os.environ.get("WXBOT_CHAT_DB")
    if env:
        return os.path.abspath(env)
    try:
        with open(_config_path(), encoding="utf-8") as f:
            c = yaml.safe_load(f) or {}
        p = str(((c.get("chat_log") or {}).get("db_path") or "")).strip()
        if p:
            return os.path.abspath(p if os.path.isabs(p) else os.path.join(BASE_DIR, p))
    except Exception:
        pass
    return DEFAULT_DB


DB_PATH = _initial_db_path()
_write_lock = threading.Lock()
_cfg_cache = {"ts": 0.0, "data": None}
_ensure_lock = threading.Lock()
_ensured = [False]

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_key      TEXT NOT NULL UNIQUE,   -- policy.dedup_key，重复推送只留一条
    ts           REAL NOT NULL,
    day          TEXT NOT NULL,          -- YYYY-MM-DD，日期筛选与展示共用
    time_str     TEXT,
    chat         TEXT NOT NULL,
    chat_name    TEXT,
    chat_type    TEXT,                  -- group / private
    is_group     INTEGER NOT NULL DEFAULT 0,
    sender       TEXT,
    sender_name  TEXT,
    direction    TEXT,                  -- in / out
    type         INTEGER,
    type_name    TEXT,
    text         TEXT,                  -- 清洗后正文
    content      TEXT,                  -- 原始内容（可能是 XML）
    app_type     TEXT,
    at_me        INTEGER NOT NULL DEFAULT 0,
    at_all       INTEGER NOT NULL DEFAULT 0,
    local_id     INTEGER,
    media_api    TEXT
);
CREATE INDEX IF NOT EXISTS idx_chatmsg_day ON chat_messages(day);
CREATE INDEX IF NOT EXISTS idx_chatmsg_ts ON chat_messages(ts);
CREATE INDEX IF NOT EXISTS idx_chatmsg_chat ON chat_messages(chat);
CREATE INDEX IF NOT EXISTS idx_chatmsg_sender ON chat_messages(sender);
"""


def config():
    """运行时读 chat_log 配置（30 秒缓存）；保留月数强制钳制到 1~12。"""
    now = time.time()
    if _cfg_cache["data"] is not None and now - _cfg_cache["ts"] < 30:
        return dict(_cfg_cache["data"])
    d = {}
    try:
        with open(_config_path(), encoding="utf-8") as f:
            c = yaml.safe_load(f) or {}
        d = c.get("chat_log") or {}
    except Exception:
        pass

    def _int(v, default, lo, hi):
        try:
            n = int(v if v not in (None, "") else default)
        except (TypeError, ValueError):
            n = default
        return max(lo, min(hi, n))

    out = {
        "enabled": bool(d.get("enabled", True)),
        "retention_months": _int(d.get("retention_months"), 3, 1, 12),
        "purge_interval_hours": _int(d.get("purge_interval_hours"), 6, 1, 168),
        "db_path": DB_PATH,
        "db_file": os.path.basename(DB_PATH),
    }
    _cfg_cache.update(ts=now, data=out)
    return dict(out)


def _connect():
    return db_util.connect(DB_PATH)


def _rows(sql, args=()):
    _ensure()
    conn = _connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _one(sql, args=()):
    r = _rows(sql, args)
    return r[0] if r else None


def init_db():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with _write_lock:
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()


def _ensure():
    """首次访问时惰性建表：主服务和后台都不用记得显式调 init_db()。

    用独立的锁，避免和 init_db() 内部的 _write_lock 递归死锁。
    """
    if _ensured[0]:
        return
    with _ensure_lock:
        if _ensured[0]:
            return
        init_db()
        _ensured[0] = True


def _like_escape(s):
    return str(s or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def save_message(m, msg_key=""):
    """归档一条 normalize 后的 webhook 消息。返回 (是否写入, 原因)。

    **绝不能抛异常**（归档失败不能拖垮 webhook 200 响应）。
    排除规则由调用方先做 `policy.is_noise_session`，这里再兜一层 chat_type，
    防止别处误传。
    """
    try:
        _ensure()
        c = config()
        if not c["enabled"]:
            return False, "disabled"
        chat = str(m.get("chat") or "").strip()
        if not chat:
            return False, "no-chat"
        # 与 policy.NOISE_CHAT_TYPES 保持一致（不引入模块级依赖，避免导入环）
        if str(m.get("chat_type") or "").lower() in (
                "official", "service", "brand", "system", "notification"):
            return False, "noise"
        ts = float(m.get("ts") or 0) or time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        tstr = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        key = str(msg_key or "") or "|".join([
            chat, str(m.get("local_id") or 0), str(int(ts)),
            str(m.get("sender") or ""), str(m.get("type") or 0),
            str(m.get("direction") or "")])
        vals = (key, ts, day, tstr, chat,
                str(m.get("chat_name") or ""), str(m.get("chat_type") or ""),
                1 if m.get("is_group") else 0,
                str(m.get("sender") or ""), str(m.get("sender_name") or ""),
                str(m.get("direction") or "in"), int(m.get("type") or 0),
                str(m.get("type_name") or ""), str(m.get("text") or ""),
                str(m.get("content") or ""), str(m.get("app_type") or ""),
                1 if m.get("at_me") else 0, 1 if m.get("at_all") else 0,
                int(m.get("local_id") or 0), str(m.get("media_api") or ""))
        with _write_lock:
            conn = _connect()
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO chat_messages("
                    "msg_key,ts,day,time_str,chat,chat_name,chat_type,is_group,"
                    "sender,sender_name,direction,type,type_name,text,content,"
                    "app_type,at_me,at_all,local_id,media_api) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)
                conn.commit()
                return (cur.rowcount > 0), ("ok" if cur.rowcount > 0 else "duplicate")
            finally:
                conn.close()
    except Exception as e:
        return False, str(e)[:150]


def _where(start=None, end=None, chat_type=None, who=None, keyword=None,
           msg_type=None, direction=None):
    w, a = [], []
    if start:
        w.append("day>=?"); a.append(str(start)[:10])
    if end:
        w.append("day<=?"); a.append(str(end)[:10])
    if chat_type in ("group", "private"):
        w.append("chat_type=?"); a.append(chat_type)
    if who and str(who).strip():
        pat = "%" + _like_escape(who) + "%"
        w.append("(IFNULL(chat,'') LIKE ? ESCAPE '\\' OR "
                 "IFNULL(chat_name,'') LIKE ? ESCAPE '\\' OR "
                 "IFNULL(sender,'') LIKE ? ESCAPE '\\' OR "
                 "IFNULL(sender_name,'') LIKE ? ESCAPE '\\')")
        a += [pat] * 4
    if keyword and str(keyword).strip():
        pat = "%" + _like_escape(keyword) + "%"
        w.append("(IFNULL(text,'') LIKE ? ESCAPE '\\' OR "
                 "IFNULL(content,'') LIKE ? ESCAPE '\\')")
        a += [pat] * 2
    if msg_type and str(msg_type).strip():
        if str(msg_type) == "other":
            w.append("type NOT IN (1,3,34,43,49)")
        else:
            try:
                w.append("type=?"); a.append(int(msg_type))
            except (TypeError, ValueError):
                pass
    if direction in ("in", "out"):
        w.append("direction=?"); a.append(direction)
    return (("WHERE " + " AND ".join(w)) if w else ""), a


def query(start=None, end=None, chat_type=None, who=None, keyword=None,
          msg_type=None, direction=None, limit=30, offset=0):
    clause, args = _where(start, end, chat_type, who, keyword, msg_type, direction)
    total = (_one("SELECT COUNT(*) AS n FROM chat_messages " + clause, args)
             or {}).get("n") or 0
    rows = _rows("SELECT * FROM chat_messages " + clause +
                 " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                 args + [int(limit), int(offset)])
    return {"total": total, "items": rows}


def all_for_export(start=None, end=None, chat_type=None, who=None, keyword=None,
                   msg_type=None, direction=None, cap=100000):
    clause, args = _where(start, end, chat_type, who, keyword, msg_type, direction)
    return _rows("SELECT * FROM chat_messages " + clause +
                 " ORDER BY ts ASC, id ASC LIMIT ?", args + [int(cap)])


def stats():
    c = config()
    r = _one("SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
             "FROM chat_messages") or {}
    cut = time.time() - c["retention_months"] * 30 * 86400
    try:
        size = os.path.getsize(DB_PATH)
    except OSError:
        size = 0
    def _fmt(ts):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"
    return {
        "total": r.get("n") or 0,
        "earliest": _fmt(r.get("first_ts")), "latest": _fmt(r.get("last_ts")),
        "enabled": c["enabled"], "retention_months": c["retention_months"],
        "purge_interval_hours": c["purge_interval_hours"],
        "db_file": c["db_file"], "db_path": c["db_path"], "db_bytes": size,
        "cutoff_before": _fmt(cut),
    }


def purge_expired(months=None):
    """删除超过保留期的消息。返回删除条数；按月折算 30 天。"""
    _ensure()
    c = config()
    try:
        m = int(months if months not in (None, "") else c["retention_months"])
    except (TypeError, ValueError):
        m = c["retention_months"]
    m = max(1, min(12, m))
    cutoff = time.time() - m * 30 * 86400
    with _write_lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM chat_messages WHERE ts < ?", (cutoff,))
            conn.commit()
            return max(0, cur.rowcount)
        finally:
            conn.close()
