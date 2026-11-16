"""AutoSummary：自动总结微信里的网页链接与卡片消息。

对标 xxxbot-pad 的 AutoSummaryEher，但有三点刻意不同：

1. **不接 Dify API**，改用宿主**内置大模型**：ctx.llm_chat() → brain.chat_only()。
   直接复用后台「AI 大模型」页选定的模型，换模型不用改插件、不用重启。
2. **不读 config.toml**，改用宿主规范的私有配置 data/plugins/AutoSummary.yaml
   （mtime 热读，改完最多 30 秒生效，不用重启主服务）。
3. **不引入新依赖**：正文提取用标准库 html.parser，不装 beautifulsoup
   （插件跑在宿主进程里，不会自动 pip 装依赖，装了等于让线上直接崩）。

用法（都是微信文本消息）：
  总结 https://example.com/a   抓正文 → 内置大模型总结
  总结 这篇讲了什么 <url>       带上自定义问题，只针对该问题作答
  总结                         总结本会话上一条链接
  追问 这篇的核心观点是什么      针对刚总结的原文追问（可连续追问）
  卡片消息（公众号文章等）        解析标题/链接 → 总结
  裸链接                        自动总结（auto_summary 开启时）

私有配置见同目录 config.example.yaml，拷到 data/plugins/AutoSummary.yaml 生效。
"""
import html
import re
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

try:
    import requests
except ImportError:      # pragma: no cover - 宿主 requirements 里一定有
    requests = None

from pluginhost import (CONTINUE, STOP_ALL, PluginBase, on_message,
                        on_post_resolve)


# ------------------------------------------------------------------------ 提示词
# 微信里看到的是纯文本：# * - _ 这些 markdown 符号原样显示，很脏。
SUMMARY_SYSTEM = (
    "你是一名专业的内容摘要助手，服务于微信客服场景。\n"
    "规则：\n"
    "1. 只依据用户给出的原文概括，不添加原文没有的信息，不编造数据与出处。\n"
    "2. 输出纯文本，不要用 # * - _ ` 等 markdown 符号，也不要用表格。\n"
    "3. 结构固定为四段，顺序不变，整体务必精简：\n"
    "   第一行直接写标题（用原文标题，没有就用原文第一句概括），不要写「标题」二字；\n"
    "   然后空一行写「📖 总结」：一句话概括核心内容，不超过 60 字；\n"
    "   然后空一行写「💡 关键要点」：只列 2 条，用 1. 2. 分行，每条不超过 25 字；\n"
    "   最后一行写「🏷 标签」：2 个以 # 开头的标签。\n"
    "4. 原文信息不足时如实说明，不要硬凑要点，也不要为了凑结构重复表述。"
)

QA_SYSTEM = (
    "你是一名专业的内容分析助手，服务于微信客服场景。\n"
    "规则：\n"
    "1. 只依据用户给出的原文回答，不添加原文没有的信息，不编造。\n"
    "2. 输出纯文本，不要用 # * - _ ` 等 markdown 符号。\n"
    "3. 直接给答案，越简短越好，不超过 80 字；需要列举时用 1. 2. 分行。\n"
    "4. 原文里没有依据时，直接说原文没有提到，不要猜。"
)


class _HtmlText(HTMLParser):
    """把 HTML 抽成纯文本（标准库实现，不依赖 beautifulsoup）。

    丢掉 script/style/nav/header/footer 这些噪声块，
    其余按块级标签断行，最后把空白压平、去空行。
    """

    SKIP = {"script", "style", "nav", "header", "footer", "aside", "form",
            "iframe", "noscript", "svg", "canvas", "button", "select",
            "textarea", "video", "audio"}
    BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "div", "section",
             "article", "main", "br", "tr", "blockquote", "pre", "td"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._parts = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self.SKIP:
            self._skip += 1
        elif t == "title":
            self._in_title = True
        elif t in self.BLOCK and not self._skip:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif t == "title":
            self._in_title = False
        elif t in self.BLOCK and not self._skip:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        s = data.strip()
        if s:
            self._parts.append(s)

    def text(self):
        out = []
        for raw in "".join(self._parts).splitlines():
            s = " ".join(raw.split())
            if s:
                out.append(s)
        return "\n".join(out)


