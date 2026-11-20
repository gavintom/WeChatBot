"""admin 蓝图：通讯录 / 群成员 / 备注与群昵称。

路由实现从 admin/app.py 拆出，函数体未改动；共用工具在 admin/web.py。
"""

import time

from flask import Blueprint, request, session

import phone_mon
import store
from admin import cfg, contacts
from admin.web import (adminlog, body, csv_response, fail, login_required,
                       main_cfg, ok)

bp = Blueprint("contacts", __name__)


@bp.route("/api/contacts/webhook", methods=["POST"])
@login_required
def api_set_webhook():
    url = str(body().get("url") or "").strip()
    if not url:
        _, base, _ = main_cfg()
        c = cfg.read_config()
        pub = (c.get("server") or {}).get("public_base_url") or "http://127.0.0.1:29999"
        tok = (c.get("webhook") or {}).get("token") or ""
        url = pub.rstrip("/") + "/webhook" + ("?token=" + tok if tok else "")
    try:
        contacts.set_webhook(url)
        return ok(message="已设置插件推送地址", url=url)
    except Exception as e:
        return fail("设置失败（手机在线吗？）：%s" % e)


@bp.route("/api/contacts/alert", methods=["POST"])
@login_required
def api_set_alert():
    """把告警地址写进手机插件的 /api/alert（1.1.1-beta1，与消息推送独立）。

    告警只走这个地址（掉线 / 授权 / 地址测试），为空表示清除、只保留设备本地
    通知。默认写入主服务的独立 /alert 端点；test=1 时让插件发一条测试告警。
    """
    raw = body().get("url")
    url = str(raw).strip() if raw is not None else None
    test = bool(body().get("test"))
    if url is None:
        _, _, _ = main_cfg()
        c = cfg.read_config()
        pub = (c.get("server") or {}).get("public_base_url") or "http://127.0.0.1:29999"
        tok = (c.get("webhook") or {}).get("token") or ""
        url = pub.rstrip("/") + "/alert" + ("?token=" + tok if tok else "")
    try:
        res = contacts.set_alert(url, test=test)
        phone_mon.set_alert_info(url)
        enabled = None
        if isinstance(res, dict):
            enabled = res.get("networkAlertEnabled")
        phone_mon.set_alert_info(url, enabled=enabled)
        msg = "已设置插件告警地址" + ("（已发测试告警）" if test else "")
        return ok(message=msg, url=url)
    except Exception as e:
        return fail("设置失败（手机在线吗？）：%s" % e)

# ---------------------------------------------------------------- 通讯录

@bp.route("/api/contacts/sync", methods=["POST"])
@login_required
def api_contacts_sync():
    try:
        r = contacts.sync_all()
        extra = r.get("extra") or {}
        adminlog("同步通讯录 群=%s 好友=%s 企业成员=%s 企微联系人=%s 替换群=%s 替换好友=%s user=%s",
                 r["groups"], r["friends"], extra.get("myEnterpriseMember"),
                 extra.get("enterpriseContact"), r.get("groups_replaced"),
                 r.get("contacts_replaced"), session.get("uid"))
        parts = ["同步完成：%d 个群、%d 位好友" % (r["groups"], r["friends"])]
        # 额外分类（企业成员 / 企微联系人）单独说清楚，方便判断「通讯录里有的人
        # 后台没出现」到底是没拉到，还是真的不在通讯录里。
        breakdown = "、".join("%s %d 位" % (label, extra[cls])
                              for cls, label in contacts.EXTRA_FRIEND_CLASSES
                              if extra.get(cls))
        if breakdown:
            parts.append("其中 %s" % breakdown)
        if not r.get("groups_replaced", True):
            parts.append("手机未返回群列表，已保留原有群聊缓存")
        if not r.get("contacts_replaced", True):
            parts.append("手机未返回好友列表，已保留原有好友缓存")
        return ok(message="；".join(parts),
                   groups=r["group_list"], friends=r["friend_list"],
                   groups_replaced=r.get("groups_replaced"),
                   contacts_replaced=r.get("contacts_replaced"))
    except Exception as e:
        return fail("同步失败（检查手机插件地址 / 手机是否在线）：%s" % e)


@bp.route("/api/contacts/groups")
@login_required
def api_contacts_groups():
    return ok(items=store.list_groups())


@bp.route("/api/contacts/groups/members")
@login_required
def api_contacts_members():
    chat = request.args.get("chat") or ""
    refresh = request.args.get("refresh") in ("1", "true", "yes")
    try:
        return ok(**contacts.group_members(chat, refresh=refresh))
    except Exception as e:
        return fail(str(e))


