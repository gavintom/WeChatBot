"""验证 1.1.2-beta5：每条群聊 / 私聊配置各自指定大模型（规则级）。

与 beta2 的「分场景」区别：
  · beta2 是**群聊整体一个、私聊整体一个**（场景级）
  · beta5 是**每条配置各选一个**（规则级），不选才走场景 → 全局 → config.yaml

覆盖：
  · group_rules / private_rules 的 llm_id 列与读写（含 0/空 = 不指定）
  · 非法模型 id 报错且库内不变；模型停用后列表标「已停用」、生成时回退
  · policy 决策结果把 llm_id 带进 match（webhook 靠它选模型）
  · brain.answer(model=<规则模型 id>) 真的把该模型的 model 发给上游
  · /api/options 给配置表单提供可选模型（不含密钥）
  · 页面：两个列表都有「大模型」列，两条整体场景卡片已移除

全程临时库，不碰生产 data/analytics.db，也不真连大模型。
"""
import os
import re
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

_TMP = tempfile.mkdtemp(prefix="wxbot_rule_llm_")
os.environ["WXBOT_DB"] = os.path.join(_TMP, "analytics.db")
os.environ.setdefault("WXBOT_ADMIN_LOG", os.path.join(_TMP, "admin.log"))

import brain  # noqa: E402
import policy  # noqa: E402
import store  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, extra))


def noop(*_a, **_k):
    pass


store.init_db()


def mk(name, model, enabled=True):
    mid = store.save_llm_model({
        "name": name, "provider": "", "base_url": "https://llm.example.com/v1",
        "api_key": "sk-x", "model": model, "temperature": 0.3, "stream": True,
        "enabled": enabled,
    })
    return mid


print("\n=== 1. 表结构补上 llm_id（老库幂等迁移）===")
cols_g = {r["name"] for r in store._rows("PRAGMA table_info(group_rules)")}
cols_p = {r["name"] for r in store._rows("PRAGMA table_info(private_rules)")}
check("group_rules 有 llm_id", "llm_id" in cols_g, cols_g)
check("private_rules 有 llm_id", "llm_id" in cols_p, cols_p)
store.init_db()   # 再跑一次不能报错（迁移是幂等的）
check("init_db 可重复执行", True)

print("\n=== 2. 群聊配置：选模型 / 不选 / 改选 ===")
m1 = mk("模型甲", "model-a")
m2 = mk("模型乙", "model-b")
dft = mk("全局默认", "model-default")
store.set_default_llm(dft)

gid = store.save_group_rule({"group_id": "111@chatroom", "group_name": "甲群",
                             "wake_words": "", "need_kb": False, "llm_id": m1})
row = [r for r in store.list_group_rules() if r["id"] == gid][0]
check("群配置落库选了模型甲", row["llm_id"] == m1, row.get("llm_id"))
check("列表带模型名称与上游 model",
      row["llm_name"] == "模型甲 · model-a", row.get("llm_name"))
check("列表标出模型可用", row["llm_available"] is True, row.get("llm_available"))

store.save_group_rule({"id": gid, "group_id": "111@chatroom", "group_name": "甲群",
                       "need_kb": False, "llm_id": m2})
row = [r for r in store.list_group_rules() if r["id"] == gid][0]
check("改选模型乙生效", row["llm_id"] == m2, row.get("llm_id"))

store.save_group_rule({"id": gid, "group_id": "111@chatroom", "group_name": "甲群",
                       "need_kb": False, "llm_id": 0})
row = [r for r in store.list_group_rules() if r["id"] == gid][0]
check("选 0 = 不指定（跟随默认）", row["llm_id"] == 0, row.get("llm_id"))
check("不指定时模型名为空（页面显示跟随默认）", row["llm_name"] == "", row.get("llm_name"))

store.save_group_rule({"id": gid, "group_id": "111@chatroom", "group_name": "甲群",
                       "need_kb": False, "llm_id": ""})
row = [r for r in store.list_group_rules() if r["id"] == gid][0]
check("空串也按不指定处理", row["llm_id"] == 0, row.get("llm_id"))

print("\n=== 3. 私聊配置：选模型 ===")
pid = store.save_private_rule({"name": "VIP 客户", "wake_words": "价格",
                               "need_kb": False, "priority": 1, "llm_id": m1})