class AutoSummary(PluginBase):
    name = "AutoSummary"
    description = "自动总结微信里的网页链接与卡片消息（内置大模型）"
    author = "WeChatBot"
    version = "1.0.0"

    # URL 正则：允许中文路径（微信文章、维基条目都可能是中文），
    # 但遇到空白、引号、中文标点就停 —— 这些字符不可能出现在正常链接里。
    # 结尾多余的 . , ) 等再单独剥掉（"(见链接)" 这种括号会被误吞）。
    URL_RE = re.compile(r"https?://[^\s\"'<>，。；：！？、（）【】《》“”‘’]+")
    # 结尾要剥掉的标点；注意右侧再加一层 strip()，把空白也一起清干净。
    TRAIL = ".,;:!?)]}'\"、，。；：！？）】》"
    # 微信文本里偶尔混进零宽空格 / 不间断空格，普通 strip() 清不掉，
    # 会让「触发词后面跟空格」看起来像链接提取失败（URL 前多了个隐形字符）。
    INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\xa0"))

    # ------------------------------------------------------------ 生命周期
    def __init__(self):
        super().__init__()
        # 会话级缓存：{chat: {...}}。进程内内存，重载插件即清空（可接受）。
        self._recent_urls = {}     # 最近一条链接，供「/总结」不带 URL 时使用
        self._summaries = {}       # 最近一次总结（原文 + 摘要），供追问使用

    def on_load(self):
        # 生命周期里没有 ctx，logger 要从 self.api 拿（handler 里则是 ctx.log）
        self.api.log.info("AutoSummary 已加载 | 内置大模型总结，不依赖外部 AI 服务")

    def on_disable(self):
        # 纯内存状态，清掉即可释放引用
        self._recent_urls.clear()
        self._summaries.clear()

    # ------------------------------------------------------------ 消息入口
    @on_message(priority=70, kinds=("text", "other"))
    def detect(self, ctx):
        """识别触发动作。

        在 on_message 里做识别（而不是 on_post_resolve），是因为本插件要接住
        「没 @ 机器人、只丢了个链接」这种消息 —— 等策略判定完这类消息早被
        _skip 掉了。真正动手前会用 _scope_ok() 核对会话是否在宿主部署范围内，
        避免在没部署的群里越权发言。
        """
        try:
            if not self._plugin_enabled(ctx):
                return CONTINUE
            action = self._detect(ctx)
            if not action:
                return CONTINUE
            if not self._scope_ok(ctx):
                return CONTINUE
            if self._require_host_trigger(ctx):
                # 严格模式：等宿主策略放行（on_post_resolve）再动手
                ctx.state["autosum_action"] = action
                return CONTINUE
            return self._run(ctx, action)
        except Exception as e:
            ctx.log.exception("AutoSummary 识别阶段异常（已忽略）: %s", e)
            return CONTINUE

    @on_post_resolve(priority=70, kinds=("text", "other"))
    def gated(self, ctx):
        """严格模式下的执行点：宿主策略已判定这条消息「该回」。"""
        action = ctx.state.get("autosum_action")
        if not action:
            return CONTINUE
        try:
            return self._run(ctx, action)
        except Exception as e:
            ctx.log.exception("AutoSummary 执行阶段异常（已忽略）: %s", e)
            return CONTINUE

    # ------------------------------------------------------------ 识别
    def _detect(self, ctx):
        """返回动作 dict，或 None 表示「这条消息跟本插件无关」。

        动作格式：
          {"kind": "summary", "url": ..., "title": ..., "question": ..., "auto": bool}
          {"kind": "qa", "question": ..., "auto": bool}
        """
        m = ctx.m
        # 先清掉微信偶尔混进来的零宽 / 不间断空格，否则「/总结\u200bhttps://…」
        # 这类输入会让触发词后面多出隐形字符，看起来就像链接提取不到。
        text = str(m.get("text") or "").translate(self.INVISIBLE).strip()
        cfg = ctx.config or {}
        self._clean_expired(ctx)

        # ① 追问：「/追问 xxx」/「/问 xxx」
        #    只有本会话刚总结过东西时才认这条触发词 —— 否则「问一下这个政策」
        #    会被插件劫持去回答上一篇文章，而不是正常问 AI。
        q = self._match_prefix(text, self._triggers(cfg, "qa_triggers"))
        if q is not None:
            question = self._strip_trigger(text, q)
            if question is None:
                # 「问题…」「请问…」这类常用词不是追问，往下继续走其它识别
                pass
            elif not question:
                return {"kind": "qa", "question": "", "auto": False}
            elif self._summaries.get(m.get("chat")):
                return {"kind": "qa", "question": question, "auto": False}
            # 没有可追问的原文 → 不劫持这条消息，让 AI 正常回答

        # ② 显式总结：「/总结 [自定义问题] <url>」
        #    关键：从触发词之后的内容里取 URL，且去掉触发词与 URL 之间的空格 / 冒号 /
        #    零宽字符，避免「/总结  https://…」这类带空格输入取不到链接。
        trig = self._match_prefix(text, self._triggers(cfg, "summary_triggers"))
        if trig is not None:
            rest = text[len(trig):].strip()
            url = self._first_url(rest)
            question = ""
            if url:
                # 触发词与链接之间的文字就是自定义问题；去掉各种空白与分隔标点
                question = rest.split(url)[0].strip(" 　:：,，、")
            return {"kind": "summary", "url": url, "title": "", "question": question,
                    "auto": False}

        # 卡片消息（公众号文章 / 小程序卡片等）：无论 auto_summary 开关都解析。
        # 裸链接 / 卡片只要出现就记入「最近链接」，否则 auto_summary=false 时
        # 用户先发链接再敲「/总结」会永远取不到缓存（历史 bug）。
        card = self._parse_card(m)
        card_url = ""
        if card:
            card_url = str(card.get("url") or "").strip()
            if card_url:
                self._remember_url(ctx, card_url, card.get("title") or "")

        # ③④ 自动总结：只在 auto_summary 打开时才生效
        if not self._bool(cfg, "auto_summary", True):
            # 关掉自动总结也别漏掉裸链接的缓存——「/总结」不带链接时要能取回
            url = self._first_url(text)
            if url:
                self._remember_url(ctx, url, "")
            return None

        if card:
            return {"kind": "summary", "url": card_url,
                    "title": card.get("title") or "",
                    "desc": card.get("desc") or "",
                    "question": "", "auto": True}

        # ④ 裸链接
        url = self._first_url(text)
        if url:
            self._remember_url(ctx, url, "")
            return {"kind": "summary", "url": url, "title": "", "question": "",
                    "auto": True}
        return None

    def _strip_trigger(self, text, trig):
        """取触发词后面的正文。

        「/问」单独做前缀时要避开「/问题…」这类词，
        否则一句「/问题来了」会被当成追问，把上一篇文章的缓存搅乱。
        返回 None 表示「这个触发词其实是误命中」。
        """
        rest = text[len(trig):].strip()
        if trig.endswith("问") and (rest.startswith("题") or rest.startswith("候")):
            return None
        # 允许「/问：xxx」「/问,xxx」「/问 xxx」
        return rest.lstrip(" 　:：,，.。!！?？").strip()

    def _match_prefix(self, text, triggers):
        """text 以某个触发词开头就返回该触发词，否则 None。"""
        if not text:
            return None
        low = text.lower()
        for t in triggers:
            t = str(t or "").strip()
            if not t:
                continue
            if low.startswith(t.lower()):
                return t
        return None

    def _triggers(self, cfg, key):
        """取触发词列表；未配置时用内置默认。"""
        raw = cfg.get(key)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            raw = []
        out = [str(x).strip() for x in raw if str(x or "").strip()]
        if not out:
            out = list(DEFAULT_TRIGGERS.get(key, []))
        # 长触发词优先（"帮我总结" 要排在 "总结" 前面）
        out.sort(key=len, reverse=True)
        return out

    # ------------------------------------------------------------ 执行
    def _run(self, ctx, action):
        """真正干活：抓正文 → 内置大模型总结 → 回复。"""
        m = ctx.m
        auto = bool(action.get("auto"))
        cfg = ctx.config or {}

        if action.get("kind") == "qa":
            return self._run_qa(ctx, action, auto)

        url = str(action.get("url") or "").strip()
        if not url:
            # 「/总结」不带 URL → 用本会话最近一条链接
            cached = self._recent_urls.get(m.get("chat"))
            if not cached:
                if auto:
                    return CONTINUE
                ctx.reply("请把要总结的链接发给我，例如：总结 https://example.com/article")
                return STOP_ALL
            url = cached.get("url") or ""
            action = dict(action)
            action.setdefault("title", cached.get("title") or "")

        if not self._url_allowed(ctx, url):
            ctx.log.info("链接被黑白名单拦下 | %s", url[:120])
            if auto:
                return CONTINUE
            ctx.reply("这个链接不在我的总结范围内～")
            return STOP_ALL

        try:
            content, title = self._fetch(ctx, url)
        except Exception as e:
            ctx.log.warning("抓取正文失败 | %s | %s", url[:120], repr(e)[:160])
            content, title = "", ""
        if not content:
            if auto:
                # 自动模式下失败保持安静：群里每条链接都回一句「抓取失败」太吵
                ctx.log.info("正文抓取为空，自动模式不回复 | %s", url[:120])
                return CONTINUE
            ctx.reply("这个链接的内容我没能抓下来，麻烦换个链接试试～")
            return STOP_ALL

        title = title or action.get("title") or ""
        summary = self._summarize(ctx, content, title, action.get("question") or "")
        if not summary:
            if auto:
                ctx.log.warning("内置大模型未返回内容，自动模式不回复 | %s", url[:120])
                return CONTINUE
            ctx.reply("这次没能总结出来，稍后再试试～")
            return STOP_ALL

        # 缓存：最近链接（给「/总结」用）+ 原文与摘要（给追问用）
        chat = m.get("chat")
        now = time.time()
        self._recent_urls[chat] = {"url": url, "title": title, "ts": now}
        self._summaries[chat] = {"url": url, "title": title, "content": content,
                                  "summary": summary, "ts": now}

        ctx.reply(summary[:self._max_reply(cfg)])
        return STOP_ALL

    def _run_qa(self, ctx, action, auto):
        """追问：拿上一次总结的原文，问内置大模型。"""
        chat = ctx.m.get("chat")
        cached = self._summaries.get(chat)
        question = str(action.get("question") or "").strip()
        if not question:
            ctx.reply("想问什么？直接写出来就行～")
            return STOP_ALL
        if not cached:
            if auto:
                return CONTINUE
            ctx.reply("我这边还没有刚总结过的内容，先发个链接让我总结一下吧～")
            return STOP_ALL
        try:
            answer = self._ask(ctx, cached.get("content") or "", question)
        except Exception as e:
            ctx.log.warning("追问调用大模型失败 | %s", repr(e)[:160])
            answer = ""
        if not answer:
            ctx.reply("这个问题我暂时答不上来，换个问法试试～")
            return STOP_ALL
        # 追问成功 → 续上时间戳，让追问窗口不被自然过期打断
        cached["ts"] = time.time()
        ctx.reply(answer[:self._max_reply(ctx.config or {})])
        return STOP_ALL

    # ------------------------------------------------------------ 大模型
    def _summarize(self, ctx, content, title, question):
        """总结 / 定向提问，都走宿主内置大模型。"""
        cfg = ctx.config or {}
        limit = self._int(cfg, "max_text_length", 6000)
        body = content[:limit]
        head = []
        if title:
            head.append("原文标题：%s" % title)
        head.append("原文内容：\n%s" % body)
        src = "\n\n".join(head)
        if question:
            user = ("请针对下面的原文回答这个问题，回答要简明，不要重复原文：%s\n\n%s"
                    % (question, src))
            system = QA_SYSTEM
        else:
            user = ("请按「标题 / 📖 总结 / 💡 关键要点 / 🏷 标签」四段输出摘要。\n\n%s"
                    % src)
            system = SUMMARY_SYSTEM
        return self._chat(ctx, system, user, cfg)

    def _ask(self, ctx, content, question):
        cfg = ctx.config or {}
        limit = self._int(cfg, "max_text_length", 6000)
        user = ("原文内容：\n%s\n\n问题：%s" % (content[:limit], question))
        return self._chat(ctx, QA_SYSTEM, user, cfg)

    def _chat(self, ctx, system, user, cfg):
        """调用宿主内置大模型。

        ctx.llm_chat() 走 brain.chat_only()：不检索知识库、messages 原样发给模型，
        失败直接抛异常（不返回兜底话术）—— 插件必须能分清「模型没答上来」
        和「模型答上来了」，把失败当成功会把错误内容缓存下来。
        """
        kw = {}
        model = str(cfg.get("llm_model") or "").strip()
        if model:
            kw["model"] = model
        temp = cfg.get("llm_temperature")
        if temp is not None:
            try:
                kw["temperature"] = float(temp)
            except (TypeError, ValueError):
                pass
        mx = self._int(cfg, "llm_max_tokens", 0)
        if mx > 0:
            kw["max_tokens"] = mx
        text = ctx.llm_chat([{"role": "user", "content": user}],
                            system_prompt=system, **kw)
        return str(text or "").strip()

    # ------------------------------------------------------------ 抓正文
    def _fetch(self, ctx, url):
        """抓正文，返回 (正文, 标题)。

        本地抓取为主路径：Jina Reader 免费额度已基本取消，无 key 时 401 是常态，
        所以默认不再先走它。本地抓不到时，若显式开启了 jina_enabled 再走 Jina 兜底。
        两路各自的异常都在这里吞掉并继续下一路，绝不因为某一路抛异常就跳过兜底。
        """
        cfg = ctx.config or {}
        if requests is None:
            raise RuntimeError("宿主缺少 requests 依赖")

        errors = []
        # ① 本地抓取（主路径，覆盖微信文章 / 公众号 / 普通网页）
        try:
            content, title = self._fetch_local(ctx, url, cfg)
            if content:
                return content, title
            errors.append("本地正文为空")
        except Exception as e:
            errors.append("本地: %s" % repr(e)[:120])

        # ② Jina 兜底（默认关闭；填了 key 或显式打开才有意义）
        if self._bool(cfg, "jina_enabled", False):
            try:
                content, title = self._fetch_jina(ctx, url, cfg)
                if content:
                    return content, title
                errors.append("Jina 正文为空")
            except Exception as e:
                errors.append("Jina: %s" % repr(e)[:120])

        raise RuntimeError("; ".join(errors) or "正文抓取失败")

    def _fetch_jina(self, ctx, url, cfg):
        base = str(cfg.get("jina_base") or "https://r.jina.ai/").strip()
        if not base.endswith("/"):
            base += "/"
        # 微信文章带一长串 query，直接拼会被截断，整段编码更稳
        target = url
        if "mp.weixin.qq.com" in url:
            target = _quote_all(url)
        headers = {"User-Agent": _UA,
                   "Accept": "text/plain, text/markdown, */*"}
        key = str(cfg.get("jina_api_key") or "").strip()
        if key:
            headers["Authorization"] = "Bearer %s" % key
        resp = requests.get(base + target, headers=headers,
                            timeout=self._timeouts(cfg))
        if resp.status_code != 200:
            raise RuntimeError("Jina HTTP %s" % resp.status_code)
        text = resp.text or ""
        # 微信被风控时会返回「环境异常」而不是正文
        if "环境异常" in text:
            raise RuntimeError("Jina 返回「环境异常」（微信风控）")
        if not text.strip():
            raise RuntimeError("Jina 返回空内容")
        return text.strip(), ""

    def _fetch_local(self, ctx, url, cfg):
        headers = {"User-Agent": _UA,
                   "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                   "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        resp = requests.get(url, headers=headers, timeout=self._timeouts(cfg),
                            allow_redirects=True)
        if resp.status_code != 200:
            raise RuntimeError("HTTP %s" % resp.status_code)
        raw = resp.content
        # 编码猜不准时退回 utf-8，避免中文全是乱码
        enc = resp.encoding
        if not enc or enc.lower() in ("iso-8859-1", "ascii"):
            enc = resp.apparent_encoding or "utf-8"
        try:
            text = raw.decode(enc, "replace")
        except (LookupError, UnicodeDecodeError):
            text = raw.decode("utf-8", "replace")

        # 微信文章：优先取 #js_content 正文容器，标题取 #activity-name / og:title。
        # 容器抓取失败就退回整页解析，不让解析差异导致整篇丢失。
        body, container_title = _extract_wechat(text)
        if not body:
            parser = _HtmlText()
            try:
                parser.feed(text)
                parser.close()
            except Exception:
                pass
            body = parser.text()
            container_title = container_title or (parser.title or "").strip()
        else:
            container_title = container_title or _meta_title(text)

        # 正文很短也可能是真短文（如一段公告），20 字以下才判失败。
        if len(body) < 20:
            raise RuntimeError("正文过短（%d 字）" % len(body))
        return body, container_title

    def _timeouts(self, cfg):
        connect = float(self._int(cfg, "fetch_connect_timeout", 5) or 5)
        read = float(self._int(cfg, "fetch_read_timeout", 20) or 20)
        return (connect, read)

    # ------------------------------------------------------------ 卡片解析
    def _parse_card(self, m):
        """解析微信卡片（appmsg）XML，取出标题 / 描述 / 链接。"""
        raw = str(m.get("content") or "").strip()
        if not raw.startswith("<"):
            raw = str(m.get("text") or "").strip()
        if not raw.startswith("<"):
            return None
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return None
        appmsg = root if root.tag == "appmsg" else root.find(".//appmsg")
        if appmsg is None:
            return None
        title = _node_text(appmsg, "title")
        desc = _node_text(appmsg, "des") or _node_text(appmsg, "des2")
        url = _node_text(appmsg, "url")
        if not url:
            return None
        return {"title": html.unescape(title).strip(),
                "desc": html.unescape(desc).strip(),
                "url": html.unescape(url).strip()}

    # ------------------------------------------------------------ 权限与范围
    def _plugin_enabled(self, ctx):
        cfg = ctx.config or {}
        return self._bool(cfg, "enabled", False)

    def _scope_ok(self, ctx):
        """会话是否允许本插件动手。

        两层控制：
        1. 插件自己的黑白名单（细粒度：按人、按群）；
        2. 宿主的部署范围（群必须在「群聊配置」表内、私聊必须有启用策略）——
           在没部署的群里总结，等于替机器人越权发言。
        插件白名单可以覆盖第 2 条：把群写进 white_group_list 就是显式授权。
        """
        m = ctx.m
        chat = str(m.get("chat") or "")
        sender = str(m.get("sender") or "")
        is_group = m.get("chat_type") == "group"
        cfg = ctx.config or {}

        if sender and sender in self._list(cfg.get("black_user_list")):
            return False
        if is_group and chat in self._list(cfg.get("black_group_list")):
            return False
        if is_group and chat in self._list(cfg.get("white_group_list")):
            return True
        if sender and sender in self._list(cfg.get("white_user_list")):
            return True
        if is_group:
            return self._host_group_ok(chat)
        return self._host_private_ok()

    def _host_group_ok(self, chat):
        """群是否在宿主的群聊配置表内。读不到配置就放行，不因为读不到就整个关掉。"""
        try:
            import store
            return bool(store.get_group_rule(chat))
        except Exception:
            return True

    def _host_private_ok(self):
        try:
            import store
            return bool(store.enabled_private_rules())
        except Exception:
            return True

    def _url_allowed(self, ctx, url):
        """URL 黑白名单（前缀匹配，和参考插件一致）。"""
        if not url.lower().startswith(("http://", "https://")):
            return False
        cfg = ctx.config or {}
        u = url.strip()
        white = self._list(cfg.get("white_url_list"))
        black = self._list(cfg.get("black_url_list"))
        if white and not any(u.startswith(w) for w in white):
            return False
        if any(u.startswith(b) for b in black):
            return False
        return True

    def _require_host_trigger(self, ctx):
        """严格模式：必须宿主策略也判定「该回」才动手。默认开启。"""
        return self._bool(ctx.config or {}, "require_host_trigger", True)

    # ------------------------------------------------------------ 缓存
    def _clean_expired(self, ctx):
        """清掉过期的缓存条目。"""
        ttl = float(self._int(ctx.config or {}, "expiration_time", 1800) or 1800)
        now = time.time()
        for bucket in (self._recent_urls, self._summaries):
            for k in list(bucket.keys()):
                if now - bucket[k].get("ts", 0) > ttl:
                    bucket.pop(k, None)

    # ------------------------------------------------------------ 缓存写入
    def _remember_url(self, ctx, url, title=""):
        """把消息里出现的链接记成「最近链接」，供「/总结」不带 URL 时取回。

        和 _run 成功后写的 _recent_urls 是同一个槽位；这里提前写，是为了让
        auto_summary=false（裸链接不自动总结）时也能记住用户刚发的链接。
        """
        url = self._first_url(url)
        if not url:
            return ""
        chat = ctx.m.get("chat")
        old = self._recent_urls.get(chat) or {}
        self._recent_urls[chat] = {"url": url,
                                   "title": str(title or "").strip() or old.get("title", ""),
                                   "ts": time.time()}
        return url

    # ------------------------------------------------------------ 小工具
    def _first_url(self, text):
        """从文本里取出第一条完整链接，去掉前后空白 / 零宽字符 / 尾部标点。"""
        s = str(text or "").translate(self.INVISIBLE)
        m = self.URL_RE.search(s)
        if not m:
            return ""
        return m.group(0).rstrip(self.TRAIL).strip()

    def _list(self, v):
        if isinstance(v, str):
            return [v.strip()] if v.strip() else []
        if isinstance(v, (list, tuple, set)):
            return [str(x).strip() for x in v if str(x or "").strip()]
        return []

    def _bool(self, cfg, key, default):
        v = cfg.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on", "是", "开")
        return bool(default)

    def _int(self, cfg, key, default):
        try:
            return int(cfg.get(key, default))
        except (TypeError, ValueError):
            return int(default)

    def _max_reply(self, cfg):
        n = self._int(cfg, "max_reply_chars", 1500)
        return n if n > 0 else 1500


# ---------------------------------------------------------------- 模块级常量
DEFAULT_TRIGGERS = {
    "summary_triggers": ["/总结", "/摘要", "/帮我总结", "/summarize", "/summary"],
    "qa_triggers": ["/追问", "/问"],
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _quote_all(url):
    """整段 URL 编码（微信文章链接的 query 太长，直接拼会被截断）。"""
    try:
        from urllib.parse import quote
        return quote(url, safe="")
    except Exception:
        return url


def _node_text(node, tag):
    """取子节点文本；找不到返回空串。"""
    try:
        el = node.find(tag)
        if el is None:
            return ""
        return (el.text or "").strip()
    except Exception:
        return ""


# 微信文章正文容器 / 标题的匹配式。微信把正文放在 #js_content，
# 导航、推荐、二维码等噪声都在容器外，只取容器能显著提升总结质量。
_DIV_OPEN_RE = re.compile(r"<div\b([^>]*)>", re.I)
_ID_RE = re.compile(r"\bid\s*=\s*[\"']([^\"']+)[\"']", re.I)
_META_TITLE_RE = re.compile(
    r'<meta[^>]*\bproperty=["\']og:title["\'][^>]*\bcontent=["\']([^"\']+)["\']',
    re.I)
_WECHAT_TITLE_RE = re.compile(
    r'<[^>]*\bid=["\']activity-name["\'][^>]*>(.*?)</', re.S | re.I)


def _meta_title(html_text):
    """从 og:title / <title> 取标题，作为正文容器抓取时的标题兜底。"""
    m = _META_TITLE_RE.search(html_text or "")
    if m:
        return html.unescape(m.group(1)).strip()
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text or "", re.S | re.I)
    return html.unescape(m.group(1)).strip() if m else ""


def _div_inner_html(html_text, target_id):
    """按 div 深度计数抽取 id=target_id 的容器内部 HTML。

    js_content 内常嵌套多层 div，正则非贪婪会在第一个 </div> 提前截断，
    所以这里手工扫描 div 开合标签、只在深度归零时结束。找不到返回 ""。
    """
    if not html_text:
        return ""
    start = -1
    depth = 0
    pos = 0
    token_re = re.compile(r"<(/?)div\b[^>]*>", re.I)
    # 先定位目标 div 的 opened 标签结束位置
    m_open = None
    for m_open in _DIV_OPEN_RE.finditer(html_text):
        idm = _ID_RE.search(m_open.group(1))
        if idm and idm.group(1).lower() == target_id.lower():
            start = m_open.end()
            depth = 1
            pos = m_open.end()
            break
    if start < 0:
        return ""
    for m in token_re.finditer(html_text, pos):
        if m.group(1):        # </div>
            depth -= 1
            if depth == 0:
                return html_text[start:m.start()]
        else:                 # <div ...>
            depth += 1
    return html_text[start:]  # 没找到闭合，返回到末尾


def _extract_wechat(html_text):
    """从微信文章 HTML 里抽 (正文, 标题)，抽不到返回 ("", "")。

    用标准库解析，不引入新依赖。只处理确实是微信文章（含 js_content 容器）的
    页面；普通网页直接返回空，交给调用方走整页解析。
    """
    if not html_text or "js_content" not in html_text:
        return "", ""
    title = ""
    tm = _WECHAT_TITLE_RE.search(html_text)
    if tm:
        title = html.unescape(re.sub(r"<[^>]+>", "", tm.group(1))).strip()
    inner = _div_inner_html(html_text, "js_content")
    if not inner:
        return "", title
    parser = _HtmlText()
    try:
        parser.feed(inner)
        parser.close()
    except Exception:
        return "", title
    return parser.text(), title
