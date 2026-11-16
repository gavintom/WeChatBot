"""每条消息一个 PluginContext：插件唯一能碰到的宿主能力门面。

**为什么不直接把 WX / BRAIN 交给插件**：直接调 WX.send_text() 会绕过
webhook_server._send_text() 里的 @ 规则、私聊硬保证、引用回复与 _event()
的消息流水，统计会错乱。续聊窗口由宿主统一控制（主链路刷新、插件回复
默认跳过），插件不要自己调 followup。所有发送必须经过 ctx.reply()。

ctx 在 process_message() 里创建一次、贯穿同一条消息的全部 hook，
插件可以用 ctx.state 在同一消息的不同阶段之间传递数据。
"""


class _PluginLogger:
    """给插件日志加插件名前缀；底层 logger 已被 _MSG_CTX 注入会话上下文。"""

    def __init__(self, log, ctx):
        self._log = log
        self._ctx = ctx

    def _call(self, level, msg, *a, **kw):
        f = getattr(self._log, level, None)
        if not callable(f):
            return
        prefix = "[插件:%s] " % (getattr(self._ctx, "plugin_name", "") or "-")
        try:
            f(prefix + str(msg), *a, **kw)
        except Exception:
            pass

    def debug(self, msg, *a, **kw):
        self._call("debug", msg, *a, **kw)

    def info(self, msg, *a, **kw):
        self._call("info", msg, *a, **kw)

    def warning(self, msg, *a, **kw):
        self._call("warning", msg, *a, **kw)

    def error(self, msg, *a, **kw):
        self._call("error", msg, *a, **kw)

    def exception(self, msg, *a, **kw):
        self._call("exception", msg, *a, **kw)


class ReadOnlyWX:
    """手机插件的**只读查询**能力，方法名与 v2 通讯录接口一一对应。

    刻意不暴露 send_* / set_webhook / activate / _req / license，
    也不暴露朋友圈的 like / comment 等真实社交动作：
    前者会绕过一致性约束，后者会改动手机端状态。

    返回值**直接透传插件文档第 6 章的结构，不做任何旧版包装**：
    `groups()` → `list[dict]`；`friends()` → `{"friends": [...], "count": n}`；
    `find_contact()` → `{mode, query, exact, count, truncated, matches[]}`。
    目前还没有已发布的插件，不需要为已下线的 `/api/contacts` 留兼容方法。
    """

    __slots__ = ("_wx",)

    def __init__(self, wx):
        self._wx = wx

    def health(self, timeout=None):
        return self._wx.health(timeout=timeout)

    def groups(self):
        """GET /api/contacts/groups → 群聊列表。"""
        return self._wx.groups()

    def friends(self, wxid=None, name=None):
        """GET /api/contacts/friends → 好友列表；带 wxid/name 时是单项反查。"""
        return self._wx.friends(wxid=wxid, name=name)

    def group_members(self, chat, wxid=None, name=None):
        """GET /api/contacts/groupmember → 群成员名单；带 wxid/name 时是成员反查。"""
        return self._wx.group_members(chat, wxid=wxid, name=name)

    def find_contact(self, wxid=None, name=None):
        """GET /api/contacts/find → 全量库统一反查（找不到不是错误）。"""
        return self._wx.find_contact(wxid=wxid, name=name)

    def media(self, media_api=None, chat=None, local_id=None):
        return self._wx.media(media_api, chat, local_id)


