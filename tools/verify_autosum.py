"""AutoSummary 插件自检脚本（不依赖真实微信与真实大模型）。

跑法：python tools/verify_autosum.py
覆盖：识别逻辑、卡片解析、HTML 抽取、黑白名单、范围判定、追问缓存、大模型调用。
"""
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pluginhost import CONTINUE, STOP_ALL, PluginBase  # noqa: E402
from plugins.AutoSummary.main import AutoSummary, _HtmlText  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ((" | " + str(extra)) if extra and not cond else ""))


class FakeLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


class FakeCtx:
    """最小 PluginContext 替身：只提供插件用到的成员。"""

    def __init__(self, m, config=None):
        self.m = m
        self.config = config or {}
        self.state = {}
        self.kind = "text"
        self.log = FakeLog()
        self.replies = []
        self.reply_kw = []
        self.calls = []

    def reply(self, text, *, with_hint=False, mark_followup=False):
        """签名对齐真实 PluginContext.reply，默认值也一并模拟。"""
        self.replies.append(text)
        self.reply_kw.append({"with_hint": with_hint,
                              "mark_followup": mark_followup})
        return "text"

    def llm_chat(self, messages, **kw):
        self.calls.append({"messages": messages, "kw": kw})
        return "【模拟摘要】这是内置大模型返回的摘要内容。"


def mk(text="", content="", chat="123@chatroom", sender="wxid_a", chat_type="group", **cfg):
    return FakeCtx({"chat": chat, "chat_type": chat_type, "sender": sender,
                    "text": text, "content": content, "type": 1}, cfg)


print("\n=== 0. 默认行为：关闭 + 严格模式 + / 触发词 ===")
p0 = AutoSummary()
check("插件默认关闭（enabled=false）",
      p0._plugin_enabled(mk("/总结 https://a.com")) is False)
check("默认需要唤醒词（require_host_trigger=true）",
      p0._require_host_trigger(mk("/总结 https://a.com")) is True)
# 显式开启 enabled 后应放行
check("enabled=true 时插件启用",
      p0._plugin_enabled(mk("/总结 https://a.com", enabled=True)) is True)
# 内置默认触发词全部带 / 前缀
from plugins.AutoSummary.main import DEFAULT_TRIGGERS  # noqa: E402
check("默认总结触发词全部以 / 开头",
      all(t.startswith("/") for t in DEFAULT_TRIGGERS["summary_triggers"]),
      DEFAULT_TRIGGERS["summary_triggers"])
check("默认追问触发词全部以 / 开头",
      all(t.startswith("/") for t in DEFAULT_TRIGGERS["qa_triggers"]),
      DEFAULT_TRIGGERS["qa_triggers"])
# 老触发词（不带 /）应识别不到（用不带链接的文本，排除裸链接自动识别的干扰）
check("旧词「总结」不再触发", p0._detect(mk("总结 讲讲这个")) is None)
check("旧词「问」不再触发", p0._detect(mk("问 这是啥")) is None)

print("\n=== 1. 识别逻辑 ===")
p = AutoSummary()

ctx = mk("/总结 https://example.com/a")
act = p._detect(ctx)
check("识别「/总结 <url>」", act and act["kind"] == "summary" and act["url"] == "https://example.com/a", act)

ctx = mk("/总结 这篇文章讲了什么 https://example.com/b")
act = p._detect(ctx)
check("识别「/总结 <自定义问题> <url>」",
      act and act["question"] == "这篇文章讲了什么" and act["url"] == "https://example.com/b", act)

ctx = mk("https://example.com/c")
act = p._detect(ctx)
check("识别裸链接（自动）", act and act["auto"] is True and act["url"] == "https://example.com/c", act)

ctx = mk("问题来了")
act = p._detect(ctx)
check("「问题来了」不误触发追问", act is None, act)

ctx = mk("/问题来了")
act = p._detect(ctx)
check("「/问题来了」不误触发追问", act is None, act)

ctx = mk("/问 这篇的核心观点是什么")
act = p._detect(ctx)
check("没有缓存时不认「/问」触发词（不劫持 AI 的问答）", act is None, act)

