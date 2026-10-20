# -*- coding: utf-8 -*-
"""预订管理：新增/编辑、到店核销、取消、提醒（惰性扫描）、历史"""
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

import db
import utils
from auth import admin_required

bp = Blueprint("reservations", __name__)


@bp.route("/reservations")
@admin_required
def reservations():
    conn = db.get_db()
    remind, expired = utils.check_reservations(conn)
    conn.commit()
    scope = request.args.get("scope", "today")
    rows = conn.execute(
        "SELECT * FROM reservations ORDER BY reserve_time").fetchall()
    if scope == "today":
        today = utils.today_str()
        rows = [r for r in rows if r["reserve_time"].startswith(today)]
    now = datetime.now()
    marked = []
    for r in rows:
        r = dict(r)
        try:
            t = datetime.strptime(r["reserve_time"], "%Y-%m-%d %H:%M")
            r["_expired"] = (t - now).total_seconds() < -3600
        except ValueError:
            r["_expired"] = False
        marked.append(r)
    rows = marked
    tables = conn.execute("SELECT * FROM tables ORDER BY sort_order, id").fetchall()
    # 到店提醒卡数据：会员画像 + 预点菜
    cards = {}
    for r in rows:
        items = conn.execute(
            "SELECT name, qty, spec_name, price FROM reservation_items"
            " WHERE reservation_id=? ORDER BY sort_order, id", (r["id"],)).fetchall()
        prof = None
        if r.get("member_id"):
            m = conn.execute("SELECT * FROM members WHERE id=?",
                             (r["member_id"],)).fetchone()
            if m:
                fav = conn.execute(
                    "SELECT oi.name, SUM(oi.qty) q FROM order_items oi"
                    " JOIN orders o ON o.id=oi.order_id"
                    " WHERE o.member_id=? AND o.status='settled' AND oi.is_combo_child=0"
                    " GROUP BY oi.name ORDER BY q DESC LIMIT 3", (m["id"],)).fetchall()
                last = conn.execute(
                    "SELECT MAX(settled_at) t FROM orders"
                    " WHERE member_id=? AND status='settled'", (m["id"],)).fetchone()
                prof = {"name": m["name"], "phone": m["phone"],
                        "balance": m["balance"], "points": m["points"],
                        "level": m["level"], "total_spend": m["total_spend"],
                        "last": (last["t"] or "")[:10], "fav": [dict(f) for f in fav]}
        cards[r["id"]] = {"items": [dict(i) for i in items], "profile": prof}
    return render_template("admin/reservations.html", reservations=rows,
                           tables=tables, scope=scope, cards=cards,
                           remind_ids={r["id"] for r in remind},
                           expired_ids={r["id"] for r in expired})


def parse_time(field):
    """校验预订时间格式 YYYY-MM-DD HH:MM，非法抛 ValueError。
    兼容浏览器 datetime-local 的 T 分隔写法"""
    s = field.strip().replace("T", " ")
    try:
        datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("时间格式应为 年-月-日 时:分，例如 2026-08-16 18:30")
    return s


@bp.route("/reservations/add", methods=["GET", "POST"])
@admin_required
def reservation_add():
    conn = db.get_db()
    tables = conn.execute("SELECT * FROM tables ORDER BY sort_order, id").fetchall()
    members = conn.execute("SELECT id, name, phone FROM members ORDER BY id").fetchall()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        try:
            rtime = parse_time(request.form.get("reserve_time", ""))
            persons = max(1, int(request.form.get("persons", "1") or 1))
        except ValueError as e:
            flash(str(e), "danger")
            return render_template("admin/reservation_form.html", tables=tables,
                                   members=members, reservation=None, data=request.form)
        if not name:
            flash("预订人姓名必填", "danger")
            return render_template("admin/reservation_form.html", tables=tables,
                                   members=members, reservation=None, data=request.form)
        tid = request.form.get("table_id", "").strip()
        table_name = ""
        if tid.isdigit():
            t = conn.execute("SELECT name FROM tables WHERE id=?", (int(tid),)).fetchone()
            table_name = t["name"] if t else ""
        _mid = request.form.get("member_id", "").strip()
        conn.execute(
            "INSERT INTO reservations(name, phone, reserve_time, persons, table_id,"
            " table_name, member_id, remark, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (name, request.form.get("phone", "").strip(), rtime, persons,
             int(tid) if tid.isdigit() else None, table_name,
             int(_mid) if _mid.isdigit() else None,
             request.form.get("remark", "").strip(), utils.now_str()))
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, "新增", utype, ukey, uname,
                     "预订：%s %s（%d人%s）" % (name, rtime, persons,
                                          ("，" + table_name) if table_name else ""),
                     request.remote_addr or "")
        conn.commit()
        flash("预订已登记", "success")
        return redirect(url_for("reservations.reservations"))
    return render_template("admin/reservation_form.html", tables=tables,
                           members=members, reservation=None, data={})


