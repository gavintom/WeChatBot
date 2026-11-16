"""知识库 HTTP 查询接口（对外）：POST /httpapi/query

给「想在自家系统里嵌入知识库问答」的调用方用（门店官网、小程序、第三方 POS…），
直接 POST 一个问题，拿回 AI 的文字答案 + 配套图片。

设计上的三个关键取舍：

1) **跑在 bridge 进程里，不另起进程**。
   向量模型 + 索引已经在 bridge 内存里热着了，另起进程要么重复加载（几十秒~几分钟、
   几百 MB 内存），要么像 admin/kb.ask 那样每次起子进程（首次 197 秒）。
   挂到 bridge 的 Flask 上（gthread 多线程）就是直接复用，响应快、不多占内存。

2) **必须和微信回复合用同一把锁（brain_lock）**。
   brain.answer() 的图片走 `BRAIN.last_images` 这个**实例属性**返回，不是返回值。
   轮询线程 answer() 完到读 last_images 之间如果有 HTTP 请求插进来 answer()，
   客户微信那边就会收到**别人的问题的图片**。所以 app.py 里的 answer() 调用
   和本模块的 answer() 调用都必须持同一把锁，且 last_images 要在锁内快照走。

3) **默认关闭 + 必须带 Token**。
   这是一个能被公网扫到的、未鉴权就会烧 token 的入口，所以：
   `api.enabled` 默认 false；开启时若 token 为空则启动即拒绝启用（不静默放行）。

图片为什么不给裸路径而给签名 URL：`/httpapi/image/<name>` 如果只按文件名取，
等于把整个知识库图片目录公开了（任何人都能枚举下载）。所以 URL 带
HMAC 签名 + 过期时间（默认 10 分钟），只对「本次调用方拿到的图」有效。

配置（config.yaml 顶层 `api:` 段，缺失时全部走安全默认值）：
    api:
      enabled: false          # 总开关；不改这一行则接口一律 404（连路由都不注册）
      api_key: ""             # 必填（enabled=true 时）；请求带 `Authorization: Bearer <api_key>`
                              # 或 `?token=`。改完必须重启 bridge。
      max_q_chars: 500        # 单次提问长度上限
      rate_limit_per_min: 30  # 单 IP 每分钟请求数；0=不限
      rate_window: 60
      image_mode: "url"       # url（签名链接，默认）/ base64（直接内嵌 data URI）/ both
      image_ttl: 600          # 图片签名有效期（秒）
      session_memory: true    # 传 session_id 时是否保留多轮上下文（仅内存，重启丢）
      session_ttl: 1800       # 多轮上下文的闲置过期（秒）
      max_sessions: 500       # 内存里最多保留多少个会话
      cors_origins: ""        # 逗号分隔；填了才发 CORS 头（默认不发 = 浏览器调不了）
      plain: false            # true 则把 Markdown 降级为纯文本（同微信回复的效果）
      record_usage: true      # 是否把 token 用量记进后台「AI 用量」（记，不记事件）
"""
import base64
import hashlib
import hmac
import os
import re
import threading
import time
from collections import OrderedDict
import urllib.parse

from flask import Response, jsonify, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 与 brain.IMAGE_EXTS 保持一致；这里独立列一份是为了**不 import brain**
# （import brain 会连带 numpy/torch 初始化，测试时想不起模型都不行）。
_IMG_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
}
_IMG_EXT_RE = re.compile(r"^\.(jpg|jpeg|png|gif|bmp|webp)$", re.I)
_SAFE_IMG_RE = re.compile(r"^[一-鿿A-Za-z0-9_][一-鿿A-Za-z0-9_.\-]{0,80}$")


# ---------------------------------------------------------------- 配置

def _cfg():
    """读 config.yaml 的 api 段。**每次请求都重读**，这样后台改完配置、
    不用等重启就能生效（rate_limit / image_mode 这类参数改错的代价也最小）。"""
    try:
        from admin import cfg as _c
        return _c.read_config().get("api") or {}
    except Exception:
        return {}


# ---------------------------------------------------------------- 运行时启停开关
# 接口路由挂在主服务进程内，Flask 3 不允许运行时增删 route，所以启动时把路由
# 挂好，用这个开关控制放行。关闭时直接回 404 —— 对外与「路由根本没有」完全一致，
# 保留了原设计「接口没启用时不暴露给扫描器」的安全语义。
_API_STATE = {"enabled": False, "registered": False, "started_at": 0.0,
              "restarts": 0, "runtime": None}


def _disabled():
    return Response(status=404)


def api_state():
    """给管理端 /healthz / 内部控制接口用的状态快照。"""
    cfg = _cfg()
    return {"enabled": bool(_API_STATE["enabled"]),
            "registered": bool(_API_STATE["registered"]),
            "key_set": bool(_token_of(cfg)),
            "config_enabled": bool(cfg.get("enabled")),
            "started_at": _API_STATE["started_at"],
            "restarts": _API_STATE["restarts"]}


