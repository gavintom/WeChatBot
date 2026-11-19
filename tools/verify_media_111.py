"""验证 1.1.1-beta1 新功能（不连真实手机 / 大模型 / 微信）。

覆盖：
  1) policy.normalize 保留文件元信息字段；message_kind 判定不变
  2) media_store：图片/文件下载、哈希命名、去重、记录写入、30 分钟查询、
     按群名/wxid/类型/场景过滤、7 天过期文件清理
  3) phone_mon：touch / mark_offline / 连续时长推算 / 告警地址缓存
  4) wx_client.set_alert / file 的协议路径与参数
  5) 插件门面 ctx.recent_media 透传到宿主
"""
import hashlib
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ((" | " + str(extra)) if extra and not cond else ""))


# ---------------------------------------------------------------- 隔离数据目录
_TMP = tempfile.mkdtemp(prefix="wxbot_media_verify_")
os.environ["WXBOT_MEDIA_DB"] = os.path.join(_TMP, "media.db")
os.environ["WXBOT_DB"] = os.path.join(_TMP, "analytics.db")

import policy  # noqa: E402
import media_store  # noqa: E402
import phone_mon  # noqa: E402

media_store.configure(index_ttl=30 * 60, file_ttl=7 * 86400,
                      images_dir=os.path.join(_TMP, "images"),
                      files_dir=os.path.join(_TMP, "files"))
phone_mon.DB_PATH = os.environ["WXBOT_DB"]

print("\n=== 0. 消息归一化：文件元信息保留 ===")
payload = {
    "chat": "123456@chatroom", "chatType": "group", "isGroup": True,
    "chatName": "测试群", "sender": "wxid_abc", "senderName": "张三",
    "type": 49, "appType": "file", "fileName": "报价单.pdf",
    "fileSize": 1024, "fileExt": "pdf", "fileMd5": "abc123",
    "localID": 8888, "ts": time.time(),
}
m = policy.normalize(payload)
check("normalize 保留 file_name", m.get("file_name") == "报价单.pdf", m.get("file_name"))
check("normalize 保留 file_ext", m.get("file_ext") == "pdf", m.get("file_ext"))
check("normalize 保留 file_size", m.get("file_size") == 1024, m.get("file_size"))
check("normalize 保留 app_type=file", m.get("app_type") == "file", m.get("app_type"))
check("message_kind 文件仍归 other", policy.message_kind(m) == "other", policy.message_kind(m))

img_payload = dict(payload, type=3, typeName="image", appType="")
mimg = policy.normalize(img_payload)
check("图片 is_image 判定", media_store.is_image(mimg))
check("文件 is_file 判定", media_store.is_file(m))
check("文本非媒体", not media_store.is_media(policy.normalize(dict(payload, type=1, appType=""))))

# 1.1.1-beta1 修复：微信真实推送里文件常以 appType=file_uploading 到达，
# 旧判定只认 file 会漏下载。这里锁住两类新口径，防止回归。
mupload = policy.normalize(dict(payload, appType="file_uploading"))
check("file_uploading 判为文件", media_store.is_file(mupload), mupload.get("app_type"))
check("file_uploading 判为媒体", media_store.is_media(mupload))
mnometa = policy.normalize(dict(payload, appType="", fileName="w.pdf", fileMd5="h"))
check("无 appType 但有文件名+md5 判为文件", media_store.is_file(mnometa))
mquote = policy.normalize(dict(payload, appType="quote", fileName="", fileMd5=""))
check("quote 不误判为文件", not media_store.is_file(mquote))
murl = policy.normalize(dict(payload, appType="url", fileName="a.pdf", fileMd5=""))
check("url 链接不误判为文件", not media_store.is_file(murl))

print("\n=== 1. media_store：下载 / 哈希命名 / 去重 ===")


class FakeWX:
    def __init__(self):
        self.file_calls = []
        self.media_calls = []

    def media(self, media_api, chat, local_id):
        self.media_calls.append((media_api, chat, local_id))
        return b"\x89PNG\r\n\x1a\n-fake-image-", "png"

    def file(self, chat, local_id):
        self.file_calls.append((chat, local_id))
        return b"%PDF-1.7 fake pdf content", "pdf"


