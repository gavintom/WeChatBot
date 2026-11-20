"""验证 1.1.2-beta3：大模型配置导出 / 导入。

覆盖：
  · 导出结构（format / version / models / default / scenes）与明文密钥开关
  · 引用关系用**模型名称**表达，跨实例导入能正确映射到新的自增 id
  · merge：同名更新、不删已有；replace：文件里没有的模型被删除
  · 密钥留空（脱敏导出）导入时沿用库里原密钥
  · 校验前置：非法内容报错且库内不变（不留半个配置）
  · 往返幂等：export → import(replace) → export 结果一致
  · 管理端接口：下载头、导入统计、非法输入 400

全程临时库，不碰生产 data/analytics.db。
"""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

_TMP = tempfile.mkdtemp(prefix="wxbot_llm_export_")
os.environ["WXBOT_DB"] = os.path.join(_TMP, "analytics.db")
os.environ.setdefault("WXBOT_ADMIN_LOG", os.path.join(_TMP, "admin.log"))

import io  # noqa: E402
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


def mk(name, model, key="sk-" + "k", base="https://llm.example.com/v1", **kw):
    d = {"name": name, "provider": "深度求索", "base_url": base,
         "api_key": key, "model": model, "temperature": 0.3, "stream": True}
    d.update(kw)
    return store.save_llm_model(d)


def reset():
    """清空模型与相关设置，回到「空实例」状态。"""
    for row in store._rows("SELECT id FROM llm_models"):
        store.delete_llm_model(row["id"])
    for k in [store.LLM_DEFAULT_KEY] + list(store.LLM_SCENE_KEYS.values()):
        store._write("DELETE FROM settings WHERE k=?", (k,))


def names(d):
    return [m["name"] for m in d["models"]]


store.init_db()

print("\n=== 1. 导出结构与引用关系 ===")
a = mk("群聊专用", "group-model", "sk-group")
b = mk("私聊专用", "private-model", "sk-private")
c = mk("全局默认", "default-model", "sk-default")
store.set_default_llm(c)
store.set_scene_llms({"group": a, "private": b})

e1 = store.export_llm_models()
check("format 正确", e1["format"] == store.LLM_EXPORT_FORMAT, e1.get("format"))
check("version 正确", e1["version"] == store.LLM_EXPORT_VERSION)
check("导出全部模型", sorted(names(e1)) == ["全局默认", "私聊专用", "群聊专用"], names(e1))
check("默认模型用名称表达", e1["default"] == "全局默认", e1.get("default"))
check("场景引用用名称表达",
      e1["scenes"] == {"group": "群聊专用", "private": "私聊专用"}, e1.get("scenes"))
check("默认带明文密钥",
      any(m["api_key"] == "sk-group" for m in e1["models"]), e1["models"])
check("导出物可 JSON 序列化",
      isinstance(json.dumps(e1, ensure_ascii=False), str))
check("导出带时间戳", bool(e1.get("exported_at")), e1.get("exported_at"))

e_nosec = store.export_llm_models(with_secrets=False)
check("脱敏导出不带密钥",
      all(m["api_key"] == "" for m in e_nosec["models"]))
check("脱敏导出仍有引用关系", e_nosec["default"] == "全局默认")

print("\n=== 2. merge 导入到空库（模拟换机器） ===")
snapshot = json.loads(json.dumps(e1, ensure_ascii=False))
reset()
check("清空后没有模型", store.list_llm_models() == [])
r = store.import_llm_models(snapshot, mode="merge")
check("新增 3 个", (r["added"], r["updated"], r["removed"]) == (3, 0, []), r)
check("默认模型按名称映射到新 id",
      store._llm_name_of(store.default_llm_id()) == "全局默认")
check("群聊场景映射正确",
      store._llm_name_of(store.scene_llm_id("group")) == "群聊专用")
check("私聊场景映射正确",
      store._llm_name_of(store.scene_llm_id("private")) == "私聊专用")
cfg = store.resolve_llm_config(scene="group")
check("群聊真的解析到导入的模型",
      cfg and cfg["model"] == "group-model" and cfg["source"] == "scene", cfg)
new_ids = {m["name"]: m["id"] for m in store.list_llm_models()}
check("导入后 id 与源实例无关（可能相同也可能不同，靠名称对齐）",
      set(new_ids) == {"全局默认", "私聊专用", "群聊专用"}, new_ids)
