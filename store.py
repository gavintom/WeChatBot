"""统一数据层：策略配置、消息流水、去重、通讯录缓存、后台账号。

主服务（webhook_server.py）与 Web 管理端（admin/app.py）共用同一个 SQLite 文件，
WAL 模式保证「一边写流水、一边看后台」互不阻塞。

三张核心业务表：
  kb_policies / kb_policy_docs  知识库策略及其包含的文档（文件名是唯一标识）
  group_rules                   群聊接入配置（群 wxid / 唤醒词 / 知识库策略）
  private_rules                 私聊接入配置（策略名 / 唤醒词 / 白名单 / 黑名单 / 知识库策略）

「新入库文档默认只属于默认知识库」用 kb_doc_registry 实现：
  只有**第一次见到**的文档会自动加入「默认知识库」，其它知识库不自动收文档；
  运营在策略里手动增减文档后，重启/同步都不会覆盖其选择（见 sync_existing_docs）。
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("WXBOT_DB") or os.path.join(BASE_DIR, "data", "analytics.db")
KNOWLEDGE_DIR = os.path.join(BASE_DIR, "data", "knowledge")

DEFAULT_ADMIN = "admin"
DEFAULT_PASSWORD = "admin123"
PBKDF2_ROUNDS = 120000

# 系统预置知识库：不可删除、不可改名（否则「不可删除」会被改名绕过）。
# 「API接口库」是 HTTP /httpapi/query 未指定 policy 时的默认检索范围，
# 初始化为空策略（不检索任何文档），运营可手动往里挑文档。
PROTECTED_POLICIES = ("默认知识库", "API接口库")
API_POLICY_NAME = "API接口库"
DEFAULT_POLICY_NAME = "默认知识库"   # 新入库文档默认只加入这一个知识库
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username   TEXT PRIMARY KEY,
    pwd_hash   TEXT NOT NULL,
    salt       TEXT NOT NULL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- AI 大模型（管理端增删改 / 选默认；brain 每次生成前读默认配置，切换即时生效）
CREATE TABLE IF NOT EXISTS llm_models (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    provider        TEXT NOT NULL DEFAULT '',
    base_url        TEXT NOT NULL,
    api_key         TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL,
    temperature     REAL NOT NULL DEFAULT 0.3,
    stream          INTEGER NOT NULL DEFAULT 1,
    max_tokens      INTEGER NOT NULL DEFAULT 0,
    connect_timeout INTEGER NOT NULL DEFAULT 10,
    read_timeout    INTEGER NOT NULL DEFAULT 60,
    total_timeout   INTEGER NOT NULL DEFAULT 240,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      REAL,
    updated_at      REAL
);

-- 知识库策略
CREATE TABLE IF NOT EXISTS kb_policies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL,
    updated_at  REAL
);
CREATE TABLE IF NOT EXISTS kb_policy_docs (
    policy_id INTEGER NOT NULL,
    doc_name  TEXT NOT NULL,
    added_at  REAL,
    PRIMARY KEY (policy_id, doc_name)
);
CREATE TABLE IF NOT EXISTS kb_doc_registry (
    doc_name   TEXT PRIMARY KEY,
    first_seen REAL
);

-- 群聊 / 私聊接入配置
CREATE TABLE IF NOT EXISTS group_rules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id       TEXT NOT NULL UNIQUE,
    group_name     TEXT NOT NULL DEFAULT '',
    wake_words     TEXT NOT NULL DEFAULT '[]',
    policy_id      INTEGER,
    enabled        INTEGER NOT NULL DEFAULT 1,
    at_all_trigger INTEGER NOT NULL DEFAULT 1,
    -- 规则级能力开关：关掉转人工=直接回复；关掉知识库=跳过检索
    need_transfer  INTEGER NOT NULL DEFAULT 1,
    need_kb        INTEGER NOT NULL DEFAULT 1,
    note           TEXT NOT NULL DEFAULT '',
    system_prompt  TEXT NOT NULL DEFAULT '',
    human_wxid     TEXT NOT NULL DEFAULT '',
    human_name     TEXT NOT NULL DEFAULT '',
    created_at     REAL,
    updated_at     REAL
);
CREATE TABLE IF NOT EXISTS private_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    wake_words  TEXT NOT NULL DEFAULT '[]',
    allow_list  TEXT NOT NULL DEFAULT '[]',
    deny_list   TEXT NOT NULL DEFAULT '[]',
    policy_id   INTEGER,
    enabled     INTEGER NOT NULL DEFAULT 1,
    priority    INTEGER NOT NULL DEFAULT 0,
    need_transfer INTEGER NOT NULL DEFAULT 1,
    need_kb       INTEGER NOT NULL DEFAULT 1,
    human_wxid  TEXT NOT NULL DEFAULT '',
    human_name  TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    created_at  REAL,
    updated_at  REAL
);

-- 消息流水（只记实际处理过的消息；跳过 / 不支持只进 DEBUG 日志）
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    day         TEXT NOT NULL,
    chat        TEXT,
    chat_type   TEXT,
    chat_name   TEXT,
    sender      TEXT,
    sender_name TEXT,
    kind        TEXT NOT NULL,
    question    TEXT,
    answer      TEXT,
    ms          INTEGER,
    ok          INTEGER NOT NULL DEFAULT 1,
    source      TEXT,
    policy_id   INTEGER,
    policy_name TEXT,
    trigger     TEXT,
    msg_type    TEXT,
    reason      TEXT,
    n_images    INTEGER,                   -- 本次回复附带的图片张数（老数据为 NULL，与 0 不同）
    plugin      TEXT                       -- 插件接管时记录插件名（普通 AI 回复为 NULL）
);
CREATE INDEX IF NOT EXISTS idx_events_day ON events(day);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_chat ON events(chat);
CREATE TABLE IF NOT EXISTS processed (
    key TEXT PRIMARY KEY,
    ts  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL NOT NULL,
    day               TEXT NOT NULL,
    model             TEXT,
    llm_id            INTEGER,                   -- 对应「AI 大模型」页纳管的模型 id（历史行为 NULL）
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    cost              REAL NOT NULL DEFAULT 0,   -- 当时算出的费用（元），单价变动仍可对账
    latency_ms        INTEGER,
    stream            INTEGER NOT NULL DEFAULT 1,
    ok                INTEGER NOT NULL DEFAULT 1,
    source            TEXT                       -- wechat / api / kb_test
);
CREATE INDEX IF NOT EXISTS idx_llm_day ON llm_usage(day);
CREATE INDEX IF NOT EXISTS idx_llm_ts ON llm_usage(ts);
CREATE INDEX IF NOT EXISTS idx_llm_model ON llm_usage(model);
CREATE TABLE IF NOT EXISTS api_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    day         TEXT NOT NULL,
    question    TEXT,
    answer      TEXT,
    n_images    INTEGER,
    q_chars     INTEGER,
    session_id  TEXT,
    client_ip   TEXT,
    status      INTEGER,
    ok          INTEGER NOT NULL DEFAULT 1,
    latency_ms  INTEGER,
    calls       INTEGER,
    total_tokens INTEGER,
    cost        REAL NOT NULL DEFAULT 0,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_calls_day ON api_calls(day);
CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls(ts);
CREATE INDEX IF NOT EXISTS idx_api_calls_ok ON api_calls(day, ok);

-- 通讯录缓存（来自手机插件 v2 的 /api/contacts/groups、friends、groupmember）
CREATE TABLE IF NOT EXISTS wx_groups (
    wxid         TEXT PRIMARY KEY,
    name         TEXT,
    member_count INTEGER,
    source       TEXT,
    synced_at    REAL
);
CREATE TABLE IF NOT EXISTS wx_contacts (
    wxid      TEXT PRIMARY KEY,
    name      TEXT,
    nick_name TEXT,
    remark    TEXT,
    type      TEXT,
    is_group  INTEGER NOT NULL DEFAULT 0,
    source    TEXT,
    detail    TEXT,
    avatar    TEXT,
    synced_at REAL
);
CREATE TABLE IF NOT EXISTS wx_group_members (
    group_id     TEXT NOT NULL,
    wxid         TEXT NOT NULL,
    name         TEXT,
    nick_name    TEXT,
    remark       TEXT,
    group_nick   TEXT,
    display_name TEXT,
    is_self      INTEGER NOT NULL DEFAULT 0,
    is_owner     INTEGER NOT NULL DEFAULT 0,
    avatar       TEXT,
    synced_at    REAL,
    PRIMARY KEY (group_id, wxid)
);
CREATE INDEX IF NOT EXISTS idx_gm_group ON wx_group_members(group_id);

-- 插件系统：启停状态 / 优先级覆盖 / 连续异常熔断。主服务（唯一加载插件的进程）
-- 与 Web 管理端共用同一库，WAL 保证后台改开关后主服务能立刻读到。
CREATE TABLE IF NOT EXISTS plugins (
    name               TEXT PRIMARY KEY,
    enabled            INTEGER NOT NULL DEFAULT 1,
    priority_override  INTEGER,
    source             TEXT NOT NULL DEFAULT '',
    fail_count         INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT NOT NULL DEFAULT '',
    updated_at        REAL
);
"""


# ---------------------------------------------------------------- 插件状态
# 注意：这些函数定义出现在 _rows / _write 之前没关系，Python 在**调用时**
# 才解析模块级名字；init_db() 会先建表，之后任何调用都是安全的。


def list_plugins():
    """所有已登记插件的状态（后台列表页 / 主服务加载时用）。"""
    return _rows("SELECT * FROM plugins ORDER BY name")


