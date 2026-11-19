"""admin 蓝图：插件管理 / 插件配置 / 定时任务 / 授权。

路由实现从 admin/app.py 拆出，函数体未改动；共用工具在 admin/web.py。
"""

import os
import re
import shutil

import yaml
from flask import Blueprint, request, session

from admin import cfg, contacts
from admin.web import (BASE_DIR, adminlog, body, call_main, fail, login_required,
                       ok)

bp = Blueprint("plugins", __name__)


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


@bp.route("/api/plugins", methods=["GET", "POST"])
@login_required
def api_plugins():
    """插件清单（GET）/ 启停与优先级（POST）。

    主服务未启动或还是旧版本时：GET 仍返回 200 但带 degraded + hint（页面给提示，
    不弹一屏 HTML）；POST 返回 400 + 一句人话。
    """
    is_get = request.method == "GET"
    try:
        if is_get:
            code, d = call_main("/internal/plugins", timeout=3)
        else:
            b = body()
            name = str(b.get("name") or "").strip()
            if not _PLUGIN_NAME_RE.match(name):
                return fail("插件名不合法（只允许字母、数字、下划线、短横线）")
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
                        return fail("优先级必须是整数或留空")
            if len(payload) == 1:
                return fail("没有要修改的字段")
            code, d = call_main("/internal/plugins/state", method="POST",
                                 body=payload, timeout=8)
            adminlog("插件状态 name=%s payload=%s user=%s ok=%s", name,
                     {k: v for k, v in payload.items() if k != "name"},
                     session.get("uid"), bool((d or {}).get("ok")))
        d = d if isinstance(d, dict) else {}
        if code >= 400 or not d.get("ok"):
            hint = _plugin_hint(code, d)
            return (ok(degraded=True, hint=hint, plugins=dict(_PLUGIN_EMPTY))
                    if is_get else fail(hint))
        p = _plugin_view(d)
        return ok(plugins=p, message=d.get("message") or "",
                   warning=p.get("reload_error") or "")
    except Exception as e:
        hint = "无法连接主服务：%s" % e
        return (ok(degraded=True, hint=hint, plugins=dict(_PLUGIN_EMPTY))
                if is_get else fail("请启动主服务后再操作"))


@bp.route("/api/plugins/reload", methods=["POST"])
@login_required
def api_plugins_reload():
    """重新扫描插件目录并原子替换注册表；整体失败时主服务会保留旧表。"""
    try:
        code, d = call_main("/internal/plugins/reload", method="POST",
                             body={}, timeout=10)
        d = d if isinstance(d, dict) else {}
        if code >= 400 or not d.get("ok"):
            return fail(_plugin_hint(code, d))
        p = _plugin_view(d)
        adminlog("插件重载 user=%s 插件=%s 处理器=%s warning=%s",
                 session.get("uid"), p.get("count"), p.get("handlers"),
                 p.get("reload_error") or "")
        return ok(plugins=p, warning=p.get("reload_error") or "",
                   message="已重新扫描并重载插件注册表")
    except Exception as e:
        return fail("请启动主服务后再重载：%s" % e)


@bp.route("/api/plugins/config", methods=["GET", "POST"])
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
        body = body()
        name = str(body.get("name") or "").strip()
    if not _PLUGIN_NAME_RE.match(name):
        return fail("插件名不合法")
    try:
        code, d = call_main("/internal/plugins", timeout=3)
        names = [str(x.get("name") or "")
                 for x in (_plugin_view(d).get("plugins") or [])]
        if code >= 400 or not (d or {}).get("ok") or name not in names:
            return fail("插件不在主服务清单中，或主服务未启动")
    except Exception:
        return fail("请先启动主服务，再编辑插件配置")
    path = _plugin_cfg_path(name)
    if request.method == "GET":
        try:
            text = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
        except Exception as e:
            return fail("读取失败：%s" % e)
        return ok(name=name, path=os.path.dirname(path), text=text,
                   exists=os.path.isfile(path))
    text = str(body.get("text") or "")
    if len(text.encode("utf-8")) > 32768:
        return fail("配置不能超过 32KB")
    try:
        parsed = yaml.safe_load(text)
        if parsed is not None and not isinstance(parsed, dict):
            return fail("插件私有配置必须是 YAML 键值对（顶层为对象）")
    except Exception as e:
        return fail("YAML 格式错误：%s" % e)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path):
            shutil.copy2(path, path + ".bak")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception as e:
        return fail("保存失败：%s" % e)
    adminlog("插件配置保存 name=%s user=%s bytes=%d", name,
             session.get("uid"), len(text))
    return ok(name=name, message="已保存，下一条消息起按 mtime 热读生效")


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
        code, d = call_main("/internal/plugins", timeout=3)
        if code >= 400 or not (d or {}).get("ok"):
            return []
        return [str(x.get("key") or "")
                for x in (((d.get("plugins") or {}).get("schedules")) or [])]
    except Exception:
        return []


