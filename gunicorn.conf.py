"""主服务（webhook_server:app）的 gunicorn 生产配置。

启动：gunicorn -c gunicorn.conf.py webhook_server:app

⚠️ workers 必须 = 1：
  BRAIN 索引、会话记忆、brain_lock、线程池都在**单个进程内**。
  多 worker 会各建一份索引（内存翻倍）、各自维护会话记忆并互相覆盖
  .conversations.json，且保护 last_images 的进程内锁不再全局有效。
  想提高并发请加大 threads（webhook 本来就是「立即 200 + 后台线程处理」）。
"""
import os
import sys

import yaml


def _resolve_base_dir():
    cands = []
    if os.environ.get("KF_BASE_DIR"):
        cands.append(os.environ["KF_BASE_DIR"])
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        here = None
    if here:
        cands.append(here)
    cands.append(os.getcwd())
    for marker in (os.path.join("data", "config.yaml"), "config.yaml.example"):
        for d in cands:
            if os.path.isfile(os.path.join(d, marker)):
                return os.path.abspath(d)
    return os.path.abspath(here or os.getcwd())


BASE_DIR = _resolve_base_dir()
chdir = BASE_DIR
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

_path = os.environ.get("WXBOT_CONFIG") or os.path.join(BASE_DIR, "data", "config.yaml")
if not os.path.isfile(_path):
    _path = os.path.join(BASE_DIR, "config.yaml.example")
try:
    _cfg = yaml.safe_load(open(_path, encoding="utf-8")) or {}
except Exception as e:
    sys.stderr.write("[gunicorn.conf.py] 读配置失败：%s（用默认端口）\n" % e)
    _cfg = {}
_srv = _cfg.get("server") or {}

bind = "%s:%s" % (os.environ.get("KF_HOST") or _srv.get("host") or "0.0.0.0",
                  os.environ.get("KF_PORT") or _srv.get("port") or 29999)

workers = 1
worker_class = "gthread"
threads = int(os.environ.get("KF_THREADS") or _srv.get("threads") or 8)
preload_app = False

timeout = 300
graceful_timeout = 30
keepalive = 5

capture_output = True
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("KF_GUNICORN_LOGLEVEL") or "info"
access_log_format = '%(h)s %(m)s %(U)s %(q)s -> %(s)s %(b)sB %(D)sus'

proc_name = "wxbot-webhook"