p_pre = AutoSummary()
p_pre._summaries["123@chatroom"] = {"content": "原文", "ts": time.time()}
ctx = mk("/问 这篇的核心观点是什么")
act = p_pre._detect(ctx)
check("有缓存时识别「/问 <问题>」",
      act and act["kind"] == "qa" and act["question"] == "这篇的核心观点是什么", act)

ctx = mk("/追问 作者是谁")
act = p_pre._detect(ctx)
check("有缓存时识别「/追问 <问题>」",
      act and act["kind"] == "qa" and act["question"] == "作者是谁", act)

ctx = mk("今天天气不错")
check("无关消息不触发", p._detect(ctx) is None)

ctx = mk("https://example.com/d", auto_summary=False)
check("auto_summary=false 时裸链接不触发", p._detect(ctx) is None)

print("\n=== 2. URL 提取 ===")
check("中文标点不吞进 URL", p._first_url("看看 https://example.com/e。") == "https://example.com/e",
      p._first_url("看看 https://example.com/e。"))
check("维基带括号的链接", p._first_url("看 (https://zh.wikipedia.org/wiki/美业)") == "https://zh.wikipedia.org/wiki/美业",
      p._first_url("看 (https://zh.wikipedia.org/wiki/美业)"))
check("无链接返回空", p._first_url("没有链接") == "")
# 触发词后跟各种空白 / 隐形字符，都要能拿到干净链接（本次修复重点）
for label, sep in [("单空格", " "), ("双空格", "  "), ("tab", "\t"),
                   ("换行", "\n"), ("全角空格", "　"), ("NBSP", "\u00a0"),
                   ("零宽空格", "\u200b"), ("中文冒号+空格", "： ")]:
    got = p._detect(mk("/总结%shttps://example.com/a" % sep))
    check("触发词后[%s]能提取链接" % label,
          got and got.get("url") == "https://example.com/a", got)
check("链接前后空白被清干净",
      p._first_url("  https://example.com/a  ") == "https://example.com/a",
      p._first_url("  https://example.com/a  "))
check("零宽字符不会混进链接",
      p._first_url("https://example.com/a\u200b") == "https://example.com/a",
      p._first_url("https://example.com/a\u200b"))

print("\n=== 3. 卡片 XML 解析 ===")
card_xml = """<msg><appmsg appid="1"><title>美业门店运营的5个数据</title>
<des>摘要描述</des><url>https://mp.weixin.qq.com/s/abc?chksm=1&amp;sn=x</url>
<type>5</type></appmsg><appname>公众号</appname></msg>"""
ctx = mk(content=card_xml)
card = p._parse_card(ctx.m)
check("解析卡片标题", card and card["title"] == "美业门店运营的5个数据", card)
check("解析卡片描述", card and card["desc"] == "摘要描述", card)
check("解析并反转义卡片 URL",
      card and card["url"] == "https://mp.weixin.qq.com/s/abc?chksm=1&sn=x", card)

ctx = mk(content=card_xml)
act = p._detect(ctx)
check("卡片消息被识别为自动总结", act and act["kind"] == "summary" and act["auto"] is True
      and act["url"] == "https://mp.weixin.qq.com/s/abc?chksm=1&sn=x", act)

check("非 XML 内容不当作卡片", p._parse_card({"content": "普通文本", "text": ""}) is None)

print("\n=== 4. HTML 正文抽取（不依赖 bs4） ===")
html = """<html><head><title>测试标题</title>
<script>var x=1;</script><style>.a{color:red}</style></head>
<body><nav>导航栏</nav><div class="content"><p>第一段正文内容，用于测试抽取是否正常。</p>
<h2>小标题</h2><p>第二段正文内容，这里也应该被保留下来。</p></div>
<footer>页脚版权信息</footer></body></html>"""
h = _HtmlText()
h.feed(html)
h.close()
text = h.text()
check("抽取到标题", h.title.strip() == "测试标题", h.title)
check("抽到正文", "第一段正文内容" in text and "第二段正文内容" in text, text)
check("丢掉 script/style", "var x=1" not in text and "color:red" not in text, text)
check("丢掉 nav/footer", "导航栏" not in text and "页脚版权信息" not in text, text)

