# ExamplePlugin

最小可运行示例，演示插件系统的消息 hook、定时任务与 HTTP 扩展路由。
需要宿主 `>= 1.1.0`（`min_host_version` 已声明）。

| 命令 | 演示的能力 |
|---|---|
| `/ping` | `ctx.reply()` + `STOP_ALL` 完全接管，AI 不再回复 |
| `/boom` | 插件抛异常被隔离，客服主链路照常回复 |
| `/echo` | `STOP_PLUGINS` 只停止后续插件，主链路继续 |
| `/答疑 xxx` | `ctx.set_answer()` 直接作答，跳过知识库与 LLM |

在 `data/config.yaml` 里把 `plugins.enabled` 设为 `true` 并重启主服务后即可使用；
或调用 `POST /internal/plugins/state` + `POST /internal/plugins/reload` 在线启用。

私有配置（新建 `data/plugins/ExamplePlugin.yaml`，mtime 热读、无需重启）：

```yaml
# 给每条 AI 回答末尾追加一行；删除或留空即关闭
append_tail: "—— 由示例插件追加"
# 定时任务周期（秒），最小 1；不写则用装饰器声明的 60 秒
schedule_seconds: 60
```

## 定时任务

`@schedule("interval", seconds=60, config_key="schedule_seconds")` 每周期只写一条
DEBUG 心跳，**不发送任何微信消息**，不会打扰真实客户。管理端「插件管理 → 定时任务」
能看到执行次数、下次执行时间与最近错误；也可查：

```bash
curl -H "X-Internal-Token: <token>" \
     http://127.0.0.1:29999/internal/plugins/schedules
```

真要做日报 / 群发，把 `ctx.log.debug(...)` 换成 `ctx.broadcast(chat, text)`。

## HTTP 扩展路由

默认关闭（`plugins.http_routes.enabled: false`）。开启并重启后，全部请求都要带
`X-Internal-Token`：

```bash
curl -H "X-Internal-Token: <token>" \
     "http://127.0.0.1:29999/ext/ExamplePlugin/ping?q=1"
curl -X POST -H "X-Internal-Token: <token>" -H "Content-Type: application/json" \
     -d '{"hello":"world"}' http://127.0.0.1:29999/ext/ExamplePlugin/echo
# /ext/ExamplePlugin/boom 故意抛异常，用于验证异常隔离（返回 500，插件不被拖垮）
```

> ⚠️ 示例插件的命令对**所有**文本消息生效。正式使用前请在管理端点「禁用」，
> 或换成自己的正式插件。
