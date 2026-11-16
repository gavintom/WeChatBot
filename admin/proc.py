"""主服务（webhook_server）进程管理：管理端启动 / 停止 / 重启 / 状态。

设计取舍：
1. **以 /healthz 探活为准，PID 文件只用于停止**。PID 会复用、会残留，
   单靠它判断「在不在跑」不可靠；探活顺带还能发现「由外部终端启动」的实例。
2. **只停止自己启动的进程**。若服务是用户用 run_webhook.bat / systemd 起的，
   管理端不越权去杀（返回明确提示），避免误杀同机上别人的进程。
3. **子进程脱离后台进程**：Windows 用 CREATE_NEW_PROCESS_GROUP + CREATE_NO_WINDOW，
   POSIX 用 start_new_session，这样后台重启/退出不会把主服务一起带走。
4. 启动命令可配 `admin.start_command`（字符串或数组），默认
   `<当前 Python> webhook_server.py`；生产可换成 gunicorn 命令。
"""
import json
import os
import shlex
import subprocess
import sys
import time

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE = os.environ.get("WXBOT_PID") or os.path.join(BASE_DIR, "data", ".webhook.pid")

from admin import cfg as _cfg  # noqa: E402  （BASE_DIR 就绪后再 import）
import logfiles                 # noqa: E402


def out_log_path():
    """主服务启动输出：按天一个文件，每次调用重新解析（跨天写新文件）。"""
    return logfiles.resolve("WXBOT_SERVICE_LOG", "webhook_server.out")


# 探活的是本机主服务，不能被环境里的 HTTP_PROXY 劫持
_LOCAL = requests.Session()
_LOCAL.trust_env = False


def _main_url():
    c = _cfg.read_config()
    return str((c.get("admin") or {}).get("main_service")
               or "http://127.0.0.1:29999").rstrip("/")


def _probe():
    try:
        r = _LOCAL.get(_main_url() + "/healthz", timeout=1.0)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _read_pid():
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return int(d.get("pid") or 0) or None
    except Exception:
        return None


def _write_pid(pid, cmd):
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "cmd": cmd, "started_at": int(time.time())},
                      f, ensure_ascii=False)
    except OSError:
        pass


def _clear_pid():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def _pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        try:
            r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, timeout=6)
            return ('"%d"' % pid) in (r.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)          # POSIX：0 号信号只探测、不发信号
        return True
    except OSError:
        return False


def _command():
    c = _cfg.read_config()
    v = (c.get("admin") or {}).get("start_command")
    if v:
        if isinstance(v, (list, tuple)):
            return [str(x) for x in v]
        parts = shlex.split(str(v), posix=(os.name != "nt"))
        return [p.strip('"').strip("'") for p in parts if p.strip('"').strip("'")]
    return [sys.executable, os.path.join(BASE_DIR, "webhook_server.py")]


def _spawn(cmd):
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    # Windows 上无控制台的子进程 stdout 默认用 ANSI(cp936/GBK) 编码，
    # 直接重定向到 UTF-8 日志文件就会全变「锟斤拷」。强制子进程用 UTF-8 输出。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        # 每次启动主服务顺手把超过 5 个的历史启动日志清掉
        logfiles.prune("webhook_server.out")
        logf = open(out_log_path(), "a", encoding="utf-8", buffering=1)
        logf.write("\n===== %s 管理端启动 %s =====\n" %
                   (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(cmd)))
        logf.flush()
    except OSError:
        logf = subprocess.DEVNULL
    kw = dict(cwd=BASE_DIR, env=env, stdin=subprocess.DEVNULL,
              stdout=logf, stderr=subprocess.STDOUT)
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kw["creationflags"] = flags
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(cmd, **kw)
    if hasattr(logf, "close"):
        logf.close()             # 父进程可以关掉，子进程自己持有句柄
    return p.pid


def _wait(up, timeout):
    """轮询等待条件成立，返回是否成立。"""
    end = time.time() + timeout
    while time.time() < end:
        if up():
            return True
        time.sleep(0.4)
    return up()


def decode_bytes(data):
    """逐行容错解码：修复前的日志可能混有 GBK 行，修复后全是 UTF-8。

    整块解码会让「UTF-8 头 + GBK 尾」的混合文件一处失败就全变乱码，
    按行分别尝试 utf-8 → gb18030 才能把历史日志也正常显示出来。
    """
    if isinstance(data, str):
        return data
    out = []
    for line in data.splitlines(keepends=True):
        for enc in ("utf-8", "gb18030"):
            try:
                out.append(line.decode(enc))
                break
            except UnicodeDecodeError:
                continue
        else:
            out.append(line.decode("utf-8", errors="replace"))
    return "".join(out)


def _tail(n=2200):
    try:
        p = out_log_path()
        with open(p, "rb") as f:
            f.seek(max(0, os.path.getsize(p) - n))
            return decode_bytes(f.read())
    except OSError:
        return ""


def status():
    """主服务状态（供运行监控页轮询）。"""
    running = _probe()
    pid = _read_pid()
    alive = _pid_alive(pid) if pid else False
    if pid and not alive:
        _clear_pid()             # 清理残留 PID，避免下次 stop 打错目标
        pid = None
    if running and pid is None:
        source = "外部启动"
    elif pid:
        source = "管理端启动"
    else:
        source = "未运行"
    return {
        "ok": True,
        "running": running,
        "starting": bool(pid and not running),
        "managed": bool(pid),
        "pid": pid,
        "pid_alive": alive,
        "source": source,
        "url": _main_url(),
        "command": " ".join(_command()),
        "log_tail": _tail(),
    }


def start():
    if _probe():
        return {"ok": True, "message": "主服务已在运行，无需重复启动", **status()}
    pid = _read_pid()
    if pid and _pid_alive(pid):
        return {"ok": True, "message": "主服务正在启动中，请稍候…", "starting": True,
                **status()}
    cmd = _command()
    try:
        pid = _spawn(cmd)
    except Exception as e:
        return {"ok": False, "error": "启动失败：%s" % e}
    _write_pid(pid, cmd)
    # 不等待就绪：管理端点「启动」后前端立即显示「启动中」并轮询，
    # 这里只做一次状态汇总（最多一次 1 秒探活），接口本身不阻塞。
    st = status()
    msg = ("主服务已启动（PID %d）" % pid) if st["running"] else \
          "已发起启动（PID %d），正在初始化索引…" % pid
    return {"ok": True, "message": msg, "starting": not st["running"], **st}


def stop():
    pid = _read_pid()
    if not pid:
        if _probe():
            return {"ok": False,
                    "error": "主服务由外部进程启动（没有管理端 PID 记录），请在启动它的终端或 systemd 中停止"}
        return {"ok": True, "message": "主服务本来就没在运行", **status()}
    if not _pid_alive(pid):
        _clear_pid()
        return {"ok": True, "message": "进程已退出，已清理 PID 记录", **status()}
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            import signal
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:
                os.kill(pid, signal.SIGTERM)
    except Exception as e:
        return {"ok": False, "error": "停止失败：%s" % e}
    stopped = _wait(lambda: not _probe(), 10)
    _clear_pid()
    return {"ok": True, "message": "主服务已停止" if stopped else
            "已发送停止信号，端口可能还在释放中", **status()}


def restart():
    r = stop()
    if not r.get("ok"):
        if _probe():
            return r
        # 没在跑但 stop 报了别的错：仍尝试启动
    time.sleep(0.6)
    return start()
