"""个人微信智能客服 · Web 管理端

独立 Flask 进程（默认 7780），与主服务共用 analytics.db。
启动：python -m admin.app   /   gunicorn -c admin/gunicorn.conf.py admin.app:app

页面：运行监控 / 日志管理（对话记录 · 聊天记录 · API 记录 · AI 用量 · 运行日志）/
      通讯录管理 / 知识库（知识库管理 · 知识库文档）/
      系统管理（群聊配置 · 私聊配置 · 系统配置 · AI 大模型）/
      一级菜单：插件管理 · 朋友圈管理 · 系统信息
"""
import csv
import functools
import io
import logging
import os
import platform
import re
import secrets
import shutil
import sys
import time
from urllib.parse import quote

import requests
import yaml
from flask import (Flask, abort, jsonify, make_response, redirect,
                   render_template, request, send_file, session, url_for)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import store
import chatlog
import followup
import logfiles
import textutil
from admin import cfg, confedit, contacts, moments, pricing, proc
from admin import kb as kbm

SECRET_FILE = os.path.join(BASE_DIR, ".admin_secret")
SESSION_COOKIE = "wxbot_admin_session"
# 应用版本号：改这里即可，侧栏徽标 / 页面标题 / /api/sysinfo 同步生效
APP_VERSION = "V1.1.0"


def main_log_path():
    """主服务运行日志；按天文件名，每次调用都重新解析（跨天不会读到昨天的文件）。"""
    return logfiles.resolve("WXBOT_LOG", "webhook")


def admin_log_path():
    return logfiles.resolve("WXBOT_ADMIN_LOG", "admin")


# 管理端自己的操作日志（与「运行日志」分开显示）
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

app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, "admin", "templates"),
            static_folder=os.path.join(BASE_DIR, "admin", "static"),
            static_url_path="/static")


def _secret():
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


app.secret_key = _secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("KF_ADMIN_HTTPS") == "1"),
    PERMANENT_SESSION_LIFETIME=8 * 3600,
    MAX_CONTENT_LENGTH=32 * 1024 * 1024,
)
app.config["SESSION_COOKIE_NAME"] = SESSION_COOKIE

# ---------------------------------------------------------------- 登录 / 安全

_fails = {}
_FAIL_WINDOW = 300
_FAIL_MAX = 5


def _client_ip():
    x = request.headers.get("X-Forwarded-For") or ""
    return (x.split(",")[0].strip() if x else request.remote_addr or "?")


def _throttled(ip):
    now = time.time()
    arr = [t for t in _fails.get(ip, []) if now - t < _FAIL_WINDOW]
    _fails[ip] = arr
    return len(arr) >= _FAIL_MAX


def _note_fail(ip):
    _fails.setdefault(ip, []).append(time.time())


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("uid"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "未登录", "code": "unauthorized"}), 401
            return redirect(url_for("login_page", next=request.path))
        return fn(*a, **kw)
    return wrapper


def _host_allowed():
    origin = request.headers.get("Origin") or ""
    if not origin:
        return True
    from urllib.parse import urlparse
    o = urlparse(origin)
    host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(":")[0]
    oh = (o.hostname or "").lower()
    if oh == host.lower():
        return True
    extra = os.environ.get("KF_ADMIN_ALLOWED_ORIGINS") or ""
    return any(oh == x.strip().lower() for x in extra.split(",") if x.strip())


@app.before_request
def _check_csrf():
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return
    if request.path in ("/login", "/logout"):
        return
    if not session.get("uid"):
        abort(403, "未登录")
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        abort(403, "缺少 X-Requested-With 头")
    if not _host_allowed():
        abort(403, "跨源请求被拒绝（反代需设置 X-Forwarded-Host）")


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(500)
def _error(e):
    msg = getattr(e, "description", None) or str(e)
    code = getattr(e, "code", 500) or 500
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": msg, "code": code}), code
    return render_template("error.html", code=code, message=msg), code


def _ok(**kw):
    d = {"ok": True}
    d.update(kw)
    return jsonify(d)