wx = FakeWX()
ok, info = media_store.ingest(mimg, wx)
check("图片入库成功", ok is True, info)
img_path = os.path.join(media_store.CFG["images_dir"], info["hash_name"])
check("图片落盘到 images_dir", os.path.isfile(img_path), img_path)
check("图片哈希文件名正确",
      info["hash_name"] == hashlib.sha256(b"\x89PNG\r\n\x1a\n-fake-image-").hexdigest() + ".png",
      info["hash_name"])
check("图片调用 media(chat,localID)", len(wx.media_calls) == 1 and wx.media_calls[0][1:] == ("123456@chatroom", 8888))

ok2, info2 = media_store.ingest(m, wx)
check("文件入库成功", ok2 is True, info2)
check("文件原名保留", info2["orig_name"] == "报价单.pdf", info2.get("orig_name"))
check("文件落盘到 files_dir", os.path.isfile(os.path.join(media_store.CFG["files_dir"], info2["hash_name"])))
check("文件调用 file(chat,localID)", wx.file_calls == [("123456@chatroom", 8888)], wx.file_calls)

# 同内容再发一次（不同 wxid / 群），应 dedup=1 不重复写盘，但记录仍在
m_dup = dict(m, sender="wxid_other", chatName="另一个群", local_id=9999)
m_dup = policy.normalize(dict(payload, sender="wxid_other", chatName="另一个群",
                               localID=9999, type=49, appType="file"))
ok3, info3 = media_store.ingest(m_dup, wx)
check("同哈希再次入库成功", ok3 is True)
check("同哈希标记 dedup", info3.get("dedup") == 1, info3.get("dedup"))

print("\n=== 2. media_store：查询 / 过滤 ===")
allrows = media_store.query(minutes=30)
check("查询返回 3 条", len(allrows) == 3, len(allrows))
by_name = media_store.query(chat_name="测试群", minutes=30)
check("按群名过滤", len(by_name) == 2, [r["chat_name"] for r in by_name])
by_wxid = media_store.query(wxid="wxid_other", minutes=30)
check("按 wxid 过滤", len(by_wxid) == 1 and by_wxid[0]["wxid"] == "wxid_other")
by_kind = media_store.query(kind="image", minutes=30)
check("按 kind=image 过滤", len(by_kind) == 1 and by_kind[0]["kind"] == "image")
by_type = media_store.query(chat_type="group", minutes=30)
check("按 chat_type=group 过滤", len(by_type) == 3)
by_chat = media_store.query(chat="123456@chatroom", minutes=30)
check("按 chat 精确过滤", len(by_chat) == 3, [r["wxid"] for r in by_chat])
check("返回含下载时间文本", bool(allrows[0].get("download_time")))
check("返回含绝对路径", allrows[0]["path"].startswith(_TMP))
check("返回 available=True", allrows[0]["available"] is True)
check("minutes=0 视为全部", len(media_store.query(minutes=0)) == 3)
check("minutes=999 仍是完整窗口（不再 clamp 到 30）",
      len(media_store.query(minutes=999)) == 3)

# 分页：query_page 的 total / pages / 越界回退
pg1 = media_store.query_page(minutes=0, page=1, page_size=2)
check("分页第 1 页返回 2 条", len(pg1["items"]) == 2, len(pg1["items"]))
check("分页 total=3", pg1["total"] == 3, pg1["total"])
check("分页 pages=2", pg1["pages"] == 2, pg1["pages"])
pg2 = media_store.query_page(minutes=0, page=2, page_size=2)
check("分页第 2 页返回 1 条", len(pg2["items"]) == 1, len(pg2["items"]))
check("两页无重复", {r["id"] for r in pg1["items"]} & {r["id"] for r in pg2["items"]} == set())
pgx = media_store.query_page(minutes=0, page=99, page_size=2)
check("页码越界回退到最后一页", pgx["page"] == 2 and len(pgx["items"]) == 1, pgx["page"])
check("query offset 生效",
      [r["id"] for r in media_store.query(minutes=0, limit=2, offset=2)] ==
      [r["id"] for r in pg2["items"]])