def plugin_state(name):
    return _one("SELECT * FROM plugins WHERE name=?", (str(name),))


def ensure_plugin(name, source="", priority=None):
    """首次发现插件时登记一行；已存在的行一律不动，保留运营改过的开关与优先级。"""
    _write("INSERT OR IGNORE INTO plugins(name,enabled,priority_override,source,"
           "fail_count,last_error,updated_at) VALUES(?,1,?,?,0,'',?)",
           (str(name), priority, str(source or ""), time.time()))
    return plugin_state(name) or {}


def set_plugin_enabled(name, enabled):
    """后台启用/禁用。

    启用时清掉熔断计数与上次错误；禁用时**保留**，这样熔断原因能在后台看到。
    """
    if enabled:
        _write("UPDATE plugins SET enabled=1, fail_count=0, last_error='', updated_at=? "
               "WHERE name=?", (time.time(), str(name)))
    else:
        _write("UPDATE plugins SET enabled=0, updated_at=? WHERE name=?",
               (time.time(), str(name)))


def set_plugin_priority(name, priority):
    """None = 回落到 manifest / 装饰器声明的优先级。"""
    _write("UPDATE plugins SET priority_override=?, updated_at=? WHERE name=?",
           (None if priority is None else int(priority), time.time(), str(name)))


def mark_plugin_failure(name, err, threshold=5):
    """累计连续失败；达到阈值自动禁用并返回 True（熔断）。"""
    row = plugin_state(name) or {}
    n = int(row.get("fail_count") or 0) + 1
    tripped = n >= max(1, int(threshold or 5))
    _write("UPDATE plugins SET fail_count=?, last_error=?, "
           "enabled=CASE WHEN ? THEN 0 ELSE enabled END, updated_at=? WHERE name=?",
           (n, str(err)[:300], 1 if tripped else 0, time.time(), str(name)))
    return tripped


def reset_plugin_failure(name):
    _write("UPDATE plugins SET fail_count=0, last_error='', updated_at=? WHERE name=?",
           (time.time(), str(name)))



def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _rows(sql, args=()):
    conn = _connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _one(sql, args=()):
    r = _rows(sql, args)
    return r[0] if r else None


def _write(sql, args=()):
    with _write_lock:
        conn = _connect()
        try:
            cur = conn.execute(sql, args)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def _write_many(fn):
    """在一个事务里执行 fn(conn)；写入统一串行化。"""
    with _write_lock:
        conn = _connect()
        try:
            fn(conn)
            conn.commit()
        finally:
            conn.close()


def _day(ts=None):
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


# ---------------------------------------------------------------- 账号

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"),
                            salt.encode("utf-8"), PBKDF2_ROUNDS).hex()
    return h, salt


def verify_user(username, password):
    row = _one("SELECT * FROM users WHERE username=?", (str(username or ""),))
    # 用户不存在时也跑一遍哈希，避免时序探测
    h, s = hash_password(password, (row or {}).get("salt") or "nosalt")
    if not row or not hmac.compare_digest(h, row["pwd_hash"]):
        return None
    return {"username": row["username"]}


def change_password(username, old, new):
    row = _one("SELECT * FROM users WHERE username=?", (username,))
    if not row:
        return False, "用户不存在"
    h, _ = hash_password(old, row["salt"])
    if not hmac.compare_digest(h, row["pwd_hash"]):
        return False, "原密码不正确"
    if not str(new or "").strip() or len(str(new)) < 6:
        return False, "新密码至少 6 位"
    h2, s2 = hash_password(new)
    _write("UPDATE users SET pwd_hash=?, salt=?, updated_at=? WHERE username=?",
           (h2, s2, time.time(), username))
    set_setting("pwd_changed_at", str(int(time.time())))
    return True, "密码已修改"


def password_changed_at():
    return get_setting("pwd_changed_at", "")


# ---------------------------------------------------------------- settings

def get_setting(k, default=None):
    row = _one("SELECT v FROM settings WHERE k=?", (k,))
    return row["v"] if row else default


