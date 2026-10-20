# -*- coding: utf-8 -*-
"""库存与报表：食材 CRUD、入库/出库流水、日/月营收、菜品销量、桌台客流"""
import sqlite3
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

import config
import db
import utils
from auth import admin_required

bp = Blueprint("reports", __name__)


# ---------- 食材库存 ----------
@bp.route("/stock")
@admin_required
def stock():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM ingredients ORDER BY id").fetchall()
    warn_count = sum(1 for r in rows if r["stock"] < r["warn_threshold"])
    # 每种食材被哪些菜品使用（配方反查），用于判断"改了库存会影响谁"
    used = {}
    for r in conn.execute(
            "SELECT r.ingredient_id, d.name FROM dish_recipes r"
            " JOIN dishes d ON d.id=r.dish_id ORDER BY d.name"):
        used.setdefault(r["ingredient_id"], []).append(r["name"])
    return render_template("admin/stock.html", ingredients=rows,
                           warn_count=warn_count, used=used)


@bp.route("/stock/add", methods=["POST"])
@admin_required
def stock_add():
    conn = db.get_db()
    name = request.form.get("name", "").strip()
    unit = request.form.get("unit", "份").strip() or "份"
    try:
        stock = float(request.form.get("stock", "0") or 0)
        warn = float(request.form.get("warn_threshold", "0") or 0)
        cost = float(request.form.get("cost", "0") or 0)
    except ValueError:
        flash("库存数量/成本必须为数字", "danger")
        return redirect(url_for("reports.stock"))
    if not name:
        flash("食材名称必填", "danger")
        return redirect(url_for("reports.stock"))
    expire = request.form.get("expire_at", "").strip() or None
    try:
        conn.execute(
            "INSERT INTO ingredients(name, unit, stock, warn_threshold, cost,"
            " expire_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (name, unit, stock, warn, utils.money(cost), expire, utils.now_str()))
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, "新增", utype, ukey, uname,
                     "食材：%s（初始 %.1f %s）" % (name, stock, unit),
                     request.remote_addr or "")
        conn.commit()
        flash("食材已添加", "success")
    except sqlite3.IntegrityError:
        flash("食材已存在", "danger")
    return redirect(url_for("reports.stock"))


@bp.route("/stock/<int:iid>/edit", methods=["POST"])
@admin_required
def stock_edit(iid):
    """修改食材的单位成本/预警值/保质期（成本是毛利计算的基础）"""
    conn = db.get_db()
    ing = conn.execute("SELECT * FROM ingredients WHERE id=?", (iid,)).fetchone()
    if not ing:
        flash("食材不存在", "danger")
        return redirect(url_for("reports.stock"))
    try:
        warn = float(request.form.get("warn_threshold", ing["warn_threshold"]) or 0)
        cost = float(request.form.get("cost", ing["cost"]) or 0)
    except ValueError:
        flash("预警值/成本必须为数字", "danger")
        return redirect(url_for("reports.stock"))
    expire = request.form.get("expire_at", "").strip() or None
    conn.execute("UPDATE ingredients SET warn_threshold=?, cost=?, expire_at=?,"
                 " updated_at=? WHERE id=?",
                 (warn, utils.money(cost), expire, utils.now_str(), iid))
    utype, ukey, uname = utils.current_user()
    utils.log_op(conn, "编辑", utype, ukey, uname,
                 "食材「%s」成本 %.2f 元/%s，预警值 %.1f，保质期 %s"
                 % (ing["name"], cost, ing["unit"], warn, expire or "未设"),
                 request.remote_addr or "")
    conn.commit()
    flash("食材已更新", "success")
    return redirect(url_for("reports.stock"))