check("密钥原样导入",
      store.get_llm_model("群聊专用")["api_key"] == "sk-group")
check("参数原样导入",
      store.get_llm_model("群聊专用")["provider"] == "深度求索")

print("\n=== 3. merge 幂等与「同名更新」 ===")
snap2 = json.loads(json.dumps(snapshot, ensure_ascii=False))
snap2["models"][0]["temperature"] = 0.9
r2 = store.import_llm_models(snap2, mode="merge")
check("再次导入全部算更新", (r2["added"], r2["updated"]) == (0, 3), r2)
check("同名模型被更新", store.get_llm_model("群聊专用")["temperature"] == 0.9)
check("merge 不删文件外的模型", len(store.list_llm_models()) == 3)

mk("本地独有", "local-only", "sk-local")
r3 = store.import_llm_models(snap2, mode="merge")
check("merge 仍保留本地独有模型",
      any(m["name"] == "本地独有" for m in store.list_llm_models()))
check("merge 不计删除", r3["removed"] == [], r3)

print("\n=== 4. replace 全量替换 ===")
r4 = store.import_llm_models(snap2, mode="replace")
check("replace 删除文件外模型", r4["removed"] == ["本地独有"], r4["removed"])
check("replace 后只剩文件里的模型",
      sorted(names(store.export_llm_models())) == ["全局默认", "私聊专用", "群聊专用"])

snap3 = json.loads(json.dumps(snap2, ensure_ascii=False))
snap3.pop("scenes", None)
snap3.pop("default", None)
store.import_llm_models(snap3, mode="replace")
check("replace 且文件没写默认 → 清空默认", store.default_llm_id() == 0)
check("replace 且文件没写场景 → 场景跟随全局",
      store.scene_llm_id("group") == 0 and store.scene_llm_id("private") == 0)

print("\n=== 5. 脱敏文件导入沿用原密钥 ===")
store.import_llm_models(snapshot, mode="replace")
store.import_llm_models(e_nosec, mode="merge")
check("空密钥不清掉已有密钥",
      store.get_llm_model("群聊专用")["api_key"] == "sk-group",
      store.get_llm_model("群聊专用")["api_key"])

print("\n=== 6. 校验前置：非法内容一律不落库 ===")
before = sorted(names(store.export_llm_models()))
bad = [
    ("不是对象", "hello"),
    ("models 不是数组", {"models": {"a": 1}}),
    ("models 为空", {"models": []}),
    ("format 不对", {"format": "other", "models": [{"name": "x"}]}),
    ("缺名称", {"models": [{"base_url": "https://a/v1", "model": "m"}]}),
    ("base_url 非法", {"models": [{"name": "x", "base_url": "ftp://a",
                                   "model": "m"}]}),
    ("缺模型 ID", {"models": [{"name": "x", "base_url": "https://a/v1"}]}),
    ("temperature 越界", {"models": [{"name": "x", "base_url": "https://a/v1",
                                      "model": "m", "temperature": 9}]}),
    ("超时不是整数", {"models": [{"name": "x", "base_url": "https://a/v1",
                                  "model": "m", "read_timeout": "abc"}]}),
    ("文件内重名", {"models": [
        {"name": "dup", "base_url": "https://a/v1", "model": "m1"},
        {"name": "dup", "base_url": "https://a/v1", "model": "m2"}]}),
    ("条目超上限", {"models": [{"name": "n%d" % i, "base_url": "https://a/v1",
                                "model": "m"} for i in range(store.LLM_IMPORT_MAX + 1)]}),
]
for label, payload in bad:
    try:
        store.import_llm_models(payload, mode="merge")
        check("拒绝：%s" % label, False, "没有抛异常")
    except ValueError as ex:
        check("拒绝：%s" % label, True)
        if label == "文件内重名":
            check("重名报错里指出了模型名", "dup" in str(ex), str(ex))
check("全部非法导入后库内不变",
      sorted(names(store.export_llm_models())) == before, names(store.export_llm_models()))

try:
    store.import_llm_models(snapshot, mode="whatever")
    check("拒绝：非法导入方式", False)
except ValueError:
    check("拒绝：非法导入方式", True)

print("\n=== 7. 引用无效时的降级 ===")
snap4 = json.loads(json.dumps(snapshot, ensure_ascii=False))
snap4["default"] = "不存在的模型"
snap4["scenes"] = {"group": "也不存在", "private": "私聊专用"}
r5 = store.import_llm_models(snap4, mode="merge")
check("引用无效不报错，记进 missing",
      len(r5["missing"]) == 2, r5["missing"])
