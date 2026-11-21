"""验证 1.1.2-beta8：大模型多模态能力位 + 输入 token 上限（任务4）。

不连大模型 / 微信，全程临时库 + 直接调纯函数：

覆盖：
  1) llm_models 新字段落库：support_image / support_file / max_input_tokens
  2) 老库迁移：SCHEMA + migrations 都能补列（用新建库模拟，列齐全）
  3) _llm_row_to_cfg 把新字段透传给 brain；export 含新字段；list 归一化为 bool
  4) 导出→导入往返，新字段值不丢
  5) brain._cap 能力读取优先级：llm > external_llm > 默认
  6) brain._media_content：支持/不支持的附件分别如何组装 content
  7) brain._estimate_tokens / _trim_messages：超限时丢最老历史、保 system + 当前问题
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

_TMP = tempfile.mkdtemp(prefix="wxbot_multimodal_")
os.environ["WXBOT_DB"] = os.path.join(_TMP, "analytics.db")
os.environ.setdefault("WXBOT_ADMIN_LOG", os.path.join(_TMP, "admin.log"))

import store  # noqa: E402
import brain  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  [OK] " if cond else "  [FAIL] ") + name +
          ((" | " + str(extra)) if extra and not cond else ""))


store.init_db()

print("=== 1. 新字段落库 ===")
mid = store.save_llm_model({
    "name": "视觉模型", "provider": "OpenAI", "base_url": "https://api.example.com/v1",
    "api_key": "sk-x", "model": "gpt-4o", "temperature": 0.3, "stream": True,
    "support_image": True, "support_file": False, "max_input_tokens": 8192,
    "max_tokens": 2048,
})
row = store.get_llm_model(str(mid))
check("support_image 落库", bool(row["support_image"]) is True, row.get("support_image"))
check("support_file 落库", bool(row["support_file"]) is False, row.get("support_file"))
check("max_input_tokens 落库", row["max_input_tokens"] == 8192, row.get("max_input_tokens"))
check("max_tokens 仍独立", row["max_tokens"] == 2048, row.get("max_tokens"))

print("\n=== 2. 老库迁移补列 ===")
cols = {r["name"] for r in store._rows("PRAGMA table_info(llm_models)")}
for c in ("support_image", "support_file", "max_input_tokens"):
    check("llm_models 含列 %s" % c, c in cols)
# 再跑一次 init_db 幂等，不报错、列不重复
try:
    store.init_db()
    check("init_db 幂等可重复", True)
except Exception as e:
    check("init_db 幂等可重复", False, e)

print("\n=== 3. cfg 透传 / list 归一化 ===")
cfg = store._llm_row_to_cfg(store._one("SELECT * FROM llm_models WHERE id=?", (mid,)), "default")
check("cfg support_image 透传", cfg.get("support_image") is True, cfg)
check("cfg support_file 透传", cfg.get("support_file") is False, cfg)
check("cfg max_input_tokens 透传", cfg.get("max_input_tokens") == 8192, cfg)
lst = [x for x in store.list_llm_models() if x["id"] == mid][0]
check("list support_image 为 bool", lst["support_image"] is True, type(lst["support_image"]))
check("list max_input_tokens 为 int", lst["max_input_tokens"] == 8192)

print("\n=== 4. 导出→导入往返不丢字段 ===")
exp = store.export_llm_models()
m = [x for x in exp["models"] if x["name"] == "视觉模型"][0]
check("导出含 support_image", m["support_image"] is True)
check("导出含 max_input_tokens", m["max_input_tokens"] == 8192)
# 删掉再导入（replace 模式）
store.import_llm_models(exp, mode="replace")
back = store.get_llm_model("视觉模型")
check("导入后 support_image 保持", bool(back["support_image"]) is True)
check("导入后 max_input_tokens 保持", back["max_input_tokens"] == 8192)

print("\n=== 5. brain._cap 读取优先级 ===")
b = brain.HybridRAGBrain({"external_llm": {
    "support_image": False, "support_file": True, "max_input_tokens": 1024}})
b.llm = {"support_image": True, "max_input_tokens": 4096}
check("_cap 优先取 llm.support_image", b._cap("support_image", False) is True)
check("_cap 回落 external_llm.support_file", b._cap("support_file", False) is True)
check("_cap llm 有值不被 external 覆盖", b._cap("max_input_tokens", 0) == 4096)
b.llm = {}
check("_cap 全无时用默认值", b._cap("no_such_key", "DEF") == "DEF")

print("\n=== 6. brain._media_content 组装 ===")
atts = [
    {"kind": "image", "name": "a.png", "data_url": "data:image/png;base64,AAAA"},
    {"kind": "file", "name": "b.pdf", "data_url": "data:application/pdf;base64,BBBB"},
]
content, used = brain.HybridRAGBrain._media_content("看这张图", atts, True, False)
check("仅图片被采用", used == ["image"], used)
check("content 为数组", isinstance(content, list))
check("首段是 text", content[0]["type"] == "text" and content[0]["text"] == "看这张图")
check("图片用 image_url.data URL", content[1]["image_url"]["url"].startswith("data:image/png"))
content2, used2 = brain.HybridRAGBrain._media_content("看文件", atts, True, True)
check("图片+文件都被采用", used2 == ["image", "file"], used2)
check("文件段带 file_data", content2[-1]["type"] == "file" and
      "file_data" in content2[-1]["file"])
content3, used3 = brain.HybridRAGBrain._media_content("纯文字", atts, False, False)
check("不支持时退回纯文本", used3 == [] and content3 == "纯文字")

print("\n=== 7. 输入 token 截断 ===")
msgs = [
    {"role": "system", "content": "系统提示"},
    {"role": "user", "content": "旧问题一"},
    {"role": "assistant", "content": "旧回答一"},
    {"role": "user", "content": "当前问题" * 3},
]
trimmed, did = b._trim_messages(msgs, 12)
check("超限触发裁剪", did is True)
check("system 保留", trimmed[0]["role"] == "system")
check("当前 user 保留", trimmed[-1]["role"] == "user")
check("历史被丢弃", len(trimmed) == 2, len(trimmed))
no_trim, did2 = b._trim_messages(msgs, 0)
check("上限0不裁剪", did2 is False and no_trim == msgs)

print("\n" + "=" * 50)
print("通过 %d 项，失败 %d 项" % (len(OK), len(FAIL)))
if FAIL:
    for x in FAIL:
        print("  [FAIL] " + x)
    sys.exit(1)
