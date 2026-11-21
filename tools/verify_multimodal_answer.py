"""验证 1.1.2-beta8 任务3：引用消息带图片/文件 → 一并上传大模型。

不连大模型 / 微信，全程拦 requests.post 看真实请求体：

覆盖：
  1) answer(attachments=[图片]) 且模型 support_image=True → payload 里 user content 为
     多模态数组，含 text + image_url(data URL)
  2) 文件附件 support_file=True → payload 含 file content
  3) 模型不支持（默认保守 False）→ 不发任何 HTTP，直接回「暂不支持图片、文件」提示
  4) media_unsupported_reply 自定义文案生效
  5) webhook._quote_attachments：模拟引用图片/文件的 quoteResolved + FakeWX，
     产出正确 kind 的 data URL 附件；无引用/下载失败返回空
  6) 图片/文件本体消息仍走静默跳过（任务2 联动，不回话术）
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

_TMP = tempfile.mkdtemp(prefix="wxbot_multianswer_")
os.environ["WXBOT_DB"] = os.path.join(_TMP, "analytics.db")
os.environ.setdefault("WXBOT_ADMIN_LOG", os.path.join(_TMP, "admin.log"))

import brain  # noqa: E402
import store  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  [OK] " if cond else "  [FAIL] ") + name +
          ((" | " + str(extra)) if extra and not cond else ""))


store.init_db()


class FakeResp:
    def __init__(self, code=200, lines=None):
        self.status_code = code
        self._lines = lines or []
        self.text = ""

    def iter_lines(self, decode_unicode=False):
        for ln in self._lines:
            yield ln if isinstance(ln, bytes) else ln.encode("utf-8")

    def json(self):
        return {}

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError("HTTP %s" % self.status_code)

    def close(self):
        pass


def make_brain(support_image=False, support_file=False, tip=None):
    cfg = {
        "external_llm": {"base_url": "https://llm.example.com/v1", "model": "test-model",
                         "api_key": "sk-test", "stream": True, "temperature": 0.3,
                         "support_image": support_image, "support_file": support_file},
        "retrieval": {"top_k": 4},
        "reply": {"media_unsupported_reply": tip} if tip else {},
        "system_prompt": "你是客服",
    }
    return brain.HybridRAGBrain(cfg, log=lambda *a, **k: None)


IMG = {"kind": "image", "name": "a.png", "data_url": "data:image/png;base64,AAAA"}
FILE = {"kind": "file", "name": "b.pdf", "data_url": "data:application/pdf;base64,BBBB"}

print("=== 1. 图片附件随正文发给模型 ===")
b = make_brain(support_image=True)
seen = {}


def fake_post(url, headers=None, json=None, stream=False, timeout=None):
    seen.update({"payload": json, "calls": seen.get("calls", 0) + 1})
    sse = ['data: {"choices":[{"delta":{"content":"看到了"}}]}', "data: [DONE]"]
    return FakeResp(200, lines=sse)


brain.requests.post = fake_post
# 空检索：sources=[] 不跑 BM25
out = b.answer("这是什么", sources=[], attachments=[IMG])
check("answer 正常返回文字", out == "看到了", repr(out))
msgs = seen.get("payload", {}).get("messages", [])
user = msgs[-1]["content"] if msgs else None
check("user content 是多模态数组", isinstance(user, list), type(user))
if isinstance(user, list):
    check("首段为 text", user[0].get("type") == "text")
    check("含 image_url 段", any(p.get("type") == "image_url" for p in user))
    img_part = [p for p in user if p.get("type") == "image_url"][0]
    check("图片用 data URL", img_part["image_url"]["url"].startswith("data:image/png"))

print("\n=== 2. 文件附件随正文发给模型 ===")
b2 = make_brain(support_file=True)
seen2 = {}


def fake_post2(url, headers=None, json=None, stream=False, timeout=None):
    seen2.update({"payload": json})
    return FakeResp(200, lines=['data: {"choices":[{"delta":{"content":"读到文件"}}]}',
                                "data: [DONE]"])


brain.requests.post = fake_post2
b2.answer("读一下", sources=[], attachments=[FILE])
u2 = seen2.get("payload", {}).get("messages", [])[-1]["content"]
check("含 file 段", isinstance(u2, list) and any(p.get("type") == "file" for p in u2))
if isinstance(u2, list):
    fp = [p for p in u2 if p.get("type") == "file"][0]
    check("文件段带 file_data", "file_data" in fp.get("file", {}))

print("\n=== 3. 模型不支持 → 不发请求、回提示 ===")
b3 = make_brain(support_image=False)  # 默认保守：不支持
calls = {"n": 0}


def fake_post3(url, **kw):
    calls["n"] += 1
    return FakeResp(200, lines=["data: [DONE]"])


brain.requests.post = fake_post3
tip = b3.answer("看图", sources=[], attachments=[IMG])
check("没有发起 LLM 请求", calls["n"] == 0, calls["n"])
check("回默认暂不支持提示", "暂时看不了" in tip, tip)

print("\n=== 4. 自定义提示文案生效 ===")
b4 = make_brain(support_image=False, tip="本模型不支持读图，请换文字描述")
brain.requests.post = lambda *a, **k: FakeResp(200, lines=["data: [DONE]"])
tip4 = b4.answer("看图", sources=[], attachments=[IMG])
check("自定义提示生效", tip4 == "本模型不支持读图，请换文字描述", tip4)

print("\n=== 5. webhook._quote_attachments 构建附件 ===")
# 用假 WX 模拟下载
import webhook_server as ws  # noqa: E402


class FakeWX:
    def media(self, api, chat, local_id):
        return b"\x89PNG\r\n\x1a\nfake", "png"

    def file(self, chat, local_id):
        return b"%PDF-1.7 fake", "pdf"


ws.WX = FakeWX()
base_m = {"chat": "g1", "app_type": "quote", "quote": {"type": 3}}
m_img = dict(base_m, quote_resolved={"found": True, "quoteType": 3,
                                     "localID": 123, "mediaApi": "/api/media?chat=g1&localID=123&hd=1"})
atts_img = ws._quote_attachments(m_img)
check("引用图片产出 1 个附件", len(atts_img) == 1, atts_img)
check("附件 kind=image", atts_img and atts_img[0]["kind"] == "image")
check("附件 data_url 正确", atts_img and atts_img[0]["data_url"].startswith("data:image/png"))
check("HD 参数被剥掉", "hd=1" not in atts_img[0]["data_url"] if atts_img else False)

m_file = dict(base_m, quote={"type": 49},
              quote_resolved={"found": True, "quoteType": 49,
                              "localID": 456, "fileName": "报价.pdf"})
atts_file = ws._quote_attachments(m_file)
check("引用文件产出 1 个附件", len(atts_file) == 1, atts_file)
check("附件 kind=file", atts_file and atts_file[0]["kind"] == "file")
check("文件名保留", atts_file and atts_file[0]["name"] == "报价.pdf")

m_none = dict(base_m, quote_resolved={"found": False})
check("无 quoteResolved 返回空", ws._quote_attachments(m_none) == [])
check("非引用消息返回空", ws._quote_attachments(dict(base_m, app_type="text")) == [])

print("\n=== 6. 任务2联动：媒体本体仍静默 ===")
import ast
web = open(os.path.join(BASE, "webhook_server.py"), encoding="utf-8").read()
tree = ast.parse(web)
br = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
          and n.name == "_stage_kind_branch")
seg = ast.get_source_segment(web, br)
block = seg.split('if kind == "other"')[0]
check("image/video 分支静默 _skip", "_skip(m, match, \"媒体消息仅落库不回复" in block)
check("静默分支不上传附件", "_quote_attachments" not in block and "_send_text" not in block)

print("\n" + "=" * 50)
print("通过 %d 项，失败 %d 项" % (len(OK), len(FAIL)))
if FAIL:
    for x in FAIL:
        print("  [FAIL] " + x)
    sys.exit(1)