def _err(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code


def _body():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def _csv(filename, headers, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for r in rows:
        w.writerow([textutil.safe_cell(x) for x in r])
    data = "\ufeff" + buf.getvalue()
    return app.response_class(
        data, mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 "attachment; filename*=UTF-8''%s" % quote(filename)})


# 后台 → 主服务是本机回环调用，绕过环境代理（否则 HTTP_PROXY 会让它超时）
_INTERNAL = requests.Session()
_INTERNAL.trust_env = False


def _main_cfg():
    c = cfg.read_config()
    main = (c.get("admin") or {}).get("main_service") or "http://127.0.0.1:29999"
    wh = c.get("webhook") or {}
    token = wh.get("internal_token") or wh.get("token") or ""
    return c, main.rstrip("/"), token


def _call_main(path, method="GET", body=None, timeout=5):
    _, base, token = _main_cfg()
    headers = {"X-Internal-Token": token} if token else {}
    r = _INTERNAL.request(method, base + path, json=body, headers=headers, timeout=timeout)
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


# ---------------------------------------------------------------- 页面

@app.route("/")
def index():
    if not session.get("uid"):
        return redirect(url_for("login_page", next="/"))
    # 首页 HTML 不缓存：改了 DOM 结构后，浏览器若还拿旧版页面会前后端错配
    # （例如旧 div#chatBody 配上新版渲染的 <tr>，标签被丢弃、所有记录连成一行）。
    resp = make_response(render_template(
        "index.html", username=session.get("uid") or "admin",
        app_version=APP_VERSION,
        password_changed_at=store.password_changed_at()))
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        return render_template("login.html", default_user="admin", error="",
                               next=request.args.get("next") or "/")
    ip = _client_ip()
    if _throttled(ip):
        return render_template("login.html", default_user="admin",
                               error="失败次数过多，请 5 分钟后再试",
                               next=request.form.get("next") or "/"), 429
    u = store.verify_user(request.form.get("username"), request.form.get("password"))
    if not u:
        _note_fail(ip)
        return render_template("login.html", default_user="admin",
                               error="账号或密码不正确",
                               next=request.form.get("next") or "/"), 401
    _fails.pop(ip, None)
    adminlog("登录成功 user=%s ip=%s", u["username"], ip)
    session.clear()
    session["uid"] = u["username"]
    session.permanent = True
    nxt = request.form.get("next") or "/"
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")


@app.route("/logout", methods=["POST"])
def logout():
    # 审计：与「登录成功」日志成对，方便排查后台会话
    adminlog("退出登录 user=%s ip=%s", session.get("uid") or "-", _client_ip())
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/api/password", methods=["POST"])
@login_required
def api_password():
    d = _body()
    ok, msg = store.change_password(session.get("uid"), d.get("old"), d.get("new"))
    if not ok:
        return _err(msg)
    session.clear()
    return _ok(message=msg)


# ---------------------------------------------------------------- 运行监控

@app.route("/api/status")
@login_required
def api_status():
    c, main, _ = _main_cfg()
    out = {"main": {"reachable": False}, "plugin": {"reachable": False},
           "summary": store.summary(), "daily": store.daily_series(7)}
    try:
        code, d = _call_main("/healthz", timeout=3)
        out["main"] = {"reachable": code == 200, **(d if isinstance(d, dict) else {})}
    except Exception as e:
        out["main"]["error"] = str(e)[:150]
    try:
        h = contacts.plugin_health()
        self_id = h.get("self") or ""
        out["plugin"] = {"reachable": True, "self": self_id,
                         "self_name": contacts.self_nickname(self_id),
                         "wechatReady": h.get("wechatReady"),
                         "webhook": h.get("webhook"), "version": h.get("version")}
        # 早期修订：授权状态单独一路 GET /api/license，插件离线时不再发请求。
        # 授权过期会主动关掉插件 HTTP 侦听，此时 health 也会失败，走下面 except。
        try:
            out["plugin"]["license"] = contacts.plugin_license()
        except Exception as e:
            out["plugin"]["license_error"] = str(e)[:150]
    except Exception as e:
        out["plugin"]["error"] = str(e)[:150]
        # 插件离线时不发额外请求，直接沿用上次缓存到的昵称
        out["plugin"]["self_name"] = contacts.cached_self_nickname()
    base = (c.get("server") or {}).get("public_base_url") or "http://127.0.0.1:29999"
    tok = (c.get("webhook") or {}).get("token") or ""
    out["webhook_suggest"] = base.rstrip("/") + "/webhook" + (
        "?token=" + tok if tok else "")
    out["main_service"] = main
    return _ok(**out)


@app.route("/api/service")
@login_required
def api_service_status():
    """主服务进程状态（启动中 / 在线 / 已停止 / 由谁启动）。"""
    return jsonify(proc.status())


@app.route("/api/service", methods=["POST"])
@login_required
def api_service_action():
    """启动 / 停止 / 重启主服务。只停止管理端自己启动的进程。"""
    action = str(_body().get("action") or "").strip().lower()
    if action not in ("start", "stop", "restart"):
        return _err("action 只能是 start / stop / restart")
    r = getattr(proc, action)()
    adminlog("服务控制 action=%s user=%s ok=%s %s", action, session.get("uid"),
             r.get("ok"), r.get("message") or r.get("error") or "")
    return jsonify(r) if r.get("ok") else (jsonify(r), 400)


@app.route("/api/api-service", methods=["GET", "POST"])
@login_required
def api_api_service():
    """运行监控：知识库接口（/httpapi/query）的状态与启动 / 停止 / 重启。

    接口路由在主服务进程内，管理端只通过 /internal/api-control 转发指令，
    不重启主服务、也不碰微信客服主业。

    主服务是旧版本（没有 /internal/api-control）或未启动时：
      - GET 仍返回 200，但给 `state.available=false` + `hint`，页面显示「请重启主服务」；
      - POST 返回 400 + 一句人话，绝不把下游的 404 HTML 抛给前端 toast。
    """
    def _hint(code, d):
        raw = str((d or {}).get("error") or (d or {}).get("message") or "")
        low = raw.lower()
        # 卡片里的「处理建议」只放几个字；排查细节写到 deploy.md 故障排查表
        if code == 404 or "html" in low or "旧版本" in raw:
            return "请重启主服务"
        return raw[:80] if raw else "请启动主服务"

    def _degraded(hint):
        return _ok(message="", degraded=True,
                   state={"available": False, "hint": hint,
                          "enabled": False, "registered": False,
                          "key_set": False, "config_enabled": False,
                          "restarts": 0})

    try:
        if request.method == "POST":
            action = str((_body().get("action") or "")).strip().lower()
            code, d = _call_main("/internal/api-control", method="POST",
                                 body={"action": action}, timeout=5)
            adminlog("知识库接口控制 action=%s user=%s ok=%s", action,
                     session.get("uid"), bool((d or {}).get("ok")))
        else:
            code, d = _call_main("/internal/api-control", timeout=3)
        d = d if isinstance(d, dict) else {}
        if code >= 400 or not d.get("ok"):
            hint = _hint(code, d)
            return _degraded(hint) if request.method == "GET" else _err(hint)
        return _ok(message=d.get("message") or "", state=d.get("state") or {})
    except Exception:
        hint = "请启动主服务"
        return _degraded(hint) if request.method == "GET" else _err(hint)


# ---------------------------------------------------------------- 插件管理（P2）
# 管理端是独立进程（默认 2 worker）：只读插件清单、写私有配置文件，
# 启停 / 优先级 / 重载一律通过 /internal/plugins/* 转发主服务，绝不 import 插件代码。
_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PLUGIN_EMPTY = {"enabled": False, "count": 0, "handlers": 0, "loaded_at": 0,
                 "dirs": [], "config_dir": "", "plugins": [], "errors": []}


def _plugin_hint(code, d):
    raw = str((d or {}).get("error") or (d or {}).get("message") or "")
    low = raw.lower()
    if code == 404 or "html" in low or "旧版本" in raw:
        return "请重启主服务（当前版本缺少插件内部接口）"
    return raw[:100] if raw else "请启动主服务"


def _plugin_view(d):
    """主服务清单原样透传，并附上 P3 的调度器运行时状态与扩展路由清单。

    管理端不缓存、不加工插件状态，也不 import 插件代码；旧版主服务没有这两个
    字段时给空对象，前端会显示「未启用」而不是报错。
    """
    d = d or {}
    p = d.get("plugins") or {}
    p = dict(p) if isinstance(p, dict) else {}
    p["scheduler"] = d.get("scheduler") or {}
    p["http_routes"] = d.get("http_routes") or {}
    p["task_manager"] = d.get("task_manager") or {}
    return p


def _plugin_cfg_path(name):
    """插件私有配置目录：与主服务同机同卷，Docker 内是 /app/data/plugins。"""
    c = cfg.read_config()
    p = str((c.get("plugins") or {}).get("config_dir") or "data/plugins")
    if not os.path.isabs(p):
        p = os.path.join(BASE_DIR, p)
    return os.path.join(os.path.normpath(p), name + ".yaml")


@app.route("/api/plugins", methods=["GET", "POST"])
@login_required
def api_plugins():
    """插件清单（GET）/ 启停与优先级（POST）。

    主服务未启动或还是旧版本时：GET 仍返回 200 但带 degraded + hint（页面给提示，
    不弹一屏 HTML）；POST 返回 400 + 一句人话。
    """
    is_get = request.method == "GET"
    try:
        if is_get:
            code, d = _call_main("/internal/plugins", timeout=3)
        else:
            b = _body()
            name = str(b.get("name") or "").strip()
            if not _PLUGIN_NAME_RE.match(name):
                return _err("插件名不合法（只允许字母、数字、下划线、短横线）")
            payload = {"name": name}
            if "enabled" in b:
                payload["enabled"] = bool(b["enabled"])
            if "priority" in b:
                v = b["priority"]
                if v in (None, "", "null"):
                    payload["priority"] = None    # 回落到 manifest / 装饰器优先级
                else:
                    try:
                        payload["priority"] = int(v)
                    except (TypeError, ValueError):
                        return _err("优先级必须是整数或留空")
            if len(payload) == 1:
                return _err("没有要修改的字段")
            code, d = _call_main("/internal/plugins/state", method="POST",
                                 body=payload, timeout=8)
            adminlog("插件状态 name=%s payload=%s user=%s ok=%s", name,
                     {k: v for k, v in payload.items() if k != "name"},
                     session.get("uid"), bool((d or {}).get("ok")))
        d = d if isinstance(d, dict) else {}
        if code >= 400 or not d.get("ok"):
            hint = _plugin_hint(code, d)
            return (_ok(degraded=True, hint=hint, plugins=dict(_PLUGIN_EMPTY))
                    if is_get else _err(hint))
        p = _plugin_view(d)
        return _ok(plugins=p, message=d.get("message") or "",
                   warning=p.get("reload_error") or "")
    except Exception as e:
        hint = "无法连接主服务：%s" % e
        return (_ok(degraded=True, hint=hint, plugins=dict(_PLUGIN_EMPTY))
                if is_get else _err("请启动主服务后再操作"))


@app.route("/api/plugins/reload", methods=["POST"])
@login_required
def api_plugins_reload():
    """重新扫描插件目录并原子替换注册表；整体失败时主服务会保留旧表。"""
    try:
        code, d = _call_main("/internal/plugins/reload", method="POST",
                             body={}, timeout=10)
        d = d if isinstance(d, dict) else {}
        if code >= 400 or not d.get("ok"):
            return _err(_plugin_hint(code, d))
        p = _plugin_view(d)
        adminlog("插件重载 user=%s 插件=%s 处理器=%s warning=%s",
                 session.get("uid"), p.get("count"), p.get("handlers"),
                 p.get("reload_error") or "")
        return _ok(plugins=p, warning=p.get("reload_error") or "",
                   message="已重新扫描并重载插件注册表")
    except Exception as e:
        return _err("请启动主服务后再重载：%s" % e)


@app.route("/api/plugins/config", methods=["GET", "POST"])
@login_required
def api_plugin_config():
    """插件私有配置 data/plugins/<Name>.yaml 的查看 / 编辑（按 mtime 热读，无需重启）。

    白名单：插件名必须同时通过正则、且出现在主服务当前清单里，防止借接口写任意文件。
    保存前校验 YAML 顶层为对象、限制 32KB、留一份 .bak，再用临时文件原子替换。
    """
    if request.method == "GET":
        name = str(request.args.get("name") or "").strip()
        body = {}
    else:
        body = _body()
        name = str(body.get("name") or "").strip()
    if not _PLUGIN_NAME_RE.match(name):
        return _err("插件名不合法")
    try:
        code, d = _call_main("/internal/plugins", timeout=3)
        names = [str(x.get("name") or "")
                 for x in (_plugin_view(d).get("plugins") or [])]
        if code >= 400 or not (d or {}).get("ok") or name not in names:
            return _err("插件不在主服务清单中，或主服务未启动")
    except Exception:
        return _err("请先启动主服务，再编辑插件配置")
    path = _plugin_cfg_path(name)
    if request.method == "GET":
        try:
            text = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
        except Exception as e:
            return _err("读取失败：%s" % e)
        return _ok(name=name, path=os.path.dirname(path), text=text,
                   exists=os.path.isfile(path))
    text = str(body.get("text") or "")
    if len(text.encode("utf-8")) > 32768:
        return _err("配置不能超过 32KB")
    try:
        parsed = yaml.safe_load(text)
        if parsed is not None and not isinstance(parsed, dict):
            return _err("插件私有配置必须是 YAML 键值对（顶层为对象）")
    except Exception as e:
        return _err("YAML 格式错误：%s" % e)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path):
            shutil.copy2(path, path + ".bak")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception as e:
        return _err("保存失败：%s" % e)
    adminlog("插件配置保存 name=%s user=%s bytes=%d", name,
             session.get("uid"), len(text))
    return _ok(name=name, message="已保存，下一条消息起按 mtime 热读生效")


