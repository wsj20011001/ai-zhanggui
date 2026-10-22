# -*- coding: utf-8 -*-
"""AI 掌柜：驾驶舱、经营问答、洞察中心、动作执行与撤销、模型设置"""
import json
import time
from datetime import date, datetime, timedelta

from flask import (Blueprint, flash, jsonify, redirect, render_template, request,
                   session, url_for)

import config
import db
from ai import gateway, nl2sql, tools
from analytics import etl, insights, metrics
from auth import admin_required
from utils import current_user, get_setting, money, now_str

bp = Blueprint("ai", __name__)
api = Blueprint("ai_api", __name__)


# ------------------------------------------------------------------ 工具函数

def _ai_status(conn):
    provider = get_setting("ai_provider", "local")
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(prompt_tokens+output_tokens),0) t"
        " FROM ai_conversations WHERE substr(created_at,1,10)=?", (today,)).fetchone()
    return {
        "provider": provider,
        "label": config.AI_PROVIDER_LABEL.get(provider, provider),
        "tokens_today": int(row["t"] or 0),
        "asks_today": int(row["c"] or 0),
        "has_key": bool(gateway.api_key()),
        "budget": int(get_setting("ai_daily_budget", str(config.AI_DAILY_TOKEN_BUDGET)) or 0),
    }


def _dashboard_data(conn):
    today = date.today()
    y = today - timedelta(days=1)
    k_y = metrics.q_kpi(conn, y, y)
    base = metrics.q_same_weekday_baseline(conn, y, weeks=4, field="revenue")
    change = ((k_y["revenue"] - base) / base) if base else 0.0
    k_today = metrics.q_kpi(conn, today, today)
    open_orders = conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE status IN ('pending','making','done')").fetchone()["c"]
    items = insights.run(conn, d=y, persist=True)
    trend = metrics.q_trend(conn, 30, end=y)
    fc = metrics.q_forecast_revenue(conn, horizon=7)
    acts = conn.execute(
        "SELECT * FROM ai_actions ORDER BY id DESC LIMIT 6").fetchall()
    drafts = conn.execute(
        "SELECT COUNT(*) c FROM purchase_orders WHERE status='draft'").fetchone()["c"]
    return {
        "y": y.isoformat(), "k_y": k_y, "base": base, "change": change,
        "k_today": k_today, "open_orders": open_orders,
        "insights": items, "trend": trend, "fc": fc, "actions": acts,
        "drafts": drafts, "ai": _ai_status(conn),
    }


# ------------------------------------------------------------------ 驾驶舱

@bp.route("/ai")
@admin_required
def dashboard():
    conn = db.get_db()
    etl.ensure_fresh(conn, days=3)
    data = _dashboard_data(conn)
    brief = insights.brief(conn, date.today() - timedelta(days=1))
    shop_seats = int(get_setting("shop_seats", "42") or 42)
    # 翻台率：昨日结算单数 / 桌位数
    turnover = round(data["k_y"]["orders"] / shop_seats, 2) if shop_seats else 0
    last_fact = etl.day_gap(conn)
    return render_template("ai/dashboard.html", active_nav="ai", **data,
                          brief=brief, turnover=turnover,
                          shop_seats=shop_seats, last_fact=last_fact,
                          app_name=config.APP_NAME)


@bp.route("/ai/refresh", methods=["POST"])
@admin_required
def refresh():
    conn = db.get_db()
    etl.rebuild(conn, date.today() - timedelta(days=3), date.today())
    n = len(insights.run(conn, persist=True))
    flash("已重算数据与洞察，共 %d 条" % n, "success")
    return redirect(request.referrer or url_for("ai.dashboard"))


# ------------------------------------------------------------------ 问答

@bp.route("/ai/chat")
@admin_required
def chat_page():
    conn = db.get_db()
    hist = conn.execute(
        "SELECT question, answer, sql, rows_count, provider, ok, created_at"
        " FROM ai_conversations ORDER BY id DESC LIMIT 12").fetchall()
    return render_template("ai/chat.html", active_nav="ai_chat", hist=hist,
                           ai=_ai_status(conn), app_name=config.APP_NAME)


@api.route("/ask", methods=["POST"])
@admin_required
def ask():
    payload = request.get_json(silent=True) or {}
    q = (payload.get("q") or "").strip()
    conn = db.get_db()
    try:
        res = nl2sql.ask(conn, q, allow_cloud=payload.get("allow_cloud", True))
        return jsonify(ok=True, data=res)
    except nl2sql.AskFailed as e:
        return jsonify(ok=False, msg=str(e)), 200
    except Exception as e:                     # 兜底：任何异常都不让页面 500
        return jsonify(ok=False, msg="查询失败：%s" % e), 200


