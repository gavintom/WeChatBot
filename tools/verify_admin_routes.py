"""管理后台路由冒烟测试：拆分 Blueprint 后防止「路由丢了 / 500 了」。

不依赖主服务、手机插件与真实数据：只验证
  1) 路由总数与关键路径存在（防止拆分时漏注册蓝图）；
  2) 登录态下访问所有无参数 GET 接口，不返回 5xx。

用法：python tools/verify_admin_routes.py
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import admin.app as admin_app

# 拆分前 app.py 的 89 条路由是基线；调接口时同步改这个数字
EXPECTED_ROUTES = 89

# 必须存在的关键路径（按业务域各取一条，蓝图漏注册会立刻暴露）
MUST_HAVE = [
    "/", "/login", "/api/status", "/api/service", "/api/log", "/api/sysinfo",
    "/api/plugins", "/api/plugins/tasks", "/api/plugin/license",
    "/api/contacts/sync", "/api/contacts/friends", "/api/contacts/search",
    "/api/media/recent", "/api/policies", "/api/kb/files", "/api/kb/ask",
    "/api/records", "/api/chat-messages", "/api/stats", "/api/apicalls",
    "/api/llm-usage", "/api/llm-models", "/api/moments/feed",
    "/api/config", "/api/followup", "/api/health", "/api/options",
]


def main():
    rules = list(admin_app.app.url_map.iter_rules())
    fails = []
    if len(rules) != EXPECTED_ROUTES:
        fails.append("路由总数 %d，期望 %d" % (len(rules), EXPECTED_ROUTES))
    paths = {r.rule for r in rules}
    for p in MUST_HAVE:
        if p not in paths:
            fails.append("缺少路由 %s" % p)

    client = admin_app.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = "admin"

    # 只测无路径参数的 GET；带 <int:pid> 之类需要真实 id，冒烟没有意义
    checked = 0
    for r in rules:
        if "GET" not in r.methods or "<" in r.rule:
            continue
        try:
            resp = client.get(r.rule)
        except Exception as e:
            fails.append("GET %s 抛异常 %r" % (r.rule, e))
            continue
        checked += 1
        if resp.status_code >= 500:
            fails.append("GET %s -> %d" % (r.rule, resp.status_code))

    print("路由 %d 条，GET 冒烟 %d 条" % (len(rules), checked))
    if fails:
        for f in fails:
            print("  FAIL", f)
        print("结果：%d 项失败" % len(fails))
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
