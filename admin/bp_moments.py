"""admin 蓝图：朋友圈只读浏览与点赞评论。

路由实现从 admin/app.py 拆出，函数体未改动；共用工具在 admin/web.py。
"""

from flask import Blueprint, make_response, request, session

from admin import moments
from admin.web import adminlog, body, fail, login_required, ok

bp = Blueprint("moments", __name__)


# ---------------------------------------------------------------- 朋友圈（v3）
# 全部只读 / 写操作都经 admin/moments.py 代理手机插件：token 不出后端，媒体不直连 CDN。
@bp.route("/api/moments/feed")
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
        return ok(**d)
    except ValueError as e:
        return fail(str(e))
    except Exception as e:
        return fail("读取朋友圈失败（检查手机插件是否在线）：%s" % e)


@bp.route("/api/moments/friends")
@login_required
def api_moments_friends():
    """「按好友」下拉：本地通讯录缓存里的好友，也允许前端手动输入 wxid。"""
    return ok(items=moments.friend_options())


def _media_response(data, mime):
    resp = make_response(data)
    resp.headers["Content-Type"] = mime or "application/octet-stream"
    resp.headers["Cache-Control"] = "private, max-age=3600"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@bp.route("/api/moments/media")
@login_required
def api_moments_media():
    """图片 / 视频二进制代理。浏览器带 Referer 直连微信 CDN 只会拿到占位图。"""
    try:
        data, mime = moments.media(request.args.get("tid"),
                                   request.args.get("index") or 0)
    except ValueError as e:
        return fail(str(e))
    except Exception as e:
        return fail("媒体读取失败（动态可能已被微信清理，请刷新）：%s" % e)
    return _media_response(data, mime)


@bp.route("/api/moments/cover")
@login_required
def api_moments_cover():
    """视频封面：后端不带 Referer 取缩略图，失败再用 ffmpeg 抽视频首帧。

    不再把 CDN 直链透给浏览器（白名单外的域名 / 直链过期会导致封面空白）。
    """
    try:
        data, mime = moments.cover(request.args.get("tid"),
                                   request.args.get("index") or 0)
    except ValueError as e:
        return fail(str(e))
    except Exception as e:
        return fail("封面读取失败：%s" % e)
    return _media_response(data, mime)


@bp.route("/api/moments/avatar")
@login_required
def api_moments_avatar():
    """朋友圈发布者头像：按 username 查通讯录缓存，后端取回 CDN 二进制转 blob:。

    和媒体 / 封面一样不带 Referer、不下发直链；查不到时前端隐藏图片回退首字母。
    """
    try:
        data, mime = moments.avatar(request.args.get("user") or "")
    except ValueError as e:
        return fail(str(e))
    except Exception as e:
        return fail("头像读取失败：%s" % e)
    return _media_response(data, mime)


@bp.route("/api/moments/video")
@login_required
def api_moments_video():
    """H.264 转码版视频：浏览器解不出 HEVC 画面时前端才来取。"""
    try:
        data, mime = moments.video_h264(request.args.get("tid"),
                                        request.args.get("index") or 0)
    except ValueError as e:
        return fail(str(e))
    except Exception as e:
        return fail("视频转换失败：%s" % e)
    return _media_response(data, mime)


@bp.route("/api/moments/like", methods=["POST"])
@login_required
def api_moments_like():
    """点赞 / 取消赞：真实社交动作，前端已二次确认，后端仍先 dryRun 预检。"""
    b = body()
    dry = bool(b.get("dryRun"))
    try:
        d = moments.like(b.get("tid"), unlike=bool(b.get("unlike")),
                         dry_run=dry)
    except (ValueError, RuntimeError) as e:
        return fail(str(e))
    except Exception as e:
        return fail("点赞失败：%s" % e)
    if not dry:
        adminlog("朋友圈%s tid=%s user=%s changed=%s",
                 "取消赞" if b.get("unlike") else "点赞", b.get("tid"),
                 session.get("uid"), d.get("changed"))
    payload = {k: v for k, v in dict(d).items() if k != "ok"}
    payload.setdefault("message", "已取消赞" if b.get("unlike") else "已点赞")
    return ok(**payload)


@bp.route("/api/moments/comment", methods=["POST"])
@login_required
def api_moments_comment():
    """发表评论：真实社交动作，前端二次确认 + 后端 dryRun 预检。"""
    b = body()
    dry = bool(b.get("dryRun"))
    try:
        d = moments.comment(b.get("tid"), b.get("content"), dry_run=dry)
    except (ValueError, RuntimeError) as e:
        return fail(str(e))
    except Exception as e:
        return fail("评论失败：%s" % e)
    if not dry:
        adminlog("朋友圈评论 tid=%s user=%s changed=%s",
                 b.get("tid"), session.get("uid"), d.get("changed"))
    payload = {k: v for k, v in dict(d).items() if k != "ok"}
    # confirmed=false 表示微信侧 CGI 回调没确认，可能只改了本地缓存：如实告知，
    # 不能让用户以为评论已经发到对方那里。
    payload.setdefault("message", "评论已发表" if d.get("confirmed", True)
                       else "评论已提交，但微信未确认（可能只改了本地缓存）")
    return ok(**payload)


@bp.route("/api/moments/rm_comment", methods=["POST"])
@login_required
def api_moments_rm_comment():
    """删除本人评论（插件 8.11）：按 commentId 精确删除，前端二次确认 + dryRun 预检。

    插件不允许「不传定位参数」的模糊删除走本后台；删除不可撤回，记入管理端日志。
    """
    b = body()
    dry = bool(b.get("dryRun"))
    try:
        d = moments.rm_comment(b.get("tid"), b.get("commentId"),
                               b.get("content"), dry_run=dry)
    except (ValueError, RuntimeError) as e:
        return fail(str(e))
    except Exception as e:
        return fail("删除评论失败：%s" % e)
    if not dry:
        adminlog("朋友圈删除评论 tid=%s commentId=%s user=%s changed=%s",
                 b.get("tid"), b.get("commentId") or "-",
                 session.get("uid"), d.get("changed"))
    payload = {k: v for k, v in dict(d).items() if k != "ok"}
    payload.setdefault("message", "评论已删除")
    return ok(**payload)
