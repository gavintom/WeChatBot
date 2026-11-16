"""定时任务调度器：单 daemon tick 线程 + 独立小执行池，零新增依赖。

设计要点：

* 主服务 `workers=1`（gunicorn.conf.py），所以调度器**只会存在一个实例**，
  天然满足「定时任务只触发一次」；snapshot() 会暴露 pid 与线程名便于核验。
* tick 线程只负责判定到期并把任务丢进小线程池，慢任务不会卡住 tick，
  也不会占用消息处理的 POOL；同一任务用 `running` 标记防止并发重入。
* 错过的时间**不追补**：一次 tick 每个任务最多触发一次，next_run 直接推到
  下一个周期，避免进程暂停/重启后瞬间群发几十条。
* 异常走 registry.invoke() 的隔离与连续失败熔断，绝不影响客服主链路。
* 不做完整 cron，不引入 APScheduler。`daily` 用本地挂钟时间，容器 TZ 与
  业务时区不一致时用 `plugins.scheduler.utc_offset` 显式指定（如东八区填 8）。
"""
import datetime
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .consts import MAX_SCHEDULE_SECONDS
from .context import TaskContext

_EPOCH = datetime.datetime(1970, 1, 1)


def _num(v, default, lo, hi, cast=float):
    try:
        return min(hi, max(lo, cast(v)))
    except (TypeError, ValueError):
        return default


