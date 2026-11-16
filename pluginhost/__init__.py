"""个人微信智能客服 · 插件系统对外 API。

设计要点：
  * 宿主是 Flask 同步 + 线程池，handler 是**同步**函数；
  * 装饰器只给方法打元数据，真正派发由 PluginRegistry 完成；
  * handler 返回 CONTINUE / STOP_PLUGINS / STOP_ALL 三态；
  * 插件拿不到 WX / BRAIN 裸对象，一切能力通过 PluginContext 门面。

最小插件：

    from pluginhost import PluginBase, on_message, STOP_ALL

    class Hello(PluginBase):
        @on_message(priority=80, kinds=("text",))
        def handle(self, ctx):
            if ctx.m["text"].strip() == "/ping":
                ctx.reply("pong")
                return STOP_ALL
            return None
"""
from .consts import (ACTIONS, CONTINUE, DEFAULT_ACTION, DEFAULT_PRIORITY,
                     HOOKS, HOST_VERSION, HTTP_HOOK, HTTP_METHODS,
                     MAX_PATH_LEN, MAX_PRIORITY, MAX_SCHEDULE_SECONDS,
                     MESSAGE_HOOKS, MIN_PRIORITY, PATH_RE, SCHEDULE_HOOK,
                     SCHEDULE_KINDS, STOP_ALL, STOP_PLUGINS, TASK_NAME_RE,
                     VALID_KINDS)
from .context import HttpContext, PluginContext, TaskContext


class PluginBase:
    """插件基类。子类**必须能被无参构造**（registry 用 cls() 实例化）。

    需要读配置用 ctx.config（消息内）或 self.api.config（生命周期里），
    不要在 __init__ 里做 IO / 网络请求 —— 那会让整个插件加载失败。
    """

    name = ""
    description = ""
    author = ""
    version = "1.0.0"

    # registry 实例化后会覆盖成 _PluginAPI，插件可在 on_load 里使用
    api = None

    def __init__(self):
        pass

    def on_load(self):
        """首次加载完成（已实例化）后调用。"""

    def on_enable(self):
        """插件处于启用状态时、每次重载完成后调用。"""

    def on_disable(self):
        """后台禁用 / 注册表被替换前调用，用来关 session、释放句柄。"""

    def on_unload(self):
        """模块即将被丢弃时调用。"""


def on(hook, priority=None, kinds=None, **meta):
    """把一个方法登记为指定 hook 的处理器。可重复装饰同一个方法。

    priority=None 表示用 plugin.yaml 里的插件级 priority（默认 50）；
    后台的 priority_override 若设置，会整体覆盖该插件所有 handler。
    meta 只供本包的 schedule() / http_route() 传调度与路由规格，第三方插件
    不要直接传（未登记的键会被注册表忽略）。
    """
    if hook not in HOOKS:
        raise ValueError("未知插件 hook：%s（合法值：%s）" % (hook, ", ".join(HOOKS)))
    if priority is not None:
        try:
            priority = min(MAX_PRIORITY, max(MIN_PRIORITY, int(priority)))
        except (TypeError, ValueError):
            priority = None
    if kinds is None:
        ks = ("*",)
    else:
        if isinstance(kinds, str):
            kinds = (kinds,)
        ks = tuple(str(k).strip().lower() for k in kinds)
        bad = [k for k in ks if k != "*" and k not in VALID_KINDS]
        if bad or not ks:
            raise ValueError("kinds 只能取 %s 或 *，收到 %r" % (VALID_KINDS, kinds))

    def deco(fn):
        marks = list(getattr(fn, "_plugin_marks", ()) or ())
        mark = {"hook": hook, "priority": priority, "kinds": ks,
                "order": len(marks)}
        mark.update(meta)
        marks.append(mark)
        setattr(fn, "_plugin_marks", tuple(marks))
        return fn

    return deco


def on_message(priority=None, kinds=None):
    return on("on_message", priority, kinds)


def on_pre_resolve(priority=None, kinds=None):
    return on("on_pre_resolve", priority, kinds)


def on_post_resolve(priority=None, kinds=None):
    return on("on_post_resolve", priority, kinds)


def on_pre_answer(priority=None, kinds=None):
    return on("on_pre_answer", priority, kinds)


def on_post_answer(priority=None, kinds=None):
    return on("on_post_answer", priority, kinds)


def on_reply_sent(priority=None, kinds=None):
    return on("on_reply_sent", priority, kinds)


def on_skip(priority=None, kinds=None):
    return on("on_skip", priority, kinds)


def on_error(priority=None, kinds=None):
    return on("on_error", priority, kinds)


