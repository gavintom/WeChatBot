"""admin 蓝图：系统配置 / 对话延续 / 大模型纳管。

路由实现从 admin/app.py 拆出，函数体未改动；共用工具在 admin/web.py。
"""

import json
import time
from urllib.parse import quote

import yaml
from flask import Blueprint, current_app, jsonify, request, session

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


@bp.route("/api/llm-models/scene", methods=["POST"])
@login_required
def api_llm_models_scene():
    """分场景（群聊 / 私聊）默认模型：{group: id, private: id}，0/空 = 跟随全局默认。

    一次保存两个场景：页面只有一个「保存」按钮，分两次写会让群聊/私聊配置
    在中间状态下短暂对不上，出问题也不好复现。
    """
    d = body()
    try:
        saved = store.set_scene_llms(d)
    except (ValueError, TypeError, OSError) as e:
        return fail(e)
    names = []
    for s, mid in saved.items():
        if not mid:
            names.append("%s=跟随全局默认" % store.LLM_SCENE_NAMES[s])
            continue
        row = store.get_llm_model(mid)
        names.append("%s=%s(id=%s)" % (store.LLM_SCENE_NAMES[s],
                                       (row or {}).get("name") or "-", mid))
    adminlog("设置分场景大模型 user=%s %s", session.get("uid"), " ".join(names))
    return ok(message="已保存分场景模型：%s；下一条消息即生效" % "，".join(names),
              **_llm_overview())


@bp.route("/api/llm-models/export")
@login_required
def api_llm_models_export():
    """下载模型配置 JSON（含默认模型 / 群聊私聊场景引用，引用用名称表达）。

    `?secrets=0` 导出不带 api_key 的版本，适合「备份 / 给同事看配置」这种
    不需要交出密钥的场合；默认带明文密钥，否则导到新机器上不能用。
    """
    with_secrets = request.args.get("secrets") not in ("0", "false", "no")
    d = store.export_llm_models(with_secrets=with_secrets)
    data = json.dumps(d, ensure_ascii=False, indent=2) + "\n"
    adminlog("导出大模型配置 user=%s 模型数=%s 含密钥=%s", session.get("uid"),
             len(d["models"]), with_secrets)
    name = "llm-models-%s%s.json" % (time.strftime("%Y%m%d-%H%M%S"),
                                     "" if with_secrets else "-no-secrets")
    return current_app.response_class(
        data, mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition":
                 "attachment; filename*=UTF-8''%s" % quote(name)})


@bp.route("/api/llm-models/import", methods=["POST"])
@login_required
def api_llm_models_import():
    """导入模型配置：{data: <导出文件内容>, mode: merge|replace}。

    也接受 {text: "<json 文本>"}，方便直接粘贴；data 传字符串时按 JSON 解析。
    """
    d = body()
    payload = d.get("data")
    if payload is None:
        payload = d.get("text")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return fail("内容不是合法的 JSON")
    if payload is None:
        return fail("没有收到要导入的内容")
    try:
        r = store.import_llm_models(payload, mode=d.get("mode") or "merge")
    except (ValueError, TypeError) as e:
        return fail(e)
    msg = "导入完成：新增 %d 个、更新 %d 个" % (r["added"], r["updated"])
    if r["removed"]:
        msg += "、删除 %d 个（%s）" % (len(r["removed"]), "、".join(r["removed"]))
    if r["missing"]:
        msg += "；有 %d 处引用无效已忽略" % len(r["missing"])
    adminlog("导入大模型配置 user=%s 方式=%s 新增=%s 更新=%s 删除=%s 缺失=%s",
             session.get("uid"), r["mode"], r["added"], r["updated"],
             len(r["removed"]), "; ".join(r["missing"]))
    return ok(message=msg, result=r, **_llm_overview())


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
            "max_input_tokens": c.get("max_input_tokens", 0),
            "support_image": c.get("support_image", False),
            "support_file": c.get("support_file", False),
            "connect_timeout": c.get("connect_timeout", 10),
            "read_timeout": c.get("read_timeout", 60),
            "total_timeout": c.get("total_timeout", 240),
        })
        store.set_default_llm(mid)
    except (ValueError, TypeError) as e:
        return fail(e)
    adminlog("从 config.yaml 导入大模型 user=%s name=%s", session.get("uid"), name)
    return ok(message="已导入「%s」并设为默认" % name, **_llm_overview())