# 私聊一条
priv = policy.normalize(dict(payload, chat="wxid_private", chatType="private",
                             isGroup=False, chatName="李四", type=3, typeName="image",
                             appType="", localID=7001))
media_store.ingest(priv, wx)
priv_rows = media_store.query(chat_type="private", minutes=30)
check("私聊记录可按 chat_type 查到", len(priv_rows) == 1 and priv_rows[0]["chat"] == "wxid_private")
check("私聊不适用群名过滤", len(media_store.query(chat_name="测试群", chat_type="private")) == 0)

print("\n=== 3. media_store：7 天过期清理 ===")
old_file = os.path.join(media_store.CFG["files_dir"], "old_expired.pdf")
with open(old_file, "wb") as f:
    f.write(b"old")
old_ts = time.time() - 8 * 86400
os.utime(old_file, (old_ts, old_ts))
media_store._LAST_PURGE = 0
media_store.maybe_purge()
check("8 天前且无引用的文件被删除", not os.path.isfile(old_file))
# 1.1.1-beta2：流水不再按 30 分钟清理，清理后记录仍在
check("清理不删 30 分钟内的流水", len(media_store.query(minutes=0)) == 4,
      len(media_store.query(minutes=0)))
check("record_ttl 跟随 file_ttl", media_store.CFG["record_ttl"] == 7 * 86400,
      media_store.CFG["record_ttl"])

# 关键规则：文件超期但表里仍被引用，绝不能删（否则记录还在、文件打不开）
ref_name = "ref_keep.pdf"
ref_file = os.path.join(media_store.CFG["files_dir"], ref_name)
with open(ref_file, "wb") as f:
    f.write(b"ref")
os.utime(ref_file, (old_ts, old_ts))
_c = media_store._connect()
try:
    media_store._ensure(_c)
    _c.execute(
        "INSERT INTO media_items (downloaded_at,chat,chat_type,chat_name,wxid,kind,"
        "orig_name,hash_name,sha256,ext,size,rel_path,tier,width,height,sent_at,via) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (time.time(), "keep@chatroom", "group", "保留测试群", "wxid_keep", "file",
         ref_name, ref_name, "deadbeef", "pdf", 3, "files/" + ref_name, "", 0, 0, 0,
         "direct"))
    _c.commit()
finally:
    _c.close()
media_store._LAST_PURGE = 0
media_store.maybe_purge()
check("8 天前但被流水引用的文件保留", os.path.isfile(ref_file))
check("引用记录仍在", len(media_store.query(chat="keep@chatroom", minutes=0)) == 1)

print("\n=== 4. phone_mon：活跃 / 离线 / 连续时长 ===")
# 用「当前时间」touch（真实链路探活/收消息都是当下时刻），避免触发 90 秒离线宽限
now = time.time()
phone_mon.mark_offline(now - 3600)
phone_mon.touch(now, source="health")
s1 = phone_mon.state()
check("touch 后在线", s1["online"] is True, s1)
check("上次活跃时间接近设定值", abs(s1["last_active"] - now) < 2, s1["last_active"])
check("连续时长接近 0（刚上线）", s1["continuous_seconds"] < 3, s1["continuous_seconds"])
check("来源记为 health", s1["last_source"] == "health")
# 模拟已连续在线 600 秒：直接把会话起点/上次活跃回拨 600 秒（仍在 90 秒宽限外，
# 但 last_active 保持当下以免被判离线），再 touch 验证「在线期间不重置起点」
import sqlite3 as _sq  # noqa: E402
_conn = _sq.connect(phone_mon.DB_PATH)
_conn.execute("UPDATE phone_monitor SET v=? WHERE k='session_start'", (str(now - 600),))
_conn.commit(); _conn.close()
phone_mon.touch(now, source="webhook")
s2 = phone_mon.state()
check("二次 touch 会话起点不变", abs(s2["session_start"] - (now - 600)) < 2, s2["session_start"])
check("连续时长约 600 秒", abs(s2["continuous_seconds"] - 600) < 5, s2["continuous_seconds"])
check("来源更新为 webhook", s2["last_source"] == "webhook")
phone_mon.mark_offline(now + 1)
s3 = phone_mon.state()
check("mark_offline 后离线", s3["online"] is False, s3)
check("离线后连续时长归零", s3["continuous_seconds"] == 0)
phone_mon.set_alert_info("http://example/alert", enabled=True)
s4 = phone_mon.state()
check("告警地址缓存", s4["alert_url"] == "http://example/alert")
check("networkAlertEnabled 缓存", s4["network_alert_enabled"] is True)

