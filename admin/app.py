"""个人微信智能客服 · Web 管理端

独立 Flask 进程（默认 7780），与主服务共用 analytics.db。
启动：python -m admin.app   /   gunicorn -c admin/gunicorn.conf.py admin.app:app

本文件只负责三件事：**应用装配、登录与安全中间件、页面路由**。
各业务域的路由按 Blueprint 拆在 admin/bp_*.py，函数实现与拆分前一致：

  bp_sys       运行监控 / 服务控制 / 日志 / 系统信息
  bp_plugins   插件清单 / 私有配置 / 定时任务 / 授权激活
  bp_contacts  通讯录 / 群成员 / 备注与群昵称
  bp_media     聊天媒体（图片 / 文件）落盘流水
  bp_kb        知识库文档 / 策略 / 群聊与私聊规则
  bp_records   对话记录 / 聊天记录 / 统计 / API 与 AI 用量
  bp_moments   朋友圈浏览与点赞评论
  bp_config    系统配置 / 对话延续 / 大模型纳管

共用能力（响应助手、登录校验、主服务转发、分页）在 admin/web.py。
"""
import os
import threading
import time
from urllib.parse import urlparse

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   session, url_for)

import store
from admin import (bp_config, bp_contacts, bp_kb, bp_media, bp_moments,
                   bp_plugins, bp_records, bp_sys, cfg)
from admin.web import (APP_VERSION, ASSET_VER, BASE_DIR, SESSION_COOKIE, adminlog,
                       body, fail, login_required, ok, secret_key)

app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, "admin", "templates"),
            static_folder=os.path.join(BASE_DIR, "admin", "static"),
            static_url_path="/static")

app.secret_key = secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("KF_ADMIN_HTTPS") == "1"),
    PERMANENT_SESSION_LIFETIME=8 * 3600,
    MAX_CONTENT_LENGTH=32 * 1024 * 1024,
)
app.config["SESSION_COOKIE_NAME"] = SESSION_COOKIE


@app.context_processor
def _inject_asset_ver():
    """所有模板统一拿到静态资源版本串，避免各模板各写一份、改样式时漏改导致旧缓存。"""
    return {"asset_ver": ASSET_VER}


# ---------------------------------------------------------------- 登录限流
_fails = {}                 # ip -> [失败时间戳]；gunicorn 多线程下需串行访问
_fails_lock = threading.Lock()
_FAIL_WINDOW = 300
_FAIL_MAX = 5
_FAIL_IPS_MAX = 5000        # 记录上限：防止伪造 X-Forwarded-For 的请求把字典撑爆


def _client_ip():
    """取客户端 IP。

    优先 X-Forwarded-For 首段（后台常挂在 Nginx 后面）；直连时用 remote_addr。
    ⚠️ 该头可被客户端伪造，所以限流只能当「降低暴力破解速度」用，
    真正的防线是强口令 + 不要把 7780 直接暴露到公网。
    """
    x = request.headers.get("X-Forwarded-For") or ""
    return (x.split(",")[0].strip() if x else request.remote_addr or "?")


def _throttled(ip):
    now = time.time()
    with _fails_lock:
        arr = [t for t in _fails.get(ip, []) if now - t < _FAIL_WINDOW]
        if arr:
            _fails[ip] = arr
        else:
            _fails.pop(ip, None)         # 窗口内无失败记录就不再占一个 key
        if len(_fails) > _FAIL_IPS_MAX:  # 兜底：超上限时整体清一遍过期项
            for k in [k for k, v in _fails.items() if not v]:
                _fails.pop(k, None)
        return len(arr) >= _FAIL_MAX


def _note_fail(ip):
    with _fails_lock:
        _fails.setdefault(ip, []).append(time.time())
        if len(_fails) > _FAIL_IPS_MAX:
            _fails.clear()               # 极端情况宁可重置计数，也不无限增长


# ---------------------------------------------------------------- 安全中间件
def _host_allowed():
    origin = request.headers.get("Origin") or ""
    if not origin:
        return True
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


# ---------------------------------------------------------------- 页面
@app.route("/")
def index():
    if not session.get("uid"):
        return redirect(url_for("login_page", next="/"))
    # 首页 HTML 不缓存：改了 DOM 结构后，浏览器若还拿旧版页面会前后端错配
    # （例如旧 div#chatBody 配上新版渲染的 <tr>，标签被丢弃、所有记录连成一行）。
    resp = app.make_response(render_template(
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
    d = body()
    changed, msg = store.change_password(session.get("uid"), d.get("old"),
                                        d.get("new"))
    if not changed:
        return fail(msg)
    session.clear()
    return ok(message=msg)


# ---------------------------------------------------------------- 装配
for _bp in (bp_sys.bp, bp_plugins.bp, bp_contacts.bp, bp_media.bp,
            bp_kb.bp, bp_records.bp, bp_moments.bp, bp_config.bp):
    app.register_blueprint(_bp)

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
