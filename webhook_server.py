"""个人微信智能客服 · 主服务

启动：
    python webhook_server.py                       # 开发（Flask 自带服务器）
    gunicorn -c gunicorn.conf.py webhook_server:app  # 生产（workers=1 + 多线程）

同时提供两类入口：
  POST /webhook          个人微信插件推送的消息（webhook 模式，立即 200、后台线程处理）
  POST /httpapi/query        纯接口模式的知识库问答（沿用原项目 kbapi，支持 policy / sources）
  GET  /healthz          健康检查
  POST /internal/reload  后台「重建索引并重载」用（清缓存后重新构建检索器）

消息链路：
  iOS 插件 → POST /webhook → 归一化/去重/策略判定
    → 文本·引用回复·语音(ASR) 交给 brain 混合检索 + LLM
    → 只检索该策略包含的知识库文档 → POST /api/send/text 回微信
  · 图片/视频 → 直接回「暂不支持」
  · 自己发出的消息（outgoing / direction=out）→ 忽略，防自问自答
"""
import atexit
import contextvars
import hmac
import json
import logging
import logging.handlers
import os
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)                     # data/ 下的配置、日志与知识库都是相对路径
# 全新部署 / Docker 首次启动时 data/ 可能还不存在；日志、会话、聊天记录库都在这里
os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import yaml
from flask import Flask, jsonify, request

import pluginhost
from pluginhost.consts import NAME_RE
from pluginhost.context import ReadOnlyWX

import brain as brainmod
import chatlog
import followup
import kbapi
import logfiles
import media_store
import phone_mon
import policy
import store
import textutil
import voice
from wx_client import WXClient, WXClientError

# ---------------------------------------------------------------- 配置


