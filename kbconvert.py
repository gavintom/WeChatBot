"""Office/PDF → 知识库 Markdown（**含内嵌图片自动提取**）。

为什么要有这个模块
------------------
门店的培训资料（操作手册、FAQ、业务字典）几乎都是 Word / Excel / PDF，
而检索器只吃 markdown。让运营手工截图、手工插 `![](images/x.png)` 不可持续——
一份 80 页的手册要插几百张图。所以这里做的是**一次导入、图文全出来**。

三种格式的取图策略完全不同，各有坑：

- **Word**：图片是 `word/media/` 里的独立文件，位置信息在段落 XML 的 `r:embed` 上。
  必须**按文档流顺序**遍历 body，才能知道图该插在哪段话后面。直接遍历
  `word/media` 会得到一坨无序图，只能全堆到文末——那样图与"点右上角导出"
  这类文字就分开了，检索时图召不回来。
- **Excel**：图在 `xl/media/`，锚点信息在 drawing XML 里。openpyxl 给了
  `ws._images` 与 `anchor._from.row`，可以插到对应工作表、对应行附近。
- **PDF**：最麻烦。① 图片可能是 JPX/JBIG2 等浏览器不认的编码，直接抽原字节
  发出去客户根本看不到 → **一律按区域重新渲染成 PNG/JPEG**；
  ② 扫描件整页没有文字层，纯抽图会得到一堆碎片 → **整页渲染**；
  ③ 文字与图要按 y 坐标交错排序，否则图文顺序全乱。

依赖缺失一律**明确报错**（告诉运营装哪个包），绝不静默丢图——
「导入了但少了图」比「导入失败」糟糕得多，运营根本不会发现。
"""
import hashlib
import importlib.util
import io
import os
import re

# ---------- 格式支持与体积上限 ----------
# .doc/.xls 在 convert_file() 入口就被明确拒绝（需另存为新格式），
# 因此不列进可转换清单，报错文案里的「支持格式」才不会自相矛盾。
CONVERTIBLE = (".docx", ".xlsx", ".xlsm", ".pdf")
WORD_EXT = (".docx",)
EXCEL_EXT = (".xlsx", ".xlsm")
PDF_EXT = (".pdf",)

MAX_SOURCE_BYTES = 60 * 1024 * 1024     # 单个源文件上限（后台也会再拦一层）
MAX_IMAGES = 300                         # 单个文档最多抽多少张图
MAX_PDF_PAGES = 500                      # 超长 PDF 直接拒绝，避免后台卡死
MIN_IMG_AREA_RATIO = 0.012                # 小于页面积 1.2% 的「图」多半是分隔线/装饰
BIG_IMG_AREA_RATIO = 0.55                 # 占页 55% 以上视为整页大图
SCANNED_TEXT_CHARS = 40                   # 文字少于这个字数且有大图 → 当扫描页整页渲染
PDF_RENDER_DPI = 150                      # 截图类 PDF 150dpi 足够看清界面文字

_PKG_HINT = {
    ".docx": "python-docx",
    ".xlsx": "openpyxl", ".xlsm": "openpyxl",
    ".pdf": "pymupdf",
}

# 图片文件名：与 admin/kb.py 的 _SAFE_IMG 保持一致
_UNSAFE_CHARS = re.compile(r"[^\w一-鿿.\-]", re.UNICODE)


class ConvertError(Exception):
    """转换失败。message 直接展示给运营，所以要写清楚「缺什么、怎么办」。"""


def _need(ext):
    """确保依赖可用，否则抛带 pip 命令的明确错误。"""
    mod = "fitz" if ext in PDF_EXT else ("docx" if ext in WORD_EXT else "openpyxl")
    try:
        found = importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        found = False
    if not found:
        pkg = _PKG_HINT.get(ext, "相关依赖")
        raise ConvertError("缺少依赖 %s，无法转换 %s 文件。请在服务器上执行："
                           "pip install %s" % (pkg, ext.lstrip("."), pkg))


