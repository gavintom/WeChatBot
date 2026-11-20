"""管理端共用层：应用常量、响应助手、登录校验、主服务转发与分页工具。

依赖方向是单向的：`app.py` → `bp_*.py` → `web.py`。蓝图不反向 import app，
避免循环依赖，也让每个蓝图模块能被单独导入测试。
"""
import csv
import functools
import io
import logging
import os
import secrets
import sys
from urllib.parse import quote

import requests
from flask import current_app, jsonify, redirect, request, session, url_for

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import logfiles
import textutil
from admin import cfg

SECRET_FILE = os.path.join(BASE_DIR, ".admin_secret")
SESSION_COOKIE = "wxbot_admin_session"
# 应用版本号：改这里即可，侧栏徽标 / 页面标题 / /api/sysinfo 同步生效
APP_VERSION = "V1.1.2-beta7"
# 静态资源缓存串：所有模板统一用 {{ asset_ver }} 拼在 ?v= 后面（index/login/error）。
# 以前各模板各写各的（index 用 betaN、login/error 还停在 20261119），改样式只改一处会漏，
# 浏览器就会拿旧 CSS 配新 HTML。以后发版只改这一行。
ASSET_VER = "20261121beta7"


# ---------------------------------------------------------------- 日志
def main_log_path():
    """主服务运行日志；按天文件名，每次调用都重新解析（跨天不会读到昨天的文件）。"""
    return logfiles.resolve("WXBOT_LOG", "webhook")


def admin_log_path():
    return logfiles.resolve("WXBOT_ADMIN_LOG", "admin")


_admin_logger = logging.getLogger("wxbot-admin")
if not _admin_logger.handlers:
    _admin_logger.setLevel(logging.INFO)
    try:
        _fh = logfiles.daily_handler(
            "admin", "WXBOT_ADMIN_LOG",
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                              "%Y-%m-%d %H:%M:%S"))
        _admin_logger.addHandler(_fh)
    except OSError:
        pass


def adminlog(msg, *args):
    """写管理端日志；日志失败绝不影响接口。"""
    try:
        _admin_logger.info(msg, *args)
    except Exception:
        pass


# ---------------------------------------------------------------- 会话密钥 / 鉴权
def secret_key():
    """读取（或首次生成）会话签名密钥，落盘权限 0600。"""
    try:
        with open(SECRET_FILE, encoding="utf-8") as f:
            s = f.read().strip()
        if s:
            return s
    except OSError:
        pass
    s = secrets.token_urlsafe(48)
    try:
        with open(SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(s)
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass
    return s


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("uid"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "未登录",
                                "code": "unauthorized"}), 401
            return redirect(url_for("login_page", next=request.path))
        return fn(*a, **kw)
    return wrapper


# ---------------------------------------------------------------- 响应助手
def ok(**kw):
    d = {"ok": True}
    d.update(kw)
    return jsonify(d)


def fail(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code


def body():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def csv_response(filename, headers, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for r in rows:
        w.writerow([textutil.safe_cell(x) for x in r])
    data = "\ufeff" + buf.getvalue()
    return current_app.response_class(
        data, mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 "attachment; filename*=UTF-8''%s" % quote(filename)})


# ---------------------------------------------------------------- 主服务转发
# 后台 → 主服务是本机回环调用，绕过环境代理（否则 HTTP_PROXY 会让它超时）
_INTERNAL = requests.Session()
_INTERNAL.trust_env = False


def main_cfg():
    c = cfg.read_config()
    main = (c.get("admin") or {}).get("main_service") or "http://127.0.0.1:29999"
    wh = c.get("webhook") or {}
    token = wh.get("internal_token") or wh.get("token") or ""
    return c, main.rstrip("/"), token


def call_main(path, method="GET", body=None, timeout=5):
    """调用主服务的 /internal/* 接口；异常统一折成 (status, {"ok": False, ...})。"""
    _, base, token = main_cfg()
    headers = {"X-Internal-Token": token} if token else {}
    r = _INTERNAL.request(method, base + path, json=body, headers=headers,
                          timeout=timeout)
    try:
        return r.status_code, r.json()
    except Exception:
        txt = (r.text or "")[:200]
        low = txt.lower()
        if "<html" in low or "doctype" in low:
            # 主服务还是旧版本时，/internal/xxx 会回 Flask 的 404 HTML。
            # 绝不能把整段 HTML 当 error 吐给前端 toast（用户看到的是一屏标签）。
            txt = ("主服务 %s 返回 HTTP %s 的 HTML 页面（通常是旧版本缺少该内部接口，"
                   "请重启主服务）" % (path, r.status_code))
        return r.status_code, {"ok": False, "error": txt}


# ---------------------------------------------------------------- 分页
def page_args():
    """解析 page / limit，返回 (原始 args, page, limit)。"""
    p = request.args
    try:
        page = max(1, int(p.get("page") or 1))
        limit = min(500, max(10, int(p.get("limit") or 30)))
    except (TypeError, ValueError):
        page, limit = 1, 30
    return p, page, limit


def paged(fetch, page, limit):
    """分页查询并把 page **钳制到总页数内**，避免翻到空页。

    fetch(offset) 返回 {"total": n, "items": [...]}。越界时用钳制后的 offset
    重查一次，返回真实最后一页的数据；前端因此能拿到一致的 page / max_page。
    """
    r = fetch((page - 1) * limit)
    total = r.get("total") or 0
    max_page = max(1, (total + limit - 1) // limit)
    if page > max_page:
        page = max_page
        r = fetch((page - 1) * limit)
    return r, page, limit, max_page


def days_arg(p):
    try:
        return min(365, max(1, int(p.get("days") or 30)))
    except (TypeError, ValueError):
        return 30
