"""webhook 消息归一化 + 群聊/私聊接入策略判定。

判定顺序（任何一步不通过都不回消息）：
  1. 忽略自己发出的消息（sent / outgoing / direction=out），防自问自答死循环
  2. 只处理群聊与私聊；公众号/服务号/文件助手等系统会话**直接略过且不记录**
     （见 is_noise_session，webhook 入口就丢掉，不写 DB）
  3. 群聊：必须在 group_rules 配置表内，且「@ 本机」/「命中唤醒词」/
     「引用了本机发出的消息」/「处在对话延续窗口内」之一才触发
     （窗口见 followup.py，可后台关闭）
  4. 私聊：全局黑名单 → 唤醒词选策略 → 白名单（空=所有人）→ 规则黑名单
  5. 触发后按该条配置绑定的知识库策略，取出允许检索的文档列表（sources）

设计取舍：策略每次都从 SQLite 读（轻量、改完立即生效、不用重启服务），
不做内存缓存 —— 后台改配置却要重启才生效，是运营最容易踩的坑。
"""
import time

import followup
import store
from textutil import (entity_in_list, match_wake_word, parse_quote_reference,
                      parse_quote_sender, strip_at_prefix)

TYPE_TEXT = 1
TYPE_IMAGE = 3
TYPE_VOICE = 34
TYPE_VIDEO = 43
TYPE_APP = 49

# 公众号 / 服务号 / 文件助手 / 微信内部会话：不是真实客户对话。
# 这些会话推送很频繁，记进「对话记录」只会把 skip 流水刷满，没有任何排查价值。
NOISE_CHAT_PREFIXES = ("gh_",)
NOISE_CHAT_IDS = frozenset((
    "filehelper",           # 文件传输助手
    "brandsessionholder",   # 品牌会话 / 服务通知
    "qmessage",             # QQ 消息助手
    "qqmail",               # QQ 邮箱提醒
    "fmessage",             # 朋友推荐消息
    "medianote",            # 语音记事本
    "floatbottle",          # 漂流瓶
    "shakeapp",             # 摇一摇
    "newsapp",              # 腾讯新闻
    "notifymessage",        # 系统通知
    "tmessage",             # 腾讯服务通知
    "weixin",               # 微信团队
))
NOISE_CHAT_TYPES = frozenset(("official", "service", "brand", "system", "notification"))


def _is_noise_chat(chat, ctype=""):
    chat = str(chat or "").strip().lower()
    ctype = str(ctype or "").strip().lower()
    return (ctype in NOISE_CHAT_TYPES or chat in NOISE_CHAT_IDS
            or chat.startswith(NOISE_CHAT_PREFIXES))


def is_noise_session(m):
    """公众号/服务号/文件助手等系统会话，调用方应**直接略过、不记任何流水**。

    与「真实客户消息但没触发回复」的 skip 严格区分：后者仍要记进 events，
    供「对话记录」排查「为什么没回」。这里的是压根不该出现在记录里的噪声。
    """
    return (m.get("chat_type") == "official" or
            _is_noise_chat(m.get("chat"), m.get("chatType") or m.get("chat_type")))


def normalize(payload):
    """把插件 webhook payload 归一成稳定字段（兼容 /api/messages 的历史消息结构）。"""
    m = dict(payload or {})
    chat = str(m.get("chat") or "").strip()
    ctype = str(m.get("chatType") or "").strip().lower()
    is_group = bool(m.get("isGroup")) or ctype == "group" or chat.endswith("@chatroom")
    if is_group:
        chat_type = "group"
    elif _is_noise_chat(chat, ctype):
        chat_type = "official"
    else:
        chat_type = "private"

    try:
        mtype = int(m.get("type") or 0)
    except (TypeError, ValueError):
        mtype = 0
    tname = str(m.get("typeName") or "")

    text = m.get("text")
    if not text and mtype == TYPE_TEXT:
        text = m.get("content")
    text = strip_at_prefix(str(text or ""))

    out = {
        "chat": chat,
        "chat_type": chat_type,
        "is_group": is_group,
        "chat_name": str(m.get("chatName") or m.get("name") or chat),
        "sender": str(m.get("sender") or "").strip(),
        "sender_name": str(m.get("senderName") or "").strip(),
        "type": mtype,
        "type_name": tname,
        "text": text,
        "content": str(m.get("content") or ""),
        "app_type": str(m.get("appType") or "").lower(),
        "at_list": [str(x) for x in (m.get("atList") or []) if x],
        "at_me": bool(m.get("atMe")),
        "at_all": bool(m.get("atAll")) or str(m.get("content") or "").startswith("@所有人"),
        "local_id": int(m.get("localID") or 0),
        "media_api": str(m.get("mediaApi") or ""),
        # 文件消息元信息（1.1.1-beta1）：payload 原本就带，早期归一化丢弃了，
        # 媒体存储要按原名落记录，这里补回来，不影响其它字段口径。
        "file_name": str(m.get("fileName") or ""),
        "file_size": m.get("fileSize") or 0,
        "file_ext": str(m.get("fileExt") or ""),
        "file_md5": str(m.get("fileMd5") or ""),
        "app_sub_type": str(m.get("appSubType") or ""),
        "outgoing": bool(m.get("outgoing")),
        "direction": str(m.get("direction") or "").lower(),
        "ts": float(m.get("ts") or 0),
        # 引用消息结构化字段（文档 7.2.1）：quote + quoteResolved。
        # 按「推送必带 quoteResolved」处理，只用于下载被引用的图片；不回查历史。
        "svrid": str(m.get("svrid") or ""),
        "quote": m.get("quote") if isinstance(m.get("quote"), dict) else {},
        "quote_resolved": (m.get("quoteResolved")
                           if isinstance(m.get("quoteResolved"), dict) else {}),
    }
    return out


