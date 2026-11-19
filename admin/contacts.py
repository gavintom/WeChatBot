"""通讯录管理：从手机插件同步群聊 / 好友，落库缓存，供后台查询与导出。

插件业务接口（WeChatBotTweak v1.2.7 / 2026-11-15，wechatbottweak_api.md 第 6~7 章）：
  GET /api/contacts/refresh        同步前重建全量通讯录（60 秒限流，数据仍有效）
  GET /api/contacts/groups         所有群聊（uiClass=group，含 memberCount）
  GET /api/contacts/friends        好友明细（含自己；不带 scope 参数）
  GET /api/contacts/find?wxid=     全量库反查，统一 matches[] 结构
  GET /api/chats                   所有会话（**无参数**；补全只在会话列表里的私聊好友）
  GET /api/contacts/groupmember?chat= 群成员（displayName / groupNick / avatar）
  GET /api/license                 授权状态 / 到期日（运行监控显示激活状态）
  POST /api/activate               提交激活码（空码 = 清除授权，本项目禁用）

社交信息写接口（WeChatBotTweak v1.2.10 起，协议第 10 章）：
  POST /api/contact/remark    改备注（只自己可见）；好友与群成员走同一个入口
  POST /api/group/nickname    改自己在群里的昵称（群内可见，v1.2.10-beta67 收敛）

v1.2.10-beta67 起：`/api/group/nickname/self`、`/api/group/nickname/member`
两个旧路径整体下线。改群成员备注**等价于改好友备注**，不需要群主 / 管理员权限；
它是全局备注——该群友在所有群里本机看到的显示名都会变成这个备注。

旧的 /api/groups、/api/groupmember、/api/contacts?scope=... 已 404 且无别名。
好友详情只读本地缓存，不再实时查插件（见 friend_detail）。
为什么要落库缓存：手机可能熄屏/掉线，但后台仍要能看群列表、导出群成员。
"""
import os
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import store
from admin import cfg as _cfg
from wx_client import WXClient

_client = None
_client_lock = threading.Lock()
_sync_lock = threading.Lock()


def client():
    global _client
    cfg = _cfg.read_config()
    w = cfg.get("wechat") or {}
    base = w.get("base_url") or ""
    token = w.get("token") or ""
    with _client_lock:
        if _client is None or _client.base != base.rstrip("/") or _client.token != token:
            _client = WXClient(base, token, w.get("timeout") or 10)
        return _client


def sync_all():
    """同步群列表 + 好友列表。返回统计（任一来源失败都往上抛，由路由转成提示）。

    v2 的 groups/friends/find 只读内存快照，快照只在 refresh 时重建，所以同步前
    必须先刷新一次；60 秒限流时返回 throttled=true，数据仍有效，不算失败。
    """
    with _sync_lock:
        wx = client()
        try:
            refreshed = wx.refresh_contacts()
        except Exception:
            refreshed = {}          # 后续读接口会自己报错，这里不额外干扰
        groups = wx.groups()
        friends = wx.friends().get("friends") or []
        # 企业成员（uiClass=myEnterpriseMember）不在默认的 friend 分类里，必须
        # 单独按分类再拉一次合并进好友列表，否则通讯录里加了企业微信同事也看不到。
        # 失败不影响主流程：拉不到就是没有这一类，不阻断其它数据落库。
        try:
            ent = wx.friends(ui_class="myEnterpriseMember").get("friends") or []
        except Exception:
            ent = []
        if ent:
            seen_ent = {str(f.get("wxid") or "") for f in friends}
            for f in ent:
                wxid = str(f.get("wxid") or "").strip()
                if not wxid or wxid in seen_ent:
                    continue
                f["uiClass"] = f.get("uiClass") or "myEnterpriseMember"
                friends.append(f)
                seen_ent.add(wxid)
        # v2 的联系人条目是统一结构（uiClass / isFriend / avatar…），没有旧版的
        # type / source；补上默认值，让本地缓存与「会话补全」追加的条目口径一致。
        # isSelf：插件 friends[] 自带；旧版插件没带时用 /api/health 的 self 兜底，
        # 保证「好友列表里的本人」这一行一定能被标出来。
        self_id = ""
        try:
            self_id = str((plugin_health() or {}).get("self") or "")
        except Exception:
            self_id = ""
        for f in friends:
            f.setdefault("type", "private")
            f.setdefault("source", "contacts")
            if self_id and str(f.get("wxid") or "") == self_id:
                f["isSelf"] = True
        # 会话列表补全：/api/chats 现在合并「微信真实会话列表 + 插件观察到过消息的会话」，
        # 可兜住那些只在会话列表、没进入通讯录索引的私聊联系人（按 wxid 去重）。
        # v1.2.0 起该接口无参数（旧版 all=1 已移除）。
        try:
            chats = wx.chats()
        except Exception:
            chats = []
        seen = {str(c.get("wxid") or "") for c in friends}
        for c in chats:
            wxid = str(c.get("wxid") or "").strip()
            if not wxid or wxid.endswith("@chatroom") or wxid.startswith("gh_") \
                    or wxid in seen:
                continue
            if c.get("type") not in (None, "", "private"):
                continue
            friends.append({"wxid": wxid, "name": c.get("name") or wxid,
                            "nickName": "", "remark": "", "type": "private",
                            "isGroup": False, "source": "session",
                            "lastTs": c.get("lastTs"), "lastContent": c.get("lastContent")})
            seen.add(wxid)
        # 所有远端数据都拉取成功后才整表替换：先清空旧的群聊/好友/群成员缓存，
        # 再写入本次结果。手机端已删除的条目随之消失；拉取阶段异常不会破坏旧缓存。
        # 某一类返回空时 replace 会保留该类旧缓存（手机快照未就绪的兜底）。
        replaced = store.replace_contacts_catalog(groups, friends)
        return {"groups": len(groups), "friends": len(friends),
                "throttled": bool(refreshed.get("throttled")),
                "groups_replaced": replaced["groups_replaced"],
                "contacts_replaced": replaced["contacts_replaced"],
                "group_list": store.list_groups(), "friend_list": store.list_friends()}


