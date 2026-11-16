"""个人微信插件（WeChatBotTweak / iOS）HTTP API 客户端。

协议来源：`WeChatBotTweak/wechatbottweak_api.md` **v1.2.7（2026-11-15）**，微信 8.0.80。
基址形如 http://192.0.2.71:18888；鉴权为 Header `X-Token`（插件 config.plist 的 token）。

本项目**只使用业务接口**：
  GET  /api/health                    版本、当前登录账号 self、webhook 地址、引擎就绪状态
  GET  /api/contacts/refresh          强制重建全量通讯录（60s 限流，同步前先调）
  GET  /api/contacts/groups           所有群聊（uiClass=group，含 memberCount）
  GET  /api/contacts/groupmember?chat= 群成员（displayName / groupNick / avatar）
  GET  /api/contacts/friends          好友明细；带 wxid/name 走统一反查
  GET  /api/contacts/find             全量库反查（matches[] 统一结构）
  GET  /api/chats                     全部会话（**无参数**）
  GET  /api/media?chat=&localID=      媒体二进制（Content-Type 按文件头判定）
  POST /api/send/text                 文本 / @某人（at + 可选 atNames / atAll / dryRun）
  POST /api/send/quote                引用回复（quoteLocalID + at + 可选 atNames）
  POST /api/send/image                图片（base64，插件上限 5MB，本客户端压到 2MB）
  POST /api/webhook                   设置 webhook 推送地址
  GET  /api/license                   授权状态 / 到期日 / 机器码
  POST /api/activate                  激活码激活（空码 = 清除，本项目禁用）
  GET  /api/moments/{timeline,user}/{gethead,getnext}   朋友圈时间线 / 好友主页
  GET  /api/moments/media             朋友圈图片 / 视频二进制
  POST /api/moments/{like,unlike,comment}               点赞 / 取消赞 / 评论

**通讯录 v2 与发送路径变更（早期修订）**：旧 `/api/groups`、
`/api/groupmember`、`/api/contacts?scope=...`、`/api/send` 全部 404 且**无别名**；
`atNames` 不再必传（插件自动取群名片），`atstyle` 已移除，`/api/send/text`
新增 `atAll` / `dryRun`（`/api/send/quote` 仍拒绝 @all）。
"""
import base64
import io
import re
import threading

import requests


class WXClientError(RuntimeError):
    pass



