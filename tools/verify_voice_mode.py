"""验证语音 voice.mode=reply 模式（不连手机 / 大模型 / 微信，不导入主模块）。

覆盖：
  1) _voice_mode 归一化：asr / reply / 大小写空格 / 非法值 / 缺失值一律安全回退
  2) _handle_message 里 reply 拦截确实存在于 _stage_asr 之前，且回退话术取自 fallback_reply
  3) config.yaml / config.yaml.example 都保留 mode + fallback_reply 两个键
  4) webhook_server.py 语法可编译
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ((" | " + str(extra)) if extra and not cond else ""))


SRC_PATH = os.path.join(ROOT, "webhook_server.py")
with open(SRC_PATH, encoding="utf-8") as f:
    SRC = f.read()
TREE = ast.parse(SRC)

# --- 把 _voice_mode 函数单独抽出来，在隔离命名空间里真实执行 ---
fn = next((n for n in TREE.body
           if isinstance(n, ast.FunctionDef) and n.name == "_voice_mode"), None)
check("_voice_mode 函数存在", fn is not None)
if fn:
    mod = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(mod)
    code = compile(mod, "<_voice_mode>", "exec")

    def run_mode(cfg):
        ns = {"VCFG": cfg}
        exec(code, ns)
        return ns["_voice_mode"]()

    print("\n=== 1. _voice_mode 归一化 ===")
    check("mode=asr → asr", run_mode({"mode": "asr"}) == "asr")
    check("mode=reply → reply", run_mode({"mode": "reply"}) == "reply")
    check("mode=' REPLY ' 容忍大小写空格", run_mode({"mode": " REPLY "}) == "reply")
    check("mode=AsR 容忍大小写", run_mode({"mode": "AsR"}) == "asr")
    check("非法 mode 回退 asr", run_mode({"mode": "foobar"}) == "asr")
    check("缺失 mode 回退 asr", run_mode({}) == "asr")
    check("mode=None 回退 asr", run_mode({"mode": None}) == "asr")
    check("mode=空串回退 asr", run_mode({"mode": "   "}) == "asr")

# --- 静态断言 reply 分支的位置与关键调用 ---
print("\n=== 2. reply 拦截分支 ===")
handle = next((n for n in TREE.body
               if isinstance(n, ast.FunctionDef) and n.name == "_handle_message"), None)
check("_handle_message 函数存在", handle is not None)
if handle:
    seg = ast.get_source_segment(SRC, handle) or ""
    i_reply = seg.find('_voice_mode() == "reply"')
    i_asr = seg.find("_stage_asr(m)")
    check("存在 reply 模式判断", i_reply >= 0)
    check("reply 拦截在 ASR 之前", i_reply >= 0 and i_asr >= 0 and i_reply < i_asr)
    check("reply 分支读取 fallback_reply", "fallback_reply" in seg)
    check("reply 分支用 _send_text 发固定话术", "_send_text(m, match, reply)" in seg)
    check("reply 分支写 _event 流水", 'reason="语音 reply 模式' in seg)
    check("reply 分支带内置兜底话术", "暂时听不了语音" in seg)

# --- 配置文件两键保留 ---
print("\n=== 3. 配置文件 ===")
for name in ("config.yaml", "config.yaml.example"):
    p = os.path.join(ROOT, "data", name)
    with open(p, encoding="utf-8") as f:
        txt = f.read()
    check("%s 含 mode 键" % name, 'mode: "asr"' in txt)
    check("%s 含 fallback_reply 键" % name, "fallback_reply:" in txt)

# --- 语法编译 ---
print("\n=== 4. 语法检查 ===")
try:
    compile(SRC, SRC_PATH, "exec")
    check("webhook_server.py 语法可编译", True)
except SyntaxError as e:
    check("webhook_server.py 语法可编译", False, e)

print("\n" + "=" * 46)
print("通过 %d 项，失败 %d 项" % (len(OK), len(FAIL)))
if FAIL:
    for x in FAIL:
        print("  [FAIL] " + x)
    sys.exit(1)
