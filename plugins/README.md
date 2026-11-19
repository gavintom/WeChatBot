# 🔌 插件开发指南

插件与宿主进程内运行，采用同步 hook + 受控门面（`PluginContext`）。开始开发前请先了解
下方的 hook 派发顺序、三态阻塞语义与能力边界。

## 📝 目录

- [快速开始](#快速开始)
- [插件结构](#插件结构)
- [消息 hook](#消息-hook)
- [优先级与阻塞](#优先级与阻塞)
- [PluginContext 能力](#plugincontext-能力)
- [私有配置](#私有配置)
- [生命周期](#生命周期)
- [启用 / 禁用 / 重载](#启用--禁用--重载)
- [定时任务](#定时任务)
- [HTTP 扩展路由](#http-扩展路由)
- [最佳实践](#最佳实践)
- [常见问题](#常见问题)

## 🌟 快速开始

1. 在 `data/config.yaml` 里把 `plugins.enabled` 设为 `true`（默认关闭）。
2. 复制 `plugins/ExamplePlugin/` 改成你的目录名，同步改 `plugin.yaml` 的 `name`。
3. 重启主服务，或调用 `POST /internal/plugins/reload` 在线加载。

`plugins.enabled=false` 或目录为空时，消息链路与插件系统上线前**逐字节等价**，可随时回滚。

## 📂 插件结构

```
plugins/
└── YourPlugin/
    ├── __init__.py      # 必须导出 entry 指定的类
    ├── main.py          # 主要逻辑
    ├── plugin.yaml      # 清单（必需）
    └── README.md        # 说明（可选）
```

`plugin.yaml`：

```yaml
name: YourPlugin                     # 必须与目录名一致，^[A-Za-z][A-Za-z0-9_]{1,47}$
version: 1.0.0
author: Your Name
description: 一句话说明
entry: main:YourPlugin               # 模块:类名；类必须继承 PluginBase
priority: 50                         # 0~100，插件级默认优先级
hooks: [on_message, on_post_answer] # 仅供说明，实际以装饰器为准
min_host_version: "1.0.0"
```

```python
# plugins/YourPlugin/__init__.py
from .main import YourPlugin
```

```python
# plugins/YourPlugin/main.py
from pluginhost import PluginBase, on_message, CONTINUE

class YourPlugin(PluginBase):
    @on_message(priority=50, kinds=("text",))
    def handle(self, ctx):
        if "你好" in str(ctx.m.get("text") or ""):
            ctx.reply("你好！")
        return CONTINUE
```

**`__init__` 必须可以无参调用**（框架用 `cls()` 实例化），不要在构造函数里做 IO / 网络请求。
需要配置请用 `ctx.config`。

## 🎯 消息 hook

| hook | 派发时机 | 常见用途 |
|---|---|---|
| `on_message` | 基础过滤（自消息 / 噪声 / 过期）之后、ASR 之前 | 命令解析、自定义黑名单、全量旁听 |
| `on_pre_resolve` | 群聊/私聊策略判定之前 | 改写 `m["text"]` 影响唤醒词匹配 |
| `on_post_resolve` | 策略判定之后、消息类型分支之前 | 换知识库策略、改触发条件、接管 |
| `on_pre_answer` | 提问构造完成、`brain_lock` 之前 | `ctx.set_answer()` 直接作答，跳过检索与 LLM |
| `on_post_answer` | 答案清洗截断之后、写对话记忆之前 | 追加免责声明、敏感词过滤、格式归一 |
| `on_reply_sent` | `_send_text` 单一出口，回复成功发出后 | 统计、二次通知（覆盖转人工 / 媒体提示 / 语音兜底） |
| `on_skip` | 消息被忽略时 | 观测「为什么没回」 |
| `on_error` | 未捕获异常 / LLM 异常 / 发送失败 | 告警 |

装饰器参数：`@on_message(priority=..., kinds=...)`。`kinds` 只能取
`text / quote / voice / image / video / other` 或 `*`；不填即 `*`。
**所有 handler 都是同步函数**，返回三态常量之一（或 `None`，等价 `CONTINUE`）。

> 图片/视频消息当前默认直接回「暂不支持」、不进知识检索；手机插件也没有
> 拍一拍 / 表情 / XML 独立类型，消息类型统一由 `kinds` 参数区分。
>
> ⚠️ `on_message` **早于**群聊/私聊策略判定（`policy.resolve`），因此会看到所有通过
> 基础过滤的消息（含未配置的群、黑名单用户）。若插件必须遵守接入策略，
> 请把处理放到 `on_post_resolve` 及之后，或自行检查 `ctx.match`。

## 🔐 优先级与阻塞

派发顺序：`priority` 降序 → 同优先级按插件名升序 → 同插件内按方法名 + 装饰顺序。
同插件的 handler 优先级默认取 `plugin.yaml` 的 `priority`；装饰器显式写
`priority=80` 则覆盖；后台设置 `priority_override` 会覆盖该插件全部 handler。

| 返回值 | 后续插件 | 核心链路 |
|---|---|---|
| `CONTINUE`（或 `None`） | 继续 | 继续 |
| `STOP_PLUGINS` | 停止 | 继续（拿到 ctx 的产出） |
| `STOP_ALL` | 停止 | **停止**：不再调 AI、不再发回复；插件需自己 `ctx.reply()` |

`STOP_ALL` 且插件确实发过回复时，框架会自动补写对话记忆与 `events` 流水，统计不会漏；
插件只是静默拦截（没回复）时不写流水。

插件抛出的任何异常都会被隔离并记录，按 `CONTINUE` 放行；连续
`fail_threshold`（默认 5）次异常后自动熔断禁用，后台可查看原因并一键恢复。

## 🧰 PluginContext 能力

`ctx` 每条消息一个，贯穿所有 hook；`ctx.state` 可在同一消息的不同阶段间传值。

| 成员 | 说明 |
|---|---|
| `ctx.m` | `policy.normalize()` 的消息字典（20 个字段），可改 `text` |
| `ctx.match` | `policy.resolve()` 的策略判定（`sources` / `system_prompt` / `need_kb` …） |
| `ctx.question` / `ctx.answer` | 当前提问 / 答案；用 `set_question()` / `set_answer()` 提交 |
| `ctx.config` | 该插件私有配置（`data/plugins/<name>.yaml`），mtime 变化立即生效 |
| `ctx.log` | 已带会话上下文与插件名前缀的 logger |
| `ctx.state` | 本条消息私有的 dict |
| `ctx.reply(text, with_hint=False, mark_followup=False)` | **唯一允许的发送方式**，转发 `_send_text`。`with_hint=True` 时群聊末尾追加续聊提示。插件回复**默认不刷新对话延续窗口**（`mark_followup=False`）：插件是旁路 responder，后续消息仍需 @/唤醒词实质触发；确实要续聊的插件可显式传 `mark_followup=True` |
| `ctx.takeover_by` | 本条消息若被某插件 `STOP_ALL` 接管，这里固化该插件名（`on_reply_sent` 等 dispatch 之外仍可读）；宿主据此把插件名写进「对话记录」流水 |
| `ctx.reply_image(path)` | 发一张本地图片，转发 `_send_images` |
| `ctx.ask_brain(question, **kw)` | 走 `brain_lock` 代理调用知识库问答 |
| `ctx.llm_chat(messages, **kw)` | 直连**内置大模型**（不检索知识库），见下 |
| `ctx.history()` / `ctx.remember(q, a)` | 读写该会话的对话记忆 |
| `ctx.recent_media(wxid=None, chat=None, chat_name=None, minutes=30, kind=None, chat_type=None, limit=200)` | **1.1.1 新增**：查最近 N 分钟（0-30）内某人 / 某会话发送的图片与文件。返回 `list[dict]`：`kind`(image/file) / `orig_name`(原名) / `hash_name` / `sha256` / `download_time` / `downloaded_at` / `chat` / `chat_name` / `wxid` / `sender_name` / `path`(绝对路径，文件已过期为 "") / `available`。例：`ctx.recent_media(wxid=ctx.m["sender"], minutes=10)`；`ctx.recent_media(chat=ctx.m["chat"], kind="file")`。`TaskContext`/`HttpContext` 也能用 |
| `ctx.wx` | 手机插件**只读查询**（v2 通讯录，方法名与插件文档第 6 章一一对应）：`health / groups / friends / group_members / find_contact / media`。返回值**原样透传**、不做包装：`groups()` → 列表，`friends()` → `{friends, count}`（带 `wxid/name` 时为 `{lookup}`），`find_contact()` → 统一反查结构 `{mode, query, exact, count, truncated, matches[]}`。已下线的 `/api/contacts` 没有对应方法，需要查群成员请用 `group_members(chat)` |
| `ctx.self_wxid()` | 当前登录账号 wxid |
| `ctx.last_reply_text` / `ctx.via` | `on_reply_sent` 时最近发出的文本与方式 |
| `ctx.skip_reason` / `ctx.error` | `on_skip` / `on_error` 时的原因 |

> ⚠️ **拿不到 `WX` / `BRAIN` / `CFG` 裸对象**。直接调 `WX.send_text()` 会绕过
> @ 规则、引用回复、私聊硬保证与 `events` 流水，导致统计错乱。续聊窗口由宿主
> 在 `_send_text` 里统一控制（主链路刷新、插件回复默认跳过），插件不要自己调
> `followup.mark_reply()`。需要新能力请给框架提门面方法，不要开后门。

### 直连内置大模型：`ctx.llm_chat()`

需要宿主 `>= 1.1.0`。给「摘要 / 改写 / 分类 / 抽关键词」这类
**只要模型、不要知识库**的场景用：

```python
text = ctx.llm_chat(
    [{"role": "user", "content": "请总结这段正文：\n" + content}],
    system_prompt="你是摘要助手，输出纯文本，不要 markdown 符号",
    temperature=0.3,          # 可选
    max_tokens=800,           # 可选
    model="gpt-4o",           # 可选；不传用后台「AI 大模型」页选定的默认模型
)
```

与 `ctx.ask_brain()` 的区别：

| | `ask_brain` | `llm_chat` |
|---|---|---|
| 知识库检索 | 做（RAG） | **不做** |
| 提问模板 | 拼成「知识库：… / 用户问题：…」 | messages 原样发给模型 |
| 失败表现 | 返回兜底话术 | **抛异常** |
| 用途 | 客服问答 | 摘要 / 改写 / 分类 |

失败抛异常是刻意的：插件必须能区分「模型没答上来」和「模型答上来了」，
静默返回兜底话术会让插件把失败当成正常结果缓存下来，之后追问全是错的。
插件自己 try/except 并决定怎么回复用户。

模型调用同样计入后台「AI 用量」页（`source=plugin`），不会白嫖。

> ⚠️ 所有消息 hook 都在 `brain_lock` **之外**派发，所以 hook 里调 `ctx.llm_chat()`
> 不会死锁；但也别在 `on_reply_sent` 里调（那里禁止再回复）。

`on_reply_sent` 内**禁止**再次调用 `ctx.reply()`（防递归发送），硬约束会抛异常。

## ⚙️ 私有配置

配置放 `data/plugins/<插件名>.yaml`（运行时数据，随 `data/` 卷持久化，不进 git / 镜像）：

```yaml
# data/plugins/YourPlugin.yaml
trigger_word: "你好"
max_items: 10
```

用 `ctx.config.get("trigger_word", "你好")` 读取。框架按文件 mtime 热读，
后台改完最多 30 秒生效，**不需要重启主服务**。读到非法 YAML 时按空配置继续，不会拖垮消息链路。

## 🔄 生命周期

`on_load()`（实例化后）→ `on_enable()`（启用状态、每次重载完成）→ 运行 →
`on_disable()`（禁用 / 注册表被替换前）→ `on_unload()`。全部同步。
`on_disable()` 用来关 session、释放自己打开的文件句柄。

## 🛠 启用 / 禁用 / 重载

状态存在 `analytics.db` 的 `plugins` 表（主服务与后台共用，WAL）。

```bash
# 查看
curl -H "X-Internal-Token: <webhook.internal_token>" \
     http://127.0.0.1:29999/internal/plugins

# 禁用（会自动重载）
curl -X POST -H "X-Internal-Token: <token>" -H "Content-Type: application/json" \
     -d '{"name":"YourPlugin","enabled":false}' \
     http://127.0.0.1:29999/internal/plugins/state

# 改优先级（null = 回落到清单声明值）
curl -X POST -H "X-Internal-Token: <token>" -H "Content-Type: application/json" \
     -d '{"name":"YourPlugin","priority":80}' \
     http://127.0.0.1:29999/internal/plugins/state

# 只重载（新增/删除插件目录、改了代码或配置后）
curl -X POST -H "X-Internal-Token: <token>" \
     http://127.0.0.1:29999/internal/plugins/reload
```

重载采用「先构造新注册表、再原子替换指针」，在途消息仍用旧快照；加载失败保留旧表。
**Web 管理端不 import 插件代码**，它只读写状态并调这些内部接口。

## ⏰ 定时任务

需要宿主 `>= 1.1.0`，在 `plugin.yaml` 的 `hooks` 里加 `on_schedule`。主线程之外只有
**一份**调度器（`workers=1`），单 daemon tick 线程判定到期、独立小线程池执行，
慢任务不会卡住消息处理；同一任务有 `running` 标记，不会并发重入。

```python
from pluginhost import PluginBase, schedule, CONTINUE

class YourPlugin(PluginBase):

    @schedule("interval", seconds=60)
    def check(self, ctx):
        ctx.log.info("第 %d 次巡检 | 配置项=%s", ctx.run_count, ctx.config)
        # ctx.broadcast("10000000001@chatroom", "早安，今日日报…")   # 定时群发
        return CONTINUE

    @schedule("interval", seconds=60, config_key="schedule_seconds")
    def tunable(self, ctx):
        # data/plugins/YourPlugin.yaml 写 schedule_seconds: 5 即可改成 5 秒，
        # mtime 热读无需重启，管理端「插件管理 → 定时任务」能看到生效周期
        return CONTINUE

    @schedule("daily", hour=9, minute=0)
    def daily_report(self, ctx):
        return CONTINUE
```

| 类型 | 参数 | 语义 |
|---|---|---|
| `interval` | `seconds`（1~2592000） | 启动后**等满一个周期**才首次执行；错过不追补 |
| `interval` | `config_key` | 从私有配置读同名整数覆盖周期，改配置即热生效 |
| `daily` | `hour` / `minute` | 每天本地挂钟时间一次；`plugins.scheduler.utc_offset` 指定时区（Docker 默认 UTC，东八区填 `8`） |

`TaskContext` 只有 `log / config / wx / ask_brain / state / run_count / task_name /
broadcast(chat, text)`。**没有 `reply()`**：定时任务不是「回复某条消息」，
`broadcast` 不 @ 人、不引用、不刷新对话延续窗口，但仍写一条回复流水。
任务抛异常会被隔离并计入连续失败熔断，绝不影响客服主链路。

不做完整 cron、不引入 APScheduler。运行时状态看
`GET /internal/plugins/schedules`（带 `X-Internal-Token`）或管理端「插件管理」页。

### 后台可以怎么改你的任务

管理端「插件管理 → 定时任务」维护 `data/plugins/_tasks.yaml`，主服务按 mtime 热读，
最多一个 tick 生效，不需要重启或重载插件注册表：

| 操作 | 效果 |
|---|---|
| 编辑 | 覆盖周期（`interval` 秒数 / `daily` 时分）与显示名，启用 / 停用 |
| 移除 / 恢复 | 代码声明的任务不能物理删除，移除 = 持久停用；恢复后回落到代码声明值 |
| 新增实例 | 以某个已声明任务为模板复制 `YourPlugin:task#suffix`，复用同一 handler，但 key / 周期 / 运行计数独立 |

**对插件作者的两点影响**：

1. 同一个 handler 可能同时被多个实例调用，周期还各不相同 —— 不要假设「全局只跑一份」，
   需要幂等或用 `ctx.state` 自行协调。`ctx.task_name` 仍是原任务名（不含 `#suffix`）。
2. 管理端一旦为任务设置了周期，就**覆盖**装饰器里的 `seconds` / `hour:minute`，
   同时不再读 `config_key` 私有配置；点「恢复」才回到代码声明与 `config_key` 行为。

后台造不出代码里没有声明过的新行为：新实例只能复用已有 handler。配置文件损坏时
主服务降级为「全部按代码声明执行」，并在管理端显示提示。

注意：**插件被禁用时注册表不会加载它的任何任务**，任务页会显示空列表并提示先启用插件，
此时也无法新增实例；重新启用插件后，`_tasks.yaml` 里已有的覆盖与实例会自动恢复生效。

## 🌐 HTTP 扩展路由

Flask 3 不允许运行时增删路由，所以宿主在**启动期**挂好固定通配路由
`/ext/<插件名>/<子路径>`，插件只用 `@http_route` 声明 handler。

```python
from pluginhost import PluginBase, http_route

class YourPlugin(PluginBase):

    @http_route("GET", "/status")
    def status(self, ctx):
        return {"ok": True, "args": ctx.args}

    @http_route("POST", "/callback", name="cb")
    def callback(self, ctx):
        return {"ok": True, "echo": ctx.json or ctx.text}, 200
```

```yaml
# data/config.yaml 的 plugins 段（默认关闭）
http_routes:
  enabled: true
  require_internal_token: true   # 不要改成 false
  max_body: 65536
```

```bash
curl -H "X-Internal-Token: <token>" \
     http://127.0.0.1:29999/ext/YourPlugin/status?q=1
```

- **默认关闭**，关闭时返回 404 且不暴露插件是否存在；开启后默认强制
  `X-Internal-Token`。29999 只应对内网/受信任调用方开放，不要直接映射到公网。
- 方法支持 `GET/POST/PUT/PATCH/DELETE`，插件可声明 `"*"` 匹配全部；路径只允许
  字母数字与 `_ / . : -`，禁止 `..`；请求体超 `max_body` 返回 413。
- `HttpContext` 给 `method / path / args / headers / body / text / json /
  config / log / wx / ask_brain / state`。**拿不到 Flask `request` 裸对象**，
  Cookie、Authorization 与 X-Internal-Token 等凭证头一律不透传。
- handler 直接 `return` dict / list / 字符串 / `(body, status)` / `None`，
  宿主转 JSON。抛异常返回 500 且计入熔断，响应里不泄露堆栈。
- 插件被禁用 / 熔断 / 未声明该路径 → 404。新增或改路由需 `/internal/plugins/reload`。

## 🏆 最佳实践

1. 始终明确返回 `CONTINUE` / `STOP_PLUGINS` / `STOP_ALL`，别依赖隐式 `None`。
2. 只在真正需要完全接管时用 `STOP_ALL`，并记得 `ctx.reply()`。
3. 高优先级（80~100）留给过滤/命令类插件，低优先级（0~39）留给旁听类。
4. handler 内自己做 `try/except`；框架虽会隔离，但异常会计入熔断次数。
5. 不要做阻塞式长耗时操作：同步线程无法被安全强杀，框架**不提供硬超时**。
   所有网络请求请通过 `ctx.wx` / `ctx.ask_brain`，它们都带 timeout。
6. 插件代码与宿主同进程运行 = **可信代码**。只装可信来源，不做沙箱，也不会自动 pip 安装依赖。
7. 定时任务只做可重入的短任务：进程重启不会续跑错过的次数，`daily` 注意容器时区；
   要发消息用 `ctx.broadcast()` 显式给 chat，别把定时群发写成无条件的 `on_message`。
8. `/ext` 是外部入口，handler 里自己做参数校验与鉴权判断，别把 `ctx.config`
   原文、内部 token 或堆栈直接返回给调用方。

## ❓ 常见问题

**插件不生效**：`plugins.enabled` 是否为 `true`？`plugins` 表里该插件是否被禁用/熔断？
`/internal/plugins` 能直接看到加载错误与熔断原因。

**改了代码没变化**：改插件目录后要调 `/internal/plugins/reload`（或重启主服务）。
只改 `data/plugins/*.yaml` 不需要，mtime 热读即可。

**消息被拦截但没有流水**：检查是否返回了 `STOP_ALL` 却没调 `ctx.reply()`；
静默拦截按设计不写回复流水。

**定时任务没触发**：`plugins.scheduler.enabled` 是否为 `true`？插件是否启用？
看 `/internal/plugins/schedules` 的 `next_run` / `runs` / `last_error`。
`interval` 启动后要等满一个周期才首次执行；`daily` 注意容器时区（配 `utc_offset`）。

**`/ext/...` 返回 404**：扩展路由默认关闭，先开 `plugins.http_routes.enabled` 并重启；
再确认插件已启用、路径与方法完全匹配、请求带对了 `X-Internal-Token`。

**Flask 路由**：插件不能自己注册 HTTP 路由（Flask 3 不允许运行时增删 route），
只能声明 `@http_route`，由启动期挂好的固定通配路由转发。新增/改动后要重载注册表。