def _load_config():
    """WXBOT_CONFIG 可指定别的配置文件（多环境 / 自动化测试用）。

    默认读 `data/config.yaml`（所有可变数据都在 data/，Docker 里单独挂卷）；
    全新部署还没配置时回退到配置模板（`data/config.yaml.example`，
    根目录是历史位置，保留以兼容老部署）。
    """
    custom = os.environ.get("WXBOT_CONFIG")
    if custom and os.path.isfile(custom):
        with open(custom, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    p = os.path.join(BASE_DIR, "data", "config.yaml")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    for tpl in (os.path.join(BASE_DIR, "data", "config.yaml.example"),
                os.path.join(BASE_DIR, "config.yaml.example")):
        if os.path.isfile(tpl):
            with open(tpl, encoding="utf-8") as f:
                c = yaml.safe_load(f) or {}
            print("[warn] 未找到 data/config.yaml，已使用 %s（请复制后填写密钥）" % tpl)
            return c
    raise RuntimeError("缺少 data/config.yaml / data/config.yaml.example")


CFG = _load_config()
WCFG = CFG.get("wechat") or {}
BCFG = CFG.get("brain") or {}
RCFG = CFG.get("reply") or {}
WHCFG = CFG.get("webhook") or {}
VCFG = CFG.get("voice") or {}
SCFG = CFG.get("server") or {}
ICFG = (BCFG.get("images") or {})
TCFG = CFG.get("transfer") or {}
PCFG = CFG.get("plugins") or {}
PSCFG = PCFG.get("scheduler") or {}          # P3 定时任务
PHCFG = PCFG.get("http_routes") or {}        # P3 /ext 插件扩展路由
MCFG = CFG.get("media_store") or {}          # 1.1.1-beta1 图片/文件存储

def _reply_gap():
    """连续两条回复（文字→配图、配图→配图）之间的间隔，秒。

    `reply.interval_seconds`，范围 0-30，默认 1；越界自动夹到边界，
    配置写错（非数字）也安全回退到 1 秒，不让主服务因一个数值崩掉。
    """
    try:
        gap = float(RCFG.get("interval_seconds", 1))
    except (TypeError, ValueError):
        gap = 1.0
    return min(30.0, max(0.0, gap))


# ASR 用外部 LLM 的 base_url/key 兜底（provider=custom 时复用）
VCFG = dict(VCFG)
VCFG.setdefault("_llm_base_url", (BCFG.get("external_llm") or {}).get("base_url") or "")
VCFG.setdefault("_llm_api_key", (BCFG.get("external_llm") or {}).get("api_key") or "")

# ---------------------------------------------------------------- 日志
# 按天一个文件、每类最多 5 个，统一由 logfiles.py 管理（data/logs/）。


# ---------------------------------------------------------------- 运行日志上下文
# 旧日志只有「已回复 | 示例成员 | text」，LLM 上下文 / 流式返回等行更是完全没有会话
# 标识，排查时根本分不清是哪条消息。用 contextvars 在消息入口设一次，Formatter
# 自动给每行补前缀；brain / voice / wx_client 都通过 log=log.info 复用同一个
# logger，所以一处注入即可覆盖整条链路，不必逐个改 log 调用。
_MSG_CTX = contextvars.ContextVar("wxbot_msg_ctx", default="")


def _brief(s, n=60):
    """日志里的问题摘要：折叠换行、限长，避免一行撑爆屏幕。"""
    t = " ".join(str(s or "").split())
    return (t[:n] + "…") if len(t) > n else (t or "-")


def _msg_tag(m):
    scene = {"group": "群聊", "private": "私聊"}.get(m.get("chat_type"), "其它")
    chat = str(m.get("chat") or "-")
    name = str(m.get("chat_name") or chat)
    sender = str(m.get("sender") or "-")
    sname = str(m.get("sender_name") or sender)
    who = sname if sname == sender else "%s(%s)" % (sname, sender)
    return "[%s·%s(%s) | 发送者=%s] " % (scene, name, chat, who)


class _CtxFormatter(logging.Formatter):
    """只补一个模板变量，不碰 record.msg / args。

    会话名里就算含 % 也不会干扰 logging 的 % 格式化；两个 handler 共享
    同一个 Formatter 实例也没有状态问题。
    """

    def format(self, record):
        record.msgctx = _MSG_CTX.get()
        return super().format(record)


def _setup_log():
    lv = os.environ.get("LOG_LEVEL") or SCFG.get("log_level") or "INFO"
    logger = logging.getLogger("wxbot")
    logger.setLevel(getattr(logging, str(lv).upper(), logging.INFO))
    # 时间 → 级别 → 会话/发送者上下文 → 正文，按阅读顺序排
    fmt = _CtxFormatter("%(asctime)s [%(levelname)s] %(msgctx)s%(message)s",
                        "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    try:
        fh = logfiles.daily_handler("webhook", "WXBOT_LOG", fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    for noisy in ("urllib3", "requests", "werkzeug", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logger


log = logging.getLogger("wxbot")
_setup_log()

# ---------------------------------------------------------------- 全局对象

store.init_db()
WX = WXClient(WCFG.get("base_url") or "http://127.0.0.1:18888",
              WCFG.get("token") or "", WCFG.get("timeout") or 10, log=log.info)
# 图片/文件存储：默认开启，可用 media_store.enabled: false 关闭。
MEDIA_ENABLED = bool(MCFG.get("enabled", True))
# 引用消息里的图片也自动下载（文档 7.2.1 quoteResolved）；可用 quote_image: false 关闭。
QUOTE_IMAGE_ENABLED = bool(MCFG.get("quote_image", True))
if MEDIA_ENABLED:
    # 兼容旧配置 index_ttl_minutes：现在流水不再按 30 分钟删，改为 record_ttl_days；
    # 没配 record_ttl_days 就与磁盘文件同寿（file_ttl_days）。
    try:
        _m_ttl = int(MCFG.get("index_ttl_minutes", 30)) * 60
    except (TypeError, ValueError):
        _m_ttl = 30 * 60
    try:
        _f_ttl = int(MCFG.get("file_ttl_days", 7)) * 86400
    except (TypeError, ValueError):
        _f_ttl = 7 * 86400
    try:
        _r_ttl = int(MCFG.get("record_ttl_days", 0)) * 86400
    except (TypeError, ValueError):
        _r_ttl = 0
    if _r_ttl <= 0:
        _r_ttl = _f_ttl
    media_store.configure(index_ttl=_m_ttl, file_ttl=_f_ttl, record_ttl=_r_ttl,
                          images_dir=MCFG.get("images_dir"),
                          files_dir=MCFG.get("files_dir"),
                          image_wait=MCFG.get("image_wait"),
                          image_poll=MCFG.get("image_poll"))
    log.info("媒体存储已启用 | 图片=%s | 文件=%s | 流水保留=%s天 | 画质等待=%ss(轮询%ss)",
             media_store.CFG["images_dir"], media_store.CFG["files_dir"],
             media_store.CFG["record_ttl"] // 86400,
             media_store.CFG["image_wait"], media_store.CFG["image_poll"])
SELF_WXID_CFG = str(WCFG.get("self_wxid") or "").strip()
_self_cache = {"wxid": SELF_WXID_CFG, "ts": 0.0}


def self_wxid():
    """本机微信号：配置里填了就用配置，否则从 /api/health 自动取（带缓存与失败节流）。"""
    if SELF_WXID_CFG:
        return SELF_WXID_CFG
    interval = 300 if _self_cache["wxid"] else 30
    if time.time() - _self_cache["ts"] < interval:
        return _self_cache["wxid"]
    _self_cache["ts"] = time.time()   # 失败也节流，避免每个请求都被离线手机拖住
    try:
        w = WX.self_wxid(refresh=True, timeout=1.2)
        if w:
            _self_cache["wxid"] = w
    except Exception as e:
        log.warning("读取插件当前账号失败（@ 判断可能不准）: %s", e)
    return _self_cache["wxid"]


# 插件状态缓存：/healthz 是管理端探活用的高频接口，绝不能每次都同步等手机
_plugin_cache = {"ts": 0.0, "data": None}


def plugin_snapshot(ttl=10):
    now = time.time()
    if now - _plugin_cache["ts"] < ttl and _plugin_cache["data"] is not None:
        return dict(_plugin_cache["data"])
    try:
        h = WX.health(timeout=1.2)
        data = {"reachable": True, "self": h.get("self") or "",
                "webhook": h.get("webhook") or "",
                "alertPushUrl": h.get("alertPushUrl") or "",
                "wechatReady": h.get("wechatReady"),
                "version": h.get("version") or ""}
        # 1.1.1-beta1：探活成功记一次活跃（服务端推算手机在线时长的依据）
        phone_mon.touch(now, source="health")
    except Exception as e:
        data = {"reachable": False, "error": str(e)[:120]}
        # 探活失败不等于离线：只在连续失败时翻转，避免抖动清零连续时长。
        phone_mon.mark_offline(now)
    _plugin_cache.update({"ts": now, "data": data})
    return dict(data)


log.info("个人微信智能客服启动 | 插件=%s | ASR=%s",
         WCFG.get("base_url"), voice.describe_asr(VCFG))
BRAIN = brainmod.HybridRAGBrain(BCFG, log=log.info)
log.info("检索器 | %s", BRAIN.describe())


class _BrainProxy:
    """转发到当前 BRAIN，让 /httpapi/query 在后台「重载索引」后自动用上新实例。"""

    def __getattr__(self, name):
        return getattr(BRAIN, name)


BRAIN_PROXY = _BrainProxy()
brain_lock = threading.Lock()          # 与 /httpapi/query 共用，保护 last_images 不串台
POOL = ThreadPoolExecutor(max_workers=int(SCFG.get("threads") or 8),
                          thread_name_prefix="wxmsg")
# 索引重建状态：后台「重建索引并重载」要能看到进度。
# state: idle | running | done | failed
# stage: 当前阶段（读取文档 / BM25 索引 / 加载向量模型 / 向量索引 …）
# pct:   该阶段内的百分比 0~100；progress: 全局百分比 0~100
RELOAD = {"state": "idle", "at": 0, "detail": "", "stage": "", "pct": 0,
          "progress": 0, "started_at": 0, "finished_at": 0, "error": ""}

# ---------------------------------------------------------------- 插件系统
# 插件只在主服务进程里加载（gunicorn.conf.py 明确 workers=1）；Web 管理端是
# 另一个进程，只通过 /internal/plugins/* 读写状态并触发重载，绝不 import 插件代码。


class _PluginHost:
    """PluginContext 门面唯一能调用的对象。

    刻意不把 WX / BRAIN / CFG 裸对象交给插件：直接 WX.send_text 会绕过 @ 规则、
    引用回复、_mark_followup 的续聊窗口刷新，以及 _event 的消息流水。
    """

    def send_text(self, m, match, text, **kw):
        return _send_text(m, match, text, **kw)

    def send_image(self, m, path):
        return _send_images(m, [path])

    def readonly_wx(self):
        return ReadOnlyWX(WX)

    def self_wxid(self):
        return self_wxid()

    def ask_brain(self, question, **kw):
        kw.setdefault("history", [])
        with brain_lock:
            return BRAIN.answer(question, **kw)

    def llm_chat(self, messages, **kw):
        """插件调用宿主内置大模型：不检索知识库，messages 原样发给模型。

        和 ask_brain 一样在锁内调用 —— brain.llm 是全进程共享状态，
        锁外改它会和客服链路的 answer() 互相串台（模型 / 密钥被换掉）。
        """
        with brain_lock:
            return BRAIN.chat_only(messages, **kw)

    def history(self, uid):
        return _history(uid)

    def remember(self, uid, question, answer):
        return _remember(uid, question, answer)

    def recent_media(self, **kw):
        """插件查询最近 N 分钟内某人/某群的图片与文件（1.1.1-beta1）。"""
        return media_store.query(**kw)

    def broadcast(self, chat, text, source=""):
        """定时任务主动发消息：不是「回复」，所以不 @、不引用、不刷续聊窗口，
        也不派发 on_reply_sent（避免插件递归）；但仍写一条回复流水，保证统计完整。
        """
        c = str(chat or "").strip()
        if not c:
            raise ValueError("定时任务发送必须指定 chat")
        WX.send_text(c, str(text))
        m = {"chat": c, "chat_type": "group", "chat_name": "", "sender": "",
             "sender_name": "", "type_name": "text", "text": "", "local_id": 0}
        _event(m, {"chat_type": "group", "policy_id": None,
                   "policy_name": "", "trigger": "", "sources": None},
               kind="reply", question="[定时任务] %s" % (source or "plugin"),
               answer=str(text), ok=True, n_images=0)
        return True


PLUGIN_HOST = _PluginHost()
_PLUGIN_CTX = contextvars.ContextVar("wxbot_plugin_ctx", default=None)
_registry_lock = threading.Lock()

# 私有配置 enabled 行：只匹配顶层（行首无缩进）的 enabled 行，保留行尾注释。
# 分两组：group(1)=值之后到行尾的注释/空白，替换时只换值、原样拼回注释。
# 后台「启用/禁用」按钮会改写这一行，使私有配置与后台开关保持同一真相。
_PLUGIN_ENABLED_RE = re.compile(r"(?m)^enabled\s*:\s*\S+([ \t]*(?:#.*)?)(?=\r?$)")


def _sync_plugin_config_enabled(name, enabled):
    """把后台启停同步写入 data/plugins/<name>.yaml 的顶层 enabled 行。

    - 文件不存在：跳过（该插件没有私有配置，纯靠后台 state 控制）。
    - 有 enabled 行：只替换该行值，保留缩进无关的顶层格式与其余全部内容。
    - 无 enabled 行：在文件开头补一行，让私有配置成为权威开关。
    返回 (是否已写, 说明)。写失败只告警、不影响 store 状态。
    """
    cfg_dir = str(PCFG.get("config_dir") or "data/plugins")
    if not os.path.isabs(cfg_dir):
        cfg_dir = os.path.normpath(os.path.join(BASE_DIR, cfg_dir))
    path = os.path.join(cfg_dir, str(name) + ".yaml")
    if not NAME_RE.match(str(name)) or not os.path.isfile(path):
        return False, "无私有配置文件"
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        newline = "\r\n" if "\r\n" in text else "\n"
        val = "true" if enabled else "false"
        if _PLUGIN_ENABLED_RE.search(text):
            # group(1) 是值之后的行尾注释（可能为空），原样保留
            new = _PLUGIN_ENABLED_RE.sub(
                lambda m: "enabled: " + val + m.group(1), text, count=1)
        else:
            new = "enabled: " + val + newline + text
        if new == text:
            return True, "已是目标值"
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(new)
        os.replace(tmp, path)
        log.info("后台启停已同步私有配置 | %s | enabled=%s", name, val)
        return True, "已同步"
    except Exception as e:
        log.warning("同步私有配置 enabled 失败（后台状态仍生效）| %s | %s", name, e)
        return False, str(e)


def reload_plugins():
    """重建注册表并整体替换指针。消息开始时取一次快照，重载不会让在途消息串台。"""
    global PLUGIN_REGISTRY
    with _registry_lock:
        old = PLUGIN_REGISTRY
        try:
            cfg = _load_config().get("plugins") or {}
        except Exception as e:
            log.warning("重载插件时读配置失败（沿用启动配置）: %s", e)
            cfg = PCFG
        new, err = pluginhost.rebuild(cfg, base_dir=BASE_DIR, host=PLUGIN_HOST,
                                      log=log, old=old)
        if err:
            # 构造整体失败（目录被删 / 配置写坏 / import 阶段炸掉）时保留旧表：
            # 插件系统不能因为一次失败重载就从「有插件」瞬间变成「空注册表」。
            log.error("插件重载失败，已保留上一份可用注册表 | %s", err)
        else:
            PLUGIN_REGISTRY = new
            try:
                old.shutdown()
            except Exception as e:
                log.warning("旧插件清理异常（已忽略）: %s", e)
            # 重载后立刻对齐调度器状态：否则管理端刷新列表会短暂看到 0 个任务，
            # 要等下一个 tick 才重建。tick_once 只重建状态，新任务 next_run 在未来，
            # 不会因此被立即触发。
            try:
                sch = globals().get("PLUGIN_SCHEDULER")
                if sch is not None:
                    sch.tick_once()
            except Exception as e:
                log.warning("重载后对齐定时任务状态失败（已忽略）: %s", e)
        d = dict(PLUGIN_REGISTRY.describe())
        if err:
            d["reload_error"] = err
        else:
            log.info("插件注册表已重载 | 插件=%d | 处理器=%d | 错误=%d",
                     d["count"], d["handlers"], len(d["errors"]))
        return d


def _fire_reply_sent(text, via):
    """_send_text 单一出口调用：转人工 / 媒体提示 / 语音兜底 / AI 回复 / 插件回复
    全部经过这里，插件的 on_reply_sent 才不会漏分支。"""
    ctx = _PLUGIN_CTX.get()
    if ctx is None:
        return
    ctx.last_reply_text, ctx.via = text, via
    ctx._no_reply = True          # 防止插件在 on_reply_sent 里递归调 ctx.reply
    try:
        ctx.fire("on_reply_sent", kind=ctx.kind)
    except Exception as e:
        log.warning("插件 on_reply_sent 派发异常（已忽略）: %s", e)
    finally:
        ctx._no_reply = False


def _fire_error(ctx, err, where=""):
    if ctx is None:
        return
    ctx.error = ("%s: %s" % (where, err)) if where else str(err)
    try:
        ctx.fire("on_error", kind=policy.message_kind(ctx.m))
    except Exception as e:
        log.warning("插件 on_error 派发异常（已忽略）: %s", e)


PLUGIN_REGISTRY = pluginhost.load_registry(
    PCFG, base_dir=BASE_DIR, host=PLUGIN_HOST, log=log)

# P4 定时任务实例表：与插件私有配置同目录（Docker 内 /app/data/plugins）。
# 管理端写文件、主服务按 mtime 热读；两端路径都由 plugins.config_dir 推导，
# 保存后最多一个 tick 生效，不需要重载注册表。
_TASK_CFG_DIR = str(PCFG.get("config_dir") or "data/plugins")
if not os.path.isabs(_TASK_CFG_DIR):
    _TASK_CFG_DIR = os.path.normpath(os.path.join(BASE_DIR, _TASK_CFG_DIR))
PLUGIN_TASK_STORE = pluginhost.TaskStore(
    os.path.join(_TASK_CFG_DIR, "_tasks.yaml"), log=log)


def _ext_enabled():
    """/ext 默认关闭：它挂在 29999 端口，开启等于给外部多一个入口。"""
    return bool(PCFG.get("enabled", False)) and bool(PHCFG.get("enabled", False))


def _ext_require_token():
    return bool(PHCFG.get("require_internal_token", True))


def _ext_max_body():
    try:
        return min(1024 * 1024, max(1024, int(PHCFG.get("max_body") or 65536)))
    except (TypeError, ValueError):
        return 65536


def _plugin_health():
    """/healthz 是高频接口，只返回概览，不构造完整插件清单。"""
    try:
        d = PLUGIN_REGISTRY.describe()
        return {"enabled": d["enabled"], "count": d["count"],
                "handlers": d["handlers"], "errors": len(d["errors"]),
                "tasks": len(d.get("schedules") or []),
                "routes": len(d.get("routes") or [])}
    except Exception:
        return {"enabled": False, "count": 0, "handlers": 0, "errors": 0,
                "tasks": 0, "routes": 0}


def _http_routes_view():
    return {"enabled": _ext_enabled(),
            "require_internal_token": _ext_require_token(),
            "prefix": "/ext", "max_body": _ext_max_body(),
            "routes": PLUGIN_REGISTRY.describe().get("routes") or []}


def _plugin_payload(reload_error=""):
    """/internal/plugins* 的统一返回：静态清单 + 调度器运行时状态 + 路由清单。

    后台只经 _call_main 拿这份数据，管理端进程不 import 任何插件代码。
    """
    d = PLUGIN_REGISTRY.describe()
    if reload_error:
        d["reload_error"] = reload_error
    return {"ok": True, "plugins": d,
            "scheduler": PLUGIN_SCHEDULER.snapshot(),
            "http_routes": _http_routes_view(),
            "task_manager": PLUGIN_TASK_STORE.describe(PLUGIN_REGISTRY.schedules())}

# ---------------------------------------------------------------- 会话记忆

CONV_FILE = os.environ.get("WXBOT_CONV") or os.path.join(BASE_DIR, "data", ".conversations.json")
MEM_LIMIT = int(BCFG.get("memory_limit") or 10)
MEM_PERSIST = BCFG.get("memory_persist", True)
_conv_lock = threading.Lock()      # 保护内存字典
_save_lock = threading.Lock()      # 串行化落盘（同一时刻只允许一个线程写文件）
_conversations = {}
# 落盘合并窗口（秒）：同一条消息里的两三次 _remember() 合并成一次全量写。
# 设 0 等价于每次立即写（旧行为），自动化测试或极端保守部署可用环境变量关掉。
SAVE_DEBOUNCE = float(os.environ.get("WXBOT_CONV_DEBOUNCE") or 1.0)
_save_pending = threading.Event()  # 有未落盘的改动
_saver_lock = threading.Lock()
_saver_started = False


def _load_conversations():
    global _conversations
    if not MEM_PERSIST:
        return
    try:
        with open(CONV_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            _conversations = {k: v for k, v in d.items() if isinstance(v, list)}
            log.info("对话记忆已载入 | %d 个会话", len(_conversations))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("对话记忆载入失败（按空记忆继续）: %s", e)


def _save_conversations():
    """整份记忆原子落盘。

    快照在 `_conv_lock` 内取、写文件在 `_save_lock` 内串行，且每次用唯一
    临时文件名：多线程同时 `_remember()` 时，若共用 `CONV_FILE + ".tmp"`，
    会互相写坏内容或在前一个线程 `os.replace` 后找不到 tmp，导致记忆文件
    损坏（下次启动整份记忆丢失）。
    """
    if not MEM_PERSIST:
        return
    try:
        with _conv_lock:
            snapshot = {k: list(v) for k, v in _conversations.items()}
        with _save_lock:
            folder = os.path.dirname(CONV_FILE) or "."
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".conversations.", suffix=".tmp",
                                       dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f, ensure_ascii=False)
                os.replace(tmp, CONV_FILE)
            except BaseException:
                try:
                    os.remove(tmp)         # 失败不留垃圾临时文件
                except OSError:
                    pass
                raise
    except Exception as e:
        log.warning("对话记忆落盘失败: %s", e)


def _saver_loop():
    """后台合并落盘线程：攒够 SAVE_DEBOUNCE 秒再写一次，写完继续等。

    纯 daemon 线程：进程退出时不必等它，退出前的兜底由 atexit 的
    flush_conversations() 负责。
    """
    while True:
        _save_pending.wait()           # 没有改动就睡着，不占 CPU
        if SAVE_DEBOUNCE > 0:
            time.sleep(SAVE_DEBOUNCE)  # 合并窗口内的多次改动
        _save_pending.clear()
        _save_conversations()


def _request_save():
    """标记「记忆有改动」；由后台线程在合并窗口后统一落盘。"""
    if not MEM_PERSIST:
        return
    if SAVE_DEBOUNCE <= 0:
        _save_conversations()
        return
    global _saver_started
    with _saver_lock:
        if not _saver_started:
            _saver_started = True
            threading.Thread(target=_saver_loop, daemon=True,
                             name="conv-saver").start()
    _save_pending.set()


def flush_conversations():
    """立刻把内存记忆写盘（进程退出 / 测试断言用），并清掉待写标记。"""
    _save_pending.clear()
    _save_conversations()


def _history(uid):
    with _conv_lock:
        return list(_conversations.get(uid, []))


def _remember(uid, question, answer):
    if MEM_LIMIT <= 0:
        return
    with _conv_lock:
        h = _conversations.setdefault(uid, [])
        h.append({"role": "user", "content": question})
        h.append({"role": "assistant", "content": answer})
        while len(h) > MEM_LIMIT:
            h.pop(0)
        if len(_conversations) > 1000:      # 简单 LRU 上限，防止单文件无限膨胀
            _conversations.pop(next(iter(_conversations)))
    _request_save()


_load_conversations()
atexit.register(flush_conversations)   # 退出前补一次，最多丢掉合并窗口内的改动

# ---------------------------------------------------------------- 回复发送


def _mark_followup(m):
    """回复已成功发出 → 刷新群聊「对话延续窗口」。

    计时从这一刻开始：LLM 慢也不吃掉窗口。记录失败绝不影响刚发出去的回复。
    """
    try:
        followup.mark_reply(m)
    except Exception as e:
        log.debug("对话延续窗口记录失败（不影响回复）: %s", e)


def _with_followup_hint(m, match, text):
    """给 AI 实质回复末尾**空一行**再追加续聊提示；关闭/私聊/空回复原样返回。

    提示文案由 followup.config() 决定（后台可即时改），分钟数随当前窗口自动变化。
    生成失败绝不影响回复本身。
    """
    if match["chat_type"] != "group":
        return text
    try:
        hint = followup.hint_text()
    except Exception as e:
        log.debug("续聊提示生成失败（不影响回复）: %s", e)
        return text
    if not hint or not str(text or "").strip():
        return text
    # 正文与提示之间留一个空行（正文末尾的空白先清掉，避免堆出三行）
    return str(text).rstrip() + "\n\n" + hint


def _send_text(m, match, text, extra_at=None, at_names=None, at_only=False,
               with_hint=False, mark_followup=True):
    """发文本回复。

    extra_at / at_only 供转人工使用：把人工接待的 wxid 放进 @ 列表，
    at_names 是可选的显示名覆盖（{"wxid": "昵称"}）；不传时插件
    会自动取群名片，普通回复因此不传，只有转人工要指定名字时才传。
    with_hint=True 只给「AI 实质回复」用：末尾空一行追加续聊提示；
    媒体提示 / 语音识别失败兜底 / 转人工话术一律不带。
    提示只影响实际发出的文本，**不写进对话记录**（流水始终记原始回答）。

    mark_followup 默认 True（AI 实质回复刷新续聊窗口）。插件通过
    ctx.reply() 发消息时传 False：插件是旁路 responder，不是「AI 正在接话」，
    不能因为回了一句摘要就让后续消息免 @ 直接触发主链路。
    """
    sent = _with_followup_hint(m, match, text) if with_hint else text
    chat, sender = m["chat"], m.get("sender") or ""
    group = match["chat_type"] == "group"
    if extra_at:
        # 转人工：先 @ 提问的用户，再 @ 人工客服（插件按 at 列表顺序展示 @）
        at = []
        if (not at_only and group and sender and RCFG.get("at_sender", True)):
            at.append(sender)
        at.extend([x for x in extra_at if x])
    else:
        at = [sender] if (group and sender and RCFG.get("at_sender", True)) else []
    at = list(dict.fromkeys(at))          # 去重且保持顺序
    # 早期修订：/api/send/text 已支持 atAll=1 与 at:["@all"] 自动转换，但
    # /api/send/quote 仍会 400「@all is not supported」。@所有人 每次都会
    # 通知全群（仅群主/管理员可用），本项目不主动开启，配置里误填一律剔除，
    # 避免一条普通回复把整个群吵醒。
    _NO_AT_ALL = ("@all", "all", "notify@all", "__wxid_all__")
    at = [x for x in at if str(x).strip().lower() not in _NO_AT_ALL]
    if not group:
        # 私聊没有 @ 的概念：普通回复不 @ 提问者，转人工也不 @ 人工接待。
        # 这里做硬保证，避免以后 extra_at 被误传导致私聊带 @ 列表。
        at = []
        at_names = None
    quote = bool(RCFG.get("quote_reply", False)) and int(m.get("local_id") or 0) > 0
    # 单一出口：转人工 / 媒体提示 / 语音兜底 / AI 实质回复 / 插件回复都会走到
    # 最后的 _fire_reply_sent，插件的 on_reply_sent 才不会漏分支（方案 3.3）。
    via = "text"
    if quote:
        try:
            # /api/send/quote 与 /api/send/text 共用 @ 规则。早期版本起 atNames
            # 是可选的「强制覆盖」：不传时插件自动取群名片，比服务端用
            # senderName 猜出来的显示名更准；只有转人工这种要指定名字时才传。
            WX.send_quote(chat, sent, m["local_id"], at=at or None,
                          at_names=at_names)
            via = "quote"
        except WXClientError as e:
            log.warning("引用回复失败，改用普通文本: %s", e)
    if via != "quote":
        WX.send_text(chat, sent, at=at or None, at_names=at_names)
    if mark_followup:
        _mark_followup(m)
    _fire_reply_sent(sent, via)
    return via


# ---------------------------------------------------------------- 转人工


def _human_agent(match):
    """人工接待：群聊策略里的配置优先，没填就用 config.yaml 的默认值。"""
    wxid = (match.get("human_wxid") or "").strip() or \
        str(TCFG.get("default_wxid") or "").strip()
    name = (match.get("human_name") or "").strip() or \
        str(TCFG.get("default_name") or "").strip() or "人工客服"
    return wxid, name


DEFAULT_TRANSFER_REPLY = "已为您转接人工，请稍候～"
DEFAULT_PRIVATE_TRANSFER_REPLY = "请联系人工客服 {name} 咨询或电话咨询。"


def _transfer_reply(name, private=False):
    """群聊用 `transfer.reply`；私聊用 `transfer.private_reply`（纯文字、不 @ 人）。

    两个模板都支持 `{name}` 占位（人工接待昵称）。配置缺项时各用各的默认值，
    不会把群聊那句带 @ 的话术带到私聊里。
    """
    if private:
        tpl = str(TCFG.get("private_reply") or DEFAULT_PRIVATE_TRANSFER_REPLY)
    else:
        tpl = str(TCFG.get("reply") or DEFAULT_TRANSFER_REPLY)
    return tpl.replace("{name}", name)


def _want_human_by_keywords(question):
    return any(k and str(k) in question for k in (TCFG.get("keywords") or []))


def _want_human_by_ai(answer):
    return any(k and str(k) in answer for k in (TCFG.get("ai_markers") or []))


def _strip_transfer_markers(answer):
    """规则关掉转人工时，把 AI 仍可能写出的 [转人工] 等标记清掉再直接回复。"""
    for k in (TCFG.get("ai_markers") or []):
        if k:
            answer = answer.replace(str(k), "")
    return " ".join(answer.split()).strip()


def _do_transfer(m, match, question, reason, answer=None):
    """发送转人工回复：群聊 @ 人工接待 + reply；私聊不 @ 任何人 + private_reply。"""
    wxid, name = _human_agent(match)
    group = match["chat_type"] == "group"
    note = _transfer_reply(name, private=not group)
    reply = (answer.strip() + "\n\n" + note) if (answer and answer.strip()) else note
    extra_at = [wxid] if (wxid and group) else None
    at_names = {wxid: name} if (wxid and group) else None
    if not wxid and group:
        log.warning("转人工但未配置人工接待 wxid（群策略或 transfer.default_wxid），不会 @ 人")
    try:
        via = _send_text(m, match, reply, extra_at=extra_at, at_names=at_names,
                         at_only=not bool(TCFG.get("keep_at_sender", False)))
        _event(m, match, kind="transfer", question=question,
               answer=reply, reason=reason, ok=True)
        log.info("转人工 | 原因=%s | 接待=%s(%s) | 问=%s | %s",
                 reason, name, wxid or "-", _brief(question), via)
    except Exception as e:
        log.warning("转人工回复失败: %s", e)
        _event(m, match, kind="error", question=question, answer=reply,
               ok=False, reason="转人工发送失败: %s" % e)


def _send_images(m, paths, lead_gap=False):
    if not ICFG.get("enabled", True) or not paths:
        return 0
    n = 0
    gap = _reply_gap()
    limit = int(ICFG.get("max_images") or 1)
    for p in paths[:max(0, limit)]:
        try:
            if not os.path.isfile(p):
                continue
            # 首图前（刚发过文字）与第二张起都等一个间隔，间隔由 reply.interval_seconds 控制
            if gap and (n or lead_gap):
                time.sleep(gap)
            WX.send_image(m["chat"], p, int(ICFG.get("max_bytes") or 2097152))
            n += 1
        except Exception as e:
            log.warning("知识库配图发送失败（不影响文字回复）: %s", e)
    return n


def _event(m, match, **kw):
    d = {
        "chat": m.get("chat"), "chat_type": m.get("chat_type"),
        "chat_name": m.get("chat_name"), "sender": m.get("sender"),
        "sender_name": m.get("sender_name"), "msg_type": policy.message_kind(m),
        "source": m.get("chat_type"), "policy_id": match.get("policy_id"),
        "policy_name": match.get("policy_name"), "trigger": match.get("trigger"),
        # 默认「确实没发图」；正常回复会用实际的发送张数覆盖。
        # 老数据没有这个维度（NULL），前端会显示「-」而不是误导成 0 张。
        "n_images": 0,
    }
    d.update(kw)
    store.log_event(**d)


def _skip(m, match, reason):
    """跳过原因只进 DEBUG 日志，**不写「对话记录」流水**。

    公众号/文件助手等系统会话已在 webhook 入口静默丢弃，不会走到这里。
    原始消息仍由 chatlog.py 全量归档到聊天记录库；要查「消息有没有收到」去那里，
    要看具体忽略原因把 LOG_LEVEL 调到 DEBUG 看运行日志。
    """
    log.debug("忽略 | 类型=%s | 原因=%s", policy.message_kind(m), reason)
    ctx = _PLUGIN_CTX.get()
    if ctx is not None:
        ctx.match = match or ctx.match
        ctx.skip_reason = reason
        try:
            ctx.fire("on_skip", kind=policy.message_kind(m))
        except Exception as e:
            log.warning("插件 on_skip 派发异常（已忽略）: %s", e)


# ---------------------------------------------------------------- 消息处理


def _hook(ctx, name, *, kind=None):
    """派发一个插件 hook；返回 True 表示插件要求 STOP_ALL（主链路到此为止）。"""
    try:
        return ctx.fire(name, kind=kind) == pluginhost.STOP_ALL
    except Exception as e:
        log.warning("插件 hook %s 派发异常（已忽略）: %s", name, e)
        return False


def _plugin_takeover_event(m, ctx):
    """插件 STOP_ALL 接管后仍要补写对话记忆与回复流水，否则统计会漏。

    插件只是静默拦截（没调用 ctx.reply）时这里什么都不写，符合预期。
    """
    if not ctx.replies:
        return
    ans = ctx.replies[-1]
    q = ctx.question or str(m.get("text") or "")
    # 用 takeover_by：dispatch 返回后 plugin_name 已被 finally 清空，
    # 只有 STOP_ALL 时固化的字段才拿得到真正的接管插件。
    plugin = (getattr(ctx, "takeover_by", "") or
              getattr(ctx, "plugin_name", "") or "-")
    if q:
        ctx.remember(q, ans)
    # 记下插件名：后台「对话记录」能看到是哪条插件接管的，和 AI 回复区分开。
    _event(m, ctx.match, kind="reply", question=q, answer=ans,
           ok=True, n_images=0, plugin=plugin)
    log.info("插件接管消息 | 插件=%s | 回复数=%d | 问=%s",
             plugin, len(ctx.replies), _brief(q))


def process_message(payload):
    """一条消息一个上下文：下游所有日志自动带会话 / 发送者，处理完必须重置。

    一定要 reset：线程池里的线程会被复用，不重置的话定时任务（清理聊天记录、
    索引重载等）的日志会错误地挂着上一条消息的会话前缀。
    """
    m = policy.normalize(payload)
    token = _MSG_CTX.set(_msg_tag(m))
    ptoken = _PLUGIN_CTX.set(None)
    try:
        # 每条消息取一次注册表快照：后台重载插件时，在途消息不会串台。
        reg = PLUGIN_REGISTRY
        ctx = pluginhost.PluginContext(
            m=m, kind=policy.message_kind(m), host=PLUGIN_HOST,
            log=log, registry=reg)
        _PLUGIN_CTX.set(ctx)
        _handle_message(m, ctx)
    except Exception as e:
        log.exception("处理消息时未捕获的异常（已忽略，不影响后续消息）")
        _fire_error(_PLUGIN_CTX.get(), e, "未捕获异常")
    finally:
        _PLUGIN_CTX.reset(ptoken)
        _MSG_CTX.reset(token)


# 消息主链路按「过滤 → 媒体 → 插件 H1 → 语音 → 策略 → 类型分支 → 提问 → 生成 → 发送」
# 分阶段实现，_handle_message 只做编排。阶段返回 True 表示「这条消息已在阶段内
# 消费完」（跳过 / 已回复 / 已转人工），编排层直接 return 不再往下走。


def _stage_filter(m, match):
    """基础过滤：无会话、系统会话、自己发出的、过期消息。返回 True 表示丢弃。"""
    if not m["chat"]:
        return True
    # 与 /webhook 入口同一口径：系统会话静默丢弃，不记流水。
    # 这一层是防御（比如历史消息补推、其它调用方直接进 process_message）。
    if policy.is_noise_session(m):
        return True
    if policy.is_outgoing(m, self_wxid()):
        return True
    if policy.too_old(m, WHCFG.get("max_msg_age") or 600):
        _skip(m, match, "消息过期（超过 max_msg_age）")
        return True
    return False


def _stage_media(m):
    """入站媒体落盘（只落地，不决定是否回复）。

    放在所有回复分支之前，保证群聊 / 私聊里每张图、每个文件都被记录，
    与是否触发 AI 回复无关；下载失败只记日志，不影响后续处理。
    """
    if MEDIA_ENABLED and media_store.is_media(m):
        try:
            media_store.ingest(m, WX, log=log.info)
        except Exception as e:
            log.warning("媒体存储异常（已忽略）: %s", e)
    # 引用消息里的图片（文档 7.2.1）：只认推送自带的 quoteResolved，
    # quote.type=3 且 found 时，用其 localID + mediaApi（普通档位，不带 hd=1）下载。
    # 发送者/时间取引用消息本身（非原图作者），via='quote'。失败只记日志。
    if MEDIA_ENABLED and QUOTE_IMAGE_ENABLED and media_store.is_media_ready_for_quote(m):
        try:
            media_store.ingest_quoted_image(m, WX, log=log.info)
        except Exception as e:
            log.warning("引用图片存储异常（已忽略）: %s", e)


def _stage_asr(m):
    """下载 SILK 语音并识别，返回 (识别文本, 是否识别失败)。"""
    asr_text = ""
    try:
        raw, ext = WX.media(m.get("media_api"), m["chat"], m.get("local_id"))
        asr_text = voice.voice_bytes_to_text(raw, VCFG, log=log.info, src_ext=ext)
    except Exception as e:
        log.warning("语音媒体下载失败: %s", e)
    return asr_text, not asr_text


def _stage_kind_branch(m, match, kind, asr_failed):
    """按消息类型直接答复的分支：图片/视频、其它类型、语音识别失败。

    返回 True 表示已经给出了答复（或明确跳过），无需再检索知识库。
    """
    # 图片 / 视频：直接回「暂不支持」，不做知识检索
    if kind in ("image", "video"):
        reply = str(RCFG.get("unsupported_reply") or
                    "暂不支持图片/视频消息，请发送文字或语音～")
        try:
            _send_text(m, match, reply)
            log.debug("媒体类型暂不支持 | 已回提示 | %s | %s",
                      m.get("chat_name") or m.get("chat"), kind)
        except Exception as e:
            log.warning("回复「暂不支持」失败: %s", e)
        return True

    if kind == "other":
        _skip(m, match, "暂不处理的消息类型 type=%s" % m.get("type"))
        return True

    if asr_failed:
        reply = str(VCFG.get("failed_reply") or
                    "抱歉，这条语音没听清，麻烦您再发一次或者直接打字～")
        try:
            _send_text(m, match, reply)
            _event(m, match, kind="error", answer=reply, reason="语音识别失败")
        except Exception as e:
            log.warning("语音兜底回复发送失败: %s", e)
        return True
    return False


def _stage_question(m, match, kind, asr_text):
    """组装提问文本；返回空串表示没有可回答的内容。"""
    # 引用本机消息时，被引用内容往往是机器人自己发出的引用回复（一大段转义 XML），
    # 拼进提问只会污染检索；这段上下文已在对话记忆里，直接用用户当前文本即可。
    ref = {} if match["trigger"] == "quote:self" else policy.quote_reference(m)
    question = textutil.build_question(match["question"] or asr_text, ref).strip()
    if not question:
        return ""
    maxq = int((CFG.get("api") or {}).get("max_q_chars") or 500) * 4
    question = question[:maxq]
    if match["trigger"] == "followup":
        log.info("对话延续生效 | 群=%s | 群友=%s | 免 @ / 唤醒词",
                 m.get("chat_name") or m["chat"],
                 m.get("sender_name") or m.get("sender") or "-")
    log.info("收到消息 | 类型=%s | 触发=%s | 策略=%s | 问=%s",
             kind, match["trigger"] or "-", match["policy_name"] or "-",
             _brief(question))
    return question


def _answer_options(match):
    """规则级能力开关，返回 (是否允许转人工, 生效的知识库来源, 追加提示词)。

    不转人工 = 忽略关键词 / AI 标记直接回复；
    不需要知识库 = sources 传空策略（retrieve 立即返回，不跑 BM25 / 向量），
    并追加一条提示词覆盖默认的「只依据知识库回答」，让模型正常作答。
    """
    want_transfer = bool(match.get("need_transfer", True))
    need_kb = bool(match.get("need_kb", True))
    effective_sources = match["sources"] if need_kb else []
    extras = []
    if want_transfer and str(TCFG.get("ai_instruction") or "").strip():
        extras.append(str(TCFG["ai_instruction"]).strip())
    if not need_kb:
        extras.append("本次不检索知识库：请直接依据通用知识简要回答，"
                      "不受「只依据知识库回答」限制，不要说知识库里没有、也不要建议转人工。")
    prompt_extra = "\n\n".join(extras) or None
    if not (want_transfer and need_kb):
        log.info("规则能力开关 | 知识库=%s 转人工=%s | 策略=%s",
                 "on" if need_kb else "off", "on" if want_transfer else "off",
                 match.get("policy_name") or "-")
    return want_transfer, effective_sources, prompt_extra


def _generate(m, ctx, uid, question, effective_sources, prompt_extra, match):
    """取答案并清洗，返回 (答案, 配图路径, 耗时ms)。

    插件已给答案时跳过 RAG+LLM；否则在 brain_lock 内跑检索与生成。
    去 markdown / 截断在这里统一做，调用方拿到的就是可以直接发出去的文本。
    """
    if ctx.has_answer:
        # 插件直接给答案：不跑检索与 LLM，但清洗 / 记忆 / 发送 / 流水一个都不能少
        ans = ctx.take_answer()
        images, cost_ms = [], 0
        log.info("插件直接作答，跳过 RAG+LLM | 问=%s", _brief(question))
    else:
        t0 = time.time()
        # 用哪个大模型：**本条群/私聊配置**里选的优先（规则级），没选才走
        # 场景默认（群聊/私聊整体）→ 全局默认 → config.yaml。
        # scene 仍然传，作为规则没指定时的兜底。
        scene = m.get("chat_type") if m.get("chat_type") in ("group", "private") else None
        rule_llm = match.get("llm_id") or None
        with brain_lock:
            try:
                ans = BRAIN.answer(question, history=_history(uid),
                                   sources=effective_sources,
                                   system_prompt=match.get("system_prompt"),
                                   system_prompt_extra=prompt_extra,
                                   model=rule_llm, scene=scene)
            except Exception as e:
                log.exception("brain.answer 异常: %s", e)
                _fire_error(ctx, e, "brain.answer")
                ans = "AI 服务暂时不可用，请稍后再试或转人工。"
                BRAIN.last_images = []
            images = list(getattr(BRAIN, "last_images", None) or [])
            attempts = [dict(a) for a in (getattr(BRAIN, "last_attempts", None) or [])]
            # 必须在锁内快照：锁放开后另一个请求可能已经切到别的纳管模型
            llm_used = dict(getattr(BRAIN, "llm", None) or {})
        cost_ms = int((time.time() - t0) * 1000)
        if ans.startswith("AI 服务暂时不可用"):
            images = []
        # 每条回复都记一条「哪个配置 + 哪个模型 + 为什么是它」：
        # 来源 override=该条群/私聊配置里选的、scene=场景默认、default=全局默认。
        src_label = {"override": "本条配置", "scene": "场景默认",
                     "default": "全局默认"}.get(llm_used.get("source") or "", "config.yaml")
        log.info("AI 应答 | 场景=%s | 配置=%s | 模型=%s | 纳管id=%s | 来源=%s | 耗时=%dms | 问=%s",
                 {"group": "群聊", "private": "私聊"}.get(scene or "", "不限"),
                 match.get("policy_name") or "-",
                 llm_used.get("model") or "-", llm_used.get("id") or "-",
                 "%s(%s)" % (llm_used.get("source") or "config.yaml", src_label),
                 cost_ms, _brief(question))
        store.log_llm_usage(attempts, source="wechat", llm_id=llm_used.get("id"),
                            scene=scene)

    if RCFG.get("plain_text", True):
        ans = textutil.strip_markdown(ans)
    ans = textutil.truncate(ans, RCFG.get("max_answer_chars") or 1000)
    return ans, images, cost_ms


def _send_answer(m, match, ctx, kind, question, ans, images, cost_ms,
                 effective_sources):
    """发送回复 + 配图，并写对话流水；发送失败降级为 error 流水。"""
    try:
        via = _send_text(m, match, ans, with_hint=True)
        n_img = _send_images(m, images, lead_gap=True)
        _event(m, match, kind="reply", question=question,
               answer=ans,
               ms=cost_ms, ok=not ans.startswith("AI 服务暂时不可用"),
               n_images=n_img)
        log.info("已回复 | 类型=%s | 策略=%s | 触发=%s | 文档=%d | %dms | "
                 "via=%s 图=%d | 问=%s",
                 kind, match["policy_name"] or "-", match["trigger"] or "-",
                 len(effective_sources or []), cost_ms, via, n_img, _brief(question))
    except Exception as e:
        log.warning("回复发送失败: %s", e)
        _fire_error(ctx, e, "回复发送")
        _event(m, match, kind="error", question=question, answer=ans,
               ms=cost_ms, ok=False, reason="发送失败: %s" % e)


def _handle_message(m, ctx=None):
    """消息主链路的**编排层**：只表达阶段顺序与早退，细节都在 _stage_* 里。

    阶段顺序：
      过滤 → 媒体落盘 → 插件 H1 → 语音 ASR → 策略（H2/H3）→ 类型分支 →
      组装提问 → 插件 H4 → 生成（H5）→ 记忆 → 发送
    """
    kind = policy.message_kind(m)
    match = {"policy_id": None, "policy_name": "", "trigger": "",
             "chat_type": m["chat_type"], "sources": None}
    # 正常链路里 ctx 由 process_message 创建并放进 contextvars；这里只给
    # 「其它调用方直接传消息进来」留一个安全兜底（此时 on_reply_sent 不派发）。
    if ctx is None:
        ctx = pluginhost.PluginContext(
            m=m, kind=kind, host=PLUGIN_HOST, log=log, registry=PLUGIN_REGISTRY)
    ctx.match = match

    if _stage_filter(m, match):
        return
    _stage_media(m)

    # H1：基础过滤之后、ASR 之前。命令解析 / 自定义黑名单 / 全量旁听。
    if _hook(ctx, "on_message", kind=kind):
        _plugin_takeover_event(m, ctx)
        return

    # 语音：先下载 SILK → ASR，识别文本也参与唤醒词匹配
    asr_text, asr_failed = "", False
    if kind == "voice":
        allowed, why = policy.basic_allowed(m, self_wxid())
        if not allowed:
            _skip(m, match, why)
            return
        if not VCFG.get("enabled", True):
            _skip(m, match, "语音处理已关闭")
            return
        asr_text, asr_failed = _stage_asr(m)

    # 图片/视频默认也要满足触发条件（避免群里每张图都回一句）；媒体消息本身
    # 不带正文 / @，true 时实际主要靠「对话延续窗口」放行。
    # reply.media_need_trigger=false 时改为无条件回复「暂不支持」。
    ignore_trigger = (kind in ("image", "video") and
                      not RCFG.get("media_need_trigger", True))
    # H2：策略判定之前。插件可以改 m["text"]（非语音场景）影响唤醒词匹配。
    if _hook(ctx, "on_pre_resolve", kind=kind):
        _plugin_takeover_event(m, ctx)
        return
    result = policy.resolve(m, self_wxid(),
                            text_override=asr_text if kind == "voice" else None,
                            ignore_trigger=ignore_trigger)
    if not result["handled"]:
        _skip(m, result, result["reason"])
        return
    match = result

    # H3：策略判定之后、类型分支之前。可换知识库策略、改触发条件或完全接管。
    ctx.match = match
    if _hook(ctx, "on_post_resolve", kind=kind):
        _plugin_takeover_event(m, ctx)
        return
    match = ctx.match

    if _stage_kind_branch(m, match, kind, asr_failed):
        return

    question = _stage_question(m, match, kind, asr_text)
    if not question:
        _skip(m, match, "提问为空")
        return

    want_transfer, effective_sources, prompt_extra = _answer_options(match)

    # H4：question 就绪、进入 brain_lock **之前**（所有 hook 都在锁外，防死锁）。
    # 插件可 set_answer 直接作答，跳过 RAG+LLM；返回 STOP_ALL 则完全接管。
    ctx.question = question
    if _hook(ctx, "on_pre_answer", kind=kind):
        _plugin_takeover_event(m, ctx)
        return
    question = ctx.question

    if want_transfer and _want_human_by_keywords(question):
        _do_transfer(m, match, question, "客户要求转人工")
        return

    uid = "%s|%s" % (m["chat"], m.get("sender") or "")
    ans, images, cost_ms = _generate(m, ctx, uid, question,
                                     effective_sources, prompt_extra, match)
    if want_transfer and _want_human_by_ai(ans):
        clean = ans.replace("[转人工]", "").strip()
        _remember(uid, question, clean)
        _do_transfer(m, match, question, "AI 判断需转人工", answer=clean)
        return
    if not want_transfer:
        ans = _strip_transfer_markers(ans)
    # H5：答案清洗完、写记忆**之前**，插件改过的答案会同步进记忆与流水。
    ctx.answer = ans
    if _hook(ctx, "on_post_answer", kind=kind):
        _plugin_takeover_event(m, ctx)
        return
    ans = ctx.answer
    _remember(uid, question, ans)
    _send_answer(m, match, ctx, kind, question, ans, images, cost_ms,
                 effective_sources)


def _async(payload):
    try:
        process_message(payload)
    except Exception:
        log.exception("处理 webhook 消息时未捕获的异常（已忽略，不影响后续消息）")


# ---------------------------------------------------------------- Flask

app = Flask(__name__)
try:
    app.json.ensure_ascii = False       # Flask 3.x；中文响应不转义成 \uXXXX
except Exception:
    pass

# register() 的副作用是注册 /httpapi/* 路由；返回值（是否注册成功）无人读取
kbapi.register(app, BRAIN_PROXY, brain_lock, log)
# gunicorn 以 import 方式加载本模块（不走 __main__），预热必须在模块级启动。
# brain.retrieval.warmup=false 可关闭：无 GPU 的机器加载 rerank 模型要 20~30s，
# 关掉后第一次检索才加载（客户首条消息会等），但启动更快、内存占用更平缓。
if (BCFG.get("retrieval") or {}).get("warmup", True):
    threading.Thread(target=BRAIN.warmup, daemon=True, name="brain-warmup").start()
else:
    log.info("检索模型预热已关闭（brain.retrieval.warmup=false）")


def _wh_auth_ok():
    token = str(WHCFG.get("token") or "").strip()
    if not token:
        return True                       # 插件默认不鉴权；建议 webhook URL 带 ?token=
    got = (request.args.get("token") or
           request.headers.get("X-Webhook-Token") or
           request.headers.get("X-Token") or "")
    h = request.headers.get("Authorization") or ""
    if not got and h.lower().startswith("bearer "):
        got = h[7:].strip()
    return hmac.compare_digest(str(got).strip(), token)


def _internal_auth_ok():
    token = str(WHCFG.get("internal_token") or WHCFG.get("token") or "").strip()
    if not token:
        return True
    got = (request.headers.get("X-Internal-Token") or request.args.get("token") or "")
    return hmac.compare_digest(str(got).strip(), token)


@app.route("/webhook", methods=["POST"])
def webhook():
    if not _wh_auth_ok():
        return jsonify({"ok": False, "error": "webhook token 无效"}), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body 必须是 JSON object"}), 400
    m = policy.normalize(payload)
    # 归档与去重共用同一个 key，一次算好（dedup_key 是纯函数）
    dedup = policy.dedup_key(m)
    # 系统会话（公众号 / 服务号 / 文件助手）既不归档、也不参与后续处理，
    # 只判一次，后面直接复用。
    noise = policy.is_noise_session(m)
    # 聊天记录归档：真实聊天会话的原始消息全量写进独立库（含没触发回复的
    # 消息与手机端自己发出的消息）。本地 SQLite 写入是毫秒级且失败只记日志，
    # 绝不影响 webhook 200 与回复链路。
    if not noise:
        key = "%s|%s" % (dedup, m.get("direction") or "")
        ok_archive, why = chatlog.save_message(m, key)
        if not ok_archive and why not in ("duplicate", "disabled", "noise"):
            log.warning("聊天记录归档失败: %s", why)
    # 这里不主动刷新 self（health 最多 10 秒），保证 webhook 立即 200
    _cached_self = SELF_WXID_CFG or _self_cache["wxid"]
    if policy.is_outgoing(m, _cached_self):
        return jsonify({"ok": True, "ignored": "self"})
    # 公众号 / 服务号 / 文件助手 / 系统通知：不是客户对话，直接略过。
    # 必须在去重与线程池之前拦掉 —— 既不写 processed 去重表，也不写 events 流水，
    # 更不会在「对话记录」里留下 skip 记录（用户明确要求「不记录」）。
    if noise:
        return jsonify({"ok": True, "ignored": "official"})
    if not store.dedup_check_and_mark(dedup,
                                      WHCFG.get("dedup_ttl") or 86400):
        return jsonify({"ok": True, "ignored": "duplicate"})
    # 收到真实客户消息也算一次手机活跃（比 10 秒探活更及时）
    phone_mon.touch(source="webhook")
    # 插件 postJSON 只有 10 秒超时，必须立即返回；AI 生成放到线程池
    POOL.submit(_async, payload)
    return jsonify({"ok": True})


@app.route("/alert", methods=["POST"])
def alert():
    """手机插件告警推送（1.1.1-beta1 独立端点，与消息 webhook 隔离）。

    手机插件把告警地址（POST /api/alert）指向本端点后，掉线 / 授权提醒 /
    地址测试等事件会推到这里。告警与业务消息走不同地址、不同处理链路：
    这里只做鉴权 + 落日志/状态，不进去重、不进 AI、不回微信。
    `background_offline` 事件会立即把手机标记为离线，让监控页的连续在线
    时长及时归零（比探活轮询更快）。
    """
    if not _wh_auth_ok():
        return jsonify({"ok": False, "error": "webhook token 无效"}), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body 必须是 JSON object"}), 400
    name = str(payload.get("alert") or "")
    title = str(payload.get("title") or "")
    body = str(payload.get("body") or "")
    log.warning("手机告警 | 事件=%s | 标题=%s | 内容=%s",
                name or "-", title or "-", _brief(body, 200))
    if name == "background_offline":
        phone_mon.mark_offline()
    return jsonify({"ok": True})


@app.route("/healthz")
def healthz():
    # 只做本地状态汇总 + 带缓存的插件探测：手机离线时也必须在毫秒级返回，
    # 否则管理端「启动/重启」的探活会永远误判成「服务没起来」。
    plugin = plugin_snapshot()
    api_st = kbapi.api_state()
    return jsonify({
        "ok": True, "mode": "personal-wechat-webhook",
        "plugin": plugin,
        "self_wxid": SELF_WXID_CFG or _self_cache["wxid"] or plugin.get("self") or "",
        "api_enabled": api_st["enabled"],
        "api_state": api_st,
        "brain": BRAIN.describe(),
        "policies": len(store.list_policies()),
        "docs": len(store.all_kb_docs()),
        "group_rules": len(store.list_group_rules()),
        "private_rules": len(store.list_private_rules()),
        "reload": dict(RELOAD),
        # 高频探活接口，只放概览；完整插件清单走 /internal/plugins
        "plugins": _plugin_health(),
    })


@app.route("/internal/status")
def internal_status():
    if not _internal_auth_ok():
        return jsonify({"ok": False, "error": "token 无效"}), 401
    return jsonify({"ok": True, "reload": dict(RELOAD),
                    "summary": store.summary(),
                    "docs": store.all_kb_docs(),
                    "brain": BRAIN.describe()})


@app.route("/internal/reload/status")
def internal_reload_status():
    """索引重建进度（供后台轮询）。比 /internal/status 轻：只返回 reload 状态。"""
    if not _internal_auth_ok():
        return jsonify({"ok": False, "error": "token 无效"}), 401
    return jsonify({"ok": True, "reload": dict(RELOAD)})


@app.route("/internal/reload", methods=["POST"])
def internal_reload():
    """后台改完知识库后调用：重新构造检索器（清缓存后重新建 BM25/向量索引）。"""
    if not _internal_auth_ok():
        return jsonify({"ok": False, "error": "token 无效"}), 401
    if RELOAD.get("state") == "running":
        return jsonify({"ok": True, "state": "running", "message": "正在重载",
                        "reload": dict(RELOAD)})
    RELOAD.update({"state": "running", "at": time.time(), "detail": "",
                   "stage": "准备中", "pct": 0, "progress": 0,
                   "started_at": time.time(), "finished_at": 0, "error": ""})

    def _progress(stage, pct, detail=""):
        """索引构建过程的上报。

        brain 报的是**阶段内**百分比（0~100），这里按各阶段的预估耗时权重
        折算成全局百分比 0~99（100 留给「完成」）。权重按实测耗时分配：
        读文档很快、BM25 很快、加载模型与编码向量占绝大部分时间。
        """
        try:
            base, span = {
                "读取文档": (0, 8),
                "BM25 索引": (8, 2),
                "加载向量模型": (10, 20),
                "向量索引": (30, 69),
            }.get(stage, (0, 0))
            RELOAD.update({"stage": stage, "pct": int(pct),
                           "progress": min(99, base + int(pct * span / 100)),
                           "detail": detail or stage, "at": time.time()})
        except Exception:
            pass

    def _do():
        global BRAIN
        try:
            with brain_lock:
                nb = brainmod.HybridRAGBrain(BCFG, log=log.info, progress=_progress)
                BRAIN = nb
            RELOAD.update({"state": "done", "at": time.time(), "progress": 100,
                           "pct": 100, "stage": "完成", "finished_at": time.time(),
                           "detail": BRAIN.describe()})
            log.info("知识库索引已重载 | %s", BRAIN.describe())
        except Exception as e:
            RELOAD.update({"state": "failed", "at": time.time(), "detail": str(e),
                           "stage": "失败", "error": str(e),
                           "finished_at": time.time()})
            log.exception("知识库索引重载失败")
    POOL.submit(_do)
    return jsonify({"ok": True, "state": "running",
                    "message": "已在后台重建索引，稍后可刷新状态"})


@app.route("/internal/api-control", methods=["GET", "POST"])
def internal_api_control():
    """管理端「运行监控」控制知识库接口：GET 状态 / POST start|stop|restart。"""
    if not _internal_auth_ok():
        return jsonify({"ok": False, "error": "token 无效"}), 401
    if request.method == "GET":
        return jsonify({"ok": True, "state": kbapi.api_state()})
    d = request.get_json(silent=True) or {}
    action = str(d.get("action") or "").strip()
    ok, msg, state = kbapi.api_control(action)
    log.info("知识库接口控制 | action=%s ok=%s | %s", action, ok, msg)
    return jsonify({"ok": ok, "message": msg, "state": state}), (200 if ok else 400)


@app.route("/internal/plugins")
def internal_plugins():
    """插件清单 / 加载错误 / 熔断状态 / 定时任务与扩展路由（供管理端读取）。"""
    if not _internal_auth_ok():
        return jsonify({"ok": False, "error": "token 无效"}), 401
    return jsonify(_plugin_payload())


@app.route("/internal/plugins/reload", methods=["POST"])
def internal_plugins_reload():
    """重新扫描插件目录、重读启停状态，并原子替换注册表。"""
    if not _internal_auth_ok():
        return jsonify({"ok": False, "error": "token 无效"}), 401
    d = reload_plugins()
    return jsonify(_plugin_payload(d.get("reload_error") or ""))


@app.route("/internal/plugins/state", methods=["POST"])
def internal_plugins_state():
    """改启用状态 / 优先级后自动重载：{"name":"X","enabled":false} 或 {"name":"X","priority":80}。"""
    if not _internal_auth_ok():
        return jsonify({"ok": False, "error": "token 无效"}), 401
    d = request.get_json(silent=True) or {}
    name = str(d.get("name") or "").strip()
    if not NAME_RE.match(name):
        return jsonify({"ok": False, "error": "插件名不合法"}), 400
    try:
        store.ensure_plugin(name)
        if "enabled" in d:
            on = bool(d["enabled"])
            store.set_plugin_enabled(name, on)
            # 私有配置 enabled 是权威开关：后台启停必须同步写文件，
            # 否则下次重载会被配置文件里的旧值覆盖回去。
            _sync_plugin_config_enabled(name, on)
        if "priority" in d:
            p = d["priority"]
            store.set_plugin_priority(name, None if p in (None, "", "null") else int(p))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    d2 = reload_plugins()
    return jsonify(_plugin_payload(d2.get("reload_error") or ""))


@app.route("/internal/plugins/schedules")
def internal_plugins_schedules():
    """定时任务运行时状态：执行次数、下次执行时间、最近错误。"""
    if not _internal_auth_ok():
        return jsonify({"ok": False, "error": "token 无效"}), 401
    return jsonify({"ok": True, "scheduler": PLUGIN_SCHEDULER.snapshot(),
                    "schedules": PLUGIN_REGISTRY.describe().get("schedules") or []})


# ---------------------------------------------------------------- P3 插件扩展路由

_EXT_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
# 这些头绝不能透给插件，否则 X-Internal-Token 会变成插件可窃取的内部凭证
_EXT_HEADER_DENY = frozenset((
    "x-internal-token", "x-webhook-token", "x-token", "authorization", "cookie",
))


@app.route("/ext/<plugin>", defaults={"sub": ""}, methods=_EXT_METHODS)
@app.route("/ext/<plugin>/<path:sub>", methods=_EXT_METHODS)
def plugin_ext_route(plugin, sub):
    """启动期挂好的固定通配路由；运行时不增删 route（Flask 3 不允许）。

    三重保险：总开关默认关闭（关闭时 404 不暴露存在性）→ 开启后默认强制
    X-Internal-Token → 插件禁用 / 熔断 / 未声明该路径一律 404。插件只拿到
    白名单后的请求信息，拿不到 Flask request 裸对象与任何凭证头。
    """
    if not _ext_enabled():
        return jsonify({"ok": False, "error": "插件扩展路由未启用"}), 404
    if _ext_require_token() and not _internal_auth_ok():
        return jsonify({"ok": False, "error": "token 无效"}), 401
    if not NAME_RE.match(str(plugin or "")):
        return jsonify({"ok": False, "error": "插件名不合法"}), 404
    reg = PLUGIN_REGISTRY
    route = reg.match_route(plugin, request.method, sub)
    if route is None or route.get("disabled"):
        return jsonify({"ok": False, "error": "插件路由不存在或已停用"}), 404
    body = request.get_data(cache=True) or b""
    if len(body) > _ext_max_body():
        return jsonify({"ok": False, "error": "请求体过大"}), 413
    hdrs = {}
    for k, v in request.headers.items():
        lk = k.lower()
        if (lk == "content-type" or lk.startswith("x-")) and lk not in _EXT_HEADER_DENY:
            hdrs[lk] = str(v)[:512]
    try:
        json_data = request.get_json(silent=True)
    except Exception:
        json_data = None
    ctx = pluginhost.HttpContext(
        plugin_name=plugin, route_name=route["route"], method=request.method,
        path="/" + str(sub or "").strip("/"),
        args={k: str(v)[:512] for k, v in request.args.items()},
        headers=hdrs, body=body, json_data=json_data,
        host=PLUGIN_HOST, log=log, registry=reg)
    result, err = reg.invoke(route["handler"], ctx)
    if err:
        # 堆栈只进主服务日志（registry.invoke 已记），响应里不泄露插件内部信息
        return jsonify({"ok": False, "error": "插件处理异常（已隔离）"}), 500
    return _ext_response(result)


def _ext_response(result):
    """把插件返回值转成 JSON 响应；插件不能自己构造 Flask Response。"""
    status = 200
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
        result, status = result[0], max(100, min(599, result[1]))
    if result is None:
        body = {"ok": True}
    elif isinstance(result, (dict, list, int, float, bool)):
        body = result
    else:
        body = {"ok": True, "text": str(result)[:4096]}
    return jsonify(body), status


# ---------------------------------------------------------------- 聊天记录过期清理


def _chatlog_purge_loop():
    """daemon 线程：启动时立即清一次，之后按配置间隔清理过期消息。

    保留月数每次从 config.yaml 读（chatlog.config 有 30 秒缓存），
    所以在「系统配置」页改 retention_months 不需要重启主服务。
    """
    while True:
        hours = 6
        try:
            c = chatlog.config()
            hours = c["purge_interval_hours"]
            if c["enabled"]:
                n = chatlog.purge_expired(c["retention_months"])
                log.info("聊天记录过期清理 | 保留 %d 个月，删除 %d 条 | 库=%s",
                         c["retention_months"], n, c["db_file"])
        except Exception as e:
            log.warning("聊天记录清理失败（不影响消息收发）: %s", e)
        time.sleep(hours * 3600)


threading.Thread(target=_chatlog_purge_loop, name="chatlog-purge",
                 daemon=True).start()


# ---------------------------------------------------------------- P3 定时任务
# 放在模块末尾启动：此时 broadcast / _event / 全部路由都已定义。
# gunicorn 以 import 方式加载本模块（不走 __main__），workers=1 保证只有一份；
# 总开关关闭时连线程都不起，行为与插件系统上线前完全一致。
_pscfg = dict(PSCFG)
_pscfg["enabled"] = bool(PCFG.get("enabled", False)) and bool(PSCFG.get("enabled", True))
PLUGIN_SCHEDULER = pluginhost.PluginScheduler(
    lambda: PLUGIN_REGISTRY, _pscfg, host=PLUGIN_HOST, log=log,
    task_store=PLUGIN_TASK_STORE)
PLUGIN_SCHEDULER.start()


if __name__ == "__main__":
    log.info("监听 %s:%s | webhook 路径 /webhook",
             SCFG.get("host") or "0.0.0.0", SCFG.get("port") or 29999)
    app.run(host=SCFG.get("host") or "0.0.0.0",
            port=int(SCFG.get("port") or 29999), threaded=True, use_reloader=False)