def is_outgoing(m, self_wxid=""):
    """自己发出的消息必须丢掉，避免机器人回复触发自己再被处理。

    早期版本起 `/api/send/*` 发出的消息与新消息走同一条 webhook 链路，
    以 `outgoing` / `direction=out` 判断，不再有 `sent=true` 发送确认。
    """
    if m.get("outgoing") or m.get("direction") == "out":
        return True
    return bool(self_wxid and m.get("sender") and m["sender"] == self_wxid)


def message_kind(m):
    """返回 text / quote / voice / image / video / other。"""
    t = m.get("type") or 0
    name = (m.get("type_name") or "").lower()
    if t == TYPE_TEXT or name == "text":
        return "text"
    if t == TYPE_APP and m.get("app_type") == "quote":
        return "quote"
    if t == TYPE_VOICE or name == "voice":
        return "voice"
    if t == TYPE_IMAGE or name == "image":
        return "image"
    if t == TYPE_VIDEO or name == "video":
        return "video"
    return "other"


def dedup_key(m):
    """localID 只在同一会话内唯一，所以键里必须带 chat。"""
    return "|".join([m.get("chat") or "", str(m.get("local_id") or 0),
                     str(int(m.get("ts") or 0)), m.get("sender") or "",
                     str(m.get("type") or 0)])


def too_old(m, max_age=600):
    ts = m.get("ts") or 0
    if ts <= 0:
        return False                       # 插件没给时间戳时不当过期处理
    try:
        max_age = int(max_age)
    except (TypeError, ValueError):
        max_age = 600
    if max_age <= 0:
        return False
    return ts < time.time() - max_age


def _skip(reason, m, **extra):
    d = {"handled": False, "reason": reason, "chat_type": m.get("chat_type"),
         "sources": None, "policy_id": None, "policy_name": "",
         "trigger": "", "question": "", "wake_word": "", "rule": {}}
    d.update(extra)
    return d


def quoted_self_message(m, self_wxid=""):
    """引用回复里被引用的是不是「本机发出的消息」（第三种触发方式）。

    微信 refermsg 的 chatusr 是被引用消息的发送者 wxid（群聊里 fromusr 是群 ID，
    私聊里 fromusr 通常就是发送者），两者任一命中本机 wxid 即成立。插件若把 XML
    裁剪到只剩 displayname，再用本机显示名兜底。命中后用户不必再 @ / 加唤醒词。
    """
    if message_kind(m) != "quote":
        return False
    ref = parse_quote_sender(m.get("content"))
    if not ref:
        return False
    me = str(self_wxid or "").strip()
    if me and me in (ref.get("chatusr"), ref.get("fromusr")):
        return True
    name = str(ref.get("displayname") or "").strip()
    if not name:
        return False
    try:
        own = str(store.self_display_name(me) or "").strip()
    except Exception:
        own = ""
    return bool(own) and name == own


def _ok(m, rule, trigger, wake_word, question):
    return {
        "handled": True,
        "reason": "",
        "chat_type": m.get("chat_type"),
        "sources": list(rule.get("docs") or []),
        "policy_id": rule.get("policy_id"),
        "policy_name": rule.get("policy_name") or "",
        "trigger": trigger,
        "question": question,
        "wake_word": wake_word,
        "system_prompt": (rule.get("system_prompt") or "").strip(),
        "human_wxid": (rule.get("human_wxid") or "").strip(),
        "human_name": (rule.get("human_name") or "").strip(),
        # 规则级能力开关（store 里两表默认 1，老库迁移后也是 1，行为不变）
        "need_transfer": bool(rule.get("need_transfer", True)),
        "need_kb": bool(rule.get("need_kb", True)),
        # 本条群/私聊配置自己选的大模型（llm_models.id）；None = 不指定，
        # 由 brain 按「场景默认 → 全局默认 → config.yaml」兜底。
        # 选的模型被停用 / 删除时（llm_available=False）视同没选 —— 不能让一条
        # 失效的模型配置把该群/私聊的回复降级到 config.yaml 或直接失败。
        "llm_id": ((int(rule.get("llm_id") or 0)
                    if rule.get("llm_available", True) else 0) or None),
        "llm_name": rule.get("llm_name") or "",
        "rule": rule,
    }


