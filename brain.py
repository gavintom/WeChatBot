"""大脑：外部 LLM（OpenAI 兼容）+ 混合检索 RAG

检索链路（效果优先）：
    文档 → 分片(chunk)
         → ① BM25 关键词召回  ┐
         → ② 向量语义召回      ┘ → RRF 融合 →（可选）cross-encoder 重排 → top_k 进 prompt

为什么混合：
    BM25 抓"专有名词/型号/价格"这类字面精确匹配；向量抓"换个说法也能懂"的语义。
    两路互补，RRF（Reciprocal Rank Fusion）融合比按分数加权更鲁棒。

图片（2026-10-15 新增）：
    知识库图片统一放 data/knowledge/images/，在 markdown 里用标准语法引用：
        ![导出报表的操作界面](images/导出报表.png)
    索引时做三件事：
      1. 图片语法换成内部占位符，保证「图」和「图周围的文字」落在**同一个 chunk**；
      2. 记住每个 chunk 带了哪些图（self.images）；
      3. 每张图额外生成一张「图片卡」chunk（用 alt 说明 + 所属小标题做文本），
         这样「导出报表怎么弄」这种**只靠图注就能答**的问题也能召回图。
    回答时把候选图以「图1 / 图2」编号喂给模型，模型在答案里写 [[图1]] 就发那���图；
    模型没写且开了 auto_fallback 时，兜底发 top1 那张图。

依赖（见 requirements.txt）：
    jieba                            中文分词（BM25 用）
    rank_bm25                        BM25Okapi
    sentence-transformers + torch    向量编码 + 重排
    numpy                            向量检索（暴力点积；>10 万 chunk 再上 faiss）

索引缓存：
    首次启动编码所有 chunk 并落盘到 data/knowledge/.index/emb_<指纹>_<chunk数>.npy。
    指纹 = chunks 内容 + 模型名 + 切分参数，不受文件 mtime / 绝对路径影响；
    只有知识库文档内容真的改了（或换模型）才重建，且 .index/ 只保留当前缓存。
"""
import hashlib
import json
import math
import os
import re
import socket
import threading
import time
import traceback
from urllib.parse import urlparse

# numpy 缺失时不阻断服务启动（检索路自动降为 none）
try:
    import numpy as np
except Exception:                                   # pragma: no cover
    np = None
import requests

# —— HuggingFace 端点：必须赶在 import sentence_transformers 之前定好 ——
# huggingface_hub 在 import 时就把 ENDPOINT / HUGGINGFACE_CO_URL_TEMPLATE 定死了，
# 之后再改环境变量不生效。国内直连 huggingface.co 基本不通（Network is unreachable），
# 所以默认「镜像优先」；要强制官方源就显式设 HF_ENDPOINT=https://huggingface.co
HF_MIRROR = os.environ.get("HF_MIRROR", "https://hf-mirror.com").rstrip("/")
HF_OFFICIAL = "https://huggingface.co"
HF_ENDPOINT_PRIMARY = (os.environ.get("HF_ENDPOINT") or "").strip().rstrip("/") or HF_MIRROR
os.environ["HF_ENDPOINT"] = HF_ENDPOINT_PRIMARY
# 元数据（HEAD/etag）超时调小：端点不通时快速失败，别卡着 60 秒
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "10")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")

# —— 可选依赖，缺任一即自动降级，不会让服务起不来 ——
try:
    import jieba
    jieba.setLogLevel(20)
except Exception:                                   # pragma: no cover
    jieba = None

try:
    from rank_bm25 import BM25Okapi
except Exception:                                   # pragma: no cover
    BM25Okapi = None


if BM25Okapi is not None:
    class _BM25PositiveIdf(BM25Okapi):
        """IDF 恒正的 BM25Okapi 变体：只给负 IDF 一个**恒正地板**。

        rank_bm25 原生 `idf = log((N-df+0.5)/(df+0.5))`，词出现在过半文档时为负；
        它把负 IDF 兜底到 `eps = epsilon * average_idf`，可小库里 average_idf
        本身也是负的，地板跟着变负 → 所有 IDF 与分数全负。后果有两层：
          ① `sc > 0` 的命中判定把全部 chunk 过滤掉，AI 只能空着上下文回答；
          ② 排序语义反转——含查询词越多的片段分数越负、排得越靠后。
        触发条件是「文档数少」，2/3/4 篇同样中招；chunk 多的库只是碰巧
        average_idf 为正才没暴露，那是运气不是安全。

        这里**只修地板，不动打分公式**：
          - `raw idf > 0` 的词原样保留，与原生 BM25 逐词一致（大库排序语义不变）；
          - `raw idf <= 0` 的词兜底到 `eps = epsilon * max(average_idf, 1.0)`，
            `max(..., 1.0)` 保证小库里 eps 也是正数。
        为什么不用 `log(1+exp(idf))`（softplus）：它把被原生 eps 抬高的高频
        品牌词打回接近 0，实测「示例品牌有多少员工」答案片段的 BM25 名次从
        第 4 掉到 112、top4 完全召回不到，还会放大小正 IDF。只改地板则大库
        与原生完全一致、小库也不再全负，是最小侵入解。
        刻意不覆写上游私有的 `_calc_idf`，只在 `super().__init__()` 之后基于
        公开字段 `doc_freqs` / `corpus_size` 重算 idf，避免上游改内部实现就崩。
        """
        def __init__(self, corpus, **kw):
            super().__init__(corpus, **kw)
            nd = {}
            for freqs in self.doc_freqs:
                for w in freqs:
                    nd[w] = nd.get(w, 0) + 1
            n = self.corpus_size
            raw = {w: math.log((n - df + 0.5) / (df + 0.5))
                   for w, df in nd.items()} if nd else {}
            avg = (sum(raw.values()) / len(raw)) if raw else 0.0
            eps = 0.25 * max(avg, 1.0)          # epsilon 与 rank_bm25 默认一致
            self.idf = {w: (v if v > 0 else eps) for w, v in raw.items()}
            self.average_idf = (sum(self.idf.values()) / len(self.idf)) if self.idf else 0.0
else:                                               # pragma: no cover
    _BM25PositiveIdf = None

try:
    from sentence_transformers import SentenceTransformer, CrossEncoder
except Exception:                                   # pragma: no cover
    SentenceTransformer = None
    CrossEncoder = None


# —— HuggingFace 端点切换（运行期兜底）——
def _noop(*_a, **_k):
    pass


# 需要一起改的常量：ENDPOINT 是根，TEMPLATE / URL_HOME 是 import 时用 ENDPOINT 拼好的副本，
# 而 file_download 拼下载/HEAD 的 URL 用的是 TEMPLATE——只改 ENDPOINT 不够（这正是
# 「切了镜像却还在连 huggingface.co」的原因）。
_HF_MODULES = ("huggingface_hub.constants", "huggingface_hub",
               "huggingface_hub.file_download", "huggingface_hub.hf_api",
               "huggingface_hub._snapshot_download")


def _switch_endpoint(endpoint):
    """把端点切到 endpoint：改环境变量 + 补丁已加载模块里的全部相关常量。"""
    endpoint = (endpoint or "").rstrip("/")
    os.environ["HF_ENDPOINT"] = endpoint
    new = {
        "ENDPOINT": endpoint,
        "HUGGINGFACE_CO_URL_TEMPLATE": endpoint + "/{repo_id}/resolve/{revision}/{filename}",
        "HUGGINGFACE_CO_URL_HOME": endpoint + "/",
    }
    for mod_name in _HF_MODULES:
        try:
            mod = __import__(mod_name, fromlist=["_"])
        except Exception:                           # pragma: no cover
            continue
        for attr, val in new.items():
            if hasattr(mod, attr):
                try:
                    setattr(mod, attr, val)
                except Exception:                   # pragma: no cover
                    pass


def _ordered_endpoints():
    """(主端点, 备用端点)：主=HF_ENDPOINT（默认镜像），备用=另一个。"""
    primary = (os.environ.get("HF_ENDPOINT") or HF_ENDPOINT_PRIMARY).rstrip("/")
    fallback = HF_OFFICIAL if primary != HF_OFFICIAL else HF_MIRROR
    return primary, fallback


def _probe_endpoint(endpoint, timeout=3):
    """快速探活：TCP 能连上就算通（不校验内容）。明显不通就别白等了。

    timeout 取 3s：不可达的端点（如国内直连 huggingface.co）会白等满整个超时，
    而这只影响「要不要多试一个端点」的判断，不值得花 5s。
    """
    try:
        host = urlparse(endpoint).hostname
        if not host:
            return False
        with socket.create_connection((host, 443), timeout=timeout):
            return True
    except Exception:
        return False


# ---------- 本地缓存命中 → 离线加载 ----------
# 为什么要这段：模型已在本地缓存（HF cache）时，sentence-transformers 仍会向仓库发
# 一系列 HEAD/etag 请求做"有无更新"校验，国内走镜像每次都要几十秒——这段时间完全是白花的。
# 只要本地有完整权重就直接离线加载，启动速度提升一个量级；缓存不存在才联网下载。

def _hf_hub_cache_dir():
    """huggingface_hub 的 hub 缓存目录（放 models--xxx 的那一层）。"""
    v = (os.environ.get("HF_HUB_CACHE") or "").strip()
    if v:
        return os.path.expanduser(v)
    v = (os.environ.get("HF_HOME") or "").strip()
    if v:
        return os.path.join(os.path.expanduser(v), "hub")
    xdg = (os.environ.get("XDG_CACHE_HOME") or "").strip()
    base = os.path.expanduser(xdg) if xdg else os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "huggingface", "hub")