def set_setting(k, v):
    _write("INSERT INTO settings(k,v) VALUES(?,?) "
           "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))



# ---------------------------------------------------------------- 知识库文档 / 策略

def all_kb_docs():
    """扫描 data/knowledge/ 下的 .md/.txt，返回相对路径（与 brain.HybridRetriever.meta 一致）。"""
    out = []
    if not os.path.isdir(KNOWLEDGE_DIR):
        return out
    for root, dirs, files in os.walk(KNOWLEDGE_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.lower().endswith((".md", ".txt")):
                out.append(os.path.relpath(os.path.join(root, f), KNOWLEDGE_DIR).replace(os.sep, "/"))
    return sorted(out)


def _safe_doc(name):
    n = str(name or "").strip().replace("\\", "/")
    if not n or ".." in n or n.startswith("/") or not n.lower().endswith((".md", ".txt")):
        raise ValueError("知识库文档名不合法：%s" % n)
    return n


def register_doc(doc_name):
    """新文档入库：默认只加入「默认知识库」；已见过的文档不重复处理。

    已登记过的文档不会被重新处理 —— 运营在任意知识库里手动增/删文档的选择都保持不动，
    这是「可编辑策略」的前提。新文档绝不自动塞进「默认知识库」以外的知识库
    （尤其是 HTTP API 的默认隔离库「API接口库」），需要时由运营手动挑选。
    """
    n = _safe_doc(doc_name)
    if _one("SELECT 1 FROM kb_doc_registry WHERE doc_name=?", (n,)):
        return False
    now = time.time()

    def _do(conn):
        conn.execute("INSERT OR IGNORE INTO kb_doc_registry(doc_name, first_seen) VALUES(?,?)", (n, now))
        # 新文档默认只归属「默认知识库」；其它知识库（含 API接口库）保持原样，
        # 一律由运营手动勾选，避免新文档在所有知识库里都处于选中状态。
        row = conn.execute("SELECT id FROM kb_policies WHERE name=?",
                           (DEFAULT_POLICY_NAME,)).fetchone()
        if row:
            conn.execute("INSERT OR IGNORE INTO kb_policy_docs(policy_id, doc_name, added_at) "
                         "VALUES(?,?,?)", (row["id"], n, now))
    _write_many(_do)
    return True


def unregister_doc(doc_name):
    n = str(doc_name or "").strip()
    if not n:
        return

    def _do(conn):
        conn.execute("DELETE FROM kb_doc_registry WHERE doc_name=?", (n,))
        conn.execute("DELETE FROM kb_policy_docs WHERE doc_name=?", (n,))
    _write_many(_do)


def prune_missing_docs():
    """清理「磁盘上已不存在」的知识库文档引用（registry + 所有策略）。

    后台 delete_file() 会即时 unregister，但测试脚本直接 os.remove、手动删文件、
    或跨进程删文件都会绕过 API。主服务启动 / 点「同步新文档」时在这里兜底，
    避免知识库列表里留着指向不存在文件的幽灵文档。返回清理掉的文档数。
    """
    if not os.path.isdir(KNOWLEDGE_DIR):
        return 0                      # 目录临时不可读时不要冒险清库
    disk = set(all_kb_docs())
    reg = _rows("SELECT doc_name FROM kb_doc_registry")
    pol = _rows("SELECT DISTINCT doc_name FROM kb_policy_docs")
    stale = sorted(
        {r["doc_name"] for r in reg if r["doc_name"] not in disk}
        | {r["doc_name"] for r in pol if r["doc_name"] not in disk})
    if not stale:
        return 0

    def _do(conn):
        for n in stale:
            conn.execute("DELETE FROM kb_doc_registry WHERE doc_name=?", (n,))
            conn.execute("DELETE FROM kb_policy_docs WHERE doc_name=?", (n,))
    _write_many(_do)
    return len(stale)


def sync_existing_docs():
    """把磁盘上尚未登记的知识库文档补进「默认知识库」；顺带清掉已删文件的悬空引用。"""
    prune_missing_docs()
    added = []
    for n in all_kb_docs():
        if register_doc(n):
            added.append(n)
    return added


def list_policies(include_disabled=True):
    sql = "SELECT * FROM kb_policies" if include_disabled else \
          "SELECT * FROM kb_policies WHERE enabled=1"
    # 系统预置的两个库永远排在最上面（后台「知识库管理」一眼能看见），
    # 其余按 id 稳定排序；用参数化而不是把中文名字拼进 SQL。
    places = ",".join("?" for _ in PROTECTED_POLICIES)
    rows = _rows(sql + " ORDER BY CASE WHEN name IN (%s) THEN 0 ELSE 1 END, id" % places,
                 tuple(PROTECTED_POLICIES))
    for p in rows:
        # 磁盘上已删的文件不显示：测试脚本 / 手动 rm 绕过后台删除 API 时，
        # 界面也能立刻看不到幽灵文档；DB 里的残留由 prune_missing_docs 兜底清理。
        p["docs"] = [r["doc_name"] for r in _rows(
            "SELECT doc_name FROM kb_policy_docs WHERE policy_id=? ORDER BY doc_name", (p["id"],))
            if os.path.isfile(os.path.join(KNOWLEDGE_DIR, r["doc_name"]))]
        p["doc_count"] = len(p["docs"])
        p["used_by"] = _one("SELECT COUNT(*) AS n FROM group_rules WHERE policy_id=?", (p["id"],))["n"] \
            + _one("SELECT COUNT(*) AS n FROM private_rules WHERE policy_id=?", (p["id"],))["n"]
        p["enabled"] = bool(p["enabled"])
        p["protected"] = p["name"] in PROTECTED_POLICIES
    return rows


def get_policy(key):
    if key is None or str(key).strip() == "":
        return None
    k = str(key).strip()
    row = _one("SELECT * FROM kb_policies WHERE name=?", (k,))
    if not row and k.isdigit():
        row = _one("SELECT * FROM kb_policies WHERE id=?", (int(k),))
    if not row:
        return None
    row["docs"] = [r["doc_name"] for r in _rows(
        "SELECT doc_name FROM kb_policy_docs WHERE policy_id=? ORDER BY doc_name", (row["id"],))
        if os.path.isfile(os.path.join(KNOWLEDGE_DIR, r["doc_name"]))]
    row["enabled"] = bool(row["enabled"])
    return row


def policy_docs(key):
    """给检索用：策略名/ID → (策略名, 文档列表)。找不到返回 None。"""
    p = get_policy(key)
    if not p:
        return None
    return p["name"], p["docs"]


def _default_policy_id():
    row = _one("SELECT id FROM kb_policies ORDER BY id LIMIT 1")
    return row["id"] if row else None


def save_policy(data):
    import json
    d = data or {}
    name = str(d.get("name") or "").strip()
    if not name:
        raise ValueError("策略名称不能为空")
    if len(name) > 60:
        raise ValueError("策略名称最长 60 字")
    docs = []
    from textutil import parse_list
    for x in parse_list(d.get("docs")):
        n = _safe_doc(x)
        if n not in docs:
            docs.append(n)
    pid = d.get("id")
    now = time.time()
    desc = str(d.get("description") or "").strip()
    enabled = 1 if d.get("enabled", True) else 0
    if pid:
        row = _one("SELECT id, name FROM kb_policies WHERE id=?", (pid,))
        if not row:
            raise ValueError("策略不存在")
        # 系统预置库不能改名：否则删掉保护名单里的名字就能绕过「不可删除」
        if row["name"] in PROTECTED_POLICIES and name != row["name"]:
            raise ValueError("「%s」是系统预置知识库，不可改名" % row["name"])
        dup = _one("SELECT id FROM kb_policies WHERE name=? AND id<>?", (name, pid))
        if dup:
            raise ValueError("策略名称已存在")
        _write("UPDATE kb_policies SET name=?, description=?, enabled=?, updated_at=? WHERE id=?",
               (name, desc, enabled, now, pid))
    else:
        if _one("SELECT id FROM kb_policies WHERE name=?", (name,)):
            raise ValueError("策略名称已存在")
        pid = _write("INSERT INTO kb_policies(name,description,enabled,created_at,updated_at) "
                     "VALUES(?,?,?,?,?)", (name, desc, enabled, now, now))

    def _do(conn):
        conn.execute("DELETE FROM kb_policy_docs WHERE policy_id=?", (pid,))
        for n in docs:
            conn.execute("INSERT INTO kb_policy_docs(policy_id,doc_name,added_at) VALUES(?,?,?)",
                         (pid, n, now))
    _write_many(_do)
    # 策略里引用的文档也要登记，避免之后 sync 把它们当新文档重新塞进别的策略
    for n in docs:
        _write("INSERT OR IGNORE INTO kb_doc_registry(doc_name,first_seen) VALUES(?,?)", (n, now))
    return int(pid)


def delete_policy(pid):
    row = _one("SELECT name FROM kb_policies WHERE id=?", (pid,))
    if row and str(row.get("name") or "") in PROTECTED_POLICIES:
        raise ValueError("「%s」是系统预置知识库，不可删除" % row["name"])
    used = _one("SELECT COUNT(*) AS n FROM group_rules WHERE policy_id=?", (pid,))["n"] + \
        _one("SELECT COUNT(*) AS n FROM private_rules WHERE policy_id=?", (pid,))["n"]
    if used:
        raise ValueError("该策略仍被 %d 条群聊/私聊配置引用，请先改配置再删除" % used)

    def _do(conn):
        conn.execute("DELETE FROM kb_policy_docs WHERE policy_id=?", (pid,))
        conn.execute("DELETE FROM kb_policies WHERE id=?", (pid,))
    _write_many(_do)


def ensure_system_policies():
    """幂等补齐系统预置知识库；已存在的一律不动（不覆盖运营编辑过的文档列表）。

    放在「默认策略 → 默认知识库」更名之后调用，老库也能正确升级：
    历史文档早已登记在 kb_doc_registry，不会被回填进新建的「API接口库」，
    所以它天然是空策略，符合「默认不选择任何知识库文档」。
    """
    for name, desc in (
            ("默认知识库", "新入库的知识库文档默认属于本知识库"),
            (API_POLICY_NAME,
             "HTTP /httpapi/query 未指定 policy 时的默认检索范围；默认不选择任何文档")):
        row = _one("SELECT id, description FROM kb_policies WHERE name=?", (name,))
        if not row:
            save_policy({"name": name, "description": desc,
                         "enabled": True, "docs": []})
        elif name == API_POLICY_NAME and "/api/query" in (row["description"] or ""):
            # 老库里的预置说明还写着旧接口路径 /api/query（现为 /httpapi/query）。
            # 只升级这句历史文案；运营自己改过的说明一律保留。
            _write("UPDATE kb_policies SET description=? WHERE id=?", (desc, row["id"]))


def init_db():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
    with _write_lock:
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            # 幂等迁移：老库缺列时补上（不改动任何已有数据）
            _migrations = {
                "group_rules": [("system_prompt", "TEXT NOT NULL DEFAULT ''"),
                                ("human_wxid", "TEXT NOT NULL DEFAULT ''"),
                                ("human_name", "TEXT NOT NULL DEFAULT ''"),
                                ("need_transfer", "INTEGER NOT NULL DEFAULT 1"),
                                ("need_kb", "INTEGER NOT NULL DEFAULT 1")],
                "private_rules": [("system_prompt", "TEXT NOT NULL DEFAULT ''"),
                                  ("need_transfer", "INTEGER NOT NULL DEFAULT 1"),
                                  ("need_kb", "INTEGER NOT NULL DEFAULT 1"),
                                  ("human_wxid", "TEXT NOT NULL DEFAULT ''"),
                                  ("human_name", "TEXT NOT NULL DEFAULT ''")],
                # 费用列：老库的 llm_usage / api_calls 没有 cost，补齐后历史行记 0
                # llm_id：老流水没有「纳管模型」维度，保持 NULL（对照表里归到「未纳管/历史」）
                "llm_usage": [("cost", "REAL NOT NULL DEFAULT 0"),
                              ("llm_id", "INTEGER")],
                "api_calls": [("cost", "REAL NOT NULL DEFAULT 0")],
                # 对话记录「配图」列：老流水没记这个维度，保持 NULL（前端显示「-」）
                # plugin：老流水没有「插件来源」维度，保持 NULL（前端归到「—」）
                "events": [("n_images", "INTEGER"), ("plugin", "TEXT")],
                # v2 通讯录 / 群成员带 avatar CDN 直链，老库缺列时补上
                "wx_contacts": [("avatar", "TEXT")],
                "wx_group_members": [("avatar", "TEXT")],
            }
            for _t, _add in _migrations.items():
                _cols = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % _t)}
                for _col, _decl in _add:
                    if _col not in _cols:
                        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (_t, _col, _decl))
            conn.commit()
            if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                h, s = hash_password(DEFAULT_PASSWORD)
                conn.execute("INSERT INTO users(username,pwd_hash,salt,updated_at) VALUES(?,?,?,?)",
                             (DEFAULT_ADMIN, h, s, time.time()))
            conn.commit()
        finally:
            conn.close()
    # 首次运行：默认知识库 + 默认私聊配置（群聊默认不配置，必须显式添加才处理）
    if not _one("SELECT 1 FROM kb_policies LIMIT 1"):
        save_policy({"name": "默认知识库", "description": "新入库的知识库文档默认属于本知识库",
                     "enabled": True, "docs": []})
    # 术语更名（幂等）：老库里系统预置的「默认策略」改叫「默认知识库」。
    # 用 id 关联而不是改外键：group_rules / private_rules 都存 policy_id，不受名字影响。
    try:
        _old = _one("SELECT id FROM kb_policies WHERE name='默认策略'")
        _new = _one("SELECT id FROM kb_policies WHERE name='默认知识库'")
        if _old and not _new:
            _write("UPDATE kb_policies SET name=?, description=? WHERE id=?",
                   ("默认知识库", "新入库的知识库文档默认属于本知识库", _old["id"]))
    except Exception:
        pass
    # 补齐系统预置库（含新版本新增的「API接口库」），必须在更名之后
    ensure_system_policies()
    if not _one("SELECT 1 FROM private_rules LIMIT 1"):
        save_private_rule({"name": "默认私聊", "wake_words": "", "allow_list": "",
                           "deny_list": "", "policy_id": _default_policy_id(),
                           "enabled": True, "priority": 99,
                           "note": "唤醒词留空=所有私聊消息都响应；优先级 99 作为兜底，"
                                   "自定义配置（默认 0）优先匹配；可按需停用或加白名单"})
    if get_setting("global_deny") is None:
        set_setting("global_deny", "[]")
    return sync_existing_docs()


# ---------------------------------------------------------------- 群聊配置

def _json_list(value):
    import json
    from textutil import parse_list
    return json.dumps(parse_list(value), ensure_ascii=False)