print("\n=== 4b. 抓取兜底：本地优先 / Jina 401 不再吞掉本地结果 ===")
from plugins.AutoSummary import main as _asmod  # noqa: E402

class _Resp:
    def __init__(self, status, text="", content=b"", enc="utf-8"):
        self.status_code = status
        self.text = text
        self.content = content if content else text.encode("utf-8")
        self.encoding = enc
    @property
    def apparent_encoding(self):
        return "utf-8"

_p_fetch = AutoSummary()
_local_html = ("<html><head><title>本地标题</title></head><body>" +
               "<p>" + ("本地抓到的正文内容足够长可以总结。" * 4) + "</p></body></html>")

# 场景一：Jina 开启且返回 401，本地必须照常成功（旧实现的回归点）
_orig_req = _asmod.requests.get if _asmod.requests else None
_calls = []
def _fake_get(url, **kw):
    _calls.append(url)
    if str(url).startswith("https://r.jina.ai/"):
        return _Resp(401, "Unauthorized")
    return _Resp(200, _local_html)
if _asmod.requests is not None:
    _asmod.requests.get = _fake_get
    try:
        c = mk("/总结 https://example.com/a", jina_enabled=True)
        content, title = _p_fetch._fetch(c, "https://example.com/a")
        check("Jina 401 时本地兜底成功", "本地抓到的正文" in content, content[:80])
        check("Jina 401 不影响标题", "本地标题" in (title or ""), title)
    finally:
        _asmod.requests.get = _orig_req
else:
    check("requests 可用（跳过抓取兜底测试）", False)

# 场景二：本地失败 + Jina 成功时，仍能拿到 Jina 结果
def _fake_get2(url, **kw):
    if str(url).startswith("https://r.jina.ai/"):
        return _Resp(200, "Jina 抓到的正文内容足够长可以总结。" * 4)
    return _Resp(403, "Forbidden")
if _asmod.requests is not None:
    _asmod.requests.get = _fake_get2
    try:
        c = mk("/总结 https://example.com/b", jina_enabled=True)
        content, _ = _p_fetch._fetch(c, "https://example.com/b")
        check("本地失败后 Jina 兜底成功", "Jina 抓到的正文" in content, content[:80])
    finally:
        _asmod.requests.get = _orig_req

# 场景三：默认配置（jina_enabled 缺省）不打 Jina
_calls.clear()
def _fake_get3(url, **kw):
    _calls.append(url)
    return _Resp(200, _local_html)
if _asmod.requests is not None:
    _asmod.requests.get = _fake_get3
    try:
        c = mk("/总结 https://example.com/c")  # 不设 jina_enabled
        _p_fetch._fetch(c, "https://example.com/c")
        check("默认不打 Jina（避免 401 噪音）",
              not any("jina.ai" in u for u in _calls), _calls)
    finally:
        _asmod.requests.get = _orig_req

# 微信文章容器抽取
_wx_html = ('<html><head><meta property="og:title" content="微信文章标题"></head><body>'
            '<nav>顶部导航噪声</nav><h1 id="activity-name">活动文章标题</h1>'
            '<div id="js_content"><p>' + ('微信正文容器里的内容足够长可以总结。' * 4) +
            '</p></div><div>底部推荐噪声</div></body></html>')
_wx_body, _wx_title = _asmod._extract_wechat(_wx_html)
check("微信 #js_content 正文抽取", "微信正文容器" in _wx_body, _wx_body[:80])
check("微信正文排除容器外噪声", "顶部导航噪声" not in _wx_body and "底部推荐噪声" not in _wx_body)
check("微信标题抽取", "活动文章标题" in (_wx_title or ""), _wx_title)
check("普通网页不误判为微信", _asmod._extract_wechat("<html><body><p>普通</p></body></html>") == ("", ""))