def _at_triggered(m, self_wxid, rule):
    at_list = m.get("at_list") or []
    if m.get("at_me"):
        return "at"
    if self_wxid and self_wxid in at_list:
        return "at"
    if m.get("at_all") and rule.get("at_all_trigger", True):
        return "atAll"
    return ""


def basic_allowed(m, self_wxid=""):
    """粗过滤（在花钱做 ASR 之前用）：会话类型、群是否配置、全局黑名单、有无启用策略。

    返回 (bool, 原因)。不做 @ / 唤醒词判断 —— 语音要识别完才知道有没有唤醒词。
    """
    if m.get("chat_type") == "official":
        return False, "非群聊/私聊会话"
    if m.get("chat_type") == "group":
        return (True, "") if store.get_group_rule(m.get("chat")) else \
            (False, "群聊不在配置表内")
    if entity_in_list(m.get("sender"), store.get_global_deny()) or \
            entity_in_list(m.get("sender_name"), store.get_global_deny()):
        return False, "全局黑名单用户"
    if not store.enabled_private_rules():
        return False, "没有启用的私聊策略"
    return True, ""


def resolve(m, self_wxid="", text_override=None):
    """核心判定。text_override 用于语音：ASR 识别出的文本参与唤醒词匹配。

    返回 dict：handled / reason / sources / policy_* / trigger / question ...
    """
    text = m.get("text") if text_override is None else str(text_override or "")

    if m.get("chat_type") == "official":
        return _skip("非群聊/私聊会话（公众号、服务号、文件助手不处理）", m)

    # ---------------- 群聊 ----------------
    if m.get("chat_type") == "group":
        rule = store.get_group_rule(m.get("chat"))
        if not rule:
            return _skip("群聊不在配置表内", m)
        at = _at_triggered(m, self_wxid, rule)
        hit, word, q = match_wake_word(text, rule.get("wake_words"))
        # 引用了本机发出的消息也算触发：用户回引机器人刚说的话继续追问时，
        # 不必再 @ / 加唤醒词。群聊规则里的「引用本机消息也触发」可以单独关掉。
        quoted_me = (rule.get("quote_self_trigger", True) and
                     quoted_self_message(m, self_wxid))
        if at or hit or quoted_me:
            trigger = at or (("wake:%s" % word) if hit else "quote:self")
        else:
            # 机器人刚在这个群回复过、且在窗口内的同一群友：视为对话继续，
            # 不需要再 @ / 唤醒。窗口的开关、时长见 followup.config()；
            # 作用范围固定为同一群友，不扩散到群里其他人。
            fw = followup.check(m, self_wxid=self_wxid)
            if not fw["ok"]:
                return _skip("未 @ 本机、未命中唤醒词且未引用本机消息（对话延续未生效：%s）"
                             % fw["reason"], m, rule=rule)
            trigger = "followup"
        question = q if hit else text
        # 策略为空仍允许回复，但 brain 按空策略检索，不会越权读别的库
        return _ok(m, rule, trigger, word, question)

    # ---------------- 私聊 ----------------
    if entity_in_list(m.get("sender"), store.get_global_deny()) or \
            entity_in_list(m.get("sender_name"), store.get_global_deny()):
        return _skip("全局黑名单用户", m)

    rules = store.enabled_private_rules()
    if not rules:
        return _skip("没有启用的私聊策略", m)

    candidates = []
    default_rule = None
    for r in rules:
        words = r.get("wake_words") or []
        if not words:
            if default_rule is None:
                default_rule = r          # 唤醒词留空 = 兜底规则，放最后匹配
            continue
        hit, word, q = match_wake_word(text, words)
        if hit:
            candidates.append((r, "wake:%s" % word, word, q))
    if default_rule is not None:
        candidates.append((default_rule, "default", "", text))
    if not candidates and quoted_self_message(m, self_wxid):
        # 引用了本机发出的消息：免唤醒词，优先用兜底规则，否则用第一条启用规则
        r = next((x for x in rules if not x.get("wake_words")), rules[0])
        candidates.append((r, "quote:self", "", text))
    if not candidates:
        return _skip("未命中任何私聊唤醒词", m)

    denied = []
    for r, trigger, word, q in candidates:
        who = (m.get("sender"), m.get("sender_name"))
        if any(entity_in_list(x, r.get("deny_list")) for x in who):
            denied.append("命中策略「%s」黑名单" % r.get("name"))
            continue
        allow = r.get("allow_list") or []
        if allow and not any(entity_in_list(x, allow) for x in who):
            denied.append("不在策略「%s」白名单内" % r.get("name"))
            continue
        return _ok(m, r, trigger, word, q)
    return _skip("；".join(denied) or "私聊策略均未通过", m)


def quote_reference(m):
    """引用回复的被引用原文（供拼 prompt）。"""
    if message_kind(m) != "quote":
        return {}
    return parse_quote_reference(m.get("content"))