def _name(v, what, default=""):
    v = str(v or default).strip()
    if v and not TASK_NAME_RE.match(v):
        raise ValueError("%s 只能含字母、数字、下划线，且以字母开头：%r" % (what, v))
    return v


def schedule(kind, *, seconds=None, hour=None, minute=None,
             config_key=None, name=None):
    """登记一个定时任务（P3）。只打元数据，由主服务的调度线程统一触发。

        @schedule("interval", seconds=60)
        @schedule("interval", seconds=60, config_key="tick_seconds")
        @schedule("daily", hour=9, minute=0)

    * `interval`：每 `seconds` 秒一次，启动后**不立即执行**（等一个完整周期）；
      `config_key` 可选，运行时从私有配置读同名整数覆盖周期，改配置无需重启。
    * `daily`：每天本地时间 `hour:minute` 执行一次，错过不追补。
    * 不做完整 cron；不引入 APScheduler，零新增依赖。

    handler 收到 `TaskContext`，可以 `return CONTINUE`（返回值不影响调度）。
    """
    kind = str(kind or "").strip().lower()
    if kind not in SCHEDULE_KINDS:
        raise ValueError("定时任务类型只能是 %s，收到 %r" % (SCHEDULE_KINDS, kind))
    spec = {"kind": kind, "name": _name(name, "任务名")}
    if kind == "interval":
        try:
            sec = int(seconds)
        except (TypeError, ValueError):
            raise ValueError("interval 任务必须给整数 seconds")
        if not 1 <= sec <= MAX_SCHEDULE_SECONDS:
            raise ValueError("seconds 必须在 1~%d 之间" % MAX_SCHEDULE_SECONDS)
        spec["seconds"] = sec
        spec["config_key"] = _name(config_key, "config_key")
    else:
        try:
            h, mi = int(hour), int(minute if minute is not None else 0)
        except (TypeError, ValueError):
            raise ValueError("daily 任务必须给整数 hour / minute")
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            raise ValueError("hour 需 0~23、minute 需 0~59")
        spec["hour"], spec["minute"] = h, mi
    return on(SCHEDULE_HOOK, schedule=spec)


def http_route(method="GET", path="/", *, name=None):
    """登记一个 HTTP 扩展路由（P3）。宿主启动期挂好固定通配路由，运行时不改路由表。

        @http_route("GET", "/ping")
        @http_route("POST", "/webhook")

    实际访问路径为 `/ext/<插件名><path>`，例如 `/ext/ExamplePlugin/ping`。
    总开关 `plugins.http_routes.enabled` 默认关闭；开启后默认强制带
    `X-Internal-Token`。handler 收到 `HttpContext`，直接 `return` 一个 dict /
    字符串 / `(body, status)` 元组，宿主负责转成 JSON 响应。
    """
    method = str(method or "GET").strip().upper()
    if method not in HTTP_METHODS:
        raise ValueError("HTTP 方法只能是 %s，收到 %r" % (HTTP_METHODS, method))
    p = str(path or "/").strip()
    if not p.startswith("/"):
        p = "/" + p
    if len(p) > MAX_PATH_LEN or not PATH_RE.match(p) or ".." in p:
        raise ValueError("扩展路由路径不合法（只允许字母数字 _ / . : -）：%r" % p)
    if p != "/":
        p = "/" + p.strip("/")
    spec = {"method": method, "path": p, "name": _name(name, "路由名")}
    return on(HTTP_HOOK, http=spec)


# registry.py 会从本模块取 PluginBase，必须等类定义完再导入（避免循环导入）
from .registry import (MemState, PluginRegistry, StoreState, load_registry,
                       rebuild)
from .scheduler import PluginScheduler
from .taskstore import TaskStore


__all__ = [
    "ACTIONS", "CONTINUE", "STOP_PLUGINS", "STOP_ALL", "DEFAULT_ACTION",
    "HOOKS", "MESSAGE_HOOKS", "VALID_KINDS", "HOST_VERSION", "DEFAULT_PRIORITY",
    "SCHEDULE_KINDS", "HTTP_METHODS", "SCHEDULE_HOOK", "HTTP_HOOK",
    "PluginBase", "PluginContext", "TaskContext", "HttpContext",
    "on", "on_message", "on_pre_resolve", "on_post_resolve",
    "on_pre_answer", "on_post_answer", "on_reply_sent",
    "on_skip", "on_error", "schedule", "http_route",
    "PluginRegistry", "PluginScheduler", "TaskStore", "load_registry", "rebuild",
    "MemState", "StoreState",
]
