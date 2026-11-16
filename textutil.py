"""纯文本工具：微信消息清洗、唤醒词处理、引用消息解析、列表解析。

不放任何微信协议/网络/数据库依赖，前后端都能安全 import。
"""
import html
import re

# U+2005 是微信 @ 后面的四分之一 em 空格；U+00A0 是部分客户端的不换行空格
_AT_SEP = " \u2005\u00a0\u3000\t"
_STRIP_CHARS = " \u2005\u00a0\u3000\t\r\n:：,，、;；.!！?？~-—"


def strip_markdown(text):
    """把 Markdown 降级为微信可阅读的纯文本（个人微信不渲染 Markdown）。"""
    if not text:
        return text
    text = re.sub(r"```[\s\S]*?```", lambda m: m.group(0).replace("```", ""), text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", text)
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*([-*_])\1{2,}\s*$", "————", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    text = re.sub(r"(?m)^\s*\|?[-:\s|]+\|?\s*$", "", text)
    text = re.sub(r"(?m)\s*\|\s*", " | ", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "• ", text)
    text = re.sub(r"(?m)^\s*\d+[.)]\s+", "• ", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    # 斜体只认「词边界上的成对标记」，单词内部的下划线不解析：
    # 否则知识库里的型号 / 字段名（ONLY_DOC_A、item_2026_no）会被悄悄改成 ONLYDOCA。
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_at_prefix(text):
    """兜底去掉 '@昵称\u2005' 前缀（插件 webhook 的 `text` 已处理，但历史消息可能没有）。"""
    s = text or ""
    for _ in range(10):
        if not s.startswith("@"):
            break
        i = 1
        while i < len(s) and s[i] not in _AT_SEP:
            i += 1
        if i >= len(s):
            return ""
        s = s[i:].lstrip(_AT_SEP)
    return s


def match_wake_word(text, words):
    """判断文本是否命中唤醒词，并剥离唤醒词。

    返回 (是否命中, 命中词, 剥离后的提问)。
    唤醒词出现在句中也算命中（微信里常写「小助 帮我查一下」或「帮我问一下小助」）。
    """
    s = (text or "").strip()
    if not s:
        return False, "", s
    for w in words or []:
        w = str(w or "").strip()
        if not w:
            continue
        i = s.find(w)
        if i < 0:
            continue
        rest = (s[:i] + s[i + len(w):]).strip(_STRIP_CHARS)
        return True, w, rest or s
    return False, "", s


def parse_list(value):
    """把配置/表单里的「wxid、昵称」列表解析成去重后的字符串列表。

    支持：list/tuple、逗号/顿号/分号/换行分隔的字符串、JSON 数组字符串。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(x) for x in value]
    else:
        s = str(value).strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                import json
                obj = json.loads(s)
                if isinstance(obj, list):
                    items = [str(x) for x in obj]
                else:
                    items = [s]
            except Exception:
                items = [s]
        else:
            items = re.split(r"[,，、;；\r\n]+", s)
    out = []
    for x in items:
        x = str(x).strip()
        if x and x not in out:
            out.append(x)
    return out


def entity_in_list(entity, list_items):
    """实体（wxid / 昵称）是否命中名单；昵称不区分大小写、去空白。"""
    target = str(entity or "").strip()
    if not target:
        return False
    low = target.lower()
    for item in list_items or []:
        v = str(item or "").strip()
        if v and (v == target or v.lower() == low):
            return True
    return False


def _xml_text(xml, tag):
    m = re.search(r"<%s>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</%s>" % (tag, tag), xml or "", re.I)
    return html.unescape(m.group(1)).strip() if m else ""


def parse_quote_reference(content):
    """解析微信引用回复（type=49 / appType=quote）里的被引用内容。

    返回 {"displayname":.., "content":..}；不是引用消息则返回空字典。
    引用消息往往写成「这个怎么弄」并引用一段说明，把被引用的原文一并送进
    大模型能显著提高回答准确率。
    """
    xml = content or ""
    if "<refermsg" not in xml:
        return {}
    block = re.search(r"<refermsg>([\s\S]*?)</refermsg>", xml, re.I)
    if not block:
        return {}
    inner = block.group(1)
    refer = {
        "displayname": _xml_text(inner, "displayname"),
        "content": _xml_text(inner, "content"),
    }
    refer["content"] = strip_at_prefix(refer["content"])[:500]
    return refer if refer["content"] else {}


def parse_quote_sender(content):
    """解析引用消息里「被引用那条」的发送者，用于判断是不是引用了本机消息。

    微信 refermsg：群聊里 <chatusr> 是被引用消息的发送者 wxid，<fromusr> 是群 ID；
    私聊里 fromusr 往往就是发送者。displayname 是显示名 / 群名片，只作兜底。
    返回 {"chatusr":.., "fromusr":.., "displayname":..}，非引用消息返回空字典。
    """
    xml = content or ""
    if "<refermsg" not in xml:
        return {}
    block = re.search(r"<refermsg>([\s\S]*?)</refermsg>", xml, re.I)
    if not block:
        return {}
    inner = block.group(1)
    ref = {
        "chatusr": _xml_text(inner, "chatusr"),
        "fromusr": _xml_text(inner, "fromusr"),
        "displayname": _xml_text(inner, "displayname"),
    }
    return ref if any(ref.values()) else {}


def build_question(text, reference=None):
    """把引用上下文拼进提问；没有引用时原样返回。"""
    q = (text or "").strip()
    if not reference or not reference.get("content"):
        return q
    who = reference.get("displayname") or "对方"
    return "引用消息（%s）：%s\n\n我的问题：%s" % (who, reference["content"], q)


def truncate(text, limit):
    """按字数截断，超长时补省略号（防止微信一条消息刷屏）。"""
    s = text or ""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return s
    if n <= 0 or len(s) <= n:
        return s
    return s[:n].rstrip() + "…"


def safe_cell(value):
    """CSV 单元格：统一成字符串，避免 None 变成 'None'。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)
