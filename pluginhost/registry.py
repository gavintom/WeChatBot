"""插件注册表：发现 → manifest 校验 → 加载 → 优先级排序 → 派发 → 熔断。

可靠性约定：
  * 单个插件加载失败 / handler 抛异常，都只影响它自己，绝不影响其它插件与客服主链路；
  * 注册表整体不可变，重载时「先构造新表、再原子替换指针」，消息开始时取一次快照；
  * 同步线程无法安全强杀，因此**不实现硬超时**，改用协作式约束 + 连续失败熔断；
  * 插件代码与宿主同进程运行 = 可信代码，这里不做沙箱。
"""
import importlib.util
import os
import sys
import threading
import time

import yaml

from . import PluginBase
from .consts import (ACTIONS, CONTINUE, DEFAULT_PRIORITY, HOOKS, HOST_VERSION,
                     HTTP_HOOK, MAX_PRIORITY, MIN_PRIORITY,
                     NAME_RE, SCHEDULE_HOOK, STOP_ALL, STOP_PLUGINS)

CONFIG_TTL = 30.0          # 插件私有配置兜底缓存；文件 mtime 一变立即失效


def _ver(v):
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except Exception:
        return (0,)


def _cfg_bool(v):
    """私有配置里的布尔判定，与插件侧 _bool 同口径，避免 enable 这类非法值误判。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y", "是", "开")
    return bool(v)


def _kind_match(kinds, kind):
    return "*" in kinds or (kind or "") in kinds


def _default_store():
    import store
    return StoreState(store)


class MemState:
    """内存态：单元测试 / 数据库不可用时使用。接口与 StoreState 完全一致。"""

    def __init__(self):
        self.rows = {}
        self._lock = threading.Lock()

    def ensure(self, name, source="", priority=None):
        with self._lock:
            return dict(self.rows.setdefault(name, {
                "name": name, "enabled": 1, "priority_override": priority,
                "source": source or "", "fail_count": 0, "last_error": ""}))

    def state(self, name):
        with self._lock:
            r = self.rows.get(name)
            return dict(r) if r else None

    def all(self):
        with self._lock:
            return [dict(r) for r in self.rows.values()]

    def set_enabled(self, name, on):
        with self._lock:
            r = self.rows.setdefault(name, {"name": name, "priority_override": None,
                                            "source": "", "fail_count": 0, "last_error": ""})
            r["enabled"] = 1 if on else 0
            if on:
                r["fail_count"] = 0
                r["last_error"] = ""

    def set_priority(self, name, p):
        with self._lock:
            r = self.rows.setdefault(name, {"name": name, "enabled": 1,
                                            "source": "", "fail_count": 0, "last_error": ""})
            r["priority_override"] = p

    def failure(self, name, err, threshold=5):
        with self._lock:
            r = self.rows.setdefault(name, {"name": name, "enabled": 1,
                                            "priority_override": None, "source": "",
                                            "fail_count": 0, "last_error": ""})
            r["fail_count"] = int(r.get("fail_count") or 0) + 1
            r["last_error"] = str(err)[:300]
            if r["fail_count"] >= max(1, int(threshold)):
                r["enabled"] = 0
                return True
            return False

    def reset_failure(self, name):
        with self._lock:
            r = self.rows.get(name)
            if r:
                r["fail_count"] = 0
                r["last_error"] = ""


class StoreState:
    """把 store.py 的插件 CRUD 适配成 registry 需要的接口。"""

    def __init__(self, mod):
        self.m = mod

    def ensure(self, name, source="", priority=None):
        return self.m.ensure_plugin(name, source, priority) or {}

    def state(self, name):
        return self.m.plugin_state(name)

    def all(self):
        return self.m.list_plugins()

    def set_enabled(self, name, on):
        return self.m.set_plugin_enabled(name, bool(on))

    def set_priority(self, name, p):
        return self.m.set_plugin_priority(name, p)

    def failure(self, name, err, threshold=5):
        return self.m.mark_plugin_failure(name, err, threshold)

    def reset_failure(self, name):
        return self.m.reset_plugin_failure(name)


class _PluginAPI:
    """生命周期回调（on_load / on_enable）里可用的最小宿主能力。"""

    def __init__(self, name, registry):
        self.name = name
        self._registry = registry

    @property
    def log(self):
        return self._registry.log

    @property
    def config(self):
        return self._registry.plugin_config(self.name)


class _Handler:
    __slots__ = ("plugin", "hook", "priority", "kinds", "fn", "sort", "meta")

    def __init__(self, plugin, hook, priority, kinds, fn, sort, meta=None):
        self.plugin = plugin
        self.hook = hook
        self.priority = priority
        self.kinds = kinds
        self.fn = fn
        self.sort = sort
        self.meta = meta or {}


class PluginRegistry:
    """一个不可变快照。重载时构造新实例再整体替换，旧消息继续用旧快照。"""

    def __init__(self, cfg=None, *, base_dir, host, log, state=None):
        cfg = dict(cfg or {})
        self.enabled = bool(cfg.get("enabled", False))
        self.base_dir = base_dir
        self.host = host
        self.log = log
        self.state = state if state is not None else _default_store()
        self.config_dir = self._abs(cfg.get("config_dir") or "data/plugins")
        self.dirs = []
        for key, default in (("dir", "plugins"), ("extra_dir", "")):
            v = cfg.get(key) or default
            if v:
                self.dirs.append(self._abs(v))
        self.fail_threshold = self._int(cfg.get("fail_threshold"), 5, lo=1, hi=1000)
        self._specs = {}          # name -> manifest
        self._plugins = {}        # name -> 实例
        self._sources = {}        # name -> 目录
        self._priorities = {}    # name -> 实际优先级
        self._by_hook = {}        # hook -> [_Handler]（已排序；只含消息类 hook）
        self._schedules = []     # P3：定时任务（调度线程消费）
        self._routes = []        # P3：HTTP 扩展路由（/ext 通配路由消费）
        self._disabled = {}      # name -> 原因（后台禁用 / 熔断）
        self._fails = {}         # name -> 连续异常次数（内存）
        self._cfg_cache = {}     # name -> {"ts","sig","data"}
        self._lock = threading.RLock()
        self._cfg_lock = threading.Lock()
        self.errors = []
        self.loaded_at = 0.0
        # 整体构造失败标记：rebuild() 见到它就保留旧注册表，绝不用空表替换。
        # 单个插件加载失败不算 fatal（其余插件仍可用），只记进 errors。
        self.fatal = False

    @staticmethod
    def _int(v, default, lo=0, hi=10 ** 9):
        try:
            return min(hi, max(lo, int(v)))
        except (TypeError, ValueError):
            return default

    def _abs(self, p):
        p = str(p or "")
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(self.base_dir, p))

    # ------------------------------------------------------------ 加载

    def load(self):
        """扫描目录并加载全部插件。任何单个失败都只记录、不抛出。"""
        if not self.enabled:
            return
        with self._lock:
            for d in self.dirs:
                if not os.path.isdir(d):
                    self.log.info("插件目录不存在，已跳过 | %s", d)
                    continue
                for name in sorted(os.listdir(d)):
                    p = os.path.join(d, name)
                    if not os.path.isdir(p) or name.startswith((".", "_")):
                        continue
                    if name in self._specs:      # 内置目录优先，同名第三方忽略
                        continue
                    try:
                        self._load_one(name, p, d)
                    except Exception as e:
                        msg = "%s: %s" % (name, e)
                        self.errors.append(msg)
                        self.log.warning("插件加载失败（已跳过，不影响其它插件）| %s", msg)
            for hook in self._by_hook:
                self._by_hook[hook].sort(
                    key=lambda h: (-h.priority, h.plugin, h.sort))
            self._schedules.sort(key=lambda t: t["key"])
            self._routes.sort(key=lambda r: r["key"])
            self.loaded_at = time.time()
            self.log.info("插件系统就绪 | 已启用=%d | 处理器=%d | 错误=%d",
                         len(self._plugins),
                         sum(len(v) for v in self._by_hook.values()), len(self.errors))

    def _load_one(self, name, path, source_dir):
        if not NAME_RE.match(name):
            raise ValueError("插件目录名不合法（需 ^[A-Za-z][A-Za-z0-9_]{1,47}$）")
        mf = os.path.join(path, "plugin.yaml")
        if not os.path.isfile(mf):
            raise ValueError("缺少 plugin.yaml")
        with open(mf, encoding="utf-8") as f:
            spec = yaml.safe_load(f) or {}
        if not isinstance(spec, dict):
            raise ValueError("plugin.yaml 必须是键值映射")
        if str(spec.get("name") or name) != name:
            raise ValueError("plugin.yaml 的 name 必须与目录名一致")
        minv = spec.get("min_host_version")
        if minv and _ver(minv) > _ver(HOST_VERSION):
            raise ValueError("需要宿主版本 >= %s（当前 %s）" % (minv, HOST_VERSION))
        declared = spec.get("hooks") or []
        if declared and (not isinstance(declared, (list, tuple)) or
                         any(str(h) not in HOOKS for h in declared)):
            raise ValueError("plugin.yaml 的 hooks 含非法 hook 名")
        entry = str(spec.get("entry") or "main:Plugin")
        if ":" not in entry:
            raise ValueError("entry 必须是 '模块:类名'")
        module, cls_name = entry.split(":", 1)
        if not (module and cls_name and
                module.replace("_", "").isalnum() and
                cls_name.replace("_", "").isalnum()):
            raise ValueError("entry 只能包含模块名与类名，不能带路径")
        if not os.path.isfile(os.path.join(path, "__init__.py")):
            raise ValueError("缺少 __init__.py（需要从中导出 %s）" % cls_name)
        if not os.path.isfile(os.path.join(path, module + ".py")):
            raise ValueError("entry 指向的 %s.py 不存在" % module)
        prio = self._int(spec.get("priority"), DEFAULT_PRIORITY,
                         lo=MIN_PRIORITY, hi=MAX_PRIORITY)

        self._specs[name] = {
            "name": name, "version": str(spec.get("version") or "1.0.0"),
            "description": str(spec.get("description") or ""),
            "author": str(spec.get("author") or ""),
            "priority": prio, "entry": entry,
            "declared_hooks": [str(h) for h in declared],
        }
        self._sources[name] = source_dir
        self._priorities[name] = prio

        try:
            row = self.state.ensure(name, source=source_dir) or {}
        except Exception as e:
            self.log.warning("插件状态读取失败（按默认启用继续）| %s | %s", name, e)
            row = {}
        override = row.get("priority_override")
        eff_prio = prio
        if override is not None:
            eff_prio = self._int(override, prio, lo=MIN_PRIORITY, hi=MAX_PRIORITY)
        # 即使插件被禁用也要显示真实优先级：后台允许对停用插件先调优先级再启用，
        # 若这里提前 return，清单会退回 manifest 默认值，运营会误以为改了没生效。
        self._priorities[name] = eff_prio
        # 私有配置 enabled 是权威开关：data/plugins/<name>.yaml 里若显式写了
        # enabled: false，则不加载、后台显示"配置禁用"。没有该字段时沿用后台状态，
        # 保持对老插件 / 无私有配置插件的兼容。后台的启用/禁用按钮会同步改写该文件。
        pcfg = self.plugin_config(name)
        if "enabled" in pcfg and not _cfg_bool(pcfg.get("enabled")):
            self._disabled[name] = "配置中 enabled=false"
            self.log.info("插件私有配置已禁用，跳过加载 | %s", name)
            return
        if not row.get("enabled", 1):
            self._disabled[name] = "后台已禁用"
            self.log.info("插件已禁用，跳过加载 | %s", name)
            return

        # ---------------------------------------------------- 动态导入
        mod_name = "wxbot_plugin_%s" % name
        sys.modules.pop(mod_name, None)      # 重载时丢掉旧模块对象
        sp = importlib.util.spec_from_file_location(
            mod_name, os.path.join(path, "__init__.py"),
            submodule_search_locations=[path])
        if sp is None or sp.loader is None:
            raise ValueError("无法创建模块 spec")
        mod = importlib.util.module_from_spec(sp)
        sys.modules[mod_name] = mod
        try:
            sp.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(mod_name, None)
            raise
        cls = getattr(mod, cls_name, None)
        if not isinstance(cls, type) or not issubclass(cls, PluginBase):
            raise ValueError("%s 必须继承 pluginhost.PluginBase" % cls_name)
        inst = cls()
        inst.api = _PluginAPI(name, self)
        self._plugins[name] = inst

        # ---------------------------------------------------- 收集 handler
        n = ns = nr = 0
        for meth in sorted(dir(cls)):
            raw = getattr(cls, meth, None)
            marks = getattr(raw, "_plugin_marks", None)
            if not marks:
                continue
            fn = getattr(inst, meth, None)
            if not callable(fn):
                continue
            for mk in marks:
                hook = mk["hook"]
                p = mk["priority"] if mk["priority"] is not None else prio
                if override is not None:
                    p = eff_prio
                h = _Handler(name, hook, p, mk["kinds"], fn,
                             "%s:%d" % (meth, mk["order"]), meta=mk)
                if hook == SCHEDULE_HOOK:
                    spec = mk.get("schedule")
                    if not isinstance(spec, dict):
                        raise ValueError("%s.%s 缺少合法的 schedule 元数据" % (name, meth))
                    tname = str(spec.get("name") or meth)
                    self._schedules.append({
                        "key": "%s:%s" % (name, tname), "plugin": name,
                        "task": tname, "spec": spec, "handler": h})
                    ns += 1
                elif hook == HTTP_HOOK:
                    spec = mk.get("http")
                    if not isinstance(spec, dict) or not spec.get("path"):
                        raise ValueError("%s.%s 缺少合法的 http 元数据" % (name, meth))
                    rname = str(spec.get("name") or meth)
                    self._routes.append({
                        "key": "%s:%s" % (name, rname), "plugin": name,
                        "route": rname, "method": str(spec.get("method") or "GET"),
                        "path": str(spec.get("path")), "handler": h})
                    nr += 1
                else:
                    self._by_hook.setdefault(hook, []).append(h)
                    n += 1
        for cb in ("on_load", "on_enable"):
            try:
                getattr(inst, cb)()
            except Exception as e:
                self.errors.append("%s.%s: %s" % (name, cb, e))
                self.log.warning("插件生命周期回调异常（已忽略）| %s.%s | %s", name, cb, e)
        self.log.info("插件已加载 | %s v%s | 优先级=%d | 消息处理器=%d | 定时任务=%d | 扩展路由=%d",
                     name, self._specs[name]["version"], eff_prio, n, ns, nr)

    def shutdown(self):
        """注册表被替换前调用，给插件释放自己持有的资源。"""
        with self._lock:
            for name, inst in self._plugins.items():
                for cb in ("on_disable", "on_unload"):
                    try:
                        getattr(inst, cb)()
                    except Exception as e:
                        self.log.warning("插件清理回调异常 | %s.%s | %s", name, cb, e)

    # ------------------------------------------------------------ 配置热读

    def plugin_config(self, name):
        """读 data/plugins/<name>.yaml。mtime 变了立即重读，不依赖跨进程通知。"""
        if not name or not NAME_RE.match(name):
            return {}
        path = os.path.join(self.config_dir, name + ".yaml")
        try:
            st = os.stat(path)
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        now = time.time()
        with self._cfg_lock:
            c = self._cfg_cache.get(name)
            if (c and c["sig"] == sig and
                    (sig is None or now - c["ts"] < CONFIG_TTL)):
                return dict(c["data"])
            data = {}
            if sig is not None:
                try:
                    with open(path, encoding="utf-8") as f:
                        d = yaml.safe_load(f) or {}
                    if isinstance(d, dict):
                        data = d
                    else:
                        self.log.warning("插件配置必须是键值映射 | %s", path)
                except Exception as e:
                    self.log.warning("插件配置读取失败（按空配置继续）| %s | %s", path, e)
            self._cfg_cache[name] = {"ts": now, "sig": sig, "data": data}
            return dict(data)

    # ------------------------------------------------------------ 派发

    # ------------------------------------------------------------ P3 清单

    def schedules(self):
        """给调度线程消费：含 handler 对象，**不可**直接 jsonify。"""
        if not self.enabled:
            return []
        with self._lock:
            return [dict(t, disabled=t["plugin"] in self._disabled)
                    for t in self._schedules]

    def routes(self):
        if not self.enabled:
            return []
        with self._lock:
            return [dict(r, disabled=r["plugin"] in self._disabled)
                    for r in self._routes]

    def match_route(self, plugin, method, path):
        """精确匹配插件名 + HTTP 方法 + 子路径；不做通配，避免误命中。"""
        if not self.enabled or not NAME_RE.match(str(plugin or "")):
            return None
        method = str(method or "").upper()
        path = "/" + str(path or "").strip("/")
        with self._lock:
            for r in self._routes:
                if (r["plugin"] == plugin and r["path"] == path and
                        r["method"] in (method, "*")):
                    return dict(r, disabled=r["plugin"] in self._disabled)
        return None

    def invoke(self, handler, ctx):
        """定时任务 / HTTP 路由的统一调用口，复用消息 hook 的异常隔离与熔断。

        返回 (result, error)；异常被吞掉并记入连续失败计数，调用方决定
        是返回 500 还是仅记日志，主链路绝不受影响。
        """
        ctx.plugin_name = handler.plugin
        try:
            r = handler.fn(ctx)
            self._record_success(handler.plugin)
            return r, ""
        except Exception as e:
            self._record_failure(handler.plugin, e, handler.hook)
            return None, str(e)
        finally:
            ctx.plugin_name = ""

    def dispatch(self, hook, ctx, *, kind=None):
        """按优先级串行派发。单个 handler 异常被隔离，按 CONTINUE 放行。

        返回 CONTINUE / STOP_PLUGINS / STOP_ALL 之一；调用方只需关心 STOP_ALL。
        """
        handlers = self._by_hook.get(hook) or ()
        if not self.enabled or not handlers:
            return CONTINUE
        try:
            for h in handlers:
                if h.plugin in self._disabled:
                    continue
                if kind is not None and not _kind_match(h.kinds, kind):
                    continue
                ctx.plugin_name = h.plugin
                try:
                    r = h.fn(ctx)
                    self._record_success(h.plugin)
                except Exception as e:
                    self._record_failure(h.plugin, e, hook)
                    continue
                if r == STOP_ALL:
                    # 固化接管插件名：下面 finally 会清空 plugin_name，
                    # 但宿主要用它写「对话记录」的插件来源（takeover 事件在 dispatch 外）。
                    ctx.takeover_by = h.plugin
                    return STOP_ALL
                if r == STOP_PLUGINS:
                    return STOP_PLUGINS
                if r not in ACTIONS and r is not None:
                    self.log.warning("插件 %s.%s 返回非法值 %r，按 CONTINUE 处理",
                                     h.plugin, hook, r)
        finally:
            ctx.plugin_name = ""
        return CONTINUE

    def _record_success(self, name):
        if self._fails.get(name):
            self._fails[name] = 0
            try:
                self.state.reset_failure(name)
            except Exception as e:
                self.log.warning("重置插件失败计数失败 | %s | %s", name, e)

    def _record_failure(self, name, exc, hook):
        self._fails[name] = self._fails.get(name, 0) + 1
        n = self._fails[name]
        self.log.warning("插件异常（已隔离，按 CONTINUE 放行）| %s.%s | 第 %d 次 | %s",
                         name, hook, n, exc)
        if n < self.fail_threshold:
            return
        self._disabled[name] = "连续 %d 次异常，已熔断禁用" % n
        try:
            self.state.failure(name, str(exc), threshold=10 ** 9)
        except Exception:
            pass
        try:
            self.state.set_enabled(name, False)
        except Exception as e:
            self.log.warning("持久化插件熔断状态失败 | %s | %s", name, e)
        self.log.error("插件 %s 连续 %d 次异常，已自动熔断禁用 | 最后错误：%s",
                       name, n, exc)

    # ------------------------------------------------------------ 可观测

    def describe(self):
        plugins = []
        for name in sorted(self._specs):
            spec = self._specs[name]
            hooks = sorted(h for h, hs in self._by_hook.items()
                           if any(x.plugin == name for x in hs))
            plugins.append({
                "name": name,
                "version": spec["version"],
                "description": spec["description"],
                "author": spec["author"],
                "enabled": name in self._plugins and name not in self._disabled,
                "priority": self._priorities.get(name, DEFAULT_PRIORITY),
                "hooks": hooks,
                "source": self._sources.get(name, ""),
                "fail_count": self._fails.get(name, 0),
                "disabled_reason": self._disabled.get(name, ""),
            })
        return {
            "enabled": self.enabled,
            "count": len(self._plugins),
            "handlers": sum(len(v) for v in self._by_hook.values()),
            "loaded_at": self.loaded_at,
            "dirs": list(self.dirs),
            "config_dir": self.config_dir,
            "plugins": plugins,
            "errors": list(self.errors),
            # 静态规格（不含不可序列化的 handler）；运行时状态走
            # /internal/plugins 的 scheduler 字段与 /internal/plugins/schedules。
            "schedules": [{"key": t["key"], "plugin": t["plugin"],
                           "task": t["task"], "kind": t["spec"]["kind"],
                           "spec": dict(t["spec"])} for t in self._schedules],
            "routes": [{"key": r["key"], "plugin": r["plugin"],
                        "route": r["route"], "method": r["method"],
                        "path": r["path"]} for r in self._routes],
        }


def load_registry(cfg=None, *, base_dir, host, log, state=None):
    """构造一个注册表。整体失败时返回空注册表，绝不让主服务启动失败。"""
    reg = PluginRegistry(cfg, base_dir=base_dir, host=host, log=log, state=state)
    if not reg.enabled:
        return reg
    try:
        reg.load()
    except Exception as e:
        try:
            log.exception("插件系统初始化异常，已降级为空注册表：%s", e)
        except Exception:
            log.warning("插件系统初始化异常，已降级为空注册表：%s", e)
        reg.errors.append("初始化异常：%s" % e)
        reg.fatal = True
        with reg._lock:
            reg._by_hook.clear()
            reg._plugins.clear()
            reg._schedules.clear()
            reg._routes.clear()
    return reg


def rebuild(cfg=None, *, base_dir, host, log, state=None, old=None):
    """构造新注册表；整体失败且存在旧表时**保留旧表**，返回 (registry, error)。

    P2 后台「一键重载」直接依赖这个语义：插件目录临时被删、配置写坏、
    import 阶段整体炸掉时，客服仍按上一份可用注册表继续跑，而不是全部插件
    瞬间消失。启动时没有旧表，只能退回空表（保证主服务照样起得来）。
    """
    new = load_registry(cfg, base_dir=base_dir, host=host, log=log, state=state)
    if not getattr(new, "fatal", False):
        return new, ""
    err = "；".join(new.errors) or "插件注册表构造失败"
    if old is not None:
        return old, err
    return new, err