def sanitize_stem(name):
    """把任意文件名压成能安全当知识库文件名的 stem（去路径、去非法字符、去后缀）。"""
    stem = os.path.basename(name or "").strip()
    stem = os.path.splitext(stem)[0]
    stem = _UNSAFE_CHARS.sub("_", stem).strip("._-")
    if not stem:
        stem = "imported"
    return stem[:80]


def _safe_img_name(stem, idx, ext, used, image_dir):
    """生成不冲突的图片文件名：`<stem>_<序号>.<ext>`。

    used 是本批次已占用的名字集合。为什么要去重而不是直接写：同名文件**直接覆盖**
    会静默毁掉前一个文档的配图——而且两份文档看起来都"导入成功了"。
    """
    ext = (ext or ".png").lower()
    if ext == ".jpeg":
        ext = ".jpg"
    if ext not in (".jpg", ".png", ".gif", ".bmp", ".webp"):
        ext = ".png"
    name = "%s_%02d%s" % (stem, idx, ext)
    n = 1
    while name in used or os.path.exists(os.path.join(image_dir, name)):
        n += 1
        name = "%s_%02d_%d%s" % (stem, idx, n, ext)
    used.add(name)
    return name


class _ImageWriter:
    """把抽出来的图片写进 data/knowledge/images/，顺手做格式与体积兜底。"""

    def __init__(self, image_dir, stem, max_bytes=10 * 1024 * 1024):
        self.dir = image_dir
        self.stem = stem
        self.max_bytes = max_bytes
        self.used = set()
        self.saved = []          # [{"name","rel","alt","bytes"}]——按图片文件去重
        self.refs = 0            # markdown 里的引用次数（含同一张图被引用多次）
        self.skipped = 0
        self._by_hash = {}
        os.makedirs(self.dir, exist_ok=True)

    def add(self, raw, ext, alt, cap=MAX_IMAGES):
        """记一次图片引用，返回可供 markdown 用的相对路径。

        **同一张图只落一个文件**（按内容哈希判定），但 markdown 里每次引用都写出来。
        手册里同一张截图常被多个步骤复用——按引用次数存会白占几倍磁盘，
        而且会在图片库里堆出好几条几乎一样的「图片卡」，白白拉低检索精度。

        为什么按内容哈希而不是按关系 id：不同 rId 常常就是同一份字节
        （Word 复制粘贴图片会复用 media 条目），哈希能跨引用去重。
        """
        if not raw:
            return None
        self.refs += 1
        h = hashlib.md5(raw).hexdigest()
        if h in self._by_hash:
            return self._by_hash[h]["rel"]
        if len(self.saved) >= cap:
            self.skipped += 1
            return None
        name = _safe_img_name(self.stem, len(self.saved) + 1, ext,
                              self.used, self.dir)
        data = raw
        # 浏览器与 Pillow 解不开的格式一律转 PNG（gif/bmp/webp 转了更安全）
        if ext not in (".jpg", ".png") or not _looks_decodable(raw):
            data = _to_png(raw)
            name = os.path.splitext(name)[0] + ".png"
            ext = ".png"
        if len(data) > self.max_bytes:
            data = _to_jpeg_thumb(raw)
            name = os.path.splitext(name)[0] + ".jpg"
            ext = ".jpg"
        fp = os.path.join(self.dir, name)
        tmp = fp + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, fp)
        rec = {"name": name, "rel": "images/" + name,
               "alt": (alt or "").strip() or "示意图", "bytes": len(data)}
        self.saved.append(rec)
        self._by_hash[h] = rec
        return rec["rel"]


def _looks_decodable(raw):
    """Pillow 能不能解开。解不开的（如某些 CMYK JPX）直接发给客户是打不开的。"""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            im.verify()
        return True
    except Exception:
        return False


def _to_png(raw):
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as im:
        if im.mode not in ("RGB", "RGBA", "L", "P"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def _to_jpeg_thumb(raw, max_side=2000):
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=82, optimize=True)
        return buf.getvalue()


def _md_escape(text):
    """表格单元格里不能出现裸的 |，否则表格结构会被切断。"""
    return re.sub(r"\s+", " ", (text or "")).replace("|", r"\|").strip()