# ---------------------------------------------------------------- 定时任务实例（P4）
# 任务的执行函数写在插件代码的 @schedule 里，后台不能凭空造出有行为的新任务。
# 这里管理的是「实例」：对已声明任务做覆盖（启停 / 周期 / 显示名），以及以某个
# 已声明任务的 handler 为模板复制出新实例。文件与主服务同机同卷，主服务按 mtime
# 热读，保存后最多一个 tick 生效，无需重启或重载注册表。
_TASK_KEY_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]{1,47}:[A-Za-z][A-Za-z0-9_]{0,47}"
    r"(?:#[A-Za-z][A-Za-z0-9_]{0,31})?$")
_TASK_MAX_SECONDS = 30 * 24 * 3600


def _plugin_tasks_path():
    c = cfg.read_config()
    p = str((c.get("plugins") or {}).get("config_dir") or "data/plugins")
    if not os.path.isabs(p):
        p = os.path.join(BASE_DIR, p)
    return os.path.join(os.path.normpath(p), "_tasks.yaml")


def _task_spec(body):
    """把表单值校验成合法的周期规格；非法返回 None。"""
    kind = str(body.get("kind") or "interval").strip().lower()
    if kind == "interval":
        try:
            sec = int(body.get("seconds"))
        except (TypeError, ValueError):
            return None
        return {"kind": "interval", "seconds": sec} if 1 <= sec <= _TASK_MAX_SECONDS else None
    if kind == "daily":
        try:
            h = int(body.get("hour"))
            mi = int(body.get("minute") if body.get("minute") is not None else 0)
        except (TypeError, ValueError):
            return None
        return ({"kind": "daily", "hour": h, "minute": mi}
                if 0 <= h <= 23 and 0 <= mi <= 59 else None)
    return None


def _read_tasks_file(path):
    if not os.path.isfile(path):
        return {"version": 1, "overrides": {}, "instances": []}
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError("任务配置顶层必须是键值映射")
    ovs = raw.get("overrides") or {}
    ins = raw.get("instances") or []
    return {"version": 1,
            "overrides": dict(ovs) if isinstance(ovs, dict) else {},
            "instances": [dict(x) for x in ins if isinstance(x, dict)]
            if isinstance(ins, list) else []}


def _write_tasks_file(path, data):
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                          default_flow_style=False)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isfile(path):
        shutil.copy2(path, path + ".bak")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _task_declarations():
    """从主服务拿插件代码里静态声明的任务 key，作为写入白名单。失败返回空列表。"""
    try:
        code, d = _call_main("/internal/plugins", timeout=3)
        if code >= 400 or not (d or {}).get("ok"):
            return []
        return [str(x.get("key") or "")
                for x in (((d.get("plugins") or {}).get("schedules")) or [])]
    except Exception:
        return []


@app.route("/api/plugins/tasks", methods=["GET", "POST"])
@login_required
def api_plugin_tasks():
    """定时任务实例的读取与增删改。

    GET  返回原始 YAML 文本、静态声明清单、主服务合并后的任务视图。
    POST body: {action: save|delete|restore|toggle, key, from?, label?,
                enabled?, kind?, seconds?, hour?, minute?}
    """
    path = _plugin_tasks_path()
    body = _body() if request.method == "POST" else {}
    if request.method == "GET":
        try:
            text = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
        except Exception as e:
            return _err("读取失败：%s" % e)
        try:
            code, d = _call_main("/internal/plugins", timeout=3)
            tm = (_plugin_view(d or {}).get("task_manager") or {})
        except Exception:
            tm = {}
        return _ok(text=text, path=os.path.dirname(path),
                   declarations=_task_declarations(),
                   items=tm.get("items") or [], error=tm.get("error") or "")

    action = str(body.get("action") or "").strip()
    key = str(body.get("key") or "").strip()
    if not _TASK_KEY_RE.match(key):
        return _err("任务标识不合法")
    decls = _task_declarations()
    if not decls:
        return _err("当前没有可管理的定时任务：请启动主服务，并至少启用一个带 @schedule 声明的插件")
    custom = "#" in key
    if not custom and key not in decls:
        return _err("任务不在插件声明清单中：%s" % key)
    try:
        data = _read_tasks_file(path)
        if action == "save":
            spec = _task_spec(body)
            if spec is None:
                return _err("周期不合法（interval 秒数 1~%d，daily 时分 0~23 / 0~59）"
                            % _TASK_MAX_SECONDS)
            label = str(body.get("label") or "").strip().replace("\n", " ")[:64]
            enabled = body.get("enabled", True) is not False
            if custom:
                src = str(body.get("from") or "").strip()
                if not _TASK_KEY_RE.match(src) or "#" in src or src not in decls:
                    return _err("新实例必须复制自一个已声明的任务")
                entry = {"key": key, "from": src, "label": label or key.split("#")[-1],
                         "enabled": enabled}
                entry.update(spec)
                data["instances"] = [x for x in data["instances"]
                                     if str(x.get("key") or "") != key]
                data["instances"].append(entry)
            else:
                entry = {"enabled": enabled, "label": label or key.split(":")[-1]}
                entry.update(spec)
                data["overrides"][key] = entry
            msg = "已保存任务实例，最多一个 tick 后生效"
        elif action == "delete":
            if custom:
                data["instances"] = [x for x in data["instances"]
                                    if str(x.get("key") or "") != key]
                msg = "已删除自定义实例"
            else:
                ov = data["overrides"].get(key) or {}
                ov["enabled"] = False
                data["overrides"][key] = ov
                msg = "已移除任务（代码声明仍在，可随时恢复）"
        elif action == "restore":
            if custom:
                return _err("自定义实例删除后请用「新增」重新创建")
            data["overrides"].pop(key, None)
            msg = "已恢复为插件代码声明的周期"
        elif action == "toggle":
            enabled = body.get("enabled", True) is not False
            if custom:
                hit = [x for x in data["instances"] if str(x.get("key") or "") == key]
                if not hit:
                    return _err("自定义实例不存在")
                hit[0]["enabled"] = enabled
            else:
                ov = data["overrides"].get(key) or {}
                ov["enabled"] = enabled
                data["overrides"][key] = ov
            msg = "已" + ("启用" if enabled else "停用") + "任务"
        else:
            return _err("未知操作")
        if len(data["overrides"]) + len(data["instances"]) > 100:
            return _err("任务条目超过 100 条上限")
        _write_tasks_file(path, data)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err("保存失败：%s" % e)
    adminlog("插件任务 action=%s key=%s user=%s", action, key, session.get("uid"))
    try:
        _, d = _call_main("/internal/plugins", timeout=3)
        p = _plugin_view(d or {})
    except Exception:
        p = dict(_PLUGIN_EMPTY)
    return _ok(plugins=p, message=msg)


@app.route("/api/contacts/webhook", methods=["POST"])
@login_required
def api_set_webhook():
    url = str(_body().get("url") or "").strip()
    if not url:
        _, base, _ = _main_cfg()
        c = cfg.read_config()
        pub = (c.get("server") or {}).get("public_base_url") or "http://127.0.0.1:29999"
        tok = (c.get("webhook") or {}).get("token") or ""
        url = pub.rstrip("/") + "/webhook" + ("?token=" + tok if tok else "")
    try:
        contacts.set_webhook(url)
        return _ok(message="已设置插件推送地址", url=url)
    except Exception as e:
        return _err("设置失败（手机在线吗？）：%s" % e)