@bp.route("/stock/<int:iid>/move", methods=["POST"])
@admin_required
def stock_move(iid):
    """入库/出库共用：type=in/out，写流水"""
    conn = db.get_db()
    ing = conn.execute("SELECT * FROM ingredients WHERE id=?", (iid,)).fetchone()
    if not ing:
        flash("食材不存在", "danger")
        return redirect(url_for("reports.stock"))
    mtype = request.form.get("type", "in")
    try:
        qty = float(request.form.get("qty", "0") or 0)
        price = float(request.form.get("price", "0") or 0)
    except ValueError:
        flash("数量/单价必须为数字", "danger")
        return redirect(url_for("reports.stock"))
    if qty <= 0:
        flash("数量必须大于 0", "danger")
        return redirect(url_for("reports.stock"))
    delta = qty if mtype == "in" else -qty
    new_stock = round(ing["stock"] + delta, 3)
    with conn:
        conn.execute("UPDATE ingredients SET stock=?, updated_at=? WHERE id=?",
                     (new_stock, utils.now_str(), iid))
        conn.execute(
            "INSERT INTO stock_movements(ingredient_id, type, qty, price, note,"
            " created_at) VALUES(?,?,?,?,?,?)",
            (iid, mtype, qty, price, request.form.get("note", "").strip(),
             utils.now_str()))
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, "库存", utype, ukey, uname,
                     "%s「%s」%.1f %s（现余 %.1f）" % (
                         "入库" if mtype == "in" else "出库", ing["name"], qty,
                         ing["unit"], new_stock),
                     request.remote_addr or "")
    if mtype == "out" and new_stock < 0:
        flash("出库后库存为负（%.1f），请核对" % new_stock, "warning")
    elif mtype == "out" and new_stock < ing["warn_threshold"]:
        flash("出库成功，库存已低于预警值！", "warning")
    else:
        flash("已%s %.1f %s" % ("入库" if mtype == "in" else "出库", qty, ing["unit"]),
              "success")
    return redirect(url_for("reports.stock"))


@bp.route("/stock/movements")
@admin_required
def stock_movements():
    conn = db.get_db()
    iid = request.args.get("ingredient_id", "").strip()
    where, params = "1=1", []
    if iid.isdigit():
        where += " AND m.ingredient_id=?"
        params.append(int(iid))
    rows = conn.execute(
        "SELECT m.*, i.name, i.unit FROM stock_movements m"
        " JOIN ingredients i ON i.id=m.ingredient_id"
        " WHERE %s ORDER BY m.id DESC LIMIT 500" % where, params).fetchall()
    ings = conn.execute("SELECT * FROM ingredients ORDER BY id").fetchall()
    return render_template("admin/stock_movements.html", movements=rows,
                           ingredients=ings, sel_iid=iid)


# ---------- 日/月营收 ----------
def daily_data(mode, date, month):
    """营收数据查询：返回 (rows, total, discounts, by_method, daily, label)"""
    conn = db.get_db()
    where, params = "o.status='settled'", []
    if mode == "month":
        where += " AND substr(o.settled_at,1,7)=?"
        params.append(month)
        label = month
    else:
        where += " AND substr(o.settled_at,1,10)=?"
        params.append(date)
        label = date
    rows = conn.execute(
        "SELECT o.* FROM orders o WHERE %s ORDER BY o.settled_at" % where,
        params).fetchall()
    total = utils.money(sum(r["final_amount"] for r in rows))
    discounts = utils.money(sum(r["discount_total"] for r in rows))
    by_method = {}
    for r in rows:
        m = r["pay_method"] or "unknown"
        by_method[m] = utils.money(by_method.get(m, 0) + r["final_amount"])
    daily = {}
    for r in rows:
        d = (r["settled_at"] or r["created_at"])[:10]
        daily[d] = [daily.get(d, [0, 0])[0] + 1,
                    utils.money(daily.get(d, [0, 0])[1] + r["final_amount"])]
    return rows, total, discounts, by_method, sorted(daily.items()), label


PAY_LABEL = {"cash": "现金", "wechat": "微信", "alipay": "支付宝",
             "balance": "会员余额", "credit": "记账", "unknown": "其他"}


@bp.route("/reports/daily")
@admin_required
def reports_daily():
    mode = request.args.get("mode", "day")
    date = request.args.get("date", "").strip() or utils.today_str()
    month = request.args.get("month", "").strip() or datetime.now().strftime("%Y-%m")
    rows, total, discounts, by_method, daily, label = daily_data(mode, date, month)
    return render_template("admin/reports_daily.html", rows=rows, mode=mode,
                           date=date, month=month, label=label, total=total,
                           discounts=discounts, by_method=by_method, daily=daily,
                           pay_label=PAY_LABEL)


