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
        store.save_groups(groups)
        friends = wx.friends().get("friends") or []
        # v2 的联系人条目是统一结构（uiClass / isFriend / avatar…），没有旧版的
        # type / source；补上默认值，让本地缓存与「会话补全」追加的条目口径一致。
        for f in friends:
            f.setdefault("type", "private")
            f.setdefault("source", "contacts")
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
        store.save_contacts(friends)
        return {"groups": len(groups), "friends": len(friends),
                "throttled": bool(refreshed.get("throttled")),
                "group_list": store.list_groups(), "friend_list": store.list_friends()}


def group_members(chat, refresh=False):
    """群成员：默认读缓存；refresh=1 时回手机重新拉。"""
    if not chat:
        raise ValueError("缺少群聊 chat")
    rows = store.list_group_members(chat)
    info = next((g for g in store.list_groups() if g["wxid"] == chat), {})
    if not refresh and rows:
        return {"chat": chat, "group_name": info.get("name") or chat,
                "count": len(rows), "members": rows, "cached": True}
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
                "truncated": True,
                "warning": "插件返回的是部分成员（遍历超时），本次未更新缓存，请稍后重试"}
    store.save_group_members(chat, d.get("members") or [])
    rows = store.list_group_members(chat)
    return {"chat": chat,
            "group_name": d.get("groupName") or info.get("name") or chat,
            "owner": d.get("owner") or "", "self": d.get("self") or "",
            "count": len(rows), "members": rows, "cached": False,
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