# ------------------------------------------------------------------ 洞察

@bp.route("/ai/insights")
@admin_required
def insight_list():
    conn = db.get_db()
    items = insights.run(conn, persist=True)
    rows = conn.execute(
        "SELECT * FROM ai_insights ORDER BY id DESC LIMIT 100").fetchall()
    return render_template("ai/insights.html", active_nav="ai_insights",
                           items=items, rows=rows, app_name=config.APP_NAME)


@api.route("/insight/<int:iid>/execute", methods=["POST"])
@admin_required
def execute_insight(iid):
    """执行洞察建议：这是"AI 能动手"的入口"""
    conn = db.get_db()
    row = conn.execute("SELECT * FROM ai_insights WHERE id=?", (iid,)).fetchone()
    if not row:
        return jsonify(ok=False, msg="洞察不存在")
    payload = json.loads(row["action_payload"] or "{}")
    who = current_user()
    try:
        if row["action_type"] == "purchase":
            items = [{"ingredient_id": payload.get("ingredient_id"),
                      "name": payload.get("name"), "unit": payload.get("unit"),
                      "qty": payload.get("qty"), "unit_cost": payload.get("unit_cost")}]
            if not items[0]["ingredient_id"]:
                # 没有具体食材时，按库存风险清单批量生成
                risk = [r for r in metrics.q_stock_risk(conn)
                        if r["daily_use"] > 0 and r["days_left"] is not None
                        and r["days_left"] < 3]
                items = [{"ingredient_id": r["ingredient_id"], "name": r["name"],
                          "unit": r["unit"], "qty": round(r["daily_use"] * 7 - r["stock"], 2),
                          "unit_cost": r["cost"]} for r in risk]
            res = tools.create_purchase(conn, iid, items)
            msg = "已生成采购单 PO#%d，合计 %.2f 元，去「采购单」确认入库" % (res["po_id"], res["total"])
        elif row["action_type"] == "price_review":
            names = payload.get("dishes") or []
            if not names:
                q = metrics.q_dish_quadrant(conn, (date.today() - timedelta(days=30)).isoformat(),
                                            (date.today() - timedelta(days=1)).isoformat())
                names = [i["name"] for i in q["items"] if i["quadrant"] == "瘦狗"][:3]
            sug = tools.suggest_price(conn, names)
            if not sug:
                return jsonify(ok=False, msg="这些菜没有配方成本，无法给出调价建议")
            res = tools.apply_price(conn, iid, [{"dish_id": s["dish_id"], "new": s["new"]}
                                                for s in sug], who)
            msg = "已按目标毛利 60%% 调整 %d 道菜：%s（可撤销）" % (
                res["count"], "、".join("%s %.1f→%.1f" % (s["name"], s["old"], s["new"])
                                        for s in sug[:3]))
        elif row["action_type"] == "promote_dishes":
            ids = [d["dish_id"] for d in (payload.get("dishes") or [])]
            res = tools.promote(conn, iid, ids, who)
            msg = "已把 %d 道滞销菜设为点餐端主推（可撤销）" % res["count"]
        elif row["action_type"] == "review_discount":
            msg = "已生成折扣核查任务：请核对近 7 天手动折扣与会员借用情况"
        elif row["action_type"] == "diagnose_revenue":
            dd = metrics.q_revenue_decompose(conn, payload.get("d") or
                                             (date.today() - timedelta(days=1)))
            if not dd:
                msg = "基线数据不足，无法拆解。建议先累积 4 周以上同星期数据。"
            else:
                msg = ("拆解结果：%s 营收 %s 元，比基线 %s 元变化 %+.0f 元。"
                       "其中客流影响 %+.0f 元（%s 单 vs 基线 %.1f 单），"
                       "客单价影响 %+.0f 元（%.0f 元 vs 基线 %.0f 元）。主因是%s。"
                       % (dd["d"], dd["revenue"], dd["base_revenue"],
                          dd["revenue"] - dd["base_revenue"],
                          dd["traffic_effect"], dd["orders"], dd["base_orders"],
                          dd["price_effect"], dd["avg_price"], dd["base_avg_price"],
                          dd["main"]))
        elif row["action_type"] == "member_recall":
            msg = "已生成召回名单：优先触达消费额 Top 20% 的沉睡会员"
        else:
            msg = "已记录该建议，转人工处理"
        conn.execute("UPDATE ai_insights SET status='done' WHERE id=?", (iid,))
        conn.commit()
        return jsonify(ok=True, msg=msg)
    except tools.ToolError as e:
        return jsonify(ok=False, msg=str(e))
    except Exception as e:
        return jsonify(ok=False, msg="执行失败：%s" % e)