@bp.route("/reports/daily/export")
@admin_required
def reports_daily_export():
    """营收报表导出 CSV（UTF-8 BOM，Excel 直接打开不乱码）"""
    import csv
    import io
    from flask import send_file
    mode = request.args.get("mode", "day")
    date = request.args.get("date", "").strip() or utils.today_str()
    month = request.args.get("month", "").strip() or datetime.now().strftime("%Y-%m")
    rows, total, discounts, by_method, daily, label = daily_data(mode, date, month)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([utils.get_setting("shop_name", "春归燕茶餐厅"), "营收报表"])
    w.writerow(["统计期间", label + ("（按日）" if mode == "day" else "（按月）")])
    w.writerow(["生成时间", utils.now_str()])
    w.writerow([])
    w.writerow(["营收合计（元）", "%.2f" % total])
    w.writerow(["结算单数", len(rows)])
    w.writerow(["优惠让利（元）", "%.2f" % discounts])
    w.writerow(["客单价（元）", "%.2f" % (total / len(rows)) if rows else "0.00"])
    w.writerow([])
    w.writerow(["【支付方式汇总】"])
    for m, v in by_method.items():
        w.writerow([PAY_LABEL.get(m, m), "%.2f" % v])
    w.writerow([])
    if mode == "month":
        w.writerow(["【每日汇总】"])
        w.writerow(["日期", "单数", "营收（元）"])
        for d, v in daily:
            w.writerow([d, v[0], "%.2f" % v[1]])
    else:
        w.writerow(["【订单明细】"])
        w.writerow(["订单号", "桌台", "结算时间", "支付方式", "实收（元）"])
        for r in rows:
            w.writerow([r["order_no"], r["table_name"],
                        r["settled_at"] or r["created_at"],
                        PAY_LABEL.get(r["pay_method"], r["pay_method"] or ""),
                        "%.2f" % r["final_amount"]])
    data = ("﻿" + buf.getvalue()).encode("utf-8")  # BOM 让 Excel 识别 UTF-8
    fname = "营收报表_%s.csv" % label.replace("-", "")
    return send_file(io.BytesIO(data), mimetype="text/csv",
                     as_attachment=True, download_name=fname)


# ---------- 菜品成本与毛利 ----------
@bp.route("/reports/profit")
@admin_required
def reports_profit():
    conn = db.get_db()
    avail = utils.dish_availability(conn)
    dishes = conn.execute(
        "SELECT d.id, d.name, d.price, d.price_mode, d.on_sale, c.name cat_name"
        " FROM dishes d JOIN categories c ON c.id=d.category_id"
        " ORDER BY c.sort_order, d.sort_order").fetchall()
    rows = []
    for d in dishes:
        a = avail.get(d["id"])
        rows.append({
            "name": d["name"], "cat": d["cat_name"], "price": d["price"],
            "on_sale": d["on_sale"], "mode": d["price_mode"],
            "cost": a["cost"] if a else None,
            "margin": a["margin"] if a else None,
            "rate": a["margin_rate"] if a else None,
            "can_make": a["can_make"] if a else None,
        })
    with_recipe = [r for r in rows if r["cost"] is not None]
    # 按 30 天实际销量估算毛利贡献
    sold = {r["name"]: r for r in conn.execute(
        "SELECT oi.name, SUM(oi.qty) qty, SUM(oi.qty*oi.price) amount"
        " FROM order_items oi JOIN orders o ON o.id=oi.order_id"
        " WHERE o.status='settled' AND oi.is_combo_child=0"
        " AND o.settled_at >= datetime('now', '-30 days')"
        " GROUP BY oi.name")}
    est = []
    for r in with_recipe:
        s = sold.get(r["name"])
        if s and s["qty"]:
            est.append({"name": r["name"], "qty": s["qty"],
                        "amount": utils.money(s["amount"]),
                        "cost": utils.money(r["cost"] * s["qty"]),
                        "profit": utils.money(s["amount"] - r["cost"] * s["qty"])})
    est.sort(key=lambda x: -x["profit"])
    total_profit = utils.money(sum(e["profit"] for e in est))
    return render_template("admin/reports_profit.html", rows=rows,
                           with_recipe=len(with_recipe), total=len(rows),
                           est=est[:50], total_profit=total_profit)


# ---------- 菜品销量 ----------
@bp.route("/reports/dishes")
@admin_required
def reports_dishes():
    conn = db.get_db()
    d1 = request.args.get("d1", "").strip() or utils.today_str()
    d2 = request.args.get("d2", "").strip() or utils.today_str()
    rows = conn.execute(
        "SELECT oi.name, SUM(oi.qty) qty, SUM(oi.qty*oi.price) amount"
        " FROM order_items oi JOIN orders o ON o.id=oi.order_id"
        " WHERE o.status='settled' AND oi.is_combo_child=0"
        " AND substr(o.settled_at,1,10) BETWEEN ? AND ?"
        " GROUP BY oi.name ORDER BY qty DESC, amount DESC LIMIT 50",
        (d1, d2)).fetchall()
    total_qty = sum(r["qty"] for r in rows)
    total_amount = utils.money(sum(r["amount"] for r in rows))
    return render_template("admin/reports_dishes.html", rows=rows, d1=d1, d2=d2,
                           total_qty=total_qty, total_amount=total_amount)


