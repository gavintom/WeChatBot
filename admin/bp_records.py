"""admin 蓝图：对话记录 / 聊天记录 / 统计 / API 与 AI 用量。

路由实现从 admin/app.py 拆出，函数体未改动；共用工具在 admin/web.py。
"""

import time

from flask import Blueprint, jsonify, request, session

import chatlog
import store
from admin import pricing
from admin.web import (adminlog, csv_response, days_arg, login_required, ok,
                       page_args, paged)

bp = Blueprint("records", __name__)


# ---------------------------------------------------------------- 对话记录

@bp.route("/api/records")
@login_required
def api_records():
    p = request.args
    try:
        page = max(1, int(p.get("page") or 1))
        limit = min(500, max(10, int(p.get("limit") or 30)))
    except ValueError:
        page, limit = 1, 30
    f = dict(start=p.get("start") or "", end=p.get("end") or "",
             keyword=p.get("keyword") or "", kind=p.get("kind") or "",
             chat_type=p.get("chat_type") or "")
    r, page, limit, max_page = paged(
        lambda off: store.query_events(f["start"], f["end"], f["keyword"],
                                       f["kind"], f["chat_type"], limit, off),
        page, limit)
    return ok(**r, page=page, limit=limit, max_page=max_page)


@bp.route("/api/records/export")
@login_required
def api_records_export():
    p = request.args
    rows = store.all_events_for_export(p.get("start") or "", p.get("end") or "",
                                       p.get("keyword") or "", p.get("kind") or "",
                                       p.get("chat_type") or "")
    headers = ["时间", "会话", "类型", "发送者", "消息类型", "结果", "知识库", "触发",
               "插件", "提问", "回复", "耗时(ms)", "原因", "配图"]
    data = [[r.get("time_str"), r.get("chat_name") or r.get("chat"),
             r.get("chat_type"), r.get("sender_name") or r.get("sender"),
             r.get("msg_type"), r.get("kind"), r.get("policy_name"),
             r.get("trigger"), r.get("plugin") or "",
             r.get("question"), r.get("answer"),
             r.get("ms"), r.get("reason"), r.get("n_images")] for r in rows]
    return csv_response("对话记录-%s.csv" % time.strftime("%Y%m%d"), headers, data)


# ---------------------------------------------------------------- 聊天记录（独立库）

def _chat_filter_args():
    p = request.args
    return dict(start=p.get("start") or "", end=p.get("end") or "",
                chat_type=p.get("chat_type") or "", who=p.get("who") or "",
                keyword=p.get("keyword") or "", msg_type=p.get("msg_type") or "",
                direction=p.get("direction") or "")


@bp.route("/api/chat-messages")
@login_required
def api_chat_messages():
    try:
        page = max(1, int(request.args.get("page") or 1))
        limit = min(500, max(10, int(request.args.get("limit") or 30)))
    except ValueError:
        page, limit = 1, 30
    flt = _chat_filter_args()
    r, page, limit, max_page = paged(
        lambda off: chatlog.query(limit=limit, offset=off, **flt), page, limit)
    return ok(**r, page=page, limit=limit, max_page=max_page,
               stats=chatlog.stats())


@bp.route("/api/chat-messages/export")
@login_required
def api_chat_messages_export():
    rows = chatlog.all_for_export(**_chat_filter_args())
    headers = ["时间", "场景", "会话", "会话名称", "发送者", "显示名", "方向",
               "消息类型", "类型名", "正文", "原始内容", "@我", "@所有人",
               "localID", "媒体地址"]
    data = [[r.get("time_str"),
             {"group": "群聊", "private": "私聊"}.get(r.get("chat_type"), r.get("chat_type")),
             r.get("chat"), r.get("chat_name"), r.get("sender"), r.get("sender_name"),
             {"in": "收到", "out": "发出"}.get(r.get("direction"), r.get("direction")),
             r.get("type"), r.get("type_name"), r.get("text"), r.get("content"),
             "是" if r.get("at_me") else "", "是" if r.get("at_all") else "",
             r.get("local_id"), r.get("media_api")] for r in rows]
    return csv_response("聊天记录-%s.csv" % time.strftime("%Y%m%d"), headers, data)


@bp.route("/api/chat-messages/purge", methods=["POST"])
@login_required
def api_chat_messages_purge():
    """手动触发一次过期清理（定时线程也会自动跑）。"""
    n = chatlog.purge_expired()
    adminlog("清理过期聊天记录 user=%s 删除=%s", session.get("uid"), n)
    return ok(message="已清理 %d 条过期消息" % n, stats=chatlog.stats())


@bp.route("/api/stats")
@login_required
def api_stats():
    """运行监控图表数据源：一次请求同时给微信对话与接口调用两条趋势，
    保证时间窗口一致。days 由卡片上的 7/30/90 切换传入。"""
    days = days_arg(request.args)
    # api_calls_summary() 只查一次：原先同一请求算了两次。
    # api / api_summary 两个键都保留（/api/stats 的历史契约），共用同一结果。
    api_summary = store.api_calls_summary()
    return ok(summary=store.summary(), daily=store.daily_series(days),
               api=api_summary, llm=store.llm_summary(),
               api_daily=store.api_calls_daily(days),
               api_summary=api_summary)