def _table_md(rows):
    """二维数组 → markdown 表格。首行当表头。空表返回空串。"""
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    out = []
    head = (rows[0] + [""] * ncol)[:ncol]
    out.append("| " + " | ".join(_md_escape(c) or " " for c in head) + " |")
    out.append("| " + " | ".join(["---"] * ncol) + " |")
    for r in rows[1:]:
        rr = (r + [""] * ncol)[:ncol]
        out.append("| " + " | ".join(_md_escape(c) or " " for c in rr) + " |")
    return "\n".join(out)


def _tidy(md):
    md = re.sub(r"\n{3,}", "\n\n", md or "")
    return md.strip() + "\n"


# ==================== Word ====================

def _docx_para_text(obj):
    """提取元素纯文本。**段落与表格单元格都要能用**。

    单元格在 python-docx 里是 `_Cell`，底层节点叫 `_tc` 不是 `_p`；
    只认 `_p` 的话一碰到表格就 AttributeError（Word 手册里表格很常见）。
    """
    from docx.oxml.ns import qn
    el = getattr(obj, "_p", None)
    if el is None:
        el = getattr(obj, "_tc", None)          # lxml 元素不能直接用真值判断
    if el is None:
        return ""
    return "".join(node.text or "" for node in el.iter() if node.tag == qn("w:t"))


def _docx_heading_level(para):
    """Word 标题层级。样式名与 outlineLvl 两条路都认（很多文档是自定义样式）。"""
    from docx.oxml.ns import qn
    style = getattr(para, "style", None)
    name = (style.name or "") if style is not None else ""
    m = re.search(r"(?:Heading|标题)\s*(\d)", name)
    if m:
        return int(m.group(1))
    pPr = para._p.find(qn("w:pPr"))
    if pPr is not None:
        ol = pPr.find(qn("w:outlineLvl"))
        if ol is not None:
            try:
                return int(ol.get(qn("w:val"))) + 1
            except (TypeError, ValueError):
                return 0
    return 0


def _docx_num_level(para):
    """列表层级；不是列表返回 None。

    ⚠️ Word 的项目符号有两种存法：①段落自己带 `w:numPr`；②用「List Bullet」
    这类**样式**（符号定义在样式里，段落上没有 numPr）。只认 ① 的话，
    python-docx 默认模板生成的项目符号列表会全部退化成普通段落——
    手册里的「常见问题」清单就变成一坨没有层级的文字，检索时和标题混在一起。
    """
    from docx.oxml.ns import qn
    pPr = para._p.find(qn("w:pPr"))
    if pPr is not None:
        numPr = pPr.find(qn("w:numPr"))
        if numPr is not None:
            ilvl = numPr.find(qn("w:ilvl"))
            try:
                return int(ilvl.get(qn("w:val")) if ilvl is not None else 0)
            except (TypeError, ValueError):
                return 0
    style = getattr(para, "style", None)
    name = (style.name or "").lower() if style is not None else ""
    if re.search(r"^(list|列表)\s*(bullet|项目符号|编号|数字)", name) \
            or re.search(r"list\s*(paragraph|continue)", name):
        return 0
    return None


def _docx_para_images(para, doc):
    """取这个段落里的图片字节列表（DrawingML 与老式 VML 都认）。

    VML 是 Word 2007 之前的老写法（`w:pict/v:imagedata`），现在仍能在客户
    大量使用的旧文档里遇到，只认 DrawingML 会漏图。
    """
    R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    rids = []
    for blip in para._p.iter(
            "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"):
        rid = blip.get(R + "embed")
        if rid:
            rids.append(rid)
    for imgdata in para._p.iter(
            "{urn:schemas-microsoft-com:vml}imagedata"):
        rid = imgdata.get(R + "id")
        if rid:
            rids.append(rid)
    out = []
    for rid in rids:
        try:
            part = doc.part.related_parts[rid]
            out.append((part.blob, os.path.splitext(str(part.partname))[1]))
        except Exception:
            continue                              # 关系断了：跳过，不中断整篇
    return out