def _role_of_self(rows):
    """从群成员缓存推断「我在这个群的角色」。

    群成员表里 is_self=1 的那一行同时带 is_owner / is_admin（插件 v1.2.10-beta11
    起才有 is_admin）。返回统一 camelCase，与插件 `/api/contacts/groupmember`
    的 role 字段保持一致，前端只认一套键名。
    """
    me = next((m for m in rows or [] if m.get("is_self")), None)
    if not me:
        return None
    is_owner = bool(me.get("is_owner"))
    is_admin = bool(me.get("is_admin"))
    return {"self": me.get("wxid") or "", "owner": "",
            "isOwner": is_owner, "isAdmin": is_admin,
            "canManage": is_owner or is_admin, "source": "cache"}


def group_members(chat, refresh=False):
    """群成员：默认读缓存；refresh=1 时回手机重新拉。"""
    if not chat:
        raise ValueError("缺少群聊 chat")
    rows = store.list_group_members(chat)
    info = next((g for g in store.list_groups() if g["wxid"] == chat), {})
    if not refresh and rows:
        return {"chat": chat, "group_name": info.get("name") or chat,
                "count": len(rows), "members": rows, "cached": True,
                "role": _role_of_self(rows)}
    d = client().group_members(chat)
    # v2 不再直接给 truncated：客户端用 count < memberCount 换算。true 表示
    # members 只是部分列表，绝不写进缓存（否则会缺人还被当成完整名单），
    # 原样返回旧缓存并提示重试。
    if d.get("truncated"):
        rows = store.list_group_members(chat)
        return {"chat": chat,
                "group_name": d.get("groupName") or info.get("name") or chat,
                "owner": d.get("owner") or "", "self": d.get("self") or "",
                "count": len(rows), "members": rows, "cached": True,
                "role": d.get("role") or _role_of_self(rows),
                "truncated": True,
                "warning": "插件返回的是部分成员（遍历超时），本次未更新缓存，请稍后重试"}
    store.save_group_members(chat, d.get("members") or [])
    rows = store.list_group_members(chat)
    return {"chat": chat,
            "group_name": d.get("groupName") or info.get("name") or chat,
            "owner": d.get("owner") or "", "self": d.get("self") or "",
            "count": len(rows), "members": rows, "cached": False,
            # 插件 v1.2.10-beta11 起直接给 role（含 isAdmin），旧版没有就按缓存推断
            "role": d.get("role") or _role_of_self(rows),
            "truncated": False}


def friend_detail(wxid):
    """好友详情：**只用本地通讯录缓存**，不查插件。

    实时查询依赖手机在线，插件离线时要等超时、详情里还会挂一大段原始 JSON。
    通讯录同步时已把 wxid / 昵称 / 备注落库，详情页直接用它即可。
    """
    if not wxid:
        raise ValueError("缺少 wxid")
    local = store.get_contact(wxid) or {"wxid": wxid, "name": wxid}
    return {"wxid": wxid, "local": local}


