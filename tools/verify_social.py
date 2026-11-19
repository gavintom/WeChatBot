"""验证 1.1.1-beta2 社交信息写接口（好友备注 / 自己的群昵称 / 群成员备注）。

全程用临时库 + 假插件客户端，不碰生产 analytics.db，也不真连手机：
  1. 三个接口都把 dryRun=0 传给插件（插件的 dryRun 默认 1，漏传就等于没写）；
  2. 只有 confirmed=true 才回写本地缓存，confirmed=false 不改本地、提示复查；
  3. 备注改完 name / display_name 按「群名片 > 备注 > 昵称 > wxid」重算；
  4. 参数边界：群 ID 当好友、改自己的群成员备注、非群 chat 等；
  5. 插件 404 / 403 / 501 翻译成人话，不把 HTTP 正文糊到用户脸上；
  6. Flask 路由层：未登录 401、正常写入 200 且带 confirmed、缺 remark 400。
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

_TMP = tempfile.mkdtemp(prefix="wxbot_social_verify_")
os.environ["WXBOT_DB"] = os.path.join(_TMP, "analytics.db")
os.environ.setdefault("WXBOT_ADMIN_LOG", os.path.join(_TMP, "admin.log"))

import store  # noqa: E402
from admin import contacts  # noqa: E402
from wx_client import WXClientError  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, extra))


class FakeWX:
    """只实现被调用的三个写接口；记录参数，按 self.reply 返回 / 抛错。"""

    def __init__(self):
        self.calls = []
        self.reply = {}

    def _ret(self, name, **kw):
        self.calls.append((name, kw))
        r = self.reply.get(name)
        if isinstance(r, Exception):
            raise r
        out = {"ok": True, "state": "done", "confirmed": True}
        out.update(r or {})
        return out

    def refresh_contacts(self, force=False, timeout=30):
        self.calls.append(("refresh_contacts", {"force": force}))
        return {"ok": True, "throttled": False}

    def contact_remark(self, user, remark, dry_run=False, timeout=20):
        return self._ret("contact_remark", user=user, remark=remark, dry_run=dry_run)

    def group_nickname_self(self, chat, nickname, dry_run=False, timeout=20):
        return self._ret("group_nickname_self", chat=chat, nickname=nickname,
                         dry_run=dry_run)

    def group_members(self, chat, wxid=None, name=None):
        return {
            "chat": chat, "groupName": "示例群", "self": "wxid_me",
            "owner": "wxid_me", "isOwner": True,
            "role": {"self": "wxid_me", "owner": "wxid_me",
                     "isOwner": True, "isAdmin": False, "canManage": True},
            "adminCount": 1, "count": 2, "memberCount": 2, "truncated": False,
            "members": [
                {"wxid": "wxid_me", "name": "老唐", "nickName": "唐", "remark": "",
                 "groupNick": "", "displayName": "老唐", "isSelf": True,
                 "isOwner": True, "isAdmin": False},
                {"wxid": "wxid_a", "name": "阿强", "nickName": "强哥", "remark": "",
                 "groupNick": "3号店店长", "displayName": "3号店店长",
                 "isSelf": False, "isOwner": False, "isAdmin": True},
            ],
        }


FAKE = FakeWX()
contacts.client = lambda: FAKE


def seed():
    now = __import__("time").time()

    def _do(conn):
        conn.execute("INSERT OR REPLACE INTO wx_groups(wxid,name,member_count,source,synced_at)"
                     " VALUES(?,?,?,?,?)", ("g1@chatroom", "示例群", 3, "sync", now))
        conn.execute("INSERT OR REPLACE INTO wx_groups(wxid,name,member_count,source,synced_at)"
                     " VALUES(?,?,?,?,?)", ("g2@chatroom", "另一个群", 2, "sync", now))
        conn.execute("INSERT OR REPLACE INTO wx_contacts(wxid,name,nick_name,remark,type,"
                     "is_group,is_self,source,synced_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     ("wxid_a", "阿强", "强哥", "", "private", 0, 0, "contacts", now))
        conn.execute("INSERT OR REPLACE INTO wx_contacts(wxid,name,nick_name,remark,type,"
                     "is_group,is_self,source,synced_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     ("wxid_b", "小美", "美美", "美美-老客", "private", 0, 0, "contacts", now))
        # 插件 /api/contacts/friends 含自己，这里同步落成 is_self=1
        conn.execute("INSERT OR REPLACE INTO wx_contacts(wxid,name,nick_name,remark,type,"
                     "is_group,is_self,source,synced_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     ("wxid_me", "老唐", "唐", "", "private", 0, 1, "contacts", now))
        for group_id, wxid, name, nick, remark, gnick, disp, is_self, is_owner, is_admin in [
                ("g1@chatroom", "wxid_me", "老唐", "唐", "", "", "老唐", 1, 1, 0),
                ("g1@chatroom", "wxid_a", "阿强", "强哥", "", "3号店店长", "3号店店长", 0, 0, 1),
                ("g1@chatroom", "wxid_b", "小美", "美美", "美美-老客", "", "美美-老客", 0, 0, 0),
                # 同一个人出现在另一个群：全局备注必须同时刷到这两行
                ("g2@chatroom", "wxid_b", "小美", "美美", "美美-老客", "", "美美-老客", 0, 0, 0)]:
            conn.execute("INSERT OR REPLACE INTO wx_group_members(group_id,wxid,name,"
                         "nick_name,remark,group_nick,display_name,is_self,is_owner,"
                         "is_admin,synced_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (group_id, wxid, name, nick, remark, gnick, disp,
                          is_self, is_owner, is_admin, now))
    store._write_many(_do)


store.init_db()
seed()
print("=== 0. 准备缓存数据 ===")
check("好友缓存 3 条（含本人）", len(store.list_friends()) == 3)
check("群成员缓存 3 条", len(store.list_group_members("g1@chatroom")) == 3)
me_fr = [f for f in store.list_friends() if f["wxid"] == "wxid_me"][0]
check("好友本人的 is_self 为 True", me_fr["is_self"] is True)
check("本人排在好友列表最前", store.list_friends()[0]["wxid"] == "wxid_me",
      store.list_friends()[0]["wxid"])
a_mem = [m for m in store.list_group_members("g1@chatroom") if m["wxid"] == "wxid_a"][0]
check("群成员管理员标记已落库", a_mem["is_admin"] is True)
role = contacts._role_of_self(store.list_group_members("g1@chatroom"))
check("角色推断：群主可管理", role["isOwner"] is True and role["canManage"] is True, role)

print("\n=== 1. 好友备注 ===")
FAKE.calls.clear()
r = contacts.set_friend_remark("wxid_a", " 冷风-供应商 ")
call = dict(FAKE.calls[0][1])
check("插件收到 dryRun=0（真写）", call["dry_run"] is False, call)
check("备注去首尾空白后下发", call["remark"] == "冷风-供应商", call)
check("响应带 confirmed", r["confirmed"] is True)
c = store.get_contact("wxid_a")
check("本地备注已回写", c["remark"] == "冷风-供应商", c["remark"])
check("显示名按备注重算", c["name"] == "冷风-供应商", c["name"])
check("before 取的是旧备注", r["before"] == "", r["before"])
check("写成功后强制插件重建快照（其它群拉取不再拿到旧值）",
      any(c[0] == "refresh_contacts" and c[1].get("force") is True
          for c in FAKE.calls), FAKE.calls)

print("\n=== 2. 清除备注 → 显示名回落昵称 ===")
FAKE.calls.clear()
r = contacts.set_friend_remark("wxid_a", "")
check("插件收到空串 remark", FAKE.calls[0][1]["remark"] == "")
c = store.get_contact("wxid_a")
check("本地备注已清空", c["remark"] == "")
check("显示名回落到昵称", c["name"] == "强哥", c["name"])

print("\n=== 3. dryRun=1 只预演，不动本地 ===")
FAKE.calls.clear()
r = contacts.set_friend_remark("wxid_a", "试写", dry_run=True)
check("插件收到 dryRun=1", FAKE.calls[0][1]["dry_run"] is True)
check("本地备注未被改动", store.get_contact("wxid_a")["remark"] == "")
check("预演也回 confirmed", r["confirmed"] is True)

print("\n=== 4. confirmed=false 不回写本地 ===")
FAKE.reply["contact_remark"] = {"confirmed": False}
FAKE.calls.clear()
r = contacts.set_friend_remark("wxid_b", "别写进去")
check("confirmed 透传 false", r["confirmed"] is False)
check("提示要求复查", "复查" in r["message"], r["message"])
check("本地备注保持旧值", store.get_contact("wxid_b")["remark"] == "美美-老客")
FAKE.reply.pop("contact_remark")

print("\n=== 5. 参数边界：群 ID / 本人不能改好友备注 ===")
try:
    contacts.set_friend_remark("g1@chatroom", "x")
    check("群 ID 被拒绝", False)
except ValueError as e:
    check("群 ID 被拒绝", "群 ID" in str(e), str(e)[:80])

FAKE.calls.clear()
try:
    contacts.set_friend_remark("wxid_me", "给自己改备注")
    check("本人被拒绝", False)
except ValueError as e:
    check("本人被拒绝", "本人" in str(e), str(e)[:80])
check("本人被拒绝时没有调用插件写接口", FAKE.calls == [], FAKE.calls)

print("\n=== 6. 改自己的群昵称 ===")
FAKE.calls.clear()
r = contacts.set_group_nickname_self("g1@chatroom", "老唐-AI客服")
call = dict(FAKE.calls[0][1])
check("插件收到 chat / nickname / dryRun=0",
      call["chat"] == "g1@chatroom" and call["nickname"] == "老唐-AI客服"
      and call["dry_run"] is False, call)
check("before 取缓存里的旧群名片", r["before"] == "", r["before"])
check("self 来自缓存", r["self"] == "wxid_me", r["self"])
me = [m for m in store.list_group_members("g1@chatroom") if m["is_self"]][0]
check("本地群名片已更新", me["group_nick"] == "老唐-AI客服", me["group_nick"])
check("本地显示名跟着变", me["display_name"] == "老唐-AI客服", me["display_name"])

print("\n=== 7. 清空群昵称 → 显示名回落到微信昵称 ===")
FAKE.calls.clear()
contacts.set_group_nickname_self("g1@chatroom", "")
me = [m for m in store.list_group_members("g1@chatroom") if m["is_self"]][0]
check("群名片已清空", me["group_nick"] == "")
check("显示名回落到 name", me["display_name"] == "老唐", me["display_name"])

print("\n=== 8. 群成员备注（走全局好友备注通道） ===")
FAKE.calls.clear()
r = contacts.set_group_member_remark("g1@chatroom", "wxid_b", "小美-加盟店")
call = dict(FAKE.calls[0][1])
check("走的是 contact_remark（群成员备注接口已下线）",
      FAKE.calls[0][0] == "contact_remark", FAKE.calls[0][0])
check("user / remark 正确且 dryRun=0",
      call["user"] == "wxid_b" and call["remark"] == "小美-加盟店"
      and call["dry_run"] is False, call)
check("响应带全局提醒 notice", "所有群" in (r.get("notice") or ""), r.get("notice"))
check("message 也带全局提醒", "所有群" in (r.get("message") or ""), r.get("message"))
b = [m for m in store.list_group_members("g1@chatroom") if m["wxid"] == "wxid_b"][0]
check("本地群成员备注已更新", b["remark"] == "小美-加盟店", b["remark"])
check("显示名按备注重算", b["display_name"] == "小美-加盟店", b["display_name"])
b2 = [m for m in store.list_group_members("g2@chatroom") if m["wxid"] == "wxid_b"][0]
check("另一个群里的同一人同步更新", b2["remark"] == "小美-加盟店"
      and b2["display_name"] == "小美-加盟店", b2)
check("好友表也同步更新", store.get_contact("wxid_b")["remark"] == "小美-加盟店",
      store.get_contact("wxid_b")["remark"])

print("\n=== 8b. 清空全局备注 → 显示名回落昵称（不能用旧 name） ===")
FAKE.calls.clear()
contacts.set_group_member_remark("g1@chatroom", "wxid_b", "")
b2 = [m for m in store.list_group_members("g2@chatroom") if m["wxid"] == "wxid_b"][0]
check("清空后回落 nick_name 而不是旧备注", b2["display_name"] == "美美", b2["display_name"])
check("好友表备注已清空", store.get_contact("wxid_b")["remark"] == "")

print("\n=== 9. 群名片优先级高于备注 ===")
FAKE.calls.clear()
contacts.set_group_member_remark("g1@chatroom", "wxid_a", "阿强-1号店")
a = [m for m in store.list_group_members("g1@chatroom") if m["wxid"] == "wxid_a"][0]
check("备注已写入", a["remark"] == "阿强-1号店", a["remark"])
check("有群名片时显示名仍是群名片", a["display_name"] == "3号店店长", a["display_name"])

print("\n=== 10. 改自己请用群昵称接口 ===")
try:
    contacts.set_group_member_remark("g1@chatroom", "wxid_me", "x")
    check("拒绝改自己的成员备注", False)
except ValueError as e:
    check("拒绝改自己的成员备注", "群昵称" in str(e), str(e)[:80])

print("\n=== 11. 插件错误翻译 ===")
for err, kw in [
        (WXClientError("插件返回 HTTP 404：{\"ok\":false}"), "版本过低"),
        (WXClientError("插件返回 HTTP 403：{\"ok\":false}"), "手机插件拒绝"),
        (WXClientError("插件返回 HTTP 501：{\"supported\":false}"), "无可用通道")]:
    FAKE.reply["contact_remark"] = err
    try:
        contacts.set_friend_remark("wxid_a", "x")
        check("错误 %s 已翻译" % err, False)
    except RuntimeError as e:
        check("错误 %s 已翻译" % err, kw in str(e), str(e)[:100])
FAKE.reply.pop("contact_remark")

print("\n=== 12. Flask 路由层 ===")
os.environ.setdefault("WXBOT_DATA_DIR", os.path.join(_TMP, "data"))
from admin import app as admin_app  # noqa: E402

app = admin_app.app
app.config.update(TESTING=True)
HDR = {"X-Requested-With": "XMLHttpRequest"}   # 管理端同源校验要求的头


def post(cli, path, body):
    return cli.post(path, json=body, headers=HDR)


with app.test_client() as cli:
    r0 = post(cli, "/api/contacts/friends/remark", {"wxid": "wxid_a", "remark": "x"})
    # 未登录的写请求会先被 before_request 的反 CSRF 拦下（403 未登录），
    # 读请求才走 login_required 的 401；两者都是「拒绝执行」。
    check("未登录被拒绝", r0.status_code in (401, 403)
          and "未登录" in (r0.get_json() or {}).get("error", ""),
          "%s %s" % (r0.status_code, r0.get_json()))
    with cli.session_transaction() as sess:
        sess["uid"] = "tester"
    FAKE.calls.clear()
    r1 = post(cli, "/api/contacts/friends/remark",
              {"wxid": "wxid_a", "remark": "路由-备注"})
    j = r1.get_json() or {}
    check("好友备注 200", r1.status_code == 200, r1.status_code)
    check("响应带 confirmed / message", j.get("confirmed") is True and j.get("message"),
          j)
    check("本地已写入", store.get_contact("wxid_a")["remark"] == "路由-备注")
    r2 = post(cli, "/api/contacts/friends/remark", {"wxid": "wxid_a"})
    check("缺 remark 返回 400", r2.status_code == 400, r2.status_code)
    r3 = post(cli, "/api/contacts/groups/nickname",
              {"chat": "g1@chatroom", "nickname": "路由-群昵称"})
    check("群昵称 200", r3.status_code == 200 and (r3.get_json() or {}).get("ok"),
          r3.status_code)
    me = [m for m in store.list_group_members("g1@chatroom") if m["is_self"]][0]
    check("群昵称已落库", me["group_nick"] == "路由-群昵称", me["group_nick"])
    r4 = post(cli, "/api/contacts/groups/member/remark",
              {"chat": "g1@chatroom", "wxid": "wxid_b", "remark": "路由-成员备注"})
    check("群成员备注 200", r4.status_code == 200 and (r4.get_json() or {}).get("ok"),
          r4.status_code)
    j4 = r4.get_json() or {}
    check("路由响应带全局提醒", "所有群" in (j4.get("notice") or ""), j4.get("notice"))
    r5 = post(cli, "/api/contacts/groups/member/remark",
              {"chat": "not-a-group", "wxid": "wxid_b", "remark": "x"})
    check("非群 chat 返回 400", r5.status_code == 400, r5.status_code)
    r6 = cli.get("/api/contacts/groups/members/export?chat=g1@chatroom", headers=HDR)
    txt = r6.get_data(as_text=True)
    check("导出 200", r6.status_code == 200, r6.status_code)
    check("导出含「是否管理员」列", "是否管理员" in txt, txt[:120])

print("\n=== 13. 群角色 / 管理员字段透传（插件 v1.2.10-beta11）===")
d = contacts.group_members("g1@chatroom", refresh=True)
check("role.canManage 透传", (d.get("role") or {}).get("canManage") is True, d.get("role"))
check("群主 isOwner 透传", (d.get("role") or {}).get("isOwner") is True, d.get("role"))
mem = {m["wxid"]: m for m in d["members"]}
check("成员 isAdmin 已落库", mem["wxid_a"]["is_admin"] is True, mem["wxid_a"])
check("本人 is_self 保留", mem["wxid_me"]["is_self"] is True, mem["wxid_me"])

print("\n=== 14. 客户端协议路径（v1.2.10-beta67 收敛）===")
from wx_client import WXClient  # noqa: E402

c = WXClient("http://example.test", "token")
seen = {}


def fake_req(method, path, body=None, params=None, timeout=None):
    seen.update(method=method, path=path, body=body or {})
    return {"ok": True, "confirmed": True}


c._req = fake_req
c.group_nickname_self("g1@chatroom", "新群昵称", dry_run=False)
check("群昵称走 /api/group/nickname", seen.get("path") == "/api/group/nickname", seen)
check("群昵称 dryRun=0 真写", seen["body"].get("dryRun") == 0, seen["body"])
c.contact_remark("wxid_b", "全局备注", dry_run=False)
check("备注走 /api/contact/remark", seen.get("path") == "/api/contact/remark", seen)
check("备注 dryRun=0 真写", seen["body"].get("dryRun") == 0, seen["body"])

print("\n=== 结果：%d 通过 / %d 失败 ===" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
