"""验证 media_items 保存 local_id（V1.1.2-beta9）。

覆盖（不连真实手机 / 大模型 / 微信）：
  1) SCHEMA 与老库迁移：新列 local_id 存在，老库 _ensure 能幂等补列
  2) 直发图片/文件：落库写入本体消息的 localID
  3) 引用图片：落库写入「被引用图片」的 localID（而非外层引用消息）
  4) 查询：query / query_page 带出 local_id
  5) chatlog 本就有 local_id（回归确认，无需改动）
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ((" | " + str(extra)) if extra and not cond else ""))


_TMP = tempfile.mkdtemp(prefix="wxbot_localid_verify_")
MEDIA_DB = os.path.join(_TMP, "media.db")
os.environ["WXBOT_MEDIA_DB"] = MEDIA_DB

import policy  # noqa: E402
import media_store as ms  # noqa: E402

ms.configure(images_dir=os.path.join(_TMP, "images"),
             files_dir=os.path.join(_TMP, "files"), image_wait=0)


class FakeWX:
    def __init__(self):
        self.file_calls = []

    def media_info(self, chat, local_id, timeout=18):
        return {"ok": True, "tier": "original", "width": 800, "height": 600}

    def media(self, media_api, chat, local_id):
        return (b"\x89PNG\r\n\x1a\n" + b"A" * 200, "png")

    def file(self, chat, local_id):
        self.file_calls.append((chat, local_id))
        return (b"%PDF-1.4" + b"B" * 200, "pdf")


wx = FakeWX()

print("\n=== 0. SCHEMA 与老库迁移 ===")
conn = sqlite3.connect(MEDIA_DB)
ms._ensure(conn)
cols = {r[1]: r for r in conn.execute("PRAGMA table_info(media_items)")}
check("SCHEMA 含 local_id 列", "local_id" in cols)
check("local_id 为 INTEGER", cols.get("local_id", (0, "", ""))[2].upper() == "INTEGER",
      cols.get("local_id"))
check("local_id 默认 0", str(cols.get("local_id", (0, "", "", "", ""))[4]) == "0",
      cols.get("local_id"))
# 模拟老库：删掉列再让 _ensure 补回来（用重建表模拟 1.1.2-beta8 及更早的表）
conn.execute("DROP TABLE media_items")
conn.executescript("""
CREATE TABLE media_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, downloaded_at REAL NOT NULL,
    chat TEXT NOT NULL, chat_type TEXT NOT NULL DEFAULT 'group',
    chat_name TEXT NOT NULL DEFAULT '', wxid TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'image',
    orig_name TEXT NOT NULL DEFAULT '', hash_name TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '', ext TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0, rel_path TEXT NOT NULL DEFAULT '',
    dedup INTEGER NOT NULL DEFAULT 0, tier TEXT NOT NULL DEFAULT '',
    width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
    sent_at REAL NOT NULL DEFAULT 0, via TEXT NOT NULL DEFAULT 'direct'
);
""")
ms._ensure(conn)   # 应补回 local_id
cols2 = {r[1] for r in conn.execute("PRAGMA table_info(media_items)")}
check("老库 _ensure 幂等补 local_id", "local_id" in cols2, cols2)
ms._ensure(conn)   # 二次调用不报错
cols3 = {r[1] for r in conn.execute("PRAGMA table_info(media_items)")}
check("迁移二次调用幂等", cols2 == cols3)
conn.close()

print("\n=== 1. 直发媒体：落本体 localID ===")
img_payload = {
    "chat": "g1@chatroom", "chatType": "group", "isGroup": True, "chatName": "直发群",
    "sender": "wxid_a", "senderName": "张三", "type": 3, "typeName": "image",
    "localID": 11111, "ts": 1700000001, "mediaApi": "/api/media?chat=g1&localID=11111",
}
mimg = policy.normalize(img_payload)
ok, info = ms.ingest(mimg, wx)
check("直发图片落库成功", ok)
check("直发图片 local_id=本体 11111", info.get("local_id") == 11111, info.get("local_id"))

file_payload = dict(img_payload, type=49, appType="file", fileName="报价单.pdf",
                    fileExt="pdf", localID=22222, ts=1700000002)
mfile = policy.normalize(file_payload)
ok2, info2 = ms.ingest(mfile, wx)
check("直发文件落库成功", ok2)
check("直发文件 local_id=本体 22222", info2.get("local_id") == 22222, info2.get("local_id"))

print("\n=== 2. 引用图片：落被引用图的 localID（外层引用消息是另一个值）===")
QUOTE_MD5 = "abcd1234abcd1234abcd1234abcd1234"
QUOTED_LOCALID = 33333          # 被引用图片的 localID
OUTER_LOCALID = 99999           # 外层引用消息自身的 localID
content = '&lt;msg&gt;&lt;img md5="%s" /&gt;&lt;/msg&gt;' % QUOTE_MD5
quote_payload = {
    "chat": "g1@chatroom", "chatType": "group", "isGroup": True, "chatName": "引用群",
    "sender": "wxid_quoter", "senderName": "引用者",
    "type": 49, "appType": "quote", "localID": OUTER_LOCALID, "ts": 1700000003,
    "content": "&lt;msg&gt;&lt;refermsg&gt;...&lt;/refermsg&gt;&lt;/msg&gt;",
    "quote": {"type": 3, "svrid": "sv1", "content": content},
    "quoteResolved": {
        "found": True, "resolvedBy": "svrid", "quoteType": 3,
        "localID": QUOTED_LOCALID,
        "mediaApi": "/api/media?chat=g1&localID=%d" % QUOTED_LOCALID,
    },
}
mq = policy.normalize(quote_payload)
check("外层引用消息 localID=99999（确认两者不同）", mq.get("local_id") == OUTER_LOCALID,
      mq.get("local_id"))
check("识别为待处理引用图", ms.is_media_ready_for_quote(mq))
okq, infoq = ms.ingest_quoted_image(mq, wx)
check("引用图落库成功", okq, infoq)
check("引用图 local_id=被引用图 33333（非外层 99999）",
      infoq.get("local_id") == QUOTED_LOCALID, infoq.get("local_id"))

print("\n=== 3. 查询带出 local_id ===")
rows = ms.query_page(all_time=True)["items"]
lids = sorted(r.get("local_id") for r in rows)
check("query_page 带出全部三条 local_id", lids == [11111, 22222, 33333], lids)
qrows = ms.query(chat="g1@chatroom", all_time=True)
qids = sorted(r.get("local_id") for r in qrows)
check("query 同样带出 local_id", qids == [11111, 22222, 33333], qids)

print("\n=== 4. chatlog 本就有 local_id（回归确认）===")
import chatlog  # noqa: E402
schema = chatlog.SCHEMA if hasattr(chatlog, "SCHEMA") else ""
check("chatlog SCHEMA 已含 local_id", "local_id" in schema)

print("\n" + "=" * 56)
print("结果：通过 %d，失败 %d" % (len(OK), len(FAIL)))
if FAIL:
    print("失败项：", FAIL)
    sys.exit(1)
