"""admin 蓝图：知识库文档 / 策略 / 群聊与私聊规则。

路由实现从 admin/app.py 拆出，函数体未改动；共用工具在 admin/web.py。
"""

import io
import os

from flask import Blueprint, abort, request, send_file, session

import store
from admin import kb as kbm
from admin.web import adminlog, body, call_main, fail, login_required, ok

bp = Blueprint("kb", __name__)


# ---------------------------------------------------------------- 知识库管理

@bp.route("/api/policies", methods=["GET", "POST"])
@login_required
def api_policies():
    if request.method == "GET":
        items = store.list_policies()
        return ok(items=items, total=len(items))
    try:
        pid = store.save_policy(body())
        adminlog("保存知识库 id=%s user=%s", pid, session.get("uid"))
        return ok(message="知识库已保存", id=pid, items=store.list_policies())
    except (ValueError, OSError) as e:
        return fail(e)


@bp.route("/api/policies/<int:pid>", methods=["DELETE"])
@login_required
def api_policy_delete(pid):
    try:
        store.delete_policy(pid)
        return ok(message="知识库已删除", items=store.list_policies())
    except ValueError as e:
        return fail(e)


@bp.route("/api/policies/sync-docs", methods=["POST"])
@login_required
def api_policies_sync_docs():
    pruned = store.prune_missing_docs()
    added = store.sync_existing_docs()
    return ok(message="已同步 %d 个新文档、清理 %d 个失效引用" % (len(added), pruned),
               added=added, pruned=pruned, items=store.list_policies())


# ---------------------------------------------------------------- 群聊 / 私聊配置

@bp.route("/api/group-rules", methods=["GET", "POST"])
@login_required
def api_group_rules():
    if request.method == "GET":
        items = store.list_group_rules()
        return ok(items=items, total=len(items))
    try:
        rid = store.save_group_rule(body())
        adminlog("保存群聊配置 id=%s user=%s", rid, session.get("uid"))
        return ok(message="群聊配置已保存", id=rid, items=store.list_group_rules())
    except (ValueError, OSError) as e:
        return fail(e)


@bp.route("/api/group-rules/<int:rid>", methods=["DELETE"])
@login_required
def api_group_rule_delete(rid):
    store.delete_group_rule(rid)
    return ok(message="已删除", items=store.list_group_rules())


@bp.route("/api/private-rules", methods=["GET", "POST"])
@login_required
def api_private_rules():
    if request.method == "GET":
        items = store.list_private_rules()
        return ok(items=items, total=len(items))
    try:
        rid = store.save_private_rule(body())
        adminlog("保存私聊配置 id=%s user=%s", rid, session.get("uid"))
        return ok(message="私聊配置已保存", id=rid, items=store.list_private_rules())
    except (ValueError, OSError) as e:
        return fail(e)


@bp.route("/api/private-rules/<int:rid>", methods=["DELETE"])
@login_required
def api_private_rule_delete(rid):
    store.delete_private_rule(rid)
    return ok(message="已删除", items=store.list_private_rules())


@bp.route("/api/global-deny", methods=["GET", "POST"])
@login_required
def api_global_deny():
    if request.method == "GET":
        return ok(items=store.get_global_deny())
    store.set_global_deny(body().get("items"))
    return ok(message="全局黑名单已保存", items=store.get_global_deny())

# ---------------------------------------------------------------- 知识库

@bp.route("/api/kb/files")
@login_required
def api_kb_files():
    # 一次目录扫描，三处复用（items / index / stats），避免重复遍历导致的慢加载。
    # 文档要 stat（列表里显示大小/修改时间），图片只需要「有多少张」——
    # 不 stat 图片：Docker bind mount 上一次 stat 约 1.3ms，242 张就是 0.34 秒。
    scan = kbm._scan(with_stat=False)
    items = kbm.list_files(scan=scan)
    return ok(items=items,
               index=kbm.index_status(scan=scan, doc_count=len(items)),
               stats=kbm.kb_stats(files=items, scan=scan),
               policies=[{"id": p["id"], "name": p["name"]} for p in store.list_policies()])


@bp.route("/api/kb/file")
@login_required
def api_kb_read():
    try:
        return ok(name=request.args.get("name") or "",
                   content=kbm.read_file(request.args.get("name") or ""))
    except (ValueError, FileNotFoundError, OSError) as e:
        return fail(e)


@bp.route("/api/kb/file", methods=["POST"])
@login_required
def api_kb_write():
    d = body()
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
        return ok(message=msg, **r)
    except (ValueError, OSError) as e:
        return fail(e)