def _is_model_cached(repo_id):
    """本地缓存里是否已有该模型的**权重文件**。

    只看目录存在不够：缓存可能残缺（只有 config、权重没下完，或 blob 被清理过），
    这时开离线会加载失败。必须找到一个真实存在的 .bin/.safetensors 才敢走离线。
    os.path.exists 会跟随软链，正好能滤掉「snapshot 软链指向的 blob 已不在」的残骸。
    """
    if not repo_id or os.path.isdir(repo_id):      # 本地目录本身就是完整模型，不走缓存判断
        return False
    snaps = os.path.join(_hf_hub_cache_dir(),
                         "models--" + repo_id.replace("/", "--"), "snapshots")
    if not os.path.isdir(snaps):
        return False
    for rev in sorted(os.listdir(snaps)):
        d = os.path.join(snaps, rev)
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if n.endswith((".bin", ".safetensors")) and os.path.exists(os.path.join(d, n)):
                return True
    return False


def _set_offline(on):
    """运行期开关 HuggingFace 离线模式。

    huggingface_hub 的 HTTP 层是「每次请求读 constants.HF_HUB_OFFLINE」（而非 import 时
    快照），所以直接补丁常量即可即时生效——这点与 ENDPOINT 不同，不用改 URL 模板。
    """
    if on:
        os.environ["HF_HUB_OFFLINE"] = "1"
    else:
        os.environ.pop("HF_HUB_OFFLINE", None)
    for mod_name in _HF_MODULES:
        try:
            mod = __import__(mod_name, fromlist=["_"])
        except Exception:                           # pragma: no cover
            continue
        try:
            setattr(mod, "HF_HUB_OFFLINE", bool(on))
        except Exception:                           # pragma: no cover
            pass


def _load_hf(factory, what="模型", log=_noop, repo_id=None):
    """本地缓存命中 → 离线秒加载；否则探活 → 主端点重试 → 备用端点重试。

    factory 是延迟执行的加载函数，签名 factory(local_files_only: bool)，
    如 lambda local: SentenceTransformer(name, device=dev, local_files_only=local)。
    每次尝试前都调 `_switch_endpoint`，保证端点切换（含 URL 模板补丁）真正生效；
    全部失败才抛最后一个异常，由调用方决定是否降级跳过该路。

    重试次数沿用旧环境变量名：HF_DIRECT_RETRIES（主端点）/ HF_MIRROR_RETRIES（备用端点）。
    """
    # ⓪ 传进来的就是本地模型目录 → 直接加载，不探活不联网
    if repo_id and os.path.isdir(repo_id):
        t = time.time()
        log("%s：加载本地模型目录（不联网）| %s", what, repo_id)
        model = factory(True)
        log("%s 本地加载完成 | 用时=%.1fs", what, time.time() - t)
        return model
    # ① 本地已有完整权重 → 离线直加载（不探活、不校验、不重试）
    if repo_id and _is_model_cached(repo_id):
        t = time.time()
        log("%s：本地缓存命中，离线加载（跳过联网校验）", what)
        try:
            _set_offline(True)
            model = factory(True)
            log("%s 离线加载完成 | 用时=%.1fs", what, time.time() - t)
            return model
        except Exception as e:
            log("%s 离线加载失败（%s），回退联网加载", what, e)
        finally:
            _set_offline(False)
    # ② 联网加载
    direct = int(os.environ.get("HF_DIRECT_RETRIES", 2))
    fallback_n = int(os.environ.get("HF_MIRROR_RETRIES", 2))
    delay = float(os.environ.get("HF_RETRY_DELAY", 3))
    endpoints = [ep for ep in _ordered_endpoints() if ep]
    live = [ep for ep in endpoints if _probe_endpoint(ep)]
    if live:
        if live != endpoints:
            log("%s：跳过不可达端点 %s", what, "、".join(e for e in endpoints if e not in live))
        endpoints = live
    else:
        log("%s：端点探活均失败（%s），仍各试 1 次", what, "、".join(endpoints))
    last = None
    for idx, ep in enumerate(endpoints):
        n = direct if idx == 0 else fallback_n
        if not live:
            n = 1
        _switch_endpoint(ep)
        for i in range(1, n + 1):
            try:
                log("%s加载中…（%s 第%d/%d 次）", what, ep, i, n)
                return factory(False)
            except Exception as e:                  # pragma: no cover
                last = e
                log("%s加载失败（%s 第%d/%d 次）: %s", what, ep, i, n, e)
                if i < n:
                    time.sleep(delay)
    raise last if last is not None else RuntimeError(f"{what}加载失败")


def _split_text(text, size=400, overlap=80):
    """按段落聚合成 ~size 字的 chunk；超长段落带 overlap 硬切。"""
    text = text.replace("\r\n", "\n")
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(p) <= size:
            if len(buf) + len(p) + 1 <= size:
                buf = (buf + "\n" + p) if buf else p
            else:
                if buf:
                    chunks.append(buf)
                buf = p
        else:
            if buf:
                chunks.append(buf)
                buf = ""
            step = max(1, size - overlap)
            for i in range(0, len(p), step):
                seg = p[i:i + size]
                if not seg.strip():
                    continue
                if i > 0 and len(seg) <= overlap:
                    break          # 尾部重叠碎片，内容已被上一片覆盖，丢弃
                chunks.append(seg)
    if buf:
        chunks.append(buf)
    return chunks


# 名单式文档（家族成员、通讯录条目等）：「短姓名：」独占一行，后面跟
# 「电话：…」「亲情关系：…」这类字段行，条目之间空行分隔。按 400 字硬切会把
# 十几个成员塞进同一片，导致查某人电话时，该人的信息被其他同姓亲戚的
# 词频稀释（实测「查某成员的电话」时，答案片段只排到第 8）。
# 只在结构确实像名单时才按条目切，普通文档一律走 _split_text，零影响。
_ROSTER_HEAD = re.compile(r"^[\u4e00-\u9fff·]{2,6}[：:]\s*$")
_ROSTER_FIELD = re.compile(r"^[^\s：:]{2,10}[：:].*$")
# 「电话：」「住址：」这类空值字段行也长得像「姓名：」，必须排除，
# 否则会被当成新条目头，把上一个人的姓名标题切到前一个 chunk 里。
_ROSTER_NOT_HEAD = frozenset((
    "电话", "亲情网", "亲情关系", "住址", "地址", "姓名", "出生日期", "生日",
    "备注", "微信", "邮箱", "编号", "说明", "单位", "工作", "性别", "年龄",
))


def _split_roster(text, size=400, overlap=80, min_entries=3, min_cover=0.5):
    """名单式文档按条目（一人一片）切分；不像名单则返回 None 让调用方回退。

    判定三重保守：① 候选头 >= min_entries；② 每个头到下一个头之间至少有一行
    「字段：值」；③ 名单部分占全文 >= min_cover。这样 QA / 操作手册 / 普通
    表格不会被误判。超长条目按 size 再切，每片都保留姓名标题，避免丢主语。
    """
    lines = [l.strip() for l in text.replace("\r\n", "\n").split("\n")]

    def is_head(l):
        return (bool(_ROSTER_HEAD.match(l))
                and l.rstrip("：:").strip() not in _ROSTER_NOT_HEAD)

    cand = [i for i, l in enumerate(lines) if is_head(l)]
    if len(cand) < min_entries:
        return None
    heads = []
    for j, i in enumerate(cand):
        end = cand[j + 1] if j + 1 < len(cand) else len(lines)
        if any(_ROSTER_FIELD.match(l) for l in lines[i + 1:end]):
            heads.append(i)
    if len(heads) < min_entries:
        return None
    total = sum(len(l) for l in lines)
    covered = sum(len(l) for l in lines[heads[0]:])
    if not total or covered / total < min_cover:
        return None

    out = _split_text("\n".join(l for l in lines[:heads[0]] if l),
                      size, overlap) if heads[0] else []
    for j, i in enumerate(heads):
        end = heads[j + 1] if j + 1 < len(heads) else len(lines)
        body = [l for l in lines[i:end] if l]
        if not body:
            continue
        # 主语显式再写一次：名单里「亲情关系：成员甲…」会让几十个别人的条目
        # 都提到这个人，只出现一次姓名时答案片段会被这些短条目压过（实测
        # 「成员甲的电话是多少」答案 BM25 只排第 21）。写成「姓名：X」既是
        # 自然语言的条目标注，也让 LLM 明确这段属于谁、减少张冠李戴。
        subj = body[0].rstrip("：:").strip()
        title = body[0] + "\n姓名：" + subj
        body = [title] + body[1:]
        if len("\n".join(body)) <= size:
            out.append("\n".join(body))
            continue
        limit = max(40, size - len(title) - 1)
        buf = ""
        for p in body[1:]:
            if len(buf) + len(p) + 1 <= limit:
                buf = (buf + "\n" + p) if buf else p
            else:
                if buf:
                    out.append(title + "\n" + buf)
                buf = p
            while len(buf) > limit:
                out.append(title + "\n" + buf[:limit])
                buf = buf[limit:]
        if buf:
            out.append(title + "\n" + buf)
    return out


def _tokenize(s):
    """中文分词；无 jieba 时用「中文 2-gram + 英文/数字词」兜底。"""
    if jieba is not None:
        return [w for w in jieba.lcut(s)
                if w.strip() and not re.fullmatch(r"[\W_]+", w)]
    return re.findall(r"[\u4e00-\u9fff]{2}|[A-Za-z]+|\d+", s)


# ---------- 知识库图片 ----------
# 为什么要单独一套：markdown 里的图片语法 `![alt](images/x.png)` 原本只是给**人**看的，
# 检索器完全当空气——于是「导出报表怎么弄」这种问题，即使文档里就贴着操作截图，
# 模型也只能凭空描述。把图片变成一等公民（进索引、随 chunk 携带、可回传客户）要三件事：
#   1) 建索引时把图片语法换成内部占位符，让「图」与「图周围的文字」落在同一 chunk；
#   2) 给每张图额外生成一张「图片卡」chunk，用图注+章节做文本，图片问题才有召回入口；
#   3) 回答时把候选图编号喂给模型，让模型点名要哪张。

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")

