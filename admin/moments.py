"""朋友圈管理：代理手机插件 v1.2.7 第 8 章的 v3 无状态接口。

插件侧不保存任何动态数据，游标由微信自己维护，调用方自行累积 `moments[]`：

  GET /api/moments/{timeline,user}/{gethead,getnext}
  GET /api/moments/media?tid=&index=
  POST /api/moments/like / unlike / comment / rm_comment

设计约束：
* **媒体一律走本后台代理**，不把 `images[]` / `thumbUrl` 的 CDN 直链交给浏览器 ——
  浏览器会自动带 Referer，微信 CDN 只回 120×120 占位图；token 也不能出现在前端。
  前端拿到二进制后转 `blob:`，所以 DOM 与地址栏都不会出现媒体地址。
  视频封面走 `/api/moments/cover`（后端不带 Referer 取缩略图，失败时用视频首帧兜底）。
* 插件的 head/next 游标是**微信全局**的，多个并发翻页会互相踩，所以本模块用
  一把锁把读操作串行化。
* 点赞 / 评论 / 删除本人评论是**真实社交动作**（会通知对方）：真实执行前先自动
  做一次 `dryRun=1` 预检，预检失败绝不发起真实动作；前端另有一次二次确认。
  删除只针对 `comments[]` 里 wxid == self 且带 `commentId` 的条目。
* 微信朋友圈视频可能是 HEVC，桌面浏览器只有声音没有画面。
  `/api/moments/video` 用镜像内置的 ffmpeg 按需转成 H.264/AAC（带 LRU 缓存），
  前端检测到 `videoWidth === 0` 才请求，正常 H.264 视频零额外开销。
* 本模块不落库、不缓存动态内容（与插件保持一致），刷新/翻页由前端累积；
  只有封面 URL 与转码结果做有上限的进程内缓存。
"""
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections import OrderedDict
from urllib.parse import urlparse

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import store                      # noqa: E402  顶层模块（项目根）
from admin import contacts        # noqa: E402

_TID_RE = re.compile(r"^[A-Za-z0-9_@.:-]{1,80}$")
_USER_RE = re.compile(r"^[A-Za-z0-9_@.:-]{1,80}$")
_CID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,80}$")
_MAX_INDEX = 50
_MAX_MEDIA = 12
_MAX_ITEMS = 500
_MAX_VIDEO_BYTES = 80 * 1024 * 1024       # 超过就不做转码 / 抽帧，避免拖垮后台

# head/next 共用微信侧全局游标；写操作之间也要避免与翻页互相干扰
_lock = threading.RLock()
_cache_lock = threading.Lock()

# 进程内、有上限的缓存：feed 时记下封面直链，cover 请求时后端自己去取。
# 前端永远只看到 blob:，直链既不出 DOM 也不出地址栏。
_COVER_CACHE = OrderedDict()               # (tid, str(index)) -> 封面直链
_COVER_CACHE_MAX = 1200
_COVER_BYTES = OrderedDict()               # (tid, str(index)) -> (bytes, mime)
_COVER_BYTES_MAX = 240
# 发布者头像：feed 时按 username 记下 CDN 直链（绝不下发），avatar 接口后端取回二进制
_AVATAR_URLS = OrderedDict()               # username -> CDN 直链
_AVATAR_BYTES = OrderedDict()              # username -> (bytes, mime)
_AVATAR_MAX = 800
_H264_CACHE = OrderedDict()                # (tid, str(index)) -> 转码后的 mp4 bytes
_H264_CACHE_BYTES = 0
_H264_CACHE_MAX_BYTES = 180 * 1024 * 1024

# 只把展示需要的字段透给前端，避免把插件内部字段 / 超大 media 结构带出去
_KEEP = ("tid", "username", "nickname", "contentDesc", "createtime", "type", "flag",
         "likeFlag", "likeCount", "likeUserCount", "commentCount", "commentUserCount",
         "likes", "comments", "mediaCount", "media", "contentTitle", "contentDescr",
         "shareUrl", "sourceUrl")


# 封面直链由**后端**去取（不带 Referer），所以仍需白名单挡住 SSRF / tracker。
# 覆盖微信朋友圈与头像 CDN 的常见域名；域名变了也不会白屏，cover() 会用视频首帧兜底。
_COVER_HOSTS = (".qpic.cn", ".qlogo.cn", ".qq.com", ".weixin.qq.com",
                ".mmcdn.cn", ".wechat.com", ".szmms.cn", ".weixin.com",
                ".tencent.com", ".myqcloud.com")


