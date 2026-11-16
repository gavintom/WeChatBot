"""日志文件统一管理：按天一个文件，最多保留 5 个。

生产默认目录 `data/logs/`（跟配置、数据库一起，Docker 只需挂一个卷）：

    data/logs/webhook-2026-11-10.log              主服务运行日志
    data/logs/admin-2026-11-10.log                管理端操作日志
    data/logs/webhook_server.out-2026-11-10.log   主服务启动输出

每类日志**各保留最近 5 个文件**（含当天，按文件名里的日期倒序），
跨天时 handler 自动切换到新文件；进程启动时也会先清一次旧文件。

测试要用固定文件时，设 `WXBOT_LOG` / `WXBOT_ADMIN_LOG` / `WXBOT_SERVICE_LOG`
指向具体路径 —— 此时不做按天轮转、也不清理，避免误删测试文件。
`WXBOT_LOG_DIR` 可整体改日志目录。
"""
import logging
import os
import re
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEEP = 5                                   # 每类日志最多保留的文件数（含当天）
LOG_DIR = os.path.abspath(
    os.environ.get("WXBOT_LOG_DIR") or os.path.join(BASE_DIR, "data", "logs"))
_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log$")

_lock = threading.RLock()
_dir_ready = [False]


def ensure_dir():
    if _dir_ready[0]:
        return
    with _lock:
        if _dir_ready[0]:
            return
        os.makedirs(LOG_DIR, exist_ok=True)
        _dir_ready[0] = True


def today():
    return time.strftime("%Y-%m-%d", time.localtime())


def resolve(env_var, stem):
    """返回该类日志**当前应写入/读取**的路径（每次调用都重新算，跨天不会写错文件）。"""
    fixed = os.environ.get(env_var)
    if fixed:
        return os.path.abspath(fixed)
    ensure_dir()
    return os.path.join(LOG_DIR, "%s-%s.log" % (stem, today()))


def prune(stem, keep=KEEP):
    """只留最近 `keep` 个 `<stem>-YYYY-MM-DD.log`，返回保留数量。

    按文件名里的日期排序而不是 mtime：手工 touch / 复制备份也不会打乱顺序。
    """
    ensure_dir()
    prefix = stem + "-"
    items = []
    try:
        names = os.listdir(LOG_DIR)
    except OSError:
        return 0
    for n in names:
        if not n.startswith(prefix) or not n.endswith(".log"):
            continue
        m = _FILE_RE.match(n[len(prefix):])
        if m:
            items.append((m.group(1), os.path.join(LOG_DIR, n)))
    items.sort(reverse=True)
    for _, p in items[keep:]:
        try:
            os.remove(p)
        except OSError:
            pass
    return len(items[:keep])


class DailyFileHandler(logging.FileHandler):
    """每天一个文件；跨天自动切换；同一类日志最多 KEEP 个文件。

    `env_var` 指定环境变量时（自动化测试）退化为普通 FileHandler，
    不轮转、不清理 —— 测试要的是固定路径与稳定 inode。
    """

    def __init__(self, stem, env_var, mode="a", encoding="utf-8"):
        self._stem = stem
        self._env = env_var
        self._fixed = bool(os.environ.get(env_var))
        self._day = today()
        ensure_dir()
        super().__init__(resolve(env_var, stem), mode=mode,
                         encoding=encoding, delay=False)
        if not self._fixed:
            prune(stem)

    def emit(self, record):
        if not self._fixed and today() != self._day:
            self._switch_day()
        super().emit(record)

    def _switch_day(self):
        with _lock:
            if today() == self._day:
                return
            self.acquire()
            try:
                self.flush()
                if self.stream:
                    try:
                        self.stream.close()
                    finally:
                        self.stream = None
                self._day = today()
                self.baseFilename = os.path.abspath(
                    resolve(self._env, self._stem))
                self.stream = self._open()
                prune(self._stem)
            finally:
                self.release()


def daily_handler(stem, env_var, formatter=None):
    h = DailyFileHandler(stem, env_var)
    if formatter:
        h.setFormatter(formatter)
    return h


def out_log_path():
    """主服务启动输出（子进程 stdout）的当天文件路径。"""
    return resolve("WXBOT_SERVICE_LOG", "webhook_server.out")
