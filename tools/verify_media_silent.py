"""验证图片/文件消息静默跳过 + 只落库（不连手机 / 大模型 / 微信）。

覆盖：
  1) _stage_kind_branch：image/video 分支静默 _skip，不再回「暂不支持」
  2) webhook 调用 resolve 不再传 ignore_trigger；policy.resolve 已无该参数与 media trigger
  3) 废弃配置 media_need_trigger / unsupported_reply 从两个配置文件移除
  4) 媒体落库字段完整（会话/发送者/时间/文件），chatlog 归档字段完整
  5) 语法可编译
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


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


WEB = read("webhook_server.py")
POL = read("policy.py")
WEB_TREE = ast.parse(WEB)
POL_TREE = ast.parse(POL)

print("=== 1. 媒体消息静默跳过 ===")
branch = next((n for n in WEB_TREE.body
               if isinstance(n, ast.FunctionDef) and n.name == "_stage_kind_branch"), None)
check("_stage_kind_branch 存在", branch is not None)
if branch:
    seg = ast.get_source_segment(WEB, branch) or ""
    check("image/video 分支用 _skip 静默", '_skip(m, match, "媒体消息仅落库不回复' in seg)
    check("不再读取 unsupported_reply", "unsupported_reply" not in seg)
    # 只看 image/video 这一小块（到 other 分支之前），不应有发送动作；
    # 语音识别失败分支里的 _send_text 是正常的，注释里「不要暂不支持提示」也允许保留
    img_block = seg.split('if kind == "other"')[0]
    check("image/video 分支不再 _send_text", "_send_text" not in img_block)

print("\n=== 2. ignore_trigger 废代码清理 ===")
check("webhook 不再计算 ignore_trigger", "ignore_trigger" not in WEB)
check("webhook 不再传 ignore_trigger", "ignore_trigger=" not in WEB)
resolve_fn = next((n for n in POL_TREE.body
                   if isinstance(n, ast.FunctionDef) and n.name == "resolve"), None)
check("policy.resolve 存在", resolve_fn is not None)
if resolve_fn:
    args = [a.arg for a in resolve_fn.args.args]
    check("resolve 签名无 ignore_trigger 参数", "ignore_trigger" not in args, args)
check("policy 无 media trigger 分支", 'trigger = "media"' not in POL)
check("policy 全文无 ignore_trigger", "ignore_trigger" not in POL)

print("\n=== 3. 废弃配置移除 ===")
for name in ("config.yaml", "config.yaml.example"):
    txt = read(os.path.join("data", name))
    check("%s 无 media_need_trigger" % name, "media_need_trigger" not in txt)
    check("%s 无旧 unsupported_reply" % name,
          'unsupported_reply: "暂不支持' not in txt)
    check("%s 有 media_unsupported_reply" % name, "media_unsupported_reply:" in txt)

print("\n=== 4. 落库字段完整性 ===")
ms = read("media_store.py")
check("媒体表有会话 chat", "chat           TEXT NOT NULL" in ms)
check("媒体表有发送者 wxid", "wxid           TEXT NOT NULL" in ms)
check("媒体表有发送时间 sent_at", "sent_at        REAL NOT NULL" in ms)
check("媒体表有文件路径 rel_path", "rel_path       TEXT NOT NULL" in ms)
check("媒体表有来源 via", "via            TEXT NOT NULL" in ms)
cl = read("chatlog.py")
check("聊天记录库有会话 chat", "chat" in cl and "chat_name" in cl)
check("聊天记录库有发送者 sender", "sender" in cl)
check("聊天记录库有时间 ts", "ts" in cl)

print("\n=== 5. 语法检查 ===")
for rel in ("webhook_server.py", "policy.py"):
    try:
        compile(read(rel), rel, "exec")
        check("%s 语法可编译" % rel, True)
    except SyntaxError as e:
        check("%s 语法可编译" % rel, False, e)

print("\n" + "=" * 46)
print("通过 %d 项，失败 %d 项" % (len(OK), len(FAIL)))
if FAIL:
    for x in FAIL:
        print("  [FAIL] " + x)
    sys.exit(1)
