"""验证引用媒体「本地优先、手机回退」（V1.1.2-beta10，不连手机/大模型）。

覆盖：
  1) media_store.find_local_media：按 chat+localID 命中/未命中、类型过滤、过期文件回退
  2) _quote_attachments：本地命中读本地文件（不调 WX）、未命中回退手机下载、
     过期回退、图片/文件两类、source 标记正确
  3) 路径还原重构后 _decorate_row 行为不变（回归）
"""
import ast
import base64
import mimetypes
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ((" | " + str(extra)) if extra and not cond else ""))


# ---------------- 隔离媒体库 ----------------
_TMP = tempfile.mkdtemp(prefix="wxbot_quotelocal_verify_")
os.environ["WXBOT_MEDIA_DB"] = os.path.join(_TMP, "media.db")
import media_store as ms  # noqa: E402
ms.configure(images_dir=os.path.join(_TMP, "images"),
             files_dir=os.path.join(_TMP, "files"), image_wait=0)

CHAT = "testroom@chatroom"
IMG_LID = 55555
FILE_LID = 66666
MISS_LID = 77777

# 造本地图片文件 + 记录
os.makedirs(ms.CFG["images_dir"], exist_ok=True)
img_bytes = b"\x89PNG\r\n\x1a\n" + b"I" * 300
img_name = "abc123.png"
img_path = os.path.join(ms.CFG["images_dir"], img_name)
with open(img_path, "wb") as f:
    f.write(img_bytes)

# 造本地文件 + 记录
os.makedirs(ms.CFG["files_dir"], exist_ok=True)
file_bytes = b"%PDF-1.4" + b"F" * 300
file_name = "def456.pdf"
file_path = os.path.join(ms.CFG["files_dir"], file_name)
with open(file_path, "wb") as f:
    f.write(file_bytes)

import time  # noqa: E402


def seed(chat, lid, kind, rel, ext, sha, orig):
    conn = ms._connect()
    try:
        ms._ensure(conn)
        conn.execute(
            "INSERT INTO media_items (downloaded_at,chat,chat_type,chat_name,wxid,"
            "sender_name,kind,orig_name,hash_name,sha256,ext,size,rel_path,dedup,"
            "tier,width,height,sent_at,via,local_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), chat, "group", "测试群", "wxid_sender", "发送者",
             kind, orig, rel.split("/")[-1], sha, ext, 100, rel, 0,
             "", 0, 0, time.time(), "quote", lid))
        conn.commit()
    finally:
        conn.close()


seed(CHAT, IMG_LID, "image", "images/" + img_name, "png", "sha_img", "原始图.png")
seed(CHAT, FILE_LID, "file", "files/" + file_name, "pdf", "sha_file", "报价单.pdf")

print("\n=== 1. find_local_media：命中/未命中/类型过滤 ===")
p1, r1 = ms.find_local_media(CHAT, IMG_LID, kind="image")
check("本地图片命中", p1 == img_path, p1)
check("命中返回原始文件名", r1 and r1.get("orig_name") == "原始图.png", r1 and r1.get("orig_name"))
p2, r2 = ms.find_local_media(CHAT, FILE_LID, kind="file")
check("本地文件命中", p2 == file_path, p2)
p3, r3 = ms.find_local_media(CHAT, IMG_LID, kind="file")
check("类型不符视为未命中", p3 is None, p3)
p4, r4 = ms.find_local_media(CHAT, MISS_LID, kind="image")
check("无记录视为未命中", p4 is None, p4)
p5, r5 = ms.find_local_media("别的群", IMG_LID, kind="image")
check("会话不符视为未命中", p5 is None, p5)
p6, r6 = ms.find_local_media(CHAT, IMG_LID, kind="image", wxid="wxid_sender")
check("wxid 过滤命中", p6 == img_path, p6)
p7, r7 = ms.find_local_media(CHAT, IMG_LID, kind="image", wxid="wrong")
check("wxid 不符视为未命中", p7 is None, p7)

print("\n=== 2. 过期文件回退（记录还在但磁盘文件没了）===")
os.remove(file_path)
p8, r8 = ms.find_local_media(CHAT, FILE_LID, kind="file")
check("文件已删除视为未命中", p8 is None, p8)
# 恢复供后续 _quote_attachments 测本地命中
with open(file_path, "wb") as f:
    f.write(file_bytes)

