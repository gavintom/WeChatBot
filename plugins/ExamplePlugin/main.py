"""示例插件：演示三态阻塞、ctx.reply、ctx.set_answer、定时任务与扩展路由。

命令（都是文本消息）：
  /ping            → 只回一条 pong，STOP_ALL 完全接管（AI 不再回复）
  /boom            → 故意抛异常，验证异常隔离：客服仍会正常走 AI 回复
  /echo            → STOP_PLUGINS：挡住后续插件，主链路继续（AI 仍会回复）
  /答疑 任意内容    → ctx.set_answer 直接作答，跳过 RAG + LLM，但记忆与流水照常写

定时任务（需要宿主 >= 1.1.0，`plugins.scheduler.enabled` 默认开启）：
  每 60 秒写一条 DEBUG 心跳，**不发送任何微信消息**；
  私有配置 `schedule_seconds: 5` 可把周期改成 5 秒（mtime 热读，无需重启）。

HTTP 扩展路由（默认关闭，开启后必须带 X-Internal-Token）：
  GET  /ext/ExamplePlugin/ping
  POST /ext/ExamplePlugin/echo
  GET  /ext/ExamplePlugin/boom   → 故意抛异常，验证异常隔离

可选私有配置：data/plugins/ExamplePlugin.yaml
  append_tail: "—— 由示例插件追加"    # 打开后给所有 AI 回答末尾追加一行，默认关闭
  schedule_seconds: 60                # 定时任务周期（秒），最小 1
"""
from pluginhost import (CONTINUE, STOP_ALL, STOP_PLUGINS, PluginBase,
                        http_route, on_message, on_post_answer, on_pre_answer,
                        schedule)


class ExamplePlugin(PluginBase):
    name = "ExamplePlugin"

    @on_message(priority=90, kinds=("text",))
    def command(self, ctx):
        text = str(ctx.m.get("text") or "").strip()
        if text == "/ping":
            ctx.reply("pong（来自 ExamplePlugin）")
            return STOP_ALL
        if text == "/boom":
            raise RuntimeError("示例插件故意抛出的异常，用于验证异常隔离")
        if text == "/echo":
            ctx.reply("echo 由插件发出；STOP_PLUGINS 只挡后续插件，主链路继续")
            return STOP_PLUGINS
        if text == "/我的文件":
            # 1.1.1 新能力示例：取当前这个人最近 10 分钟发的图片/文件
            items = ctx.recent_media(wxid=ctx.m.get("sender"), minutes=10)
            if not items:
                ctx.reply("最近 10 分钟没有收到你的图片或文件。")
            else:
                lines = ["%s：%s（%s）" % (
                    "图" if it["kind"] == "image" else "文件",
                    it["orig_name"], it["download_time"]) for it in items[:20]]
                ctx.reply("最近 10 分钟收到你 %d 个：\n%s" % (len(items), "\n".join(lines)))
            return STOP_ALL
        return CONTINUE

    @on_pre_answer(priority=60, kinds=("text",))
    def direct_answer(self, ctx):
        if str(ctx.question or "").strip().startswith("/答疑"):
            ctx.set_answer("这是插件直接给出的答案，没有调用知识库与大模型。")
        return CONTINUE

    @on_post_answer(priority=10)
    def append_tail(self, ctx):
        tail = str(ctx.config.get("append_tail") or "").strip()
        if tail:
            ctx.answer = str(ctx.answer).rstrip() + "\n\n" + tail
        return CONTINUE

    # ------------------------------------------------------------ P3 定时任务
    @schedule("interval", seconds=60, config_key="schedule_seconds",
              name="heartbeat")
    def heartbeat(self, ctx):
        """只写 DEBUG 日志，不主动发微信 —— 示例插件不该打扰真实客户。

        真要做日报 / 定时群发，用 `ctx.broadcast(chat, text)`：会写一条回复流水，
        但不 @ 人、不引用、不刷新对话延续窗口（定时任务没有「原消息」）。
        """
        ctx.log.debug("定时任务心跳 | 任务=%s | 第 %d 次 | 周期=%s",
                      ctx.task_name, ctx.run_count,
                      ctx.config.get("schedule_seconds"))
        return CONTINUE

    # ------------------------------------------------------------ P3 扩展路由
    @http_route("GET", "/ping", name="ping")
    def ext_ping(self, ctx):
        return {"ok": True, "plugin": ctx.plugin_name, "path": ctx.path,
                "route": ctx.route_name, "args": ctx.args,
                # 只给键名，不把私有配置内容透给外部调用方
                "config_keys": sorted(str(k) for k in (ctx.config or {}))}

    @http_route("POST", "/echo", name="echo")
    def ext_echo(self, ctx):
        return {"ok": True, "echo": ctx.json if ctx.json is not None else ctx.text}

    @http_route("GET", "/boom", name="boom")
    def ext_boom(self, ctx):
        raise RuntimeError("示例插件 HTTP 路由故意抛出的异常")