@app.route("/api/plugin/license")
@login_required
def api_plugin_license():
    """运行监控：主动刷新手机插件授权状态（GET /api/license）。"""
    try:
        return _ok(license=contacts.plugin_license(refresh=True))
    except Exception as e:
        return _err("读取授权状态失败（手机在线吗？）：%s" % e)


@app.route("/api/plugin/activate", methods=["POST"])
@login_required
def api_plugin_activate():
    """运行监控：提交手机插件激活码（POST /api/activate）。

    ⚠️ 插件协议里空码 / 省略 code 表示「清除激活码、回到试用」，是危险操作。
    本后台只提供「激活」，空码（含纯空格 / 换行 / 连字符的等价空码）与
    非字符串一律 400 拒绝，绝不转发给插件；wx_client.activate() 还会再挡一层。
    """
    raw = _body().get("code")
    if not isinstance(raw, str):
        return _err("激活码必须是字符串")
    code = raw.strip()
    if not re.sub(r"[\s\-]+", "", code):
        return _err("激活码不能为空（空码会清除当前授权，已拒绝提交）")
    try:
        contacts.activate(code)
    except Exception as e:
        adminlog("插件激活失败 user=%s err=%s", session.get("uid"), str(e)[:150])
        return _err("激活失败：%s" % e)
    adminlog("插件激活成功 user=%s", session.get("uid"))
    try:
        lic = contacts.plugin_license(refresh=True)
    except Exception:
        lic = {}
    return _ok(message="激活成功，授权状态已更新", license=lic)


# ---------------------------------------------------------------- 下拉选项

@app.route("/api/options")
@login_required
def api_options():
    return _ok(policies=store.list_policies(), docs=store.all_kb_docs(),
               groups=store.list_groups())


# ---------------------------------------------------------------- 知识库管理

@app.route("/api/policies", methods=["GET", "POST"])
@login_required
def api_policies():
    if request.method == "GET":
        items = store.list_policies()
        return _ok(items=items, total=len(items))
    try:
        pid = store.save_policy(_body())
        adminlog("保存知识库 id=%s user=%s", pid, session.get("uid"))
        return _ok(message="知识库已保存", id=pid, items=store.list_policies())
    except (ValueError, OSError) as e:
        return _err(e)


@app.route("/api/policies/<int:pid>", methods=["DELETE"])
@login_required
def api_policy_delete(pid):
    try:
        store.delete_policy(pid)
        return _ok(message="知识库已删除", items=store.list_policies())
    except ValueError as e:
        return _err(e)


@app.route("/api/policies/sync-docs", methods=["POST"])
@login_required
def api_policies_sync_docs():
    pruned = store.prune_missing_docs()
    added = store.sync_existing_docs()
    return _ok(message="已同步 %d 个新文档、清理 %d 个失效引用" % (len(added), pruned),
               added=added, pruned=pruned, items=store.list_policies())


# ---------------------------------------------------------------- 群聊 / 私聊配置

@app.route("/api/group-rules", methods=["GET", "POST"])
@login_required
def api_group_rules():
    if request.method == "GET":
        items = store.list_group_rules()
        return _ok(items=items, total=len(items))
    try:
        rid = store.save_group_rule(_body())
        adminlog("保存群聊配置 id=%s user=%s", rid, session.get("uid"))
        return _ok(message="群聊配置已保存", id=rid, items=store.list_group_rules())
    except (ValueError, OSError) as e:
        return _err(e)


@app.route("/api/group-rules/<int:rid>", methods=["DELETE"])
@login_required
def api_group_rule_delete(rid):
    store.delete_group_rule(rid)
    return _ok(message="已删除", items=store.list_group_rules())


@app.route("/api/private-rules", methods=["GET", "POST"])
@login_required
def api_private_rules():
    if request.method == "GET":
        items = store.list_private_rules()
        return _ok(items=items, total=len(items))
    try:
        rid = store.save_private_rule(_body())
        adminlog("保存私聊配置 id=%s user=%s", rid, session.get("uid"))
        return _ok(message="私聊配置已保存", id=rid, items=store.list_private_rules())
    except (ValueError, OSError) as e:
        return _err(e)


@app.route("/api/private-rules/<int:rid>", methods=["DELETE"])
@login_required
def api_private_rule_delete(rid):
    store.delete_private_rule(rid)
    return _ok(message="已删除", items=store.list_private_rules())


@app.route("/api/global-deny", methods=["GET", "POST"])
@login_required
def api_global_deny():
    if request.method == "GET":
        return _ok(items=store.get_global_deny())
    store.set_global_deny(_body().get("items"))
    return _ok(message="全局黑名单已保存", items=store.get_global_deny())


# ---------------------------------------------------------------- 对话记录

@app.route("/api/records")
@login_required
def api_records():
    p = request.args
    try:
        page = max(1, int(p.get("page") or 1))
        limit = min(500, max(10, int(p.get("limit") or 30)))
    except ValueError:
        page, limit = 1, 30
    f = dict(start=p.get("start") or "", end=p.get("end") or "",
             keyword=p.get("keyword") or "", kind=p.get("kind") or "",
             chat_type=p.get("chat_type") or "")
    r, page, limit, max_page = _paged(
        lambda off: store.query_events(f["start"], f["end"], f["keyword"],
                                       f["kind"], f["chat_type"], limit, off),
        page, limit)
    return _ok(**r, page=page, limit=limit, max_page=max_page)


@app.route("/api/records/export")
@login_required
def api_records_export():
    p = request.args
    rows = store.all_events_for_export(p.get("start") or "", p.get("end") or "",
                                       p.get("keyword") or "", p.get("kind") or "",
                                       p.get("chat_type") or "")
    headers = ["时间", "会话", "类型", "发送者", "消息类型", "结果", "知识库", "触发",
               "插件", "提问", "回复", "耗时(ms)", "原因", "配图"]
    data = [[r.get("time_str"), r.get("chat_name") or r.get("chat"),
             r.get("chat_type"), r.get("sender_name") or r.get("sender"),
             r.get("msg_type"), r.get("kind"), r.get("policy_name"),
             r.get("trigger"), r.get("plugin") or "",
             r.get("question"), r.get("answer"),
             r.get("ms"), r.get("reason"), r.get("n_images")] for r in rows]
    return _csv("对话记录-%s.csv" % time.strftime("%Y%m%d"), headers, data)


# ---------------------------------------------------------------- 聊天记录（独立库）

def _chat_filter_args():
    p = request.args
    return dict(start=p.get("start") or "", end=p.get("end") or "",
                chat_type=p.get("chat_type") or "", who=p.get("who") or "",
                keyword=p.get("keyword") or "", msg_type=p.get("msg_type") or "",
                direction=p.get("direction") or "")


@app.route("/api/chat-messages")
@login_required
def api_chat_messages():
    try:
        page = max(1, int(request.args.get("page") or 1))
        limit = min(500, max(10, int(request.args.get("limit") or 30)))
    except ValueError:
        page, limit = 1, 30
    flt = _chat_filter_args()
    r, page, limit, max_page = _paged(
        lambda off: chatlog.query(limit=limit, offset=off, **flt), page, limit)
    return _ok(**r, page=page, limit=limit, max_page=max_page,
               stats=chatlog.stats())


@app.route("/api/chat-messages/export")
@login_required
def api_chat_messages_export():
    rows = chatlog.all_for_export(**_chat_filter_args())
    headers = ["时间", "场景", "会话", "会话名称", "发送者", "显示名", "方向",
               "消息类型", "类型名", "正文", "原始内容", "@我", "@所有人",
               "localID", "媒体地址"]
    data = [[r.get("time_str"),
             {"group": "群聊", "private": "私聊"}.get(r.get("chat_type"), r.get("chat_type")),
             r.get("chat"), r.get("chat_name"), r.get("sender"), r.get("sender_name"),
             {"in": "收到", "out": "发出"}.get(r.get("direction"), r.get("direction")),
             r.get("type"), r.get("type_name"), r.get("text"), r.get("content"),
             "是" if r.get("at_me") else "", "是" if r.get("at_all") else "",
             r.get("local_id"), r.get("media_api")] for r in rows]
    return _csv("聊天记录-%s.csv" % time.strftime("%Y%m%d"), headers, data)


