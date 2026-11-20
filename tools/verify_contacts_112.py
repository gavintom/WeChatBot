"""验证 1.1.2-beta1：通讯录好友补齐「企业号」类联系人（uiClass 分类拉取）。

插件的 /api/contacts/friends 不带 uiClass 只返回微信好友；企业成员
（myEnterpriseMember）与企微联系人（enterpriseContact）必须各自再拉一次，
否则通讯录里明明有这些人，后台好友列表就是不出现。本脚本用临时库 + 假插件
客户端验证：

  1. 三类合并：默认好友 + 企业成员 + 企微联系人全部进好友池；
  2. 按 wxid 去重：同一人重复出现在多个分类里只保留一条，且不丢分类标记；
  3. 某个分类拉取失败（旧插件 404 / 手机离线）只当这一类为空，不阻断整次同步；
  4. 插件自带 uiClass 时沿用，不强行覆盖成请求的分类；
  5. 端到端 sync_all：落库后 list_friends 能按 ui_class 区分三类，
     并返回各分类新增条数（供同步提示语展示）。

全程不碰生产 analytics.db，也不真连手机。
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

_TMP = tempfile.mkdtemp(prefix="wxbot_contacts_112_")
os.environ["WXBOT_DB"] = os.path.join(_TMP, "analytics.db")
os.environ.setdefault("WXBOT_ADMIN_LOG", os.path.join(_TMP, "admin.log"))

import store  # noqa: E402
from admin import contacts  # noqa: E402

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
    """假插件客户端：按 uiClass 返回不同好友池，可指定某分类抛错。"""

    def __init__(self):
        self.broken = set()          # 这些 uiClass 的拉取抛错（模拟旧插件 / 离线）
        self.health_self = "wxid_self"
        self.pools = {
            None: [
                {"wxid": "wxid_self", "name": "我自己", "isSelf": True},
                {"wxid": "friend1", "name": "普通好友1"},
                # 与外部联系人同名同 wxid：验证去重时保留默认好友这条
                {"wxid": "dup_ext", "name": "重复的人"},
            ],
            "myEnterpriseMember": [
                {"wxid": "ent1@openim", "name": "企业同事1", "uiClass": "myEnterpriseMember"},
            ],
            "enterpriseContact": [
                {"wxid": "ext1", "name": "企微联系人1", "uiClass": "enterpriseContact"},
                {"wxid": "ext2", "name": "企微联系人2"},        # 插件没给 uiClass，要补上
                {"wxid": "dup_ext", "name": "重复的人"},       # 重复 wxid
            ],
        }

    def friends(self, wxid=None, name=None, ui_class=None, timeout=30):
        if ui_class in self.broken:
            raise RuntimeError("HTTP 404 not found")
        return {"friends": [dict(f) for f in self.pools.get(ui_class, [])],
                "count": len(self.pools.get(ui_class, []))}

    # ---- sync_all 端到端需要的其它接口 ----
    def refresh_contacts(self, force=False, timeout=30):
        return {"ok": True, "throttled": False}

    def groups(self):
        return [{"wxid": "g1@chatroom", "name": "群1", "memberCount": 3}]

    def chats(self):
        return []

    def health(self, timeout=2):
        return {"ok": True, "self": self.health_self}


print("=== 1. 三类合并（默认好友 + 企业成员 + 企微联系人）===")
wx = FakeWX()
friends, stats = contacts._fetch_friends(wx)
wxids = [f["wxid"] for f in friends]
check("默认好友在内", "friend1" in wxids, wxids)
check("企业成员已并入", "ent1@openim" in wxids, wxids)
check("企微联系人已并入", "ext1" in wxids and "ext2" in wxids, wxids)
check("合并后无重复 wxid", len(wxids) == len(set(wxids)), wxids)
check("重复 wxid 只保留默认好友那条",
      [f["name"] for f in friends if f["wxid"] == "dup_ext"] == ["重复的人"], friends)
check("重复条目未被打上外部联系人分类",
      next(f for f in friends if f["wxid"] == "dup_ext").get("uiClass") in (None, ""), friends)
check("插件没给 uiClass 的按请求分类补 enterpriseContact",
      next(f for f in friends if f["wxid"] == "ext2").get("uiClass") == "enterpriseContact", friends)
check("插件自带 uiClass 时沿用", 
      next(f for f in friends if f["wxid"] == "ext1").get("uiClass") == "enterpriseContact", friends)
check("分类统计：企业成员 1 位", stats.get("myEnterpriseMember") == 1, stats)
check("分类统计：企微联系人 2 位", stats.get("enterpriseContact") == 2, stats)
check("分类口径用词锁死（企业成员 / 企微联系人，不是企微外部）",
      dict(contacts.EXTRA_FRIEND_CLASSES) == {"myEnterpriseMember": "企业成员",
                                               "enterpriseContact": "企微联系人"},
      contacts.EXTRA_FRIEND_CLASSES)
check("合并条目补了 type / source 默认值",
      all(f.get("type") == "private" and f.get("source") == "contacts" for f in friends),
      friends)

print("\n=== 2. 某一分类拉取失败（旧插件 / 手机离线）===")
wx_bad = FakeWX()
wx_bad.broken.add("enterpriseContact")
friends_bad, stats_bad = contacts._fetch_friends(wx_bad)
check("企微联系人失败不影响默认好友",
      [f["wxid"] for f in friends_bad if f["wxid"] == "friend1"], friends_bad)
check("企微联系人失败仍能拿到企业成员",
      "ent1@openim" in [f["wxid"] for f in friends_bad], friends_bad)
check("失败分类统计为 0", stats_bad.get("enterpriseContact") == 0, stats_bad)

print("\n=== 3. 端到端 sync_all：落库 + 分类标记 + 分类条数 ===")
store.init_db()
contacts.client = lambda: FakeWX()
r = contacts.sync_all()
check("统计里的好友总数 = 合并结果", r["friends"] == len(store.list_friends()), r["friends"])
check("返回 extra 分类条数", r.get("extra") == {"myEnterpriseMember": 1,
                                              "enterpriseContact": 2}, r.get("extra"))
check("返回 extra_total", r.get("extra_total") == 3, r.get("extra_total"))
rows = {x["wxid"]: x for x in store.list_friends()}
check("企业成员落库且 ui_class 正确",
      rows.get("ent1@openim", {}).get("ui_class") == "myEnterpriseMember", rows.get("ent1@openim"))
check("企微联系人落库且 ui_class 正确",
      rows.get("ext1", {}).get("ui_class") == "enterpriseContact", rows.get("ext1"))
check("普通好友 ui_class 为空", not rows.get("friend1", {}).get("ui_class"), rows.get("friend1"))
check("本人标记仍在", rows.get("wxid_self", {}).get("is_self") is True, rows.get("wxid_self"))

print("\n=== 4. 二次同步幂等（不产生重复行）===")
n1 = len(store.list_friends())
contacts.sync_all()
check("再次同步行数不变", len(store.list_friends()) == n1,
      (n1, len(store.list_friends())))

print("\n=== 结果：%d 通过 / %d 失败 ===" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
