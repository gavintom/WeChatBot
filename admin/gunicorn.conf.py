"""管理后台 gunicorn 配置（生产）。

启动：gunicorn -c admin/gunicorn.conf.py admin.app:app

后台只读 SQLite + 静态文件，无模块级业务线程 → 可以多 worker。
登录限速是进程内内存态，多 worker 会把阈值放大（要严格就设 KF_ADMIN_WORKERS=1）。
"""
import os
import sys

try:
    here = os.path.dirname(os.path.abspath(__file__))
except NameError:
    here = None


def _resolve_base_dir():
    cands = []
    if os.environ.get("KF_BASE_DIR"):
        cands.append(os.environ["KF_BASE_DIR"])
    if here:
        cands.append(os.path.dirname(here))
        cands.append(here)
    cands.append(os.getcwd())
    for marker in (os.path.join("data", "config.yaml"), "config.yaml.example"):
        for d in cands:
            if os.path.isfile(os.path.join(d, marker)):
                return os.path.abspath(d)
    return os.path.abspath(cands[0])


BASE_DIR = _resolve_base_dir()
chdir = BASE_DIR
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    import yaml
    _p = os.environ.get("WXBOT_CONFIG") or os.path.join(BASE_DIR, "data", "config.yaml")
    if not os.path.isfile(_p):
        _p = os.path.join(BASE_DIR, "config.yaml.example")
    _cfg = yaml.safe_load(open(_p, encoding="utf-8")) or {}
    _admin = _cfg.get("admin") or {}
except Exception:
    _admin = {}

bind = "%s:%s" % (os.environ.get("KF_ADMIN_HOST") or _admin.get("host") or "0.0.0.0",
                  os.environ.get("KF_ADMIN_PORT") or _admin.get("port") or 7780)

workers = int(os.environ.get("KF_ADMIN_WORKERS", 2))
worker_class = "gthread"
threads = int(os.environ.get("KF_ADMIN_THREADS", 4))
preload_app = False

timeout = 120
graceful_timeout = 30
keepalive = 5

capture_output = True
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("KF_ADMIN_LOGLEVEL", "info")
access_log_format = '%(h)s %(m)s %(U)s %(q)s -> %(s)s %(b)sB %(D)sus'

proc_name = "wxbot-admin"
pidfile = os.path.join(BASE_DIR, ".admin.pid")