@app.route("/api/chat-messages/purge", methods=["POST"])
@login_required
def api_chat_messages_purge():
    """手动触发一次过期清理（定时线程也会自动跑）。"""
    n = chatlog.purge_expired()
    adminlog("清理过期聊天记录 user=%s 删除=%s", session.get("uid"), n)
    return _ok(message="已清理 %d 条过期消息" % n, stats=chatlog.stats())


@app.route("/api/stats")
@login_required
def api_stats():
    """运行监控图表数据源：一次请求同时给微信对话与接口调用两条趋势，
    保证时间窗口一致。days 由卡片上的 7/30/90 切换传入。"""
    days = _days_arg(request.args)
    # api_calls_summary() 只查一次：原先同一请求算了两次。
    # api / api_summary 两个键都保留（/api/stats 的历史契约），共用同一结果。
    api_summary = store.api_calls_summary()
    return _ok(summary=store.summary(), daily=store.daily_series(days),
               api=api_summary, llm=store.llm_summary(),
               api_daily=store.api_calls_daily(days),
               api_summary=api_summary)


# ---------------------------------------------------------------- 统计查询通用参数

def _page_args():
    p = request.args
    try:
        page = max(1, int(p.get("page") or 1))
        limit = min(500, max(10, int(p.get("limit") or 30)))
    except (TypeError, ValueError):
        page, limit = 1, 30
    return p, page, limit


def _paged(fetch, page, limit):
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


def _days_arg(p):
    try:
        return min(365, max(1, int(p.get("days") or 30)))
    except (TypeError, ValueError):
        return 30


@app.route("/api/apicalls")
@login_required
def api_apicalls():
    p, page, limit = _page_args()
    f = dict(start=p.get("start") or None, end=p.get("end") or None,
             keyword=(p.get("keyword") or "").strip()[:100] or None,
             only_failed=p.get("only_failed") == "1")
    r, page, limit, max_page = _paged(
        lambda off: store.api_calls_records(limit=limit, offset=off, **f),
        page, limit)
    return _ok(**r, page=page, limit=limit, max_page=max_page)


@app.route("/api/api-calls/stats")
@login_required
def api_api_calls_stats():
    """API 调用汇总（含失败原因分布）+ 每日趋势。"""
    p = request.args
    return _ok(summary=store.api_calls_summary(p.get("start") or None,
                                               p.get("end") or None),
               daily=store.api_calls_daily(_days_arg(p), end_day=p.get("end") or None))


@app.route("/api/api-calls/export")
@login_required
def api_api_calls_export():
    p = request.args
    rows = store.api_calls_all_for_export(
        start=p.get("start") or None, end=p.get("end") or None,
        keyword=(p.get("keyword") or "").strip()[:100] or None,
        only_failed=p.get("only_failed") == "1")
    headers = ["时间", "日期", "提问", "答案", "图片数", "提问字数", "会话ID", "调用方IP",
               "HTTP状态", "结果", "耗时(ms)", "LLM调用次数", "总token", "费用(元)", "失败原因"]
    data = [[r.get("time_str"), r.get("day"), r.get("question"), r.get("answer"),
             r.get("n_images"), r.get("q_chars"), r.get("session_id"), r.get("client_ip"),
             r.get("status"), "成功" if r.get("ok") else "失败", r.get("latency_ms"),
             r.get("calls"), r.get("total_tokens"), r.get("cost"), r.get("error")]
            for r in rows]
    return _csv("API调用记录-%s.csv" % time.strftime("%Y%m%d"), headers, data)


# ---------------------------------------------------------------- AI 大模型用量

def _llm_model_labels():
    """上游模型 ID → 模型库里的纳管名称（可能一对多），给筛选下拉显示用。"""
    labels = {}
    for m in store.list_llm_models():
        labels.setdefault(m.get("model") or "", []).append(m.get("name") or "")
    return {k: "、".join(v) for k, v in labels.items() if k}


@app.route("/api/llm-usage")
@login_required
def api_llm_usage():
    """区间汇总 + 按模型分布 + 每日趋势 + 今日（不能拿全量冒充）。

    汇总 / 分布 / 趋势 / 今日**全部带上同一个 model 条件**，
    与明细 /api/llm-records 的结果集严格一致，筛选后合计才对得上。
    """
    p = request.args
    start, end = p.get("start") or None, p.get("end") or None
    model = (p.get("model") or "").strip()[:100] or None
    s = store.llm_summary(start, end, model)
    s.update(ok=True,
             daily=store.llm_daily(_days_arg(p), end_day=end, model=model),
             today=store.llm_summary(store.today(), store.today(), model),
             pricing=pricing.all_known_pricing(),
             # 下拉选项取全量模型，不能取筛选后的 models（那样筛完只剩一个可选）
             all_models=[m["model"] for m in store.llm_summary().get("models", [])
                         if m.get("model")],
             # 以「AI 大模型」页纳管的模型为主体做用量对照（含零用量 / 未纳管分组）
             registry=store.llm_registry_usage(start, end, model),
             model_labels=_llm_model_labels(),
             model_filter=model or "", start=start or "", end=end or "")
    return jsonify(s)


@app.route("/api/llm-records")
@login_required
def api_llm_records():
    p, page, limit = _page_args()
    f = dict(start=p.get("start") or None, end=p.get("end") or None,
             model=(p.get("model") or "").strip()[:100] or None)
    r, page, limit, max_page = _paged(
        lambda off: store.llm_records(start=f["start"], end=f["end"],
                                      model=f["model"], limit=limit, offset=off),
        page, limit)
    return _ok(**r, page=page, limit=limit, max_page=max_page,
               models=[m["model"] for m in store.llm_summary().get("models", [])])


@app.route("/api/llm-records/export")
@login_required
def api_llm_export():
    p = request.args
    rows = store.llm_all_for_export(start=p.get("start") or None,
                                    end=p.get("end") or None,
                                    model=(p.get("model") or "").strip()[:100] or None)
    headers = ["时间", "日期", "模型", "来源", "输入token", "输出token", "总token",
               "费用(元)", "耗时(ms)", "流式", "成功"]
    data = [[r.get("time_str"), r.get("day"), r.get("model"), r.get("source"),
             r.get("prompt_tokens"), r.get("completion_tokens"), r.get("total_tokens"),
             r.get("cost"), r.get("latency_ms"), "是" if r.get("stream") else "否",
             "是" if r.get("ok") else "否"] for r in rows]
    return _csv("大模型使用记录-%s.csv" % time.strftime("%Y%m%d"), headers, data)


# ---------------------------------------------------------------- 通讯录

@app.route("/api/contacts/sync", methods=["POST"])
@login_required
def api_contacts_sync():
    try:
        r = contacts.sync_all()
        adminlog("同步通讯录 群=%s 好友=%s user=%s", r["groups"], r["friends"], session.get("uid"))
        return _ok(message="同步完成：%d 个群、%d 位好友" % (r["groups"], r["friends"]),
                   groups=r["group_list"], friends=r["friend_list"])
    except Exception as e:
        return _err("同步失败（检查手机插件地址 / 手机是否在线）：%s" % e)


@app.route("/api/contacts/groups")
@login_required
def api_contacts_groups():
    return _ok(items=store.list_groups())


@app.route("/api/contacts/groups/members")
@login_required
def api_contacts_members():
    chat = request.args.get("chat") or ""
    refresh = request.args.get("refresh") in ("1", "true", "yes")
    try:
        return _ok(**contacts.group_members(chat, refresh=refresh))
    except Exception as e:
        return _err(str(e))


@app.route("/api/contacts/groups/members/export")
@login_required
def api_contacts_members_export():
    chat = request.args.get("chat") or ""
    try:
        d = contacts.group_members(chat, refresh=False)
    except Exception as e:
        return _err(str(e))
    headers = ["群聊", "wxid", "显示名", "昵称", "备注", "群名片", "是否本人", "是否群主"]
    # 显示名必须和界面口径一致（app.js 的 shown_name）：
    # display_name → name → wxid。旧实现只写 name，导出里看到的是原始昵称，
    # 和表格里显示的群名片 / 自定义显示名对不上。
    rows = [[d.get("group_name") or chat, m.get("wxid"),
             m.get("display_name") or m.get("name") or m.get("wxid"),
             m.get("nick_name"), m.get("remark"), m.get("group_nick"),
             "是" if m.get("is_self") else "", "是" if m.get("is_owner") else ""]
            for m in d.get("members") or []]
    name = (d.get("group_name") or chat).replace("/", "_")
    return _csv("群成员-%s-%s.csv" % (name, time.strftime("%Y%m%d")), headers, rows)