def _rules_with_policy(rows):
    for r in rows:
        r["wake_words"] = _loads(r.get("wake_words"))
        r["allow_list"] = _loads(r.get("allow_list")) if "allow_list" in r else []
        r["deny_list"] = _loads(r.get("deny_list")) if "deny_list" in r else []
        r["enabled"] = bool(r["enabled"])
        p = get_policy(r.get("policy_id")) if r.get("policy_id") else None
        r["policy_name"] = p["name"] if p else ""
        r["docs"] = p["docs"] if p else []
        r["at_all_trigger"] = bool(r.get("at_all_trigger", 1)) if "at_all_trigger" in r else True
        r["need_transfer"] = bool(r.get("need_transfer", 1))
        r["need_kb"] = bool(r.get("need_kb", 1))
        r["system_prompt"] = r.get("system_prompt") or ""
        r["human_wxid"] = r.get("human_wxid") or ""
        r["human_name"] = r.get("human_name") or ""
    return rows


def _loads(s):
    import json
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def list_group_rules(include_disabled=True):
    sql = "SELECT * FROM group_rules" + ("" if include_disabled else " WHERE enabled=1")
    return _rules_with_policy(_rows(sql + " ORDER BY id"))


def get_group_rule(group_id):
    rows = _rules_with_policy(_rows(
        "SELECT * FROM group_rules WHERE group_id=? AND enabled=1", (str(group_id or ""),)))
    return rows[0] if rows else None