print("\n=== 5. wx_client：告警 / 文件协议路径 ===")
import wx_client  # noqa: E402
calls = []


class FakeSession:
    trust_env = True

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        calls.append((method, url, params, json))
        return _Resp()

    def __getattr__(self, k):
        return lambda *a, **kw: None


class _Resp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"ok": True}


c = wx_client.WXClient("http://192.0.2.71:18888", token="t")
c._session = FakeSession()
# 实测新图 info=1 阻塞约 12s，默认超时必须 >12s，否则新图必被误判 pending
import inspect as _inspect
check("media_info 默认超时>=15s",
      _inspect.signature(wx_client.WXClient.media_info).parameters["timeout"].default >= 15,
      _inspect.signature(wx_client.WXClient.media_info).parameters["timeout"].default)
c.set_alert("http://server/alert", test=True)
check("set_alert 打 /api/alert", calls[-1][1].endswith("/api/alert"), calls[-1])
check("set_alert body 带 url+test", calls[-1][3].get("url") == "http://server/alert" and calls[-1][3].get("test") == 1)
c.set_alert_status = None  # noqa
try:
    c.alert_status()
    check("alert_status 打 GET /api/alert", calls[-1][0] == "GET" and calls[-1][1].endswith("/api/alert"))
except Exception as e:
    check("alert_status 打 GET /api/alert", False, e)

print("\n=== 6. 插件门面透传 ===")
from pluginhost.context import PluginContext, _BaseContext  # noqa: E402


class FakeHost:
    def __init__(self):
        self.got = None

    def recent_media(self, **kw):
        self.got = kw
        return [{"kind": "image"}]


h = FakeHost()
ctx = PluginContext(m=mimg, kind="image", host=h, log=None, registry=None)
res = ctx.recent_media(wxid="wxid_abc", minutes=10)
check("ctx.recent_media 返回结果", res == [{"kind": "image"}])
check("ctx.recent_media 透传参数", h.got and h.got["wxid"] == "wxid_abc" and h.got["minutes"] == 10, h.got)
base = _BaseContext(plugin_name="X", host=h, log=None, registry=None)
check("_BaseContext 也有 recent_media", callable(getattr(base, "recent_media", None)))

print("\n=== 7. 图片画质轮询：0/5/10/20 检查点，高清提前下、低清到上限接受 ===")
_old_wait, _old_poll = media_store.CFG["image_wait"], media_store.CFG["image_poll"]
_old_sched = media_store.IMAGE_PROBE_SCHEDULE
# 测试用短检查点 0/1/2/3 对应生产 0/5/10/20（等满 4 次约 3 秒）
media_store.IMAGE_PROBE_SCHEDULE = (0, 1, 2, 3)
media_store.CFG["image_wait"], media_store.CFG["image_poll"] = 6, 1


class PendingThenMiddleWX(FakeWX):
    """前两次 pending（图还没落地），第三次升到 middle，验证轮询能等到。"""
    def __init__(self):
        super().__init__()
        self.probes = 0

    def media_info(self, chat, local_id):
        self.probes += 1
        if self.probes < 3:
            return {"_state": "pending", "ok": False}
        return {"_state": "ok", "ok": True, "tier": "middle",
                "width": 1000, "height": 2000}


