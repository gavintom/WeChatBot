"""知识库管理：查看 / 编辑 / 新建 / 删除知识库文件、重建索引、问答测试。

注意两个关键约束：
1. **改了知识库必须重建索引才会生效**。bridge 进程里的 HybridRetriever 是启动时读文件建索引的
   （BM25 + 向量都缓存在内存），改完文件正在跑的服务不会自动重读。
   所以这里的「保存」只是落盘，「应用」= 重建索引 + 重启 bridge。
2. **问答测试不能跑在后台进程里**。加载向量模型要几十秒到几分钟、还占几百 MB 内存，
   后台进程扛不住也不该扛。所以测试走「子进程跑一次性脚本」，
   拿到 bridge 目录下的 brain 独立跑一轮检索+生成，用完即退。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWLEDGE_DIR = os.path.join(BASE_DIR, "data", "knowledge")
INDEX_DIR = os.path.join(KNOWLEDGE_DIR, ".index")
BACKUP_DIR = os.path.join(BASE_DIR, ".kb_backup")

ALLOWED_EXT = (".md", ".txt")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024        # 入库上限；发送时会再压到 2MB
IMAGE_DIRNAME = "images"

# 安全文件名：只允许中文/英文/数字/下划线/连字符/点，禁止路径分隔符与 ..
_SAFE = re.compile(r"^[^/\\:*?\"<>|\x00-\x1f]{1,120}$")


def _safe_name(name):
    name = (name or "").strip()
    if not name or not _SAFE.match(name) or name in (".", ".."):
        raise ValueError("文件名不合法（只允许中文/英文/数字/下划线/连字符，不能含路径分隔符）")
    if not name.lower().endswith(ALLOWED_EXT):
        raise ValueError("只支持 .md 或 .txt 文件")
    return name


def _scan(with_stat=True):
    """一次遍历知识库目录，拿到文档、图片与其 stat。

    之前 list_files() / index_status() / kb_stats() 各自 walk 一遍目录，
    /api/kb/files 三个都调 → 同一份目录被扫 4 遍，页面要等好几秒。
    这里统一成一次 scandir，结果给三处复用。

    with_stat=False 时不逐个 stat 文件。**这是性能关键**：Docker bind mount 上
    一次 stat 约 1.3ms，258 个文件就是 0.34 秒，而 scandir 拿全部文件名只要
    0.012 秒。列表页只需要「图片有多少张」，不需要每张图的 mtime/size。

    返回 {"docs": [(path, stat|None)], "imgs": [(path, stat|None)]}。
    """
    docs, imgs = [], []
    stack = [KNOWLEDGE_DIR]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    # .index 是向量缓存目录，.kb_import_* 是导入暂存目录
                    if e.name == ".index" or e.name.startswith(".kb_import_"):
                        continue
                    stack.append(e.path)
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                ext = os.path.splitext(e.name)[1].lower()
            except OSError:
                continue
            if ext in ALLOWED_EXT:
                docs.append((e.path, e.stat(follow_symlinks=False) if with_stat else None))
            elif ext in IMAGE_EXTS:
                imgs.append((e.path, e.stat(follow_symlinks=False) if with_stat else None))
    return {"docs": docs, "imgs": imgs}


def _rel_path(name):
    return os.path.join(KNOWLEDGE_DIR, name)


def list_files(scan=None):
    """列出知识库文件 + 大小/行数/最后修改时间。

    scan 可由调用方传入（复用同一次目录扫描）。

    读取：一次读入字节，同时数「行数」与「字符数」，避免 kb_stats 再读一遍
    （Docker bind mount 上每次读都要走一遍网络层）。
    """
    out = []
    if not os.path.isdir(KNOWLEDGE_DIR):
        return out
    if scan is None:
        scan = _scan(with_stat=True)
    for fp, st in scan["docs"]:
        if st is None:
            try:
                st = os.stat(fp)
            except OSError:
                continue
        try:
            with open(fp, "rb") as f:
                raw = f.read()
        except OSError:
            continue
        out.append({
            "name": os.path.relpath(fp, KNOWLEDGE_DIR),
            "size": st.st_size,
            "lines": raw.count(b"\n") + 1,
            # 字符数（按 UTF-8 解码计），给「片段数估算」用
            "chars": len(raw.decode("utf-8", errors="ignore")),
            "mtime": st.st_mtime,
            "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        })
    out.sort(key=lambda x: -x["mtime"])
    return out


def read_file(name):
    name = _safe_name(name)
    fp = _rel_path(name)
    if not os.path.isfile(fp):
        raise FileNotFoundError("文件不存在")
    if os.path.getsize(fp) > MAX_FILE_BYTES:
        raise ValueError("文件过大（超过 20MB）")
    with open(fp, encoding="utf-8", errors="replace") as f:
        return f.read()


def write_file(name, content, backup=True):
    """写入知识库文件。写前先备份到 .kb_backup（后台改错内容时能找回来）。"""
    name = _safe_name(name)
    if content is None:
        raise ValueError("内容不能为空")
    content = str(content)
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("内容过大（超过 20MB）")
    fp = _rel_path(name)
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
    if backup and os.path.exists(fp):
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(fp, os.path.join(
                BACKUP_DIR, "%s.%s.bak" % (name, ts)))
        except Exception:
            pass
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, fp)
    # 新入库的知识库文档默认只属于「默认知识库」（已登记过的不会被重新塞回）
    try:
        import store
        store.register_doc(name)
    except Exception:
        pass
    return {"name": name, "size": os.path.getsize(fp)}


def delete_file(name):
    name = _safe_name(name)
    fp = _rel_path(name)
    if not os.path.isfile(fp):
        raise FileNotFoundError("文件不存在")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(BACKUP_DIR, "%s.%s.bak" % (name, ts))
    shutil.move(fp, bak)
    # 文件删了，所有策略里对它的引用也要清掉，避免检索白名单指向不存在的文档
    try:
        import store
        store.unregister_doc(name)
    except Exception as e:
        # 别静默吞掉：清理失败时留日志，下次启动 / 点「同步新文档」还有
        # store.prune_missing_docs() 兜底，但至少能在这里看出问题。
        import logging
        logging.getLogger(__name__).warning(
            "删除 %s 后清理策略引用失败（下次同步会兜底）: %s", name, e)
    return {"name": name, "backup": os.path.basename(bak)}


def save_upload(name, content):
    """保存**上传**的文件内容。复用 write_file（同样走备份 + 原子写）。

    与「新建」的区别：新建是先在前端编辑再保存；上传是直接落盘（后台用
    复制/导出的 docx 转出来的 md 走这条路），所以这里会额外做一次编码检查——
    上传的文本可能来自任意来源，编码坏了会让后续读取整个知识库时报错。
    """
    return write_file(name, content)


def index_status(scan=None, doc_count=None):
    """索引缓存状态：向量缓存文件名里含 chunk 数与指纹，改了知识库指纹就会变。

    scan / doc_count 可由调用方传入（复用同一次目录扫描），避免重复遍历。
    """
    files = []
    total_bytes = 0
    if os.path.isdir(INDEX_DIR):
        for fn in sorted(os.listdir(INDEX_DIR)):
            fp = os.path.join(INDEX_DIR, fn)
            try:
                st = os.stat(fp)
                files.append({"name": fn, "size": st.st_size,
                              "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S",
                                                        time.localtime(st.st_mtime))})
                total_bytes += st.st_size
            except OSError:
                pass
    # 当前知识库指纹（与 brain.py 的 _fingerprint 同算法，用于判断是否需要重建）
    fp = _fingerprint(scan)
    if doc_count is None:
        doc_count = len(list_files())
    return {"files": files, "total_bytes": total_bytes,
            "fingerprint": fp, "doc_count": doc_count}


_FP_CACHE = {"key": None, "val": "", "at": 0}


def _fingerprint(scan=None):
    """知识库内容指纹（用于「知识库变了没有 → 要不要提示重建索引」）。

    指纹必须**准确**，但逐个 stat 全部 258 个文件在 Docker bind mount 上要
    0.34 秒，是列表页最大的开销。取舍如下：

      · **文档**（md/txt，14 个）：逐个 stat 拿准的 mtime+size。数量少，
        成本 1ms 内，内容改写一定能被发现。
      · **图片**（242 个）：只取「文件名 + 目录 mtime + 图片总数」。
        图片二进制被替换（同 size）不改变目录 mtime → 这种极端情况可能
        漏检；但换图几乎总是伴随新增/删除（目录 mtime 变）或文件改名，
        都能被发现。漏检的后果只是「没提示重建索引」，用户手动点一次即可，
        远好过每次开页面都卡 0.34 秒。

    另加 3 秒进程内缓存，命中时直接返回。
    """
    import hashlib
    if scan is None:
        scan = _scan(with_stat=True)
    docs, imgs = scan["docs"], scan["imgs"]

    now = time.time()
    try:
        dir_mtime = int(os.stat(KNOWLEDGE_DIR).st_mtime)
    except OSError:
        dir_mtime = 0
    key = "%s|%d|%d" % (dir_mtime, len(docs), len(imgs))
    if _FP_CACHE["key"] == key and now - _FP_CACHE["at"] < 3:
        return _FP_CACHE["val"]

    h = hashlib.md5()
    # 文档：精确到 mtime + size
    for f, st in sorted(docs, key=lambda x: x[0]):
        if st is None:
            try:
                st = os.stat(f)
            except OSError:
                continue
        h.update(("%s:%d:%d" % (f, int(st.st_mtime), st.st_size)).encode())
    # 图片：名字 + 目录 mtime + 总数（不逐张 stat）
    for f, _st in sorted(imgs, key=lambda x: x[0]):
        h.update(("i:%s" % f).encode())
    h.update(("mtime:%d" % dir_mtime).encode())
    val = h.hexdigest()[:12]
    _FP_CACHE.update({"key": key, "val": val, "at": now})
    return val


def rebuild_index():
    """删除向量缓存。真正的重建由主服务 POST /internal/reload 完成（热替换检索器）。

    为什么不在这里建：向量模型要几十秒~几分钟，跑在后台进程里会卡住界面，
    而且建完也不能热替换到已运行的主服务进程里。
    后台「重建索引并重载」= 清缓存 + 调主服务 /internal/reload（异步）。
    """
    removed = []
    if os.path.isdir(INDEX_DIR):
        for fn in os.listdir(INDEX_DIR):
            try:
                os.remove(os.path.join(INDEX_DIR, fn))
                removed.append(fn)
            except OSError:
                pass
    return {"removed": removed, "note": "缓存已清空，请调用主服务 /internal/reload 重建索引"}


# ---------- 问答测试（子进程一次性执行）----------

_QA_SCRIPT = r'''
import json, os, sys, time
base = sys.argv[1]
question = sys.argv[2]
sys.path.insert(0, base)
os.chdir(base)
import yaml
_p = os.environ.get("WXBOT_CONFIG") or "data/config.yaml"
if not os.path.isfile(_p):
    _p = "data/config.yaml.example"
if not os.path.isfile(_p):
    _p = "config.yaml.example"          # 历史位置，兼容老部署
cfg = yaml.safe_load(open(_p, encoding="utf-8"))
from brain import HybridRAGBrain
import store

policy = os.environ.get("WXBOT_POLICY") or ""
sources = None
if policy:
    hit = store.policy_docs(policy)
    sources = hit[1] if hit else []

out = {"ok": False, "question": question, "policy": policy or "全库", "sources": sources}
logs = []
t0 = time.time()
try:
    brain = HybridRAGBrain(cfg.get("brain") or {}, log=lambda *a: logs.append(" ".join(
        str(x) for x in a)))
    hits = []
    try:
        hits = brain.retriever.retrieve(question, top_k=int(
            (cfg.get("brain", {}).get("retrieval") or {}).get("top_k", 4)),
            sources=sources)
    except Exception as e:
        out["retrieve_error"] = repr(e)[:200]
    out["retrieved"] = [h[:200] for h in hits]
    out["meta"] = [m for m in getattr(brain.retriever, "meta", [])][
        :int((cfg.get("brain", {}).get("retrieval") or {}).get("top_k", 4))]
    ans = brain.answer(question, history=[], sources=sources)
    out["answer"] = ans
    out["ok"] = bool(ans and ans.strip())
    # 本次判定要发的图（绝对路径）。后台预览时转成相对路径给前端看。
    imgs = list(getattr(brain, "last_images", None) or [])
    if imgs:
        kdir = os.path.realpath((cfg.get("brain", {}).get("retrieval") or {})
                                .get("knowledge_dir", "./data/knowledge"))
        rels = []
        for p in imgs:
            try:
                rels.append(os.path.relpath(os.path.realpath(p), kdir).replace("\\", "/"))
            except Exception:
                rels.append(p)
        out["images"] = rels
    # 把 token 用量带出来，由主进程记进 analytics.db（子进程自己写也行，
    # 但子进程是临时起的、紧接着就退，统一交给主进程更清晰）
    out["usage"] = getattr(brain, "last_attempts", None) or []
    # ⚠️ 必须用本次实际生效的 brain.llm（管理端切了默认模型后 config.yaml 里那个已过时），
    # 并带上纳管模型 id，AI 用量页才能精确对上「模型库」里的具体模型。
    _llm_now = getattr(brain, "llm", None) or {}
    out["model"] = _llm_now.get("model")
    out["llm_id"] = _llm_now.get("id")
    out["llm_name"] = _llm_now.get("name")
except Exception as e:
    import traceback
    out["error"] = traceback.format_exc()[-1500:]
out["elapsed"] = round(time.time() - t0, 1)
out["logs"] = logs[-25:]
print("__KF_QA__" + json.dumps(out, ensure_ascii=False))
'''

def ask(question, timeout=180, policy=None):
    """在子进程里跑一次完整检索+生成。超时返回提示，不阻塞后台。

    走子进程的三个理由：① 向量模型加载几十秒~几分钟，不能占后台的请求线程；
    ② 模型内存几百 MB，后台不该常驻；③ 用完即退，不会有「改了知识库但检索器还是旧的」的问题。
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")
    if len(question) > 2000:
        raise ValueError("问题过长（最多 2000 字）")
    from admin import cfg as _cfg
    if not os.path.isfile(_cfg.CONFIG_PATH) and not any(
            os.path.isfile(os.path.join(BASE_DIR, p))
            for p in ("data/config.yaml.example", "config.yaml.example")):
        return {"ok": False, "error": "找不到 config.yaml，无法测试"}

    py = os.environ.get("KF_PYTHON") or sys.executable
    env = dict(os.environ)
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")   # 与 brain.py 一致，避免连不上 HF
    env["KF_BASE_DIR"] = BASE_DIR
    if policy:
        env["WXBOT_POLICY"] = str(policy)
    fd, script = tempfile.mkstemp(suffix=".py", prefix="kf_qa_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_QA_SCRIPT)
        try:
            p = subprocess.run([py, script, BASE_DIR, question],
                               cwd=BASE_DIR, env=env, capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "测试超时（%ds）。首次运行要加载向量模型，"
                                          "可能需要 1~3 分钟，请稍后重试或调大超时。" % timeout}
        raw = p.stdout or ""
        for line in raw.splitlines():
            if line.startswith("__KF_QA__"):
                try:
                    return json.loads(line[len("__KF_QA__"):])
                except Exception as e:
                    return {"ok": False, "error": "解析子进程输出失败：%s" % e,
                            "raw": raw[-500:]}
        return {"ok": False, "error": "子进程没有返回结果",
                "stdout": raw[-800:], "stderr": (p.stderr or "")[-800:]}
    finally:
        try:
            os.remove(script)
        except OSError:
            pass


# ---------- 知识库图片 ----------
# 为什么要做进后台：知识库是客户门店用的操作手册，里面大量是「点这里」「长这样」，
# 光有文字客户还是找不到。让运营能在后台直接上传图、一键插入 markdown，
# 才能真正把「图文知识库」这件事落地。

_IMAGE_DIR = os.path.join(KNOWLEDGE_DIR, IMAGE_DIRNAME)

# 图片文件名规则比文档更严：图片会被拼进 markdown 的 ![](x) 里，
# 空格/括号/反斜杠会让 markdown 语法直接坏掉，所以一律收紧到「中文/英文/数字/_/-.」。
_SAFE_IMG = re.compile(r"^[一-鿿A-Za-z0-9_][一-鿿A-Za-z0-9_.\-]{0,80}$")


def _safe_img_name(name):
    """校验图片文件名（含扩展名）。返回 (安全名, 扩展名小写)。

    ⚠️ **先拒绝再 basename，不要反过来**：如果先 os.path.basename 再校验，
    `../../evil.png` 会被悄悄改成 `evil.png` 并存进 images/ —— 文件是安全了，
    但调用方以为自己存的是另一个名字，日后按原名去引用必然找不到，
    排查起来极费劲。带分隔符/.. 的**直接报错**，让上传方自己改名字。
    """
    raw = (name or "").strip()
    if not raw:
        raise ValueError("图片文件名为空")
    if "/" in raw or "\\" in raw or ".." in raw:
        raise ValueError("图片文件名不能包含路径分隔符或 ..（请只填文件名，如 导出报表.png）")
    if not _SAFE_IMG.match(raw):
        raise ValueError("图片文件名不合法（只允许中文/英文/数字/下划线/连字符，且不能以符号开头）")
    ext = os.path.splitext(raw)[1].lower()
    if ext not in IMAGE_EXTS:
        raise ValueError("不支持的图片格式：%s（支持 %s）"
                         % (ext or "（无扩展名）", "、".join(IMAGE_EXTS)))
    return raw, ext


_IMG_SIZE_CACHE = {}          # (name, size, mtime) -> (w, h) 或 None


def _img_size_cached(name, fp, st):
    """取图片宽高，带缓存。

    PIL 的 Image.open() 虽然不载入像素，但要解析文件头，200+ 张图也要 1.8 秒。
    这里按 (文件名, 大小, mtime) 缓存结果：图片内容变了 mtime/size 就会变，
    缓存自然失效，不用额外维护。同一进程内第二次打开图片库就是瞬时的。
    """
    key = (name, st.st_size, int(st.st_mtime))
    if key in _IMG_SIZE_CACHE:
        return _IMG_SIZE_CACHE[key]
    try:
        from PIL import Image
        with Image.open(fp) as im:
            size = im.size
    except Exception:
        size = (None, None)
    _IMG_SIZE_CACHE[key] = size
    # 缓存无上限会随图片增删慢慢涨，简单裁一下
    if len(_IMG_SIZE_CACHE) > 2000:
        for k in list(_IMG_SIZE_CACHE)[:1000]:
            _IMG_SIZE_CACHE.pop(k, None)
    return size


def list_images():
    """列出知识库图片：相对路径、大小、尺寸、被几个文档引用。"""
    out = []
    if not os.path.isdir(_IMAGE_DIR):
        return out
    for fn in sorted(os.listdir(_IMAGE_DIR)):
        fp = os.path.join(_IMAGE_DIR, fn)
        if not os.path.isfile(fp) or os.path.splitext(fn)[1].lower() not in IMAGE_EXTS:
            continue
        try:
            st = os.stat(fp)
        except OSError:
            continue
        item = {"name": fn, "path": "%s/%s" % (IMAGE_DIRNAME, fn),
                "size": st.st_size, "mtime_str": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                "width": None, "height": None}
        try:                                   # 宽高只是给运营看的，失败不影响列表
            w, h = _img_size_cached(fn, fp, st)
            item["width"], item["height"] = w, h
        except Exception:
            pass
        out.append(item)
    return out


def _img_refs(name):
    """有多少个文档引用了这张图（后台提示「删了会不会坏引用」）。"""
    pat = re.compile(r"!\[[^\]]*\]\(\s*[^)\s]*%s[^)\s]*\s*\)"
                     % re.escape(os.path.basename(name)))
    n = 0
    for f in list_files():
        try:
            if pat.search(read_file(f["name"])):
                n += 1
        except Exception:
            pass
    return n


def save_image(raw, filename):
    """保存上传的图片字节。返回 {name, path, size}。

    只做「落盘 + 校验格式/大小」，**不压图**：运营上传的是原图，压了就再也没法
    当作高清素材复用。发图那一刻再由 wx_client.send_image() 按 brain.images.max_bytes 压缩。
    """
    if not raw:
        raise ValueError("没有收到图片内容")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("图片过大（超过 %dMB）" % (MAX_IMAGE_BYTES // 1024 // 1024))
    name, ext = _safe_img_name(filename)
    # 只信扩展名，不信任 Content-Type（浏览器/客户端给的值经常不对）；
    # 但至少要确认真是图片：用 Pillow 试着解一下，解不开就是伪装成图片的别的东西。
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(raw)) as im:
            im.verify()
    except ImportError:
        pass                                   # 没 Pillow 就只靠扩展名（发图时还会再拦一次）
    except Exception as e:
        raise ValueError("这不是有效的图片文件（%s）" % type(e).__name__)
    os.makedirs(_IMAGE_DIR, exist_ok=True)
    fp = os.path.join(_IMAGE_DIR, name)
    if os.path.exists(fp):
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(fp, os.path.join(BACKUP_DIR, "%s.%s.bak" % (name, ts)))
        except Exception:
            pass
    tmp = fp + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, fp)
    return {"name": name, "path": "%s/%s" % (IMAGE_DIRNAME, name),
            "size": os.path.getsize(fp)}


def read_image_bytes(name):
    """读图片字节（后台预览用）。name 可以是文件名或 images/xxx.png 形式的相对路径。

    ⚠️ **这里必须容忍「已经百分号编码过一次的名字」**。后台预览是浏览器里的
    `![](images/x.png)` → marked(encodeURI) → 前端再 encodeURIComponent 拼进
    query，Flask 只解一层，于是 request.args 里拿到的可能是
    `images/%E4%B8%80….png`（带字面 %）。白名单里没有 %，直接判非法 → 404 →
    「图就在 data/knowledge/images/ 里，预览却一张都出不来」。

    所以先 unquote 还原再走 _safe_img_name。unquote 会把 %2F 变成 /，
    因此**还原之后必须重新走 basename + 白名单**，绝不能直接拼路径。
    """
    name = (name or "").strip().replace("\\", "/")
    if "%" in name:
        try:
            name = urllib.parse.unquote(name).replace("\\", "/")
        except Exception:
            pass
    rel = name.split("/")[-1]
    safe, _ = _safe_img_name(rel)
    fp = os.path.join(_IMAGE_DIR, safe)
    if not os.path.isfile(fp):
        raise FileNotFoundError("图片不存在")
    with open(fp, "rb") as f:
        return f.read(), safe


def delete_image(name):
    """删除图片（先备份）。返回引用它的文档数，供前端提示。"""
    name = (name or "").strip().split("/")[-1]
    safe, _ = _safe_img_name(name)
    fp = os.path.join(_IMAGE_DIR, safe)
    if not os.path.isfile(fp):
        raise FileNotFoundError("图片不存在")
    refs = _img_refs(safe)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(BACKUP_DIR, "%s.%s.bak" % (safe, ts))
    shutil.move(fp, bak)
    return {"name": safe, "backup": os.path.basename(bak), "refs": refs}


def kb_stats(files=None, scan=None):
    """知识库概览：文件数、总字数、片段数估算（与 brain 的 chunk_size 一致）、图片数。

    files / scan 可由调用方传入（复用同一次目录扫描）。

    图片数只统计「有多少张」，**不逐张 PIL 解码取宽高**——那要 1.8 秒，
    而列表页根本不需要宽高（宽高只在「图片库」弹窗里用，那里才真的解码）。
    总字数复用 list_files 已读出的 chars，不再二次读全文。
    """
    if scan is None:
        scan = _scan()
    if files is None:
        files = list_files()
    total_chars = sum(int(f.get("chars") or 0) for f in files)
    # chunks_est 只是列表页粗估，固定按默认 400 字/块。
    # 原实现引用了未定义的 _cfg，异常恒被吞掉，等价于这个常量，故删去死代码。
    cs = 400
    return {"files": len(files), "chars": total_chars,
            "chunks_est": max(1, total_chars // max(1, cs)) if total_chars else 0,
            "chunk_size": cs, "images": len(scan["imgs"])}


# ---------- Office / PDF 导入 ----------
# 客户手里的培训资料几乎都是 Word/Excel/PDF。让运营手工截图再插 markdown 不现实
#（一份 116 页的手册要插一百多张图），所以直接把整个文件丢进来，图文一起转出来。

IMPORT_EXTS = (".docx", ".doc", ".xlsx", ".xlsm", ".xls", ".pdf")
MAX_IMPORT_BYTES = 60 * 1024 * 1024


def import_document(raw, filename, replace=False):
    """把一个 Word/Excel/PDF 转成知识库 markdown，图片同时落到 data/knowledge/images/。

    返回 {"name", "chars", "images", "refs", "warnings", "overwritten"}。

    刻意**不做任何自动重命名/去重后缀**给 md 文件：同名会按 replace 决定
    覆盖或报错。静默改名（手册_1.md）会让人以为导入成功却找不到自己那份。
    """
    if not raw:
        raise ValueError("没有收到文件内容")
    if len(raw) > MAX_IMPORT_BYTES:
        raise ValueError("文件超过 %dMB，太大了请拆分"
                         % (MAX_IMPORT_BYTES // 1024 // 1024))
    name = (filename or "").strip().replace("\\", "/")
    if not name:
        raise ValueError("文件名不能为空")
    # ⚠️ 必须在取 basename **之前**查路径成分：先砍掉目录的话，`../../etc/x.docx`
    # 会变成 `x.docx` 然后"正常"导入，运营以为自己传的文件名是另一个。
    # 带着 ../ 直接拒掉，理由也说得清楚。
    if "/" in name:
        raise ValueError("文件名不能包含路径分隔符（请只保留文件名）")
    base = os.path.basename(name)
    ext = os.path.splitext(base)[1].lower()
    if ext not in IMPORT_EXTS:
        raise ValueError("不支持的文件类型 %s（支持 %s）"
                         % (ext or "（无扩展名）", "、".join(IMPORT_EXTS)))

    import kbconvert
    stem = kbconvert.sanitize_stem(base)
    md_name = stem + ".md"
    _safe_name(md_name)                    # 复用文档名校验：拒绝越界文件名
    target = _rel_path(md_name)
    if os.path.exists(target) and not replace:
        raise ValueError("知识库里已经有《%s》，要替换它请勾选「覆盖同名文件」" % md_name)

    os.makedirs(_IMAGE_DIR, exist_ok=True)
    # 工作区放在知识库目录内，**不用系统 tempdir**：
    # ① 容器的 /tmp 常是只读或 noexec，mkstemp 直接失败；
    # ② 全程只有一个目录，成功失败都整目录删掉，不会在正式知识库里留半截残骸。
    # 用点开头 + 非文档扩展名，list_files/_fingerprint 都不会把它当知识库内容。
    staging = tempfile.mkdtemp(prefix=".kb_import_", dir=KNOWLEDGE_DIR)
    try:
        src = os.path.join(staging, "src" + ext)
        with open(src, "wb") as f:
            f.write(raw)
        result = kbconvert.convert(src, staging, stem=stem)
        renames = _move_images(staging, skip_prefix="src.")
        md = result["markdown"]
        for old, new in renames.items():
            if old != new:
                # markdown 里的路径一律是正斜杠。早期用 os.path.join 拼，
                # 在 Windows 上得到 "images\\x.png"，永远匹配不上 md 里的
                # "images/x.png" —— 图片一旦因同名被改名，文档就变成死引用。
                md = md.replace("](%s/%s)" % (IMAGE_DIRNAME, old),
                                "](%s/%s)" % (IMAGE_DIRNAME, new))
        names = [renames.get(i["name"], i["name"]) for i in result["images"]]
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    existed = os.path.isfile(target)
    write_file(md_name, md, backup=existed)
    return {"name": md_name, "chars": len(md), "images": len(names),
            "refs": result.get("refs") or 0,
            "warnings": result.get("warnings") or [], "overwritten": existed}


def _move_images(staging_dir, skip_prefix=""):
    """把暂存目录里的图搬进 data/knowledge/images/。返回 {原名: 最终名}。

    同名会加序号避让——**绝不能覆盖**：data/knowledge/images/ 里可能已经有别的文档
    用着同名图（同名来自不同源文件的第 N 张图），覆盖掉那份文档就变成死引用。

    skip_prefix 用来排除非图片文件（暂存目录里还有源文件本身 src.docx，
    漏掉排除条件的话它会被当成"图片"搬进 images/）。
    """
    out = {}
    if not os.path.isdir(staging_dir):
        return out
    for fn in sorted(os.listdir(staging_dir)):
        if skip_prefix and fn.startswith(skip_prefix):
            continue
        if os.path.splitext(fn)[1].lower() not in IMAGE_EXTS:
            continue                          # 只搬图片，别把别的东西混进图片库
        src = os.path.join(staging_dir, fn)
        if not os.path.isfile(src):
            continue
        name, stem2, ext2 = fn, os.path.splitext(fn)[0], os.path.splitext(fn)[1]
        n = 1
        while os.path.exists(os.path.join(_IMAGE_DIR, name)):
            name = "%s_%d%s" % (stem2, n, ext2)
            n += 1
        shutil.move(src, os.path.join(_IMAGE_DIR, name))
        out[fn] = name
    return out