@bp.route("/reservations/<int:rid>/edit", methods=["GET", "POST"])
@admin_required
def reservation_edit(rid):
    conn = db.get_db()
    res = conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
    if not res:
        flash("预订不存在", "danger")
        return redirect(url_for("reservations.reservations"))
    tables = conn.execute("SELECT * FROM tables ORDER BY sort_order, id").fetchall()
    members = conn.execute("SELECT id, name, phone FROM members ORDER BY id").fetchall()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        try:
            rtime = parse_time(request.form.get("reserve_time", ""))
            persons = max(1, int(request.form.get("persons", "1") or 1))
        except ValueError as e:
            flash(str(e), "danger")
            return render_template("admin/reservation_form.html", tables=tables,
                                   members=members, reservation=res, data=request.form)
        if not name:
            flash("预订人姓名必填", "danger")
            return render_template("admin/reservation_form.html", tables=tables,
                                   members=members, reservation=res, data=request.form)
        tid = request.form.get("table_id", "").strip()
        table_name = ""
        if tid.isdigit():
            t = conn.execute("SELECT name FROM tables WHERE id=?", (int(tid),)).fetchone()
            table_name = t["name"] if t else ""
        _mid = request.form.get("member_id", "").strip()
        conn.execute(
            "UPDATE reservations SET name=?, phone=?, reserve_time=?, persons=?,"
            " table_id=?, table_name=?, member_id=?, remark=? WHERE id=?",
            (name, request.form.get("phone", "").strip(), rtime, persons,
             int(tid) if tid.isdigit() else None, table_name,
             int(_mid) if _mid.isdigit() else None,
             request.form.get("remark", "").strip(), rid))
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, "编辑", utype, ukey, uname,
                     "预订：%s %s" % (name, rtime), request.remote_addr or "")
        conn.commit()
        flash("预订已保存", "success")
        return redirect(url_for("reservations.reservations"))
    return render_template("admin/reservation_form.html", tables=tables,
                           members=members, reservation=res, data={})


@bp.route("/reservations/<int:rid>/arrive", methods=["POST"])
@admin_required
def reservation_arrive(rid):
    conn = db.get_db()
    res = conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
    if not res or res["status"] != "pending":
        flash("预订不存在或已处理", "danger")
        return redirect(url_for("reservations.reservations"))
    conn.execute("UPDATE reservations SET status='arrived', arrived_at=? WHERE id=?",
                 (utils.now_str(), rid))
    if res["table_id"]:
        conn.execute("UPDATE tables SET status='occupied' WHERE id=?", (res["table_id"],))
    utype, ukey, uname = utils.current_user()
    utils.log_op(conn, "状态变更", utype, ukey, uname,
                 "预订到店核销：%s（%s）" % (res["name"], res["reserve_time"]),
                 request.remote_addr or "")
    conn.commit()
    flash("预订已核销" + ("，桌台 %s 已标记占用" % res["table_name"] if res["table_name"] else ""),
          "success")
    return redirect(url_for("reservations.reservations"))