@bp.route("/api/plugins/tasks", methods=["GET", "POST"])
@login_required
def api_plugin_tasks():
    """定时任务实例的读取与增删改。

    GET  返回原始 YAML 文本、静态声明清单、主服务合并后的任务视图。
    POST body: {action: save|delete|restore|toggle, key, from?, label?,
                enabled?, kind?, seconds?, hour?, minute?}
    """
    path = _plugin_tasks_path()
    payload = body() if request.method == "POST" else {}
    if request.method == "GET":
        try:
            text = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
        except Exception as e:
            return fail("读取失败：%s" % e)
        try:
            code, d = call_main("/internal/plugins", timeout=3)
            tm = (_plugin_view(d or {}).get("task_manager") or {})
        except Exception:
            tm = {}
        return ok(text=text, path=os.path.dirname(path),
                   declarations=_task_declarations(),
                   items=tm.get("items") or [], error=tm.get("error") or "")

    action = str(payload.get("action") or "").strip()
    key = str(payload.get("key") or "").strip()
    if not _TASK_KEY_RE.match(key):
        return fail("任务标识不合法")
    decls = _task_declarations()
    if not decls:
        return fail("当前没有可管理的定时任务：请启动主服务，并至少启用一个带 @schedule 声明的插件")
    custom = "#" in key
    if not custom and key not in decls:
        return fail("任务不在插件声明清单中：%s" % key)
    try:
        data = _read_tasks_file(path)
        if action == "save":
            spec = _task_spec(body)
            if spec is None:
                return fail("周期不合法（interval 秒数 1~%d，daily 时分 0~23 / 0~59）"
                            % _TASK_MAX_SECONDS)
            label = str(body.get("label") or "").strip().replace("\n", " ")[:64]
            enabled = body.get("enabled", True) is not False
            if custom:
                src = str(body.get("from") or "").strip()
                if not _TASK_KEY_RE.match(src) or "#" in src or src not in decls:
                    return fail("新实例必须复制自一个已声明的任务")
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
                return fail("自定义实例删除后请用「新增」重新创建")
            data["overrides"].pop(key, None)
            msg = "已恢复为插件代码声明的周期"
        elif action == "toggle":
            enabled = body.get("enabled", True) is not False
            if custom:
                hit = [x for x in data["instances"] if str(x.get("key") or "") == key]
                if not hit:
                    return fail("自定义实例不存在")
                hit[0]["enabled"] = enabled
            else:
                ov = data["overrides"].get(key) or {}
                ov["enabled"] = enabled
                data["overrides"][key] = ov
            msg = "已" + ("启用" if enabled else "停用") + "任务"
        else:
            return fail("未知操作")
        if len(data["overrides"]) + len(data["instances"]) > 100:
            return fail("任务条目超过 100 条上限")
        _write_tasks_file(path, data)
    except ValueError as e:
        return fail(str(e))
    except Exception as e:
        return fail("保存失败：%s" % e)
    adminlog("插件任务 action=%s key=%s user=%s", action, key, session.get("uid"))
    try:
        _, d = call_main("/internal/plugins", timeout=3)
        p = _plugin_view(d or {})
    except Exception:
        p = dict(_PLUGIN_EMPTY)
    return ok(plugins=p, message=msg)

@bp.route("/api/plugin/license")
@login_required
def api_plugin_license():
    """运行监控：主动刷新手机插件授权状态（GET /api/license）。"""
    try:
        return ok(license=contacts.plugin_license(refresh=True))
    except Exception as e:
        return fail("读取授权状态失败（手机在线吗？）：%s" % e)


@bp.route("/api/plugin/activate", methods=["POST"])
@login_required
def api_plugin_activate():
    """运行监控：提交手机插件激活码（POST /api/activate）。

    ⚠️ 插件协议里空码 / 省略 code 表示「清除激活码、回到试用」，是危险操作。
    本后台只提供「激活」，空码（含纯空格 / 换行 / 连字符的等价空码）与
    非字符串一律 400 拒绝，绝不转发给插件；wx_client.activate() 还会再挡一层。
    """
    raw = body().get("code")
    if not isinstance(raw, str):
        return fail("激活码必须是字符串")
    code = raw.strip()
    if not re.sub(r"[\s\-]+", "", code):
        return fail("激活码不能为空（空码会清除当前授权，已拒绝提交）")
    try:
        contacts.activate(code)
    except Exception as e:
        adminlog("插件激活失败 user=%s err=%s", session.get("uid"), str(e)[:150])
        return fail("激活失败：%s" % e)
    adminlog("插件激活成功 user=%s", session.get("uid"))
    try:
        lic = contacts.plugin_license(refresh=True)
    except Exception:
        lic = {}
    return ok(message="激活成功，授权状态已更新", license=lic)