def save_group_rule(data):
    d = data or {}
    gid = str(d.get("group_id") or "").strip()
    if not gid:
        raise ValueError("群聊 ID 不能为空（形如 12345@chatroom）")
    need_kb = bool(d.get("need_kb", True))
    need_transfer = bool(d.get("need_transfer", True))
    # 关掉知识库就不绑定任何策略（下拉禁用且值为空）；开启但没选则回退默认库。
    raw_pid = d.get("policy_id")
    pid = _default_policy_id() if need_kb else None
    if need_kb and raw_pid:
        pid = int(raw_pid)
    if pid is not None and not get_policy(pid):
        raise ValueError("知识库策略不存在")
    # 关掉转人工时人工接待一律清空，避免残留值以后被误用
    human_wxid = str(d.get("human_wxid") or "").strip() if need_transfer else ""
    human_name = str(d.get("human_name") or "").strip() if need_transfer else ""
    now = time.time()
    vals = (gid, str(d.get("group_name") or "").strip(),
            _json_list(d.get("wake_words")), pid,
            1 if d.get("enabled", True) else 0,
            1 if d.get("at_all_trigger", False) else 0,   # 新建默认不把 @所有人 当触发
            str(d.get("note") or "").strip(),
            str(d.get("system_prompt") or "").strip(),
            human_wxid, human_name,
            1 if need_transfer else 0, 1 if need_kb else 0)
    rid = d.get("id")
    if rid:
        dup = _one("SELECT id FROM group_rules WHERE group_id=? AND id<>?", (gid, rid))
        if dup:
            raise ValueError("群聊 ID 已存在")
        _write("UPDATE group_rules SET group_id=?,group_name=?,wake_words=?,policy_id=?,"
               "enabled=?,at_all_trigger=?,note=?,system_prompt=?,human_wxid=?,human_name=?,"
               "need_transfer=?,need_kb=?,updated_at=? WHERE id=?", vals + (now, rid))
        return int(rid)
    if _one("SELECT id FROM group_rules WHERE group_id=?", (gid,)):
        raise ValueError("群聊 ID 已存在，请直接编辑该条配置")
    return _write("INSERT INTO group_rules(group_id,group_name,wake_words,policy_id,enabled,"
                  "at_all_trigger,note,system_prompt,human_wxid,human_name,"
                  "need_transfer,need_kb,created_at,updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals + (now, now))


def delete_group_rule(rid):
    _write("DELETE FROM group_rules WHERE id=?", (rid,))


# ---------------------------------------------------------------- 私聊配置

def list_private_rules(include_disabled=True):
    sql = "SELECT * FROM private_rules" + ("" if include_disabled else " WHERE enabled=1")
    return _rules_with_policy(_rows(sql + " ORDER BY priority, id"))


def enabled_private_rules():
    return _rules_with_policy(_rows(
        "SELECT * FROM private_rules WHERE enabled=1 ORDER BY priority, id"))


def save_private_rule(data):
    d = data or {}
    name = str(d.get("name") or "").strip()
    if not name:
        raise ValueError("私聊策略名不能为空")
    need_kb = bool(d.get("need_kb", True))
    need_transfer = bool(d.get("need_transfer", True))
    raw_pid = d.get("policy_id")
    pid = _default_policy_id() if need_kb else None
    if need_kb and raw_pid:
        pid = int(raw_pid)
    if pid is not None and not get_policy(pid):
        raise ValueError("知识库策略不存在")
    human_wxid = str(d.get("human_wxid") or "").strip() if need_transfer else ""
    human_name = str(d.get("human_name") or "").strip() if need_transfer else ""
    now = time.time()
    vals = (name, _json_list(d.get("wake_words")), _json_list(d.get("allow_list")),
            _json_list(d.get("deny_list")), pid,
            1 if d.get("enabled", True) else 0,
            int(d.get("priority") or 0), str(d.get("note") or "").strip(),
            str(d.get("system_prompt") or "").strip(),
            human_wxid, human_name,
            1 if need_transfer else 0, 1 if need_kb else 0)
    rid = d.get("id")
    if rid:
        _write("UPDATE private_rules SET name=?,wake_words=?,allow_list=?,deny_list=?,"
               "policy_id=?,enabled=?,priority=?,note=?,system_prompt=?,"
               "human_wxid=?,human_name=?,need_transfer=?,need_kb=?,updated_at=? WHERE id=?",
               vals + (now, rid))
        return int(rid)
    return _write("INSERT INTO private_rules(name,wake_words,allow_list,deny_list,policy_id,"
                  "enabled,priority,note,system_prompt,human_wxid,human_name,"
                  "need_transfer,need_kb,created_at,updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals + (now, now))


def delete_private_rule(rid):
    _write("DELETE FROM private_rules WHERE id=?", (rid,))


def get_global_deny():
    return _loads(get_setting("global_deny", "[]"))


def set_global_deny(value):
    from textutil import parse_list
    import json
    set_setting("global_deny", json.dumps(parse_list(value), ensure_ascii=False))


# ---------------------------------------------------------------- 消息流水

_EVENT_FIELDS = ("ts", "chat", "chat_type", "chat_name", "sender", "sender_name",
                 "kind", "question", "answer", "ms", "ok", "source", "policy_id",
                 "policy_name", "trigger", "msg_type", "reason", "n_images", "plugin")


def log_event(**kw):
    """记一条消息/回复流水。失败绝不影响主链路。

    events.ok 是 NOT NULL DEFAULT 1，但 INSERT 会把缺失字段**显式写成 NULL**
    （DEFAULT 只在整列省略时生效），所以这里补默认值，避免写入被静默丢掉。
    """
    try:
        if kw.get("ok") is None:
            kw["ok"] = 1
        ts = float(kw.get("ts") or time.time())
        vals = [ts, _day(ts)] + [kw.get(k) for k in _EVENT_FIELDS[1:]]
        sql = "INSERT INTO events(ts,day,%s) VALUES(%s)" % (
            ",".join(_EVENT_FIELDS[1:]), ",".join(["?"] * (len(vals))))
        _write(sql, tuple(vals))
    except Exception:
        pass


def query_events(start="", end="", keyword="", kind="", chat_type="",
                 limit=50, offset=0):
    where, args = [], []
    if start:
        where.append("day>=?"); args.append(start)
    if end:
        where.append("day<=?"); args.append(end)
    if keyword:
        where.append("(question LIKE ? OR answer LIKE ? OR chat_name LIKE ? OR sender_name LIKE ?)")
        args += ["%%%s%%" % keyword] * 4
    if kind:
        where.append("kind=?"); args.append(kind)
    if chat_type:
        where.append("chat_type=?"); args.append(chat_type)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = _one("SELECT COUNT(*) AS n FROM events" + w, tuple(args))["n"]
    rows = _rows("SELECT * FROM events" + w + " ORDER BY ts DESC LIMIT ? OFFSET ?",
                 tuple(args) + (int(limit), int(offset)))
    for r in rows:
        r["time_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
        r["ok"] = bool(r["ok"])
    return {"total": total, "items": rows}


def all_events_for_export(start="", end="", keyword="", kind="", chat_type=""):
    return query_events(start, end, keyword, kind, chat_type, limit=100000, offset=0)["items"]


def summary():
    d = _day()
    r = _one("SELECT COUNT(*) AS total, "
             "SUM(CASE WHEN kind='reply' THEN 1 ELSE 0 END) AS replies, "
             "SUM(CASE WHEN kind='skip' THEN 1 ELSE 0 END) AS skips, "
             "SUM(CASE WHEN kind='unsupported' THEN 1 ELSE 0 END) AS unsupported, "
             "SUM(CASE WHEN chat_type='group' AND kind='reply' THEN 1 ELSE 0 END) AS group_replies, "
             "SUM(CASE WHEN chat_type='private' AND kind='reply' THEN 1 ELSE 0 END) AS private_replies, "
             "AVG(CASE WHEN kind='reply' THEN ms ELSE NULL END) AS avg_ms "
             "FROM events WHERE day=?", (d,)) or {}
    return {
        "today_total": r.get("total") or 0,
        "today_replies": r.get("replies") or 0,
        "today_skips": r.get("skips") or 0,
        "today_unsupported": r.get("unsupported") or 0,
        "today_group_replies": r.get("group_replies") or 0,
        "today_private_replies": r.get("private_replies") or 0,
        "avg_ms": int(r.get("avg_ms") or 0),
        "group_rules": _one("SELECT COUNT(*) AS n FROM group_rules WHERE enabled=1")["n"],
        "private_rules": _one("SELECT COUNT(*) AS n FROM private_rules WHERE enabled=1")["n"],
        "policies": _one("SELECT COUNT(*) AS n FROM kb_policies")["n"],
        "docs": len(all_kb_docs()),
        "contacts": _one("SELECT COUNT(*) AS n FROM wx_contacts")["n"],
        "groups": _one("SELECT COUNT(*) AS n FROM wx_groups")["n"],
    }


def daily_series(days=30):
    """最近 N 天对话趋势：AI 回复 / 跳过 / 转人工，缺失日期补 0。

    与 api_calls_daily 共用 _daily_range，保证运行监控里所有图表的 X 轴对齐、
    时间窗口一致（各查一次会出现「图和卡片对不上」）。
    """
    n, end_day, start_day, start_ts = _daily_range(days)
    rows = _rows("SELECT day, "
                 "SUM(CASE WHEN kind='reply' THEN 1 ELSE 0 END) AS replies, "
                 "SUM(CASE WHEN kind='skip' THEN 1 ELSE 0 END) AS skips, "
                 "SUM(CASE WHEN kind='transfer' THEN 1 ELSE 0 END) AS transfers "
                 "FROM events WHERE day>=? AND day<=? GROUP BY day",
                 (start_day, end_day))
    m = {r["day"]: r for r in rows}
    out = []
    for i in range(n + 1):
        d = _day(start_ts + i * 86400)
        r = m.get(d)
        out.append({"day": d,
                    "replies": r["replies"] if r else 0,
                    "skips": r["skips"] if r else 0,
                    "transfers": r["transfers"] if r else 0})
    return out


# ---------------------------------------------------------------- 去重

def dedup_check_and_mark(key, ttl=86400):
    """原子判重：True=首次见（可处理），False=重复推送（丢弃）。"""
    now = time.time()
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM processed WHERE ts<?", (now - max(60, int(ttl)),))
            try:
                conn.execute("INSERT INTO processed(key,ts) VALUES(?,?)", (str(key), now))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False
        finally:
            conn.close()


# ---------------------------------------------------------------- 统计埋点（主服务 / kbapi / 后台共用）

def today():
    """本地今天 YYYY-MM-DD（「今日」卡片不能拿全量冒充）。"""
    return _day()


def _like_escape(s):
    return str(s or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _range_where(start=None, end=None):
    """日期区间 WHERE 片段；day 列本身就是 YYYY-MM-DD，直接字符串比较即可。"""
    where, args = [], []
    if start:
        where.append("day>=?"); args.append(str(start)[:10])
    if end:
        where.append("day<=?"); args.append(str(end)[:10])
    return (("WHERE " + " AND ".join(where)) if where else ""), args


def _daily_range(days, end_day=None):
    """返回 (天数, 结束日, 起始日, 起始时间戳)，用于缺失日期补 0 的趋势图。"""
    try:
        n = max(1, min(365, int(days)))
    except (TypeError, ValueError):
        n = 30
    end_day = (end_day or today())[:10]
    try:
        end_ts = time.mktime(time.strptime(end_day, "%Y-%m-%d")) + 86399
    except ValueError:
        end_day = today()
        end_ts = time.mktime(time.strptime(end_day, "%Y-%m-%d")) + 86399
    start_ts = end_ts - n * 86400
    return n, end_day, _day(start_ts), start_ts


def _time_str(rows):
    for r in rows:
        r["time_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
    return rows


def _cost_of(model, pt, ct):
    """费用算完**存进库**，不在查询时重算（单价会变，历史值才对账）。"""
    try:
        from admin import pricing
        cost, _known = pricing.compute_cost(model, pt, ct)
        return round(float(cost), 6)
    except Exception:
        return 0.0


# ---------- 大模型调用流水 ----------

def log_llm_usage(attempts, model=None, source="normal", llm_id=None):
    """一次 answer() 可能有多行（重试 / 流式失败）。**绝不能抛异常**（统计不能拖垮回复）。

    llm_id：本次实际使用的「AI 大模型」页纳管模型 id。用 id 而不是 model 字符串，
    才能区分两个纳管模型共用同一个上游模型 ID（不同供应商 / base_url）的情况。
    """
    try:
        now = time.time()
        for a in attempts or []:
            m = model or a.get("model")
            pt, ct = a.get("prompt_tokens"), a.get("completion_tokens")
            _write("INSERT INTO llm_usage(ts,day,model,llm_id,prompt_tokens,completion_tokens,"
                   "total_tokens,cost,latency_ms,stream,ok,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (now, _day(now), m, llm_id, pt, ct, a.get("total_tokens"),
                    _cost_of(m, pt, ct), a.get("latency_ms"),
                    1 if a.get("stream") else 0,
                    1 if a.get("ok", True) else 0, source))
    except Exception:
        pass


def llm_summary(start=None, end=None, model=None):
    """区间汇总 + 按模型分布。无参 = 全量（/api/stats 仍按老键 calls/total_tokens 读）。

    model 可选，与 llm_records 用同一套模糊匹配口径 —— 后台按模型筛选时，
    顶部卡片 / 按模型分布 / 明细三处必须是同一个结果集，否则「合计」对不上。
    """
    clause, args = _range_where(start, end)
    args = list(args)
    if model and str(model).strip():
        clause = (clause + " AND " if clause else "WHERE ") + \
            "IFNULL(model,'') LIKE ? ESCAPE '\\'"
        args.append("%" + _like_escape(model) + "%")
    r = _one("SELECT COUNT(*) AS calls,"
             " COALESCE(SUM(prompt_tokens),0) AS pt,"
             " COALESCE(SUM(completion_tokens),0) AS ct,"
             " COALESCE(SUM(total_tokens),0) AS tt,"
             " COALESCE(SUM(cost),0) AS cost,"
             " COALESCE(SUM(ok),0) AS ok,"
             " COALESCE(AVG(latency_ms),0) AS lat"
             " FROM llm_usage " + clause, args) or {}
    models = _rows("SELECT model, COUNT(*) AS calls,"
                   " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
                   " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
                   " COALESCE(SUM(total_tokens),0) AS total_tokens,"
                   " COALESCE(SUM(cost),0) AS cost,"
                   " COALESCE(AVG(latency_ms),0) AS avg_latency_ms"
                   " FROM llm_usage " + clause +
                   " GROUP BY model ORDER BY total_tokens DESC", args)
    try:
        from admin import pricing
        for m in models:
            m["priced"] = pricing.get_pricing(m.get("model")).get("kind") != "unknown"
    except Exception:
        for m in models:
            m["priced"] = False
    return {
        "calls": r.get("calls") or 0,
        "total_tokens": r.get("tt") or 0,
        "prompt_tokens": r.get("pt") or 0,
        "completion_tokens": r.get("ct") or 0,
        "cost": round(r.get("cost") or 0, 4),
        "ok_calls": r.get("ok") or 0,
        "fail_calls": max(0, (r.get("calls") or 0) - (r.get("ok") or 0)),
        "avg_latency_ms": int(r.get("lat") or 0),
        "models": models,
        "model_count": len(models),
        "unpriced_models": [m["model"] for m in models
                            if not m["priced"] and m["model"]],
        "start": start, "end": end,
    }


def llm_daily(days=30, end_day=None, model=None):
    """最近 N 天每日用量（缺失日期补 0，让趋势图 X 轴连续）；可按模型筛选。"""
    n, end_day, start_day, start_ts = _daily_range(days, end_day)
    where, args = "WHERE day>=? AND day<=?", [start_day, end_day]
    if model and str(model).strip():
        where += " AND IFNULL(model,'') LIKE ? ESCAPE '\\'"
        args.append("%" + _like_escape(model) + "%")
    rows = _rows("SELECT day, COUNT(*) AS calls,"
                 " COALESCE(SUM(total_tokens),0) AS tt,"
                 " COALESCE(SUM(prompt_tokens),0) AS pt,"
                 " COALESCE(SUM(completion_tokens),0) AS ct,"
                 " COALESCE(SUM(cost),0) AS cost FROM llm_usage " +
                 where + " GROUP BY day", args)
    m = {x["day"]: x for x in rows}
    out = []
    for i in range(n + 1):
        d = _day(start_ts + i * 86400)
        x = m.get(d)
        out.append({"day": d,
                    "calls": x["calls"] if x else 0,
                    "total_tokens": x["tt"] if x else 0,
                    "prompt_tokens": x["pt"] if x else 0,
                    "completion_tokens": x["ct"] if x else 0,
                    "cost": round(x["cost"], 4) if x else 0.0})
    return out


def _attach_llm_names(rows):
    """给用量明细补上「模型库」里的纳管名称，页面才能看出到底用了哪个模型。"""
    try:
        regs = _rows("SELECT id, name, provider, model FROM llm_models")
        by_id = {r["id"]: r for r in regs}
        by_model = {}
        for r in regs:
            by_model.setdefault(r["model"], []).append(r)
        for row in rows:
            m = by_id.get(row.get("llm_id"))
            inferred = False
            if not m and row.get("model"):
                cand = by_model.get(row["model"]) or []
                if len(cand) == 1:            # 历史行没记 id，但上游 ID 唯一时能推断
                    m, inferred = cand[0], True
            row["llm_name"] = (m or {}).get("name") or ""
            row["llm_provider"] = (m or {}).get("provider") or ""
            row["llm_registered"] = bool(m) and not inferred
            row["llm_inferred"] = inferred
    except Exception:
        for row in rows:
            row.setdefault("llm_name", "")
            row.setdefault("llm_provider", "")
            row.setdefault("llm_registered", False)
            row.setdefault("llm_inferred", False)
    return rows


def llm_registry_usage(start=None, end=None, model=None):
    """以「AI 大模型」页纳管的模型为主体对照用量（含零用量模型）。

    按 llm_id 精确匹配 —— 两个纳管模型共用同一个上游 model ID 时也不会串账。
    历史 / 未纳管流水（llm_id 为空，或指向已删除的模型）单独归一组按 model 列出，
    避免「模型库里明明有这个模型，用量却查不到归属」。
    """
    clause, args = _range_where(start, end)
    args = list(args)
    if model and str(model).strip():
        clause = (clause + " AND " if clause else "WHERE ") + \
            "IFNULL(model,'') LIKE ? ESCAPE '\\'"
        args.append("%" + _like_escape(model) + "%")
    regs = _rows("SELECT id, name, provider, model, base_url, enabled "
                 "FROM llm_models ORDER BY id")
    default_id = default_llm_id()
    by_id = {}
    for r in _rows("SELECT llm_id, COUNT(*) AS calls,"
                   " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
                   " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
                   " COALESCE(SUM(total_tokens),0) AS total_tokens,"
                   " COALESCE(SUM(cost),0) AS cost,"
                   " COALESCE(AVG(latency_ms),0) AS avg_latency_ms"
                   " FROM llm_usage " + clause + " GROUP BY llm_id", args):
        by_id[r["llm_id"]] = r
    items = []
    for reg in regs:
        u = by_id.get(reg["id"]) or {}
        items.append({
            "llm_id": reg["id"], "name": reg["name"],
            "provider": reg["provider"], "model": reg["model"],
            "base_url": reg["base_url"], "enabled": bool(reg["enabled"]),
            "is_default": reg["id"] == default_id,
            "calls": u.get("calls") or 0,
            "prompt_tokens": u.get("prompt_tokens") or 0,
            "completion_tokens": u.get("completion_tokens") or 0,
            "total_tokens": u.get("total_tokens") or 0,
            "cost": round(u.get("cost") or 0, 4),
            "avg_latency_ms": int(u.get("avg_latency_ms") or 0),
        })
    extra = "(llm_id IS NULL OR llm_id NOT IN (SELECT id FROM llm_models))"
    w2 = (clause + " AND " if clause else "WHERE ") + extra
    un = _rows("SELECT IFNULL(model,'（未知）') AS model, COUNT(*) AS calls,"
               " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
               " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
               " COALESCE(SUM(total_tokens),0) AS total_tokens,"
               " COALESCE(SUM(cost),0) AS cost,"
               " COALESCE(AVG(latency_ms),0) AS avg_latency_ms"
               " FROM llm_usage " + w2 +
               " GROUP BY model ORDER BY total_tokens DESC", args)
    for u in un:
        u["cost"] = round(u.get("cost") or 0, 4)
        u["avg_latency_ms"] = int(u.get("avg_latency_ms") or 0)
    try:
        from admin import pricing as _pr
        for it in items + un:
            it["priced"] = _pr.get_pricing(it.get("model")).get("kind") != "unknown"
    except Exception:
        for it in items + un:
            it["priced"] = False
    return {"items": items, "unmanaged": un, "default_id": default_id}


def llm_records(start=None, end=None, model=None, limit=50, offset=0, order="desc"):
    """大模型调用明细：日期区间 + 模型模糊匹配。"""
    where, args = [], []
    if start:
        where.append("day>=?"); args.append(str(start)[:10])
    if end:
        where.append("day<=?"); args.append(str(end)[:10])
    if model and str(model).strip():
        where.append("IFNULL(model,'') LIKE ? ESCAPE '\\'")
        args.append("%" + _like_escape(model) + "%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    order_sql = "ASC" if order == "asc" else "DESC"
    total = _one("SELECT COUNT(*) AS n FROM llm_usage " + clause, args)["n"]
    rows = _rows("SELECT * FROM llm_usage " + clause +
                 " ORDER BY ts " + order_sql + " LIMIT ? OFFSET ?",
                 args + [int(limit), int(offset)])
    return {"total": total, "items": _time_str(_attach_llm_names(rows))}


def llm_all_for_export(start=None, end=None, model=None, cap=100000):
    return llm_records(start, end, model, limit=cap, offset=0, order="asc")["items"]


# ---------- 知识库 HTTP 接口调用流水 ----------

def log_api_call(question=None, answer=None, n_images=None, q_chars=None,
                 session_id=None, client_ip=None, status=200, ok=True,
                 latency_ms=None, calls=None, total_tokens=None, cost=None, error=None):
    """一次 /httpapi/query 外部请求一行；鉴权失败 / 限流 / 参数错**也要记**。

    费用用调用方在锁内算好的值（kbapi._api_cost），不能在这里读 brain 属性——
    锁已放开，那是另一个请求的用量，会记到别人头上。
    """
    try:
        now = time.time()
        _write("INSERT INTO api_calls(ts,day,question,answer,n_images,q_chars,session_id,"
               "client_ip,status,ok,latency_ms,calls,total_tokens,cost,error) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (now, _day(now), question, answer, n_images, q_chars,
                (session_id or "")[:64] or None, client_ip, int(status or 0),
                1 if ok else 0, latency_ms, calls, total_tokens,
                round(float(cost or 0.0), 6), (error or "")[:200] or None))
    except Exception:
        pass


def api_calls_summary(start=None, end=None):
    """区间汇总 + 失败原因分布。total/ok 是旧键，/api/stats 一直按它读。"""
    clause, args = _range_where(start, end)
    r = _one("SELECT COUNT(*) AS calls, COALESCE(SUM(ok),0) AS ok_calls,"
             " COALESCE(SUM(total_tokens),0) AS tt, COALESCE(SUM(cost),0) AS cost,"
             " COALESCE(SUM(n_images),0) AS imgs, COALESCE(SUM(calls),0) AS llm_calls,"
             " COALESCE(AVG(latency_ms),0) AS lat, COALESCE(AVG(q_chars),0) AS qc,"
             " COUNT(DISTINCT IFNULL(session_id,'')) AS sessions,"
             " COUNT(DISTINCT IFNULL(client_ip,'')) AS ips"
             " FROM api_calls " + clause, args) or {}
    # 失败原因分布：ok=0 必须追加进 where 列表再拼，不能写成 clause + " AND ok=0"
    w2 = []
    if start:
        w2.append("day>=?")
    if end:
        w2.append("day<=?")
    w2.append("ok=0")
    errs = _rows("SELECT IFNULL(error,'(未知原因)') AS e, COUNT(*) AS c FROM api_calls"
                 " WHERE " + " AND ".join(w2) +
                 " GROUP BY e ORDER BY c DESC LIMIT 10", list(args))
    calls = r.get("calls") or 0
    ok_calls = r.get("ok_calls") or 0
    return {
        "total": calls, "ok": ok_calls,
        "calls": calls, "ok_calls": ok_calls,
        "fail_calls": max(0, calls - ok_calls),
        "total_tokens": r.get("tt") or 0,
        "cost": round(r.get("cost") or 0, 4),
        "images": r.get("imgs") or 0,
        "llm_calls": r.get("llm_calls") or 0,
        "avg_latency_ms": int(r.get("lat") or 0),
        "avg_q_chars": int(r.get("qc") or 0),
        "sessions": r.get("sessions") or 0,
        "ips": r.get("ips") or 0,
        "errors": [{"error": x["e"], "count": x["c"]} for x in errs],
        "start": start, "end": end,
    }


def api_calls_daily(days=30, end_day=None):
    """最近 N 天每日调用量（成功 / 失败分开，缺失补 0）。"""
    n, end_day, start_day, start_ts = _daily_range(days, end_day)
    rows = _rows("SELECT day, COUNT(*) AS calls, COALESCE(SUM(ok),0) AS ok,"
                 " COALESCE(SUM(total_tokens),0) AS tt, COALESCE(SUM(cost),0) AS cost,"
                 " COALESCE(SUM(n_images),0) AS imgs, COALESCE(AVG(latency_ms),0) AS lat"
                 " FROM api_calls WHERE day>=? AND day<=? GROUP BY day",
                 (start_day, end_day))
    m = {x["day"]: x for x in rows}
    out = []
    for i in range(n + 1):
        d = _day(start_ts + i * 86400)
        x = m.get(d)
        out.append({"day": d,
                    "calls": x["calls"] if x else 0,
                    "ok": x["ok"] if x else 0,
                    "fail": (x["calls"] - x["ok"]) if x else 0,
                    "total_tokens": x["tt"] if x else 0,
                    "cost": round(x["cost"], 4) if x else 0.0,
                    "images": x["imgs"] if x else 0,
                    "avg_latency_ms": int(x["lat"] or 0) if x else 0})
    return out


def api_calls_records(limit=50, offset=0, start=None, end=None,
                      keyword=None, only_failed=False, order="desc"):
    """API 调用明细：日期 + 关键字 + 只看失败。

    前两个位置参数保持 limit/offset（老路由 /api/apicalls 就是这么调的）。
    keyword 一个框覆盖 提问 / 答案 / 会话 ID / 调用方 IP / 失败原因。
    IFNULL 必写：失败行 answer 是 NULL，直接 LIKE 会静默漏掉整行。
    """
    where, args = [], []
    if start:
        where.append("day>=?"); args.append(str(start)[:10])
    if end:
        where.append("day<=?"); args.append(str(end)[:10])
    if only_failed:
        where.append("ok=0")
    if keyword and str(keyword).strip():
        kw = "%" + _like_escape(keyword) + "%"
        where.append("(IFNULL(question,'') LIKE ? ESCAPE '\\'"
                     " OR IFNULL(answer,'') LIKE ? ESCAPE '\\'"
                     " OR IFNULL(session_id,'') LIKE ? ESCAPE '\\'"
                     " OR IFNULL(client_ip,'') LIKE ? ESCAPE '\\'"
                     " OR IFNULL(error,'') LIKE ? ESCAPE '\\')")
        args += [kw] * 5
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    order_sql = "ASC" if order == "asc" else "DESC"
    total = _one("SELECT COUNT(*) AS n FROM api_calls " + clause, args)["n"]
    rows = _rows("SELECT * FROM api_calls " + clause +
                 " ORDER BY ts " + order_sql + " LIMIT ? OFFSET ?",
                 args + [int(limit), int(offset)])
    return {"total": total, "items": _time_str(rows)}


def api_calls_all_for_export(start=None, end=None, keyword=None,
                             only_failed=False, cap=50000):
    return api_calls_records(limit=cap, offset=0, start=start, end=end,
                             keyword=keyword, only_failed=only_failed,
                             order="asc")["items"]


# ---------------------------------------------------------------- 通讯录缓存

def _avatar_url(v):
    """v2 联系人 / 群成员的 avatar 是微信 CDN 直链：只留 http(s)，挡掉脏数据。

    浏览器渲染时用 referrerpolicy="no-referrer"，否则 CDN 只回 120×120 占位图。
    """
    s = str(v or "").strip()
    if not s.startswith(("http://", "https://")):
        return ""
    if any(ch in s for ch in " \t\r\n\"'<>`"):
        return ""
    return s


def save_groups(groups):
    now = time.time()

    def _do(conn):
        for g in groups or []:
            wxid = str(g.get("wxid") or g.get("id") or "").strip()
            if not wxid:
                continue
            conn.execute("INSERT INTO wx_groups(wxid,name,member_count,source,synced_at) "
                         "VALUES(?,?,?,?,?) ON CONFLICT(wxid) DO UPDATE SET "
                         "name=excluded.name, member_count=excluded.member_count, "
                         "source=excluded.source, synced_at=excluded.synced_at",
                         (wxid, g.get("name") or "", int(g.get("memberCount") or g.get("member_count") or 0),
                          g.get("source") or "", now))
    _write_many(_do)


def list_groups():
    rows = _rows("SELECT * FROM wx_groups ORDER BY name")
    for r in rows:
        r["synced_str"] = time.strftime("%m-%d %H:%M", time.localtime(r["synced_at"] or 0))
        r["cached_members"] = _one(
            "SELECT COUNT(*) AS n FROM wx_group_members WHERE group_id=?", (r["wxid"],))["n"]
    return rows


def save_contacts(contacts):
    import json
    now = time.time()

    def _do(conn):
        for c in contacts or []:
            wxid = str(c.get("wxid") or "").strip()
            if not wxid:
                continue
            conn.execute("INSERT INTO wx_contacts(wxid,name,nick_name,remark,type,is_group,"
                         "source,detail,avatar,synced_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                         "ON CONFLICT(wxid) DO UPDATE SET name=excluded.name, "
                         "nick_name=excluded.nick_name, remark=excluded.remark, "
                         "type=excluded.type, is_group=excluded.is_group, "
                         "source=excluded.source, detail=excluded.detail, "
                         "avatar=excluded.avatar, synced_at=excluded.synced_at",
                         (wxid, c.get("name") or "", c.get("nickName") or "",
                          c.get("remark") or "", c.get("type") or "",
                          1 if c.get("isGroup") else 0, c.get("source") or "",
                          json.dumps(c, ensure_ascii=False),
                          _avatar_url(c.get("avatar")), now))
    _write_many(_do)


def list_friends():
    rows = _rows("SELECT * FROM wx_contacts WHERE is_group=0 ORDER BY name, wxid")
    for r in rows:
        r["synced_str"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["synced_at"] or 0))
    return rows


def avatar_map():
    """wxid -> 头像 CDN 直链：好友表优先，群成员表兜底。

    朋友圈发布者不一定在好友列表里（同群的陌生人也能刷到），所以两张缓存表
    都查；只返回 http(s) 且无空白 / 引号的值，避免把脏数据交给后端去请求。
    表还没建（通讯录从未同步）时返回空字典，绝不让朋友圈 feed 因此失败。
    """
    out = {}
    try:
        # 先群成员、后好友：同 wxid 时好友表的记录覆盖群成员
        for table in ("wx_group_members", "wx_contacts"):
            rows = _rows("SELECT wxid, avatar FROM %s "
                         "WHERE avatar IS NOT NULL AND avatar != ''" % table)
            for r in rows:
                u = _avatar_url(r.get("avatar"))
                if u:
                    out[str(r.get("wxid") or "")] = u
    except Exception:
        return out
    return out


def get_contact(wxid):
    r = _one("SELECT * FROM wx_contacts WHERE wxid=?", (str(wxid or ""),))
    if r:
        import json
        try:
            r["detail"] = json.loads(r.get("detail") or "{}")
        except Exception:
            r["detail"] = {}
        recent = _one("SELECT ts, question FROM events WHERE sender=? OR chat=? "
                      "ORDER BY ts DESC LIMIT 1", (r["wxid"], r["wxid"]))
        if recent:
            r["last_seen"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(recent["ts"]))
            r["last_question"] = recent.get("question") or ""
    return r


def self_display_name(wxid=""):
    """从本地通讯录缓存找当前登录账号的显示名（插件查不到自己时兜底）。

    插件的 /api/contacts/find?wxid= 未必包含本机账号；但同步过群成员后
    `wx_group_members` 里会有 is_self=1 的记录，其 display_name 就是群里显示的自己。
    """
    r = _one("SELECT name, nick_name, display_name FROM wx_group_members "
             "WHERE is_self=1 AND TRIM(IFNULL(name,''))<>'' LIMIT 1")
    if r:
        # `name` 是全局通讯录显示名；display_name 可能是某个群里的群名片，
        # 不适合当「当前微信昵称」，只在没有 name 时才退而求其次。
        return str(r.get("name") or "").strip()
    r = _one("SELECT nick_name, display_name FROM wx_group_members "
             "WHERE is_self=1 LIMIT 1")
    if r:
        return str(r.get("nick_name") or r.get("display_name") or "").strip()
    c = get_contact(wxid) if wxid else None
    if c:
        return str(c.get("remark") or c.get("name") or c.get("nick_name") or "").strip()
    return ""


def save_group_members(group_id, members):
    now = time.time()

    def _do(conn):
        for m in members or []:
            wxid = str(m.get("wxid") or "").strip()
            if not wxid:
                continue
            conn.execute("INSERT INTO wx_group_members(group_id,wxid,name,nick_name,remark,"
                         "group_nick,display_name,is_self,is_owner,avatar,synced_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(group_id,wxid) DO UPDATE SET "
                         "name=excluded.name,nick_name=excluded.nick_name,remark=excluded.remark,"
                         "group_nick=excluded.group_nick,display_name=excluded.display_name,"
                         "is_self=excluded.is_self,is_owner=excluded.is_owner,"
                         "avatar=excluded.avatar,synced_at=excluded.synced_at",
                         (str(group_id), wxid, m.get("name") or "", m.get("nickName") or "",
                          m.get("remark") or "", m.get("groupNick") or "",
                          m.get("displayName") or m.get("name") or "",
                          1 if m.get("isSelf") else 0, 1 if m.get("isOwner") else 0,
                          _avatar_url(m.get("avatar")), now))
    _write_many(_do)


def search_contacts(q, limit=200):
    """全量通讯录搜索：好友 / 群聊 / 群成员一起按 wxid、name 模糊匹配。

    只读本地同步缓存，不查手机插件。LIKE 通配符（% _ \\）先转义再用
    ESCAPE '\\'，避免用户输入变成通配或注入；结果按「好友 → 群聊 → 群友」
    分组、同组内按名称排序，最多 limit 条。
    """
    key = str(q or "").strip()
    if not key or len(key) > 64:
        return []
    esc = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = "%" + esc + "%"
    n = max(1, min(200, int(limit or 200)))
    out = []

    def hit(row, fields, kind, extra=None):
        d = {
            "kind": kind,
            "wxid": row.get("wxid") or "",
            "name": row.get("name") or "",
            "nick_name": row.get("nick_name") or "",
            "remark": row.get("remark") or "",
            "avatar": row.get("avatar") or "",
            "source": row.get("source") or "",
            "synced_str": time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(row.get("synced_at") or 0)),
            "match": next((f for f in fields
                           if key.lower() in str(row.get(f) or "").lower()), ""),
        }
        if extra:
            d.update(extra)
        return d

    # 好友 / 联系人缓存（is_group=0 是好友；带 is_group 的群也一并列出）
    for r in _rows(
        "SELECT wxid,name,nick_name,remark,avatar,source,is_group,synced_at "
        "FROM wx_contacts WHERE wxid LIKE ? ESCAPE '\\' "
        "OR name LIKE ? ESCAPE '\\' OR nick_name LIKE ? ESCAPE '\\' "
        "OR remark LIKE ? ESCAPE '\\' LIMIT ?",
        (like, like, like, like, n)):
        kind = "group" if r.get("is_group") else "friend"
        out.append(hit(r, ("wxid", "name", "nick_name", "remark"), kind))

    # 群聊（独立群表，含成员数）
    for r in _rows(
        "SELECT wxid,name,source,synced_at,member_count FROM wx_groups "
        "WHERE wxid LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' LIMIT ?",
        (like, like, n)):
        out.append(hit(r, ("wxid", "name"), "group",
                       {"member_count": r.get("member_count") or 0}))

    # 群成员：同一个人在多个群里会出现多行，带上所属群名方便定位
    for r in _rows(
        "SELECT m.wxid,m.name,m.nick_name,m.remark,m.group_nick,m.display_name,"
        "m.avatar,m.group_id,m.is_self,m.is_owner,m.synced_at,"
        "g.name AS group_name FROM wx_group_members m "
        "LEFT JOIN wx_groups g ON g.wxid=m.group_id "
        "WHERE m.wxid LIKE ? ESCAPE '\\' OR m.name LIKE ? ESCAPE '\\' "
        "OR m.nick_name LIKE ? ESCAPE '\\' OR m.remark LIKE ? ESCAPE '\\' "
        "OR m.group_nick LIKE ? ESCAPE '\\' OR m.display_name LIKE ? ESCAPE '\\' "
        "LIMIT ?",
        (like, like, like, like, like, like, n)):
        d = hit(r, ("wxid", "name", "nick_name", "remark", "group_nick",
                    "display_name"), "member", {
            "shown_name": r.get("display_name") or r.get("name") or r.get("wxid"),
            "group_id": r.get("group_id") or "",
            "group_name": r.get("group_name") or "",
            "group_nick": r.get("group_nick") or "",
            "is_self": bool(r.get("is_self")),
            "is_owner": bool(r.get("is_owner")),
            "source": "groupMembers",
        })
        out.append(d)

    order = {"friend": 0, "group": 1, "member": 2}
    out.sort(key=lambda x: (order.get(x["kind"], 9),
                            (x.get("name") or x["wxid"]).lower()))
    # 同一 wxid 在好友表和多个群里重复时去重，保留信息更全的一条
    seen, uniq = set(), []
    for d in out:
        k = (d["kind"], d["wxid"], d.get("group_id") or "")
        if k not in seen:
            seen.add(k)
            uniq.append(d)
        if len(uniq) >= n:
            break
    return uniq


def list_group_members(group_id):
    rows = _rows("SELECT * FROM wx_group_members WHERE group_id=? "
                 "ORDER BY is_owner DESC, is_self DESC, display_name", (str(group_id),))
    for r in rows:
        r["is_self"] = bool(r["is_self"])
        r["is_owner"] = bool(r["is_owner"])
    return rows


# ---------------------------------------------------------------- AI 大模型管理

LLM_DEFAULT_KEY = "default_llm_id"


def _mask_key(k):
    """管理端只显示掩码，绝不把明文密钥发到页面（截图/演示安全）。"""
    k = str(k or "")
    if not k:
        return ""
    return "********" + (k[-4:] if len(k) > 8 else "")


def list_llm_models():
    rows = _rows("SELECT * FROM llm_models ORDER BY id")
    out = []
    for r in rows:
        d = dict(r)
        d["api_key_masked"] = _mask_key(d.pop("api_key", ""))
        d["enabled"] = bool(d["enabled"])
        d["stream"] = bool(d["stream"])
        out.append(d)
    return out


def get_llm_model(key):
    """key 可以是 id / 名称 / 上游模型 ID；只返回启用的模型，含明文 api_key。"""
    if key is None or str(key).strip() == "":
        return None
    k = str(key).strip()
    row = _one("SELECT * FROM llm_models WHERE name=? AND enabled=1", (k,))
    if not row and k.isdigit():
        row = _one("SELECT * FROM llm_models WHERE id=? AND enabled=1", (int(k),))
    if not row:
        row = _one("SELECT * FROM llm_models WHERE model=? AND enabled=1 "
                   "ORDER BY id LIMIT 1", (k,))
    return dict(row) if row else None


def default_llm_id():
    try:
        return int(get_setting(LLM_DEFAULT_KEY, "") or 0)
    except (TypeError, ValueError):
        return 0


def set_default_llm(mid):
    """mid 为空 / 0 = 清除默认，brain 回退到 config.yaml 的 brain.external_llm。"""
    if mid in (None, "", 0, "0"):
        _write("DELETE FROM settings WHERE k=?", (LLM_DEFAULT_KEY,))
        return 0
    row = _one("SELECT id FROM llm_models WHERE id=? AND enabled=1", (int(mid),))
    if not row:
        raise ValueError("模型不存在或已停用，不能设为默认")
    set_setting(LLM_DEFAULT_KEY, int(row["id"]))
    return int(row["id"])


def save_llm_model(data):
    """新增 / 编辑模型。编辑时 api_key 留空表示「不修改」。

    返回模型 id；校验失败抛 ValueError（管理端会转成 400 提示）。
    """
    d = data or {}
    name = str(d.get("name") or "").strip()
    if not name:
        raise ValueError("模型名称不能为空")
    if len(name) > 60:
        raise ValueError("模型名称最长 60 字")
    base_url = str(d.get("base_url") or "").strip().rstrip("/")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
    model = str(d.get("model") or "").strip()
    if not model:
        raise ValueError("模型 ID 不能为空")
    try:
        temp = float(d.get("temperature", 0.3))
    except (TypeError, ValueError):
        raise ValueError("temperature 必须是数字")
    if not (0 <= temp <= 2):
        raise ValueError("temperature 需在 0~2 之间")

    def _int(v, default, lo, hi):
        try:
            n = int(v if v not in (None, "") else default)
        except (TypeError, ValueError):
            raise ValueError("超时 / token 参数必须是整数")
        return max(lo, min(hi, n))

    mid = d.get("id")
    old = None
    if mid:
        old = _one("SELECT * FROM llm_models WHERE id=?", (int(mid),))
        if not old:
            raise ValueError("模型不存在")
    dup = _one("SELECT id FROM llm_models WHERE name=? AND id<>?",
               (name, int(mid or 0)))
    if dup:
        raise ValueError("已存在同名模型「%s」" % name)
    # 编辑时留空 = 保留原密钥；新增时空密钥也允许（本地网关常不需鉴权）
    api_key = str(d.get("api_key") or "").strip() or (old["api_key"] if old else "")
    vals = (name, str(d.get("provider") or "").strip(), base_url, api_key, model,
            temp, 1 if d.get("stream", True) else 0,
            _int(d.get("max_tokens"), 0, 0, 200000),
            _int(d.get("connect_timeout"), 10, 1, 300),
            _int(d.get("read_timeout"), 60, 1, 3600),
            _int(d.get("total_timeout"), 240, 1, 7200),
            1 if d.get("enabled", True) else 0)
    now = time.time()
    if old:
        _write("UPDATE llm_models SET name=?,provider=?,base_url=?,api_key=?,model=?,"
               "temperature=?,stream=?,max_tokens=?,connect_timeout=?,read_timeout=?,"
               "total_timeout=?,enabled=?,updated_at=? WHERE id=?", vals + (now, int(mid)))
        return int(mid)
    return int(_write(
        "INSERT INTO llm_models(name,provider,base_url,api_key,model,temperature,stream,"
        "max_tokens,connect_timeout,read_timeout,total_timeout,enabled,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals + (now, now)))


def delete_llm_model(mid):
    row = _one("SELECT * FROM llm_models WHERE id=?", (int(mid),))
    if not row:
        raise ValueError("模型不存在")

    def _do(conn):
        conn.execute("DELETE FROM llm_models WHERE id=?", (int(mid),))
        # 删掉的正好是默认模型时一并清除，brain 才会安全回退到 config.yaml
        conn.execute("DELETE FROM settings WHERE k=? AND v=?",
                     (LLM_DEFAULT_KEY, str(mid)))
    _write_many(_do)
    return {"name": row["name"]}


def resolve_llm_config(model=None):
    """brain 用：本次生成实际生效的 external_llm 风格配置 dict；找不到返回 None。

    优先级：调用方显式指定的 model > 管理端选定的默认模型 > None（回退 config.yaml）。
    每次生成都查一次库，所以后台切换默认模型**无需重启主服务**。
    """
    try:
        if model:
            row = get_llm_model(model)
            if not row:
                return None          # 显式指定了但查不到，不偷偷换成别家模型
            source = "override"
        else:
            mid = default_llm_id()
            row = _one("SELECT * FROM llm_models WHERE id=? AND enabled=1", (mid,)) \
                if mid else None
            source = "default"
        if not row:
            return None
        return {
            "id": row["id"], "name": row["name"], "source": source,
            "base_url": row["base_url"], "api_key": row["api_key"],
            "model": row["model"], "temperature": row["temperature"],
            "stream": bool(row["stream"]),
            "max_tokens": int(row["max_tokens"] or 0),
            "connect_timeout": int(row["connect_timeout"] or 10),
            "read_timeout": int(row["read_timeout"] or 60),
            "total_timeout": int(row["total_timeout"] or 240),
            # 这两个是 brain 的可选开关，表里不单独存，沿用 config.yaml 的默认值
            "partial_min_chars": 20, "stream_usage": True,
        }
    except Exception:
        return None                   # DB 异常绝不拖垮回复链路，回退 config.yaml


def llm_models_status(cfg_fallback=None):
    """管理端概览：默认 id + 当前实际生效模型（遮蔽密钥）。"""
    c = resolve_llm_config()
    if c:
        eff = {"name": c["name"], "model": c["model"],
               "base_url": c["base_url"], "source": "default"}
    else:
        f = cfg_fallback or {}
        eff = {"name": "config.yaml · brain.external_llm",
               "model": f.get("model") or "", "base_url": f.get("base_url") or "",
               "source": "config"}
    return {"default_id": default_llm_id(), "effective": eff}