check("无效引用不动原默认", store._llm_name_of(store.default_llm_id()) == "全局默认")
check("有效引用正常生效",
      store._llm_name_of(store.scene_llm_id("private")) == "私聊专用")
check("无效场景保持原值",
      store._llm_name_of(store.scene_llm_id("group")) == "群聊专用")

print("\n=== 8. 往返幂等 ===")
store.import_llm_models(snapshot, mode="replace")
x1 = store.export_llm_models()
store.import_llm_models(x1, mode="replace")
x2 = store.export_llm_models()
x1.pop("exported_at", None)
x2.pop("exported_at", None)
check("export → import(replace) → export 一致", x1 == x2,
      [k for k in x1 if x1.get(k) != x2.get(k)])

print("\n=== 9. 管理端接口 ===")
import admin.app as admin_app  # noqa: E402

cl = admin_app.app.test_client()
with cl.session_transaction() as s:
    s["uid"] = "admin"
H = {"X-Requested-With": "XMLHttpRequest"}

resp = cl.get("/api/llm-models/export")
check("导出接口 200", resp.status_code == 200)
check("导出带下载头",
      "attachment" in (resp.headers.get("Content-Disposition") or ""),
      resp.headers.get("Content-Disposition"))
body = json.loads(resp.get_data(as_text=True))
check("导出接口内容可用", body.get("format") == store.LLM_EXPORT_FORMAT)

resp = cl.get("/api/llm-models/export?secrets=0")
nosec = json.loads(resp.get_data(as_text=True))
check("secrets=0 时不含密钥", all(m["api_key"] == "" for m in nosec["models"]))

reset()
resp = cl.post("/api/llm-models/import", json={"data": snapshot, "mode": "merge"},
               headers=H)
check("导入接口 200", resp.status_code == 200)
d = resp.get_json()
check("导入接口有统计", (d.get("result") or {}).get("added") == 3, d.get("result"))
check("导入接口返回新概览", len(d.get("items") or []) == 3)
check("导入提示语包含新增/更新",
      "新增 3 个" in (d.get("message") or ""), d.get("message"))

resp = cl.post("/api/llm-models/import", json={"text": "not json"}, headers=H)
check("非 JSON 文本 400", resp.status_code == 400, resp.status_code)
resp = cl.post("/api/llm-models/import", json={"data": {"models": []}}, headers=H)
check("空 models 400", resp.status_code == 400)
resp = cl.post("/api/llm-models/import", json={}, headers=H)
check("没给内容 400", resp.status_code == 400)
resp = cl.post("/api/llm-models/import", json={"text": json.dumps(snapshot)},
               headers=H)
check("文本形式也能导入", resp.status_code == 200, resp.status_code)

# 未登录不能导出（含密钥接口必须锁住）
cl2 = admin_app.app.test_client()
resp = cl2.get("/api/llm-models/export")
check("未登录导出被拒（401）", resp.status_code == 401, resp.status_code)

print("\n=== 10. 页面元素 ===")
with cl.session_transaction() as s:
    s["uid"] = "admin"
html = cl.get("/").get_data(as_text=True)
for el in ("btnLlmModelsExport", "btnLlmImportFile", "llmImportInput", "llmImportMode"):
    check("页面含 %s" % el, el in html)

# ⚠️ 同 id 会让 $() 只拿到第一个元素，按钮静默失效（1.1.2-beta3 线上踩过：
# 模型页「导出配置」和用量页「导出 CSV」都叫 btnLlmExport，点导出没反应）。
import collections as _c
import re as _re
_ids = _re.findall(r'\bid="([^"]+)"', html)
_dup = {k: v for k, v in _c.Counter(_ids).items() if v > 1}
check("模板内无重复 id", not _dup, _dup)
_js = io.open("admin/static/app.js", encoding="utf-8").read()
check("模型页导出按钮有独立绑定",
      _js.count("$('btnLlmModelsExport')") == 1
      and _js.count("$('btnLlmExport')") == 1,
      "models=%d csv=%d" % (_js.count("$('btnLlmModelsExport')"),
                            _js.count("$('btnLlmExport')")))
check("模型页导出指向 /api/llm-models/export",
      "/api/llm-models/export" in _js)

print("\n" + "=" * 46)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