uwx = PendingThenMiddleWX()
ok3, info3 = media_store.ingest(mimg, uwx)
check("pending后升高清入库成功", ok3 is True, info3)
check("pending探测次数>=3（等到升级）", uwx.probes >= 3, uwx.probes)
check("记录到升级后的 middle 档位", info3.get("tier") == "middle", info3.get("tier"))
check("分辨率取升级后的值", info3.get("width") == 1000 and info3.get("height") == 2000,
      (info3.get("width"), info3.get("height")))


class AlwaysThumbWX(FakeWX):
    """明确 thumb：按用户要求仍等到 20s 上限（测试里 4 个检查点）才接受当前档位。"""
    def __init__(self):
        super().__init__()
        self.probes = 0

    def media_info(self, chat, local_id):
        self.probes += 1
        return {"_state": "ok", "ok": True, "tier": "thumb",
                "width": 120, "height": 240}


twx = AlwaysThumbWX()
ok4, info4 = media_store.ingest(mimg, twx)
check("低清仍入库", ok4 is True, info4)
check("低清等到全部检查点（4次）", twx.probes == 4, twx.probes)
check("记录低清 thumb 档位", info4.get("tier") == "thumb", info4.get("tier"))


class MiddleAtLastCheckWX(FakeWX):
    """前 3 个检查点都是 preview，最后一个才升 middle——必须查到最后并拿到高清。"""
    def __init__(self):
        super().__init__()
        self.probes = 0

    def media_info(self, chat, local_id):
        self.probes += 1
        if self.probes < 4:
            return {"_state": "ok", "ok": True, "tier": "preview",
                    "width": 300, "height": 400}
        return {"_state": "ok", "ok": True, "tier": "middle",
                "width": 1280, "height": 2112}


mwx = MiddleAtLastCheckWX()
okm, infom = media_store.ingest(mimg, mwx)
check("最后检查点升高清仍入库", okm is True, infom)
check("查到最后检查点（4次）", mwx.probes == 4, mwx.probes)
check("拿到最后出现的 middle", infom.get("tier") == "middle", infom.get("tier"))
media_store.CFG["image_wait"], media_store.CFG["image_poll"] = _old_wait, _old_poll

print("\n=== 8. 空档位不再秒存缩略图（关键回归）===")
# 真实故障：图刚到时插件 info=1 返回 ok:true 但 tier 为空。旧代码把空档
# 当「高清就绪」立即 break → 存下缩略图。新代码必须继续轮询到 middle。
media_store.CFG["image_wait"], media_store.CFG["image_poll"] = 4, 1


class EmptyThenMiddleWX(FakeWX):
    def __init__(self):
        super().__init__()
        self.probes = 0

    def media_info(self, chat, local_id):
        self.probes += 1
        if self.probes == 1:
            return {"_state": "ok", "ok": True, "tier": "", "width": 0, "height": 0}
        return {"_state": "ok", "ok": True, "tier": "middle",
                "width": 1000, "height": 2000}


ewx = EmptyThenMiddleWX()
oke, infoe = media_store.ingest(mimg, ewx)
check("空档位不秒退（探测>=2次）", ewx.probes >= 2, ewx.probes)
check("空档位后拿到 middle", infoe.get("tier") == "middle", infoe.get("tier"))

print("\n=== 9. 后台复查：低清落盘后升到高清则原地升级 ===")
_old_delays = media_store.UPGRADE_DELAYS
media_store.UPGRADE_DELAYS = (1,)  # 测试用短延迟
media_store.CFG["image_wait"], media_store.CFG["image_poll"] = 6, 1

# 说明：IMAGE_PROBE_SCHEDULE 仍为 §7 设的 (0,1,2,3)，ingest 会查满 4 次。
# 让前 4 次都低清，复查（第 5 次起）才升 middle，才能验证后台升级路径。


