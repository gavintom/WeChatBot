"""AI 大模型费用计算（管理端「AI 用量」与「API 调用记录」共用）。

单价的真实情况（必须先说清，否则用户会对数字掉以轻心）：
- 价格由**模型供应商**定，同一模型走不同渠道（官方 / 第三方网关）可能差几倍；
- 厂商会随时调价，内置价只是**量级参考**；
- 有些网关按次收费而非按 token，按 token 估出来的费用只能当参考。

策略：
1. 内置一份常见模型的默认价（单位：元 / 百万 token），标注为「参考价」；
2. 可在 `config.yaml` 的 `brain.llm_pricing` 覆盖或新增（**以实际账单为准**）；
3. 支持 `per_call`（按次）与 `{in, out}`（按 token）两种规则；
4. 查不到单价的模型 → cost 记 0，并在 UI 标「单价未配置」，
   绝不把「未配置」显示成「0 元 / 不花钱」。

配置示例（写进 config.yaml）：
    brain:
      llm_pricing:
        deepseek/deepseek-v4.1-flash: {in: 1.0, out: 2.0}   # 元 / 百万 token
        some-gateway-model:           {per_call: 0.01}       # 每次固定 1 分
"""
import yaml

from . import cfg as _cfgmod            # noqa: E402

# 运行时读模块变量，测试可用 WXBOT_CONFIG 指向沙箱副本
_MILLION = 1_000_000.0

# —— 内置参考价（元 / 百万 token）——
# key 用模型名的可匹配片段（小写），匹配时取最长命中，避免 `gpt-4` 抢走 `gpt-4o-mini`。
DEFAULT_PRICING = {
    "deepseek-chat":          {"in": 2.0, "out": 8.0},
    "deepseek-reasoner":      {"in": 3.0, "out": 9.0},
    "deepseek-v3":            {"in": 2.0, "out": 8.0},
    "flash":                  {"in": 0.5, "out": 1.5},
    "mini":                   {"in": 0.3, "out": 1.2},
    "lite":                   {"in": 0.15, "out": 0.6},
    "gpt-4o-mini":            {"in": 0.15, "out": 0.6},
    "gpt-4o":                 {"in": 2.5, "out": 10.0},
    "gpt-4.1-mini":           {"in": 0.4, "out": 1.6},
    "gpt-4.1":                {"in": 2.0, "out": 8.0},
    "claude-3-5-haiku":       {"in": 0.8, "out": 4.0},
    "claude-3-5-sonnet":      {"in": 3.0, "out": 15.0},
    "qwen":                   {"in": 0.8, "out": 2.4},
    "glm-4.5":                {"in": 0.6, "out": 2.2},
    "glm-4":                  {"in": 0.6, "out": 0.6},
    "moonshot":               {"in": 1.2, "out": 4.0},
    "doubao":                 {"in": 0.8, "out": 2.0},
    "kimi":                   {"in": 1.0, "out": 3.0},
    "whisper":                {"in": 0.0, "out": 0.0},
    "yi-":                    {"in": 0.4, "out": 1.2},
    "default":                {"in": 1.0, "out": 3.0},
}


def _load_user_pricing():
    """读 config.yaml 的 brain.llm_pricing。每次重读（后台改完价要立刻生效）。"""
    try:
        c = yaml.safe_load(open(_cfgmod.CONFIG_PATH, encoding="utf-8")) or {}
        p = ((c.get("brain") or {}).get("llm_pricing")) or {}
        return p if isinstance(p, dict) else {}
    except Exception:
        return {}


def get_pricing(model):
    """返回 {kind: token|per_call|unknown, ...}。

    查找顺序：用户配置（精确 > 片段最长）→ 内置参考价（片段最长）→ unknown。
    """
    model = (model or "").strip()
    if not model:
        return {"kind": "unknown"}
    user = _load_user_pricing()
    low = model.lower()

    def norm(d):
        if not isinstance(d, dict):
            return None
        if "per_call" in d:
            try:
                return {"kind": "per_call", "price": float(d["per_call"])}
            except (TypeError, ValueError):
                return None
        if "price" in d and "in" not in d and "out" not in d:
            try:
                return {"kind": "per_call", "price": float(d["price"])}
            except (TypeError, ValueError):
                return None
        if "in" in d or "out" in d:
            try:
                return {"kind": "token", "in": float(d.get("in") or 0.0),
                        "out": float(d.get("out") or 0.0)}
            except (TypeError, ValueError):
                return None
        return None

    if model in user:
        r = norm(user[model])
        if r:
            return r
    for key in sorted(user, key=len, reverse=True):
        if key.lower() in low:
            r = norm(user[key])
            if r:
                return r
    for key in sorted(DEFAULT_PRICING, key=len, reverse=True):
        if key in low:
            return dict(DEFAULT_PRICING[key], kind="token")
    return {"kind": "unknown"}


def compute_cost(model, prompt_tokens, completion_tokens):
    """算一次调用费用（元）。返回 (费用, 是否已知单价)。"""
    p = get_pricing(model)
    if p.get("kind") == "per_call":
        return round(float(p.get("price") or 0.0), 6), True
    if p.get("kind") != "token":
        return 0.0, False
    pi = int(prompt_tokens or 0)
    co = int(completion_tokens or 0)
    cost = (pi / _MILLION) * float(p.get("in") or 0.0) + \
           (co / _MILLION) * float(p.get("out") or 0.0)
    return round(cost, 6), True


def pricing_table():
    """UI 展示用：用户自己配的单价规则。"""
    return [{"match": k, "source": "config.yaml", "rule": v}
            for k, v in sorted(_load_user_pricing().items())]


def all_known_pricing():
    """内置 + 用户配置合并，让用户知道哪些模型其实有参考价。"""
    merged = {k: {"source": "内置参考", "rule": {"in": v["in"], "out": v["out"]}}
              for k, v in DEFAULT_PRICING.items()}
    for row in pricing_table():
        merged[row["match"]] = {"source": row["source"], "rule": row["rule"]}
    return merged