def _docx_alt(para, default):
    """图注优先取 Word 自带的「替换文字」，没有就用邻近文字兜底。"""
    for docPr in para._p.iter(
            "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}docPr"):
        d = (docPr.get("descr") or docPr.get("title") or "").strip()
        if d:
            return d
    return default


def convert_docx(path, writer):
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        doc = Document(path)
    except Exception as e:
        raise ConvertError("Word 文件打不开（可能不是真正的 .docx，或已损坏）：%s" % e)

    lines = []
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag
        if tag == qn("w:p"):
            p = Paragraph(child, doc)
            txt = _docx_para_text(p).strip()
            lvl = _docx_heading_level(p)
            num = _docx_num_level(p)
            # 先写文字、**再**写图：Word 里操作说明的图几乎都紧跟在步骤文字下方。
            # 顺序反了不要紧（同一片里），但文字在前更符合阅读顺序，
            # 也让「点右上角导出」这句话紧贴截图，分片时不会把它们切开。
            if lvl:
                if txt:
                    lines.append("%s %s" % ("#" * min(lvl, 6), txt))
                    lines.append("")
            elif num is not None:
                lines.append("%s- %s" % ("  " * num, txt))
            elif txt:
                lines.append(txt)
                lines.append("")
            for blob, ext in _docx_para_images(p, doc):
                rel = writer.add(blob, ext, _docx_alt(p, txt[:60] or "操作示意图"))
                if rel:
                    lines.append("")
                    lines.append("![%s](%s)" % (
                        (txt[:60] or os.path.splitext(rel)[0]), rel))
                    lines.append("")
        elif tag == qn("w:tbl"):
            t = Table(child, doc)
            rows = []
            cell_imgs = []
            for r in t.rows:
                cells = []
                for c in r.cells:
                    cells.append(_md_escape(_docx_para_text(c)) or " ")
                    for para in c.paragraphs:
                        cell_imgs += _docx_para_images(para, doc)
                rows.append(cells)
            tbl = _table_md(rows)
            if tbl:
                lines.append("")
                lines.append(tbl)
                lines.append("")
            # 表内图片 Word 会重复引用同一张（每行各一次），这里只取前几张且去重
            seen = set()
            for blob, ext in cell_imgs:
                h = hash(blob)
                if h in seen:
                    continue
                seen.add(h)
                rel = writer.add(blob, ext, "表格配图")
                if rel:
                    lines.append("![表格配图](%s)" % rel)
                    lines.append("")
    if not any(l.strip() for l in lines):
        raise ConvertError("这个 Word 文件里没有读到任何文字（可能是纯图片扫描件，"
                           "请改用 PDF 或截图上传）")
    return _tidy("\n".join(lines))


# ==================== Excel ====================

def convert_xlsx(path, writer):
    import openpyxl
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as e:
        raise ConvertError("Excel 文件打不开：%s" % e)

    lines = []
    for ws in wb.worksheets:
        lines.append("## 工作表：%s" % ws.title)
        lines.append("")
        rows = []
        # 行号 → 该行的图片，统一收到表格之后再输出
        row_imgs = {}
        sheet_imgs = []
        try:
            for im in getattr(ws, "_images", []) or []:
                try:
                    blob = im._data()
                except Exception:
                    blob = None
                if not blob:
                    continue
                r = getattr(getattr(im, "anchor", None), "_from", None)
                row = getattr(r, "row", None)
                row_imgs.setdefault(row, []).append(
                    (blob, os.path.splitext(str(getattr(im, "ref", "") or ""))[1] or ".png"))
        except Exception:
            pass                                   # 图片读不到不该让整篇失败
        for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
            cells = [_md_escape("" if v is None else str(v)) or " " for v in row]
            if not any((c or " ").strip() for c in cells):
                continue
            rows.append(cells)
            if i - 1 in row_imgs:
                sheet_imgs += row_imgs.pop(i - 1)
        tbl = _table_md(rows)
        if tbl:
            lines.append(tbl)
        else:
            lines.append("（空工作表）")
        lines.append("")
        # 图排在整表之后（按行序）：表格中间插图会把 markdown 表格切断成两半。
        # 「这一格写着什么」的信息由 alt（工作表名）和图里自带的文字承担。
        for blob, ext in sheet_imgs:
            rel = writer.add(blob, ext, "%s 配图" % ws.title)
            if rel:
                lines.append("")
                lines.append("![%s 配图](%s)" % (ws.title, rel))
                lines.append("")
    wb.close()
    if not any(l.strip() for l in lines):
        raise ConvertError("这个 Excel 文件里没有读到任何数据")
    return _tidy("\n".join(lines))