@bp.route("/reservations/<int:rid>/cancel", methods=["POST"])
@admin_required
def reservation_cancel(rid):
    conn = db.get_db()
    res = conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
    if not res or res["status"] != "pending":
        flash("预订不存在或已处理", "danger")
        return redirect(url_for("reservations.reservations"))
    conn.execute("UPDATE reservations SET status='cancelled' WHERE id=?", (rid,))
    utype, ukey, uname = utils.current_user()
    utils.log_op(conn, "状态变更", utype, ukey, uname,
                 "取消预订：%s（%s）" % (res["name"], res["reserve_time"]),
                 request.remote_addr or "")
    conn.commit()
    flash("预订已取消", "success")
    return redirect(url_for("reservations.reservations"))

# ---------- 预订预点菜 ----------

def create_order_from_items(conn, table_id, items, persons=1, remark=""):
    """按给定明细直接建单（预订转订单 / 宴席排菜共用）。

    服务端在这里统一把关，所有调用方一并受益：
      · 菜品必须存在且**仍在售** —— 防止用陈旧页面/预点菜把已下架的菜下单
      · 单价一律以**数据库当前价**为准，不信任调用方传入的价格
      · **时价菜自动标记待定价**，否则会被按 0 元结算（漏收）
      · 套餐自动展开子项，保证扣料与厨房票据一致
    """
    table = conn.execute("SELECT * FROM tables WHERE id=?", (table_id,)).fetchone()
    if not table:
        raise ValueError("桌台不存在")
    safe = []
    for it in items:
        did = it.get("dish_id")
        if not did:
            continue
        d = conn.execute("SELECT * FROM dishes WHERE id=?", (did,)).fetchone()
        if not d:
            raise ValueError("有菜品已不存在，请刷新后重试")
        if not d["on_sale"]:
            raise ValueError("菜品「%s」已下架，无法转成订单，请先移除" % d["name"])
        try:
            qty = max(1, int(it.get("qty") or 1))
        except (TypeError, ValueError):
            qty = 1
        safe.append({
            "dish_id": d["id"], "name": d["name"],
            "price": utils.money(d["price"]),
            "price_pending": 1 if d["price_mode"] == "market" else 0,
            "qty": qty, "note": (it.get("note") or "")[:100],
            "spec_name": it.get("spec_name") or "",
            "is_combo": d["is_combo"],
        })
    if not safe:
        raise ValueError("没有可下单的菜品")
    order_no = utils.make_order_no()
    calc = utils.calc_order([{"price": i["price"], "qty": i["qty"]} for i in safe])
    oid = conn.execute(
        "INSERT INTO orders(order_no, table_id, table_name, persons, status,"
        " total_amount, final_amount, remark, created_at)"
        " VALUES(?,?,?,?,'pending',?,?,?,?)",
        (order_no, table_id, table["name"], persons, calc["total"], calc["final"],
         remark, utils.now_str())).lastrowid
    sort = 0
    for it in safe:
        sort += 1
        pid = conn.execute(
            "INSERT INTO order_items(order_id, dish_id, name, price, price_pending,"
            " spec_name, spec_delta, qty, note, is_combo_child, sort_order)"
            " VALUES(?,?,?,?,?,?,0,?,?,0,?)",
            (oid, it["dish_id"], it["name"], it["price"], it["price_pending"],
             it["spec_name"], it["qty"], it["note"], sort)).lastrowid
        if it["is_combo"]:                     # 套餐展开子项（不计价，仅用于出餐与扣料）
            for ch in conn.execute(
                    "SELECT c.id, c.name, c.price, ci.qty FROM combo_items ci"
                    " JOIN dishes c ON c.id=ci.dish_id WHERE ci.combo_id=?", (it["dish_id"],)):
                sort += 1
                conn.execute(
                    "INSERT INTO order_items(order_id, dish_id, name, price,"
                    " price_pending, spec_name, spec_delta, qty, note,"
                    " is_combo_child, combo_of, sort_order)"
                    " VALUES(?,?,?,?,0,'',0,?,'',1,?,?)",
                    (oid, ch["id"], ch["name"], utils.money(ch["price"]),
                     ch["qty"] * it["qty"], pid, sort))
    conn.execute("UPDATE tables SET status='ordering' WHERE id=?", (table_id,))
    return oid, order_no