def api_control(action):
    """启动 / 停止 / 重启知识库接口。返回 (ok, message, state)。

    「重启」不重启进程：清空内存里的多轮上下文与限流计数、重新放行，
    相当于让接口以干净状态重新开始服务。
    """
    if not _API_STATE["registered"]:
        return False, "接口未注册：请在 data/config.yaml 配好 api.api_key 后重启主服务", api_state()
    action = str(action or "").strip().lower()
    if action == "start":
        _API_STATE["enabled"] = True
        _API_STATE["started_at"] = time.time()
        msg = "知识库接口已启动（POST /httpapi/query 可用）"
    elif action == "stop":
        _API_STATE["enabled"] = False
        _API_STATE["started_at"] = 0.0
        msg = "知识库接口已停止（/httpapi/query 返回 404）"
    elif action == "restart":
        rt = _API_STATE.get("runtime") or {}
        for k in ("limiter", "sessions"):
            obj = rt.get(k)
            if obj and hasattr(obj, "clear"):
                obj.clear()
        _API_STATE["enabled"] = True
        _API_STATE["started_at"] = time.time()
        _API_STATE["restarts"] += 1
        msg = "知识库接口已重启（多轮上下文与限流计数已清空）"
    else:
        return False, "action 只能是 start / stop / restart", api_state()
    return True, msg, api_state()


def _int(v, default, lo=0, hi=1 << 31):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 限流

class _RateLimiter:
    """固定窗口计数（每 IP 每窗口 N 次）。

    刻意不做滑动窗口/令牌桶：这个接口的瓶颈是 LLM（一次十几秒），
    真被并发打满时进程线程池先满、限流已经没意义了。要的是「别被顺手刷爆烧钱」。
    单机单进程用 dict 就够，不引入外部依赖（部署件越简单越好）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._hits = {}

    def clear(self):
        """管理端「重启知识库接口」时清掉限流计数。"""
        with self._lock:
            self._hits.clear()

    def allow(self, ip, limit, window):
        if limit <= 0:
            return True, 0
        now = time.time()
        with self._lock:
            arr = self._hits.get(ip) or []
            arr = [t for t in arr if now - t < window]
            if len(arr) >= limit:
                self._hits[ip] = arr
                return False, limit - len(arr)
            arr.append(now)
            self._hits[ip] = arr
            # 顺手清掉空槽：长期运行 + 大量来源 IP 时 dict 会只涨不清
            if len(self._hits) > 4096:
                for k in [k for k, v in self._hits.items() if not v]:
                    self._hits.pop(k, None)
            return True, 0


# ---------------------------------------------------------------- 多轮上下文

class _Sessions:
    """按 session_id 保留最近若干轮对话（**仅内存**，重启即失忆）。

    为什么单独一份而不复用微信那边的 _conversations：
    微信那边是按 external_userid 存的，还会参与全局 LRU 裁剪、落盘 .conversations.json。
    HTTP 调用方的会话塞进去会污染真实客户的记忆与统计（后台「正在服务中」会把
    调用方算成门店客户）。这里刻意隔离，且可由 api.session_memory 关掉。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._d = OrderedDict()          # sid -> {"hist": [...], "ts": float}

    def clear(self):
        """管理端「重启知识库接口」时清掉多轮上下文。"""
        with self._lock:
            self._d.clear()

    def _sweep(self, ttl, cap):
        now = time.time()
        for k in [k for k, v in self._d.items() if now - v["ts"] > ttl]:
            self._d.pop(k, None)
        while len(self._d) > cap:
            self._d.popitem(last=False)

    def get(self, sid):
        if not sid:
            return None
        with self._lock:
            it = self._d.get(sid)
            if not it:
                return None
            self._d.move_to_end(sid)
            it["ts"] = time.time()
            return list(it["hist"])

    def put(self, sid, q, a, limit, ttl, cap):
        if not sid:
            return
        with self._lock:
            it = self._d.get(sid)
            if not it:
                it = {"hist": [], "ts": time.time()}
                self._d[sid] = it
            self._d.move_to_end(sid)
            it["hist"].append({"role": "user", "content": q})
            it["hist"].append({"role": "assistant", "content": a})
            while len(it["hist"]) > limit:
                it["hist"].pop(0)
            it["ts"] = time.time()
            self._sweep(ttl, cap)

    def drop(self, sid):
        with self._lock:
            self._d.pop(sid, None)


# ---------------------------------------------------------------- 签名

def _secret(token):
    """签名密钥：由 token 派生，不额外引入配置项，也**不落盘**。
    换 token 之后所有旧的图片链接同时失效（这是想要的）。"""
    return hashlib.sha256(("kbapi/v1|" + (token or "")).encode("utf-8")).digest()