class WXClient:
    def __init__(self, base_url, token="", timeout=10, log=None):
        self.base = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = int(timeout or 10)
        self.log = log or (lambda *a, **kw: None)
        self._session = requests.Session()
        # 手机插件在局域网/本机，必须直连：开发机常设 HTTP_PROXY（IDE / 科学上网），
        # 走代理会让 /api/health、/api/send 全部超时。外网 LLM/ASR 不受影响。
        self._session.trust_env = False
        self._self_wxid = ""
        self._self_lock = threading.Lock()

    # ------------------------------------------------------------ 底层

    def _url(self, path):
        if not self.base:
            raise WXClientError("未配置手机插件地址（config.yaml 的 wechat.base_url）")
        if not path.startswith("/"):
            path = "/" + path
        return self.base + path

    def _req(self, method, path, params=None, body=None, raw=False, timeout=None):
        url = self._url(path)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if self.token:
            headers["X-Token"] = self.token
        try:
            r = self._session.request(
                method, url, params=params, json=body, headers=headers,
                timeout=timeout or self.timeout)
        except requests.RequestException as e:
            raise WXClientError("手机插件不可达（%s）：%s" % (url, e))
        if r.status_code >= 400:
            raise WXClientError("插件返回 HTTP %s：%s" % (r.status_code, (r.text or "")[:200]))
        if raw:
            return r.content, (r.headers.get("Content-Type") or "").split(";")[0].strip()
        try:
            d = r.json()
        except Exception:
            raise WXClientError("插件返回的不是 JSON：%s" % (r.text or "")[:200])
        if isinstance(d, dict) and d.get("ok") is False:
            raise WXClientError(d.get("error") or "插件返回失败")
        return d

    # ------------------------------------------------------------ 状态 / 身份

    def health(self, timeout=None):
        """timeout 可单独调小：探活/健康检查不该被离线手机拖住几秒。"""
        return self._req("GET", "/api/health", timeout=timeout)

    def self_wxid(self, refresh=False, timeout=None):
        """当前登录的微信号（判断「是否 @ 了本机」用）。结果缓存 5 分钟。"""
        with self._self_lock:
            if self._self_wxid and not refresh:
                return self._self_wxid
        h = self.health(timeout=timeout)
        wxid = (h.get("self") or "").strip()
        with self._self_lock:
            self._self_wxid = wxid
        return wxid

    def set_webhook(self, url):
        return self._req("POST", "/api/webhook", body={"url": str(url or "")})

    def license(self, timeout=None):
        """GET /api/license（现行协议）：授权状态、到期日、剩余天数、机器码。

        授权过期后插件会主动关闭 HTTP 侦听，此时这个接口也会不可达，
        调用方需要按「插件离线」处理并提示去微信内设置页激活。
        """
        return self._req("GET", "/api/license", timeout=timeout)

    def activate(self, code, dry_run=False, timeout=None):
        """POST /api/activate（现行协议）：提交激活码。

        ⚠️ 插件协议里 **空串 / 省略 code = 清除激活码、回到试用**，是危险操作。
        本项目只提供「激活」，所以客户端层直接拒绝空码与「全是空格 / 换行 /
        连字符」的等价空码（插件会自动忽略这些字符），任何上层调用都不可能
        误触发清除。dryRun=1 只校验不写入，用于预检。
        """
        if not isinstance(code, str):
            raise WXClientError("激活码必须是字符串")
        if not re.sub(r"[\s\-]+", "", code):
            raise WXClientError("激活码不能为空（空码会清除当前授权，已拒绝提交）")
        body = {"code": code}
        if dry_run:
            body["dryRun"] = 1
        return self._req("POST", "/api/activate", body=body, timeout=timeout)

    # ------------------------------------------------------------ 通讯录（v2）

    def refresh_contacts(self, force=False, timeout=30):
        """GET /api/contacts/refresh：重建全量通讯录（6.1）。

        默认 60 秒限流：重复调用返回 throttled=true / reused=true / retryAfter，
        数据仍有效，不算错误。force=1 绕过限流立即重扫。只读内存快照的
        groups/friends/find 都依赖这份快照，所以同步前应先调一次。
        """
        p = {"force": 1} if force else None
        return self._req("GET", "/api/contacts/refresh", params=p, timeout=timeout)

    def groups(self):
        """GET /api/contacts/groups（6.2）：groups[] 每项含 wxid/name/uiClass/
        isGroup/memberCount。旧的 /api/groups 已 404 且无别名。"""
        return (self._req("GET", "/api/contacts/groups", timeout=30).get("groups") or [])

    def friends(self, wxid=None, name=None, timeout=30):
        """GET /api/contacts/friends（6.4）。

        无参数 → {"friends": [...], "count": n}；带 wxid/name → {"lookup": 统一反查结构}。
        好友池含自己，本机实测只有个位数条目是正常的（全量里的 stranger 不是好友）。
        """
        p = {}
        if wxid:
            p["wxid"] = wxid
        if name:
            p["name"] = name
        d = self._req("GET", "/api/contacts/friends", params=p or None, timeout=timeout)
        if p:
            return {"lookup": d}
        return {"friends": d.get("friends") or [], "count": d.get("count", 0)}

    def find_contact(self, wxid=None, name=None, timeout=30):
        """GET /api/contacts/find（6.5）：在全量库（含群成员 / 公众号）反查。

        统一返回 {mode, query, exact, count, truncated, matches[]}；
        **找不到不是错误**：ok=true、count=0、matches=[]。
        """
        p = {}
        if wxid:
            p["wxid"] = wxid
        if name:
            p["name"] = name
        if not p:
            raise WXClientError("find_contact 需要 wxid 或 name（插件会返回 400）")
        return self._req("GET", "/api/contacts/find", params=p, timeout=timeout)

    def group_members(self, chat, wxid=None, name=None):
        """GET /api/contacts/groupmember（6.3）：列表模式或反查模式。

        v2 不再返回 truncated / elapsedMs；改看 count（本次返回条数）与
        memberCount（成员总数）。两者不等说明名单不全，仍用 truncated 标出来，
        让上层继续坚持「不把部分名单写进缓存」。
        """
        p = {"chat": chat}
        if wxid:
            p["wxid"] = wxid
        if name:
            p["name"] = name
        d = self._req("GET", "/api/contacts/groupmember", params=p, timeout=30)
        if wxid or name:
            return {"lookup": d}
        members = d.get("members") or []
        total = int(d.get("memberCount") or 0)
        returned = int(d.get("count") if d.get("count") is not None else len(members))
        return {"chat": d.get("chat") or chat,
                "groupName": d.get("groupName") or "",
                "owner": d.get("owner") or "",
                "self": d.get("self") or "",
                "isOwner": bool(d.get("isOwner")),
                "members": members,
                "count": returned, "memberCount": total,
                "truncated": bool(total and (returned < total or len(members) < total))}

    def chats(self):
        """文档 7.1：无参数；旧版 all=1 已移除，未知 query 会被插件静默忽略。"""
        return (self._req("GET", "/api/chats", timeout=30).get("chats") or [])

    def contact_lookup(self, wxid, timeout=30):
        """运行监控查当前微信昵称：走 /api/contacts/find 的全量库反查（6.5 / 6.6）。

        返回统一反查结构，命中项在 matches[]；旧版 /api/contacts?wxid= 的
        match 字段已不存在，调用方按 matches[0] 取值。
        """
        return self.find_contact(wxid=wxid, timeout=timeout)

    # ------------------------------------------------------------ 媒体

    def media(self, media_api=None, chat=None, local_id=None):
        """取媒体二进制，返回 (bytes, ext)。只接受本插件的 /api/media 路径，防止被塞外链。"""
        path = media_api or ""
        if not path:
            if not chat or not local_id:
                raise WXClientError("缺少 mediaApi / chat / localID")
            path = "/api/media?chat=%s&localID=%s" % (chat, local_id)
        head = path.split("?", 1)[0]
        if head != "/api/media":
            raise WXClientError("非法媒体地址：%s" % head)
        if "?" in path:
            from urllib.parse import parse_qsl
            head, qs = path.split("?", 1)
            params = dict(parse_qsl(qs))
        else:
            params = {}
            head = path
        data, mime = self._req("GET", head, params=params, raw=True, timeout=30)
        # 文档 7.3：Content-Type 按**文件头**判定，实测语音是 audio/silk、
        # 图片 image/png / image/jpeg、视频 video/mp4，还有旧网关的 octet-stream。
        # 语音必须映射成 silk，否则 voice.transcode 不会走 SILK 解码、ASR 拿到的是
        # 原始 SILK 二进制却按普通文件处理 → 识别必然失败。
        ext = {"audio/silk": "silk", "audio/octet-stream": "silk",
               "audio/amr": "amr", "image/png": "png", "image/jpeg": "jpg",
               "video/mp4": "mp4"}.get(mime, "bin")
        return data, ext

    # ------------------------------------------------------------ 发送

    def send_text(self, chat, text, at=None, at_names=None, at_all=False, dry_run=False):
        """POST /api/send/text（9.1）：文本 / @某人 / @所有人。

        旧路径 `/api/send` 已下线。正文传**纯正文**，不要自己写 `@昵称`：插件按
        `at[]` 自动拼 `@名字 + U+2005`。`atNames` 现在是**可选的强制覆盖**——
        不传时插件会自动取群名片（`m_nsChatRoomData`）再回退全局显示名，比
        服务端用 senderName 猜出来的名字更准，所以普通回复一律不传。
        `at_all=True` 会通知全群（仅群主/管理员可用），本项目默认不开；
        `dry_run=True` 只回显构造结果，零社交动作。
        """
        body = {"chat": chat, "text": str(text or "")}
        if at:
            body["at"] = list(at)
        if at_names:
            body["atNames"] = dict(at_names)
        if at_all:
            body["atAll"] = 1
        if dry_run:
            body["dryRun"] = 1
        return self._req("POST", "/api/send/text", body=body, timeout=30)

    def send_quote(self, chat, text, quote_local_id, at=None, at_names=None):
        """POST /api/send/quote（9.2）：引用回复，参数规则与 send_text 一致。

        注意 quote **仍拒绝** `at` 里的 @all 系列（11 章 `@all is not supported`），
        且不支持 atAll；调用方需先过滤。`quote` 对象 / `mode` 已废弃。
        """
        body = {"chat": chat, "text": str(text or ""),
                "quoteLocalID": int(quote_local_id)}
        if at:
            body["at"] = list(at)
        if at_names:
            body["atNames"] = dict(at_names)
        return self._req("POST", "/api/send/quote", body=body, timeout=30)

    def send_image(self, chat, path, max_bytes=2097152):
        """把本地图片以 base64 发给插件；超过上限时用 Pillow 压成 JPG。

        插件侧硬上限是 5MB（超限返回 413），这里保守按 2MB 压缩，
        留足 base64 膨胀与 HTTP 请求体的余量。
        """
        raw = open(path, "rb").read()
        if len(raw) > max_bytes:
            raw = self._compress(raw, max_bytes)
        b64 = base64.b64encode(raw).decode("ascii")
        return self._req("POST", "/api/send/image",
                         body={"chat": chat, "image": b64}, timeout=60)

    # ------------------------------------------------------------ 朋友圈（v3）

    @staticmethod
    def _moments_params(scope, user, media, limit):
        """gethead / getnext 共用参数。user/* 必须带 user，timeline 忽略 user。"""
        p = {"media": 1 if media else 0}
        if limit:
            p["limit"] = int(limit)
        if scope == "user":
            if not str(user or "").strip():
                raise WXClientError("好友主页必须提供 user（username / wxid）")
            p["user"] = str(user).strip()
        return p

    def moments_head(self, scope="timeline", user=None, media=True, limit=0,
                     timeout=60):
        """重置并返回最新一页（8.3 / 8.5）。会强制重建时间线会话，冷启动约 1.7s。"""
        which = "user" if scope == "user" else "timeline"
        return self._req("GET", "/api/moments/%s/gethead" % which,
                         params=self._moments_params(which, user, media, limit),
                         timeout=timeout)

    def moments_next(self, scope="timeline", user=None, media=True, limit=0,
                     timeout=60):
        """追加并返回下一页（8.4 / 8.6）。atEnd=true 且 count=0 时调用方停止翻页。"""
        which = "user" if scope == "user" else "timeline"
        return self._req("GET", "/api/moments/%s/getnext" % which,
                         params=self._moments_params(which, user, media, limit),
                         timeout=timeout)

    def moments_media(self, tid, index=0, original=True, timeout=60):
        """GET /api/moments/media（8.7）：返回 (bytes, mime)。

        走本客户端代理而不是把 CDN 直链丢给浏览器：浏览器会带 Referer，
        微信 CDN 只回 120×120 占位图；而且 token 不能出现在前端。
        """
        p = {"tid": str(tid), "index": int(index)}
        if not original:
            p["original"] = 0
        return self._req("GET", "/api/moments/media", params=p,
                         raw=True, timeout=timeout)

    def moments_media_info(self, tid, index=0, timeout=30):
        """info=1 只取元信息 JSON（mediaType / mime / bytes / videoDuration）。"""
        return self._req("GET", "/api/moments/media",
                         params={"tid": str(tid), "index": int(index), "info": 1},
                         timeout=timeout)

    def moments_like(self, tid, unlike=False, dry_run=False, timeout=30):
        """POST /api/moments/like | unlike（8.8 / 8.9）：真实社交动作，幂等。"""
        body = {"tid": str(tid)}
        if dry_run:
            body["dryRun"] = 1
        path = "/api/moments/unlike" if unlike else "/api/moments/like"
        return self._req("POST", path, body=body, timeout=timeout)

    def moments_comment(self, tid, content, dry_run=False, timeout=30):
        """POST /api/moments/comment（8.10）：真实社交动作，会通知对方。"""
        body = {"tid": str(tid), "content": str(content or "")}
        if dry_run:
            body["dryRun"] = 1
        return self._req("POST", "/api/moments/comment", body=body, timeout=timeout)

    def moments_rm_comment(self, tid, comment_id=None, content=None,
                           dry_run=False, timeout=30):
        """POST /api/moments/rm_comment（8.11）：删除**本人**评论，不可撤回。

        `comment_id` 精确匹配（优先）；不传则插件删本人最新一条。
        `dry_run=1` 只回候选清单，零社交动作。
        """
        body = {"tid": str(tid)}
        if comment_id:
            body["commentId"] = str(comment_id)
        elif content:
            body["content"] = str(content)
        if dry_run:
            body["dryRun"] = 1
        return self._req("POST", "/api/moments/rm_comment", body=body,
                         timeout=timeout)

    @staticmethod
    def _compress(raw, max_bytes):
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(raw))
            if im.mode in ("RGBA", "P"):
                im = im.convert("RGB")
            im.thumbnail((1600, 1600))
            for quality in (85, 72, 60, 45):
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=quality)
                data = buf.getvalue()
                if len(data) <= max_bytes:
                    return data
            return data
        except Exception:
            return raw