# markdown 图片：![alt](path "可选title")；同时兼容 HTML <img src="...">
_MD_IMG_RE = re.compile(r'!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+"[^"]*")?\s*\)')
_HTML_IMG_RE = re.compile(r'<img\b[^>]*?\bsrc\s*=\s*["\']([^"\']+)["\'][^>]*>', re.I)
# 索引期的内部占位符。用 \x00 包起来：正常文档不会出现，切分与分词都不会把它拆开。
_IMG_TOK = "\x00IMG%d\x00"
_IMG_TOK_RE = re.compile(r"\x00IMG(\d+)\x00")


def resolve_image(src, doc_path, knowledge_dir):
    """把 markdown 里的图片地址解析成本地绝对路径；解析不了/不安全就返回 None。

    安全边界很重要：图片路径来自知识库 md（后台可上传），必须限制在 knowledge_dir 内，
    否则 `![](../../../../etc/passwd)` 这类写法会把服务器上的文件读出来发给客户。
    远程 URL 一律不支持（只发本地文件，下载别人的图再转发有合规风险）。
    """
    src = (src or "").strip()
    if not src or src.startswith(("http://", "https://", "data:")):
        return None
    base = os.path.dirname(os.path.abspath(doc_path))
    p = os.path.abspath(os.path.join(base, src))
    try:                            # realpath 消掉 ../ 与软链，堵住路径穿越
        rp = os.path.realpath(p)
        rk = os.path.realpath(knowledge_dir)
    except OSError:
        return None
    if not (rp == rk or rp.startswith(rk + os.sep)):
        return None
    if not os.path.isfile(rp):
        return None
    if os.path.splitext(rp)[1].lower() not in IMAGE_EXTS:
        return None
    return rp


def parse_doc_images(text, doc_path, knowledge_dir):
    """抽取文档里的图片，并把图片语法换成占位符。

    返回 (新文本, 图片列表)。图片列表每项：
        path 绝对路径 / rel 相对 knowledge_dir / alt 图注 / heading 所属小标题 /
        para 图片所在段落的文字（用来丰富图片卡的召回）

    顺带记下「图周围的段落原文」：图片卡只有一句 alt 时召回太窄，
    带上「点击右上角『导出』即可」这类上下文，匹配率高很多。
    """
    images = []
    index_by_path = {}

    def _add(src, alt):
        """登记一张图，返回它在 images 里的下标；解析不出就返回 None。

        ⚠️ 重复调用**必须返回同一个下标**而不是 None：这张函数会被调用两轮
        （先按行扫描收集「所属小标题/所在段落」，再统一替换语法），如果第二轮
        因为「已经登记过」而返回 None，替换就会退化成只留 alt 文字，
        图与周围文字的绑定关系整个丢掉 —— 表现为「图永远召不回来」且极难排查。
        """
        ap = resolve_image(src, doc_path, knowledge_dir)
        if not ap:
            return None
        alt = (alt or "").strip()
        if ap in index_by_path:            # 同一张图被引用两次：补 alt，不重复登记
            i = index_by_path[ap]
            if alt and not images[i]["alt"]:
                images[i]["alt"] = alt
            return i
        root = os.path.realpath(knowledge_dir)
        index_by_path[ap] = len(images)
        images.append({"path": ap, "rel": os.path.relpath(ap, root).replace(os.sep, "/"),
                       "alt": alt, "heading": "", "para": ""})
        return len(images) - 1

    # 先按行扫一遍：记下每张图所属的小标题 + 所在段落
    heading = ""
    lines = (text or "").split("\n")
    for i, line in enumerate(lines):
        if re.match(r"^\s{0,3}#{1,6}\s+", line):
            heading = re.sub(r"^\s{0,3}#{1,6}\s+", "", line).strip()
        for m in _MD_IMG_RE.finditer(line):
            idx = _add(m.group(2), m.group(1))
            # **只在首次登记时记小标题/段落**：同一张图常在多处被引用
            # （正文先引用、附录再回顾），若每次都覆盖，最终留下的是最后一次
            # 出现的章节，而正文那处才是客户问起时最该匹配的上下文。
            if idx is not None and not images[idx]["heading"] and not images[idx]["para"]:
                images[idx]["heading"] = heading
                for j in range(i - 1, max(-1, i - 6), -1):
                    cand = lines[j].strip()
                    if cand and not _MD_IMG_RE.search(cand) and not _HTML_IMG_RE.search(cand):
                        images[idx]["para"] = re.sub(r"^[#>\-\*\s]+", "", cand)[:160]
                        break

    def _sub_md(m):
        idx = _add(m.group(2), m.group(1))
        # 解析不出（远程/越界/文件不存在）→ 保留 alt 文字，至少不丢信息
        return ("\n" + (_IMG_TOK % idx) + "\n") if idx is not None else m.group(1).strip()

    def _sub_html(m):
        idx = _add(m.group(1), "")
        return ("\n" + (_IMG_TOK % idx) + "\n") if idx is not None else ""

    out = _HTML_IMG_RE.sub(_sub_html, _MD_IMG_RE.sub(_sub_md, text or ""))
    return out, images


def img_tokens(chunk):
    """取出一个 chunk 里的图片占位符编号（按出现顺序，去重）。"""
    seen, out = set(), []
    for x in _IMG_TOK_RE.findall(chunk or ""):
        i = int(x)
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def strip_img_tokens(chunk, images):
    """把占位符还原成人类可读的「【图示：图注】」，别把内部符号喂给模型。"""
    def _sub(m):
        i = int(m.group(1))
        alt = (images[i]["alt"] if 0 <= i < len(images) else "") or "示意图"
        return "【图示：%s】" % alt
    return _IMG_TOK_RE.sub(_sub, chunk or "")


def image_card_text(img, max_len=220):
    """一张图片的「检索卡」文本。

    只靠 alt 常常太短（「截图1」），召回率低。所以拼上：所属小标题 + 图注 +
    图周围那段话 + 文件名。目的只有一个——让「导出报表界面长什么样」这种
    **只有图、没有文字答案**的问题也能把图召回。
    """
    parts = []
    if img.get("heading"):
        parts.append(img["heading"])
    if img.get("alt"):
        parts.append(img["alt"])
    if img.get("para"):
        parts.append(img["para"])
    parts.append("（图示，文件 %s）" % os.path.basename(img.get("rel") or ""))
    return ("图示说明：" + "；".join(parts))[:max_len]