class PluginScheduler:
    """一个进程一份。构造不启动线程，显式调用 start()（幂等）。"""

    def __init__(self, get_registry, cfg=None, *, host, log, task_store=None):
        c = dict(cfg or {})
        self.enabled = bool(c.get("enabled", True))
        self.tick = _num(c.get("tick_seconds"), 1.0, 0.2, 10.0)
        self.max_workers = int(_num(c.get("max_workers"), 2, 1, 4, int))
        off = c.get("utc_offset")
        if off in (None, ""):
            off = getattr(time.localtime(), "tm_gmtoff", 0) / 3600.0
        self.utc_offset = _num(off, 0.0, -14.0, 14.0)
        self._get_registry = get_registry
        self._task_store = task_store   # P4：任务实例覆盖表（None 时行为与 P3 完全一致）
        self._host = host
        self._log = log
        self._lock = threading.RLock()
        self._states = {}          # key -> 可变状态字典
        self._pool = None
        self._thread = None
        self._started_at = 0.0
        self.ticks = 0
        self.pid = os.getpid()

    # ------------------------------------------------------------ 生命周期

    def start(self):
        """启动 daemon tick 线程。即便配置关闭也不抛异常。"""
        with self._lock:
            if not self.enabled or self._thread is not None:
                return False
            self._pool = ThreadPoolExecutor(
                max_workers=self.max_workers, thread_name_prefix="plugin-task")
            t = threading.Thread(target=self._loop, name="plugin-scheduler",
                                 daemon=True)
            self._thread = t
            self._started_at = time.time()
        t.start()
        self._log.info(
            "插件定时任务调度器已启动 | tick=%ss | 线程=1 | 执行池=%d | 时区=UTC%+.1f | pid=%d",
            self.tick, self.max_workers, self.utc_offset, self.pid)
        return True

    def _loop(self):
        while True:
            try:
                self.tick_once()
            except Exception as e:      # 调度器本身绝不能死
                self._log.warning("插件定时任务 tick 异常（已忽略）: %s", e)
            time.sleep(self.tick)

    # ------------------------------------------------------------ 周期计算

    def _interval(self, task, reg):
        """interval 实际周期；私有配置的 config_key 可热改周期（mtime 热读）。"""
        spec = task["spec"]
        secs = int(spec.get("seconds") or 60)
        key = spec.get("config_key")
        if key:
            try:
                val = (reg.plugin_config(task["plugin"]) or {}).get(key)
                n = int(val)
                if 1 <= n <= MAX_SCHEDULE_SECONDS:
                    secs = n
            except (TypeError, ValueError, AttributeError):
                pass
        return float(secs)

    def _next_daily(self, now, hour, minute):
        """下一个 UTC+offset 挂钟时间 hour:minute 的真实时间戳。"""
        off = self.utc_offset * 3600.0
        local = datetime.datetime.fromtimestamp(
            now + off, datetime.timezone.utc).replace(tzinfo=None)
        target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= local:
            target += datetime.timedelta(days=1)
        return (target - _EPOCH).total_seconds() - off

    def _fingerprint(self, task, reg):
        spec = task["spec"]
        if spec.get("kind") == "daily":
            return "daily:%02d:%02d" % (spec["hour"], spec["minute"])
        return "interval:%d" % self._interval(task, reg)

    def _next(self, task, reg, now):
        spec = task["spec"]
        if spec.get("kind") == "daily":
            return self._next_daily(now, spec["hour"], spec["minute"])
        return now + self._interval(task, reg)

    def _describe(self, task, reg):
        """展示**当前实际生效**的周期：私有配置覆盖后要能看到新值。"""
        spec = task["spec"]
        if spec.get("kind") == "daily":
            return "每天 %02d:%02d" % (spec["hour"], spec["minute"])
        s = "每 %d 秒" % int(self._interval(task, reg))
        return s + ("（可由 %s 覆盖）" % spec["config_key"] if spec.get("config_key") else "")

    # ------------------------------------------------------------ tick

    def tick_once(self, now=None):
        """判定并执行到期任务，返回本次提交的任务数。单元测试可直接调用。"""
        now = time.time() if now is None else float(now)
        with self._lock:
            self.ticks += 1
        reg = self._get_registry()
        if reg is None or not getattr(reg, "enabled", False):
            return 0
        tasks = reg.schedules()
        if self._task_store is not None:
            # 覆盖 / 停用 / 自定义实例都在这里生效：文件按 mtime 热读，
            # 管理端保存后最多一个 tick 就生效，不需要重载插件注册表。
            try:
                tasks = self._task_store.apply(tasks)
            except Exception as e:
                self._log.warning("插件任务实例配置异常，本次 tick 按代码声明继续: %s", e)
                tasks = reg.schedules()
        due = []
        with self._lock:
            seen = set()
            for t in tasks:
                key = t["key"]
                seen.add(key)
                fp = self._fingerprint(t, reg)
                st = self._states.get(key)
                if st is None:
                    st = {
                        "key": key, "plugin": t["plugin"], "task": t["task"],
                        "kind": t["spec"].get("kind", "interval"),
                        "schedule": self._describe(t, reg), "fingerprint": fp,
                        "next_run": self._next(t, reg, now), "last_run": 0.0,
                        "runs": 0, "fails": 0, "last_error": "", "last_ms": 0,
                        "running": False, "disabled": False,
                    }
                    self._states[key] = st
                # 静态字段与周期可能因重载 / 私有配置热读而变化
                st["schedule"] = self._describe(t, reg)
                st["disabled"] = bool(t.get("disabled"))
                if st["fingerprint"] != fp:
                    st["fingerprint"] = fp
                    st["next_run"] = self._next(t, reg, now)
                if st["disabled"]:
                    # 熔断期间持续推后，恢复后不会把错过的次数补跑
                    st["next_run"] = self._next(t, reg, now)
                    continue
                if st["running"] or now < st["next_run"]:
                    continue
                # 先推进再提交：无论执行耗时多久，都不会在下次 tick 重复触发
                st["next_run"] = self._next(t, reg, now)
                st["running"] = True
                due.append((t, st))
            for key in list(self._states):
                if key not in seen:
                    del self._states[key]      # 插件被禁用 / 目录删除后清状态
        for t, st in due:
            if self._pool is not None:
                self._pool.submit(self._run, t, st, reg)
            else:
                # 未 start() 时（单元测试 / 手动 tick）同步跑，语义一致
                self._run(t, st, reg)
        return len(due)

    def _run(self, task, st, reg):
        t0 = time.time()
        ctx = TaskContext(plugin_name=task["plugin"], task_name=task["task"],
                          host=self._host, log=self._log, registry=reg,
                          run_count=int(st["runs"]) + 1)
        _, err = reg.invoke(task["handler"], ctx)
        with self._lock:
            st["running"] = False
            st["last_run"] = t0
            st["last_ms"] = int((time.time() - t0) * 1000)
            st["runs"] += 1
            if err:
                st["fails"] += 1
                st["last_error"] = str(err)[:200]
            else:
                st["last_error"] = ""
        return err

    # ------------------------------------------------------------ 可观测

    def snapshot(self):
        """可 JSON 序列化的运行时状态；管理端与 /internal/plugins 直接使用。"""
        with self._lock:
            tasks = [dict(s) for s in self._states.values()]
            ticks = self.ticks
            started = self._started_at
            pool = self._pool is not None
        tasks.sort(key=lambda x: x["key"])
        alive = bool(self._thread and self._thread.is_alive())
        return {
            "enabled": self.enabled, "running": alive,
            "pid": self.pid, "thread": self._thread.name if self._thread else "",
            "threads": 1 if alive else 0, "pool": pool,
            "tick_seconds": self.tick, "max_workers": self.max_workers,
            "utc_offset": self.utc_offset, "ticks": ticks,
            "started_at": started, "task_count": len(tasks), "tasks": tasks,
        }
