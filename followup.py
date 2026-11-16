"""群聊「对话延续窗口」（follow-up window）。

场景：群里 A @ 机器人或命中唤醒词、机器人回复之后，接下来 3 分钟（可调，
1~30 分钟）内 A 再说话不必重新 @ / 唤醒，直接视为同一轮对话的继续；
超时必须重新 @ / 唤醒。群聊 AI 实质回复末尾会空一行附带一条可配置的续聊提示，
告诉群友这段时间内不用再 @。

设计取舍：
  * 计时起点是**回复成功发出的时刻**，不是收到提问的时刻。LLM 可能要跑几秒
    甚至几十秒，从收消息开始计时的话，等机器人答完窗口已经被烧掉了。
  * 每次成功回复都会刷新（滚动窗口），「一问一答」可以自然连成多轮。
  * 作用范围**固定为同一群 + 同一群友**（按 wxid），不提供全群扩散选项：
    群里别人插话绝不会被机器人抢答。配置里的 `scope` 一律忽略。
  * 窗口状态只在内存里（进程重启即失效）。分钟级的短状态没必要落库，也避免
    多副本读到过期窗口；主服务已要求单 worker（server.threads 提供并发）。
  * 配置唯一来源是 `config.yaml` 的 `reply.followup` 段：后台「保存并生效」会
    文本级写回该文件（保留注释），手工改文件也一样生效。每次判定都会检查文件
    mtime，变了就重读 —— 管理端与主服务是两个进程，靠文件变化即时同步，
    不需要跨进程通知、不需要重启。

配置：`config.yaml` 的 `reply.followup` 段 > 内置默认
（开启 / 3 分钟 / 仅同一群友 / 显示续聊提示）。
时长**一律用分钟**：文件里是 `window_minutes`（1~30），后台输入框、接口、
提示文案里的 `{minutes}` 都是分钟；只有和 `time.time()` 比较时才换算成秒，
这个换算值是内部计算，不写回文件、也不作为配置项暴露。
"""
import os
import threading
import time

import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SCOPE = "sender"                   # 唯一允许的作用范围：同一群 + 同一群友（不可配置）
DEFAULT_HINT = "（{minutes}分钟内您无需 @/ 唤醒续聊）"
DEFAULTS = {
    "enabled": True,
    "window_minutes": 3.0,         # 默认 3 分钟
    "hint_enabled": True,          # 是否在群聊 AI 回复末尾空一行追加提示
    "hint": DEFAULT_HINT,          # 模板：{minutes} 换成当前窗口分钟数
}
MIN_MINUTES = 1.0                  # 可调范围 1~30 分钟
MAX_MINUTES = 30.0
MAX_HINT_LEN = 200                 # 提示文案长度上限，防止误填一大段刷屏
SECONDS_PER_MINUTE = 60.0
FILE_TTL = 30.0                    # mtime 拿不到时的兜底缓存（秒）
PRUNE_AFTER = 3600.0               # 内存里过期窗口的清理阈值（秒）

# data / ts / sig：sig 是 (mtime_ns, size)，文件被后台写回后立刻失效。
_file_cache = {"ts": 0.0, "data": None, "sig": None}
_state_lock = threading.Lock()
_windows = {}                      # (chat, sender) -> 上次成功回复的时刻


# ---------------------------------------------------------------- 配置


def _config_path():
    p = os.environ.get("WXBOT_CONFIG")
    if p and os.path.isfile(p):
        return p
    p = os.path.join(BASE_DIR, "data", "config.yaml")
    if os.path.isfile(p):
        return p
    return os.path.join(BASE_DIR, "data", "config.yaml.example")


def _as_bool(v, default=True):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v if v is not None else "").strip().lower()
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n"):
        return False
    return default


def _as_number(v, default, low, high):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    if n != n or n in (float("inf"), float("-inf")):
        return default
    return max(float(low), min(float(high), n))


def _as_minutes(v, default=3.0):
    return _as_number(v, default, MIN_MINUTES, MAX_MINUTES)


def _tidy(n):
    """整数写成 int，写回 config.yaml 时不会出现 `3.0` 这种噪点。"""
    n = float(n)
    return int(n) if n.is_integer() else n


def _clean(d, base):
    """把任意来源的配置片段规整成合法值；只覆盖片段里出现的键。

    时长只认 `window_minutes`；旧版的 `window_seconds` 和 `scope` 都不读。
    """
    out = dict(base)
    if not isinstance(d, dict):
        return out
    if "enabled" in d:
        out["enabled"] = _as_bool(d["enabled"], out["enabled"])
    if "window_minutes" in d:
        # 非法值保持原窗口，不悄悄跳回默认 3 分钟
        out["window_minutes"] = _tidy(
            _as_minutes(d["window_minutes"], out["window_minutes"]))
    if "hint_enabled" in d:
        out["hint_enabled"] = _as_bool(d["hint_enabled"], out["hint_enabled"])
    if "hint" in d:
        h = d["hint"]
        out["hint"] = ("" if h is None else str(h))[:MAX_HINT_LEN]
    return out


def clear_cache():
    """让下一次 config() 立刻重读 config.yaml（测试 / 后台保存时用）。"""
    _file_cache.update(ts=0.0, data=None, sig=None)