class HybridRetriever:
    """分片 + BM25 + 向量 + RRF 融合（可选 cross-encoder 重排）。"""

    def __init__(self, cfg, log=_noop, images_cfg=None, progress=None):
        self.cfg = cfg or {}
        self.log = log or _noop
        # 进度回调：progress(stage, pct, detail)
        # 后台「重建索引并重载」要能看到进度，否则点了没反应（模型加载+编码
        # 在 CPU 上要几十秒~几分钟）。不传就是 _noop，不影响现有调用方。
        self.progress = progress or _noop
        self.images_cfg = images_cfg          # 由 HybridRAGBrain 从 brain.images 传下来
        self.chunks = []
        self.meta = []
        # chunk 图片：self.chunk_imgs[i] = 第 i 个 chunk 关联的图片字典列表（可能为空）
        self.chunk_imgs = []
        self.chunk_is_card = []               # 该 chunk 是不是「图片卡」（整段只讲一张图）
        self.images = []                      # 全部去重后的图片字典（给后台统计用）
        self.bm25 = None
        self.emb = None
        self.embedder = None
        self.reranker = None
        self._rerank_lock = threading.Lock()        # 防预热线程与首个请求同时加载重排模型
        self._rerank_skip_logged = False
        self._load()

    # ---------- 索引构建 ----------
    def _iter_files(self, d):
        files = []
        if not os.path.isdir(d):
            return files
        for root, dirs, fs in os.walk(d):
            dirs[:] = [x for x in dirs if not x.startswith(".")]   # 跳过 .index
            for f in fs:
                if f.endswith((".md", ".txt")):
                    files.append(os.path.join(root, f))
        return sorted(files)

    @staticmethod
    def _doc_name(fp, root):
        """chunk 的来源标识 = 相对 data/knowledge/ 的路径。

        与后台 admin/kb.list_files() 的 `name` 口径完全一致，策略表存的就是这个字符串。
        根目录文档时等价于 basename；子目录文档形如 `sub/a.md`（旧版只存 basename 会串档）。
        """
        try:
            return os.path.relpath(fp, root).replace(os.sep, "/")
        except (ValueError, TypeError):
            return os.path.basename(fp)

    def _index_fingerprint(self, model_name):
        """向量索引指纹 = 真正参与编码的内容，不看文件 mtime / 绝对路径。

        旧实现把每个 md 和几百张图片的「路径 + mtime + size」全部计入 hash：
        复制图片（copy2 保留源 mtime）、工具刷新时间戳、甚至 knowledge_dir 从
        相对路径改成绝对路径，都会让指纹漂移 → 每次启动重编码（CPU 70+ 秒）。
        而 encode() 的输入只有 self.chunks（含图片卡文本），所以直接对 chunks
        内容做哈希：文档内容没变就命中缓存，改了才重建。
        """
        h = hashlib.md5()
        h.update(("model=%s\n" % str(model_name)).encode("utf-8", "ignore"))
        h.update(("chunk=%s/%s\n" % (self.cfg.get("chunk_size", 400),
                                      self.cfg.get("chunk_overlap", 80))).encode())
        # 切分策略也进指纹：只改 roster_chunk 开关、文档内容没变时也要重建向量
        h.update(("roster=%s\n" % bool(self.cfg.get("roster_chunk", True))).encode())
        for c in self.chunks:
            h.update(str(c).encode("utf-8", "ignore"))
            h.update(b"\x00")
        return h.hexdigest()[:12]

    @staticmethod
    def _prune_vector_cache(idx_dir, keep_path):
        """只保留当前指纹的缓存，避免文档每次更新都在 .index/ 里留下一个 emb_*.npy。"""
        keep = os.path.basename(keep_path)
        for fn in os.listdir(idx_dir):
            if fn.startswith("emb_") and fn.endswith(".npy") and fn != keep:
                try:
                    os.remove(os.path.join(idx_dir, fn))
                except OSError:
                    pass

    def _load(self):
        d = self.cfg.get("knowledge_dir", "./data/knowledge")
        files = self._iter_files(d)
        imgs_enabled = not self._img_cfg().get("disabled")
        size = int(self.cfg.get("chunk_size", 400))
        overlap = int(self.cfg.get("chunk_overlap", 80))
        roster = bool(self.cfg.get("roster_chunk", True))
        total_files = len(files)
        self.progress("读取文档", 0, "共 %d 个文档" % total_files)
        for fi, fp in enumerate(files):
            try:
                txt = open(fp, encoding="utf-8", errors="ignore").read()
            except OSError as e:
                self.log("读取知识库文件失败 %s: %s", fp, e)
                continue
            doc_imgs = []
            if imgs_enabled:
                try:
                    txt, doc_imgs = parse_doc_images(txt, fp, d)
                except Exception as e:
                    self.log("解析文档图片失败（按纯文本处理） %s: %s", fp, e)
            parts = _split_roster(txt, size, overlap) if roster else None
            if parts is None:
                parts = _split_text(txt, size, overlap)
            else:
                self.log("名单式文档按条目切分 | %s -> %d 片", self._doc_name(fp, d), len(parts))
            for c in parts:
                toks = img_tokens(c) if imgs_enabled else []
                if toks:
                    mine = [doc_imgs[i] for i in toks if 0 <= i < len(doc_imgs)]
                    # 占位符换成【图示：图注】，模型看到的是人话而不是内部符号
                    self.chunks.append(strip_img_tokens(c, doc_imgs))
                else:
                    mine = []
                    self.chunks.append(c)
                self.meta.append(self._doc_name(fp, d))
                self.chunk_imgs.append(mine)
                self.chunk_is_card.append(False)
            # 每张图再单独生成一张「图片卡」chunk：只有图没有文字答案的问题靠它召回
            if imgs_enabled:
                for im in doc_imgs:
                    self.chunks.append(image_card_text(im))
                    self.meta.append(self._doc_name(fp, d))
                    self.chunk_imgs.append([im])
                    self.chunk_is_card.append(True)
                    self.images.append(im)
            # 每个文档处理完报一次进度（阶段内 0~100，由调用方映射成全局百分比）
            self.progress("读取文档", int(100 * (fi + 1) / max(1, total_files)),
                          "%d/%d %s" % (fi + 1, total_files,
                                       os.path.basename(fp)))
        n = len(self.chunks)
        if n == 0:
            self.log("知识库为空：%s 下没有 .md/.txt", os.path.abspath(d))
            return
        if self.images:
            self.log("知识库图片 | 引用 %d 张（独立图片卡 %d 个），回答时可随文附图",
                     len(self.images), len(self.images))
        # ① BM25（IDF 恒正变体，见 _BM25PositiveIdf：小库负 IDF 会导致召回为空/排序反转）
        if _BM25PositiveIdf is not None:
            t = time.time()
            self.progress("BM25 索引", 0, "构建关键词索引（%d 片段）" % n)
            self.bm25 = _BM25PositiveIdf([_tokenize(c) for c in self.chunks])
            self.progress("BM25 索引", 100, "关键词索引就绪（%d 片段）" % n)
            self.log("BM25 索引就绪 | chunk=%d 用时=%.2fs | IDF 恒正", n, time.time() - t)
        else:
            self.log("未安装 rank_bm25，BM25 路跳过（pip install rank-bm25）")
        # ② 向量
        if self.cfg.get("use_vector", True) and SentenceTransformer is not None:
            self.progress("加载向量模型", 0, "载入 embedding 模型")
            self._build_vectors(d)
        elif SentenceTransformer is None:
            self.log("未安装 sentence-transformers，向量路跳过，仅 BM25 检索")

    def _img_cfg(self):
        """图片相关配置。优先用构造时传入的 brain.images，其次兼容 retrieval.images。"""
        return (self.images_cfg
                or (self.cfg.get("retrieval") or {}).get("images")
                or self.cfg.get("images")
                or {})

    def _device(self):
        dev = self.cfg.get("device") or ""
        if dev:
            return dev
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
            if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        return "cpu"

    def _build_vectors(self, d):
        model_name = self.cfg.get("embedding_model", "BAAI/bge-base-zh-v1.5")
        idx_dir = os.path.join(d, ".index")
        os.makedirs(idx_dir, exist_ok=True)
        # 指纹只看实际编码的 chunks 内容 + 模型名 + 切分参数；
        # 图片二进制、mtime、文件绝对路径都不参与，文档没更新就命中缓存。
        fp = self._index_fingerprint(model_name)
        vec_path = os.path.join(idx_dir, "emb_%s_%d.npy" % (fp, len(self.chunks)))
        local_dir = os.path.isdir(model_name)
        if local_dir:
            self.log("向量模型走本地目录（不联网）| %s", model_name)
        elif _is_model_cached(model_name):
            self.log("向量模型：本地缓存已就绪 → 离线加载（不校验更新，此为启动提速关键）| %s",
                     model_name)
        else:
            self.log("向量模型走在线仓库 | 端点=%s（HF_ENDPOINT 可覆盖；"
                     "无外网可先把模型下到本地再填 retrieval.embedding_model 为本地目录）",
                     os.environ.get("HF_ENDPOINT"))
        try:
            self.embedder = _load_hf(
                lambda local: SentenceTransformer(model_name, device=self._device(),
                                                  local_files_only=local),
                what="向量模型", log=self.log, repo_id=model_name)
        except Exception as e:
            self.log("向量模型加载失败（已重试主/备用端点，跳过向量路）: %s", e)
            self.embedder = None
            return
        if os.path.exists(vec_path):
            self.emb = np.load(vec_path)
            self.log("向量缓存命中 | %s shape=%s（文档内容未变化，跳过编码）",
                     vec_path, self.emb.shape)
            self._prune_vector_cache(idx_dir, vec_path)
            self.progress("向量索引", 100, "命中缓存，跳过编码")
            return
        t = time.time()
        self.log("首次构建向量索引 | chunk=%d model=%s device=%s（CPU 可能数分钟，仅一次）",
                 len(self.chunks), model_name, self._device())
        self.progress("向量索引", 0, "开始编码 %d 个片段（CPU 可能数分钟）" % len(self.chunks))
        # 分批编码：一次性 encode 内部是黑盒，跑完才知道结果，进度条会一直卡住。
        # 改成按批循环，每批结束报一次进度。已验证分批与一次性编码结果完全一致
        # （逐行归一化 + 无跨样本交互，数值差异 0）。
        #
        # 批大小取 32：实测（预热后）外层 64/32/16 的**总耗时基本相同**
        # （12.8s / 12.3s / 13.8s / 128 片段），但上报频率差很多——
        # 64 要 6 秒才动一次，看着像卡死；32 只要 3 秒一次。
        # 内部 batch_size 固定 16，保留 sentence-transformers 的按长度排序优化。
        bs = 32
        inner = 16
        parts = []
        total = len(self.chunks)
        for i in range(0, total, bs):
            part = self.chunks[i:i + bs]
            parts.append(self.embedder.encode(
                part, batch_size=inner, normalize_embeddings=True,
                convert_to_numpy=True, show_progress_bar=False))
            self.progress("向量索引", int(100 * min(i + bs, total) / max(1, total)),
                          "已编码 %d/%d 片段" % (min(i + bs, total), total))
        emb = np.vstack(parts) if parts else np.zeros((0, 0), dtype="float32")
        self.emb = np.asarray(emb, dtype="float32")
        try:
            np.save(vec_path, self.emb)
            self._prune_vector_cache(idx_dir, vec_path)
        except OSError as e:
            self.log("向量缓存写入失败: %s", e)
        self.log("向量索引完成 | shape=%s 用时=%.1fs（已缓存，下次秒起）",
                 self.emb.shape, time.time() - t)
        self.progress("向量索引", 100, "完成，%d 个片段" % len(self.chunks))

    # ---------- 检索 ----------
    @staticmethod
    def _rrf(rank_lists, k=60):
        """Reciprocal Rank Fusion：score = Σ 1/(k + rank)"""
        s = {}
        for rl in rank_lists:
            for r, idx in enumerate(rl):
                s[idx] = s.get(idx, 0.0) + 1.0 / (k + r + 1)
        return sorted(s.items(), key=lambda x: -x[1])

    def _rerank_enabled(self):
        """是否真的走重排（开关 + 依赖 + 知识库规模三道判断）。

        「库小跳过」这道的理由：cross-encoder 要逐对算，耗时随候选数线性涨
        （实测无 GPU 的机器上每候选 2~3 秒，16 个候选就是 30~50 秒）。而知识库只有
        一两百个片段时，BM25+向量经 RRF 融合的 top_k 已经足够准，再重排基本不改变结果，
        只是让客户白等半分钟——投入产出完全不成比例。
        rerank.min_chunks 设成非 0 后，片段数不足即自动跳过；想强制开启就设回 0。

        缺省口径（与 config.yaml.example 一致）：
          · enabled 缺省 False —— 整段 rerank 配置没写也一律不重排。
          · min_chunks 缺省 1000 —— 即便显式写了 enabled: true 而漏写 min_chunks，
            片段数不足 1000 也自动跳过；只有把 min_chunks 写成 0 才强制开启。
        """
        rc = self.cfg.get("rerank", {}) or {}
        # enabled 缺省 False：没写就是不开
        if not rc.get("enabled") or CrossEncoder is None:
            return False
        # min_chunks 缺省 1000：漏写也按 1000 走，只有显式写 0 才强制开启
        min_chunks = rc.get("min_chunks")
        min_chunks = 1000 if min_chunks is None else int(min_chunks)
        if min_chunks and len(self.chunks) < min_chunks:
            if not self._rerank_skip_logged:        # 只提示一次，避免每条消息刷日志
                self._rerank_skip_logged = True
                self.log("重排已跳过 | 知识库片段 %d < rerank.min_chunks=%d：小库经 RRF 融合已足够准，"
                         "重排只会让每条消息多等几十秒。（想强制开启请把 min_chunks 设为 0）",
                         len(self.chunks), min_chunks)
            return False
        return True

    def _ensure_reranker(self):
        """确保重排模型已加载，返回是否可用。

        这个模型原先是在「第一次检索」时才懒加载的——代价是**第一个客户要干等 20~30 秒**
        才收到回复。现在启动期由 warmup() 在后台线程提前调它，把这段等待挪出客户链路。
        加锁是必须的：预热线程与首条消息可能同时进来，不锁会重复加载两份模型（吃双倍内存）。
        """
        if self.reranker is not None:
            return True
        if not self._rerank_enabled():
            return False
        rc = self.cfg.get("rerank", {}) or {}
        with self._rerank_lock:
            if self.reranker is not None:           # 双重检查：等锁期间可能已被预热线程加载
                return True
            name = rc.get("model", "BAAI/bge-reranker-base")
            try:
                t = time.time()
                self.reranker = _load_hf(
                    lambda local: CrossEncoder(name, device=self._device(),
                                               local_files_only=local),
                    what="重排模型", log=self.log, repo_id=name)
                # 截断长度：chunk 只有 400 字、query 更短，512 截断长度下多出的计算纯属浪费
                ml = int(rc.get("max_length") or 0)
                if ml > 0:
                    self.reranker.max_length = ml
                self.log("重排模型就绪 | %s 用时=%.1fs（截断长度=%s）",
                         name, time.time() - t, getattr(self.reranker, "max_length", "默认"))
                return True
            except Exception as e:
                self.log("重排模型加载失败（已重试主/备用端点），本次跳过重排: %s", e)
                rc["enabled"] = False
                return False

    def warmup(self):
        """启动期预热：把「首个请求才做」的冷启动开销提前到启动期。

        两件事：
        ① 向量编码器首次推理——torch 第一次 encode 要建线程池/编译 kernel，实测比后续
           慢十几秒，纯属一次性开销；
        ② 重排模型懒加载（无 GPU 机器上 20~30 秒）。
        都挪到启动期的后台线程做，客户第一条消息就不用等这些。
        """
        if self.embedder is not None and np is not None:
            try:
                t = time.time()
                self.embedder.encode(["预热"], normalize_embeddings=True, convert_to_numpy=True)
                self.log("向量编码器预热完成 | 用时=%.1fs", time.time() - t)
            except Exception as e:
                self.log("向量编码器预热失败（不影响检索）: %s", e)
        if not self._rerank_enabled() or self.reranker is not None:
            return
        self.log("预热重排模型…（提前加载，避免第一个客户等待）")
        self._ensure_reranker()

    def _rerank(self, q, cand):
        if not self._rerank_enabled():
            return cand
        if not self._ensure_reranker():
            return cand
        pairs = [(q, self.chunks[i]) for i in cand]
        scores = self.reranker.predict(pairs)
        order = np.argsort(np.asarray(scores))[::-1]
        return [cand[int(i)] for i in order]

    def _bm25_contains(self, q_tokens, i):
        """chunk i 里是否真的出现了任一查询词。

        rank_bm25.BM25.doc_freqs[i] 是第 i 个 chunk 的「词 → 词频」字典。
        命中判定只认这个，不认分数符号：分数是打分管线的副产品，会随语料
        规模 / IDF 漂移；把它当召回开关，同样的故障换个规模就会再来一次。
        """
        bm = self.bm25
        if bm is None or not q_tokens or i < 0 or i >= len(bm.doc_freqs):
            return False
        freqs = bm.doc_freqs[i]
        return any(t in freqs for t in q_tokens)

    def retrieve(self, q, top_k=4, with_images=False, sources=None):
        """检索。默认返回纯文本列表（与老行为一致）；
        with_images=True 时返回 [{text, source, images:[{path,rel,alt}]}]。

        sources（新增，知识库策略用）：只允许这些文档参与检索。
          - None        = 全库检索（老行为，纯接口模式默认）
          - []          = 空策略，直接返回空（绝不退化成全库，避免越权读到别的库）
          - ["a.md",…]  = 只检索这些文档；兼容传 basename 与相对路径两种写法

        为什么不直接改返回类型：kb.py 的问答测试脚本等都直接吃
        `retrieve()` 的字符串列表，改类型会一次性打断多处调用点。宁可多一个开关。
        """
        n = len(self.chunks)
        if n == 0 or not q.strip():
            return []
        allow = None
        if sources is not None:
            allow = set()
            for s in sources or []:
                s = (s or "").strip().replace("\\", "/")
                if not s:
                    continue
                allow.add(s)
                allow.add(os.path.basename(s))
            if not allow:
                return []

        def _allowed(i):
            if allow is None:
                return True
            m = self.meta[i] if i < len(self.meta) else ""
            return m in allow or os.path.basename(m) in allow

        def _mask(scores):
            """在排序前把非白名单 chunk 的分数压到极小，避免 [:recall_k] 先切掉白名单。"""
            if allow is None:
                return scores
            bad = [i for i in range(n) if not _allowed(i)]
            if not bad:
                return scores
            if np is not None:
                arr = np.asarray(scores).copy()
                arr[bad] = -1e9
                return arr
            out = list(scores)
            for i in bad:
                out[i] = -1e9
            return out

        recall_k = min(int(self.cfg.get("recall_k", 20)), n)
        ranks = []
        # ① BM25：先用「真的含查询词 + 策略白名单」圈定候选，再在候选内按分数排序。
        # 分数只用来排序，不用来决定「能不能召回」（见 _bm25_contains）。
        # 不再做「分数全负就硬塞前 N 名」的兜底：IDF 全负时排序是反的，
        # 那样取到的恰好是最不相关的片段，比空上下文更糟。
        if self.bm25 is not None:
            q_tokens = _tokenize(q)
            sc = self.bm25.get_scores(q_tokens)
            cand = [i for i in range(n)
                    if _allowed(i) and self._bm25_contains(q_tokens, i)]
            cand.sort(key=lambda i: -float(sc[i]))
            hit = [int(i) for i in cand[:recall_k]]
            if hit:
                ranks.append(hit)
        # ② 向量
        if self.emb is not None and self.embedder is not None and np is not None:
            qv = self.embedder.encode([q], normalize_embeddings=True,
                                      convert_to_numpy=True)[0].astype("float32")
            sims = _mask(self.emb @ qv)
            order = np.argsort(sims)[::-1][:recall_k]
            thr = float(self.cfg.get("vector_min_score", 0.3))
            hit = [int(i) for i in order if sims[i] >= thr and _allowed(int(i))]
            if hit:
                ranks.append(hit)
        if not ranks:
            return []
        fused = self._rrf(ranks)
        # 重排候选数可配：候选越多越准但越慢（耗时近似线性），CPU 机器建议降到 8
        rc = self.cfg.get("rerank", {}) or {}
        n_cand = int(rc.get("candidates") or 0) or max(top_k * 4, top_k)
        cand = [i for i, _ in fused[:n_cand]]
        cand = self._rerank(q, cand)
        picked = cand[:top_k]
        if not with_images:
            return [self.chunks[i] for i in picked]
        out = []
        for i in picked:
            imgs = (self.chunk_imgs[i] if i < len(self.chunk_imgs) else None) or []
            # 只留还在磁盘上的图：后台删过图但没重建索引时，不该发出一个 404
            alive = [im for im in imgs if os.path.isfile(im.get("path") or "")]
            out.append({"text": self.chunks[i],
                        "source": self.meta[i] if i < len(self.meta) else "",
                        "images": alive,
                        "is_image_card": bool(self.chunk_is_card[i])
                        if i < len(self.chunk_is_card) else False})
        return out

    def describe(self):
        parts = [f"chunk={len(self.chunks)}"]
        parts.append("BM25=on" if self.bm25 is not None else "BM25=off")
        if self.emb is not None:
            parts.append(f"向量={self.cfg.get('embedding_model')}(dim={self.emb.shape[1]})")
        else:
            parts.append("向量=off")
        parts.append("图片=%d" % len(self.images) if self.images else "图片=0")
        rc = self.cfg.get("rerank", {}) or {}
        if self._rerank_enabled():
            parts.append("重排=on")
        elif rc.get("enabled"):
            # 用户显式开了重排，但被「库小」或「缺模型」挡下来了
            mc = rc.get("min_chunks")
            mc = 1000 if mc is None else int(mc)
            if mc and len(self.chunks) < mc:
                parts.append("重排=off(片段%d<%d)" % (len(self.chunks), mc))
            else:
                parts.append("重排=off(缺模型)")
        else:
            parts.append("重排=off")
        return "混合检索[" + " ".join(parts) + "]"


