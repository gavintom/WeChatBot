"""验证 brain.chat_only：插件直连内置大模型的通道。

用假的 requests.post 顶替真实 LLM 网关，检查：
  · 请求打到了 base_url + /chat/completions
  · payload 里带上了 system 消息、model、temperature
  · 流式 SSE 能正确拼出正文
  · 非流式兜底能正确取到 content
  · 两边都失败时抛异常（**不返回兜底话术**）
  · 用量会记进 llm_usage
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain  # noqa: E402

# 隔离真实环境：_resolve_llm 会先从 store 读后台选定的默认模型，
# 若运行环境已配了真实 LLM（部署机就是），会覆盖下面 make_brain 传入的假配置，
# 让「请求落到哪」这类断言不稳定。统一屏蔽，强制走本脚本的假 cfg。
import store as _store  # noqa: E402
_store.resolve_llm_config = lambda model=None: None

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ((" | " + str(extra)) if extra and not cond else ""))


class FakeResp:
    def __init__(self, status=200, lines=None, body=None, text=""):
        self.status_code = status
        self._lines = lines or []
        self._body = body
        self.text = text

    def iter_lines(self, decode_unicode=False):
        for ln in self._lines:
            yield ln if isinstance(ln, bytes) else ln.encode("utf-8")

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError("HTTP %s" % self.status_code)

    def close(self):
        pass


def make_brain(**llm):
    cfg = {"external_llm": dict({
        "base_url": "https://llm.example.com/v1", "api_key": "sk-test",
        "model": "test-model", "temperature": 0.3, "stream": True,
        "connect_timeout": 5, "read_timeout": 30, "total_timeout": 120,
    }, **llm)}
    b = brain.HybridRAGBrain(cfg, log=lambda *a, **k: None)
    return b


print("\n=== 1. 流式调用 ===")
b = make_brain()
seen = {}


def fake_post_stream(url, headers=None, json=None, stream=False, timeout=None):
    seen.update({"url": url, "headers": headers, "payload": json, "stream": stream})
    sse = [
        'data: {"choices":[{"delta":{"content":"你好"}}]}',
        'data: {"choices":[{"delta":{"content":"，世界"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,'
        '"total_tokens":15}}',
        "data: [DONE]",
    ]
    return FakeResp(200, lines=sse)


brain.requests.post = fake_post_stream
out = b.chat_only([{"role": "user", "content": "总结一下这篇"}],
                   system_prompt="你是摘要助手")
check("流式拼出完整正文", out == "你好，世界", repr(out))
check("打到了 chat/completions", seen["url"] == "https://llm.example.com/v1/chat/completions", seen["url"])
check("带上了 Bearer 鉴权", seen["headers"].get("Authorization") == "Bearer sk-test", seen["headers"])
check("payload 带 model", seen["payload"]["model"] == "test-model", seen["payload"])
check("payload 带 stream=True", seen["payload"].get("stream") is True, seen["payload"])
msgs = seen["payload"]["messages"]
check("system 消息在最前", msgs and msgs[0]["role"] == "system"
      and msgs[0]["content"] == "你是摘要助手", msgs)
check("user 消息在最后", msgs and msgs[-1]["role"] == "user"
      and msgs[-1]["content"] == "总结一下这篇", msgs)

print("\n=== 2. 温度与 max_tokens 覆盖 ===")
b2 = make_brain()
seen2 = {}


def fake_post2(url, headers=None, json=None, stream=False, timeout=None):
    seen2.update({"payload": json})
    return FakeResp(200, lines=[b'data: {"choices":[{"delta":{"content":"ok"}}]}',
                                b'data: [DONE]'])


brain.requests.post = fake_post2
b2.chat_only([{"role": "user", "content": "x"}], temperature=0.9, max_tokens=128)
check("temperature 被覆盖", seen2["payload"].get("temperature") == 0.9, seen2["payload"])
check("max_tokens 被带上", seen2["payload"].get("max_tokens") == 128, seen2["payload"])

print("\n=== 3. 流式失败 → 非流式兜底 ===")
b3 = make_brain()
calls = {"n": 0}


def fake_post3(url, headers=None, json=None, stream=False, timeout=None):
    calls["n"] += 1
    if stream:
        return FakeResp(500, text="boom")
    return FakeResp(200, body={"choices": [{"message": {"content": "非流式结果"}}],
                               "usage": {"prompt_tokens": 3, "completion_tokens": 2}})


brain.requests.post = fake_post3
out3 = b3.chat_only([{"role": "user", "content": "x"}])
check("非流式兜底拿到内容", out3 == "非流式结果", repr(out3))
check("确实先试了流式", calls["n"] >= 2, calls)

print("\n=== 4. 全失败时抛异常（不返回兜底话术） ===")
b4 = make_brain()


def fake_post4(url, headers=None, json=None, stream=False, timeout=None):
    if stream:
        return FakeResp(500, text="boom")
    raise RuntimeError("全部挂掉")


brain.requests.post = fake_post4
try:
    r = b4.chat_only([{"role": "user", "content": "x"}])
    check("失败时抛异常", False, "返回了 %r" % r)
    check("异常不是兜底话术", "暂时不可用" not in str(r), r)
except Exception as e:
    check("失败时抛异常", True)
    check("异常不是兜底话术", "暂时不可用" not in str(e), e)

print("\n=== 5. 脏消息被过滤 ===")
b5 = make_brain()
seen5 = {}


def fake_post5(url, headers=None, json=None, stream=False, timeout=None):
    seen5.update({"payload": json})
    return FakeResp(200, lines=[b'data: {"choices":[{"delta":{"content":"ok"}}]}',
                                b'data: [DONE]'])


brain.requests.post = fake_post5
b5.chat_only([{"role": "user", "content": "正常"},
               {"role": "bogus", "content": "脏角色"},
               {"role": "user", "content": "   "},
               "不是字典"])
msgs5 = seen5["payload"]["messages"]
check("脏角色被丢弃", all(m["role"] == "user" for m in msgs5), msgs5)
check("空内容被丢弃", len(msgs5) == 1, msgs5)

print("\n=== 6. 空消息直接报错 ===")
try:
    b5.chat_only([])
    check("空消息抛异常", False)
except Exception:
    check("空消息抛异常", True)

print("\n=== 7. 用量记进 llm_usage ===")
import store  # noqa: E402
logged = []
_orig_log = store.log_llm_usage


def fake_log(attempts, model=None, source="normal", llm_id=None):
    logged.append({"attempts": list(attempts or []), "source": source, "llm_id": llm_id})


store.log_llm_usage = fake_log
b6 = make_brain()


def fake_post6(url, headers=None, json=None, stream=False, timeout=None):
    return FakeResp(200, lines=[
        'data: {"choices":[{"delta":{"content":"ok"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,'
        '"total_tokens":15}}',
        "data: [DONE]"])


brain.requests.post = fake_post6
b6.chat_only([{"role": "user", "content": "x"}], usage_source="plugin")
store.log_llm_usage = _orig_log
check("记了一条用量", len(logged) == 1, logged)
if logged:
    check("source 标为 plugin", logged[0]["source"] == "plugin", logged[0])
    check("带上了 token 数", logged[0]["attempts"]
          and logged[0]["attempts"][0].get("total_tokens") == 15, logged[0]["attempts"])

print("\n=== 7b. 上游不返回 usage 时不臆造 token 数 ===")
logged.clear()
store.log_llm_usage = fake_log
b8 = make_brain()
brain.requests.post = fake_post5      # 这条 SSE 里没有 usage
b8.chat_only([{"role": "user", "content": "x"}], usage_source="plugin")
store.log_llm_usage = _orig_log
check("没有 usage 时不写 token=0 的假账",
      not logged or not logged[0]["attempts"]
      or logged[0]["attempts"][0].get("total_tokens") is None, logged)

print("\n=== 8. model_override 用完即清 ===")
b7 = make_brain()
b7.chat_only([{"role": "user", "content": "x"}])
check("model_override 已复位", b7.model_override is None, b7.model_override)

print("\n" + "=" * 50)
print("通过 %d 项，失败 %d 项" % (len(OK), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部通过 ✅")
