"""admin 蓝图：系统配置 / 对话延续 / 大模型纳管。

路由实现从 admin/app.py 拆出，函数体未改动；共用工具在 admin/web.py。
"""

import yaml
from flask import Blueprint, jsonify, request, session

import followup
import store
from admin import cfg, confedit
from admin.web import adminlog, body, fail, login_required, ok

bp = Blueprint("config", __name__)


# ---------------------------------------------------------------- 配置 / 日志 / 系统

@bp.route("/api/followup")
@login_required
def api_followup_get():
    """群聊对话延续：当前生效配置（含来源、活跃窗口数）。"""
    try:
        return ok(**followup.config())
    except Exception as e:
        return fail(e)


@bp.route("/api/followup", methods=["POST"])
@login_required
def api_followup_save():
    """开关 / 分钟数 / 续聊提示**文本级写回 config.yaml**，主服务下一条消息立即生效。

    时长统一用分钟：`window_minutes`（1~30）既用于接口，也原样写进文件，
    不再接收或换算旧版秒字段。写回会先校验、备份、原子替换，中文注释
    原样保留。作用范围固定同一群友，scope 参数一律忽略。
    """
    d = body()
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
            return fail(msg)
        cur = followup.reload()
        adminlog("保存群聊对话延续到 config.yaml enabled=%s 分钟数=%s 提示=%s 备份=%s user=%s",
                 cur["enabled"], cur["window_minutes"], bool(cur["hint_preview"]),
                 (detail or {}).get("backup"), session.get("uid"))
        return ok(message=msg, **cur)
    except Exception as e:
        return fail(e)


@bp.route("/api/config")
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
        return fail(e)


@bp.route("/api/config", methods=["POST"])
@login_required
def api_config_save():
    d = body()
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
        return fail(e)


@bp.route("/api/config/validate", methods=["POST"])
@login_required
def api_config_validate():
    text = body().get("text") or ""
    ok, err = confedit.validate(text)
    try:
        warns = confedit._semantic_warn(yaml.safe_load(text) or {})
    except Exception:
        warns = []
    return ok(valid=ok, error=err, warns=warns)


@bp.route("/api/config/backup/<name>")
@login_required
def api_config_backup(name):
    try:
        return ok(text=confedit.read_backup(name))
    except (ValueError, OSError) as e:
        return fail(e)


@bp.route("/api/config/backup/<name>", methods=["DELETE"])
@login_required
def api_config_backup_delete(name):
    """手工删除一份历史备份（列表每行一个删除按钮）。"""
    try:
        return ok(message="已删除备份「%s」" % name, **confedit.delete_backup(name))
    except (ValueError, OSError) as e:
        return fail(e)


@bp.route("/api/config/rollback", methods=["POST"])
@login_required
def api_config_rollback():
    name = body().get("name") or ""
    try:
        # rollback() 同样返回 (ok, message) 二元组，不能 ** 展开
        ok, msg = confedit.rollback(name)
        if not ok:
            return fail(msg)
        adminlog("回滚 config.yaml user=%s name=%s", session.get("uid"), name)
        return ok(message=msg, backups=confedit.list_backups())
    except (ValueError, OSError) as e:
        return fail(e)


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


@bp.route("/api/llm-models")
@login_required
def api_llm_models():
    return ok(**_llm_overview())


@bp.route("/api/llm-models", methods=["POST"])
@login_required
def api_llm_models_save():
    d = body()
    try:
        mid = store.save_llm_model(d)
        adminlog("保存大模型 user=%s id=%s name=%s", session.get("uid"),
                 mid, d.get("name"))
        return ok(message="已保存大模型「%s」" % (d.get("name") or ""),
                   id=mid, **_llm_overview())
    except (ValueError, TypeError) as e:
        return fail(e)


@bp.route("/api/llm-models", methods=["DELETE"])
@login_required
def api_llm_models_delete():
    raw = request.args.get("id")
    try:
        was_default = store.default_llm_id() == int(raw) and int(raw) > 0
        r = store.delete_llm_model(raw)
    except (ValueError, TypeError, OSError) as e:
        return fail(e)
    adminlog("删除大模型 user=%s id=%s name=%s", session.get("uid"), raw, r["name"])
    return ok(message="已删除「%s」%s" % (
        r["name"], "（它是默认模型，已回退到 config.yaml）" if was_default else ""),
        **_llm_overview())


@bp.route("/api/llm-models/default", methods=["POST"])
@login_required
def api_llm_models_default():
    try:
        mid = store.set_default_llm(body().get("id"))
    except (ValueError, TypeError, OSError) as e:
        return fail(e)
    adminlog("设置默认大模型 user=%s id=%s", session.get("uid"), mid)
    return ok(message=("已清除默认模型，回退到 config.yaml" if not mid
                        else "默认大模型已切换，主服务下一次生成即生效（无需重启）"),
               **_llm_overview())


@bp.route("/api/llm-models/import-config", methods=["POST"])
@login_required
def api_llm_models_import_config():
    """把 config.yaml 里正在用的 external_llm 一键纳管（幂等）。"""
    c = _config_llm()
    base = str(c.get("base_url") or "").strip()
    model = str(c.get("model") or "").strip()
    if not base or not model:
        return fail("config.yaml 的 brain.external_llm 没有配置完整")
    name = ("当前配置·" + model)[:60]
    if any(x["name"] == name for x in store.list_llm_models()):
        return fail("已从当前配置导入过「%s」" % name)
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
        return fail(e)
    adminlog("从 config.yaml 导入大模型 user=%s name=%s", session.get("uid"), name)
    return ok(message="已导入「%s」并设为默认" % name, **_llm_overview())