def set_webhook(url):
    return client().set_webhook(url)


def set_alert(url, test=False):
    """写手机插件的告警推送地址（POST /api/alert，1.1.1-beta1）。"""
    return client().set_alert(url, test=test)


# ---------------------------------------------------------------- 社交信息（备注 / 群昵称）
# 插件 v1.2.10 起提供三个写接口（wechatbottweak_api.md 第 10 章）。它们都是
# **真实写操作**，插件的 dryRun 默认 1；本项目在路由层显式传 dry_run，默认真写。
# 成功判据一律是响应里的 `confirmed`（插件回读微信自己的数据得出），不是 ok
# 也不是 callRet（好友备注实测成功时 callRet 是 60~63，不是 0）。
#
# 改完立即回写本地缓存：插件的通讯录快照要等下一次 refresh 才重建，不回写的话
# 后台列表会继续显示旧备注，用户会以为没生效。
_PLUGIN_TOO_OLD = ("手机插件版本过低或接口已调整：写备注 / 群昵称需要 WeChatBotTweak "
                   "v1.2.10-beta67 及以上，请先升级手机插件后重试")

# 写完后让插件重建内存快照。插件的 /api/contacts/{friends,groupmember} 只读
# 快照，写备注不会自动更新它：不 force refresh 的话，用户点「从手机重新拉取」
# 会把刚写入的新备注又盖回旧值 —— 这就是「其它群 / 好友页不同步」的根因。
_snapshot_refresh = {"ts": 0.0, "lock": threading.Lock()}


def refresh_plugin_snapshot():
    """写成功后强制插件重扫通讯录（2 秒内只做一次），失败 / 离线都忽略。

    本地缓存已经先回写了，这一步只为后续从手机拉取拿到的也是新值。
    """
    now = time.time()
    with _snapshot_refresh["lock"]:
        if now - _snapshot_refresh["ts"] < 2:
            return False
        _snapshot_refresh["ts"] = now
    try:
        client().refresh_contacts(force=True)
        return True
    except Exception:
        return False


def _social(fn, *args, **kw):
    """统一把插件的「不支持 / 版本过低」翻译成人话，其余错误原样抛出。"""
    try:
        return fn(*args, **kw)
    except Exception as e:
        msg = str(e)
        if "HTTP 404" in msg:
            raise RuntimeError(_PLUGIN_TOO_OLD)
        if "HTTP 501" in msg:
            raise RuntimeError("插件明确表示该操作在当前微信版本无可用通道：%s" % msg)
        if "HTTP 403" in msg:
            raise RuntimeError("手机插件拒绝了这次写入（403），请确认插件已升级到 "
                               "v1.2.10-beta67 及以上。%s" % msg)
        raise


def _write_result(d, scope, before, after, extra=None, dry_run=False):
    """把插件响应收敛成统一结构，附带「是否已回读确认」的人话提示。"""
    confirmed = bool(d.get("confirmed"))
    out = {"scope": scope, "before": before, "after": after,
           "dryRun": bool(dry_run), "confirmed": confirmed,
           "plugin": {k: v for k, v in d.items() if k in
                      ("ok", "state", "requestId", "role", "target", "hint")}}
    if extra:
        out.update(extra)
    if dry_run:
        out["message"] = "预演完成：将写入「%s」，未改动手机数据" % (after or "（清空）")
    elif confirmed:
        out["message"] = "已写入手机微信：%s" % (after or "（已清空）")
    else:
        out["message"] = ("已下发但手机回读未见新值（多为落库延迟或与旧值相同），"
                          "稍后刷新缓存复查即可")
    return out


def _is_self_wxid(wxid):
    """判断某个 wxid 是不是当前登录账号自己。

    优先用通讯录缓存里的 is_self（同步时由插件 friends[].isSelf 落库）；
    老缓存没有这个标记时，退一步比对插件 /api/health 的 self。
    插件离线且缓存无标记时返回 False —— 微信自己也没有「给自己改备注」的
    通道，最终仍会被插件拒绝，这里只是提前给人话提示。
    """
    wxid = str(wxid or "").strip()
    if not wxid:
        return False
    local = store.get_contact(wxid) or {}
    if local.get("is_self"):
        return True
    try:
        self_id = str((plugin_health() or {}).get("self") or "")
    except Exception:
        self_id = ""
    return bool(self_id) and self_id == wxid