def _sign(token, name, exp):
    m = "%s|%d" % (name, int(exp))
    return hmac.new(_secret(token), m.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _sign_ok(token, name, exp, sig):
    """验签 + 验有效期。用 compare_digest 常数时间比较，别用 ==。

    返回 (是否通过, 剩余秒数)；不通过时若因为过期，剩余秒数为**负数**，
    调用方据此区分「链接过期了，重新调一次」和「签名不对，参数被改了」。
    """
    try:
        exp = int(exp)
        sig = (sig or "").strip()
    except (TypeError, ValueError):
        return False, 0
    now = int(time.time())
    if exp < now:                       # 过期
        return False, exp - now
    # 签名是对「文件名」算的。Flask 已把路径参数解码过一次，而部分客户端会
    # 再编码一次，于是同一个文件有两种可能的字符串形态（如含 % 的名字）。
    # 两种都试一遍：正常情况第一种就过，含 % 的老链接第二种兜住。
    cands = [name]
    alt = _safe_name(name)
    if alt and alt != name:
        cands.append(alt)
    for cand in cands:
        if hmac.compare_digest(sig, _sign(token, cand, exp)):
            return True, exp - now
    return False, 0


# ---------------------------------------------------------------- 图片

def _knowledge_dir(brain):
    """知识库目录（与 brain 建索引时用的是同一个配置项）。"""
    try:
        d = ((brain.retriever.cfg or {}).get("knowledge_dir")) or "./data/knowledge"
    except Exception:
        d = "./data/knowledge"
    if not os.path.isabs(d):
        d = os.path.join(BASE_DIR, d)
    return os.path.realpath(d)


def _images_dir(brain):
    return os.path.join(_knowledge_dir(brain), "images")


_UNESCAPE_U_RE = re.compile(r"%u([0-9a-fA-F]{4})")


def _safe_name(name):
    """文件名规范化：把各种「被编码过」的形态还原回磁盘上的真名。

    为什么必须处理这么多种：调用方拼 URL 的方式五花八门，
    同一张图会以不同形态到达服务端，任何一种没兜住都是「图裂了」：
      ① 原样中文            「客户常见.png」        —— Flask 解码后就是这个
      ② encodeURIComponent  「%E5%AE%A2%E6%88%B7.png」
      ③ **双重编码**        「%25E5%25AE%25A2.png」—— 前端框架经典坑
                             （marked 已 encodeURI，拼 query 时又 encodeURIComponent），
                             Flask 只解一层 → 名字里还留着字面 %
      ④ **escape() 的 %uXXXX**「%u5ba2%u6237.png」—— JS 老 API，
                             unquote 完全解不了；且名字里的**反斜杠**若先被
                             replace("\\","/") 归一，路径结构会被提前破坏

    顺序要求：%uXXXX **必须最先解**（在反斜杠归一之前），再 unquote，最后取 basename。
    """
    n = (name or "").strip()
    # ① escape() 的 %uXXXX —— 必须最先处理
    if "%u" in n.lower():
        try:
            n = _UNESCAPE_U_RE.sub(lambda m: chr(int(m.group(1), 16)), n)
        except (ValueError, OverflowError):
            pass
    # ② 百分号编码（双重编码会解掉一层，还可能剩一层，下面 basename 前再兜一次）
    if "%" in n:
        try:
            n = urllib.parse.unquote(n)
        except Exception:
            pass
    # ③ 双重编码的第二层：unquote 后可能还带 %
    if "%" in n:
        try:
            n = urllib.parse.unquote(n)
        except Exception:
            pass
    return n.replace("\\", "/").split("/")[-1]


def _safe_image_path(brain, name):
    """把请求里的图片名解析成 data/knowledge/images 下的绝对路径。

    三道锁，缺一不可：
      1) 路由本身已限制「不能含 /」（用 <name> 而非 <path:name>）；
      2) 白名单正则挡住 `..`、空格、反斜杠、命令注入字符；
      3) realpath 后**再确认仍在 images 目录内** —— 防符号链接把读取引到目录外。
    名字先经 _safe_name 还原（容忍各种编码形态，见那里的说明）。
    """
    name = _safe_name(name)
    if not _SAFE_IMG_RE.match(name):
        return None
    if not _IMG_EXT_RE.match(os.path.splitext(name)[1]):
        return None
    root = _images_dir(brain)
    try:
        rp = os.path.realpath(os.path.join(root, name))
        rr = os.path.realpath(root)
    except OSError:
        return None
    if not (rp == rr or rp.startswith(rr + os.sep)):
        return None
    if not os.path.isfile(rp):
        return None
    return rp


def _img_meta(brain, name, mode, token, ttl, base_url):
    """组装一张图片的对外描述。base_url 由 public_base_url 决定（空则用相对路径）。"""
    ext = os.path.splitext(name)[1].lower()
    item = {"name": name, "mime": _IMG_EXT_MIME.get(ext, "application/octet-stream")}
    if mode in ("url", "both"):
        exp = int(time.time()) + int(ttl)
        q = "exp=%d&sign=%s" % (exp, _sign(token, name, exp))
        # ⚠️ 文件名**必须百分号编码一次**再拼进 URL。
        # 不编码的话中文名在部分客户端（curl 之外的很多 HTTP 库、老浏览器）
        # 会因为非 ASCII 直接请求失败；编码一次才是标准做法。
        # ⚠️ 但**绝不能编码两次** —— 调用方拿到 URL 后若再 encodeURIComponent
        # 就成了双重编码，Flask 只解一层 → 404。这正是后台预览踩过的坑
        # （marked 已 encodeURI，前端又 encodeURIComponent）。
        # 所以这里编一次，并在 _safe_name 里容忍「再多一层」与 JS escape() 的 %uXXXX。
        seg = urllib.parse.quote(name, safe="")
        item["url"] = ("%s/httpapi/image/%s?%s" % (base_url, seg, q)) if base_url \
            else ("/httpapi/image/%s?%s" % (seg, q))
        item["expires_at"] = exp
    if mode in ("base64", "both"):
        fp = _safe_image_path(brain, name)
        if not fp:
            return None
        try:
            with open(fp, "rb") as f:
                raw = f.read()
        except OSError:
            return None
        item["size"] = len(raw)
        item["base64"] = "data:%s;base64,%s" % (
            item["mime"], base64.b64encode(raw).decode("ascii"))
    return item


def _public_base_url(brain, cfg):
    """图片 URL 的前缀。优先 server.public_base_url（对外域名），
    没配就用相对路径 —— 相对路径在同源调用（自家后台/前端）里完全够用，
    跨机调用方拿到的相对路径自己拼域名，不会因为写死 IP 而在换域名后全挂。"""
    base = ((cfg.get("server") or {}).get("public_base_url") or "").rstrip("/")
    if base:
        return base
    return ""


# ---------------------------------------------------------------- 鉴权

def _token_of(cfg):
    """读 api.api_key。字段名刻意带 _api_key 后缀 —— 后台的密钥遮蔽名单
    按后缀匹配，裸 `token` 被刻意排除（歧义太大），用 `token` 会导致这个
    唯一的防线在后台以明文显示、还能被截图带出去。"""
    return str((cfg.get("api_key") or "")).strip()


def _client_ip():
    """取调用方 IP。只认 X-Forwarded-For 的**第一个**值（多级反代时最左才是真客户端）。
    直连时该头没有，落到 remote_addr。"""
    xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if xff:
        return xff[:64]
    return (request.remote_addr or "-")[:64]


def _auth_ok(cfg, token):
    """校验 Bearer Token。

    接受三种传法，因为调用方五花八门：
      Authorization: Bearer <token>   （标准，推荐）
      X-API-Key: <token>              （有些网关/小程序 SDK 只方便加自定义头）
      ?token=<token>                  （浏览器 <img>、curl 方便；会进 nginx 访问日志）
    """
    got = ""
    h = (request.headers.get("Authorization") or "").strip()
    if h[:7].lower() == "bearer ":
        got = h[7:].strip()
    if not got:
        got = (request.headers.get("X-API-Key") or "").strip()
    if not got:
        got = (request.args.get("token") or "").strip()
    return bool(got) and hmac.compare_digest(got, token)


def _cors(cfg, resp):
    """按配置补 CORS 头。默认**不发** —— 浏览器跨域调不了是刻意的：
    这是个烧 token 的接口，宁可让用户显式配 api.cors_origins 也不默认对全网开。"""
    origins = str(cfg.get("cors_origins") or "")
    if not origins:
        return resp
    allowed = [o.strip() for o in origins.split(",") if o.strip()]
    if not allowed:
        return resp
    origin = (request.headers.get("Origin") or "").strip()
    if origin and (origin in allowed or "*" in allowed):
        resp.headers["Access-Control-Allow-Origin"] = origin if "*" not in allowed else "*"
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-API-Key"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "600"
    return resp


def _err(cfg, code, msg):
    d = {"ok": False, "error": msg, "code": code}
    return _cors(cfg, jsonify(d)), code


# ---------------------------------------------------------------- 主体

def _api_cost(attempts, brain):
    """按当前单价算这次调用花了多少钱。

    复用 pricing.compute_cost（与 llm_usage 同一套单价），算出来的值**存进库**，
    不在查询时重算 —— 单价会变，存历史值才对账。
    取不到单价时返回 None，UI 显示「—」而不是 0 元（0 会被误读成「不花钱」）。
    """
    if not attempts:
        return None
    try:
        from admin import pricing
        model = ((getattr(brain, "llm", None) or {}).get("model"))
        pt = sum(a.get("prompt_tokens") or 0 for a in attempts)
        ct = sum(a.get("completion_tokens") or 0 for a in attempts)
        cost, known = pricing.compute_cost(model, pt, ct)
        return round(float(cost), 6) if known else None
    except Exception:
        return None


def _record_api_call(log, resp, status, cost_ms, q, sid, cost=None, attempts=None):
    """把一次 /httpapi/query 调用落库。**任何异常都吞掉**（统计不能拖垮调用方）。

    为什么要在这里统一记、而不是在每个 return 前各记一次：
    _query_inner() 里有 6 个提前返回（鉴权失败/限流/参数空/超长/session_id 非法/
    brain 异常），逐个加埋点**必然漏** —— 而漏掉的恰好是「接口出问题」那几种，
    等于出故障时后台一片空白，只能去翻 app.log（最难排查的地方）。
    所以改成在出口统一记：成功与失败都记，失败原因从响应体的 error 里取。

    ⚠️ 「接口没开」的情况（路由压根没注册）不会走到这里 —— 那类调用
    Flask 直接 404，压根进不到视图函数。后台显示 0 条是准确的。

    cost 由调用方在**锁内**算好传进来：不能从响应体读（费用不对外暴露，
    见 _query_inner 里的说明），也不能在这里读 brain 属性（锁已经放开了，
    那是另一个请求的用量 —— 记到别人头上）。
    """
    try:
        import store
        try:
            d = resp.get_json(silent=True)
        except Exception:
            d = None
        d = d if isinstance(d, dict) else {}
        ok = bool(d.get("ok")) and 200 <= int(status or 0) < 300
        # 答案可能被 plain 模式降级过，这里存的是**真正返回给调用方的文本**
        imgs = d.get("images")
        u = d.get("usage") or {}
        store.log_api_call(
            question=d.get("question") or q,
            answer=d.get("answer"),
            n_images=len(imgs) if isinstance(imgs, list) else 0,
            q_chars=len(d.get("question") or q or ""),
            session_id=d.get("session_id") or sid or "",
            client_ip=_client_ip(),
            status=int(status or 0),
            ok=ok,
            latency_ms=cost_ms,
            calls=u.get("calls"),
            total_tokens=u.get("total_tokens"),
            cost=cost,
            error=(None if ok else (d.get("error") or ("HTTP %s" % status))),
        )
    except Exception:
        pass


def _handle_query(brain, brain_lock, limiter, sessions, log):
    """对外入口：**只负责计时与埋点**，真正的业务在 _query_inner 里。

    这样切分是为了让「记录一次调用」这件事只有一个出口 ——
    inner 里那 6 个提前 return 全都自动被覆盖到，一个都不会漏。
    """
    # ⚠️ OPTIONS 预检**必须在埋点之前直接返回**，两个理由：
    #   ① 预检不是「一次调用」—— 浏览器每次跨域调用都会先发一个，
    #      记进统计会让调用量翻倍、还会凭空多出一堆"失败"；
    #   ② 它返回的是 `Response(status=204)`，没有 JSON 体，
    #      走到埋点会被判成 ok=false + error="HTTP 200"（假失败）。
    # 预检本来就该在鉴权之前回（浏览器不发 Authorization），所以挪到这里安全。
    if request.method == "OPTIONS":
        return _cors(_cfg(), Response(status=204))

    t0 = time.time()
    q, sid = "", ""
    try:
        # q / sid 尽力提前取一次：inner 里任何一个提前返回都要能记下问题原文，
        # 否则失败记录就只有一句「Token 无效」，排查时看不到调用方到底问了什么
        q, sid = _peek_question()
    except Exception:
        pass
    try:
        rv = _query_inner(brain, brain_lock, limiter, sessions, log)
        # inner 的返回值形态不统一（有的是 response，有的是 (response, code)），
        # 这里统一成 (response, status)，别去改 inner 的 6 个 return
        if isinstance(rv, tuple):
            resp = rv[0]
            status = int(rv[1]) if len(rv) > 1 else 200
        else:
            # ⚠️ 单个 response 时状态码**必须从 response 自己取**，不能默认 200：
            # `Response(status=204)` 这类非 JSON 返回会被误判成"200 但失败"。
            resp = rv
            status = int(getattr(rv, "status_code", 200) or 200)
    except Exception:
        log.exception("知识库 API 处理异常（已跳过）")
        return _err(_cfg(), 500, "AI 服务暂时不可用，请稍后再试或转人工。")

    # 费用**必须锁内算**：last_attempts 是 brain 的实例属性，一放开锁就可能被
    # 微信回复或另一个 API 请求覆盖 → 费用记到别人头上。这里只在 inner 返回
    # 后取一次（inner 内部已持过一次锁，不重复加锁 —— 每请求多一次锁会实打实
    # 拖低并发吞吐，而这接口本身就慢）。
    cost = None
    try:
        if 200 <= int(status or 0) < 300:      # 只有成功答出来的才有费用
            with brain_lock:
                cost = _api_cost(
                    [dict(x) for x in (getattr(brain, "last_attempts", None) or [])],
                    brain)
    except Exception:
        pass
    _record_api_call(log, resp, status, int((time.time() - t0) * 1000),
                     q, sid, cost=cost)
    return rv


def _peek_question():
    """在真正处理前把 question / session_id 摸出来（只给埋点用）。

    为什么单独摸一遍而不是从响应体里取：鉴权失败/限流/参数错误这些分支
    压根走不到解析 question 那步，响应体里也没有 question。
    而「谁在用这个接口、问了什么、为什么被拒」恰恰是排查时最需要的信息。
    """
    q = ""
    if request.method == "POST":
        d = request.get_json(silent=True)
        if isinstance(d, dict):
            q = d.get("question") or d.get("q") or ""
        if not q:
            q = request.form.get("question") or request.form.get("q") or ""
    if not q:
        q = request.args.get("question") or request.args.get("q") or ""
    sid = request.args.get("session_id") or ""
    if not sid and request.method == "POST":
        d = request.get_json(silent=True)
        if isinstance(d, dict):
            sid = d.get("session_id") or ""
    return (q or "").strip(), (sid or "").strip()[:64]


def _resolve_sources(cfg, log):
    """解析本次问答允许检索的知识库文档（知识库策略过滤）。

    优先级：`sources` > `policy` > `api.default_policy` > None（全库）。
      - `sources`: ["a.md","b.md"] 或逗号分隔字符串；显式空数组 = 空策略（不检索）
      - `policy` : 后台「知识库策略」的名称或 id，展开成该策略包含的文档
    指定但找不到的策略会返回一个错误字符串（调用方回 400），
    **绝不静默退化成全库检索** —— 那是跨策略读库的越权。
    """
    args = request.args
    body = request.get_json(silent=True) if request.method == "POST" else None
    body = body if isinstance(body, dict) else {}

    def _param(name):
        v = args.get(name)
        if v is None:
            v = body.get(name)
        if v is None and request.method == "POST":
            v = request.form.get(name)
        return v

    raw = _param("sources")
    policy = str(_param("policy") or "").strip()
    if raw is not None:
        if isinstance(raw, (list, tuple)):
            sources = [str(x).strip() for x in raw if str(x).strip()]
        else:
            s = str(raw).strip()
            sources = [x.strip() for x in s.split(",") if x.strip()]
        label = "sources"
    elif policy:
        try:
            import store
            hit = store.policy_docs(policy)
        except Exception as e:
            log.warning("读取知识库策略失败: %s", e)
            return None, None, "知识库配置服务不可用"
        if hit is None:
            return None, None, "知识库配置不存在：%s" % policy
        pname, sources = hit
        label = pname
    else:
        default = str(cfg.get("default_policy") or "").strip()
        if default:
            try:
                import store
                hit = store.policy_docs(default)
            except Exception:
                hit = None
            if hit:
                label, sources = hit
            else:
                return None, "全库", None
        else:
            return None, "全库", None
    if not sources:
        label = label or "空知识库"
    return sources, label, None


def _query_inner(brain, brain_lock, limiter, sessions, log):
    """真正的业务逻辑。**只在 _handle_query 里被调用**，不要直接调。

    OPTIONS 预检的处理已上移到 _handle_query（那里要先于埋点返回，
    否则每次跨域调用都会凭空多出一条"失败"记录）。
    """
    cfg = _cfg()
    token = _token_of(cfg)
    if not token:
        # 走到这里说明配置被改成了 enabled=true 但 token 空了。
        # 此时**拒绝服务**并说明原因，而不是放行（放行 = 公开烧 token 接口）。
        log.error("知识库 API 已启用但 api.api_key 为空 → 已拒绝所有请求。"
                  "请在 config.yaml 的 api 段填 api_key 后重启 bridge。")
        return _err(cfg, 401, "接口未正确配置（api.api_key 为空），请联系管理员")

    if not _auth_ok(cfg, token):
        return _err(cfg, 401, "Token 无效或缺失（用 Authorization: Bearer <token>）")

    limit = _int(cfg.get("rate_limit_per_min"), 30)
    window = _int(cfg.get("rate_window"), 60, lo=1)
    ok, _ = limiter.allow(_client_ip(), limit, window)
    if not ok:
        r, code = _err(cfg, 429, "请求过于频繁，请 %d 秒后重试" % max(1, window))
        r.headers["Retry-After"] = str(max(1, window))
        return r, code

    # —— 取问题：JSON / 表单 / query 参数都支持 ——
    q = ""
    if request.method == "POST":
        d = request.get_json(silent=True)
        if isinstance(d, dict):
            q = d.get("question") or d.get("q") or ""
        if not q:
            q = request.form.get("question") or request.form.get("q") or ""
    if not q:
        q = request.args.get("question") or request.args.get("q") or ""
    q = (q or "").strip()
    if not q:
        return _err(cfg, 400, "参数 question 不能为空")
    # 可选：调用方「特别指定」本次用的大模型（名称 / 上游模型 ID）。
    # 不传则由后台「AI 大模型」页选定的默认模型接管；限长防滥用。
    model = ""
    if request.method == "POST":
        d = request.get_json(silent=True)
        if isinstance(d, dict):
            model = d.get("model") or ""
        if not model:
            model = request.form.get("model") or ""
    if not model:
        model = request.args.get("model") or ""
    model = (model or "").strip()[:80]

    maxq = _int(cfg.get("max_q_chars"), 500, lo=1)
    if len(q) > maxq:
        return _err(cfg, 400, "问题过长（最多 %d 字，当前 %d 字）" % (maxq, len(q)))

    sid = (request.args.get("session_id") or "").strip()
    if not sid and request.method == "POST":
        dj = request.get_json(silent=True)
        if isinstance(dj, dict):
            sid = (dj.get("session_id") or "").strip()
    # 外部传进来的 session_id 会进内存 dict 当键，必须限长限形：
    # ① 超长/含怪字符的键会占住一个会话槽，把 max_sessions 挤满 → 真会话被 LRU 挤掉；
    # ② 先判长度再截断 —— 截断会让「超长」这个攻击面看起来合法（截到 64 位就通过了）。
    sid = (sid or "").strip()
    if sid and (len(sid) > 64 or not re.match(r"^[\w:.\-]{1,64}$", sid)):
        return _err(cfg, 400, "session_id 只能含字母/数字/下划线/连字符/点/冒号，最长 64 位")
    reset = request.args.get("reset") == "1"

    # —— 知识库策略：按策略包含的文档检索，未指定策略时沿用纯接口模式的全库检索 ——
    sources, policy_label, perr = _resolve_sources(cfg, log)
    if perr:
        return _err(cfg, 400, perr)

    use_mem = bool(cfg.get("session_memory", True))
    if reset and sid:
        sessions.drop(sid)
    history = sessions.get(sid) if (use_mem and sid) else None

    mode = str(cfg.get("image_mode") or "url").strip().lower()
    if mode not in ("url", "base64", "both"):
        mode = "url"
    ttl = _int(cfg.get("image_url_ttl") or cfg.get("image_ttl"), 600, lo=30)
    base_url = _public_base_url(brain, cfg)

    t0 = time.time()
    # ⚠️ 锁必须覆盖 answer() **和** last_images 的读取。
    # last_images 是 brain 的实例属性，不加锁的话微信那边会给客户发错图。
    with brain_lock:
        try:
            ans = brain.answer(q, history=history, sources=sources,
                               model=(model or None))
        except Exception:
            # brain 内部已兜底成统一话术；这里真抛异常说明是它没预料到的问题。
            # 细节只落日志，不回传给调用方。
            log.exception("知识库 API 调用 brain.answer() 异常: %s", q[:40])
            return _err(cfg, 500, "AI 服务暂时不可用，请稍后再试或转人工。")
        imgs = list(getattr(brain, "last_images", None) or [])
        attempts = [dict(a) for a in (getattr(brain, "last_attempts", None) or [])]
        llm_id_used = (getattr(brain, "llm", None) or {}).get("id")
    cost_ms = int((time.time() - t0) * 1000)

    # 兜底话术时 last_images 已被 brain 清空，这里再兜一层：理论上不该有图，
    # 但真出现「回答是兜底 + 带着图」时宁可不发图（错的图比没图更糟）。
    if ans.startswith("AI 服务暂时不可用"):
        imgs = []

    images = []
    for p in imgs:
        nm = os.path.basename(p)
        it = _img_meta(brain, nm, mode, token, ttl, base_url)
        if it:
            images.append(it)

    if cfg.get("plain", False):
        try:
            from textutil import strip_markdown
            ans = strip_markdown(ans)
        except Exception:
            pass

    if use_mem and sid:
        sessions.put(sid, q, ans, 20,
                     _int(cfg.get("session_ttl"), 1800, lo=60),
                     _int(cfg.get("max_sessions"), 500, lo=1))

    if cfg.get("record_usage", True) and attempts:
        try:
            import store
            store.log_llm_usage(attempts, source="api", llm_id=llm_id_used)
        except Exception:
            pass

    # ⚠️ 绝不把提问原文打进 app.log（生产 INFO 级 = 全量落盘）。
    # 只记长度与耗时；要复现具体问题得靠 debug 级 + 用户主动开。
    log.info("知识库 API | 提问 %d 字 | %dms | 图 %d 张%s",
             len(q), cost_ms, len(images),
             (" | session=%s" % sid) if sid else "")

    usage = None
    tot = [a.get("total_tokens") for a in attempts if a.get("total_tokens") is not None]
    if tot:
        usage = {"calls": len(attempts), "total_tokens": sum(tot)}
        # 取不到就整块不返回，绝不填 0 —— 0 会被调用方误读成「没消耗」
        for k in ("prompt_tokens", "completion_tokens"):
            vs = [a.get(k) for a in attempts if a.get(k) is not None]
            if vs:
                usage[k] = sum(vs)
        # ⚠️ 费用**绝不进响应体**：单价是本地估算、不是账单，暴露出去会被
        # 调用方当成账单数字去对账，亏了钱来找我们。后台埋点自己算（见 _api_cost）。

    return _cors(cfg, jsonify({
        "ok": True,
        "question": q,
        "answer": ans,
        "images": images,
        "session_id": sid or None,
        "policy": policy_label,
        "sources": sources,
        "elapsed_ms": cost_ms,
        "usage": usage,
    }))


def _content_disposition(name):
    """构造 Content-Disposition 响应头。

    ⚠️ **头里绝不能出现非 ASCII 字符**。中文文件名原样放进去，gunicorn 会判定
    「Invalid HTTP Header」并**直接 400 掐断连接** —— 客户端只看到
    `RemoteDisconnected` / HTTP 000，连错误响应体都读不到，症状像"路由坏了"，
    真凶却是响应头。这类 bug 光看 Flask 代码是看不出来的。

    按 RFC 6266 正确写法：
      filename="export.png"          → ASCII 兜底（老客户端用）
      filename*=UTF-8''%E4%B8%80...  → 真实中文名（新浏览器用，会正确解码）
    """
    ext = os.path.splitext(name)[1].lower() or ".png"
    ascii_name = "image" + ext
    quoted = urllib.parse.quote(name, safe="")
    return 'inline; filename="%s"; filename*=UTF-8\'\'%s' % (ascii_name, quoted)


def _handle_image(brain, cfg, log, name):
    """GET /httpapi/image/<name>?exp=&sign=

    图片本身是知识库内容，**必须签名 + 限时**，否则等于公开图片库。
    """
    token = _token_of(cfg)
    if not token:
        return _err(cfg, 401, "接口未正确配置（api.api_key 为空）")
    exp = request.args.get("exp")
    sig = request.args.get("sign")
    # 也允许直接带 token 取图（调试方便）：仍要过白名单与目录限制
    if not sig and request.args.get("token"):
        if not _auth_ok(cfg, token):
            return _err(cfg, 401, "Token 无效")
    else:
        ok, left = _sign_ok(token, name, exp, sig)
        if not ok:
            msg = ("图片链接已过期，请重新调用 /httpapi/query 获取"
                   if left < 0 else "图片签名无效")
            return _err(cfg, 403, msg)

    fp = _safe_image_path(brain, name)
    if not fp:
        return _err(cfg, 404, "图片不存在")
    ext = os.path.splitext(fp)[1].lower()
    with open(fp, "rb") as f:
        raw = f.read()
    resp = Response(raw, mimetype=_IMG_EXT_MIME.get(ext, "application/octet-stream"))
    # ⚠️ **Content-Disposition 里绝不能直接放中文文件名**。
    # HTTP 头只允许 ASCII，中文原样塞进去 gunicorn 会判定
    # 「Invalid HTTP Header」并**直接 400 掐断连接**（不是 500，是连接被关掉，
    # 客户端只看到 RemoteDisconnected / HTTP 000，根本读不到任何错误信息）。
    # 症状极像"图片路由坏了"，但真凶是响应头。
    # RFC 5987/6266 的正确做法：ASCII 兜底名放 filename，真实中文名放 filename*。
    resp.headers["Content-Disposition"] = _content_disposition(name)
    resp.headers["Cache-Control"] = "private, max-age=%d" % _int(cfg.get("image_ttl"), 600, lo=30)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return _cors(cfg, resp)


# ---------------------------------------------------------------- 注册

def register(app, brain, brain_lock, log):
    """把路由挂到主服务的 Flask 上，并初始化运行时启停开关。

    Flask 3 不允许在首次请求后增删 route，所以只要 `api.api_key` 配好就把路由
    挂上，**是否放行由 `_API_STATE["enabled"]` 决定**；关闭时 `/httpapi/query` 与
    `/httpapi/image` 直接返回 404，对外与「路由根本不存在」完全一致 —— 保留了
    原设计「接口没启用时不暴露给扫描器」的安全语义，同时让管理端可以随时启停。
    初始状态仍取 `api.enabled`，所以不改配置时行为和以前一模一样。
    """
    cfg = _cfg()
    if not _token_of(cfg):
        _API_STATE.update(enabled=False, registered=False, runtime=None)
        log.error("知识库 HTTP 接口 | api.api_key 为空 → 未注册路由，/httpapi/query 保持 404。"
                  "客服功能不受影响。"
                  '生成一个：python -c "import secrets;print(secrets.token_urlsafe(32))"')
        return False

    enabled = bool(cfg.get("enabled"))
    limiter = _RateLimiter()
    sessions = _Sessions()
    _API_STATE.update(enabled=enabled, registered=True,
                      started_at=(time.time() if enabled else 0.0),
                      runtime={"limiter": limiter, "sessions": sessions})
    # 限流与会话由下面的路由闭包共享（多线程单例），无需再挂 app.config

    @app.route("/httpapi/query", methods=["POST", "GET", "OPTIONS"])
    def kb_query():
        if not _API_STATE["enabled"]:
            return _disabled()
        return _handle_query(brain, brain_lock, limiter, sessions, log)

    @app.route("/httpapi/image/<name>", methods=["GET", "OPTIONS"])
    def kb_image(name):
        # name 来自**路径参数**（Flask 已 URL 解码过一次），不是 request.args。
        if not _API_STATE["enabled"]:
            return _disabled()
        return _handle_image(brain, _cfg(), log, name)

    if enabled:
        log.info("知识库 HTTP 接口 | 已开启 | POST %s/httpapi/query | 鉴权=Authorization: Bearer <api_key>"
                 " | 图片=%s | 多轮=%s",
                 _public_base_url(brain, cfg), cfg.get("image_mode", "url"),
                 bool(cfg.get("session_memory", True)))
    else:
        log.info("知识库 HTTP 接口 | 待启动（api.enabled=false）；可在运行监控页点「启动」")
    return True