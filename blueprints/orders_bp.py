# -*- coding: utf-8 -*-
"""PC 订单管理：实时订单（轮询片段）、状态流转、详情、小票、历史查询"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import config
import db
import utils
from auth import admin_required

bp = Blueprint("orders", __name__)

ACTIVE = ("pending", "making", "done")


def order_with_items(oid):
    """订单 + 明细（父行带子行）"""
    conn = db.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not order:
        return None, []
    items = conn.execute(
        "SELECT * FROM order_items WHERE order_id=? ORDER BY sort_order", (oid,)).fetchall()
    return order, items


def order_rows_query(tab):
    """实时订单页按状态页签查询（含催菜排序）"""
    conn = db.get_db()
    if tab == "pending":
        rows = conn.execute(
            "SELECT * FROM orders WHERE status='pending' ORDER BY hurry DESC, created_at").fetchall()
    elif tab == "making":
        rows = conn.execute(
            "SELECT * FROM orders WHERE status='making' ORDER BY created_at").fetchall()
    elif tab == "done":
        rows = conn.execute(
            "SELECT * FROM orders WHERE status='done' ORDER BY done_at DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status IN ('pending','making','done')"
            " ORDER BY CASE status WHEN 'pending' THEN 1 WHEN 'making' THEN 2 ELSE 3 END,"
            " hurry DESC, created_at").fetchall()
    return rows


def pending_map(conn, rows):
    """{订单id: 待定价条目数}，供列表/详情显示 ⚠️ 标记"""
    ids = [r["id"] for r in rows]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {r["order_id"]: r["c"] for r in conn.execute(
        "SELECT order_id, COUNT(*) c FROM order_items WHERE price_pending=1"
        " AND order_id IN (%s) GROUP BY order_id" % marks, ids)}


@bp.route("/orders")
@admin_required
def orders():
    tab = request.args.get("tab", "pending")
    if tab not in ("pending", "making", "done", "all"):
        tab = "pending"
    rows = order_rows_query(tab)
    return render_template("admin/orders.html", orders=rows, tab=tab,
                           pending=pending_map(db.get_db(), rows),
                           order_status_label=config.ORDER_STATUS_LABEL)


@bp.route("/partial/orders")
@admin_required
def partial_orders():
    """轮询片段：返回当前页签的订单行 HTML"""
    tab = request.args.get("tab", "pending")
    if tab not in ("pending", "making", "done", "all"):
        tab = "pending"
    rows = order_rows_query(tab)
    return render_template("partials/_order_rows.html", orders=rows, tab=tab,
                           pending=pending_map(db.get_db(), rows),
                           order_status_label=config.ORDER_STATUS_LABEL)


@bp.route("/orders/<int:oid>")
@admin_required
def order_detail(oid):
    order, items = order_with_items(oid)
    if not order:
        flash("订单不存在", "danger")
        return redirect(url_for("orders.orders"))
    return render_template("admin/order_detail.html", order=order, items=items,
                           pending=utils.pending_price_items(db.get_db(), oid),
                           order_status_label=config.ORDER_STATUS_LABEL,
                           pay_method_label=config.PAY_METHOD_LABEL)


@bp.route("/orders/<int:oid>/confirm", methods=["POST"])
@admin_required
def order_confirm(oid):
    conn = db.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not order or order["status"] != "pending":
        flash("订单不存在或状态已变更", "danger")
        return redirect(request.referrer or url_for("orders.orders"))
    with conn:
        conn.execute("DELETE FROM carts WHERE table_id=?", (order["table_id"],))
        conn.execute(
            "UPDATE orders SET status='making', hurry=0, confirmed_at=? WHERE id=?",
            (utils.now_str(), oid))
        conn.execute("UPDATE tables SET status='occupied' WHERE id=?", (order["table_id"],))
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, "状态变更", utype, ukey, uname,
                     "确认订单%s（%s）" % (order["order_no"], order["table_name"]),
                     request.remote_addr or "")
    flash("订单已确认，开始制作", "success")
    return redirect(request.referrer or url_for("orders.orders"))


@bp.route("/orders/<int:oid>/done", methods=["POST"])
@admin_required
def order_done(oid):
    conn = db.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not order or order["status"] != "making":
        flash("订单不存在或状态已变更", "danger")
        return redirect(request.referrer or url_for("orders.orders"))
    conn.execute("UPDATE orders SET status='done', done_at=? WHERE id=?",
                 (utils.now_str(), oid))
    utype, ukey, uname = utils.current_user()
    # 出餐即扣料（幂等，重复点不会扣两次）
    used = utils.consume_stock_for_order(conn, oid, order["order_no"])
    utils.log_op(conn, "状态变更", utype, ukey, uname,
                 "订单%s 出餐完成%s" % (
                     order["order_no"],
                     ("，按配方扣减 %d 项食材" % used) if used else ""),
                 request.remote_addr or "")
    conn.commit()
    flash("订单已完成，等待结账", "success")
    return redirect(request.referrer or url_for("orders.orders"))


@bp.route("/orders/<int:oid>/cancel", methods=["POST"])
@admin_required
def order_cancel(oid):
    conn = db.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not order or order["status"] != "pending":
        flash("仅待确认订单可作废", "danger")
        return redirect(request.referrer or url_for("orders.orders"))
    conn.execute("UPDATE orders SET status='cancelled', cancelled_at=? WHERE id=?",
                 (utils.now_str(), oid))
    utype, ukey, uname = utils.current_user()
    utils.log_op(conn, "状态变更", utype, ukey, uname,
                 "作废订单%s" % order["order_no"], request.remote_addr or "")
    conn.commit()
    flash("订单已作废", "success")
    return redirect(request.referrer or url_for("orders.orders"))


@bp.route("/orders/<int:oid>/receipt")
@admin_required
def order_receipt(oid):
    """下单小票 / 结算小票（?type=settle）"""
    order, items = order_with_items(oid)
    if not order:
        flash("订单不存在", "danger")
        return redirect(url_for("orders.orders"))
    rtype = request.args.get("type", "order")
    if rtype == "settle" and order["status"] != "settled":
        flash("订单尚未结算", "danger")
        return redirect(url_for("orders.order_detail", oid=oid))
    return render_template("receipt.html", order=order, items=items,
                           s=utils.get_settings(), rtype=rtype,
                           autoprint=request.args.get("autoprint") == "1",
                           pay_method_label=config.PAY_METHOD_LABEL)


@bp.route("/orders/history")
@admin_required
def orders_history():
    conn = db.get_db()
    tables = conn.execute("SELECT id, name FROM tables ORDER BY sort_order, id").fetchall()
    table_id = request.args.get("table_id", "").strip()
    status = request.args.get("status", "").strip()
    d1 = request.args.get("d1", "").strip()
    d2 = request.args.get("d2", "").strip()
    where, params = "1=1", []
    if table_id.isdigit():
        where += " AND o.table_id=?"
        params.append(int(table_id))
    if status in config.ORDER_STATUS:
        where += " AND o.status=?"
        params.append(status)
    if d1:
        where += " AND o.created_at >= ?"
        params.append(d1 + " 00:00:00")
    if d2:
        where += " AND o.created_at <= ?"
        params.append(d2 + " 23:59:59")
    rows = conn.execute(
        "SELECT o.* FROM orders o WHERE %s ORDER BY o.id DESC LIMIT 500" % where,
        params).fetchall()
    return render_template("admin/orders_history.html", orders=rows, tables=tables,
                           order_status_label=config.ORDER_STATUS_LABEL,
                           pay_method_label=config.PAY_METHOD_LABEL,
                           table_id=table_id, status=status, d1=d1, d2=d2)