print("\n=== 5. URL 黑白名单 ===")
ctx = mk("/总结 https://example.com/x", black_url_list=["https://example.com"])
check("黑名单 URL 被拦", not p._url_allowed(ctx, "https://example.com/x"))
ctx = mk("/总结 https://other.com/x", white_url_list=["https://other.com"])
check("白名单内放行", p._url_allowed(ctx, "https://other.com/x"))
ctx = mk("/总结 https://nope.com/x", white_url_list=["https://other.com"])
check("白名单非空时其它域名被拦", not p._url_allowed(ctx, "https://nope.com/x"))
ctx = mk("/总结 ftp://example.com/x")
check("非 http(s) 拦下", not p._url_allowed(ctx, "ftp://example.com/x"))

print("\n=== 6. 会话范围判定 ===")
p2 = AutoSummary()
ctx = mk(chat="999@chatroom", sender="wxid_a")
import store as _store  # noqa: E402  (真实 store 需要数据库，失败则按放行处理)
check("范围判定不抛异常", isinstance(p2._scope_ok(ctx), bool))

print("\n=== 7. 总结 + 追问闭环 ===")
p3 = AutoSummary()
p3._fetch = lambda ctx, url: ("这是抓到的网页正文，里面有门店获客成本上升的结论。", "美业获客")
ctx = mk("/总结 https://example.com/post")
p3._summarize = lambda ctx, content, title, question: "【摘要】门店获客成本上升。"
r = p3._run(ctx, {"kind": "summary", "url": "https://example.com/post", "title": "",
                   "question": "", "auto": False})
check("总结流程返回 STOP_ALL", r == STOP_ALL, r)
check("总结流程发出了回复", len(ctx.replies) == 1, ctx.replies)
check("总结后写入了原文缓存",
      p3._summaries.get("123@chatroom", {}).get("url") == "https://example.com/post",
      list(p3._summaries))

ctx2 = mk("/问 主要说了什么")
p3._ask = lambda ctx, content, question: "主要讲了获客成本上升。"
r2 = p3._run(ctx2, {"kind": "qa", "question": "主要说了什么", "auto": False})
check("追问命中缓存并回复", r2 == STOP_ALL and len(ctx2.replies) == 1, ctx2.replies)
check("追问答案正确", ctx2.replies and "获客成本上升" in ctx2.replies[0], ctx2.replies)

ctx3 = mk("/问 主要说了什么", chat="other@chatroom")
r3 = p3._run(ctx3, {"kind": "qa", "question": "主要说了什么", "auto": False})
check("无缓存时追问给出提示", r3 == STOP_ALL and ctx3.replies, ctx3.replies)

print("\n=== 8. 大模型调用参数 ===")
p4 = AutoSummary()
ctx4 = mk()
p4._chat(ctx4, "系统提示", "用户提示", {"llm_model": "gpt-4o", "llm_temperature": 0.2})
call = ctx4.calls[0]
check("调用了 llm_chat", len(ctx4.calls) == 1)
check("传了 system_prompt", call["kw"].get("system_prompt") == "系统提示", call["kw"])
check("传了 model", call["kw"].get("model") == "gpt-4o", call["kw"])
check("传了 temperature", call["kw"].get("temperature") == 0.2, call["kw"])
check("messages 只有 user", len(call["messages"]) == 1
      and call["messages"][0]["role"] == "user")

ctx5 = mk()
p4._chat(ctx5, "s", "u", {})
kw5 = ctx5.calls[0]["kw"]
check("未配 model 时不传 model", "model" not in kw5, kw5)
check("未配温度时不传 temperature", "temperature" not in kw5, kw5)

print("\n=== 9. 缓存过期清理 ===")
p6 = AutoSummary()
p6._recent_urls["c1"] = {"url": "u", "ts": 0}
p6._summaries["c1"] = {"content": "c", "ts": 0}
p6._clean_expired(mk())
check("过期缓存被清掉", not p6._recent_urls and not p6._summaries)

p6._recent_urls["c2"] = {"url": "u", "ts": __import__("time").time()}
p6._clean_expired(mk())
check("未过期缓存保留", "c2" in p6._recent_urls)

print("\n=== 9b. 最近链接缓存（auto_summary=false 也要记） ===")
p7 = AutoSummary()
# 用户先发裸链接，auto_summary 关闭：detect 返回 None，但链接必须进缓存
ctx = mk("https://example.com/cached", auto_summary=False)
act = p7._detect(ctx)
check("auto_summary=false 裸链接不自动总结", act is None, act)
check("但裸链接仍写入最近缓存",
      p7._recent_urls.get("123@chatroom", {}).get("url") == "https://example.com/cached",
      p7._recent_urls)