@bp.route("/api/kb/file", methods=["DELETE"])
@login_required
def api_kb_delete():
    try:
        r = kbm.delete_file(request.args.get("name") or "")
        adminlog("知识库删除 name=%s user=%s", r["name"], session.get("uid"))
        return ok(message="已删除，并已从所有知识库移除", **r)
    except (ValueError, FileNotFoundError, OSError) as e:
        return fail(e)


@bp.route("/api/kb/upload", methods=["POST"])
@login_required
def api_kb_upload():
    name, raw = "", b""
    f = request.files.get("file")
    if f:
        name, raw = f.filename or "", f.read()
    else:
        d = body()
        name, raw = d.get("name") or "", (d.get("content") or "").encode("utf-8")
    if not name:
        return fail("缺少文件名")
    if not name.lower().endswith((".md", ".txt")):
        return fail("只支持 .md / .txt（Word/Excel/PDF 请用「导入」）")
    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return fail("文件编码无法识别（请转成 UTF-8）")
    try:
        return ok(message="已上传（新文档默认加入「默认知识库」）",
                   **kbm.save_upload(name, text))
    except (ValueError, OSError) as e:
        return fail(e)


@bp.route("/api/kb/import", methods=["POST"])
@login_required
def api_kb_import():
    results = []
    files = request.files.getlist("files") or ([request.files["file"]] if request.files.get("file") else [])
    replace = body().get("replace") or request.form.get("replace")
    for f in files:
        try:
            r = kbm.import_document(f.read(), f.filename or "", replace=bool(replace))
            results.append({"name": f.filename, "ok": True, "result": r})
        except Exception as e:
            results.append({"name": f.filename, "ok": False, "error": str(e)})
    if not results:
        return fail("没有收到文件")
    return ok(message="导入完成：%d 成功 / %d 失败" %
               (sum(1 for x in results if x["ok"]), sum(1 for x in results if not x["ok"])),
               results=results)


@bp.route("/api/kb/images")
@login_required
def api_kb_images():
    return ok(items=kbm.list_images())


@bp.route("/api/kb/image", methods=["POST"])
@login_required
def api_kb_image_upload():
    f = request.files.get("file")
    if not f:
        return fail("缺少文件")
    try:
        return ok(**kbm.save_image(f.read(), f.filename or ""))
    except (ValueError, OSError) as e:
        return fail(e)


@bp.route("/api/kb/image", methods=["DELETE"])
@login_required
def api_kb_image_delete():
    try:
        return ok(**kbm.delete_image(request.args.get("name") or ""))
    except (ValueError, FileNotFoundError, OSError) as e:
        return fail(e)


@bp.route("/api/kb/image/raw")
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


@bp.route("/api/kb/rebuild", methods=["POST"])
@login_required
def api_kb_rebuild():
    r = kbm.rebuild_index()
    try:
        code, d = call_main("/internal/reload", "POST", {}, timeout=10)
        return ok(message="索引缓存已清空，主服务正在后台重建",
                   removed=r.get("removed") or [], reload=d)
    except Exception as e:
        return ok(message="索引缓存已清空，但主服务未在线，需手动重启主服务：%s" % e,
                   removed=r.get("removed") or [], reload={"ok": False})


@bp.route("/api/kb/rebuild/status")
@login_required
def api_kb_rebuild_status():
    """查询索引重建进度（前端轮询）。主服务离线时返回 last 状态而不是报错。"""
    try:
        code, d = call_main("/internal/reload/status", timeout=5)
        if code == 200 and isinstance(d, dict):
            return ok(reload=d.get("reload") or {})
        return ok(reload={}, reachable=False,
                   message="主服务未响应，请检查主服务是否在线")
    except Exception as e:
        return ok(reload={}, reachable=False, message=str(e)[:150])


@bp.route("/api/kb/ask", methods=["POST"])
@login_required
def api_kb_ask():
    d = body()
    try:
        r = kbm.ask(d.get("question"), policy=d.get("policy") or None)
        # 问答测试也会真实调用大模型，同样记进「AI 用量」（source=kb_test）。
        # llm_id 来自子进程里实际生效的纳管模型，不是 config.yaml 里的旧值。
        if r.get("usage"):
            store.log_llm_usage(r["usage"], model=r.get("model"), source="kb_test",
                                llm_id=r.get("llm_id"))
        return ok(**r)
    except (ValueError, OSError) as e:
        return fail(e)