@app.route("/api/contacts/search")
@login_required
def api_contacts_search():
    """全量通讯录搜索：好友 / 群聊 / 群成员一起按 wxid、name 查本地缓存。"""
    try:
        q = (request.args.get("q") or "").strip()
        items = store.search_contacts(q)
        return _ok(q=q, items=items, count=len(items),
                   groups=sum(1 for x in items if x["kind"] == "group"),
                   friends=sum(1 for x in items if x["kind"] == "friend"),
                   members=sum(1 for x in items if x["kind"] == "member"))
    except (ValueError, OSError) as e:
        return _err(str(e))


@app.route("/api/contacts/friends")
@login_required
def api_contacts_friends():
    return _ok(items=store.list_friends())


@app.route("/api/contacts/friends/detail")
@login_required
def api_contacts_friend_detail():
    """好友详情：只读本地通讯录缓存，不再向插件发实时查询。"""
    try:
        return _ok(**contacts.friend_detail(request.args.get("wxid") or ""))
    except (ValueError, OSError) as e:
        return _err(e)


# ---------------------------------------------------------------- 朋友圈（v3）
# 全部只读 / 写操作都经 admin/moments.py 代理手机插件：token 不出后端，媒体不直连 CDN。
@app.route("/api/moments/feed")
@login_required
def api_moments_feed():
    """时间线 / 好友主页：action=head 重置到最新，next 追加一页。"""
    try:
        d = moments.feed(
            source=request.args.get("source") or "timeline",
            action=request.args.get("action") or "head",
            user=request.args.get("user") or "",
            media=request.args.get("media", "1") not in ("0", "false", "no"),
            limit=request.args.get("limit") or 0)
        return _ok(**d)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err("读取朋友圈失败（检查手机插件是否在线）：%s" % e)


@app.route("/api/moments/friends")
@login_required
def api_moments_friends():
    """「按好友」下拉：本地通讯录缓存里的好友，也允许前端手动输入 wxid。"""
    return _ok(items=moments.friend_options())


def _media_response(data, mime):
    resp = make_response(data)
    resp.headers["Content-Type"] = mime or "application/octet-stream"
    resp.headers["Cache-Control"] = "private, max-age=3600"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.route("/api/moments/media")
@login_required
def api_moments_media():
    """图片 / 视频二进制代理。浏览器带 Referer 直连微信 CDN 只会拿到占位图。"""
    try:
        data, mime = moments.media(request.args.get("tid"),
                                   request.args.get("index") or 0)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err("媒体读取失败（动态可能已被微信清理，请刷新）：%s" % e)
    return _media_response(data, mime)


@app.route("/api/moments/cover")
@login_required
def api_moments_cover():
    """视频封面：后端不带 Referer 取缩略图，失败再用 ffmpeg 抽视频首帧。

    不再把 CDN 直链透给浏览器（白名单外的域名 / 直链过期会导致封面空白）。
    """
    try:
        data, mime = moments.cover(request.args.get("tid"),
                                   request.args.get("index") or 0)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err("封面读取失败：%s" % e)
    return _media_response(data, mime)


@app.route("/api/moments/avatar")
@login_required
def api_moments_avatar():
    """朋友圈发布者头像：按 username 查通讯录缓存，后端取回 CDN 二进制转 blob:。

    和媒体 / 封面一样不带 Referer、不下发直链；查不到时前端隐藏图片回退首字母。
    """
    try:
        data, mime = moments.avatar(request.args.get("user") or "")
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err("头像读取失败：%s" % e)
    return _media_response(data, mime)


@app.route("/api/moments/video")
@login_required
def api_moments_video():
    """H.264 转码版视频：浏览器解不出 HEVC 画面时前端才来取。"""
    try:
        data, mime = moments.video_h264(request.args.get("tid"),
                                        request.args.get("index") or 0)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err("视频转换失败：%s" % e)
    return _media_response(data, mime)


@app.route("/api/moments/like", methods=["POST"])
@login_required
def api_moments_like():
    """点赞 / 取消赞：真实社交动作，前端已二次确认，后端仍先 dryRun 预检。"""
    b = _body()
    dry = bool(b.get("dryRun"))
    try:
        d = moments.like(b.get("tid"), unlike=bool(b.get("unlike")),
                         dry_run=dry)
    except (ValueError, RuntimeError) as e:
        return _err(str(e))
    except Exception as e:
        return _err("点赞失败：%s" % e)
    if not dry:
        adminlog("朋友圈%s tid=%s user=%s changed=%s",
                 "取消赞" if b.get("unlike") else "点赞", b.get("tid"),
                 session.get("uid"), d.get("changed"))
    payload = {k: v for k, v in dict(d).items() if k != "ok"}
    payload.setdefault("message", "已取消赞" if b.get("unlike") else "已点赞")
    return _ok(**payload)


@app.route("/api/moments/comment", methods=["POST"])
@login_required
def api_moments_comment():
    """发表评论：真实社交动作，前端二次确认 + 后端 dryRun 预检。"""
    b = _body()
    dry = bool(b.get("dryRun"))
    try:
        d = moments.comment(b.get("tid"), b.get("content"), dry_run=dry)
    except (ValueError, RuntimeError) as e:
        return _err(str(e))
    except Exception as e:
        return _err("评论失败：%s" % e)
    if not dry:
        adminlog("朋友圈评论 tid=%s user=%s changed=%s",
                 b.get("tid"), session.get("uid"), d.get("changed"))
    payload = {k: v for k, v in dict(d).items() if k != "ok"}
    # confirmed=false 表示微信侧 CGI 回调没确认，可能只改了本地缓存：如实告知，
    # 不能让用户以为评论已经发到对方那里。
    payload.setdefault("message", "评论已发表" if d.get("confirmed", True)
                       else "评论已提交，但微信未确认（可能只改了本地缓存）")
    return _ok(**payload)


@app.route("/api/moments/rm_comment", methods=["POST"])
@login_required
def api_moments_rm_comment():
    """删除本人评论（插件 8.11）：按 commentId 精确删除，前端二次确认 + dryRun 预检。

    插件不允许「不传定位参数」的模糊删除走本后台；删除不可撤回，记入管理端日志。
    """
    b = _body()
    dry = bool(b.get("dryRun"))
    try:
        d = moments.rm_comment(b.get("tid"), b.get("commentId"),
                               b.get("content"), dry_run=dry)
    except (ValueError, RuntimeError) as e:
        return _err(str(e))
    except Exception as e:
        return _err("删除评论失败：%s" % e)
    if not dry:
        adminlog("朋友圈删除评论 tid=%s commentId=%s user=%s changed=%s",
                 b.get("tid"), b.get("commentId") or "-",
                 session.get("uid"), d.get("changed"))
    payload = {k: v for k, v in dict(d).items() if k != "ok"}
    payload.setdefault("message", "评论已删除")
    return _ok(**payload)


# ---------------------------------------------------------------- 知识库

@app.route("/api/kb/files")
@login_required
def api_kb_files():
    # 一次目录扫描，三处复用（items / index / stats），避免重复遍历导致的慢加载。
    # 文档要 stat（列表里显示大小/修改时间），图片只需要「有多少张」——
    # 不 stat 图片：Docker bind mount 上一次 stat 约 1.3ms，242 张就是 0.34 秒。
    scan = kbm._scan(with_stat=False)
    items = kbm.list_files(scan=scan)
    return _ok(items=items,
               index=kbm.index_status(scan=scan, doc_count=len(items)),
               stats=kbm.kb_stats(files=items, scan=scan),
               policies=[{"id": p["id"], "name": p["name"]} for p in store.list_policies()])


@app.route("/api/kb/file")
@login_required
def api_kb_read():
    try:
        return _ok(name=request.args.get("name") or "",
                   content=kbm.read_file(request.args.get("name") or ""))
    except (ValueError, FileNotFoundError, OSError) as e:
        return _err(e)


@app.route("/api/kb/file", methods=["POST"])
@login_required
def api_kb_write():
    d = _body()
    try:
        name = str(d.get("name") or "").strip()
        existed = False
        try:
            existed = os.path.isfile(kbm._rel_path(name))
        except Exception:
            pass
        r = kbm.write_file(name, d.get("content"))
        msg = ("已保存（内容已更新）" if existed else
               "已新建「%s」，并默认加入「默认知识库」" % r["name"])
        adminlog("知识库写入 name=%s user=%s", r["name"], session.get("uid"))
        return _ok(message=msg, **r)
    except (ValueError, OSError) as e:
        return _err(e)


