"""入站图片 / 文件存储与检索（1.1.1-beta1 新增）。

职责（对应需求 2/3/4/5/6）：
  * 群聊 / 私聊的图片（type=3）下载到 data/images，文件（type=49 + appType=file）
    下载到 data/files；按内容哈希命名，哈希一致则跳过重复写盘。
  * 磁盘文件最多保留 7 天，过期自动删除（按 mtime）。
  * 每次下载（含哈希命中跳过的）记一条流水到独立库 data/media.db，
    流水与磁盘文件同寿（默认 7 天，record_ttl），支持「全部 / 最近 N 分钟」
    两种时间范围与分页检索。
  * 提供 query() / query_page()：按群聊名 / 群聊号 / wxid / 会话类型 / 类型 /
    时间范围（全部或最近 N 分钟）快速检索；插件门面走 query()，管理端走分页版。

设计要点：
  * 独立 SQLite（data/media.db，WAL），与 analytics.db / chatlog.db 解耦：
    这份数据按天失效（默认 7 天），生命周期与长期业务库不同，不污染业务库。
  * 惰性建表 + 节流清理：首次写/查时建表，清理每 purge_interval 秒至多一次，
    不给每条消息增加 IO 负担。
  * 下载本身是网络 IO，在 webhook 线程池内调用；失败只记日志，绝不影响
    消息回复链路。
"""
import hashlib
import heapq
import itertools
import os
import re
import sqlite3
import threading
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

DEFAULT_INDEX_TTL = 1800      # 兼容旧配置（历史「流水保留 30 分钟」语义已废弃）
DEFAULT_FILE_TTL = 7 * 86400  # 磁盘文件保留 7 天（秒）
DEFAULT_RECORD_TTL = 7 * 86400  # 下载流水保留 7 天（与磁盘文件同寿）
DEFAULT_PURGE_INTERVAL = 60   # 清理节流（秒）
# 图片画质轮询（用户指定节奏）：先查一次 info=1，再在 5s/10s/20s 各查一次；
# 档位升到 middle/original 就立即下载，到 20s 上限仍低清就下载当前能拿到的
# 任何档位（不再无限等）。注意：新图 info=1 本身可能阻塞约 12s，调度会跳过
# 已被这次探测自然越过的中间检查点，直接落到最后一个，避免密集连查。
DEFAULT_IMAGE_WAIT = 20       # 最长等待秒数（最后一个检查点），0=不等待
DEFAULT_IMAGE_POLL = 2        # 兼容旧配置；现由固定检查点驱动，不再使用
IMAGE_PROBE_SCHEDULE = (0, 5, 10, 20)  # 相对首次探测的检查点（秒）；测试可覆写
# 低画质落盘后的延迟复查点（秒，相对保存时刻）：用户可能过几分钟才在微信里
# 点开那张图，缓存才升级为 middle。在这些时间点重探，若升到高清且内容哈希变了
# 就替换文件、原地更新流水（不新增记录、不破坏 30 分钟窗口）。测试可覆写。
UPGRADE_DELAYS = (15, 45, 90, 180, 300, 480)
_HIGH_TIERS = ("original", "middle")