class LateUpgradeWX(FakeWX):
    """前几次探测 tier 一直空（保存低清），延迟复查时才 middle；二进制也随之变清晰。"""
    def __init__(self):
        super().__init__()
        self.info_probes = 0
        self.media_calls = 0

    def media_info(self, chat, local_id):
        self.info_probes += 1
        if self.info_probes <= 4:
            return {"_state": "ok", "ok": True, "tier": "", "width": 0, "height": 0}
        return {"_state": "ok", "ok": True, "tier": "middle",
                "width": 1280, "height": 2112}

    def media(self, media_api, chat, local_id):
        self.media_calls += 1
        if self.media_calls == 1:
            return b"THUMB-smallsize", "jpg"
        return b"ORIGINAL-HIGH-RESOLUTION-content-0123456789", "jpg"


lwx = LateUpgradeWX()
okl, infol = media_store.ingest(mimg, lwx)
check("低清先落库", okl is True and infol.get("tier") in ("", "thumb", "preview"),
      infol.get("tier"))
old_hash = infol.get("hash_name")
time.sleep(2.5)  # 等后台复查线程跑完（1s 延迟）
rows = media_store.query(chat="123456@chatroom", minutes=30, kind="image")
upgraded = [r for r in rows if r.get("tier") == "middle" and r.get("hash_name") != old_hash]
check("后台升级后流水 tier=middle", len(upgraded) >= 1,
      [(r.get("tier"), r.get("size")) for r in rows])
if upgraded:
    up = upgraded[0]
    check("后台升级后文件已替换为高清内容", up.get("size") == len(b"ORIGINAL-HIGH-RESOLUTION-content-0123456789"),
          up.get("size"))
    check("旧缩略图文件已删除", not os.path.isfile(os.path.join(media_store.CFG["images_dir"], old_hash)))

print("\n=== 10. info 一直超时/失败，但二进制变大也能兜底升级 ===")
media_store.UPGRADE_DELAYS = (1,)  # 单点复查（info 失败时兜底逻辑仅在最后一点触发）
media_store.CFG["image_wait"], media_store.CFG["image_poll"] = 2, 1


class InfoFailBiggerWX(FakeWX):
    """info 永远超时；二进制第一次是小缩略图，复查时变成 3 倍大的清晰图。"""
    def __init__(self):
        super().__init__()
        self.media_calls = 0

    def media_info(self, chat, local_id):
        return {"_state": "error", "_error": "timeout"}

    def media(self, media_api, chat, local_id):
        self.media_calls += 1
        if self.media_calls == 1:
            return b"tiny-thumb", "jpg"
        return (b"HD" * 500), "jpg"  # 1000 bytes >> tiny 的 2 倍


bfwx = InfoFailBiggerWX()
okb, infob = media_store.ingest(mimg, bfwx)
check("info失败仍先落库", okb is True, infob)
old_hash_b = infob.get("hash_name")
time.sleep(2.8)  # 等两次复查
rows_b = media_store.query(chat="123456@chatroom", minutes=30, kind="image")
bigger = [r for r in rows_b if r.get("hash_name") != old_hash_b and r.get("size") == 1000]
check("info失败但二进制变大→兜底升级", len(bigger) >= 1,
      [(r.get("tier"), r.get("size")) for r in rows_b])

media_store.UPGRADE_DELAYS = _old_delays
media_store.IMAGE_PROBE_SCHEDULE = _old_sched
media_store.CFG["image_wait"], media_store.CFG["image_poll"] = _old_wait, _old_poll

print("\n=== 11. 检查点调度：按 image_wait 截断 ===")
_sched_old = media_store.IMAGE_PROBE_SCHEDULE
media_store.IMAGE_PROBE_SCHEDULE = (0, 5, 10, 20)
check("默认 20s → 0/5/10/20 四个点",
      media_store._probe_schedule(20) == [0, 5, 10, 20],
      media_store._probe_schedule(20))
check("0s → 只探测一次",
      media_store._probe_schedule(0) == [0],
      media_store._probe_schedule(0))
check("7s → 截到 0/5",
      media_store._probe_schedule(7) == [0, 5],
      media_store._probe_schedule(7))
check("3s → 至少有一个点 0",
      media_store._probe_schedule(3) == [0],
      media_store._probe_schedule(3))