@app.route("/api/kb/file", methods=["DELETE"])
@login_required
def api_kb_delete():
    try:
        r = kbm.delete_file(request.args.get("name") or "")
        adminlog("知识库删除 name=%s user=%s", r["name"], session.get("uid"))
        return _ok(message="已删除，并已从所有知识库移除", **r)
    except (ValueError, FileNotFoundError, OSError) as e:
        return _err(e)


@app.route("/api/kb/upload", methods=["POST"])
@login_required
def api_kb_upload():
    name, raw = "", b""
    f = request.files.get("file")
    if f:
        name, raw = f.filename or "", f.read()
    else:
        d = _body()
        name, raw = d.get("name") or "", (d.get("content") or "").encode("utf-8")
    if not name:
        return _err("缺少文件名")
    if not name.lower().endswith((".md", ".txt")):
        return _err("只支持 .md / .txt（Word/Excel/PDF 请用「导入」）")
    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return _err("文件编码无法识别（请转成 UTF-8）")
    try:
        return _ok(message="已上传（新文档默认加入「默认知识库」）",
                   **kbm.save_upload(name, text))
    except (ValueError, OSError) as e:
        return _err(e)


@app.route("/api/kb/import", methods=["POST"])
@login_required
def api_kb_import():
    results = []
    files = request.files.getlist("files") or ([request.files["file"]] if request.files.get("file") else [])
    replace = _body().get("replace") or request.form.get("replace")
    for f in files:
        try:
            r = kbm.import_document(f.read(), f.filename or "", replace=bool(replace))
            results.append({"name": f.filename, "ok": True, "result": r})
        except Exception as e:
            results.append({"name": f.filename, "ok": False, "error": str(e)})
    if not results:
        return _err("没有收到文件")
    return _ok(message="导入完成：%d 成功 / %d 失败" %
               (sum(1 for x in results if x["ok"]), sum(1 for x in results if not x["ok"])),
               results=results)


@app.route("/api/kb/images")
@login_required
def api_kb_images():
    return _ok(items=kbm.list_images())


@app.route("/api/kb/image", methods=["POST"])
@login_required
def api_kb_image_upload():
    f = request.files.get("file")
    if not f:
        return _err("缺少文件")
    try:
        return _ok(**kbm.save_image(f.read(), f.filename or ""))
    except (ValueError, OSError) as e:
        return _err(e)


@app.route("/api/kb/image", methods=["DELETE"])
@login_required
def api_kb_image_delete():
    try:
        return _ok(**kbm.delete_image(request.args.get("name") or ""))
    except (ValueError, FileNotFoundError, OSError) as e:
        return _err(e)


@app.route("/api/kb/image/raw")
@login_required
def api_kb_image_raw():
    """给预览用的原图字节（后台登录态内可读）。

    ⚠️ read_image_bytes() 的第二返回值是**安全文件名**，不是 MIME。
    早期这里直接 `mimetype=mime`，浏览器收到 `Content-Type: xxx.png`
    这种非法类型会拒绝渲染 <img> —— 图明明在库里，预览却一片空白。
    """
    try:
        data, safe = kbm.read_image_bytes(request.args.get("name") or "")
    except (ValueError, FileNotFoundError, OSError):
        abort(404)
    ext = safe.rsplit(".", 1)[-1].lower()
    mime = "image/%s" % ("jpeg" if ext in ("jpg", "jpeg") else ext)
    return send_file(io.BytesIO(data), mimetype=mime, max_age=0)


@app.route("/api/kb/rebuild", methods=["POST"])
@login_required
def api_kb_rebuild():
    r = kbm.rebuild_index()
    try:
        code, d = _call_main("/internal/reload", "POST", {}, timeout=10)
        return _ok(message="索引缓存已清空，主服务正在后台重建",
                   removed=r.get("removed") or [], reload=d)
    except Exception as e:
        return _ok(message="索引缓存已清空，但主服务未在线，需手动重启主服务：%s" % e,
                   removed=r.get("removed") or [], reload={"ok": False})


@app.route("/api/kb/rebuild/status")
@login_required
def api_kb_rebuild_status():
    """查询索引重建进度（前端轮询）。主服务离线时返回 last 状态而不是报错。"""
    try:
        code, d = _call_main("/internal/reload/status", timeout=5)
        if code == 200 and isinstance(d, dict):
            return _ok(reload=d.get("reload") or {})
        return _ok(reload={}, reachable=False,
                   message="主服务未响应，请检查主服务是否在线")
    except Exception as e:
        return _ok(reload={}, reachable=False, message=str(e)[:150])


@app.route("/api/kb/ask", methods=["POST"])
@login_required
def api_kb_ask():
    d = _body()
    try:
        r = kbm.ask(d.get("question"), policy=d.get("policy") or None)
        # 问答测试也会真实调用大模型，同样记进「AI 用量」（source=kb_test）。
        # llm_id 来自子进程里实际生效的纳管模型，不是 config.yaml 里的旧值。
        if r.get("usage"):
            store.log_llm_usage(r["usage"], model=r.get("model"), source="kb_test",
                                llm_id=r.get("llm_id"))
        return _ok(**r)
    except (ValueError, OSError) as e:
        return _err(e)


# ---------------------------------------------------------------- 配置 / 日志 / 系统

@app.route("/api/followup")
@login_required
def api_followup_get():
    """群聊对话延续：当前生效配置（含来源、活跃窗口数）。"""
    try:
        return _ok(**followup.config())
    except Exception as e:
        return _err(e)


@app.route("/api/followup", methods=["POST"])
@login_required
def api_followup_save():
    """开关 / 分钟数 / 续聊提示**文本级写回 config.yaml**，主服务下一条消息立即生效。

    时长统一用分钟：`window_minutes`（1~30）既用于接口，也原样写进文件，
    不再接收或换算旧版秒字段。写回会先校验、备份、原子替换，中文注释
    原样保留。作用范围固定同一群友，scope 参数一律忽略。
    """
    d = _body()
    try:
        c = followup.normalize({
            "enabled": d.get("enabled", True),
            "window_minutes": d.get("window_minutes",
                                    followup.DEFAULTS["window_minutes"]),
            "hint_enabled": d.get("hint_enabled", True),
            "hint": d.get("hint", followup.DEFAULT_HINT),
        })
        ok, msg, detail = confedit.write_followup(c)
        if not ok:
            return _err(msg)
        cur = followup.reload()
        adminlog("保存群聊对话延续到 config.yaml enabled=%s 分钟数=%s 提示=%s 备份=%s user=%s",
                 cur["enabled"], cur["window_minutes"], bool(cur["hint_preview"]),
                 (detail or {}).get("backup"), session.get("uid"))
        return _ok(message=msg, **cur)
    except Exception as e:
        return _err(e)


@app.route("/api/config")
@login_required
def api_config_get():
    try:
        d = confedit.read(redact=request.args.get("redact") in ("1", "true"))
        # path / backups 前端「系统配置」页要用（状态栏 + 备份查看/回滚），
        # confedit.read() 只返回文本本身，这里补齐。
        return jsonify({"ok": True, **d,
                        "path": confedit.CONFIG_PATH,
                        "backups": confedit.list_backups()})
    except (OSError, ValueError) as e:
        return _err(e)


@app.route("/api/config", methods=["POST"])
@login_required
def api_config_save():
    d = _body()
    try:
        # ⚠️ confedit.write() 返回的是 (ok, message, detail) **三元组**，不是 dict。
        # 早期这里写成 `**r` 直接把 tuple 当 mapping 展开，保存必报
        # TypeError: argument after ** must be a mapping, not tuple。
        ok, msg, detail = confedit.write(d.get("text") or "",
                                         tag=(d.get("tag") or "edit")[:16])
        if not ok:
            return jsonify({"ok": False, "error": msg, **detail}), 400
        adminlog("保存 config.yaml user=%s", session.get("uid"))
        return jsonify({"ok": True, "message": msg, **detail,
                        "backups": confedit.list_backups()})
    except (ValueError, OSError) as e:
        return _err(e)


@app.route("/api/config/validate", methods=["POST"])
@login_required
def api_config_validate():
    text = _body().get("text") or ""
    ok, err = confedit.validate(text)
    try:
        warns = confedit._semantic_warn(yaml.safe_load(text) or {})
    except Exception:
        warns = []
    return _ok(valid=ok, error=err, warns=warns)


