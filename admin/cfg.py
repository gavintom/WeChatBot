"""读 config.yaml 的**唯一**入口。

为什么要有这个模块：项目里到处是 `open(BASE_DIR + "/config.yaml")`，
路径散落在 4~5 个文件里。带来的问题：
- 测试要隔离时得逐个打补丁，漏一个就污染真实配置（2026-10-03 踩过）
- 改配置路径（比如以后支持多套配置）要改一堆地方

所以统一成这里，其他模块一律通过 `CONFIG_PATH` / `read_config()` 拿。
注意都是**运行时**读模块变量，不在 import 时绑死 —— 否则测试改了也不生效。
"""
import os

import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# WXBOT_CONFIG 可覆盖配置文件（多环境 / 自动化测试用）
CONFIG_PATH = os.environ.get("WXBOT_CONFIG") or os.path.join(BASE_DIR, "data", "config.yaml")


def read_config():
    """读并解析 config.yaml。读不到/格式错时返回 {}（调用方自己决定要不要报错）。"""
    try:
        return yaml.safe_load(open(CONFIG_PATH, encoding="utf-8")) or {}
    except Exception:
        return {}