def _cover_url(x):
    """视频封面直链（8.12）：thumbUrl > attachThumbUrl > previewUrls[0]。

    只给后端 `cover()` 用，绝不透给前端；`hasCover` 才是给浏览器的信号。
    """
    v = x.get("thumbUrl") or x.get("attachThumbUrl") or ""
    if not v:
        pre = x.get("previewUrls") or []
        v = pre[0] if isinstance(pre, list) and pre else ""
    s = str(v or "").strip()
    if not s.startswith(("http://", "https://")):
        return ""
    if any(ch in s for ch in " \t\r\n\"'<>`"):
        return ""
    host = urlparse(s).netloc.lower().split(":")[0]
    if not host or host in ("localhost", "127.0.0.1", "::1"):
        return ""
    return s if any(host == h.lstrip(".") or host.endswith(h) for h in _COVER_HOSTS) else ""


def _cache_put(cache, key, value, limit):
    with _cache_lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)


def _ffmpeg():
    """镜像内 imageio-ffmpeg 自带的静态 ffmpeg（requirements.docker.txt 已装）。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise RuntimeError("当前环境没有 ffmpeg，无法转换视频：%s" % e)


def _wx():
    """复用通讯录模块里按配置热更新的 WXClient（同一个手机插件地址 / token）。"""
    return contacts.client()


def _self_wxid():
    """当前登录微信号，供前端从 likes[] 里认出「本人」那条。

    self_wxid() 内部有缓存；插件离线时取不到就返回空串，绝不让整页 feed 失败。
    """
    try:
        return str(_wx().self_wxid(timeout=3) or "").strip()
    except Exception:
        return ""


def _tid(tid):
    s = str(tid or "").strip()
    if not _TID_RE.match(s):
        raise ValueError("动态 ID 不合法")
    return s


def _user(user):
    s = str(user or "").strip()
    if not _USER_RE.match(s):
        raise ValueError("好友 username / wxid 不合法")
    return s


def _index(index):
    try:
        n = int(index)
    except (TypeError, ValueError):
        raise ValueError("媒体下标必须是整数")
    if not 0 <= n <= _MAX_INDEX:
        raise ValueError("媒体下标越界")
    return n


def _public(items):
    out = []
    for m in (items or [])[:_MAX_ITEMS]:
        if not isinstance(m, dict) or not m.get("tid"):
            continue
        d = {k: m[k] for k in _KEEP if k in m}
        media = []
        for x in (d.get("media") or [])[:_MAX_MEDIA]:
            if not isinstance(x, dict):
                continue
            is_video = (int(x.get("type") or 0) == 5
                        or int(x.get("videoDuration") or 0) > 0)
            cover = _cover_url(x) if is_video else ""
            idx = x.get("index", len(media))
            if cover:
                _cache_put(_COVER_CACHE, (str(d.get("tid")), str(idx)),
                           cover, _COVER_CACHE_MAX)
            media.append({
                "index": idx,
                "type": x.get("type"),
                "videoDuration": x.get("videoDuration") or 0,
                "width": x.get("width"), "height": x.get("height"),
                "title": x.get("title"), "desc": x.get("desc"),
                "isVideo": is_video,
                # 视频一律允许前端来要封面：插件没给 thumbUrl / 域名不在白名单时，
                # 后端还有「ffmpeg 抽视频首帧」兜底，不能让卡片退回无封面占位。
                "hasCover": 1 if is_video else 0,
            })
        d["media"] = media
        # 清空原图 CDN 直链：图片一律走 /api/moments/media 代理，避免 Referer 降级；
        # 视频封面走 /api/moments/cover 代理（含视频首帧兜底）。
        d["images"] = []
        out.append(d)
    return out


def feed(source="timeline", action="head", user="", media=True, limit=0):
    """重置到最新（head）或追加一页（next）。返回可 JSON 序列化的精简结构。"""
    which = "user" if source == "user" else "timeline"
    if action not in ("head", "next"):
        raise ValueError("action 只能是 head / next")
    if which == "user":
        user = _user(user)
    else:
        user = ""
    try:
        lim = max(0, min(500, int(limit or 0)))
    except (TypeError, ValueError):
        lim = 0
    with _lock:
        wx = _wx()
        if action == "head":
            d = wx.moments_head(which, user=user or None, media=media, limit=lim)
        else:
            d = wx.moments_next(which, user=user or None, media=media, limit=lim)
    items = _public(d.get("moments"))
    # 发布者头像只给 hasAvatar 信号：CDN 直链留在后端，DOM / 地址栏都不出现。
    # 通讯录同步过就有头像；没同步过也不影响 feed（回退昵称首字母）。
    avatars = store.avatar_map()
    for it in items:
        u = str(it.get("username") or "").strip()
        url = avatars.get(u, "")
        if url:
            _cache_put(_AVATAR_URLS, u, url, _AVATAR_MAX)
        it["hasAvatar"] = 1 if url else 0
    me = _self_wxid()
    if me:
        # 插件 likes[] / comments[] 里「本人」是真实 wxid，前端本地点赞/评论则是假
        # 条目 'me'。统一打标：取消赞时能清干净，删除按钮也只出现在本人的评论上。
        for it in items:
            for row in (it.get("likes") or []) + (it.get("comments") or []):
                if isinstance(row, dict) and str(row.get("wxid") or "") == me:
                    row["isSelf"] = 1
    return {
        "source": which, "user": user, "action": action,
        "selfWxid": me,
        "items": items, "count": len(items),
        "total": d.get("total"), "atEnd": bool(d.get("atEnd")),
        "stopReason": d.get("stopReason") or "",
        "elapsedMs": d.get("elapsedMs"),
    }


def avatar(user):
    """朋友圈发布者头像：按 username 查本地通讯录缓存，后端不带 Referer 取回二进制。

    与 media / cover 一样，前端只看到 blob:，CDN 直链既不下发 DOM 也不进地址栏。
    查不到头像或域名不在白名单时抛错，前端隐藏 <img>、回退昵称首字母。
    """
    u = _user(user)
    with _cache_lock:
        hit = _AVATAR_BYTES.get(u)
        if hit:
            _AVATAR_BYTES.move_to_end(u)
            return hit
        url = _AVATAR_URLS.get(u, "")
    if not url:
        # 进程重启 / 直接请求接口时 feed 缓存可能已丢，回查一次本地库
        url = store.avatar_map().get(u, "")
        if url:
            _cache_put(_AVATAR_URLS, u, url, _AVATAR_MAX)
    if not url:
        raise RuntimeError("通讯录里没有这个发布者的头像")
    host = urlparse(url).netloc.lower().split(":")[0]
    if not any(host == h.lstrip(".") or host.endswith(h) for h in _COVER_HOSTS):
        raise RuntimeError("头像域名不在白名单")
    # _fetch_cover 就是「不带 Referer 取图片 + 重定向白名单 + image/* 校验」，通用
    out = _fetch_cover(url)
    _cache_put(_AVATAR_BYTES, u, out, _AVATAR_MAX)
    return out


def friend_options():
    """「按好友」下拉：来自本地通讯录缓存（手机离线也能选），可再手动输入 wxid。"""
    out = []
    for r in store.list_friends():
        wxid = str(r.get("wxid") or "").strip()
        if wxid:
            out.append({"wxid": wxid, "name": r.get("name") or wxid})
    return out


def media(tid, index=0):
    """取图片 / 视频二进制，返回 (bytes, mime)，由路由直接写回浏览器。"""
    return _wx().moments_media(_tid(tid), _index(index))


def _fetch_cover(url):
    """后端直取缩略图：**不带 Referer**（带了微信 CDN 只给 120×120 占位图）。"""
    r = requests.get(url, timeout=(3, 8),
                     headers={"Accept": "image/*", "User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    # 重定向后的最终 host 也必须留在白名单里，避免 CDN 302 把请求带进内网
    host = urlparse(r.url or url).netloc.lower().split(":")[0]
    if not any(host == h.lstrip(".") or host.endswith(h) for h in _COVER_HOSTS):
        raise RuntimeError("封面重定向到了非白名单域名")
    ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not ct.startswith("image/") or not r.content:
        raise RuntimeError("封面地址返回的不是图片")
    return r.content, ct


def _run_ffmpeg(data, args, out_name, timeout):
    """用镜像内静态 ffmpeg 处理内存里的视频，返回输出文件字节。"""
    exe = _ffmpeg()
    with tempfile.TemporaryDirectory(prefix="wxbot-mom-") as td:
        src, dst = os.path.join(td, "in.bin"), os.path.join(td, out_name)
        with open(src, "wb") as f:
            f.write(data)
        p = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y",
                            "-i", src] + list(args) + [dst],
                           capture_output=True, timeout=timeout)
        if p.returncode != 0 or not os.path.isfile(dst):
            raise RuntimeError("ffmpeg 处理失败：%s" %
                               (p.stderr or b"").decode("utf-8", "ignore")[-160:])
        with open(dst, "rb") as f:
            return f.read()


def _video_bytes(tid, index):
    data, mime = media(tid, index)
    if not str(mime or "").startswith("video/"):
        raise RuntimeError("该媒体不是视频")
    if len(data) > _MAX_VIDEO_BYTES:
        raise RuntimeError("视频超过 %dMB，已跳过本地处理" %
                           (_MAX_VIDEO_BYTES // 1048576))
    return data


def cover(tid, index=0):
    """视频封面：优先代理插件给的缩略图直链；拿不到就用 ffmpeg 抽视频首帧。

    之前把 CDN 直链透给浏览器，白名单外的域名 / 直链过期 / 混合内容都会导致
    「没看到封面」；现在一律由后端取回转 blob，前端永远不接触直链。
    """
    tid, index = _tid(tid), _index(index)
    key = (tid, str(index))
    with _cache_lock:
        hit = _COVER_BYTES.get(key)
        if hit:
            _COVER_BYTES.move_to_end(key)
            return hit
        url = _COVER_CACHE.get(key, "")
    if url:
        try:
            out = _fetch_cover(url)
            _cache_put(_COVER_BYTES, key, out, _COVER_BYTES_MAX)
            return out
        except Exception:
            pass            # 微信 filekey 过期很常见，落到视频首帧
    data = _video_bytes(tid, index)
    out = (_run_ffmpeg(data, ["-frames:v", "1", "-q:v", "3"],
                        "frame.jpg", 45), "image/jpeg")
    _cache_put(_COVER_BYTES, key, out, _COVER_BYTES_MAX)
    return out


def video_h264(tid, index=0):
    """把 HEVC 等浏览器解不了的视频转成 H.264/AAC；结果按 tid|index 缓存。

    前端只有检测到 `videoWidth === 0`（有声音无画面）才请求这里，
    正常 H.264 视频不会触发任何转码。
    """
    tid, index = _tid(tid), _index(index)
    key = (tid, str(index))
    with _cache_lock:
        if key in _H264_CACHE:
            buf = _H264_CACHE.pop(key)
            _H264_CACHE[key] = buf
            return buf, "video/mp4"
    data = _video_bytes(tid, index)
    out = _run_ffmpeg(
        data,
        ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
         "-movflags", "+faststart"],
        "out.mp4", 180)
    global _H264_CACHE_BYTES
    with _cache_lock:
        _H264_CACHE[key] = out
        _H264_CACHE_BYTES += len(out)
        while _H264_CACHE_BYTES > _H264_CACHE_MAX_BYTES and len(_H264_CACHE) > 1:
            _, old = _H264_CACHE.popitem(last=False)
            _H264_CACHE_BYTES -= len(old)
    return out, "video/mp4"


def _write(preview, commit):
    """真实社交动作前先 dryRun 预检：预检不通过就绝不执行。"""
    pre = preview()
    if not isinstance(pre, dict) or pre.get("ok") is False:
        raise RuntimeError((pre or {}).get("error") or "预检未通过，已取消操作")
    return commit()


def like(tid, unlike=False, dry_run=False):
    tid = _tid(tid)
    wx = _wx()
    if dry_run:
        return wx.moments_like(tid, unlike=unlike, dry_run=True)
    with _lock:
        return _write(
            lambda: wx.moments_like(tid, unlike=unlike, dry_run=True),
            lambda: wx.moments_like(tid, unlike=unlike),
        )


def comment(tid, content, dry_run=False):
    tid = _tid(tid)
    text = str(content or "").strip()
    if not text or len(text) > 1000:
        raise ValueError("评论内容需为 1~1000 字")
    wx = _wx()
    if dry_run:
        return wx.moments_comment(tid, text, dry_run=True)
    with _lock:
        return _write(
            lambda: wx.moments_comment(tid, text, dry_run=True),
            lambda: wx.moments_comment(tid, text),
        )


def rm_comment(tid, comment_id=None, content=None, dry_run=False):
    """删除**本人**评论（8.11）：先 dryRun 定位，再按 commentId 精确删除。

    前端只对 feed 里打了 `isSelf` 且带 `commentId` 的评论显示删除按钮，
    所以不会误删他人评论；后端仍强制要求 commentId 或 content 之一。
    """
    tid = _tid(tid)
    cid = str(comment_id or "").strip()
    if cid and not _CID_RE.match(cid):
        raise ValueError("评论 ID 不合法")
    text = str(content or "").strip()
    if len(text) > 1000:
        raise ValueError("评论内容超长")
    if not cid and not text:
        # 插件「不传参数」= 删本人最新一条，风险太大，本后台不允许这种模糊删除
        raise ValueError("必须指定要删除的评论 ID")
    wx = _wx()
    kw = dict(comment_id=cid or None, content=text or None)

    def preview():
        p = wx.moments_rm_comment(tid, dry_run=True, **kw)
        if (isinstance(p, dict) and p.get("dryRun")
                and "wouldDelete" in p and p.get("wouldDelete") is None):
            raise RuntimeError("没有找到可删除的本人评论（可能已刷新或不是本人评论）")
        return p

    if dry_run:
        return preview()
    with _lock:
        return _write(preview, lambda: wx.moments_rm_comment(tid, **kw))
