"""admin 蓝图：聊天媒体（图片 / 文件）落盘流水。

路由实现从 admin/app.py 拆出，函数体未改动；共用工具在 admin/web.py。
"""

import os

from flask import Blueprint, abort, request, send_file

import media_store
from admin.web import login_required, ok

bp = Blueprint("media", __name__)


@bp.route("/api/media/recent")
@login_required
def api_media_recent():
    """分页查询图片与文件流水（1.1.1-beta2：分页 + 时间参数）。

    query: chat / chat_name / wxid / minutes（默认 30；传 0 或 all=1 = 全部）/
           kind(image|file) / chat_type(group|private) / page（默认 1）/
           page_size（默认 50，最大 500）
    """
    q = request.args
    try:
        minutes = int(q.get("minutes", 30))
    except (TypeError, ValueError):
        minutes = 30
    all_time = minutes <= 0 or str(q.get("all") or "") in ("1", "true", "yes")
    try:
        page = int(q.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(q.get("page_size", 50))
    except (TypeError, ValueError):
        page_size = 50
    d = media_store.query_page(
        chat=q.get("chat") or None,
        chat_name=q.get("chat_name") or None,
        wxid=q.get("wxid") or None,
        minutes=minutes,
        kind=q.get("kind") or None,
        chat_type=q.get("chat_type") or None,
        page=page, page_size=page_size,
        all_time=all_time)
    return ok(items=d["items"], total=d["total"], page=d["page"],
               page_size=d["page_size"], pages=d["pages"], all_time=d["all_time"],
               expired=d.get("expired", 0), stats=media_store.stats())


@bp.route("/api/media/file")
@login_required
def api_media_file():
    """预览/下载一张已落盘的图片或文件（1.1.1-beta1）。

    只允许访问 media_store 配置的 images_dir / files_dir 下的文件，
    防止路径穿越读取任意文件。
    """
    p = str(request.args.get("path") or "").strip()
    if not p:
        abort(404)
    ap = os.path.realpath(p)
    roots = [os.path.realpath(media_store.CFG["images_dir"]),
             os.path.realpath(media_store.CFG["files_dir"])]
    if not any(ap == r or ap.startswith(r + os.sep) for r in roots):
        abort(404)
    if not os.path.isfile(ap):
        abort(404)
    return send_file(ap)
