"""插件定时任务实例管理（P4）：`data/plugins/_tasks.yaml` 的读取与合并。

为什么需要这一层：定时任务的**执行函数**写在插件代码的 `@schedule` 装饰器里，
注册表每次重载都从代码重新读，管理端不可能凭空造出一个有行为的新任务。所以
后台能管理的是「实例」：

* `overrides`：对代码已声明任务做覆盖 —— 启停（停用即视为移除）、周期、显示名；
* `instances`：以某个已声明任务的 handler 为模板，复制出一个新实例，
  拥有独立 key / 周期 / 运行状态（执行次数、下次执行时间互不干扰）。

文件由管理端写入（同机同卷），主服务按 mtime 热读，**不需要重启也不需要重载
插件注册表**。任何解析异常都降级为「全部按代码声明执行」，绝不影响客服主链路。

格式：

    version: 1
    overrides:
      ExamplePlugin:heartbeat:
        enabled: false
        label: 心跳
        kind: interval
        seconds: 30
    instances:
      - key: ExamplePlugin:heartbeat#fast
        from: ExamplePlugin:heartbeat
        label: 快速心跳
        enabled: true
        kind: interval
        seconds: 10
"""
import os
import re
import threading
import time

import yaml

from .consts import MAX_SCHEDULE_SECONDS

# <插件名>:<任务名>[#<实例后缀>]。实例后缀只允许安全字符，且必须以字母开头，
# 这样它既能当任务名用，也不会被误当成路径或 YAML 锚点。
KEY_RE = re.compile(
    r"^(?P<plugin>[A-Za-z][A-Za-z0-9_]{1,47}):"
    r"(?P<task>[A-Za-z][A-Za-z0-9_]{0,47})"
    r"(?:#(?P<suffix>[A-Za-z][A-Za-z0-9_]{0,31}))?$"
)
MAX_FILE = 65536
MAX_LABEL = 64
MAX_ENTRIES = 100


def normalize_key(value):
    """合格返回规范化 key，否则返回 None。"""
    s = str(value or "").strip()
    m = KEY_RE.match(s)
    return s if m else None


def normalize_spec(entry):
    """从一条覆盖配置里提取周期；非法返回 None（回落插件代码声明值）。"""
    if not isinstance(entry, dict):
        return None
    kind = str(entry.get("kind") or "").strip().lower()
    if kind == "interval":
        try:
            sec = int(entry.get("seconds"))
        except (TypeError, ValueError):
            return None
        if not 1 <= sec <= MAX_SCHEDULE_SECONDS:
            return None
        return {"kind": "interval", "seconds": sec, "name": ""}
    if kind == "daily":
        try:
            h = int(entry.get("hour"))
            mi = int(entry.get("minute") if entry.get("minute") is not None else 0)
        except (TypeError, ValueError):
            return None
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return None
        return {"kind": "daily", "hour": h, "minute": mi, "name": ""}
    return None


def public_spec(spec):
    """只保留可 JSON 序列化的白名单字段，避免把 handler / 私有对象带出去。"""
    s = spec if isinstance(spec, dict) else {}
    out = {"kind": str(s.get("kind") or "interval")}
    if out["kind"] == "daily":
        out["hour"] = int(s.get("hour") or 0)
        out["minute"] = int(s.get("minute") or 0)
    else:
        out["seconds"] = int(s.get("seconds") or 60)
    if s.get("config_key"):
        out["config_key"] = str(s["config_key"])
    if s.get("name"):
        out["name"] = str(s["name"])
    return out


def _label(v, fallback):
    s = str(v or "").strip().replace("\n", " ").replace("\r", " ")
    return s[:MAX_LABEL] or fallback