# ---------- 桌台客流 ----------
@bp.route("/reports/tables")
@admin_required
def reports_tables():
    conn = db.get_db()
    d1 = request.args.get("d1", "").strip() or utils.today_str()
    d2 = request.args.get("d2", "").strip() or utils.today_str()
    rows = conn.execute(
        "SELECT o.table_name, COUNT(*) cnt, SUM(o.persons) persons,"
        " SUM(o.final_amount) amount"
        " FROM orders o WHERE o.status='settled'"
        " AND substr(o.settled_at,1,10) BETWEEN ? AND ?"
        " GROUP BY o.table_name ORDER BY amount DESC", (d1, d2)).fetchall()
    total_tables = len(rows)
    total_amount = utils.money(sum(r["amount"] or 0 for r in rows))
    total_persons = sum(r["persons"] or 0 for r in rows)
    return render_template("admin/reports_tables.html", rows=rows, d1=d1, d2=d2,
                           total_tables=total_tables, total_amount=total_amount,
                           total_persons=total_persons)

# ---------- 按人均标准自动排菜 ----------

@bp.route("/banquet", methods=["GET", "POST"])
@admin_required
def banquet():
    """宴席排菜：输入人数与人均标准，自动生成一套建议菜单（最终由人确认）"""
    conn = db.get_db()
    tables = conn.execute(
        "SELECT * FROM tables WHERE status='idle' ORDER BY sort_order, id").fetchall()
    plan = None
    params = {"persons": request.form.get("persons", "8"),
              "per_person": request.form.get("per_person", "150"),
              "mode": request.form.get("mode", utils.get_setting("rec_mode", "balanced")),
              "exclude": request.form.getlist("exclude")}
    if request.method == "POST":
        try:
            persons = max(1, int(params["persons"] or 8))
            per_person = max(1.0, float(params["per_person"] or 150))
        except ValueError:
            flash("人数和人均标准必须是数字", "danger")
            return redirect(url_for("reports.banquet"))
        must = []
        for k in request.form.getlist("must"):
            if str(k).isdigit():
                must.append(int(k))
        plan = utils.plan_banquet(conn, persons, per_person,
                                  exclude_roles=params["exclude"],
                                  must_ids=must, mode=params["mode"])
        if not plan["courses"]:
            flash("没有匹配到可用的菜品，请先在「分类管理」给分类设置宴席角色", "warning")
    all_dishes = conn.execute(
        "SELECT d.id, d.name, c.name cat FROM dishes d JOIN categories c ON c.id=d.category_id"
        " WHERE d.on_sale=1 ORDER BY c.sort_order, d.sort_order").fetchall()
    return render_template("admin/banquet.html", plan=plan, params=params,
                           tables=tables, roles=config.COURSE_ROLES,
                           all_dishes=all_dishes)


@bp.route("/banquet/create", methods=["POST"])
@admin_required
def banquet_create():
    """把排好的菜单一键建成订单"""
    conn = db.get_db()
    tid = request.form.get("table_id", "").strip()
    kept = [k for k in request.form.getlist("dish_id") if str(k).isdigit()]
    if not tid.isdigit():
        flash("请选择桌台", "danger")
        return redirect(url_for("reports.banquet"))
    if not kept:
        flash("至少保留一道菜", "danger")
        return redirect(url_for("reports.banquet"))
    try:
        persons = max(1, int(request.form.get("persons", "1") or 1))
    except ValueError:
        persons = 1
    items = []
    for did in kept:
        d = conn.execute("SELECT * FROM dishes WHERE id=? AND on_sale=1",
                         (int(did),)).fetchone()
        if not d:
            continue
        items.append({"dish_id": d["id"], "name": d["name"],
                      "price": utils.money(d["price"]), "qty": 1, "note": "",
                      "spec_name": "", "price_pending": 1 if d["price_mode"] == "market" else 0})
    if not items:
        flash("没有有效菜品", "danger")
        return redirect(url_for("reports.banquet"))
    from blueprints.reservations_bp import create_order_from_items
    try:
        with conn:
            oid, order_no = create_order_from_items(
                conn, int(tid), items, persons, "宴席排菜（人均 %s 元）"
                % request.form.get("per_person", ""))
            utype, ukey, uname = utils.current_user()
            utils.log_op(conn, "下单", utype, ukey, uname,
                         "宴席排菜转订单 %s（%d 道菜 / %d 人）" % (order_no, len(items), persons),
                         request.remote_addr or "")
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(url_for("reports.banquet"))
    flash("已生成订单 %s" % order_no, "success")
    return redirect(url_for("orders.order_detail", oid=oid))
