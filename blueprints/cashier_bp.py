# -*- coding: utf-8 -*-
"""收银结算：桌台未结订单、会员折扣/积分抵扣、支付方式、整桌/单笔结算"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import config
import db
import utils
from auth import admin_required

bp = Blueprint("cashier", __name__)

UNPAID = ("pending", "making", "done")


def unpaid_orders(table_id=None):
    conn = db.get_db()
    if table_id:
        return conn.execute(
            "SELECT * FROM orders WHERE status IN ('pending','making','done')"
            " AND table_id=? ORDER BY id", (table_id,)).fetchall()
    return conn.execute(
        "SELECT * FROM orders WHERE status IN ('pending','making','done')"
        " ORDER BY table_id, id").fetchall()


def settle_order(conn, order, pay_method, member, points_to_use, use_member_discount):
    """结算单笔订单（调用方须包在事务 with conn: 中）。
    返回 (final_amount, points_earned, points_actually_used, points_discount, member_discount, reduction)"""
    # 硬拦截：含未定价的时价菜，一律不允许结算
    if utils.order_has_pending_price(conn, order["id"]):
        raise ValueError("订单 %s 含未定价的时价菜，请先补价再结算" % order["order_no"])
    # 兜底：跳过"出餐完成"直接结算的单也要扣料（幂等）
    utils.consume_stock_for_order(conn, order["id"], order["order_no"])
    items = conn.execute(
        "SELECT * FROM order_items WHERE order_id=? AND is_combo_child=0",
        (order["id"],)).fetchall()
    calc = utils.calc_order(
        [{"price": i["price"], "qty": i["qty"]} for i in items],
        member_discount_rate=1.0,
        points_to_use=points_to_use if member else 0,
        use_member=use_member_discount and member is not None)
    now = utils.now_str()
    conn.execute(
        "UPDATE orders SET status='settled', settled_at=?, pay_method=?,"
        " reduction=?, member_discount=?, points_discount=?,"
        " discount_total=?, final_amount=?, member_id=?, member_name=?,"
        " points_earned=?, points_used=?, hurry=0 WHERE id=?",
        (now, pay_method, calc["reduction"], calc["member_discount"],
         calc["points_discount"], calc["discount_total"], calc["final"],
         member["id"] if member else None, member["name"] if member else "",
         calc["points_earned"], calc["points_actually_used"], order["id"]))
    if member:
        balance_used = calc["final"] if pay_method == "balance" else 0.0
        if balance_used > 0 and member["balance"] < balance_used:
            raise ValueError("会员余额不足（余额 %.2f 元）" % member["balance"])
        points_delta = calc["points_earned"] - calc["points_actually_used"]
        conn.execute(
            "UPDATE members SET balance=ROUND(balance-?,2), points=points+?,"
            " total_spend=ROUND(total_spend+?,2), last_visit_at=? WHERE id=?",
            (balance_used, points_delta, calc["final"], now, member["id"]))
        # amount        = 本次消费金额（业务口径，负号表示支出）
        # balance_change= 真实余额变动：只有"余额支付"才动余额，现金/微信等为 0
        # 旧实现只记 amount，导致现金支付时"流水扣了钱、余额却没变"，无法对账
        conn.execute(
            "INSERT INTO member_ledger(member_id, type, amount, balance_change,"
            " points_change, order_id, note, created_at)"
            " VALUES(?, 'consume', ?, ?, ?, ?, ?, ?)",
            (member["id"], -calc["final"], -balance_used, points_delta,
             order["id"], "消费 订单%s" % order["order_no"], now))
    return calc


def reset_table_if_free(conn, table_id):
    """桌台无未结订单且购物车空 → 置空闲"""
    n = conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE table_id=? AND status IN"
        " ('pending','making','done')", (table_id,)).fetchone()["c"]
    cart = conn.execute(
        "SELECT COUNT(*) c FROM carts WHERE table_id=?", (table_id,)).fetchone()["c"]
    if n == 0 and cart == 0:
        conn.execute("UPDATE tables SET status='idle' WHERE id=?", (table_id,))


@bp.route("/cashier")
@admin_required
def cashier():
    conn = db.get_db()
    tables = conn.execute("SELECT * FROM tables ORDER BY sort_order, id").fetchall()
    counts, amounts, pending = {}, {}, {}
    for o in unpaid_orders():
        counts[o["table_id"]] = counts.get(o["table_id"], 0) + 1
        amounts[o["table_id"]] = round(amounts.get(o["table_id"], 0) + o["final_amount"], 2)
        if utils.order_has_pending_price(conn, o["id"]):
            pending[o["table_id"]] = pending.get(o["table_id"], 0) + 1
    return render_template("admin/cashier.html", tables=tables,
                           table_status_label=config.TABLE_STATUS_LABEL,
                           counts=counts, amounts=amounts, pending=pending)


@bp.route("/cashier/<int:table_id>", methods=["GET", "POST"])
@admin_required
def cashier_settle(table_id):
    conn = db.get_db()
    table = conn.execute("SELECT * FROM tables WHERE id=?", (table_id,)).fetchone()
    if not table:
        flash("桌台不存在", "danger")
        return redirect(url_for("cashier.cashier"))
    orders = unpaid_orders(table_id)
    members = conn.execute("SELECT * FROM members ORDER BY id").fetchall()
    if not orders:
        flash("该桌台暂无未结订单", "warning")
        return redirect(url_for("cashier.cashier"))
    # 金额预览（无会员时按满减计算）
    preview, pending_total = [], 0
    for o in orders:
        items = conn.execute(
            "SELECT * FROM order_items WHERE order_id=? AND is_combo_child=0",
            (o["id"],)).fetchall()
        calc = utils.calc_order([{"price": i["price"], "qty": i["qty"]} for i in items])
        pend = utils.pending_price_items(conn, o["id"])
        pending_total += len(pend)
        preview.append({"order": o, "calc": calc, "pending": pend})
    preview_total = utils.money(sum(p["calc"]["final"] for p in preview))
    # 原价合计（未打折），用于结算时的"异常低价"预警
    preview_origin = utils.money(sum(
        sum(i["price"] * i["qty"] for i in conn.execute(
            "SELECT price, qty FROM order_items WHERE order_id=? AND is_combo_child=0",
            (o["id"],)).fetchall()) for o in orders))
    return render_template("admin/cashier_settle.html", table=table, orders=orders,
                           members=members, preview=preview,
                           preview_total=preview_total, preview_origin=preview_origin,
                           pending_total=pending_total,
                           low_ratio=config.LOW_PRICE_RATIO,
                           order_status_label=config.ORDER_STATUS_LABEL,
                           pay_methods=config.PAY_METHODS,
                           pay_method_label=config.PAY_METHOD_LABEL)


@bp.route("/orders/<int:oid>/settle", methods=["POST"])
@admin_required
def order_settle(oid):
    conn = db.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    # 必须白名单校验：旧实现只挡 settled，实测把已撤回(cancelled)的订单
    # 直接改成 settled 并计入营收
    if not order or order["status"] not in config.SETTLEABLE_STATUS:
        flash("订单不存在或状态不可结算（已结算/已撤回的订单不能结算）", "danger")
        return redirect(request.referrer or url_for("cashier.cashier"))
    if utils.order_has_pending_price(conn, oid):
        flash("订单 %s 含未定价的时价菜，请先补价" % order["order_no"], "warning")
        return redirect(url_for("cashier.order_price", oid=oid))
    pay_method = request.form.get("pay_method", "cash")
    if pay_method not in config.PAY_METHODS:
        pay_method = "cash"
    member = None
    mid = request.form.get("member_id", "").strip()
    if mid.isdigit():
        member = conn.execute("SELECT * FROM members WHERE id=?", (int(mid),)).fetchone()
    try:
        pts = int(request.form.get("points_use", "0") or 0)
    except (TypeError, ValueError):
        pts = 0
    if pts < 0:
        flash("积分不能为负", "danger")
        return redirect(request.referrer or url_for("cashier.cashier"))
    use_discount = 1 if request.form.get("use_member_discount") else 0
    if member and pts > member["points"]:
        flash("积分不足（当前 %d 分）" % member["points"], "danger")
        return redirect(request.referrer or url_for("cashier.cashier"))
    try:
        with conn:
            calc = settle_order(conn, order, pay_method, member, pts, use_discount)
            reset_table_if_free(conn, order["table_id"])
            utype, ukey, uname = utils.current_user()
            utils.log_op(conn, "结算", utype, ukey, uname,
                         "订单%s（%s）实收%.2f元 支付%s%s" % (
                             order["order_no"], order["table_name"], calc["final"],
                             config.PAY_METHOD_LABEL.get(pay_method, pay_method),
                             _low_note(conn, order, calc)),
                         request.remote_addr or "")
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(request.referrer or url_for("cashier.cashier"))
    flash("订单 %s 已结算，实收 %.2f 元" % (order["order_no"], calc["final"]), "success")
    return redirect(url_for("orders.order_receipt", oid=oid, type="settle", autoprint=1))


@bp.route("/cashier/<int:table_id>/settle_all", methods=["POST"])
@admin_required
def settle_all(table_id):
    conn = db.get_db()
    orders = unpaid_orders(table_id)
    if not orders:
        flash("该桌台暂无未结订单", "warning")
        return redirect(url_for("cashier.cashier"))
    stuck = [o for o in orders if utils.order_has_pending_price(conn, o["id"])]
    if stuck:
        flash("该桌台有 %d 笔订单含未定价的时价菜，请先逐单补价" % len(stuck), "warning")
        return redirect(url_for("cashier.cashier_settle", table_id=table_id))
    pay_method = request.form.get("pay_method", "cash")
    if pay_method not in config.PAY_METHODS:
        pay_method = "cash"
    member = None
    mid = request.form.get("member_id", "").strip()
    if mid.isdigit():
        member = conn.execute("SELECT * FROM members WHERE id=?", (int(mid),)).fetchone()
    try:
        pts = int(request.form.get("points_use", "0") or 0)
    except (TypeError, ValueError):
        pts = 0
    if pts < 0:
        flash("积分不能为负", "danger")
        return redirect(url_for("cashier.cashier_settle", table_id=table_id))
    use_discount = 1 if request.form.get("use_member_discount") else 0
    if member and pts > member["points"]:
        flash("积分不足（当前 %d 分）" % member["points"], "danger")
        return redirect(url_for("cashier.cashier_settle", table_id=table_id))
    # 多单分摊积分：按"当前真实应付"比例分配。
    # 不能用 orders.final_amount —— 那是下单时的快照（未含满减/会员折扣），
    # 用它做权重会把积分分摊到错误的订单上。
    weights = []
    for o in orders:
        its = conn.execute(
            "SELECT price, qty FROM order_items WHERE order_id=? AND is_combo_child=0",
            (o["id"],)).fetchall()
        weights.append(utils.calc_order(
            [{"price": i["price"], "qty": i["qty"]} for i in its])["final"])
    total_w = sum(weights) or 1
    assigned = []
    for o, w in zip(orders, weights):
        share = int(pts * w / total_w) if pts else 0
        assigned.append((o, share))
    assigned[-1] = (assigned[-1][0], assigned[-1][1] + (pts - sum(a[1] for a in assigned)))
    try:
        with conn:
            totals = [settle_order(conn, o, pay_method, member, share, use_discount)
                      for o, share in assigned]
            reset_table_if_free(conn, table_id)
            total = utils.money(sum(t["final"] for t in totals))
            utype, ukey, uname = utils.current_user()
            utils.log_op(conn, "结算", utype, ukey, uname,
                         "整桌结算%s 共%d笔 实收%.2f元 支付%s" % (
                             orders[0]["table_name"], len(orders), total,
                             config.PAY_METHOD_LABEL.get(pay_method, pay_method)),
                         request.remote_addr or "")
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(url_for("cashier.cashier_settle", table_id=table_id))
    flash("整桌结算完成，共 %d 笔，实收 %.2f 元" % (len(orders), total), "success")
    return redirect(url_for("cashier.cashier"))


def _low_note(conn, order, calc):
    """实收显著低于原价时，在日志里留下警示标记"""
    items = conn.execute(
        "SELECT price, qty FROM order_items WHERE order_id=? AND is_combo_child=0",
        (order["id"],)).fetchall()
    origin = sum(i["price"] * i["qty"] for i in items)
    if origin > 0 and calc["final"] < origin * config.LOW_PRICE_RATIO:
        return "（⚠️ 实收仅为原价 %.0f%%，请核对）" % (calc["final"] / origin * 100)
    return ""


@bp.route("/orders/<int:oid>/price", methods=["GET", "POST"])
@admin_required
def order_price(oid):
    """时价补价：列出待定价条目，逐项填价后重算订单金额"""
    conn = db.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not order:
        flash("订单不存在", "danger")
        return redirect(url_for("cashier.cashier"))
    if order["status"] in ("settled", "cancelled"):
        flash("已结算/已撤回的订单不能补价", "danger")
        return redirect(url_for("orders.order_detail", oid=oid))
    items = utils.pending_price_items(conn, oid)
    if not items:
        flash("该订单没有待定价的时价菜", "success")
        return redirect(url_for("cashier.cashier_settle", table_id=order["table_id"]))
    if request.method == "POST":
        utype, ukey, uname = utils.current_user()
        changed = []
        try:
            with conn:
                for it in items:
                    raw = request.form.get("price_%d" % it["id"], "").strip()
                    if raw == "":
                        raise ValueError("「%s」还没有填价格" % it["name"])
                    val = utils.money(raw)
                    if val <= 0:
                        raise ValueError("「%s」的价格必须大于 0" % it["name"])
                    conn.execute(
                        "UPDATE order_items SET price=?, price_pending=0,"
                        " price_set_by=?, price_set_at=? WHERE id=?",
                        (val, uname or ukey, utils.now_str(), it["id"]))
                    changed.append("%s %.2f元" % (it["name"], val))
                calc = utils.recalc_order_amounts(conn, oid)
                utils.log_op(conn, "定价", utype, ukey, uname,
                             "时价补价 订单%s：%s（补价后合计 %.2f元）" % (
                                 order["order_no"], "；".join(changed), calc["total"]),
                             request.remote_addr or "")
        except (ValueError, TypeError) as e:
            flash(str(e), "danger")
            return redirect(url_for("cashier.order_price", oid=oid))
        flash("补价完成，订单合计已更新为 %.2f 元" % calc["total"], "success")
        return redirect(url_for("cashier.cashier_settle", table_id=order["table_id"]))
    priced = conn.execute(
        "SELECT * FROM order_items WHERE order_id=? AND is_combo_child=0"
        " AND price_pending=0 ORDER BY sort_order", (oid,)).fetchall()
    return render_template("admin/order_pricing.html", order=order, items=items,
                           priced=priced,
                           table_row=conn.execute(
                               "SELECT * FROM tables WHERE id=?",
                               (order["table_id"],)).fetchone())