media_store.IMAGE_PROBE_SCHEDULE = _sched_old

print("\n=== 12. 引用图片（新接口 quoteResolved）：下载原图 / 记录引用者 ===")
QUOTE_MD5 = "fedcba9876543210fedcba9876543210"
QUOTE_MD5_B = "1234abcd5678ef901234abcd5678ef90"
quote_content = '&lt;msg&gt;&lt;img md5="%s" aeskey="k" /&gt;&lt;/msg&gt;' % QUOTE_MD5


def make_quote_payload(md5, quote_type=3, with_qr=True, found=True,
                       local_id=4321, svrid="999"):
    content = '&lt;msg&gt;&lt;img md5="%s" /&gt;&lt;/msg&gt;' % md5
    qr = {}
    if with_qr:
        qr = ({"found": True, "resolvedBy": "svrid", "quoteType": quote_type,
               "localID": local_id,
               "mediaApi": "/api/media?chat=88888@chatroom&localID=%d" % local_id,
               "mediaApiHD": "/api/media?chat=88888@chatroom&localID=%d&hd=1" % local_id}
              if found else
              {"found": False, "resolvedBy": "none", "quoteType": quote_type,
               "hint": "not found"})
    return {
        "chat": "88888@chatroom", "chatType": "group", "isGroup": True,
        "chatName": "引用测试群", "sender": "wxid_quoter", "senderName": "引用者",
        "type": 49, "appType": "quote", "localID": local_id, "ts": 1700000999,
        "content": "&lt;msg&gt;&lt;refermsg&gt;...&lt;/refermsg&gt;&lt;/msg&gt;",
        "svrid": "outer-svrid",
        "quote": {"type": quote_type, "typeName": "image" if quote_type == 3 else "app",
                  "svrid": svrid, "fromUsr": "wxid_original_author",
                  "displayName": "原图作者", "createTime": 1700000123,
                  "content": content},
        "quoteResolved": qr,
    }


class QuoteWX(FakeWX):
    """带 media_info；插件侧普通档位返回 middle（不带 hd 探测）。"""
    def __init__(self):
        super().__init__()
        self.info_probes = []
        self.msg_calls = []

    def media_info(self, chat, local_id, timeout=18):
        self.info_probes.append((chat, local_id))
        return {"_state": "ok", "ok": True,
                "tier": "middle",
                "width": 1280, "height": 1707, "bytes": 84504}

    def media(self, media_api, chat, local_id):
        self.media_calls.append((media_api, chat, local_id))
        return (b"\x89PNG\r\n\x1a\n-MIDDLE-TIER-IMAGE-", "png")

    def messages(self, chat=None, limit=20):
        self.msg_calls.append((chat, limit))
        return [{
            "localID": 4322,
            "quote": {"svrid": "999", "type": 3, "content": quote_content},
            "quoteResolved": {"found": True, "resolvedBy": "svrid", "quoteType": 3,
                              "localID": 4322,
                              "mediaApi": "/api/media?chat=88888@chatroom&localID=4322"},
        }]


# policy 保留新字段
mq = policy.normalize(make_quote_payload(QUOTE_MD5))
check("normalize 保留 quote", mq.get("quote", {}).get("type") == 3, mq.get("quote"))
check("normalize 保留 quote_resolved", mq.get("quote_resolved", {}).get("found") is True,
      mq.get("quote_resolved"))
check("normalize 保留 svrid", mq.get("svrid") == "outer-svrid", mq.get("svrid"))
check("is_media_ready_for_quote 命中图片引用",
      media_store.is_media_ready_for_quote(mq) is True)
check("非图片引用不处理",
      media_store.is_media_ready_for_quote(
          policy.normalize(make_quote_payload(QUOTE_MD5, quote_type=49))) is False)

qwx = QuoteWX()
okq, infoq = media_store.ingest_quoted_image(mq, qwx, log=lambda *a, **kw: None)
check("引用图片下载成功", okq is True, infoq)
check("用 quoteResolved.localID 下载", qwx.media_calls and qwx.media_calls[-1][2] == 4321,
      qwx.media_calls)