@bp.route("/reservations/<int:rid>/items", methods=["GET", "POST"])
@admin_required
def reservation_items(rid):
    """录预点菜：到店核销时可一键转成正式订单"""
    conn = db.get_db()
    res = conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
    if not res:
        flash("预订不存在", "danger")
        return redirect(url_for("reservations.reservations"))
    if request.method == "POST":
        cart = {}
        for k, v in request.form.items():
            if k.startswith("qty_") and str(v).strip():
                try:
                    q = int(v)
                except ValueError:
                    continue
                if q > 0:
                    cart[k[4:]] = q
        n = 0
        with conn:
            conn.execute("DELETE FROM reservation_items WHERE reservation_id=?", (rid,))
            for did, q in cart.items():
                if not did.isdigit():
                    continue
                d = conn.execute("SELECT * FROM dishes WHERE id=?", (int(did),)).fetchone()
                if not d or not d["on_sale"]:
                    continue
                conn.execute(
                    "INSERT INTO reservation_items(reservation_id, dish_id, name,"
                    " price, qty, note, sort_order) VALUES(?,?,?,?,?,'',?)",
                    (rid, d["id"], d["name"], d["price"], q, n))
                n += 1
            utype, ukey, uname = utils.current_user()
            utils.log_op(conn, "编辑", utype, ukey, uname,
                         "预订「%s」预点菜 %d 道" % (res["name"], n),
                         request.remote_addr or "")
        flash("预点菜已保存：%d 道" % n, "success")
        return redirect(url_for("reservations.reservations"))
    cats = conn.execute("SELECT * FROM categories ORDER BY sort_order, id").fetchall()
    dishes = conn.execute(
        "SELECT d.*, c.name cat_name FROM dishes d JOIN categories c ON c.id=d.category_id"
        " WHERE d.on_sale=1 ORDER BY c.sort_order, d.sort_order").fetchall()
    cur = {r["dish_id"]: r["qty"] for r in conn.execute(
        "SELECT dish_id, qty FROM reservation_items WHERE reservation_id=?", (rid,))}
    return render_template("admin/reservation_items.html", res=res, dishes=dishes,
                           cats=cats, cur=cur)


@bp.route("/reservations/<int:rid>/to-order", methods=["POST"])
@admin_required
def reservation_to_order(rid):
    """把预点菜转成正式订单"""
    conn = db.get_db()
    res = conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
    if not res:
        flash("预订不存在", "danger")
        return redirect(url_for("reservations.reservations"))
    if not res["table_id"]:
        flash("该预订没有指定桌台，无法转订单", "warning")
        return redirect(url_for("reservations.reservations"))
    rows = conn.execute("SELECT * FROM reservation_items WHERE reservation_id=?"
                        " ORDER BY sort_order, id", (rid,)).fetchall()
    if not rows:
        flash("该预订还没有预点菜", "warning")
        return redirect(url_for("reservations.reservations"))
    items = [{"dish_id": r["dish_id"], "name": r["name"],
              "price": utils.money(r["price"]), "qty": r["qty"],
              "note": r["note"] or "", "spec_name": r["spec_name"] or ""}
             for r in rows]
    try:
        with conn:
            oid, order_no = create_order_from_items(
                conn, res["table_id"], items, res["persons"],
                "预订转单：%s %s" % (res["name"], res["reserve_time"]))
            utype, ukey, uname = utils.current_user()
            utils.log_op(conn, "下单", utype, ukey, uname,
                         "预订「%s」转订单 %s（%d 道菜）" % (res["name"], order_no, len(items)),
                         request.remote_addr or "")
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(url_for("reservations.reservations"))
    flash("已转成订单 %s" % order_no, "success")
    return redirect(url_for("orders.order_detail", oid=oid))