# 再敲「总结」不带链接 → 从缓存取回并完成总结
p7._fetch = lambda ctx, url: ("缓存链接的正文内容，足够长了可以总结出东西来。", "缓存标题")
p7._summarize = lambda ctx, content, title, question: "【缓存摘要】"
ctx2 = mk("/总结")
r = p7._run(ctx2, p7._detect(ctx2))
check("「总结」不带链接取回缓存并总结",
      r == STOP_ALL and ctx2.replies and "缓存摘要" in ctx2.replies[0],
      (r, ctx2.replies))
# auto_summary=false 时卡片也要入缓存
p7b = AutoSummary()
ctx3 = mk(content=card_xml, auto_summary=False)
p7b._detect(ctx3)
check("auto_summary=false 卡片链接也写入缓存",
      p7b._recent_urls.get("123@chatroom", {}).get("url", "").startswith("https://mp.weixin.qq.com"),
      p7b._recent_urls)

print("\n=== 9c. 插件回复不唤醒续聊（不带提示、不刷新窗口） ===")
p8 = AutoSummary()
p8._fetch = lambda ctx, url: ("这是足够长的网页正文内容，可以拿来总结出门店经营的关键结论。", "标题")
p8._summarize = lambda ctx, content, title, question: "【摘要内容】"
ctx = mk("/总结 https://example.com/hint")
p8._run(ctx, p8._detect(ctx))
check("摘要回复不带 with_hint", ctx.reply_kw and ctx.reply_kw[-1].get("with_hint") is False, ctx.reply_kw)
check("摘要回复默认 mark_followup=False",
      ctx.reply_kw and ctx.reply_kw[-1].get("mark_followup") is False, ctx.reply_kw)
# 追问答案同样不唤醒
p8._ask = lambda ctx, content, question: "【追问答案】"
ctx2 = mk("/追问 主要说什么")
p8._run(ctx2, p8._detect(ctx2))
check("追问答案也不带 with_hint",
      ctx2.reply_kw and ctx2.reply_kw[-1].get("with_hint") is False, ctx2.reply_kw)
check("追问答案默认 mark_followup=False",
      ctx2.reply_kw and ctx2.reply_kw[-1].get("mark_followup") is False, ctx2.reply_kw)
# 插件不再读取 followup_hint 这个配置项
ctx4 = mk("/总结 https://example.com/hint2", followup_hint=True)
p8._run(ctx4, p8._detect(ctx4))
check("followup_hint=true 也不带提示（配置项已移除）",
      ctx4.reply_kw and ctx4.reply_kw[-1].get("with_hint") is False, ctx4.reply_kw)

print("\n=== 10. 插件注册表加载 ===")
import logging  # noqa: E402
import tempfile  # noqa: E402
import shutil  # noqa: E402
import pluginhost  # noqa: E402
_reg_log = logging.getLogger("verify")
_reg_log.addHandler(logging.NullHandler())
_reg_log.setLevel(logging.CRITICAL)
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# enabled 现在是权威开关（私有配置 enabled:false 时不加载）。用临时 config_dir
# 写一份 enabled:true 的配置来验证正常加载路径；再单独验证 false 不加载。
_tmp_cfgdir = tempfile.mkdtemp(prefix="autosum_reg_")
try:
    with open(os.path.join(_tmp_cfgdir, "AutoSummary.yaml"), "w", encoding="utf-8") as f:
        f.write("enabled: true\n")
    reg = pluginhost.load_registry({"enabled": True, "dir": "plugins",
                                    "config_dir": _tmp_cfgdir},
                                   base_dir=_root, host=types.SimpleNamespace(),
                                   log=_reg_log, state=pluginhost.MemState())
    info = reg.describe()
    plugins = info.get("plugins") or []
    names = sorted(x.get("name") for x in plugins if isinstance(x, dict))
    check("AutoSummary 被注册", "AutoSummary" in names, (names, reg.errors))
    check("ExamplePlugin 仍正常", "ExamplePlugin" in names, (names, reg.errors))
    check("注册表无致命错误", not getattr(reg, "fatal", False), reg.errors)
    if "AutoSummary" in names:
        meta = next(x for x in plugins if x.get("name") == "AutoSummary")
        check("注册了 on_message / on_post_resolve",
              "on_message" in (meta.get("hooks") or [])
              and "on_post_resolve" in (meta.get("hooks") or []), meta.get("hooks"))
        check("优先级为 80", meta.get("priority") == 80, meta.get("priority"))