check("下载地址不带 hd=1",
      qwx.media_calls and "hd=1" not in qwx.media_calls[-1][0], qwx.media_calls)
check("画质探测不带 hd 参数", bool(qwx.info_probes), qwx.info_probes)
check("记录 tier=middle（普通档位）", infoq.get("tier") == "middle", infoq.get("tier"))
check("记录 via=quote", infoq.get("via") == "quote", infoq.get("via"))
check("发送者=引用者（非原图作者）", infoq.get("wxid") == "wxid_quoter", infoq.get("wxid"))
check("发送者名=引用者", infoq.get("sender_name") == "引用者", infoq.get("sender_name"))
check("发送时间=引用时间（非原图时间）", infoq.get("sent_at") == 1700000999,
      infoq.get("sent_at"))
check("发送时间不是原图 createTime", infoq.get("sent_at") != 1700000123,
      infoq.get("sent_at"))
check("文件名带 md5 前缀", infoq.get("orig_name", "").startswith(QUOTE_MD5[:16]),
      infoq.get("orig_name"))
check("落盘 hash 文件正确",
      infoq.get("hash_name") == hashlib.sha256(
          b"\x89PNG\r\n\x1a\n-MIDDLE-TIER-IMAGE-").hexdigest() + ".png",
      infoq.get("hash_name"))
check("引用图文件已落盘",
      os.path.isfile(os.path.join(media_store.CFG["images_dir"], infoq["hash_name"])))
qrows = media_store.query(chat="88888@chatroom", minutes=30, kind="image")
check("查询含引用记录及引用发送时间",
      any(r.get("via") == "quote" and r.get("sent_time") for r in qrows),
      [(r.get("via"), r.get("sent_time")) for r in qrows])
# 重复推送去重
okq2, infoq2 = media_store.ingest_quoted_image(mq, qwx, log=lambda *a, **kw: None)
check("引用图重复推送被去重", okq2 is False and "duplicate" in str(infoq2), infoq2)
# 推送未带 quoteResolved → 直接跳过，不回查 /api/messages
mq_fb = policy.normalize(make_quote_payload(QUOTE_MD5_B, with_qr=False, local_id=4322))
qwx2 = QuoteWX()
okq3, infoq3 = media_store.ingest_quoted_image(mq_fb, qwx2, log=lambda *a, **kw: None)
check("缺 quoteResolved 时跳过（不下载）", okq3 is False and "not-resolved" in str(infoq3),
      infoq3)
check("缺 quoteResolved 时不回查 messages", not qwx2.msg_calls, qwx2.msg_calls)
check("缺 quoteResolved 时不发下载请求", not qwx2.media_calls, qwx2.media_calls)
# mediaApi 自带 hd=1 也要剥掉
mq_hd = policy.normalize(make_quote_payload("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                                            local_id=4355, svrid="1001"))
mq_hd["quote_resolved"] = dict(mq_hd["quote_resolved"])
mq_hd["quote_resolved"]["mediaApi"] = "/api/media?chat=88888@chatroom&localID=4355&hd=1"
qwx3 = QuoteWX()
okq5, infoq5 = media_store.ingest_quoted_image(mq_hd, qwx3, log=lambda *a, **kw: None)
check("mediaApi 带 hd=1 时自动改写", okq5 is True, infoq5)
check("改写后下载地址无 hd=1",
      qwx3.media_calls and "hd=1" not in qwx3.media_calls[-1][0], qwx3.media_calls)
# found=false → 失败
mq_no = policy.normalize(make_quote_payload("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                            local_id=4399, found=False))
okq4, infoq4 = media_store.ingest_quoted_image(mq_no, QuoteWX(), log=lambda *a, **kw: None)
check("quoteResolved.found=false 时跳过", okq4 is False and "not-resolved" in str(infoq4),
      infoq4)

print("\n=== 结果 ===")
print("PASS:", len(OK), "FAIL:", len(FAIL))
if FAIL:
    print("失败项：", FAIL)
    sys.exit(1)
print("全部通过 ✅")