prow = [r for r in store.list_private_rules() if r["id"] == pid][0]
check("私聊配置落库选了模型甲", prow["llm_id"] == m1, prow.get("llm_id"))
check("私聊列表带模型名称", prow["llm_name"] == "模型甲 · model-a", prow.get("llm_name"))
store.save_private_rule({"id": pid, "name": "VIP 客户", "need_kb": False,
                         "priority": 1, "llm_id": m2})
prow = [r for r in store.list_private_rules() if r["id"] == pid][0]
check("私聊改选模型乙生效", prow["llm_id"] == m2, prow.get("llm_id"))

print("\n=== 4. 非法输入不落库 ===")
before = [r for r in store.list_group_rules() if r["id"] == gid][0]["llm_id"]
try:
    store.save_group_rule({"id": gid, "group_id": "111@chatroom", "group_name": "甲群",
                           "need_kb": False, "llm_id": 999999})
    check("不存在的模型应当报错", False, "没有抛 ValueError")
except ValueError as e:
    check("不存在的模型报错", "不存在" in str(e), str(e))
after = [r for r in store.list_group_rules() if r["id"] == gid][0]["llm_id"]
check("报错后库里原值不变", before == after, (before, after))
try:
    store.save_group_rule({"id": gid, "group_id": "111@chatroom", "need_kb": False,
                           "llm_id": "abc"})
    check("非数字 llm_id 应当报错", False)
except ValueError:
    check("非数字 llm_id 报错", True)

print("\n=== 5. 模型停用 / 删除后的表现 ===")
store.save_group_rule({"id": gid, "group_id": "111@chatroom", "group_name": "甲群",
                       "need_kb": False, "llm_id": m2})
store.save_llm_model({"id": m2, "name": "模型乙", "base_url": "https://llm.example.com/v1",
                      "model": "model-b", "enabled": False})
row = [r for r in store.list_group_rules() if r["id"] == gid][0]
check("指定模型停用后仍保留 id（便于提示）", row["llm_id"] == m2, row.get("llm_id"))
check("列表标出不可用", row["llm_available"] is False, row.get("llm_available"))
cfg = store.resolve_llm_config(m2)
check("停用模型解析不到（回退由 brain 兜底）", cfg is None, cfg)
store.save_llm_model({"id": m2, "name": "模型乙", "base_url": "https://llm.example.com/v1",
                      "model": "model-b", "enabled": True})

print("\n=== 6. policy 决策把 llm_id 带进 match ===")
rule = {"policy_id": None, "policy_name": "", "docs": [], "llm_id": m1,
        "llm_name": "模型甲 · model-a", "system_prompt": ""}
ok = policy._ok({"chat_type": "group"}, rule, "at", "", "你好")
check("match.llm_id = 规则选的模型", ok["llm_id"] == m1, ok.get("llm_id"))
check("match.llm_name 带过去（日志用）", ok["llm_name"] == "模型甲 · model-a", ok.get("llm_name"))
ok0 = policy._ok({"chat_type": "group"}, {"llm_id": 0}, "at", "", "你好")
check("规则没选时 match.llm_id 为 None", ok0["llm_id"] is None, ok0.get("llm_id"))
# 选了但模型已停用 / 已删除：视同没选，回退默认模型（而不是降级到 config.yaml）
okx = policy._ok({"chat_type": "group"},
                 {"llm_id": 7, "llm_name": "已停用 · x", "llm_available": False},
                 "at", "", "你好")
check("模型不可用时 match.llm_id 置空（回退默认）", okx["llm_id"] is None, okx.get("llm_id"))
oky = policy._ok({"chat_type": "group"}, {"llm_id": 7, "llm_name": "x"}, "at", "", "你好")
check("老数据没有 llm_available 时按可用处理", oky["llm_id"] == 7, oky.get("llm_id"))

print("\n=== 7. brain 按规则模型真的发给上游 ===")
seen = {}


class FakeResp:
    status_code = 200
    text = ""

    def iter_lines(self, decode_unicode=False):
        for ln in ('data: {"choices":[{"delta":{"content":"好"}}]}',
                   'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1,'
                   '"total_tokens":4}}',
                   "data: [DONE]"):
            yield ln.encode("utf-8")

    def close(self):
        pass

    def raise_for_status(self):
        pass


def fake_post(url, headers=None, json=None, stream=False, timeout=None):
    seen["payload"] = json
    return FakeResp()


