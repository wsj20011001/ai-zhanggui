# -*- coding: utf-8 -*-
"""数据分析页：经营总览 / 菜品工程 / 客流时段 / 预测备货 / 会员资产"""
from datetime import date, timedelta

from flask import Blueprint, render_template, request

import config
import db
from analytics import insights, metrics
from auth import admin_required
from utils import get_setting, money

bp = Blueprint("analytics", __name__)


def _window(days=30):
    end = date.today() - timedelta(days=1)
    return (end - timedelta(days=days - 1)), end


@bp.route("/analytics/overview")
@admin_required
def overview():
    conn = db.get_db()
    days = int(request.args.get("days", 30) or 30)
    d1, d2 = _window(days)
    k = metrics.q_kpi(conn, d1, d2)
    p1, p2 = d1 - timedelta(days=days), d1 - timedelta(days=1)
    prev = metrics.q_kpi(conn, p1, p2)
    trend = metrics.q_trend(conn, days, end=d2)
    cats = conn.execute(
        "SELECT category, SUM(qty) qty, ROUND(SUM(amount),2) amount,"
        " ROUND(SUM(profit),2) profit FROM v_category_sales"
        " WHERE d BETWEEN ? AND ? GROUP BY category ORDER BY amount DESC",
        (d1.isoformat(), d2.isoformat())).fetchall()
    cats = [dict(r) for r in cats]
    pays = conn.execute(
        "SELECT pay_method, SUM(orders) orders, ROUND(SUM(amount),2) amount"
        " FROM v_pay_daily WHERE d BETWEEN ? AND ? GROUP BY pay_method ORDER BY amount DESC",
        (d1.isoformat(), d2.isoformat())).fetchall()
    pays = [dict(r) for r in pays]
    hours = metrics.q_hour_profile(conn, days)
    seats = int(get_setting("shop_seats", "42") or 42)
    turnover = round(k["orders"] / (seats * days), 2) if seats and days else 0

    def delta(a, b):
        return round((a - b) / b, 4) if b else 0.0

    return render_template(
        "analytics/overview.html", active_nav="ana_overview", days=days,
        k=k, prev=prev, trend=trend, cats=cats, pays=pays, hours=hours,
        turnover=turnover, seats=seats,
        d_revenue=delta(k["revenue"], prev["revenue"]),
        d_orders=delta(k["orders"], prev["orders"]),
        d_avg=delta(k["avg_price"], prev["avg_price"]),
        d_margin=round(k["margin_rate"] - prev["margin_rate"], 4),
        pay_label=config.PAY_METHOD_LABEL, app_name=config.APP_NAME)


@bp.route("/analytics/dishes")
@admin_required
def dishes():
    conn = db.get_db()
    days = int(request.args.get("days", 30) or 30)
    d1, d2 = _window(days)
    q = metrics.q_dish_quadrant(conn, d1.isoformat(), d2.isoformat())
    slow = metrics.q_slow_dishes(conn)
    # ABC 分析：累计销量 70/20/10
    items = sorted(q["items"], key=lambda x: -x["qty"])
    total_qty = sum(i["qty"] for i in items) or 1
    acc, cls = 0, {}
    for i in items:
        acc += i["qty"]
        i["share"] = round(acc / total_qty, 4)
        i["abc"] = "A" if acc / total_qty <= 0.7 else ("B" if acc / total_qty <= 0.9 else "C")
        cls[i["abc"]] = cls.get(i["abc"], 0) + 1
    no_recipe = conn.execute(
        "SELECT COUNT(*) c FROM dishes d WHERE d.on_sale=1 AND d.id NOT IN"
        " (SELECT DISTINCT dish_id FROM dish_recipes)").fetchone()["c"]
    return render_template("analytics/dishes.html", active_nav="ana_dishes", days=days,
                           q=q, slow=slow, abc=cls, no_recipe=no_recipe,
                           top=[i for i in items[:15]], app_name=config.APP_NAME)


@bp.route("/analytics/traffic")
@admin_required
def traffic():
    conn = db.get_db()
    days = int(request.args.get("days", 30) or 30)
    d1, d2 = _window(days)
    hours = metrics.q_hour_profile(conn, days)
    dow = conn.execute(
        "SELECT weekday, ROUND(AVG(revenue),2) avg_revenue, ROUND(AVG(orders),1) avg_orders"
        " FROM v_sales_daily WHERE d BETWEEN ? AND ? GROUP BY weekday",
        (d1.isoformat(), d2.isoformat())).fetchall()
    order = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    dow = sorted([dict(r) for r in dow],
                 key=lambda r: order.index(r["weekday"]) if r["weekday"] in order else 9)
    seats = int(get_setting("shop_seats", "42") or 42)
    daily = conn.execute(
        "SELECT d, orders, persons, revenue, ROUND(orders*1.0/?, 2) turnover"
        " FROM v_sales_daily WHERE d BETWEEN ? AND ? ORDER BY d",
        (seats, d1.isoformat(), d2.isoformat())).fetchall()
    return render_template("analytics/traffic.html", active_nav="ana_traffic",
                           days=days, hours=hours, dow=dow, daily=daily,
                           seats=seats, app_name=config.APP_NAME)


@bp.route("/analytics/forecast")
@admin_required
def forecast():
    conn = db.get_db()
    fc = metrics.q_forecast_revenue(conn, horizon=config.FORECAST_HORIZON)
    hist = metrics.q_trend(conn, 30, end=date.today() - timedelta(days=1))
    need = metrics.q_consumption_need(conn, days=config.FORECAST_HORIZON)
    risk = {r["ingredient_id"]: r for r in metrics.q_stock_risk(conn)}
    plan = []
    for n in need:
        r = risk.get(n["ingredient_id"])
        if not r:
            continue
        qty = round(n["need"] - r["stock"], 2)
        if qty <= 0:
            continue
        plan.append({**n, "days_left": r["days_left"], "qty": qty,
                     "amount": money(qty * (r["cost"] or 0))})
    plan.sort(key=lambda x: -(x["days_left"] or 99))
    plan = sorted(plan, key=lambda x: (x["days_left"] if x["days_left"] is not None else 99))
    total = money(sum(p["amount"] for p in plan))
    return render_template("analytics/forecast.html", active_nav="ana_forecast",
                           fc=fc, hist=hist, plan=plan, total=total,
                           horizon=config.FORECAST_HORIZON, app_name=config.APP_NAME)


@bp.route("/analytics/members")
@admin_required
def members():
    conn = db.get_db()
    s = metrics.q_member_summary(conn)
    top = [dict(r) for r in conn.execute(
        "SELECT name, level, balance, total_spend, visit_count, last_visit_at"
        " FROM v_member_rank ORDER BY total_spend DESC LIMIT 20")]
    levels = conn.execute(
        "SELECT level, COUNT(*) c, ROUND(SUM(total_spend),2) amount FROM members"
        " GROUP BY level ORDER BY amount DESC").fetchall()
    levels = [dict(r) for r in levels]
    lost = conn.execute(
        "SELECT COUNT(*) c FROM members WHERE last_visit_at IS NOT NULL"
        " AND substr(last_visit_at,1,10) < ?",
        ((date.today() - timedelta(days=60)).isoformat(),)).fetchone()["c"]
    return render_template("analytics/members.html", active_nav="ana_members",
                           s=s, top=top, levels=levels, lost=lost,
                           app_name=config.APP_NAME)
