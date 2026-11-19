"""验证通讯录全量替换逻辑（1.1.1，同步前清空旧群聊/好友/群成员）。

用临时库隔离，不碰生产 analytics.db：
  1. 先塞入旧群聊 / 好友 / 群成员；
  2. replace_contacts_catalog 用全新数据替换；
  3. 断言旧数据（含手机端已删除的）全部消失，新数据落库，群成员缓存被清空；
  4. 再替换一次为空，断言三张表全空（模拟手机通讯录为空）。
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

_TMP = tempfile.mkdtemp(prefix="wxbot_contacts_verify_")
os.environ["WXBOT_DB"] = os.path.join(_TMP, "analytics.db")

import store  # noqa: E402

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


def seed_old():
    """直接写库塞入旧数据（模拟同步前的历史缓存）。"""
    now = __import__("time").time()

    def _do(conn):
        conn.execute("INSERT OR REPLACE INTO wx_groups(wxid,name,member_count,source,synced_at) "
                     "VALUES(?,?,?,?,?)", ("oldgroup@chatroom", "旧群A", 10, "sync", now))
        conn.execute("INSERT OR REPLACE INTO wx_contacts(wxid,name,is_group,source,synced_at) "
                     "VALUES(?,?,?,?,?)", ("oldfriend", "旧好友A", 0, "sync", now))
        conn.execute("INSERT OR REPLACE INTO wx_group_members(group_id,wxid,name,synced_at) "
                     "VALUES(?,?,?,?)", ("oldgroup@chatroom", "oldfriend", "旧成员", now))
    store._write_many(_do)


print("=== 1. 准备旧数据 ===")
store.init_db()
seed_old()
check("旧群聊已入库", len(store.list_groups()) == 1)
check("旧好友已入库", len(store.list_friends()) == 1)
check("旧群成员已入库",
      len(store.list_group_members("oldgroup@chatroom")) == 1)

print("\n=== 2. 全量替换为手机最新数据 ===")
new_groups = [
    {"wxid": "newgroup@chatroom", "name": "新群B", "memberCount": 25, "source": "sync"},
]
new_friends = [
    {"wxid": "newfriend", "name": "新好友B", "nickName": "B", "type": "private",
     "source": "contacts", "isGroup": False},
]
store.replace_contacts_catalog(new_groups, new_friends)

groups = store.list_groups()
friends = store.list_friends()
check("旧群聊被清空（手机已删除的不残留）",
      all(g["wxid"] != "oldgroup@chatroom" for g in groups), groups)
check("旧好友被清空", all(f["wxid"] != "oldfriend" for f in friends), friends)
check("新群聊已写入",
      len(groups) == 1 and groups[0]["wxid"] == "newgroup@chatroom", groups)
check("新好友已写入",
      len(friends) == 1 and friends[0]["wxid"] == "newfriend", friends)
check("群成员缓存随群聊一起被清空",
      len(store.list_group_members("newgroup@chatroom")) == 0)
check("已删除群的成员缓存也清空",
      len(store.list_group_members("oldgroup@chatroom")) == 0)

print("\n=== 3. 手机返回空群列表 → 保留旧群聊，好友照常替换 ===")
r = store.replace_contacts_catalog([], [{"wxid": "friendC", "name": "好友C"}])
check("空群列表时标记未替换群", r["groups_replaced"] is False, r)
check("空群列表时旧群聊被保留", len(store.list_groups()) == 1, store.list_groups())
check("好友非空时照常替换", r["contacts_replaced"] is True and
      [f["wxid"] for f in store.list_friends()] == ["friendC"], store.list_friends())

print("\n=== 4. 手机返回空好友列表 → 保留旧好友 ===")
r = store.replace_contacts_catalog([{"wxid": "groupD@chatroom", "name": "群D"}], [])
check("空好友列表时标记未替换好友", r["contacts_replaced"] is False, r)
check("空好友列表时旧好友被保留", len(store.list_friends()) == 1, store.list_friends())
check("群聊非空时照常替换", r["groups_replaced"] is True and
      len(store.list_groups()) == 1, store.list_groups())

print("\n=== 5. 两类都为空 → 全部保留（手机快照故障兜底）===")
r = store.replace_contacts_catalog([], [])
check("两类均未替换", not r["groups_replaced"] and not r["contacts_replaced"], r)
check("群聊未被清空", len(store.list_groups()) == 1)
check("好友未被清空", len(store.list_friends()) == 1)

print("\n=== 结果：%d 通过 / %d 失败 ===" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