def _fmt_usage(u):
    """把 usage 拼成一行日志用的短文本。"""
    if not u:
        return "未返回"
    return "in=%s out=%s total=%s" % (
        u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens"))


class Brain:
    def answer(self, question, history=None, sources=None, system_prompt=None,
               system_prompt_extra=None):
        raise NotImplementedError


# —— 默认系统提示词 ——
# 客服场景的要害是「短、准、直接」：客户在微信里对话，长回答要翻好几屏，
# 反而找不到答案。实测（2026-09-26）旧的一句话提示词下，回答长度会跑到 1218 字，
# 所以这里把「多长、不要什么、怎么排版」都写成明确规则。
# 想改话术不必动代码：config.yaml 的 brain.system_prompt 会覆盖这里。
DEFAULT_SYSTEM_PROMPT = """你是客服助手，解答客户提出的各种问题。

回答规则（务必遵守）：
1. 只依据「知识库」回答。知识库里没有的，直接说不确定并建议转人工，不要自己编。
2. 简洁优先：直接给答案，一般不超过 120 字，能用一句话说清就不用两句。
3. 不复述用户的问题，不写"您好""根据知识库""希望对您有帮助"这类开场白和客套话。
4. 知识库仅供参考：只提取与问题直接相关的部分，不要整段照抄，无关内容一律不提。
5. 需要讲步骤时用「1. 2. 3.」分行列出，每行一句话，最多 5 步。
6. 用口语化中文，不要用 # * - 这类 markdown 符号（客户在微信里看到的是纯文本）。"""


# 模型在答案里点名要发哪张图用的标记。刻意用双层括号：客户话术里几乎不会出现，
# 即使漏解析也只是把 [[图1]] 留在正文里，不会误吞正常文字。
_IMG_MARK_RE = re.compile(r"\[\[\s*图\s*(\d{1,2})\s*\]\]|【\s*图\s*(\d{1,2})\s*】")


def extract_image_marks(text, valid_n=None, max_n=1):
    """从模型答案里取出图片标记。

    返回 (去掉标记后的正文, 编号列表)。四条护栏，缺一个都会出事：
    - 越界编号丢掉（模型偶尔会 hallucin 出 [[图9]]）；
    - 同一张图只发一次；
    - **超过 max_n 立刻截断**（客户在微信里连收 5 张图会烦，也会撞接口频控）；
    - 截断后多出来的标记**也要从正文抹掉** —— 否则客户会看到裸露的 [[图3]]。
    """
    valid = set(valid_n or [])
    out, seen = [], set()

    def _sub(m):
        n = int(m.group(1) or m.group(2))
        if valid and n not in valid:
            return ""
        if n in seen or len(out) >= max_n:
            return ""
        seen.add(n)
        out.append(n)
        return ""

    return _IMG_MARK_RE.sub(_sub, text or ""), out


class HybridRAGBrain(Brain):
    """外部 LLM（OpenAI 兼容）+ 混合检索 RAG。

    cfg 结构（config.yaml 的 brain 段）：
        brain:
          external_llm: {api_key, base_url, model, temperature}
          retrieval:    {knowledge_dir, chunk_size, top_k, embedding_model, rerank, ...}
    """

    def __init__(self, cfg, log=_noop, progress=None):
        self.cfg = cfg or {}
        self.log = log or _noop
        self.llm = self.cfg.get("external_llm", {}) or {}
        self.retrieval = self.cfg.get("retrieval", {}) or {}
        # 图片配置放在 brain 层级（brain.images），与 memory_limit / system_prompt 同级
        self.images_cfg = self.cfg.get("images", {}) or {}
        self.retriever = HybridRetriever(self.retrieval, log=self.log,
                                          images_cfg=self.images_cfg,
                                          progress=progress)
        # 最近一次 answer() 里的所有 LLM 调用用量（见 answer 里的说明）。
        # 初始化为空，这样即使 answer 从没被调过、app.py 也能安全地读它。
        self.last_attempts = []
        # 最近一次 answer() 判定要发给客户的图片（绝对路径列表）。
        # 走属性而不是改 answer() 的返回值：返回值被 answer() 自己的兜底逻辑、kb.py 的
        # 问答测试脚本、verify_*.py 多处依赖，改成 tuple 会全部打断。
        self.last_images = []
        # 本次生成「特别指定」的模型（None = 用管理端选定的默认模型）。
        # answer() / chat_only() 进出都会复位，避免上一次指定污染下一次。
        self.model_override = None
        # 本次生成的场景（group 群聊 / private 私聊 / None 不区分）。
        # 管理端可以给两个场景各选一个模型，解析优先级见 store.resolve_llm_config。
        self.scene = None

    def describe(self):
        return f"大脑=外部LLM({self.llm.get('model')}) {self.retriever.describe()}"

    def _resolve_llm(self, scene=None):
        """本次生成该用哪套大模型配置。

        优先级：**调用方显式指定的 model** > 该场景（群聊/私聊）选定的模型 >
        管理端选定的全局默认模型 > config.yaml 的 brain.external_llm（在调用方处理 None 时兜底）。
        放在每次生成时解析而不是 __init__ 时，是为了后台切换模型后
        **无需重启主服务**；brain_lock 保证同一进程内不会有两个 answer() 并发改 self.llm。
        """
        try:
            import store
            return store.resolve_llm_config(
                getattr(self, "model_override", None),
                scene if scene is not None else getattr(self, "scene", None))
        except Exception as e:
            self.log("读取默认大模型失败，回退 config.yaml | %s", repr(e)[:150])
            return None

    def warmup(self):
        """启动期预热（由 app.py 后台线程调用）：提前加载重排模型，客户首条消息不必等。"""
        self.retriever.warmup()

    # ---------- LLM 调用（优先流式，抗超时） ----------
    #
    # 为什么用流式：非流式请求的 timeout 是「整段生成」的墙钟上限，
    # 模型思考 + 长回答一旦超过就整体失败（客户只收到兜底话术）。
    # 流式下 read_timeout 只约束「相邻两个数据块之间的间隔」，
    # 只要 token 持续到达就不会超时，长回答也能稳定返回。
    def _endpoint(self):
        return self.llm["base_url"].rstrip("/") + "/chat/completions"

    def _headers(self):
        return {"Authorization": f"Bearer {self.llm['api_key']}",
                "Content-Type": "application/json"}

    def _payload(self, messages, stream):
        p = {
            "model": self.llm["model"],
            "temperature": self.llm.get("temperature", 0.3),
            "messages": messages,
        }
        mt = int(self.llm.get("max_tokens") or 0)
        if mt > 0:
            p["max_tokens"] = mt            # 限制生成长度，配合「防跑飞」总时长上限
        if stream:
            p["stream"] = True
            # 让上游在最后一个 chunk 里回 usage（prompt/completion tokens）。
            # **不加这个，流式请求永远拿不到 token 数** —— usage 只在流结束时随
            # 最后一个带 data: 的 chunk 一起返回，默认不发。
            # 有个开关可关：个别网关不认这个字段会报 400（此时退化为不统计 token）。
            if self.llm.get("stream_usage", True):
                p["stream_options"] = {"include_usage": True}
        return p

    def _pick_usage(self, obj):
        """从响应体里取 usage。兼容多种字段名（不同网关命名不一致）。

        OpenAI 规范是 usage.prompt_tokens / completion_tokens / total_tokens；
        另有网关用 input_tokens / output_tokens（DeepSeek/Anthropic 风格）。
        都取不到时返回 None —— 调用方按「未知」处理，绝不臆造 0
        （填 0 会被误读成「真的没消耗」，那比没有数据更糟）。
        """
        u = (obj or {}).get("usage")
        if not isinstance(u, dict):
            return None

        def pick(*names):
            for n in names:
                v = u.get(n)
                if isinstance(v, (int, float)) and v >= 0:
                    return int(v)
            return None

        pt = pick("prompt_tokens", "input_tokens", "prompt_eval_tokens")
        ct = pick("completion_tokens", "output_tokens", "eval_tokens")
        tt = pick("total_tokens")
        if tt is None and (pt is not None or ct is not None):
            tt = (pt or 0) + (ct or 0)
        if pt is None and ct is None and tt is None:
            return None
        # model 一并带出：管理端「AI 用量」按模型算费用 / 分组都靠它，
        # 上游响应里往往没有模型名，用本次实际请求的模型兜底。
        return {"model": (obj or {}).get("model") or self.llm.get("model"),
                "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": tt}

    def _timeouts(self):
        """(连接超时, 读间隔超时, 流式总时长上限)。"""
        return (float(self.llm.get("connect_timeout", 10)),
                float(self.llm.get("read_timeout", 60)),
                float(self.llm.get("total_timeout", 240)))

    @staticmethod
    def _pick_text(obj):
        """从一个 chunk 里取文本增量。

        兼容三种形态：标准流式 delta.content、非流式 message.content（网关忽略
        stream 参数时）、以及个别网关的 text 字段。
        """
        try:
            ch = (obj.get("choices") or [{}])[0]
        except Exception:
            return ""
        for path in (("delta", "content"), ("message", "content")):
            node = ch
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
                if node is None:
                    break
            if isinstance(node, str) and node:
                return node
        t = ch.get("text") if isinstance(ch, dict) else None
        return t if isinstance(t, str) else ""

    def _chat_stream(self, messages):
        """流式请求，边收边拼。

        返回 (已累积文本, 是否正常收完, usage)。中途断流但已有内容时不抛异常，
        靠 complete=False 让上层决定是直接用、还是重试；一个字都没收到才抛异常
        （交给非流式兜底）。

        usage 从流里抓：开了 include_usage 后，上游会把 usage 放在**最后一个**
        data chunk（可能紧跟 [DONE] 之前或之后）。所以这里不能 break 太早，
        见到 usage 就存下来继续读到 [DONE]。

        注意：正常读到 `[DONE]` 或上游干净关闭连接（部分网关不发 `[DONE]`）都算收完；
        只有**连接异常中断**才判定为未收完——真实断流是异常，而干净 EOF 通常只是
        上游不发 `[DONE]`，若一律判为未收完会导致对这类上游重复调用。
        """
        connect, read, total = self._timeouts()
        t0 = time.time()
        parts, nchunk, complete = [], 0, False
        usage = None
        r = requests.post(self._endpoint(), headers=self._headers(),
                          json=self._payload(messages, stream=True),
                          stream=True, timeout=(connect, read))
        try:
            if r.status_code != 200:
                # 错误体很小，读全便于定位（如 401/429/503）
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            for raw in r.iter_lines(decode_unicode=False):
                if not raw:
                    continue                       # SSE 空行分隔符
                line = raw.decode("utf-8", "ignore").strip()
                if not line or line.startswith(":"):
                    continue                       # 心跳注释行
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue                       # 网关夹带的非 JSON 行，忽略
                if isinstance(obj, dict) and obj.get("error"):
                    raise RuntimeError(f"流内错误: {str(obj['error'])[:200]}")
                # usage 只在最后一个 chunk 里出现，抓到就存（不 break，要继续读到 [DONE]）
                if isinstance(obj, dict) and obj.get("usage"):
                    u = self._pick_usage(obj)
                    if u:
                        usage = u
                piece = self._pick_text(obj)
                if piece:
                    parts.append(piece)
                    nchunk += 1
                if time.time() - t0 > total:
                    raise TimeoutError(f"流式总时长超 {total:.0f}s")
            complete = True
        except Exception:
            if not parts:
                raise                              # 没拿到任何内容 → 上层走兜底
            self.log("LLM 流式中断（已收 %d 段 / %d 字），保留已收内容",
                     nchunk, len("".join(parts)))
        finally:
            r.close()
        return "".join(parts), complete, usage

    def _chat_blocking(self, messages):
        """非流式兜底（老逻辑），仅在流式完全没拿到内容时用。返回 (文本, usage)。"""
        connect, read, _ = self._timeouts()
        r = requests.post(self._endpoint(), headers=self._headers(),
                          json=self._payload(messages, stream=False),
                          timeout=(connect, read))
        r.raise_for_status()
        d = r.json()
        return d["choices"][0]["message"]["content"], self._pick_usage(d)

    def _call_with_retry(self, fn, what):
        """LLM HTTP 请求失败时重试（`external_llm.retries`，默认再试 2 次）。

        只对「请求抛异常」重试（网络抖动 / 5xx / 超时）；上游返回空内容或流式
        中断仍由 _generate 的流式→非流式兜底处理，避免同一次问答重复计费。
        退避 1s、2s（最多 4s），重试次数夹在 0~5，防止配置写错把服务拖死。
        """
        try:
            retries = int(self.llm.get("retries", 2) or 0)
        except (TypeError, ValueError):
            retries = 2
        retries = min(5, max(0, retries))
        for i in range(retries + 1):
            try:
                return fn()
            except Exception as e:
                if i >= retries:
                    raise
                delay = min(2 ** i, 4)
                self.log("%s 第 %d 次失败，%.0fs 后重试（剩余 %d 次）| %s",
                         what, i + 1, delay, retries - i, repr(e)[:160])
                time.sleep(delay)

    def _img_cfg(self):
        """图片配置（brain.images），各项都有安全默认值。"""
        c = self.images_cfg or {}
        return {
            "enabled": c.get("enabled", True),           # 总开关；关掉则只走纯文本
            "max_images": max(0, int(c.get("max_images", 1) or 0)),   # 单次最多发几张
            "max_candidates": max(1, int(c.get("max_candidates", 4) or 4)),  # 最多给模型看几张
            "auto_fallback": bool(c.get("auto_fallback", False)),      # 模型没点名时是否兜底发 top1
            "min_score": float(c.get("min_score", 0) or 0),            # 图片卡最低分（0=不限）
        }

    def _pick_images(self, hits, cfg):
        """从检索命中里挑出候选图（按检索得分顺序去重）。

        刻意的取舍：同一张图只进候选一次 —— 一份操作说明里同一张截图常被引用多次，
        重复编号会让模型在「图1/图2」之间乱指。
        """
        cands, seen = [], set()
        for h in hits:
            for im in (h.get("images") or []):
                p = im.get("path")
                if not p or p in seen:
                    continue
                seen.add(p)
                cands.append(im)
                if len(cands) >= cfg["max_candidates"]:
                    return cands
        return cands

    @staticmethod
    def _image_block(cands):
        """给模型看的候选图清单。

        刻意写清「不写标记就只会发文字」，因为模型默认不会凭空加标记，
        漏了这条约束的表现是「图配好了但永远不发」。
        """
        lines = ["", "可选配图（下面每张图都有一个编号）："]
        for i, im in enumerate(cands, 1):
            alt = (im.get("alt") or "").strip()
            cap = alt or os.path.basename(im.get("rel") or "")
            lines.append("图%d：%s" % (i, cap[:40]))
        lines += [
            "",
            "如果问题需要看界面/操作位置，而你正好能用上上面某张图，"
            "就在答案里单独一行写 [[图1]]（多张就 [[图1]] [[图2]]），"
            "系统会把那张图作为图片消息发给客户；该标记不会显示在文字里。"
            "文字已经说清楚了、或图不相关时，就**不要**写标记。",
        ]
        return "\n".join(lines)

    def _finish(self, text, cands, cfg):
        """把答案里的图片标记摘出来，结果写到 self.last_images，返回纯文字。"""
        self.last_images = []
        if not cands or not text:
            return text
        n_max = cfg["max_images"]
        if n_max <= 0:
            return _IMG_MARK_RE.sub("", text or "")
        valid = range(1, len(cands) + 1)
        body, nums = extract_image_marks(text, valid_n=valid, max_n=n_max)
        if not nums and cfg["auto_fallback"]:
            # 模型没点名：只有「top1 命中就是这张图」时才兜底发，否则宁可不发
            #（发一张不相干的图比不发更糟，客户会以为那是答案）
            nums = [1]
            self.log("图片兜底 | 模型未点名，按 top1 命中发图1")
        picked = [cands[n - 1]["path"] for n in nums]
        self.last_images = picked
        if picked:
            self.log("回答附图 | %d 张：%s", len(picked),
                     "、".join(os.path.basename(p) for p in picked))
        body = re.sub(r"[ \t]+\n", "\n", body or "").strip()
        # 标记原本独占一行，去掉后可能留下空行/重复空行，压一下
        body = re.sub(r"\n{3,}", "\n\n", body)
        return body

    def answer(self, q, history=None, sources=None, system_prompt=None,
               system_prompt_extra=None, model=None, scene=None):
        """对外唯一入口：生成答案 → 摘出图片标记 → 返回 (纯文字)。

        model：可选，本次调用「特别指定」的大模型（名称 / 上游模型 ID）。
        不传时按 scene 走管理端「AI 大模型」页选定的场景模型 / 全局默认模型。

        scene：可选，group（群聊）/ private（私聊）。管理端给两个场景各选模型时，
        靠它决定本次用哪一个；不传则跳过分场景配置，直接用全局默认。

        sources：知识库文档名列表（策略过滤）。None=全库；[]=空策略不检索。
        system_prompt：本次请求专属提示词（群聊/私聊策略）；为空则沿用
        config.yaml 的 brain.system_prompt，再为空才用内置默认提示词。
        拆成两个方法的理由：_generate 里有三处兜底分支（流式/非流式/已收片段），
        每处都要走一遍「摘标记」，与其在三个 return 上各写一遍，
        不如让 _generate 只管返回原文，标记处理集中在这里做一次。
        """
        # ⚠️ 不能写成 str(model).strip() —— str(None) 是 "None"，
        # 会把「未指定」误判成「指定了一个叫 None 的模型」，默认模型永远不生效。
        self.model_override = (str(model or "").strip() or None)
        self.scene = str(scene or "").strip().lower() or None
        try:
            text, cands, icfg = self._generate(q, history=history, sources=sources,
                                               system_prompt=system_prompt,
                                               system_prompt_extra=system_prompt_extra,
                                               scene=self.scene)
        finally:
            # 无论成功失败都要清掉，否则下一次普通调用会误用上一次的指定模型 / 场景
            self.model_override = None
            self.scene = None
        try:
            return self._finish(text, cands, icfg)
        except Exception as e:
            # 标记处理出错绝不能连累文字：客户拿不到答案比多收到一个 [[图1]] 严重得多
            self.log("图片标记处理异常（已忽略，文字照发）: %s", e)
            self.last_images = []
            return text

    # ------------------------------------------------------------ 插件用：只调模型
    def chat_only(self, messages, model=None, temperature=None, max_tokens=None,
                  system_prompt=None, usage_source="plugin"):
        """只调大模型、**不做知识库检索**。

        给插件用的第二个入口（第一个是 answer / ask_brain）：插件要处理的是
        「从外面抓来的网页正文、卡片摘要」这类内容，跟知识库没关系。走 answer()
        会被套进「知识库：… / 用户问题：…」的模板，还要先做一次注定没用的检索，
        检索不到就是「（无相关知识库内容）」，白白占 token 还容易让模型跑偏。

        与 answer() 的关键区别：
          * messages 原样发给模型，不拼知识库模板；
          * **失败直接抛异常**，不返回「AI 服务暂时不可用」这种兜底话术。
            插件必须能区分「模型没答上来」和「模型答上来了」——
            静默兜底会让插件把失败当成正常结果缓存下来，之后的追问全是错的。

        model：可选，本次特别指定的大模型（名称 / 上游模型 ID），不传用管理端默认。
        temperature / max_tokens：可选，覆盖本次请求参数。
        system_prompt：可选，作为本次的第一条 system 消息（排在 messages 之前）。
        usage_source：用量记进 llm_usage 的哪个来源，默认 "plugin"。

        ⚠️ 必须持有 brain_lock 调用：self.llm 是全进程共享状态。
        """
        self.model_override = (str(model or "").strip() or None)
        # 插件直连固定走全局默认：插件调用没有「群聊/私聊」语境，别继承上一次场景
        self.scene = None
        prev_llm = self.llm
        llm, attempts = {}, []
        try:
            llm = dict(self._resolve_llm(None) or (self.cfg.get("external_llm") or {}))
            if not (llm.get("base_url") and llm.get("model")):
                raise RuntimeError("没有可用的内置大模型配置")
            if temperature is not None:
                llm["temperature"] = float(temperature)
            if max_tokens:
                llm["max_tokens"] = int(max_tokens)
            msgs = []
            sp = str(system_prompt or "").strip()
            if sp:
                msgs.append({"role": "system", "content": sp})
            for m in (messages or []):
                if not isinstance(m, dict):
                    continue
                role = str(m.get("role") or "").strip().lower()
                content = m.get("content")
                if role in ("system", "user", "assistant") and isinstance(content, str) \
                        and content.strip():
                    msgs.append({"role": role, "content": content})
            if not msgs:
                raise ValueError("chat_only 至少需要一条有效消息")
            # 借用既有的 _payload / _headers / _endpoint / _timeouts，
            # 不在插件侧再实现一套「几乎一样但参数会漂移」的 LLM 调用。
            self.llm = llm
            text = self._chat_only_run(msgs, attempts)
        finally:
            self.llm = prev_llm
            self.model_override = None
            self.scene = None
        # 插件的模型调用同样要计费、要进后台「AI 用量」页，不能只记客服链路。
        try:
            import store
            store.log_llm_usage(attempts, source=usage_source, llm_id=llm.get("id"))
        except Exception:
            pass
        return text

    def _chat_only_run(self, messages, attempts):
        """流式优先、非流式兜底；两者都拿不到内容就抛异常（不返回兜底话术）。"""
        partial, keep_min = "", int(self.llm.get("partial_min_chars", 20))
        if self.llm.get("stream", True):
            try:
                t = time.time()
                partial, done, u = self._call_with_retry(
                    lambda: self._chat_stream(messages), "LLM 流式调用")
                if u:
                    u.update(stream=True, latency_ms=int((time.time() - t) * 1000),
                             ok=bool(partial.strip()) and (done or len(partial) >= keep_min))
                    attempts.append(u)
                if partial.strip() and (done or len(partial) >= keep_min):
                    return partial
                self.log("chat_only 流式未拿到完整内容，转非流式重试")
            except Exception as e:
                self.log("chat_only 流式失败，转非流式重试 | %s", repr(e)[:200])
        t = time.time()
        text, u = self._call_with_retry(lambda: self._chat_blocking(messages),
                                        "LLM 非流式调用")
        if u:
            u.update(stream=False, latency_ms=int((time.time() - t) * 1000),
                     ok=bool(text.strip()))
            attempts.append(u)
        if text.strip():
            return text
        if partial.strip():
            # 两种模式都失败，但流式已收到可用内容 → 用它，优于直接报错
            return partial
        raise RuntimeError("内置大模型没有返回内容")

    def _generate(self, q, history=None, sources=None, system_prompt=None,
                  system_prompt_extra=None, scene=None):
        """只负责检索 + 生成原文。返回 (文本, 候选图列表, 图片配置)。

        scene：本次场景（group / private），用于选模型；None 表示不区分场景。
        """
        # 每次生成前重新解析生效模型：后台切换默认模型 / 场景模型立即生效。
        # 解析不到（没设默认 / DB 异常）就明确回退 config.yaml，不能残留上一次的默认模型。
        self.llm = self._resolve_llm(scene) or (self.cfg.get("external_llm") or {})
        # 本次实际用的模型写进日志：群聊/私聊可以各配一个模型，出问题时第一眼要能
        # 看出「这条回复是哪个模型答的」，否则只能靠猜。
        self.log("本次生成模型 | 场景=%s | 模型=%s | 纳管id=%s | 来源=%s",
                 {"group": "群聊", "private": "私聊"}.get(scene or "", scene or "不限"),
                 self.llm.get("model") or "-", self.llm.get("id") or "-",
                 self.llm.get("source") or "config.yaml")
        # 本次 answer() 里的**所有** LLM 调用记录（每次尝试一条），供 app.py 埋点。
        # 为什么要多条：流式失败会自动重试非流式，这两次调用**都真的花了钱**。
        # 只记最后一次的话，重试频繁时账目会明显偏低（漏掉流式那次的消耗）。
        # 每条形如 {prompt_tokens, completion_tokens, total_tokens, stream, latency_ms, ok}
        self.last_attempts = []
        self.last_images = []
        top_k = int(self.retrieval.get("top_k", 4))
        icfg = self._img_cfg()
        want_imgs = bool(icfg["enabled"]) and icfg["max_images"] > 0 \
            and bool(getattr(self.retriever, "images", None))
        try:
            hits = (self.retriever.retrieve(q, top_k=top_k, with_images=True, sources=sources)
                    if want_imgs else
                    self.retriever.retrieve(q, top_k=top_k, sources=sources))
        except Exception as e:
            self.log("检索异常（本次按无知识回答）: %s", e)
            hits = []
        if want_imgs:
            cands = self._pick_images(hits, icfg)
            ctx = "\n---\n".join(h["text"] for h in hits) or "（无相关知识库内容）"
            if cands:
                ctx += "\n" + self._image_block(cands)
        else:
            cands = []
            texts = [h["text"] if isinstance(h, dict) else h for h in hits]
            ctx = "\n---\n".join(texts) or "（无相关知识库内容）"
        # 提示词：config.brain.system_prompt 优先，留空则用内置的简洁版
        # 优先级：本次策略的提示词 > config.yaml 全局 > 内置默认
        system = ((system_prompt or "").strip() or
                  (self.cfg.get("system_prompt") or "").strip() or
                  DEFAULT_SYSTEM_PROMPT)
        # 追加型指令（如「需要人工时写 [转人工]」），不覆盖用户自定义提示词
        if (system_prompt_extra or "").strip():
            system += "\n\n" + system_prompt_extra.strip()
        user = f"知识库：\n{ctx}\n\n用户问题：{q}"
        # 多轮记忆：把历史对话（[{role, content}, ...]）拼到 system 之后、当前问题之前
        messages = [{"role": "system", "content": system}]
        if history:
            # 只保留合法的 user/assistant 角色，避免脏数据导致 API 报错
            for m in history:
                if isinstance(m, dict) and m.get("role") in ("user", "assistant") \
                        and isinstance(m.get("content"), str) and m["content"].strip():
                    messages.append({"role": m["role"], "content": m["content"]})
        messages.append({"role": "user", "content": user})
        # —— 发送日志：只打**汇总**（条数 + 角色序）——
        # 刻意不打历史明细：那是**客户对话原文**，每轮都打会把 app.log 灌满，
        # 而且排查时看汇总就够 —— "历史 0 条" 就能判断多轮记忆有没有生效。
        # 确实要看原文时用 verify_memory.py（一次性脚本，不在服务里跑）。
        hist_n = max(0, len(messages) - 2)   # 去掉 system + 当前 user
        self.log("LLM 上下文 | 共 %d 条消息（历史 %d 条）| 角色序=%s",
                 len(messages), hist_n, "→".join(m["role"] for m in messages))
        # ① 流式（默认）；② 流式没拿到或只拿到零头 → 非流式兜底；③ 都失败 → 兜底话术
        partial, keep_min = "", int(self.llm.get("partial_min_chars", 20))
        if self.llm.get("stream", True):
            try:
                t = time.time()
                partial, done, u = self._call_with_retry(
                    lambda: self._chat_stream(messages), "LLM 流式调用")
                if u:
                    u.update(stream=True, latency_ms=int((time.time() - t) * 1000),
                             ok=bool(partial.strip()) and (done or len(partial) >= keep_min))
                    # 上游没回模型名时用本次请求的模型兜底，保证「AI 用量」每行都有模型
                    u.setdefault("model", self.llm.get("model"))
                    self.last_attempts.append(u)
                if partial.strip() and (done or len(partial) >= keep_min):
                    self.log("LLM 流式返回 | 耗时=%.1fs 长度=%d 完整=%s%s",
                             time.time() - t, len(partial), done,
                             (" | token=%s" % _fmt_usage(u)) if u else " | token=未返回")
                    return partial, cands, icfg
                if partial.strip():
                    self.log("LLM 流式中断且仅收到 %d 字（< %d），转非流式重试",
                             len(partial), keep_min)
                else:
                    self.log("LLM 流式未收到任何内容（上游可能忽略 stream），转非流式重试")
            except Exception as e:
                # 错误细节只落日志，绝不回传给客户
                self.log("LLM 流式调用失败，转非流式重试 | %s | %s",
                         repr(e)[:200], traceback.format_exc())
        try:
            t = time.time()
            text, u = self._call_with_retry(
                lambda: self._chat_blocking(messages), "LLM 非流式调用")
            if u:
                u.update(stream=False, latency_ms=int((time.time() - t) * 1000),
                         ok=bool(text.strip()))
                u.setdefault("model", self.llm.get("model"))
                self.last_attempts.append(u)
            if text.strip():
                self.log("LLM 非流式返回 | 耗时=%.1fs 长度=%d%s", time.time() - t, len(text),
                         (" | token=%s" % _fmt_usage(u)) if u else " | token=未返回")
                return text, cands, icfg
            self.log("LLM 非流式返回为空（空回答）")
        except Exception:
            self.log("AI 调用异常（已向客户回通用兜底话术，不暴露错误细节）: %s",
                     traceback.format_exc())
        if partial.strip():
            # 两种模式都失败，但流式已收到可用内容 → 返回它，优于让客户收到兜底话术
            self.log("两种模式均失败，返回流式已收到的内容 | 长度=%d", len(partial))
            return partial, cands, icfg
        # 兜底话术：绝不能配图（此时没有任何有意义的检索依据）
        return "AI 服务暂时不可用，请稍后再试或转人工。", [], icfg