class PluginContext:
    """插件 handler 的唯一入参。所有字段都是本条消息私有的，线程安全。"""

    def __init__(self, m, kind, host, log, registry, match=None):
        self.m = m                        # policy.normalize 产出的消息字典（可改 text）
        self.kind = kind                  # text / quote / voice / image / video / other
        self.match = match or {           # policy.resolve 产出的策略判定
            "chat_type": m.get("chat_type"), "sources": None,
            "policy_id": None, "policy_name": "", "trigger": "",
        }
        self.question = ""
        self.answer = ""
        self._answer_set = False
        self.state = {}                   # 同一条消息跨 hook 传值
        self.replies = []                 # 插件通过 ctx.reply 发出的文本
        self.last_reply_text = ""         # 最近一次实际发出的文本（供 on_reply_sent）
        self.via = ""                     # text / quote
        self.skip_reason = ""
        self.error = ""
        self.plugin_name = ""             # dispatch 时由 registry 切换成当前插件
        self.takeover_by = ""             # STOP_ALL 时固化接管插件名（finally 会清 plugin_name）
        self._host = host
        self._registry = registry
        self._no_reply = False            # on_reply_sent 期间防递归
        self._logger = _PluginLogger(log, self)

    # ------------------------------------------------------------ 元数据
    @property
    def log(self):
        return self._logger

    @property
    def config(self):
        """当前插件的私有配置：data/plugins/<name>.yaml，mtime 变了自动重读。"""
        return self._registry.plugin_config(self.plugin_name)

    @property
    def wx(self):
        """只读的手机插件查询能力。"""
        return self._host.readonly_wx()

    def fire(self, hook, *, kind=None):
        """宿主内部派发 hook 用；插件自身不应调用。"""
        return self._registry.dispatch(hook, self, kind=kind)

    def self_wxid(self):
        return self._host.self_wxid()

    def _uid(self):
        return "%s|%s" % (self.m.get("chat") or "", self.m.get("sender") or "")

    # ------------------------------------------------------------ 能力
    def reply(self, text, *, with_hint=False, mark_followup=False):
        """发一条文本回复。必须走这里，不要用 WX.send_text。

        走 _send_text 才能保证 @ 规则、引用回复与消息流水。

        插件回复**默认不刷新对话延续窗口**（mark_followup=False）：插件是旁路
        responder，回一句摘要不等于「AI 正在接话」，后续消息仍需 @/唤醒词实质
        触发。确实要续聊的插件可显式传 mark_followup=True。
        """
        if self._no_reply:
            raise RuntimeError("on_reply_sent 内禁止再次回复（防止递归发送）")
        via = self._host.send_text(self.m, self.match, text,
                                  with_hint=with_hint, mark_followup=mark_followup)
        self.replies.append(str(text))
        return via

    def reply_image(self, path):
        if self._no_reply:
            raise RuntimeError("on_reply_sent 内禁止再次回复（防止递归发送）")
        return self._host.send_image(self.m, path)

    def ask_brain(self, question, **kw):
        """走 brain_lock 代理调用知识库问答。不要在持锁期间回调任何插件。"""
        return self._host.ask_brain(question, **kw)

    def llm_chat(self, messages, **kw):
        """调用宿主**内置大模型**（不检索知识库）。

        给插件做摘要 / 改写 / 分类这类「只要模型、不要知识库」的场景。

        与 ask_brain 的区别：
          * ask_brain 会先做 RAG 检索，再把检索结果拼进「知识库：… / 用户问题：…」模板；
          * llm_chat 把 messages 原样发给模型，一个字都不多。

        messages：[{role: "user"/"assistant"/"system", content: "..."}, ...]
        可选 kw：model / temperature / max_tokens / system_prompt。

        返回模型生成的纯文本；**失败抛异常**（不返回兜底话术），
        插件自己决定怎么兜底 —— 把失败当成成功会把错误内容缓存下来。
        """
        return self._host.llm_chat(messages, **kw)

    def history(self, uid=None):
        return self._host.history(uid or self._uid())

    def remember(self, question, answer, uid=None):
        return self._host.remember(uid or self._uid(), question, answer)

    # ------------------------------------------------------------ 产出提交
    def set_answer(self, text):
        """H4 直接给出答案 → 跳过 RAG+LLM，仍会走清洗、记忆、发送与流水。"""
        self.answer = str(text)
        self._answer_set = True

    def take_answer(self):
        self._answer_set = False
        return self.answer

    @property
    def has_answer(self):
        return self._answer_set

    def set_question(self, text):
        self.question = str(text)


# ---------------------------------------------------------------- P3 上下文
# 定时任务 / HTTP 扩展路由都不是「回复某条消息」，所以刻意不继承 PluginContext：
# 没有 match / replies / set_answer，也没有会发到空 chat 的 reply()。两者只共享
# 日志、私有配置、只读 WX 与 ask_brain 这几项受控能力。


class _BaseContext:
    """TaskContext / HttpContext 的公共受控能力。"""

    def __init__(self, plugin_name, host, log, registry):
        self.plugin_name = plugin_name
        self.state = {}
        self._host = host
        self._registry = registry
        self._logger = _PluginLogger(log, self)

    @property
    def log(self):
        return self._logger

    @property
    def config(self):
        """当前插件的私有配置（mtime 热读），与消息 ctx 完全一致。"""
        return self._registry.plugin_config(self.plugin_name)

    @property
    def wx(self):
        return self._host.readonly_wx()

    def ask_brain(self, question, **kw):
        """走 brain_lock 代理；不要在持锁期间回调任何插件。"""
        return self._host.ask_brain(question, **kw)

    def llm_chat(self, messages, **kw):
        """调用宿主内置大模型（不检索知识库）。语义见 PluginContext.llm_chat。"""
        return self._host.llm_chat(messages, **kw)


class TaskContext(_BaseContext):
    """`@schedule` handler 的入参。定时群发必须显式给 chat。"""

    def __init__(self, *, plugin_name, task_name, host, log, registry,
                 run_count=0):
        super().__init__(plugin_name, host, log, registry)
        self.task_name = task_name
        self.run_count = run_count          # 本进程内该任务的第几次执行（从 1 起）
        self.result = None                  # 预留给插件；调度器当前不回写上一次结果

    def broadcast(self, chat, text):
        """主动发到指定会话，并写一条回复流水。

        定时任务没有「原消息」，因此不 @ 人、不引用、不刷新对话延续窗口，
        也不派发 on_reply_sent；但统计口径仍与正常回复一致。
        """
        return self._host.broadcast(
            chat, str(text), source="%s.%s" % (self.plugin_name, self.task_name))


class HttpContext(_BaseContext):
    """`@http_route` handler 的入参。

    刻意不暴露 Flask 的 request 裸对象：只给白名单后的请求信息，插件无法
    读到 Cookie / Authorization 等敏感头，也无法构造任意响应对象。
    handler 直接 `return` dict / str / (body, status)，宿主负责转 JSON。
    """

    def __init__(self, *, plugin_name, route_name, method, path, args,
                 headers, body, json_data, host, log, registry):
        super().__init__(plugin_name, host, log, registry)
        self.route_name = route_name
        self.method = method
        self.path = path
        self.args = dict(args or {})          # query string，值统一为字符串
        self.headers = dict(headers or {})    # 只含 content-type / x-* 白名单头
        self.body = body or b""
        self.json = json_data                 # JSON body 解析结果，否则 None
        self.text = self.body.decode("utf-8", "replace")
