"""插件系统常量。独立成模块，避免 __init__ 与 registry 之间循环导入。"""
import re

# 宿主能力版本：manifest 的 min_host_version 超过它就拒绝加载。
# 与产品版本 APP_VERSION(V1.1.1) 对齐。
# 1.1.1 新增 ctx.recent_media()（查询最近 N 分钟的图片/文件）；
# 老插件声明 min_host_version 1.1.0 仍可正常加载。
HOST_VERSION = "1.1.1"

# 三态阻塞语义
CONTINUE = "continue"           # 继续后续插件 + 继续核心链路
STOP_PLUGINS = "stop_plugins"   # 停止后续插件，核心链路继续
STOP_ALL = "stop_all"           # 停止后续插件 + 停止核心链路（插件完全接管）
ACTIONS = frozenset((CONTINUE, STOP_PLUGINS, STOP_ALL))
DEFAULT_ACTION = CONTINUE

# 消息类 hook：按优先级串行派发，计入 describe()["handlers"]
MESSAGE_HOOKS = (
    "on_message", "on_pre_resolve", "on_post_resolve",
    "on_pre_answer", "on_post_answer", "on_reply_sent",
    "on_skip", "on_error",
)
# P3 新增的两类 handler 不走消息派发：定时任务由调度线程调用，HTTP 路由由
# /ext 通配路由调用。放进同一个 HOOKS 只是让 manifest 声明与 on() 校验统一。
SCHEDULE_HOOK = "on_schedule"
HTTP_HOOK = "on_http"
HOOKS = MESSAGE_HOOKS + (SCHEDULE_HOOK, HTTP_HOOK)

# policy.message_kind() 的全部取值，装饰器 kinds 参数只能填这些或 "*"
VALID_KINDS = ("text", "quote", "voice", "image", "video", "other")

SCHEDULE_KINDS = ("interval", "daily")
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "*")
MAX_SCHEDULE_SECONDS = 30 * 24 * 3600
MAX_PATH_LEN = 128

DEFAULT_PRIORITY = 50
MIN_PRIORITY = 0
MAX_PRIORITY = 100

# 插件名同时用作 sys.modules 名与配置文件名，必须严格限制，防路径穿越
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,47}$")
# 任务名 / 路由名 / 配置键：只在插件内部使用，同样限制字符集
TASK_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
# HTTP 子路径：只允许安全字符，禁止 .. 与空白
PATH_RE = re.compile(r"^[A-Za-z0-9_/.:-]{0,128}$")