@app.route("/api/config/backup/<name>")
@login_required
def api_config_backup(name):
    try:
        return _ok(text=confedit.read_backup(name))
    except (ValueError, OSError) as e:
        return _err(e)


@app.route("/api/config/backup/<name>", methods=["DELETE"])
@login_required
def api_config_backup_delete(name):
    """手工删除一份历史备份（列表每行一个删除按钮）。"""
    try:
        return _ok(message="已删除备份「%s」" % name, **confedit.delete_backup(name))
    except (ValueError, OSError) as e:
        return _err(e)


@app.route("/api/config/rollback", methods=["POST"])
@login_required
def api_config_rollback():
    name = _body().get("name") or ""
    try:
        # rollback() 同样返回 (ok, message) 二元组，不能 ** 展开
        ok, msg = confedit.rollback(name)
        if not ok:
            return _err(msg)
        adminlog("回滚 config.yaml user=%s name=%s", session.get("uid"), name)
        return _ok(message=msg, backups=confedit.list_backups())
    except (ValueError, OSError) as e:
        return _err(e)


# ---------------------------------------------------------------- AI 大模型管理

def _config_llm():
    """config.yaml 的 brain.external_llm（未纳管时的兜底配置）。"""
    try:
        return ((cfg.read_config().get("brain") or {}).get("external_llm") or {})
    except Exception:
        return {}


def _llm_overview():
    d = {"items": store.list_llm_models()}
    d.update(store.llm_models_status(_config_llm()))
    return d


@app.route("/api/llm-models")
@login_required
def api_llm_models():
    return _ok(**_llm_overview())


@app.route("/api/llm-models", methods=["POST"])
@login_required
def api_llm_models_save():
    d = _body()
    try:
        mid = store.save_llm_model(d)
        adminlog("保存大模型 user=%s id=%s name=%s", session.get("uid"),
                 mid, d.get("name"))
        return _ok(message="已保存大模型「%s」" % (d.get("name") or ""),
                   id=mid, **_llm_overview())
    except (ValueError, TypeError) as e:
        return _err(e)


@app.route("/api/llm-models", methods=["DELETE"])
@login_required
def api_llm_models_delete():
    raw = request.args.get("id")
    try:
        was_default = store.default_llm_id() == int(raw) and int(raw) > 0
        r = store.delete_llm_model(raw)
    except (ValueError, TypeError, OSError) as e:
        return _err(e)
    adminlog("删除大模型 user=%s id=%s name=%s", session.get("uid"), raw, r["name"])
    return _ok(message="已删除「%s」%s" % (
        r["name"], "（它是默认模型，已回退到 config.yaml）" if was_default else ""),
        **_llm_overview())


@app.route("/api/llm-models/default", methods=["POST"])
@login_required
def api_llm_models_default():
    try:
        mid = store.set_default_llm(_body().get("id"))
    except (ValueError, TypeError, OSError) as e:
        return _err(e)
    adminlog("设置默认大模型 user=%s id=%s", session.get("uid"), mid)
    return _ok(message=("已清除默认模型，回退到 config.yaml" if not mid
                        else "默认大模型已切换，主服务下一次生成即生效（无需重启）"),
               **_llm_overview())


@app.route("/api/llm-models/import-config", methods=["POST"])
@login_required
def api_llm_models_import_config():
    """把 config.yaml 里正在用的 external_llm 一键纳管（幂等）。"""
    c = _config_llm()
    base = str(c.get("base_url") or "").strip()
    model = str(c.get("model") or "").strip()
    if not base or not model:
        return _err("config.yaml 的 brain.external_llm 没有配置完整")
    name = ("当前配置·" + model)[:60]
    if any(x["name"] == name for x in store.list_llm_models()):
        return _err("已从当前配置导入过「%s」" % name)
    try:
        mid = store.save_llm_model({
            "name": name, "provider": "", "base_url": base,
            "api_key": c.get("api_key") or "", "model": model,
            "temperature": c.get("temperature", 0.3),
            "stream": c.get("stream", True),
            "max_tokens": c.get("max_tokens", 0),
            "connect_timeout": c.get("connect_timeout", 10),
            "read_timeout": c.get("read_timeout", 60),
            "total_timeout": c.get("total_timeout", 240),
        })
        store.set_default_llm(mid)
    except (ValueError, TypeError) as e:
        return _err(e)
    adminlog("从 config.yaml 导入大模型 user=%s name=%s", session.get("uid"), name)
    return _ok(message="已导入「%s」并设为默认" % name, **_llm_overview())


def _tail(path, max_bytes=120000):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            return proc.decode_bytes(f.read())
    except OSError:
        return ""


@app.route("/api/log")
@login_required
def api_log():
    """两个标签各取各的日志，互不混在一起。

    text       → 运行日志：主服务 webhook.log
    admin_text → 管理端日志：admin.log（后台操作）+ webhook_server.out.log（启动输出）
    """
    main = _tail(main_log_path())
    own = _tail(admin_log_path(), 20000)
    svc = _tail(proc.out_log_path(), 6000)
    admin_parts = []
    if own.strip():
        admin_parts.append("===== 管理端操作日志（admin · 尾部） =====\n" + own)
    if svc.strip():
        admin_parts.append("===== 主服务启动输出（webhook_server.out · 尾部） =====\n" + svc)
    runtime = "===== 主服务运行日志（webhook · 尾部） =====\n" + (main or "（暂无日志）")
    return _ok(text=runtime,
               admin_text="\n\n".join(admin_parts) or "（暂无管理端日志）",
               path=main_log_path(), admin_path=admin_log_path(),
               service_path=proc.out_log_path())


@app.route("/api/log/clear", methods=["POST"])
@login_required
def api_log_clear():
    """按当前标签清空：runtime / admin / all。"""
    which = str(_body().get("which") or "all").strip().lower()
    if which == "runtime":
        targets = (main_log_path(),)
    elif which == "admin":
        targets = (admin_log_path(), proc.out_log_path())
    else:
        targets = (main_log_path(), admin_log_path(), proc.out_log_path())
        which = "all"
    cleared = []
    for p in targets:
        try:
            with open(p, "w", encoding="utf-8"):
                pass
            cleared.append(os.path.basename(p))
        except OSError:
            pass
    adminlog("清空日志 which=%s user=%s files=%s", which, session.get("uid"),
             ",".join(cleared))
    if not cleared:
        return _err("没有可清空的日志文件")
    return _ok(message="已清空 " + "、".join(cleared) +
               "（truncate，不删文件，避免主服务句柄失效）")


@app.route("/api/sysinfo")
@login_required
def api_sysinfo():
    import importlib.util
    deps = {}
    for mod, pipname in (("flask", "flask"), ("requests", "requests"), ("yaml", "pyyaml"),
                         ("numpy", "numpy"), ("jieba", "jieba"),
                         ("rank_bm25", "rank-bm25"), ("torch", "torch"),
                         ("sentence_transformers", "sentence-transformers"),
                         ("PIL", "pillow"), ("pilk", "pilk"),
                         ("docx", "python-docx"), ("fitz", "pymupdf"),
                         ("openpyxl", "openpyxl")):
        deps[pipname] = bool(importlib.util.find_spec(mod))
    c = cfg.read_config()
    return _ok(platform=platform.platform(), python=platform.python_version(),
               app_version=APP_VERSION, base_dir=BASE_DIR,
               config_exists=os.path.isfile(cfg.CONFIG_PATH),
               db_path=store.DB_PATH,
               db_size=os.path.getsize(store.DB_PATH) if os.path.isfile(store.DB_PATH) else 0,
               knowledge_docs=len(store.all_kb_docs()),
               vector=(c.get("brain", {}).get("retrieval", {}) or {}).get("use_vector", True),
               deps=deps)


@app.route("/api/health")
@login_required
def api_health():
    return _ok(ok=True, db=os.path.isfile(store.DB_PATH),
               config=os.path.isfile(cfg.CONFIG_PATH), ts=time.time())


# 模块级初始化（gunicorn 以 import 方式加载，不走 __main__）；幂等，主服务也会调一次
store.init_db()


def main():
    c = cfg.read_config()
    a = c.get("admin") or {}
    app.run(host=os.environ.get("KF_ADMIN_HOST") or a.get("host") or "0.0.0.0",
            port=int(os.environ.get("KF_ADMIN_PORT") or a.get("port") or 7780),
            debug=bool(os.environ.get("KF_ADMIN_DEBUG")), use_reloader=False)


if __name__ == "__main__":
    main()