finally:
    shutil.rmtree(_tmp_cfgdir, ignore_errors=True)

# enabled:false 的插件应被跳过加载、后台显示禁用（问题1的核心行为）
_tmp_off = tempfile.mkdtemp(prefix="autosum_off_")
try:
    with open(os.path.join(_tmp_off, "AutoSummary.yaml"), "w", encoding="utf-8") as f:
        f.write("enabled: false\n")
    reg_off = pluginhost.load_registry({"enabled": True, "dir": "plugins",
                                        "config_dir": _tmp_off},
                                       base_dir=_root, host=types.SimpleNamespace(),
                                       log=_reg_log, state=pluginhost.MemState())
    off_meta = next((x for x in reg_off.describe().get("plugins") or []
                     if x.get("name") == "AutoSummary"), {})
    check("enabled=false 时插件不加载", off_meta.get("enabled") is False, off_meta)
    check("enabled=false 时后台显示原因", "enabled" in (off_meta.get("disabled_reason") or ""),
          off_meta.get("disabled_reason"))
finally:
    shutil.rmtree(_tmp_off, ignore_errors=True)

print("\n=== 11. 真实链路：插件回复不刷新续聊窗口 ===")
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp_cfg = os.path.join(_root, "data", "config.yaml")
_created_cfg = not os.path.exists(_tmp_cfg)
if _created_cfg:
    with open(os.path.join(_root, "data", "config.yaml.example"), encoding="utf-8") as _f:
        with open(_tmp_cfg, "w", encoding="utf-8") as _o:
            _o.write(_f.read())
try:
    import webhook_server as W  # noqa: E402
    import followup as FU  # noqa: E402

    _sent = []

    class _FakeWX:
        def send_text(self, chat, text, **k):
            _sent.append(text)
            return True

        def send_quote(self, chat, text, *a, **k):
            _sent.append(text)
            return True

    W.WX = _FakeWX()
    _m = W.policy.normalize({"chat": "t@@chatroom", "chatType": "group",
                             "sender": "wxid_a", "localID": 1,
                             "text": "总结 https://a.com", "type": 1})
    _match = {"chat_type": "group", "policy_id": None,
              "policy_name": "", "trigger": ""}
    _ctx = pluginhost.PluginContext(m=_m, kind="text", host=W.PLUGIN_HOST,
                                    log=_reg_log, registry=W.PLUGIN_REGISTRY)

    FU.clear()
    check("初始无续聊窗口", not FU.check(_m)["ok"], FU.check(_m))
    _ctx.reply("【摘要内容】")
    check("插件 reply 后仍无续聊窗口（不刷新）", not FU.check(_m)["ok"], FU.check(_m))
    check("插件回复文本不带续聊提示", _sent[-1] == "【摘要内容】", _sent[-1])

    # 主链路不能误伤：不传 mark_followup 时仍刷新
    FU.clear()
    W._send_text(_m, _match, "AI 实质回复")
    check("主链路回复仍刷新续聊窗口（未误伤）", FU.check(_m)["ok"], FU.check(_m))

    # 插件确实要续聊时可显式 opt-in
    FU.clear()
    _ctx.reply("要续聊的回复", mark_followup=True)
    check("插件显式 mark_followup=True 时刷新", FU.check(_m)["ok"], FU.check(_m))
finally:
    if _created_cfg and os.path.exists(_tmp_cfg):
        os.remove(_tmp_cfg)

print("\n" + "=" * 50)
print("通过 %d 项，失败 %d 项" % (len(OK), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部通过 ✅")