# ==================== PDF ====================

def _pdf_line_sizes(page):
    """本页每一行的字号：{"行文本": 字号}。用于把大字行判成标题。

    为什么不用模块级全局表：不同 PDF 里常出现同一句文字（「操作说明」「注意事项」），
    全局缓存会让后一份文档按前一份的字号去判标题，错得莫名其妙。必须逐页局部收集。
    """
    out = {}
    try:
        for blk in page.get_text("dict").get("blocks", []):
            for line in blk.get("lines", []):
                txt = "".join(sp.get("text") or "" for sp in line.get("spans", [])).strip()
                if not txt:
                    continue
                mx = max((sp.get("size") or 0) for sp in line.get("spans", []) or [{}])
                if mx and (txt not in out or mx > out[txt]):
                    out[txt] = mx
    except Exception:
        pass
    return out


def _pdf_body_size(all_sizes):
    """正文基准字号 = 众数。取不到就用中位数；都没有返回 None（此时不判标题）。"""
    vals = [s for s in all_sizes if s > 0]
    if not vals:
        return None
    try:
        import statistics
        return statistics.mode(vals)
    except Exception:
        return sorted(vals)[len(vals) // 2]


def _pdf_text_as_md(text, body_size, sizes):
    """把一个文字块转成 markdown，按字号粗判标题层级。

    判不准就当普通段落——错判成段落最多影响分片粒度，不会改错内容。
    """
    out = []
    for raw in (text or "").split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line:
            continue
        size = sizes.get(line)
        if body_size and size and size >= body_size * 1.25 and len(line) <= 40:
            lvl = max(2, min(6, int(round((size - body_size) / 3.0)) + 2))
            out.append("%s %s" % ("#" * lvl, line))
        else:
            out.append(line)
    return "\n\n".join(out)


def _pdf_big_images(page):
    """本页占面积较大的图片 [(rect, 面积比)]，按面积倒序。"""
    import fitz
    out = []
    try:
        page_area = abs(page.rect.get_area()) or 1.0
        for info in page.get_image_info():
            bbox = info.get("bbox")
            if not bbox:
                continue
            r = fitz.Rect(bbox)
            ratio = abs(r.get_area()) / page_area
            if ratio >= MIN_IMG_AREA_RATIO:
                out.append((r, ratio))
    except Exception:
        pass
    out.sort(key=lambda x: -x[1])
    return out


def _render_pdf_area(page, writer, alt, clip=None):
    """把页面（或页面的一块区域）渲染成 JPEG 并入库，返回相对路径。

    一律**重新渲染**而不抽原始字节：PDF 里的位图可能是 JPX / JBIG2 / CCITT，
    浏览器和 Pillow 都解不开，直接发出去客户看到的是裂图。渲染顺带统一了输出格式。
    """
    try:
        rect = clip if clip is not None else page.rect
        pix = page.get_pixmap(dpi=PDF_RENDER_DPI, clip=rect)
        return writer.add(pix.tobytes("jpeg"), ".jpg", alt)
    except Exception:
        return None


HEADER_BAND_RATIO = 0.08                 # 页眉/页脚判定带（占页高比例）
MAX_HEADER_CHARS = 30                    # 页眉页脚都很短


def _pdf_is_header_footer(rect, page_height, text):
    """判断一个文字块是不是页眉/页脚（页码、公司抬头那一行）。

    为什么必须滤：真实 PDF 几乎每页都有页码与抬头，滤掉之前，
    每页开头都会塞一句「1-3 / 示例公司 · 操作手册」，既污染检索文本，
    又会让「## 第 N 页」和页内标题重复出现两次。
    判据是「位置在上下边缘 + 内容很短」——正文里不会有这么短的边条。
    """
    if len(text) > MAX_HEADER_CHARS:
        return False
    top = rect.y1 <= page_height * HEADER_BAND_RATIO
    bottom = rect.y0 >= page_height * (1 - HEADER_BAND_RATIO)
    return top or bottom


def _pdf_page_items(page, writer, body_size, sizes):
    """把一页里的文字块与图片按 y 坐标交错排序（PDF 没有"文档流"，只能靠坐标）。"""
    import fitz                              # 循环里要用 fitz.Rect，只导一次
    items = []
    try:
        blocks = page.get_text("blocks")
    except Exception:
        blocks = []
    for b in blocks:
        try:
            x0, y0, txt = b[0], b[1], b[4]
        except (IndexError, TypeError):
            continue
        if len(b) > 6 and b[6] == 1:          # block_type 1 = 图片块，文本为空
            continue
        flat = re.sub(r"\s+", "", txt or "")
        if not flat:
            continue
        if _pdf_is_header_footer(fitz.Rect(b[0], b[1], b[2], b[3]),
                                 abs(page.rect.height) or 1.0, flat):
            continue
        md = _pdf_text_as_md(txt, body_size, sizes)
        if md:
            items.append({"y": y0, "x": x0, "text": md})

    try:
        page_area = abs(page.rect.get_area()) or 1.0
        seen = []
        for info in page.get_image_info():
            bbox = info.get("bbox")
            if not bbox:
                continue
            r = fitz.Rect(bbox)
            if abs(r.get_area()) / page_area < MIN_IMG_AREA_RATIO:
                continue                       # 分隔线/装饰条，抽进来只会污染图片库
            # 同一张图常被多个近似 bbox 描述（叠加图层/重复引用），重叠的只留一个
            if any(abs(r.y0 - s[0]) < 3 and abs(r.y1 - s[1]) < 3 for s in seen):
                continue
            seen.append((r.y0, r.y1))
            rel = _render_pdf_area(page, writer, "原文截图", clip=r)
            if rel:
                items.append({"y": r.y0, "x": r.x0,
                              "text": "![原文截图](%s)" % rel})
    except Exception:
        pass
    # 同一条水平带（页眉页脚）内的块排序抖动会打乱阅读顺序，用 3pt 容差并排
    items.sort(key=lambda it: (round(it["y"] / 3.0), it["x"]))
    return items


def convert_pdf(path, writer):
    import fitz
    try:
        doc = fitz.open(path)
    except Exception as e:
        raise ConvertError("PDF 打不开（可能已加密或损坏）：%s" % e)
    try:
        if getattr(doc, "needs_pass", False):
            raise ConvertError("PDF 已加密，无法读取")
        if doc.page_count > MAX_PDF_PAGES:
            raise ConvertError("PDF 有 %d 页，超过上限 %d 页。请拆成几份再导入"
                               % (doc.page_count, MAX_PDF_PAGES))

        # 全书正文字号取众数（各页字号未必一致），标题判定才有统一基准
        all_sizes = []
        for pg in doc:
            all_sizes += list(_pdf_line_sizes(pg).values())
        body_size = _pdf_body_size(all_sizes)

        lines = []
        for pno in range(doc.page_count):
            page = doc[pno]
            pageno = pno + 1
            sizes = _pdf_line_sizes(page)
            text = (page.get_text("text") or "").strip()
            big = _pdf_big_images(page)
            # 扫描页：几乎没有文字 + 有大图 → 整页渲染成一张图。
            # 这种情况按坐标抽碎片毫无意义（扫描件里的"图"是噪点与边缘），
            # 而且抽出来客户根本拼不回原页面。
            if len(text) < SCANNED_TEXT_CHARS and big and big[0][1] >= BIG_IMG_AREA_RATIO:
                rel = _render_pdf_area(page, writer, "第 %d 页原文截图" % pageno)
                if rel:
                    lines.append("## 第 %d 页" % pageno)
                    lines.append("")
                    lines.append("![第 %d 页原文截图](%s)" % (pageno, rel))
                    lines.append("")
                    continue
            items = _pdf_page_items(page, writer, body_size, sizes)
            if not items:
                continue
            if len(items) == 1 and not big:
                lines.append(items[0]["text"])       # 整页就一段文字，不必塞页标题
                lines.append("")
                continue
            # 页内首行已经是个标题时不再加「## 第 N 页」——否则同一页出现两个标题，
            # 而且这俩文字往往一模一样（PDF 导出常把页码做成大标题）
            first = items[0]["text"].lstrip() if items else ""
            if not first.startswith("#"):
                lines.append("## 第 %d 页" % pageno)
                lines.append("")
            for it in items:
                lines.append(it["text"])
                lines.append("")
        md = _tidy("\n".join(lines))
        if not md.strip():
            raise ConvertError("这个 PDF 没读到任何内容（可能是纯图片扫描件且渲染失败）")
        return md
    finally:
        try:
            doc.close()
        except Exception:
            pass


# ==================== 入口 ====================

def convert(src_path, image_dir, stem=None):
    """把一个 Office/PDF 文件转成 markdown，图片落到 image_dir。

    返回 {"markdown", "images", "skipped", "ext"}；失败一律抛 ConvertError。
    """
    # ⚠️ 先判类型、再判文件在不在：反过来会让「x.exe 不存在」报成「文件不存在」，
    # 而运营真正要修的是「格式不对」。类型是廉价判断，先做没有代价。
    ext = os.path.splitext(src_path or "")[1].lower()
    if ext == ".xls":
        raise ConvertError("暂不支持 .xls 老格式，请在 Excel 里另存为 .xlsx 再导入")
    if ext == ".doc":
        raise ConvertError("暂不支持 .doc 老格式，请在 Word 里另存为 .docx 再导入")
    if ext not in CONVERTIBLE:
        raise ConvertError("不支持的文件类型 %s（支持 %s）"
                           % (ext or "（无扩展名）", "、".join(CONVERTIBLE)))
    if not os.path.isfile(src_path):
        raise ConvertError("源文件不存在")
    size = os.path.getsize(src_path)
    if size > MAX_SOURCE_BYTES:
        raise ConvertError("文件超过 %dMB，请先删掉无关内容再导入"
                           % (MAX_SOURCE_BYTES // 1024 // 1024))
    _need(ext)

    stem = sanitize_stem(stem or src_path)
    writer = _ImageWriter(image_dir, stem)
    if ext in PDF_EXT:
        md = convert_pdf(src_path, writer)
    elif ext in WORD_EXT:
        md = convert_docx(src_path, writer)
    else:
        md = convert_xlsx(src_path, writer)

    md = _ensure_title(md, stem)
    # 只在「源文件确实带图、但一张都没抽出来」时才报警。
    # 业务字典、QA 表这类纯文字表格本来就没图，报了「没抽到图片」只会让人以为导入失败。
    warnings = []
    if not writer.saved:
        n_src = _count_source_images(src_path, ext)
        if n_src:
            warnings.append("源文件里有 %d 张图片，但一张都没抽到，"
                            "可能是图片格式特殊（建议改存为 docx/普通 PDF 后重试）"
                            % n_src)
    if writer.skipped:
        warnings.append("有 %d 张图超过单文档上限 %d 张被跳过"
                        % (writer.skipped, MAX_IMAGES))
    return {"markdown": md, "images": writer.saved, "skipped": writer.skipped,
            "refs": writer.refs, "ext": ext, "stem": stem, "warnings": warnings}


def _count_source_images(path, ext):
    """数一下 Office 包里 media 目录有多少图（只用于报警文案）。"""
    if ext not in WORD_EXT + EXCEL_EXT:
        return 0
    try:
        import zipfile
        with zipfile.ZipFile(path) as z:
            return sum(1 for n in z.namelist() if "/media/" in n)
    except Exception:
        return 0


def _ensure_title(md, stem):
    """保证首行是 H1。缺标题时 fragment 会整段混在一个 chunk 里，不好定位。"""
    head = md.lstrip()
    if head.startswith("# "):
        return md
    return "# %s\n\n%s" % (stem, md)