"""SQLite 连接工厂：主库 / 聊天记录库 / 媒体库 / 手机活跃库共用一套 PRAGMA。

为什么统一：这几个库都是同一台机器上的进程在读写，WAL、synchronous、
busy_timeout 必须口径一致。以前四处各写一遍，改一处漏三处就会出现
「一方在 WAL 里等锁、另一方还是 rollback journal 直接互斥」的偶发
`database is locked`；调参数时也得翻四个文件。
"""
import os
import sqlite3


def connect(path, timeout=15, row_factory=True, wal=True):
    """打开一个连接并设好 PRAGMA。

    - 父目录不存在时自动创建（首次部署 / 新建数据卷的常见情况）
    - row_factory 默认 sqlite3.Row，调用方可以按列名取值
    - timeout 同时用于 connect 与 busy_timeout，单位秒，避免两处不一致
    - wal=False 留给只读或不支持 WAL 的场景（例如某些网络文件系统）
    """
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout)
    if row_factory:
        conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
    if wal:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            # 个别文件系统不支持 WAL：退回默认日志模式也要能用，不能因此启动失败
            pass
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