def _file_config():
    """读文件配置；**文件 mtime 一变就重读**，管理端保存后主服务无需通知即生效。"""
    now = time.time()
    path = _config_path()
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    cached = _file_cache
    if (cached["data"] is not None and sig is not None
            and sig == cached["sig"] and now - cached["ts"] < FILE_TTL):
        return dict(cached["data"])
    out = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            c = yaml.safe_load(f) or {}
        seg = (c.get("reply") or {}).get("followup") or {}
        out = _clean(seg, out)
    except Exception:
        pass                       # 配置文件缺失/写坏时按默认走，绝不让消息处理崩掉
    cached.update(ts=now, data=out, sig=sig)
    return dict(out)


def normalize(d):
    """把后台传来的值规整成合法配置（钳制分钟数、截断文案、规整布尔）。"""
    return _clean(d, DEFAULTS)


def reload():
    """后台写回 config.yaml 之后调用：清缓存，返回最新生效配置。"""
    clear_cache()
    return config()


def _seconds(c):
    """窗口长度换算成秒，仅用于和 time.time() 比较；不是配置项。"""
    return float(c.get("window_minutes") or DEFAULTS["window_minutes"]) * SECONDS_PER_MINUTE


def hint_text(c=None):
    """群聊回复末尾要追加的续聊提示（不含前导换行）；不该显示时返回空串。

    模板里的 `{minutes}` 换成当前窗口分钟数（整数不带小数点）；模板不含占位符时
    原样输出，方便写死一句固定文案。用 replace 而不是 format，用户模板里出现
    其它花括号（如 `{other}`）也不会抛异常。
    """
    if c is None:
        c = config()
    if not c.get("enabled") or not c.get("hint_enabled"):
        return ""
    t = str(c.get("hint") or "")
    if not t.strip():
        return ""
    mins = "%g" % float(c.get("window_minutes") or DEFAULTS["window_minutes"])
    return t.replace("{minutes}", mins)


def config():
    """当前生效配置：enabled / window_minutes / scope / hint_* 等。

    scope 恒为 "sender"，仅用于后台状态展示，不接受任何配置覆盖。
    hint_preview 是渲染后的提示文案，供后台直接展示预览。
    """
    out = dict(_file_config())
    out["scope"] = SCOPE
    out["hint_preview"] = hint_text(out)
    out["active_windows"] = stats(out)
    return out


# ---------------------------------------------------------------- 窗口状态


def _is_group(m):
    return bool(m.get("chat_type") == "group" or m.get("is_group"))


def _key(m):
    """窗口键固定为 (群 ID, 群友 wxid)；缺任一标识都不能开放窗口。"""
    chat = str(m.get("chat") or "").strip()
    if not chat:
        return None
    sender = str(m.get("sender") or "").strip()
    if not sender:
        return None                     # 缺 wxid 时不能把「未知发送者」混成一个窗口
    return (chat, sender)


def _prune(now, window):
    """清掉明显过期的窗口，避免字典随群数无限增长。

    判定是否命中仍严格按当前窗口（分钟换算出的秒数），窗口调小不会让旧记录「复活」。
    """
    limit = max(PRUNE_AFTER, float(window) * 2)
    for k, ts in list(_windows.items()):
        if now - ts > limit:
            _windows.pop(k, None)


def stats(c):
    """当前仍在窗口内的会话数（后台状态展示用）。"""
    now = time.time()
    w = _seconds(c)
    with _state_lock:
        return sum(1 for ts in _windows.values() if 0 <= now - ts <= w)


def mark_reply(m, now=None):
    """群聊回复**成功发出后**调用：记下时刻，滚动刷新该群友的对话窗口。"""
    c = config()
    if not c["enabled"] or not _is_group(m):
        return False
    key = _key(m)
    if key is None:
        return False
    t = time.time() if now is None else float(now)
    with _state_lock:
        _prune(t, _seconds(c))
        _windows[key] = t
    return True


def check(m, now=None, self_wxid=""):
    """这条群聊消息能否当作「上一轮对话的继续」。

    返回 dict：ok / reason / age / window_minutes / scope / enabled。
    reason 会拼进 skip 原因，方便 DEBUG 日志排查「为什么没接话」。
    """
    c = config()
    w = _seconds(c)
    mins = float(c["window_minutes"])
    out = {"ok": False, "reason": "", "age": None,
           "window_minutes": mins, "scope": SCOPE, "enabled": c["enabled"]}
    if not c["enabled"]:
        out["reason"] = "对话延续功能已关闭"
        return out
    if not _is_group(m):
        out["reason"] = "不是群聊消息"
        return out
    if self_wxid and str(m.get("sender") or "").strip() == self_wxid:
        # 兜底：插件偶尔会把自己发的消息回流，别让它触发「自己接自己」
        out["reason"] = "本机自己发出的消息"
        return out
    key = _key(m)
    if key is None:
        out["reason"] = "缺少群 ID 或发送者标识"
        return out
    t = time.time() if now is None else float(now)
    with _state_lock:
        ts = _windows.get(key)
    if ts is None:
        out["reason"] = "该群（群友）此前没有机器人回复"
        return out
    age = max(0.0, t - ts)             # 时钟回拨时按 0 处理
    out["age"] = age
    if age > w:
        out["reason"] = "距上次回复 %.1f 秒，已超过 %g 分钟窗口" % (age, mins)
        return out
    out["ok"] = True
    out["reason"] = "距上次回复 %.1f 秒（窗口 %g 分钟）" % (age, mins)
    return out


def clear():
    """清空窗口状态与配置缓存（测试 / 排障用）。"""
    with _state_lock:
        _windows.clear()
    clear_cache()