class TaskStore:
    """一个主服务进程一份；read() 按 mtime 热读，view()/apply() 无副作用。"""

    def __init__(self, path, log=None):
        self.path = os.path.abspath(str(path))
        self.log = log
        self._lock = threading.RLock()
        self._sig = None
        self._data = {"overrides": {}, "instances": []}
        self.error = ""
        self.loaded_at = 0.0

    def _warn(self, msg, *a):
        if self.log is not None:
            try:
                self.log.warning(msg, *a)
            except Exception:
                pass

    # ------------------------------------------------------------ 读取

    def read(self):
        with self._lock:
            try:
                st = os.stat(self.path)
                sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                sig = None
            if sig == self._sig:
                return self._data
            data, err = {"overrides": {}, "instances": []}, ""
            if sig is not None:
                try:
                    if st.st_size > MAX_FILE:
                        err = "任务配置超过 64KB，已忽略"
                    else:
                        with open(self.path, encoding="utf-8") as f:
                            raw = yaml.safe_load(f) or {}
                        if not isinstance(raw, dict):
                            err = "任务配置顶层必须是键值映射"
                        else:
                            ovs = raw.get("overrides") or {}
                            if isinstance(ovs, dict):
                                for k, v in list(ovs.items())[:MAX_ENTRIES]:
                                    key = normalize_key(k)
                                    if not key or "#" in key or not isinstance(v, dict):
                                        continue
                                    data["overrides"][key] = {
                                        "enabled": v.get("enabled", True) is not False,
                                        "label": _label(v.get("label"), key.split(":")[-1]),
                                        "spec": normalize_spec(v),
                                    }
                            ins = raw.get("instances") or []
                            if isinstance(ins, list):
                                seen = set()
                                for v in ins[:MAX_ENTRIES]:
                                    if not isinstance(v, dict):
                                        continue
                                    key = normalize_key(v.get("key"))
                                    base = normalize_key(v.get("from"))
                                    # 实例必须带 # 后缀；源任务必须是原生 key
                                    if (not key or "#" not in key or not base or
                                            "#" in base or key in seen):
                                        continue
                                    seen.add(key)
                                    data["instances"].append({
                                        "key": key, "from": base,
                                        "label": _label(v.get("label"),
                                                        key.split("#")[-1]),
                                        "enabled": v.get("enabled", True) is not False,
                                        "spec": normalize_spec(v),
                                    })
                except Exception as e:
                    err = "任务配置读取失败：%s" % e
            self._sig, self._data, self.error = sig, data, err
            self.loaded_at = time.time()
            if err:
                self._warn("插件任务配置异常，已按插件代码声明继续 | %s", err)
            return self._data

    # ------------------------------------------------------------ 合并

    def view(self, base_tasks):
        """管理端展示用：代码声明 + 覆盖 + 自定义实例，全部可 JSON 序列化。"""
        data = self.read()
        base = {}
        for t in base_tasks or []:
            key = normalize_key(t.get("key"))
            if key and "#" not in key:
                base[key] = t
        items = []
        for key in sorted(base):
            t = base[key]
            ov = data["overrides"].get(key)
            spec = (ov or {}).get("spec") or t.get("spec") or {}
            items.append({
                "key": key, "plugin": t.get("plugin", ""),
                "task": t.get("task", ""),
                "label": (ov or {}).get("label") or t.get("task", key),
                "enabled": bool(ov.get("enabled", True)) if ov else True,
                "plugin_disabled": bool(t.get("disabled")),
                "custom": False, "base_key": "",
                "source": "override" if ov else "code",
                "spec": public_spec(spec),
                "declared": public_spec(t.get("spec") or {}),
            })
        for ins in data["instances"]:
            src = base.get(ins["from"])
            if src is None:
                # 源任务已被删 / 插件被移除：既不展示也不执行，避免幽灵任务
                continue
            items.append({
                "key": ins["key"], "plugin": src.get("plugin", ""),
                "task": src.get("task", ""), "label": ins["label"],
                "enabled": bool(ins["enabled"]),
                "plugin_disabled": bool(src.get("disabled")),
                "custom": True, "base_key": ins["from"], "source": "instance",
                "spec": public_spec(ins["spec"] or src.get("spec") or {}),
                "declared": public_spec(src.get("spec") or {}),
            })
        return items

    def apply(self, base_tasks):
        """调度线程消费：含 handler 对象，**不可** jsonify。"""
        base = {}
        for t in base_tasks or []:
            key = normalize_key(t.get("key"))
            if key and "#" not in key:
                base[key] = t
        out = []
        for it in self.view(base_tasks):
            if not it["enabled"] or it["plugin_disabled"]:
                continue
            src = base.get(it["base_key"] or it["key"])
            handler = src.get("handler") if src else None
            if handler is None:      # _Handler 是数据对象，不是 callable
                continue
            out.append({
                "key": it["key"], "plugin": src["plugin"], "task": src["task"],
                "spec": dict(it["spec"]), "handler": src["handler"],
                "disabled": False, "label": it["label"],
                "custom": it["custom"],
            })
        return out

    def describe(self, base_tasks):
        d = self.read()
        return {
            "version": 1, "enabled": True, "path": self.path,
            "loaded_at": self.loaded_at, "error": self.error,
            "override_count": len(d["overrides"]),
            "instance_count": len(d["instances"]),
            "items": self.view(base_tasks),
        }
