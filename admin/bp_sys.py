"""admin 蓝图：运行监控 / 服务控制 / 日志 / 系统信息。

路由实现从 admin/app.py 拆出，函数体未改动；共用工具在 admin/web.py。
"""

import os
import platform
import time

from flask import Blueprint, jsonify, request, session

import media_store
import phone_mon
import store
from admin import cfg, contacts, proc
from admin.web import (APP_VERSION, BASE_DIR, admin_log_path, adminlog, body,
                       call_main, fail, login_required, main_cfg,
                       main_log_path, ok)

bp = Blueprint("sys", __name__)


# ---------------------------------------------------------------- 运行监控

@bp.route("/api/status")
@login_required
def api_status():
    c, main, _ = main_cfg()
    out = {"main": {"reachable": False}, "plugin": {"reachable": False},
           "summary": store.summary(), "daily": store.daily_series(7)}
    try:
        code, d = call_main("/healthz", timeout=3)
        out["main"] = {"reachable": code == 200, **(d if isinstance(d, dict) else {})}
    except Exception as e:
        out["main"]["error"] = str(e)[:150]
    try:
        h = contacts.plugin_health()
        self_id = h.get("self") or ""
        out["plugin"] = {"reachable": True, "self": self_id,
                         "self_name": contacts.self_nickname(self_id),
                         "wechatReady": h.get("wechatReady"),
                         "webhook": h.get("webhook"),
                         "alertPushUrl": h.get("alertPushUrl"),
                         "version": h.get("version")}
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
    q = ("?token=" + tok) if tok else ""
    out["webhook_suggest"] = base.rstrip("/") + "/webhook" + q
    # 1.1.1-beta1：告警独立端点，与消息 webhook 同基址不同路径
    out["alert_suggest"] = base.rstrip("/") + "/alert" + q
    # 手机活跃监控（探活推算）+ 媒体存储概览
    out["phone"] = phone_mon.state()
    out["media"] = media_store.stats()
    out["main_service"] = main
    return ok(**out)


@bp.route("/api/service")
@login_required
def api_service_status():
    """主服务进程状态（启动中 / 在线 / 已停止 / 由谁启动）。"""
    return jsonify(proc.status())


@bp.route("/api/service", methods=["POST"])
@login_required
def api_service_action():
    """启动 / 停止 / 重启主服务。只停止管理端自己启动的进程。"""
    action = str(body().get("action") or "").strip().lower()
    if action not in ("start", "stop", "restart"):
        return fail("action 只能是 start / stop / restart")
    r = getattr(proc, action)()
    adminlog("服务控制 action=%s user=%s ok=%s %s", action, session.get("uid"),
             r.get("ok"), r.get("message") or r.get("error") or "")
    return jsonify(r) if r.get("ok") else (jsonify(r), 400)


@bp.route("/api/api-service", methods=["GET", "POST"])
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
        return ok(message="", degraded=True,
                   state={"available": False, "hint": hint,
                          "enabled": False, "registered": False,
                          "key_set": False, "config_enabled": False,
                          "restarts": 0})

    try:
        if request.method == "POST":
            action = str((body().get("action") or "")).strip().lower()
            code, d = call_main("/internal/api-control", method="POST",
                                 body={"action": action}, timeout=5)
            adminlog("知识库接口控制 action=%s user=%s ok=%s", action,
                     session.get("uid"), bool((d or {}).get("ok")))
        else:
            code, d = call_main("/internal/api-control", timeout=3)
        d = d if isinstance(d, dict) else {}
        if code >= 400 or not d.get("ok"):
            hint = _hint(code, d)
            return _degraded(hint) if request.method == "GET" else fail(hint)
        return ok(message=d.get("message") or "", state=d.get("state") or {})
    except Exception:
        hint = "请启动主服务"
        return _degraded(hint) if request.method == "GET" else fail(hint)

# ---------------------------------------------------------------- 下拉选项

@bp.route("/api/options")
@login_required
def api_options():
    return ok(policies=store.list_policies(), docs=store.all_kb_docs(),
               groups=store.list_groups(),
               # 群聊/私聊配置表单的「大模型」下拉：只给启用的，不带密钥
               llms=[{"id": m["id"], "name": m["name"], "model": m["model"]}
                     for m in store.list_llm_models() if m.get("enabled")])

def _tail(path, max_bytes=120000):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            return proc.decode_bytes(f.read())
    except OSError:
        return ""


@bp.route("/api/log")
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
    return ok(text=runtime,
               admin_text="\n\n".join(admin_parts) or "（暂无管理端日志）",
               path=main_log_path(), admin_path=admin_log_path(),
               service_path=proc.out_log_path())


@bp.route("/api/log/clear", methods=["POST"])
@login_required
def api_log_clear():
    """按当前标签清空：runtime / admin / all。"""
    which = str(body().get("which") or "all").strip().lower()
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
        return fail("没有可清空的日志文件")
    return ok(message="已清空 " + "、".join(cleared) +
               "（truncate，不删文件，避免主服务句柄失效）")


@bp.route("/api/sysinfo")
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
    return ok(platform=platform.platform(), python=platform.python_version(),
               app_version=APP_VERSION, base_dir=BASE_DIR,
               config_exists=os.path.isfile(cfg.CONFIG_PATH),
               db_path=store.DB_PATH,
               db_size=os.path.getsize(store.DB_PATH) if os.path.isfile(store.DB_PATH) else 0,
               knowledge_docs=len(store.all_kb_docs()),
               vector=(c.get("brain", {}).get("retrieval", {}) or {}).get("use_vector", True),
               deps=deps)


@bp.route("/api/health")
@login_required
def api_health():
    return ok(ok=True, db=os.path.isfile(store.DB_PATH),
               config=os.path.isfile(cfg.CONFIG_PATH), ts=time.time())