def _probe_schedule(wait_max):
    """按 image_wait 截断检查点；保证至少探测一次（t=0）。"""
    pts = [s for s in IMAGE_PROBE_SCHEDULE if 0 <= s <= wait_max]
    if not pts or pts[0] != 0:
        pts = [0] + pts
    return pts

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    downloaded_at  REAL NOT NULL,
    chat           TEXT NOT NULL,
    chat_type      TEXT NOT NULL DEFAULT 'group',
    chat_name      TEXT NOT NULL DEFAULT '',
    wxid           TEXT NOT NULL DEFAULT '',
    sender_name    TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL DEFAULT 'image',
    orig_name      TEXT NOT NULL DEFAULT '',
    hash_name      TEXT NOT NULL DEFAULT '',
    sha256         TEXT NOT NULL DEFAULT '',
    ext            TEXT NOT NULL DEFAULT '',
    size           INTEGER NOT NULL DEFAULT 0,
    rel_path       TEXT NOT NULL DEFAULT '',
    dedup          INTEGER NOT NULL DEFAULT 0,
    tier           TEXT NOT NULL DEFAULT '',      -- 图片画质：original/middle/preview/thumb
    width          INTEGER NOT NULL DEFAULT 0,
    height         INTEGER NOT NULL DEFAULT 0,
    sent_at        REAL NOT NULL DEFAULT 0,       -- 消息发送时间（引用图=引用消息发送时间）
    via            TEXT NOT NULL DEFAULT 'direct' -- direct 直接消息 / quote 引用消息带图
);
CREATE INDEX IF NOT EXISTS idx_media_time ON media_items(downloaded_at);
CREATE INDEX IF NOT EXISTS idx_media_chat ON media_items(chat, wxid, downloaded_at);
CREATE INDEX IF NOT EXISTS idx_media_hash ON media_items(sha256);
"""

_LOCK = threading.Lock()
_DB_PATH = None
_LAST_PURGE = 0.0
CFG = {
    "index_ttl": DEFAULT_INDEX_TTL,     # 兼容旧配置，已不再用于删除流水
    "file_ttl": DEFAULT_FILE_TTL,
    "record_ttl": DEFAULT_RECORD_TTL,
    "images_dir": os.path.join(DATA_DIR, "images"),
    "files_dir": os.path.join(DATA_DIR, "files"),
    "image_wait": DEFAULT_IMAGE_WAIT,
    "image_poll": DEFAULT_IMAGE_POLL,
}


def configure(**kw):
    """由 webhook_server 用 config.yaml 的 media_store 段覆盖默认值。"""
    for k in ("index_ttl", "file_ttl", "record_ttl", "images_dir", "files_dir",
              "image_wait", "image_poll"):
        if k in kw and kw[k] not in (None, ""):
            CFG[k] = kw[k]
    # 未显式配置 record_ttl 时，流水与磁盘文件同寿（默认 7 天）
    if "record_ttl" not in kw or kw.get("record_ttl") in (None, ""):
        CFG["record_ttl"] = CFG["file_ttl"]
    for k in ("index_ttl", "file_ttl", "record_ttl", "image_wait", "image_poll"):
        try:
            CFG[k] = int(CFG[k])
        except (TypeError, ValueError):
            CFG[k] = {"index_ttl": DEFAULT_INDEX_TTL, "file_ttl": DEFAULT_FILE_TTL,
                      "record_ttl": DEFAULT_RECORD_TTL,
                      "image_wait": DEFAULT_IMAGE_WAIT,
                      "image_poll": DEFAULT_IMAGE_POLL}[k]


def db_path():
    global _DB_PATH
    if _DB_PATH is None:
        _DB_PATH = os.environ.get("WXBOT_MEDIA_DB") or os.path.join(DATA_DIR, "media.db")
    return _DB_PATH


def _connect():
    p = db_path()
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(p, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _ensure(conn):
    conn.executescript(SCHEMA)
    # 旧库迁移：1.1.1-beta1 首版没有 tier/width/height，补列（幂等）。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(media_items)")}
    for name, ddl in (("tier", "TEXT NOT NULL DEFAULT ''"),
                      ("width", "INTEGER NOT NULL DEFAULT 0"),
                      ("height", "INTEGER NOT NULL DEFAULT 0"),
                      ("sent_at", "REAL NOT NULL DEFAULT 0"),
                      ("via", "TEXT NOT NULL DEFAULT 'direct'")):
        if name not in cols:
            try:
                conn.execute("ALTER TABLE media_items ADD COLUMN %s %s" % (name, ddl))
            except sqlite3.OperationalError:
                pass


def _dir_for(kind):
    return CFG["images_dir"] if kind == "image" else CFG["files_dir"]


def _root_for_rel(prefix, kind):
    """rel_path 首段是落盘目录名，映射回配置目录（兼容 images/files 被自定义）。"""
    for d in (CFG["images_dir"], CFG["files_dir"]):
        if os.path.basename(os.path.normpath(d)) == prefix:
            return d
    return _dir_for(kind)


def is_image(m):
    try:
        return int(m.get("type") or 0) == 3 or (m.get("type_name") or "").lower() == "image"
    except (TypeError, ValueError):
        return False


def is_file(m):
    """文件消息：type=49 的应用消息，appType 属于文件类。

    微信把文件作为应用消息下发，实测 appType 至少有两种取值：
      * file            已就绪的文件
      * file_uploading  刚收到、插件还在异步落地的文件
    只认 file 会漏掉 file_uploading——而 webhook 通常只推一次，漏了就再
    没有第二次机会。因此这里同时接受 file / file_* 前缀，并对带文件名或
    md5 元信息、又不属于 quote/link/music 等已知非文件类型的应用消息兜底。
    """
    try:
        t = int(m.get("type") or 0)
    except (TypeError, ValueError):
        return False
    if t != 49:
        return False
    app = (m.get("app_type") or "").lower()
    if app == "file" or app.startswith("file_"):
        return True
    # 兜底：部分插件版本 appType 不规范，但带文件元信息即可确认是文件。
    if (m.get("file_name") or m.get("file_md5")) and app not in (
            "quote", "link", "url", "music", "miniapp", "chatlog"):
        return True
    return False


def is_media(m):
    return is_image(m) or is_file(m)


def _orig_name(m, kind, ext):
    """原始图片名 / 文件名：优先 fileName，其次是正文（uploading 阶段
    payload 常把文件名放在 text/content 里），最后用 kind+localID 兜底。"""
    name = str(m.get("file_name") or m.get("origin_name") or "").strip()
    if not name and kind == "file":
        # file_uploading 消息 text 通常就是文件名，如「xxx.pdf」
        for key in ("text", "content"):
            cand = str(m.get(key) or "").strip()
            if cand and "." in cand and len(cand) < 200:
                name = cand
                break
    if name:
        return name
    return "%s_%s.%s" % (kind, m.get("local_id") or int(time.time()), ext)


def _ext_for(kind, mime_ext, orig_name):
    if mime_ext and mime_ext != "bin":
        return mime_ext.lstrip(".").lower()
    name = str(orig_name or "")
    if "." in name:
        ext = name.rsplit(".", 1)[-1].lower()
        if ext and len(ext) <= 12 and ext.isalnum():
            return ext
    return "jpg" if kind == "image" else "bin"


def _plain_media_api(media_api, chat, local_id):
    """统一下载地址：图片一律走普通档位，绝不带 hd=1。

    插件/推送给的 mediaApi 理论上已是普通档位，但仍按参数逐字兜底一次：
    地址缺失、或地址里带 hd 参数，就按 chat + localID 重新拼一个无 hd 的。
    """
    api = str(media_api or "")
    if not api or "hd=" in api or "hd%3D" in api:
        return "/api/media?chat=%s&localID=%s" % (chat, local_id)
    return api


def ingest(m, wx, log=None, override=None):
    """下载一条媒体消息并落记录。失败返回 (False, reason)，成功 (True, info)。

    在 webhook 线程池内同步调用；下载是网络 IO，耗时可能数秒，但绝不抛异常到
    消息回复链路。哈希命中的文件跳过写盘，但仍记一条流水（dedup=1）。

    override：dict，可覆盖 rec 的 wxid/sender_name/orig_name/sent_at/via，
    以及 m 的 media_api（引用图的下载地址）。用于「引用图片带图」场景；
    普通媒体消息不传（None）。

    画质策略：一律下载普通档位（不带 hd=1 的链接）。
    """
    _log = log or (lambda *a, **kw: None)
    override = override or {}
    # 引用图片用 quoteResolved.mediaApi 覆盖下载地址（普通档位，不带 hd=1）
    if override.get("media_api"):
        m = dict(m)
        m["media_api"] = override["media_api"]
    if not is_media(m):
        return False, "not-media"
    kind = "image" if is_image(m) else "file"
    chat = str(m.get("chat") or "").strip()
    if not chat:
        return False, "no-chat"
    local_id = m.get("local_id") or 0
    if not local_id:
        return False, "no-local-id"
    # 图片画质轮询（用户指定节奏）：
    #   立即查一次 info=1，然后在 5s/10s/20s 三个时间点各再查一次；
    #   只要档位是 middle/original 就立刻下载最高档；到 20s 上限仍拿不到
    #   高清，就下载当前能拿到的任何档位（有图就存，不再无限等）。
    #   新图 info=1 本身可能阻塞约 12s，若这次探测已跨过后续中间检查点，
    #   就跳过它们直接落到最后一个，避免退化成连查。
    tier, width, height = "", 0, 0
    probe_log = []
    if kind == "image" and hasattr(wx, "media_info"):
        try:
            wait_max = max(0, int(CFG["image_wait"]))
            pts = _probe_schedule(wait_max)
            start = time.monotonic()
            info = {}
            i = 0
            while i < len(pts):
                elapsed = time.monotonic() - start
                if elapsed < pts[i]:
                    time.sleep(pts[i] - elapsed)
                info = wx.media_info(chat, local_id) or {}
                state = info.get("_state") or ("ok" if info.get("ok") else "pending")
                t = str(info.get("tier") or "")
                elapsed = time.monotonic() - start
                probe_log.append("%.1fs:%s/%s" % (elapsed, state, t or "-"))
                # 只有明确高清才提前结束等待；低清/空档/pending 一律继续等
                # 到下一个检查点，直到 20s 上限接受当前档位。
                if state == "ok" and t in _HIGH_TIERS:
                    break
                i += 1
                # 本次探测耗时已越过后续中间检查点 → 跳过，直接落最后一个
                while i < len(pts) - 1 and (time.monotonic() - start) >= pts[i]:
                    i += 1
            tier = str(info.get("tier") or "")
            width = int(info.get("width") or 0)
            height = int(info.get("height") or 0)
        except Exception:
            pass
    if kind == "image" and probe_log:
        _log("图片画质探测 | chat=%s localID=%s | %s", chat, local_id,
             " → ".join(probe_log))

    # 文件在 file_uploading 阶段插件可能还没落地，/api/file 短暂报 404；
    # 而 webhook 只推一次，失败就永久丢了。所以文件下载做有限次退避重试，
    # 图片用 mediaApi 通常即时可用，不重试（画质等待已在上面完成）。
    raw, mime_ext = None, ""
    attempts = (0.0, 1.5, 3.0, 5.0) if kind == "file" else (0.0,)
    last_err = None
    for delay in attempts:
        if delay:
            time.sleep(delay)
        try:
            if kind == "image":
                # 一律用普通档位地址（不含 hd=1），避免下到插件侧的高清原图
                raw, mime_ext = wx.media(
                    _plain_media_api(m.get("media_api"), chat, local_id), chat, local_id)
            else:
                raw, mime_ext = wx.file(chat, local_id)
            if raw:
                break
        except Exception as e:
            last_err = e
    if not raw:
        _log("媒体下载失败 | kind=%s | chat=%s | localID=%s | 重试%d次 | %s",
             kind, chat, local_id, len(attempts), last_err or "空内容")
        return False, "download-failed: %s" % (last_err or "empty")

    sha = hashlib.sha256(raw).hexdigest()
    ext = _ext_for(kind, mime_ext, m.get("file_name"))
    hash_name = "%s.%s" % (sha, ext)
    orig = _orig_name(m, kind, ext)
    folder = _dir_for(kind)
    os.makedirs(folder, exist_ok=True)
    full = os.path.join(folder, hash_name)
    dedup = 0
    if os.path.isfile(full) and os.path.getsize(full) == len(raw):
        dedup = 1
    else:
        tmp = full + ".part"
        try:
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, full)
        except Exception as e:
            _log("媒体落盘失败 | %s | %s", full, e)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False, "write-failed: %s" % e

    now = time.time()
    # 只存「目录名/哈希文件名」这种与盘符无关的相对路径：images_dir / files_dir
    # 可能在任意盘（Docker 里是 /app/data，测试里可能是临时目录），用
    # os.path.relpath(full, BASE_DIR) 会在跨盘符时抛 ValueError。
    rel_path = "%s/%s" % (os.path.basename(os.path.normpath(folder)), hash_name)
    rec = {
        "downloaded_at": now, "chat": chat,
        "chat_type": m.get("chat_type") or ("group" if m.get("is_group") else "private"),
        "chat_name": str(m.get("chat_name") or chat),
        "wxid": str(m.get("sender") or ""),
        "sender_name": str(m.get("sender_name") or ""),
        "kind": kind, "orig_name": orig, "hash_name": hash_name,
        "sha256": sha, "ext": ext, "size": len(raw),
        "rel_path": rel_path, "dedup": dedup,
        "tier": tier, "width": width, "height": height,
        # 发送时间：普通媒体用消息 ts；引用图片由 override 改成引用消息的 ts
        "sent_at": float(m.get("ts") or 0),
        "via": "direct",
    }
    # 引用图片的覆盖字段：发送者 / 显示名 / 时间 / 文件名 / 来源
    for k in ("wxid", "sender_name", "orig_name", "sent_at", "via"):
        if k in override and override[k] not in (None, ""):
            rec[k] = override[k]
    row_id = None
    try:
        with _LOCK:
            conn = _connect()
            try:
                _ensure(conn)
                cur = conn.execute(
                    "INSERT INTO media_items (downloaded_at,chat,chat_type,chat_name,"
                    "wxid,sender_name,kind,orig_name,hash_name,sha256,ext,size,rel_path,dedup,"
                    "tier,width,height,sent_at,via) "
                    "VALUES (:downloaded_at,:chat,:chat_type,:chat_name,:wxid,:sender_name,"
                    ":kind,:orig_name,:hash_name,:sha256,:ext,:size,:rel_path,:dedup,"
                    ":tier,:width,:height,:sent_at,:via)", rec)
                row_id = cur.lastrowid
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        _log("媒体流水写入失败 | %s | %s", hash_name, e)
        return False, "db-failed: %s" % e

    _log("媒体已保存%s | %s | %s | %s | %d bytes | %s%s",
         "（哈希命中跳过写盘）" if dedup else "", kind,
         rec["chat_name"], orig, len(raw), hash_name[:16],
         (" | 画质=%s%s" % (tier, " %dx%d" % (width, height) if width and height else ""))
         if tier else "")

    # 低画质图片：后台延迟复查。若稍后插件补齐了高清（middle/original），
    # 重新下载并原地升级这条流水，保证最终拿到的是最清晰的可用版本。
    if kind == "image" and row_id and tier not in _HIGH_TIERS and hasattr(wx, "media_info"):
        _schedule_upgrade(row_id, chat, local_id, m.get("media_api"), rec, wx, _log)

    maybe_purge(_log)
    return True, rec


def _quote_md5(quote):
    """从 quote.content（被引图片的 XML，可能多重转义）里取 md5，作稳定文件名前缀。"""
    import html as _html
    content = str((quote or {}).get("content") or "")
    for _ in range(6):
        n = _html.unescape(content)
        if n == content:
            break
        content = n
    hit = re.search(r'\bmd5\s*=\s*["\']?([0-9a-fA-F]{16,})', content)
    return hit.group(1).lower() if hit else ""


def _quote_resolved(m):
    """取引用消息的 quoteResolved（文档 7.2.1）。

    只认 webhook 推送自带的这一份。不再用 svrid / localID 回查 /api/messages
    ——文档 247 行与 489 行的说法冲突，按「推送必带 quoteResolved」为准。
    """
    qr = m.get("quote_resolved") or {}
    return qr if isinstance(qr, dict) else {}


def ingest_quoted_image(m, wx, log=None, ignore_dedup_minutes=30):
    """处理「消息里引用了图片」：用推送自带的 quoteResolved 下载被引用图片。

    仅当被引用内容类型 quote.type==3（图片）且 quoteResolved.found 时下载。
    发送者 / 发送时间取外层引用消息 m（发这条引用的人 / 这条引用的时间），
    不是被引用原图的作者与时间；via='quote'。下载走普通档位 mediaApi（不带 hd=1）。
    返回 (ok, info/reason)。
    """
    _log = log or (lambda *a, **kw: None)
    chat = str(m.get("chat") or "").strip()
    if not chat or not is_media_ready_for_quote(m):
        return False, "not-quote-image"

    qr = _quote_resolved(m)
    if not qr or not qr.get("found") or str(qr.get("quoteType")) != "3":
        return False, "quote-not-resolved"
    local_id = int(qr.get("localID") or 0)
    if not local_id:
        return False, "quote-no-local-id"

    quote = m.get("quote") or {}
    md5 = _quote_md5(quote)
    name_key = md5[:16] if md5 else str(quote.get("svrid") or local_id)[-16:]
    orig_name = "%s.jpg" % name_key

    # 近 N 分钟内若已按同 chat + 文件名前缀记过引用图，跳过（引用消息可能多次推送）
    try:
        with _LOCK:
            conn = _connect()
            try:
                _ensure(conn)
                cutoff = time.time() - max(0, int(ignore_dedup_minutes)) * 60
                dup = conn.execute(
                    "SELECT id FROM media_items WHERE chat=? AND via='quote' "
                    "AND orig_name LIKE ? AND downloaded_at>=? LIMIT 1",
                    (chat, name_key + "%", cutoff)).fetchone()
            finally:
                conn.close()
        if dup:
            return False, "quote-duplicate"
    except Exception:
        pass

    # 只用普通档位地址（不带 hd=1）；缺失或带 hd 时按 chat+localID 重拼
    media_api = _plain_media_api(qr.get("mediaApi"), chat, local_id)
    img_msg = {
        "chat": chat,
        "chat_type": m.get("chat_type"),
        "is_group": m.get("is_group"),
        "chat_name": m.get("chat_name"),
        "type": 3, "type_name": "image",
        "local_id": local_id,
        "media_api": media_api,
        "sender": m.get("sender") or "",       # 引用消息的发送者
        "sender_name": m.get("sender_name") or "",
        "ts": m.get("ts") or 0,                 # 引用消息的发送时间
    }
    override = {
        "wxid": str(m.get("sender") or ""),
        "sender_name": str(m.get("sender_name") or ""),
        "sent_at": float(m.get("ts") or 0),
        "orig_name": orig_name,
        "via": "quote",
        "media_api": media_api,
    }
    ok, info = ingest(img_msg, wx, log=_log, override=override)
    if ok:
        _log("引用图片已保存 | chat=%s | 引用者=%s | 引用时间=%s | %s | %s",
             chat, m.get("sender") or "-", m.get("ts") or "-",
             info.get("hash_name"),
             "画质=%s" % info.get("tier") if info.get("tier") else "")
    return ok, info


def is_media_ready_for_quote(m):
    """是否是需要处理引用图片的引用消息：appType=quote 且被引内容 type=3。"""
    if str(m.get("app_type") or "").lower() != "quote":
        return False
    quote = m.get("quote") or {}
    try:
        return int(quote.get("type") or 0) == 3
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- 画质升级复查调度
# 所有待复查图片共用**一个**调度线程，按到期时间排队；不再「每张低清图起一个
# 存活十几分钟的线程」—— 群里连续发图时原实现会把线程数堆到几十上百个。
_UPGRADE_JOBS = []                       # 最小堆：(到期时间戳, 序号, job)
_UPGRADE_CV = threading.Condition()
_UPGRADE_SEQ = itertools.count()
_UPGRADE_THREAD = None


def _enqueue_upgrade(job, due):
    global _UPGRADE_THREAD
    with _UPGRADE_CV:
        heapq.heappush(_UPGRADE_JOBS, (due, next(_UPGRADE_SEQ), job))
        if _UPGRADE_THREAD is None or not _UPGRADE_THREAD.is_alive():
            _UPGRADE_THREAD = threading.Thread(
                target=_upgrade_dispatcher, daemon=True, name="media-upgrade")
            _UPGRADE_THREAD.start()
        _UPGRADE_CV.notify()


def _upgrade_dispatcher():
    """按到期时间依次执行复查；任何异常都不能让调度线程退出。"""
    while True:
        try:
            with _UPGRADE_CV:
                while not _UPGRADE_JOBS:
                    _UPGRADE_CV.wait()
                due, _, job = _UPGRADE_JOBS[0]
                wait = due - time.time()
                if wait > 0:
                    _UPGRADE_CV.wait(wait)   # 期间有新任务入队会提前唤醒，重新排期
                    continue
                heapq.heappop(_UPGRADE_JOBS)
            _upgrade_step(job)
        except Exception:
            time.sleep(1)


def _requeue_upgrade(job):
    """还有后续检查点则按「保存时刻 + 延迟」重新入队，否则结束。"""
    step = job["step"] + 1
    if step >= len(UPGRADE_DELAYS):
        return
    job["step"] = step
    _enqueue_upgrade(job, job["saved_at"] + UPGRADE_DELAYS[step])


def _schedule_upgrade(row_id, chat, local_id, media_api, rec, wx, log):
    """为一张低画质图片安排后台画质升级复查（失败静默）。

    UPGRADE_DELAYS 是相对保存时刻的多个探测点。每次只取元信息，档位升到
    middle/original 才重新拉一次二进制；哈希变化说明内容确实变清晰了，
    落新文件并原地 UPDATE 这条流水（保留下载时间/会话/发送者等原字段），
    旧文件若已无其它流水引用则删除。
    """
    now = time.time()
    job = {"row_id": row_id, "chat": chat, "local_id": local_id,
           "media_api": media_api, "rec": dict(rec or {}), "wx": wx, "log": log,
           "saved_at": now, "step": 0}
    _enqueue_upgrade(job, now + UPGRADE_DELAYS[0])


def _upgrade_step(job):
    """执行一个检查点：探测档位，必要时重下并原地升级。"""
    row_id, chat, local_id = job["row_id"], job["chat"], job["local_id"]
    media_api, rec, wx, log = job["media_api"], job["rec"], job["wx"], job["log"]
    is_last = job["step"] >= len(UPGRADE_DELAYS) - 1
    try:
        info = wx.media_info(chat, local_id) or {}
        state = info.get("_state") or ("ok" if info.get("ok") else "pending")
        t = str(info.get("tier") or "")
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
        info_ok = state == "ok"
        info_high = t in _HIGH_TIERS

        # 拿一次二进制：info 明确升到高清要下；info 失败/超时但缓存可能
        # 已升级时也下一次，用「明显更大」做兜底判断（不依赖 info）。
        if not info_ok and not is_last:
            # info 失败且还有后续复查点，先跳过，避免每次都白发二进制
            _requeue_upgrade(job)
            return
        raw, mime_ext = wx.media(media_api, chat, local_id)
        if not raw:
            _requeue_upgrade(job)
            return
        new_sha = hashlib.sha256(raw).hexdigest()
        bigger = len(raw) > int(rec.get("size") or 0) * 2
        if not info_high and not bigger:
            _requeue_upgrade(job)      # 既没升档、体积也没明显变大 → 还是老缩略图
            return
        if new_sha == rec.get("sha256"):
            # 内容没变，只是之前没探测到档位：补记 tier/分辨率即可
            with _LOCK:
                conn = _connect()
                try:
                    conn.execute(
                        "UPDATE media_items SET tier=?,width=?,height=? WHERE id=?",
                        (t or "middle", width, height, row_id))
                    conn.commit()
                finally:
                    conn.close()
            log("媒体画质补记 | tier=%s %dx%d | chat=%s localID=%s",
                t or "middle", width, height, chat, local_id)
            return
        # 内容确实变清晰：落新文件 + 更新流水
        ext = _ext_for("image", mime_ext, rec.get("orig_name"))
        hash_name = "%s.%s" % (new_sha, ext)
        folder = _dir_for("image")
        os.makedirs(folder, exist_ok=True)
        full = os.path.join(folder, hash_name)
        tmp = full + ".part"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, full)
        rel_path = "%s/%s" % (os.path.basename(os.path.normpath(folder)), hash_name)
        old_hash_name = rec.get("hash_name")
        with _LOCK:
            conn = _connect()
            try:
                conn.execute(
                    "UPDATE media_items SET hash_name=?,sha256=?,ext=?,size=?,"
                    "rel_path=?,tier=?,width=?,height=?,dedup=0 WHERE id=?",
                    (hash_name, new_sha, ext, len(raw), rel_path,
                     t, width, height, row_id))
                # 旧文件若无其它流水引用则删除
                used = conn.execute(
                    "SELECT COUNT(*) AS n FROM media_items WHERE hash_name=?",
                    (old_hash_name,)).fetchone()["n"]
                conn.commit()
            finally:
                conn.close()
        if used <= 0 and old_hash_name:
            try:
                os.remove(os.path.join(folder, old_hash_name))
            except OSError:
                pass
        log("媒体画质已升级 | %s -> %s %dx%d | %d bytes | chat=%s localID=%s",
            rec.get("tier") or "-", t, width, height, len(raw), chat, local_id)
    except Exception as e:
        log("媒体画质升级复查异常（已忽略） | chat=%s localID=%s | %s",
            chat, local_id, e)
        _requeue_upgrade(job)


def _build_where(chat=None, chat_name=None, wxid=None, kind=None,
                 chat_type=None, minutes=30, all_time=False, now=None):
    """拼 WHERE 子句：时间范围 + 会话 / 发送者 / 类型过滤。

    时间范围语义（1.1.1-beta2 起）：
      * all_time=True 或 minutes<=0 → 不加时间条件，即「全部」
      * minutes>0 → 最近 N 分钟
    不再把窗口 clamp 到 30 分钟。
    """
    where, args = [], []
    win = None
    if not all_time:
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = 30
        if minutes > 0:
            win = minutes * 60
    if win:
        where.append("downloaded_at >= ?")
        args.append((now or time.time()) - win)
    if chat:
        where.append("chat = ?")
        args.append(str(chat))
    if chat_name:
        where.append("(chat_name LIKE ? OR sender_name LIKE ?)")
        args.extend(["%%%s%%" % chat_name, "%%%s%%" % chat_name])
    if wxid:
        where.append("wxid = ?")
        args.append(str(wxid))
    if kind in ("image", "file"):
        where.append("kind = ?")
        args.append(kind)
    if chat_type in ("group", "private"):
        where.append("chat_type = ?")
        args.append(chat_type)
    return where, args


def _decorate_row(r):
    """补可读字段：磁盘路径 / 是否可预览 / 时间串 / 画质标签 / 分辨率。"""
    rel = str(r.get("rel_path") or "").replace("\\", "/")
    if "/" in rel:
        prefix, _ = rel.split("/", 1)
        base_dir = _root_for_rel(prefix, r["kind"])
    else:
        base_dir = _dir_for(r["kind"])
    path = os.path.join(base_dir, os.path.basename(rel)) if rel else ""
    r["path"] = path if path and os.path.isfile(path) else ""
    r["available"] = bool(r["path"])
    r["download_time"] = datetime.fromtimestamp(r["downloaded_at"]).strftime(
        "%Y-%m-%d %H:%M:%S")
    # 发送时间（引用图=引用消息时间）：仅在有值时给出可读串
    r["sent_time"] = (datetime.fromtimestamp(r["sent_at"]).strftime("%Y-%m-%d %H:%M:%S")
                      if r.get("sent_at") else "")
    # 可读画质标签：图片按 tier 映射，文件不适用。
    tier = str(r.get("tier") or "")
    if r["kind"] == "image" and tier:
        r["tier_label"] = {
            "original": "原图", "middle": "清晰图",
            "preview": "预览图", "thumb": "缩略图",
        }.get(tier, tier)
    else:
        r["tier_label"] = ""
    if r.get("width") and r.get("height"):
        r["resolution"] = "%dx%d" % (r["width"], r["height"])
    else:
        r["resolution"] = ""
    r.pop("rel_path", None)
    return r


def query(chat=None, chat_name=None, wxid=None, minutes=30, kind=None,
          chat_type=None, limit=200, include_expired_file=False,
          all_time=False, offset=0):
    """检索媒体流水（不分页版本，插件 ctx.recent_media 也用这个）。

    minutes: 最近 N 分钟；<=0 或 all_time=True 表示「全部」（不限时间）。
    chat_name: 群聊名 / 私聊显示名，做包含匹配（LIKE %x%）。
    chat: 精确会话 ID（群号或对方 wxid）。
    wxid: 精确发送者 wxid。
    kind: image / file。
    chat_type: group / private。
    limit / offset: 取第 offset 条起的 limit 条（1..1000）。
    """
    where, args = _build_where(chat=chat, chat_name=chat_name, wxid=wxid,
                               kind=kind, chat_type=chat_type,
                               minutes=minutes, all_time=all_time)
    try:
        limit = max(1, min(1000, int(limit)))
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    sql = ("SELECT * FROM media_items%s ORDER BY downloaded_at DESC LIMIT ? OFFSET ?"
           % (" WHERE " + " AND ".join(where) if where else ""))
    args = list(args) + [limit, offset]
    try:
        with _LOCK:
            conn = _connect()
            try:
                _ensure(conn)
                rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
            finally:
                conn.close()
    except Exception:
        return []

    rows = [_decorate_row(r) for r in rows]
    if not include_expired_file:
        rows = [r for r in rows if r["available"]]
    return rows


def query_page(chat=None, chat_name=None, wxid=None, minutes=30, kind=None,
               chat_type=None, page=1, page_size=50, all_time=False):
    """分页检索（管理端「媒体文件」页用）。

    minutes<=0 或 all_time=True → 全部记录，不限时间。
    返回 dict：items / total / page / page_size / pages / all_time。
    与 query() 不同：这里不过滤「已过期」行（否则总数与翻页对不上），
    由前端按 available 字段展示「已过期」。
    """
    where, args = _build_where(chat=chat, chat_name=chat_name, wxid=wxid,
                               kind=kind, chat_type=chat_type,
                               minutes=minutes, all_time=all_time)
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(1, min(500, int(page_size)))
    except (TypeError, ValueError):
        page_size = 50
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    out = {"items": [], "total": 0, "page": page, "page_size": page_size,
           "pages": 1, "all_time": bool(all_time)}
    try:
        with _LOCK:
            conn = _connect()
            try:
                _ensure(conn)
                total = conn.execute("SELECT COUNT(*) FROM media_items" + clause,
                                     args).fetchone()[0]
                rows = conn.execute(
                    "SELECT * FROM media_items%s ORDER BY downloaded_at DESC "
                    "LIMIT ? OFFSET ?" % clause,
                    list(args) + [page_size, (page - 1) * page_size]).fetchall()
            finally:
                conn.close()
    except Exception:
        return out
    out["total"] = int(total or 0)
    out["pages"] = max(1, (out["total"] + page_size - 1) // page_size)
    if page > out["pages"]:
        # 页码越界（例如筛选后数据变少）：回到最后一页，避免空白页
        out["page"] = out["pages"]
        try:
            with _LOCK:
                conn = _connect()
                try:
                    _ensure(conn)
                    rows = conn.execute(
                        "SELECT * FROM media_items%s ORDER BY downloaded_at DESC "
                        "LIMIT ? OFFSET ?" % clause,
                        list(args) + [page_size, (out["page"] - 1) * page_size]).fetchall()
                finally:
                    conn.close()
        except Exception:
            rows = []
    out["items"] = [_decorate_row(dict(r)) for r in rows]
    out["expired"] = sum(1 for r in out["items"] if not r["available"])
    return out


def maybe_purge(log=None):
    """节流清理：过期流水（record_ttl）+ 过期且「无人引用」的磁盘文件（file_ttl）。

    1.1.1-beta2 起两条规则：
      1. 流水保留 record_ttl（默认与 file_ttl 同为 7 天）；
      2. 磁盘文件**不能只按时间删**——表里还有记录引用的文件必须留着，否则
         管理端/插件查到记录却打不开文件。只有「超过 file_ttl 且 hash_name
         在 media_items 里已无任何引用」的文件才会被删除。
    先删过期流水、再按剩余引用判断文件，这样记录没了文件才会跟着走。
    """
    global _LAST_PURGE
    _log = log or (lambda *a, **kw: None)
    now = time.time()
    if now - _LAST_PURGE < DEFAULT_PURGE_INTERVAL:
        return
    with _LOCK:
        if now - _LAST_PURGE < DEFAULT_PURGE_INTERVAL:
            return
        _LAST_PURGE = now
    referenced = set()
    try:
        conn = _connect()
        try:
            _ensure(conn)
            conn.execute("DELETE FROM media_items WHERE downloaded_at < ?",
                         (now - CFG["record_ttl"],))
            conn.commit()
            referenced = {r[0] for r in conn.execute(
                "SELECT DISTINCT hash_name FROM media_items").fetchall() if r[0]}
        finally:
            conn.close()
    except Exception as e:
        # 拿不到引用表就先不碰文件，宁可多占点空间也不能误删仍被引用的文件
        _log("媒体流水清理失败（本轮跳过文件清理） | %s", e)
        return

    cutoff = now - CFG["file_ttl"]
    removed = kept = 0
    for folder in (CFG["images_dir"], CFG["files_dir"]):
        try:
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                p = os.path.join(folder, name)
                try:
                    if not os.path.isfile(p) or os.path.getmtime(p) >= cutoff:
                        continue
                    if name in referenced and not name.endswith(".part"):
                        kept += 1
                        continue
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass
        except OSError as e:
            _log("媒体目录清理失败 | %s | %s", folder, e)
    if removed or kept:
        _log("媒体清理 | 删除过期且无引用文件 %d 个 | 因仍被流水引用保留 %d 个",
             removed, kept)


def stats():
    """监控页展示用：记录条数 / 磁盘文件数 / 占用字节。"""
    try:
        conn = _connect()
        try:
            _ensure(conn)
            n = conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]
        finally:
            conn.close()
    except Exception:
        n = 0
    files = 0
    size = 0
    for folder in (CFG["images_dir"], CFG["files_dir"]):
        try:
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                p = os.path.join(folder, name)
                if os.path.isfile(p):
                    files += 1
                    try:
                        size += os.path.getsize(p)
                    except OSError:
                        pass
        except OSError:
            pass
    return {"index_count": n, "file_count": files, "file_bytes": size}