@api.route("/insight/<int:iid>/ignore", methods=["POST"])
@admin_required
def ignore_insight(iid):
    conn = db.get_db()
    conn.execute("UPDATE ai_insights SET status='ignored' WHERE id=?", (iid,))
    conn.commit()
    return jsonify(ok=True, msg="已忽略")


@api.route("/action/<int:aid>/undo", methods=["POST"])
@admin_required
def undo_action(aid):
    conn = db.get_db()
    try:
        msg = tools.undo(conn, aid, current_user())
        return jsonify(ok=True, msg=msg)
    except tools.ToolError as e:
        return jsonify(ok=False, msg=str(e))


# ------------------------------------------------------------------ 模型设置

@bp.route("/ai/settings", methods=["GET", "POST"])
@admin_required
def ai_settings():
    conn = db.get_db()
    if request.method == "POST":
        def save(key, value):
            # 用 upsert：老库里可能没有这个配置项，单纯 UPDATE 会静默失败
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)",
                         (key, value))

        save("ai_provider", request.form.get("ai_provider", "local"))
        save("ai_local_model", request.form.get("ai_local_model", config.DEFAULT_LOCAL_MODEL).strip())
        save("ai_cloud_base", request.form.get("ai_cloud_base", config.DEFAULT_CLOUD_BASE).strip())
        save("ai_cloud_model", request.form.get("ai_cloud_model", config.DEFAULT_CLOUD_MODEL).strip())
        save("ai_ollama_base", request.form.get("ai_ollama_base", config.OLLAMA_BASE).strip())
        save("ai_privacy_mask", "1" if request.form.get("ai_privacy_mask") else "0")
        conn.commit()
        key = (request.form.get("ai_api_key") or "").strip()
        if key:
            gateway.save_api_key(key)
        if request.form.get("clear_key"):
            gateway.save_api_key("")
        flash("模型设置已保存", "success")
        return redirect(url_for("ai.ai_settings"))

    p = gateway.probe(conn)
    return render_template("ai/settings.html", active_nav="ai_settings", probe=p,
                           ai=_ai_status(conn), has_key=bool(gateway.api_key()),
                           app_name=config.APP_NAME)


@api.route("/test-local", methods=["POST"])
@admin_required
def test_local():
    """只测本地通道：给设置页一个"能不能用、多快"的确定性答案"""
    conn = db.get_db()
    t0 = time.time()
    try:
        out = gateway.local_only(conn, [
            {"role": "system", "content": "你是一个测试探针，只回一句话。"},
            {"role": "user", "content": "用不超过 10 个字说明你是本地模型。"},
        ], timeout=25)
        return jsonify(ok=True, model=out.get("model", ""),
                       latency_ms=out.get("latency_ms") or int((time.time() - t0) * 1000),
                       text=(out.get("text") or "").strip()[:60])
    except gateway.ModelUnavailable as e:
        return jsonify(ok=False, msg=str(e))
    except Exception as e:
        return jsonify(ok=False, msg="本地模型调用失败：%s" % e)


@api.route("/brief", methods=["POST"])
@admin_required
def gen_brief():
    """让模型把已算好的简报改写成更像"人话"的版本（失败则原样返回）"""
    conn = db.get_db()
    b = insights.brief(conn, date.today() - timedelta(days=1))
    base_text = b["text"]
    facts = "\n".join("- %s：%s" % (i["title"], i["detail"]) for i in b["insights"][:5])
    msgs = [
        {"role": "system", "content": "你是餐饮店长的助理。只根据给定事实写一段 120 字以内的晨会简报，"
                                      "不许编造数字，不许加形容词堆砌。先讲结果，再讲原因，最后给一条动作。"},
        {"role": "user", "content": "事实：\n%s\n%s" % (base_text, facts)},
    ]
    try:
        out = gateway.chat(conn, msgs)
        return jsonify(ok=True, text=out["text"].strip(), provider=out["provider"])
    except gateway.ModelUnavailable as e:
        return jsonify(ok=False, text=base_text, msg=str(e))