@bp.route("/api/apicalls")
@login_required
def api_apicalls():
    p, page, limit = page_args()
    f = dict(start=p.get("start") or None, end=p.get("end") or None,
             keyword=(p.get("keyword") or "").strip()[:100] or None,
             only_failed=p.get("only_failed") == "1")
    r, page, limit, max_page = paged(
        lambda off: store.api_calls_records(limit=limit, offset=off, **f),
        page, limit)
    return ok(**r, page=page, limit=limit, max_page=max_page)


@bp.route("/api/api-calls/stats")
@login_required
def api_api_calls_stats():
    """API 调用汇总（含失败原因分布）+ 每日趋势。"""
    p = request.args
    return ok(summary=store.api_calls_summary(p.get("start") or None,
                                               p.get("end") or None),
               daily=store.api_calls_daily(days_arg(p), end_day=p.get("end") or None))


@bp.route("/api/api-calls/export")
@login_required
def api_api_calls_export():
    p = request.args
    rows = store.api_calls_all_for_export(
        start=p.get("start") or None, end=p.get("end") or None,
        keyword=(p.get("keyword") or "").strip()[:100] or None,
        only_failed=p.get("only_failed") == "1")
    headers = ["时间", "日期", "提问", "答案", "图片数", "提问字数", "会话ID", "调用方IP",
               "HTTP状态", "结果", "耗时(ms)", "LLM调用次数", "总token", "费用(元)", "失败原因"]
    data = [[r.get("time_str"), r.get("day"), r.get("question"), r.get("answer"),
             r.get("n_images"), r.get("q_chars"), r.get("session_id"), r.get("client_ip"),
             r.get("status"), "成功" if r.get("ok") else "失败", r.get("latency_ms"),
             r.get("calls"), r.get("total_tokens"), r.get("cost"), r.get("error")]
            for r in rows]
    return csv_response("API调用记录-%s.csv" % time.strftime("%Y%m%d"), headers, data)


# ---------------------------------------------------------------- AI 大模型用量

def _llm_model_labels():
    """上游模型 ID → 模型库里的纳管名称（可能一对多），给筛选下拉显示用。"""
    labels = {}
    for m in store.list_llm_models():
        labels.setdefault(m.get("model") or "", []).append(m.get("name") or "")
    return {k: "、".join(v) for k, v in labels.items() if k}


@bp.route("/api/llm-usage")
@login_required
def api_llm_usage():
    """区间汇总 + 按模型分布 + 每日趋势 + 今日（不能拿全量冒充）。

    汇总 / 分布 / 趋势 / 今日**全部带上同一个 model 条件**，
    与明细 /api/llm-records 的结果集严格一致，筛选后合计才对得上。
    """
    p = request.args
    start, end = p.get("start") or None, p.get("end") or None
    model = (p.get("model") or "").strip()[:100] or None
    s = store.llm_summary(start, end, model)
    s.update(ok=True,
             daily=store.llm_daily(days_arg(p), end_day=end, model=model),
             today=store.llm_summary(store.today(), store.today(), model),
             pricing=pricing.all_known_pricing(),
             # 下拉选项取全量模型，不能取筛选后的 models（那样筛完只剩一个可选）
             all_models=[m["model"] for m in store.llm_summary().get("models", [])
                         if m.get("model")],
             # 以「AI 大模型」页纳管的模型为主体做用量对照（含零用量 / 未纳管分组）
             registry=store.llm_registry_usage(start, end, model),
             model_labels=_llm_model_labels(),
             model_filter=model or "", start=start or "", end=end or "")
    return jsonify(s)


@bp.route("/api/llm-records")
@login_required
def api_llm_records():
    p, page, limit = page_args()
    f = dict(start=p.get("start") or None, end=p.get("end") or None,
             model=(p.get("model") or "").strip()[:100] or None)
    r, page, limit, max_page = paged(
        lambda off: store.llm_records(start=f["start"], end=f["end"],
                                      model=f["model"], limit=limit, offset=off),
        page, limit)
    return ok(**r, page=page, limit=limit, max_page=max_page,
               models=[m["model"] for m in store.llm_summary().get("models", [])])


@bp.route("/api/llm-records/export")
@login_required
def api_llm_export():
    p = request.args
    rows = store.llm_all_for_export(start=p.get("start") or None,
                                    end=p.get("end") or None,
                                    model=(p.get("model") or "").strip()[:100] or None)
    headers = ["时间", "日期", "模型", "来源", "输入token", "输出token", "总token",
               "费用(元)", "耗时(ms)", "流式", "成功"]
    data = [[r.get("time_str"), r.get("day"), r.get("model"), r.get("source"),
             r.get("prompt_tokens"), r.get("completion_tokens"), r.get("total_tokens"),
             r.get("cost"), r.get("latency_ms"), "是" if r.get("stream") else "否",
             "是" if r.get("ok") else "否"] for r in rows]
    return csv_response("大模型使用记录-%s.csv" % time.strftime("%Y%m%d"), headers, data)
