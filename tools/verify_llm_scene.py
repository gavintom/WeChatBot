"""验证 1.1.2-beta2：群聊 / 私聊各自选择大模型。

覆盖两个层面：
  · store.resolve_llm_config 的选择优先级（显式指定 > 场景默认 > 全局默认 > config.yaml）
  · 模型停用 / 删除后场景不会「挂着失效配置」，能明确回退
  · brain.answer 真的按场景把模型发给了上游（拦 requests.post 看 payload.model）
  · llm_usage 落库带 scene，能核对「配置选的是不是真的用上了」
  · 管理端 llm_models_status 的 scenes 摘要（id / 生效模型 / 是否继承）

全程用临时库，不碰生产 data/analytics.db，也不真连大模型。
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

_TMP = tempfile.mkdtemp(prefix="wxbot_llm_scene_")
os.environ["WXBOT_DB"] = os.path.join(_TMP, "analytics.db")
os.environ.setdefault("WXBOT_ADMIN_LOG", os.path.join(_TMP, "admin.log"))

import brain  # noqa: E402
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


def mk(name, model, key="sk-x"):
    return store.save_llm_model({
        "name": name, "provider": "", "base_url": "https://llm.example.com/v1",
        "api_key": key, "model": model, "temperature": 0.3, "stream": True,
    })


print("\n=== 1. 场景模型落库与读取 ===")
g = mk("群聊专用", "group-model")
p = mk("私聊专用", "private-model")
d = mk("全局默认", "default-model")
store.set_default_llm(d)
check("群聊场景初始未选（跟随全局）", store.scene_llm_id("group") == 0)
check("非法场景名被收敛成空", store.norm_llm_scene("GROUP ") == "group"
      and store.norm_llm_scene("whatever") == "")

saved = store.set_scene_llms({"group": g, "private": str(p)})
check("一次保存两个场景", saved == {"group": g, "private": p}, saved)
check("群聊场景 id 读回正确", store.scene_llm_id("group") == g)
check("私聊场景 id 读回正确（字符串也认）", store.scene_llm_id("private") == p)

print("\n=== 2. 选择优先级 ===")
cg = store.resolve_llm_config(scene="group")
cp = store.resolve_llm_config(scene="private")
cd = store.resolve_llm_config()
check("群聊走群聊模型", cg["model"] == "group-model" and cg["source"] == "scene", cg)
check("私聊走私聊模型", cp["model"] == "private-model" and cp["source"] == "scene", cp)
check("不传场景走全局默认", cd["model"] == "default-model" and cd["source"] == "default", cd)
co = store.resolve_llm_config("私聊专用", scene="group")
check("显式指定模型压过场景模型",
      co["model"] == "private-model" and co["source"] == "override", co)
cn = store.resolve_llm_config(scene="whatever")
check("不认识的场景退化为全局默认", cn["model"] == "default-model", cn)
check("解析结果带上场景标记", cg.get("scene") == "group")

print("\n=== 3. 场景配置失效时的回退 ===")
store.set_scene_llm("group", 0)
check("清空场景 = 跟随全局默认", store.scene_llm_id("group") == 0)
cg2 = store.resolve_llm_config(scene="group")
check("清空后群聊用全局默认", cg2["model"] == "default-model"
      and cg2["source"] == "default", cg2)

# 把私聊选的模型停用：不能报错，要明确回退（停一个模型不该让私聊哑掉）
store.set_scene_llm("group", g)
store.save_llm_model({"id": p, "name": "私聊专用", "base_url": "https://llm.example.com/v1",
                      "model": "private-model", "enabled": False})
cp2 = store.resolve_llm_config(scene="private")
check("场景模型停用后退回全局默认",
      cp2["model"] == "default-model" and cp2["source"] == "default", cp2)
st = store.llm_models_status({})
check("状态摘要标出「继承全局」", st["scenes"]["private"]["inherited"] is True, st["scenes"])
check("状态摘要保留原选 id（便于页面提示已停用）",
      st["scenes"]["private"]["id"] == p, st["scenes"])
store.save_llm_model({"id": p, "name": "私聊专用", "base_url": "https://llm.example.com/v1",
                      "model": "private-model", "enabled": True})

print("\n=== 4. 删除模型时清理场景配置 ===")
tmp = mk("临时模型", "tmp-model")
store.set_scene_llm("private", tmp)
check("删除前私聊指向临时模型", store.scene_llm_id("private") == tmp)
store.delete_llm_model(tmp)
check("删除后私聊配置被清理", store.scene_llm_id("private") == 0)
check("删除后私聊回退全局默认",
      store.resolve_llm_config(scene="private")["model"] == "default-model")

print("\n=== 5. 非法输入不落半截配置 ===")
before = {"group": store.scene_llm_id("group"), "private": store.scene_llm_id("private")}
try:
    store.set_scene_llms({"group": 0, "private": 999999})
    check("不存在的模型应当报错", False, "没有抛 ValueError")
except ValueError:
    check("不存在的模型报错", True)
after = {"group": store.scene_llm_id("group"), "private": store.scene_llm_id("private")}
check("报错时另一场景没被写入（先校验后落库）", before == after, (before, after))
try:
    store.set_scene_llm("wechat", g)
    check("非法场景应当报错", False)
except ValueError:
    check("非法场景报错", True)

print("\n=== 6. brain.answer 按场景选模型 ===")
# 场景模型恢复成独立配置再测链路
store.set_scene_llms({"group": g, "private": p})
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
    seen["url"] = url
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
b.answer("群聊问题", sources=[], scene="group")
check("群聊请求打到群聊模型的 model", seen["payload"]["model"] == "group-model", seen["payload"])
b.answer("私聊问题", sources=[], scene="private")
check("私聊请求打到私聊模型的 model", seen["payload"]["model"] == "private-model", seen["payload"])
b.answer("接口问题", sources=[])
check("不传场景走全局默认", seen["payload"]["model"] == "default-model", seen["payload"])
check("answer 结束后场景状态已复位", b.scene is None and b.model_override is None,
      (b.scene, b.model_override))
b.answer("强制指定", sources=[], model="群聊专用", scene="private")
check("显式指定压过场景", seen["payload"]["model"] == "group-model", seen["payload"])

# 场景模型停用后，链路必须还能答（回退全局默认），不能整条哑掉
store.save_llm_model({"id": g, "name": "群聊专用", "base_url": "https://llm.example.com/v1",
                      "model": "group-model", "enabled": False})
b.answer("群聊问题（模型停用）", sources=[], scene="group")
check("场景模型停用后仍能答，用全局默认",
      seen["payload"]["model"] == "default-model", seen["payload"])
store.save_llm_model({"id": g, "name": "群聊专用", "base_url": "https://llm.example.com/v1",
                      "model": "group-model", "enabled": True})

print("\n=== 7. 用量流水记录模型与场景 ===")
store.log_llm_usage([{"model": "group-model", "prompt_tokens": 10, "completion_tokens": 2,
                      "total_tokens": 12, "stream": True, "ok": True, "latency_ms": 88}],
                    source="wechat", llm_id=g, scene="group")
store.log_llm_usage([{"model": "default-model", "prompt_tokens": 5, "completion_tokens": 1,
                      "total_tokens": 6, "ok": True}],
                    source="api", llm_id=d)
rows = store.llm_records(limit=10)["items"]
by_src = {r["source"]: r for r in rows}
grp = by_src.get("wechat") or {}
api = by_src.get("api") or {}
check("微信流水记下场景 group", grp.get("scene") == "group", grp)
check("微信流水记下模型与纳管 id",
      grp.get("model") == "group-model" and grp.get("llm_id") == g, grp)
check("非微信来源不写场景", (api.get("scene") or "") == "", api)
check("非法场景名不会写进库", (store.norm_llm_scene("x") or None) is None)

print("\n=== 8. 管理端摘要 ===")
st = store.llm_models_status({"model": "cfg-model", "base_url": "https://cfg"})
check("摘要含两个场景", set(st["scenes"]) == {"group", "private"}, st["scenes"])
check("场景摘要带中文名", st["scenes"]["group"]["label"] == "群聊", st["scenes"])
check("场景摘要带生效模型名",
      st["scenes"]["group"]["effective"]["name"] == "群聊专用", st["scenes"])
check("独立配置时 inherited=False", st["scenes"]["group"]["inherited"] is False, st["scenes"])
check("全局生效仍是全局默认", st["effective"]["name"] == "全局默认", st["effective"])

print("\n%d 项通过，%d 项失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