@bp.route("/api/contacts/groups/members/export")
@login_required
def api_contacts_members_export():
    chat = request.args.get("chat") or ""
    try:
        d = contacts.group_members(chat, refresh=False)
    except Exception as e:
        return fail(str(e))
    headers = ["群聊", "wxid", "显示名", "昵称", "备注", "群名片", "是否本人", "是否群主", "是否管理员"]
    # 显示名必须和界面口径一致（app.js 的 shown_name）：
    # display_name → name → wxid。旧实现只写 name，导出里看到的是原始昵称，
    # 和表格里显示的群名片 / 自定义显示名对不上。
    rows = [[d.get("group_name") or chat, m.get("wxid"),
             m.get("display_name") or m.get("name") or m.get("wxid"),
             m.get("nick_name"), m.get("remark"), m.get("group_nick"),
             "是" if m.get("is_self") else "", "是" if m.get("is_owner") else "",
             "是" if m.get("is_admin") else ""]
            for m in d.get("members") or []]
    name = (d.get("group_name") or chat).replace("/", "_")
    return csv_response("群成员-%s-%s.csv" % (name, time.strftime("%Y%m%d")), headers, rows)


@bp.route("/api/contacts/search")
@login_required
def api_contacts_search():
    """全量通讯录搜索：好友 / 群聊 / 群成员一起按 wxid、name 查本地缓存。"""
    try:
        q = (request.args.get("q") or "").strip()
        items = store.search_contacts(q)
        return ok(q=q, items=items, count=len(items),
                   groups=sum(1 for x in items if x["kind"] == "group"),
                   friends=sum(1 for x in items if x["kind"] == "friend"),
                   members=sum(1 for x in items if x["kind"] == "member"))
    except (ValueError, OSError) as e:
        return fail(str(e))


@bp.route("/api/contacts/friends")
@login_required
def api_contacts_friends():
    return ok(items=store.list_friends())


@bp.route("/api/contacts/friends/detail")
@login_required
def api_contacts_friend_detail():
    """好友详情：只读本地通讯录缓存，不再向插件发实时查询。"""
    try:
        return ok(**contacts.friend_detail(request.args.get("wxid") or ""))
    except (ValueError, OSError) as e:
        return fail(e)


# ------------------------------------------------ 社交信息：备注 / 群昵称（1.1.1-beta2）
# 三个写接口都代理手机插件第 10 章，默认 dryRun=0（真写）；传 dryRun=1 可预演。
# 成功判据是插件回读的 confirmed，响应里一并回给前端做提示。

@bp.route("/api/contacts/friends/remark", methods=["POST"])
@login_required
def api_contacts_friend_remark():
    b = body()
    if "remark" not in b:
        return fail("缺少 remark（清空备注请传空串）")
    dry = bool(b.get("dryRun"))
    try:
        r = contacts.set_friend_remark(b.get("wxid"), b.get("remark"), dry_run=dry)
        adminlog("改好友备注 wxid=%s 新值=%r dryRun=%s confirmed=%s user=%s",
                 (b.get("wxid") or ""), r.get("after"), dry, r.get("confirmed"),
                 session.get("uid"))
        return ok(**r)
    except (ValueError, RuntimeError) as e:
        return fail(e)


@bp.route("/api/contacts/groups/nickname", methods=["POST"])
@login_required
def api_contacts_group_nickname_self():
    """改自己在群里的昵称（群内其他人可见，前端已二次确认）。"""
    b = body()
    if "nickname" not in b:
        return fail("缺少 nickname（清空群昵称请传空串）")
    dry = bool(b.get("dryRun"))
    try:
        r = contacts.set_group_nickname_self(b.get("chat"), b.get("nickname"),
                                            dry_run=dry)
        adminlog("改本人群昵称 chat=%s 新值=%r dryRun=%s confirmed=%s user=%s",
                 b.get("chat"), r.get("after"), dry, r.get("confirmed"),
                 session.get("uid"))
        return ok(**r)
    except (ValueError, RuntimeError) as e:
        return fail(e)


@bp.route("/api/contacts/groups/member/remark", methods=["POST"])
@login_required
def api_contacts_group_member_remark():
    """改群成员备注（v1.2.10-beta67 起走好友备注通道，全局生效，无需群主/管理员）。"""
    b = body()
    if "remark" not in b:
        return fail("缺少 remark（清空备注请传空串）")
    dry = bool(b.get("dryRun"))
    try:
        r = contacts.set_group_member_remark(b.get("chat"), b.get("wxid"),
                                            b.get("remark"), dry_run=dry)
        adminlog("改群成员备注 chat=%s wxid=%s 新值=%r dryRun=%s confirmed=%s user=%s",
                 b.get("chat"), b.get("wxid"), r.get("after"), dry,
                 r.get("confirmed"), session.get("uid"))
        return ok(**r)
    except (ValueError, RuntimeError) as e:
        return fail(e)