def set_friend_remark(wxid, remark, dry_run=False):
    """改好友备注（10.3，只自己可见）。成功后同步本地通讯录缓存。

    自己的账号也在好友列表里（插件 /api/contacts/friends 含自己），但微信
    没有「给自己改备注」的通道，接口层直接拒绝，避免前端点到才报错。
    """
    wxid = str(wxid or "").strip()
    remark = str(remark or "").strip()
    if not wxid:
        raise ValueError("缺少 wxid")
    if wxid.endswith("@chatroom"):
        raise ValueError("群 ID 不能改好友备注，请到「群聊 → 查看群友」里改群成员备注")
    if _is_self_wxid(wxid):
        raise ValueError("这是你自己的账号（本人），不能给自己改备注")
    local = store.get_contact(wxid) or {}
    before = str(local.get("remark") or "")
    d = _social(client().contact_remark, wxid, remark, dry_run=dry_run)
    if not dry_run and d.get("confirmed"):
        # 备注是全局的：好友表 + 该 wxid 在所有群里的成员行一起刷新，
        # 否则从「查看群友」进去还会看到改之前的旧显示名。
        store.update_remark_everywhere(wxid, remark)
        refresh_plugin_snapshot()
    return _write_result(d, "friend_remark", before, remark,
                         {"wxid": wxid, "name": local.get("name") or wxid},
                         dry_run=dry_run)


def set_group_nickname_self(chat, nickname, dry_run=False):
    """改自己在群里的昵称（10.1）。群内其他人可见，前端需二次确认。"""
    chat = str(chat or "").strip()
    nickname = str(nickname or "").strip()
    if not chat:
        raise ValueError("缺少群聊 chat")
    if not chat.endswith("@chatroom"):
        raise ValueError("chat 必须是群 ID（以 @chatroom 结尾）")
    self_wxid, before = "", ""
    for m in store.list_group_members(chat):
        if m.get("is_self"):
            self_wxid = m.get("wxid") or ""
            before = str(m.get("group_nick") or "")
            break
    if not self_wxid:
        # 缓存里没有本人记录（群成员还没拉过）：先拉一次，插件也需要 self 校验
        try:
            self_wxid = str((client().group_members(chat) or {}).get("self") or "")
        except Exception:
            self_wxid = ""
    d = _social(client().group_nickname_self, chat, nickname, dry_run=dry_run)
    if not self_wxid:
        self_wxid = str(d.get("self") or "")
    if not dry_run and d.get("confirmed") and self_wxid:
        store.update_self_group_nick(chat, self_wxid, nickname)
        refresh_plugin_snapshot()
    return _write_result(d, "group_nickname_self", before, nickname,
                         {"chat": chat, "self": self_wxid,
                          "role": d.get("role") or {}}, dry_run=dry_run)


# 群成员备注在 v1.2.10-beta67 起就是全局好友备注，这条提醒必须随写操作回到界面
GLOBAL_REMARK_NOTICE = "提醒：这是全局好友备注，该群友在所有群中本机看到的显示名都将是该备注"


def set_group_member_remark(chat, wxid, remark, dry_run=False):
    """改群成员备注（v1.2.10-beta67 起走 /api/contact/remark，全局生效）。

    插件把「群成员备注」与「好友备注」收敛成同一个入口，不再有群维度的备注，
    也不再要求群主 / 管理员。写的是全局备注：该 wxid 在所有群里本机看到的
    显示名都会变成这个值，所以本地缓存要把该 wxid 的所有群成员行一起刷新。
    `chat` 只用于定位来源群与取修改前的备注，不再发给插件。
    """
    chat = str(chat or "").strip()
    wxid = str(wxid or "").strip()
    remark = str(remark or "").strip()
    if not chat.endswith("@chatroom"):
        raise ValueError("chat 必须是群 ID（以 @chatroom 结尾）")
    if not wxid:
        raise ValueError("缺少成员 wxid")
    before, is_self = "", False
    for m in store.list_group_members(chat):
        if m.get("wxid") == wxid:
            before = str(m.get("remark") or "")
            is_self = bool(m.get("is_self"))
            break
    if is_self:
        raise ValueError("改自己请用「改我的群昵称」，群成员备注只用于别人")
    d = _social(client().contact_remark, wxid, remark, dry_run=dry_run)
    if not dry_run and d.get("confirmed"):
        store.update_remark_everywhere(wxid, remark)
        refresh_plugin_snapshot()
    out = _write_result(d, "group_member_remark", before, remark,
                        {"chat": chat, "wxid": wxid, "notice": GLOBAL_REMARK_NOTICE},
                        dry_run=dry_run)
    # 预演时也提醒，避免用户误以为只改当前群
    out["message"] += "；" + GLOBAL_REMARK_NOTICE
    return out