brain.requests.post = fake_post
cfg = {
    "external_llm": {"base_url": "https://fallback.example.com/v1", "api_key": "sk-fb",
                     "model": "fallback-model", "stream": True},
    "retrieval": {"knowledge_dir": os.path.join(_TMP, "knowledge"),
                  "use_vector": False, "warmup": False, "top_k": 1},
    "system_prompt": "测试",
}
b = brain.HybridRAGBrain(cfg, log=noop)
b.answer("甲群问题", sources=[], model=m1, scene="group")
check("规则选了模型甲 → 请求用 model-a", seen["payload"]["model"] == "model-a", seen["payload"])
b.answer("没选模型的问题", sources=[], scene="group")
check("规则没选 → 回退场景/全局默认", seen["payload"]["model"] == "model-default",
      seen["payload"])
b.answer("另一个群", sources=[], model=m2)
check("另一个群选模型乙 → 请求用 model-b", seen["payload"]["model"] == "model-b",
      seen["payload"])
check("answer 结束后状态复位", b.model_override is None, b.model_override)
# 规则指向的模型被停用：不能抛异常，要回退到默认继续答
store.save_llm_model({"id": m1, "name": "模型甲", "base_url": "https://llm.example.com/v1",
                      "model": "model-a", "enabled": False})
b.answer("模型被停用", sources=[], model=m1)
check("指定模型停用后回退 config.yaml 继续答",
      seen["payload"]["model"] == "fallback-model", seen["payload"])
store.save_llm_model({"id": m1, "name": "模型甲", "base_url": "https://llm.example.com/v1",
                      "model": "model-a", "enabled": True})

print("\n=== 8. 管理端接口与页面 ===")
import admin.app as admin_app  # noqa: E402

cl = admin_app.app.test_client()
cl.get("/login")
with cl.session_transaction() as s:
    s["uid"] = "admin"
H = {"X-Requested-With": "XMLHttpRequest"}

d = cl.get("/api/options").get_json()
llms = d.get("llms") or []
check("/api/options 提供可选模型", len(llms) >= 3, llms)
check("模型选项字段精简（无密钥）",
      set(llms[0]) == {"id", "name", "model"}, llms[0])
check("选项只含启用模型",
      all(m["id"] in (m1, m2, dft) for m in llms), [m["id"] for m in llms])

resp = cl.post("/api/group-rules", json={"group_id": "222@chatroom", "group_name": "乙群",
                                         "need_kb": False, "llm_id": m1}, headers=H)
check("接口保存群配置带 llm_id", resp.status_code == 200, resp.status_code)
new_id = resp.get_json()["id"]
check("接口返回的列表带 llm_name",
      any(r["llm_name"] == "模型甲 · model-a"
          for r in resp.get_json()["items"] if r["id"] == new_id))
resp = cl.post("/api/private-rules", json={"name": "接口私聊", "need_kb": False,
                                           "llm_id": m2}, headers=H)
check("接口保存私聊配置带 llm_id", resp.status_code == 200, resp.status_code)
resp = cl.post("/api/group-rules", json={"group_id": "222@chatroom", "need_kb": False,
                                         "llm_id": 999999}, headers=H)
check("接口拒绝不存在的模型（400）", resp.status_code == 400, resp.get_json())

html = cl.get("/").get_data(as_text=True)
check("群聊表头有「大模型」列",
      '<th data-sort="llm_name">大模型</th>' in html)
check("群聊配置说明提到单独指定模型", "单独指定一个大模型" in html)
check("私聊表头有「大模型」列", html.count('<th data-sort="llm_name">大模型</th>') == 2)
check("两条整体「应答模型」卡片已移除",
      "grpSceneModel" not in html and "prvSceneModel" not in html)
check("AI 大模型页保留分场景兜底卡片", "llmSceneGroup" in html)

js = open("admin/static/app.js", encoding="utf-8").read()
check("表单里有本群使用的大模型下拉", "本群使用的大模型" in js)
check("表单里有本配置使用的大模型下拉", "本配置使用的大模型" in js)
check("列表渲染用 ruleLlmCell", js.count("'<td>' + ruleLlmCell(r)") == 2,
      js.count("'<td>' + ruleLlmCell(r)"))
check("已删除整体场景卡片的绑定", "btnGrpModelSave" not in js and "btnPrvModelSave" not in js)
check("规则模型下拉首项是「不指定」", "不指定（跟随默认模型）" in js)

ids = re.findall(r'\bid="([^"]+)"', html)
dup = {k: ids.count(k) for k in set(ids) if ids.count(k) > 1}
check("模板内无重复 id", not dup, dup)

print("\n%d 项通过，%d 项失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