print("\n=== 3. _quote_attachments：本地优先 / 手机回退 ===")
SRC = open(os.path.join(ROOT, "webhook_server.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)
fn = next((n for n in TREE.body
           if isinstance(n, ast.FunctionDef) and n.name == "_quote_attachments"), None)
check("_quote_attachments 存在", fn is not None)
mod = ast.Module(body=[fn], type_ignores=[])
ast.fix_missing_locations(mod)
code = compile(mod, "<_quote_attachments>", "exec")


class FakeWX:
    def __init__(self):
        self.media_calls = []
        self.file_calls = []

    def media(self, api, chat, lid):
        self.media_calls.append((api, chat, lid))
        return (b"\x89PNG\r\n\x1a\n" + b"P" * 200, "png")

    def file(self, chat, lid):
        self.file_calls.append((chat, lid))
        return (b"%PDF-1.4" + b"D" * 200, "pdf")


class FakeLog:
    def __init__(self):
        self.infos, self.warns = [], []

    def info(self, *a, **k):
        self.infos.append(a)

    def warning(self, *a, **k):
        self.warns.append(a)


def make_quote(lid, qtype, found=True, media_api="/api/media"):
    return {"app_type": "quote", "chat": CHAT,
            "quote_resolved": {"found": found, "localID": lid, "quoteType": qtype,
                               "mediaApi": media_api, "fileName": "手机文件.pdf"},
            "quote": {"type": qtype, "title": "被引文件"}}


def run(m, wx, log):
    ns = {"WX": wx, "log": log, "media_store": ms,
          "base64": base64, "mimetypes": mimetypes, "re": re}
    exec(code, ns)
    return ns["_quote_attachments"](m)


# 图片：本地命中
wx, lg = FakeWX(), FakeLog()
out = run(make_quote(IMG_LID, 3), wx, lg)
check("图片本地命中：返回1个附件", len(out) == 1, out)
check("图片本地命中：source=local", out and out[0].get("source") == "local", out)
check("图片本地命中：data_url 用本地内容",
      out and base64.b64decode(out[0]["data_url"].split(",", 1)[1]) == img_bytes)
check("图片本地命中：不调手机 media", len(wx.media_calls) == 0, wx.media_calls)
check("图片本地命中：文件名用本地 orig_name", out and out[0]["name"] == "原始图.png", out)

# 图片：本地未命中 → 手机下载
wx, lg = FakeWX(), FakeLog()
out = run(make_quote(MISS_LID, 3), wx, lg)
check("图片本地未命中：回退返回附件", len(out) == 1, out)
check("图片回退：source=phone", out and out[0].get("source") == "phone", out)
check("图片回退：调了手机 media", len(wx.media_calls) == 1, wx.media_calls)

# 文件：本地命中
wx, lg = FakeWX(), FakeLog()
out = run(make_quote(FILE_LID, 49), wx, lg)
check("文件本地命中：source=local", len(out) == 1 and out[0].get("source") == "local", out)
check("文件本地命中：data_url 用本地内容",
      out and base64.b64decode(out[0]["data_url"].split(",", 1)[1]) == file_bytes)
check("文件本地命中：不调手机 file", len(wx.file_calls) == 0, wx.file_calls)
check("文件本地命中：文件名用本地 orig_name", out and out[0]["name"] == "报价单.pdf", out)

# 文件：本地未命中 → 手机下载
wx, lg = FakeWX(), FakeLog()
out = run(make_quote(MISS_LID, 49), wx, lg)
check("文件本地未命中：回退 source=phone", len(out) == 1 and out[0].get("source") == "phone", out)
check("文件回退：调了手机 file", len(wx.file_calls) == 1, wx.file_calls)

# 文件：本地记录过期（删文件）→ 回退
os.remove(file_path)
wx, lg = FakeWX(), FakeLog()
out = run(make_quote(FILE_LID, 49), wx, lg)
check("文件过期：回退 source=phone", len(out) == 1 and out[0].get("source") == "phone", out)
check("文件过期：调了手机 file", len(wx.file_calls) == 1, wx.file_calls)
with open(file_path, "wb") as f:
    f.write(file_bytes)

# 无引用 / found=false → 空
wx, lg = FakeWX(), FakeLog()
check("非引用消息→空", run({"app_type": "text"}, wx, lg) == [])
check("found=false→空", run(make_quote(IMG_LID, 3, found=False), wx, lg) == [])
check("不支持类型→空", run(make_quote(IMG_LID, 1), wx, lg) == [])

print("\n=== 4. 路径还原回归（_decorate_row 行为不变）===")
row = {"rel_path": "images/" + img_name, "kind": "image", "downloaded_at": time.time(),
       "sent_at": time.time(), "tier": "original", "width": 800, "height": 600}
dec = ms._decorate_row(dict(row))
check("_decorate_row 还原 path", dec.get("path") == img_path, dec.get("path"))
check("_decorate_row 标记 available", dec.get("available") is True)
check("_decorate_row 去掉 rel_path", "rel_path" not in dec)

print("\n" + "=" * 56)
print("结果：通过 %d，失败 %d" % (len(OK), len(FAIL)))
if FAIL:
    print("失败项：", FAIL)
    sys.exit(1)
