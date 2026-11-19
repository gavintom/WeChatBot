"""手机插件活跃状态监控（1.1.1-beta1 新增）。

WeChatBotTweak 的 /api/health 只回 runtime.listening / keepAlive 等开关，
**不直接上报「上次活跃时间」或「后台连续运行时长」**。本模块按服务端可观测
事实推算，并把状态落到 analytics.db，供主服务与 Web 管理端两个进程共享：

  * 上次活跃时间（last_active）：最近一次探活成功或收到该手机消息的时间戳。
  * 连续在线（session_start）：一次「离线 → 在线」翻转时记起点；在线期间
    continuous_seconds = now - session_start；探活失败即判离线并归零。
  * 后台连续运行时间即 continuous_seconds（对外展示为 xxx 分 / xxx 小时）。
  * 告警地址（alert_url）由管理端写入后缓存在这里，供状态页即时回显。

并发：管理端与主服务会同时写，用 SQLite BEGIN IMMEDIATE 事务保证读改写原子，
WAL 模式保证写不影响读。所有操作都吞异常（监控失败绝不影响主链路）。
"""
import os
import threading
import time
from datetime import datetime

import db_util

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("WXBOT_DB") or os.path.join(BASE_DIR, "data", "analytics.db")

_SCHEMA = "CREATE TABLE IF NOT EXISTS phone_monitor (k TEXT PRIMARY KEY, v TEXT)"
_LOCK = threading.RLock()
# 探活失败多久后才判定离线（秒）。短抖动容错，避免一条消息之间误清零连续时长。
OFFLINE_GRACE = 90


def _conn():
    return db_util.connect(DB_PATH, timeout=10)


def _get(conn, k, default=""):
    row = conn.execute("SELECT v FROM phone_monitor WHERE k=?", (k,)).fetchone()
    return row[0] if row and row[0] is not None else default


def _set(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO phone_monitor(k,v) VALUES(?,?)",
                 (k, str(v)))


def _fmt(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "—"


def touch(ts=None, source=""):
    """标记一次「手机活跃」。探活成功 / 收到消息都调用。

    若此前判离线，则开启新会话（重置 session_start）。source 仅用于排查
    （health / webhook / status）。
    """
    ts = float(ts or time.time())
    try:
        with _LOCK:
            conn = _conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(_SCHEMA)
                online = _get(conn, "online") == "1"
                if not online:
                    _set(conn, "session_start", ts)
                _set(conn, "last_active", ts)
                _set(conn, "online", "1")
                _set(conn, "last_source", source or "-")
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
            finally:
                conn.close()
    except Exception:
        pass


def mark_offline(ts=None):
    """标记一次「探活失败」。仅在已在线时翻转，避免反复写。"""
    ts = float(ts or time.time())
    try:
        with _LOCK:
            conn = _conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(_SCHEMA)
                if _get(conn, "online") != "1":
                    conn.commit()
                    return
                start = _get(conn, "session_start", "")
                if start:
                    _set(conn, "last_session_seconds", max(0, ts - float(start)))
                _set(conn, "online", "0")
                _set(conn, "offline_since", ts)
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
            finally:
                conn.close()
    except Exception:
        pass


def set_alert_info(url="", enabled=None):
    """管理端写入告警地址后调用，缓存回显用。"""
    try:
        with _LOCK:
            conn = _conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(_SCHEMA)
                _set(conn, "alert_url", str(url or ""))
                if enabled is not None:
                    _set(conn, "network_alert_enabled", "1" if enabled else "0")
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
            finally:
                conn.close()
    except Exception:
        pass


def state():
    """返回监控页所需状态字典（时间戳 + 人类可读文本 + 连续秒数）。"""
    out = {
        "online": False, "last_active": 0, "last_active_str": "—",
        "session_start": 0, "session_start_str": "—", "continuous_seconds": 0,
        "last_source": "", "alert_url": "", "network_alert_enabled": None,
    }
    try:
        with _LOCK:
            conn = _conn()
            try:
                conn.execute(_SCHEMA)
                online = _get(conn, "online") == "1"
                last = _get(conn, "last_active", "")
                start = _get(conn, "session_start", "")
                out["last_source"] = _get(conn, "last_source", "")
                out["alert_url"] = _get(conn, "alert_url", "")
                nae = _get(conn, "network_alert_enabled", "")
                out["network_alert_enabled"] = (nae == "1") if nae != "" else None
            finally:
                conn.close()
    except Exception:
        return out
    try:
        last_f = float(last) if last else 0
    except (TypeError, ValueError):
        last_f = 0
    try:
        start_f = float(start) if start else 0
    except (TypeError, ValueError):
        start_f = 0
    now = time.time()
    # 长时间没有任何信号（既没探活也没消息）时，即便 online=1 也按超时离线显示，
    # 避免进程重启后残留的 online 标记把连续时长算到天上去。
    if online and last_f and (now - last_f) > OFFLINE_GRACE:
        online = False
    out["online"] = online
    out["last_active"] = last_f
    out["last_active_str"] = _fmt(last_f) if last_f else "—"
    out["session_start"] = start_f
    out["session_start_str"] = _fmt(start_f) if start_f else "—"
    out["continuous_seconds"] = int(now - start_f) if (online and start_f) else 0
    return out