_health_cache = {"ts": 0.0, "data": None, "error": None}


def plugin_health(ttl=8):
    """插件健康状态：短超时 + 短缓存，手机离线时后台状态页也不会卡住。

    失败仍抛异常（保持调用方原有的 try/except 语义），但异常结果也缓存，
    避免每次刷新都等 2 秒。
    """
    now = time.time()
    if now - _health_cache["ts"] < ttl:
        if _health_cache["error"]:
            raise RuntimeError(_health_cache["error"])
        if _health_cache["data"] is not None:
            return dict(_health_cache["data"])
    try:
        data = client().health(timeout=2)
        _health_cache.update({"data": data, "error": None})
    except Exception as e:
        _health_cache.update({"data": None, "error": str(e)[:150]})
    _health_cache["ts"] = now
    if _health_cache["error"]:
        raise RuntimeError(_health_cache["error"])
    return dict(_health_cache["data"])


_license_cache = {"ts": 0.0, "data": None, "error": None}


def plugin_license(ttl=8, refresh=False):
    """插件授权状态（GET /api/license）：短超时 + 短缓存。

    授权过期后插件会主动关闭 HTTP 侦听，这个接口也会不可达；失败按异常抛出，
    由状态页转成「读取失败」提示，不拖慢页面（异常结果同样缓存）。
    """
    now = time.time()
    if refresh:
        _license_cache.update({"ts": 0.0, "data": None, "error": None})
    if now - _license_cache["ts"] < ttl:
        if _license_cache["error"]:
            raise RuntimeError(_license_cache["error"])
        if _license_cache["data"] is not None:
            return dict(_license_cache["data"])
    try:
        data = client().license(timeout=2)
        _license_cache.update({"data": data, "error": None})
    except Exception as e:
        _license_cache.update({"data": None, "error": str(e)[:150]})
    _license_cache["ts"] = now
    if _license_cache["error"]:
        raise RuntimeError(_license_cache["error"])
    return dict(_license_cache["data"])


def activate(code):
    """提交激活码，成功后立刻失效授权缓存，状态页下一次刷新即可看到新状态。"""
    d = client().activate(code)
    _license_cache.update({"ts": 0.0, "data": None, "error": None})
    return d


# ---------------------------------------------------------------- 当前微信昵称
# 运行监控每次刷新都会请求 /api/status，昵称查询必须短超时 + 缓存：
# 手机离线时不能把状态页拖住，且临时离线时仍显示上次成功取到的昵称。
_self_name_cache = {"ts": 0.0, "wxid": "", "name": ""}


def cached_self_nickname():
    """只取缓存、不发网络请求（插件离线分支用）。"""
    return _self_name_cache.get("name") or ""


def self_nickname(wxid, ttl=300):
    """当前登录微信昵称：优先插件 /api/contacts/find?wxid=，查不到回退本地缓存。

    v2 反查统一返回 matches[]（找不到是 ok=true / count=0，不是错误）；
    旧版 /api/contacts?wxid= 的 match 字段已不存在。
    插件的通讯录索引不一定包含本机账号，所以再用 `store.self_display_name()`
    从已同步的群成员（is_self=1 的 display_name）兜底。
    成功缓存 5 分钟；失败或空结果 1 分钟后重试，但**保留上次成功的昵称**。
    """
    wxid = str(wxid or "").strip()
    if not wxid:
        return ""
    now = time.time()
    c = _self_name_cache
    if c["wxid"] == wxid and now - c["ts"] < (ttl if c["name"] else 60):
        return c["name"]
    try:
        matches = client().contact_lookup(wxid, timeout=2).get("matches") or []
        hit = matches[0] if matches else {}
        name = str(hit.get("remark") or hit.get("name") or
                   hit.get("nickName") or "").strip()
    except Exception:
        name = ""
    if not name:
        name = store.self_display_name(wxid)
    if name:
        c["name"] = name
    c.update({"wxid": wxid, "ts": now})
    return c["name"]
