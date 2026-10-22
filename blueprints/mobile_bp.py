# -*- coding: utf-8 -*-
"""移动端点餐（局域网）：员工工号密码登录后点餐（桌台、菜单、购物车、下单/撤回/催菜）"""
import json

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

import config
import db
import utils
from auth import emp_required

bp = Blueprint("mobile", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    """点餐端登录：工号 + 密码"""
    if session.get("emp_logged_in"):
        return redirect(url_for("mobile.table_select"))
    error = ""
    if request.method == "POST":
        no = request.form.get("employee_no", "").strip()
        pwd = request.form.get("password", "")
        conn = db.get_db()
        emp = conn.execute(
            "SELECT * FROM employees WHERE employee_no=?", (no,)).fetchone()
        ok = bool(emp and emp["active"] and utils.verify_pwd(emp["pwd"], pwd))
        if ok and utils.needs_rehash(emp["pwd"]):
            # 历史裸 SHA-256 口令：首次登录成功后自动升级为加盐迭代哈希
            conn.execute("UPDATE employees SET pwd=? WHERE id=?",
                         (utils.make_pwd_hash(pwd), emp["id"]))
        utils.log_login(conn, "employee", no,
                        emp["name"] if emp else "", ok,
                        request.remote_addr or "")
        conn.commit()
        if ok:
            session.clear()          # 防会话固定攻击
            session.permanent = True
            session["emp_logged_in"] = True
            session["emp_id"] = emp["id"]
            session["emp_name"] = emp["name"]
            session["emp_no"] = emp["employee_no"]
            return redirect(url_for("mobile.table_select"))
        error = "工号或密码错误"
        if emp and not emp["active"]:
            error = "该账号已停用，请联系管理员"
    return render_template("mobile/login.html", error=error)


@bp.route("/logout")
def logout():
    session.pop("emp_logged_in", None)
    session.pop("emp_id", None)
    session.pop("emp_name", None)
    session.pop("emp_no", None)
    return redirect(url_for("mobile.login"))


def cart_items(table_id):
    """读取某桌购物车，返回 (items list, persons, remark, updated_at)"""
    row = db.get_db().execute(
        "SELECT * FROM carts WHERE table_id=?", (table_id,)).fetchone()
    if not row:
        return [], 1, "", ""
    try:
        items = json.loads(row["items"])
    except (ValueError, TypeError):
        items = []
    return items, row["persons"], row["remark"] or "", row["updated_at"]


def dishes_with_children(conn):
    """上架菜品（含套餐子项展开），按分类分组"""
    dishes = conn.execute(
        "SELECT d.*, c.name cat_name FROM dishes d JOIN categories c"
        " ON c.id=d.category_id WHERE d.on_sale=1"
        " ORDER BY c.sort_order, d.sort_order, d.id").fetchall()
    combos = conn.execute(
        "SELECT ci.*, d.name dish_name, d.price dish_price, d.on_sale"
        " FROM combo_items ci JOIN dishes d ON d.id=ci.dish_id").fetchall()
    combo_map = {}
    for c in combos:
        combo_map.setdefault(c["combo_id"], []).append(c)
    out = []
    for d in dishes:
        item = dict(d)
        item["children"] = combo_map.get(d["id"], [])
        out.append(item)
    return out


def menu_json():
    """菜单 JSON：分类、菜品（含可售份数）、今日主推"""
    conn = db.get_db()
    cats = conn.execute(
        "SELECT * FROM categories ORDER BY sort_order, id").fetchall()
    dishes = dishes_with_children(conn)
    cat_ids = {d["category_id"] for d in dishes}
    avail = utils.dish_availability(conn)
    specs = {}
    for r in conn.execute("SELECT * FROM dish_specs ORDER BY dish_id, sort_order, id"):
        specs.setdefault(r["dish_id"], []).append(dict(r))
    season = utils.active_season()
    out = []
    for d in dishes:
        item = dict(d)
        item["children"] = [dict(c) for c in item["children"]]  # Row → dict
        a = avail.get(d["id"])
        # 只对"已录配方"的菜显示可做份数；未录配方 → None（不做限制）
        item["can_make"] = a["can_make"] if a else None
        # 时价菜不支持规格（价格本身就是待定的）
        item["specs"] = [] if d["price_mode"] == "market" else specs.get(d["id"], [])
        # 过季的时令菜
        item["season"] = d["season"] or ""
        item["season_blocked"] = bool(d["is_seasonal"]) and (d["season"] or "") not in ("", season)
        out.append(item)
    mode = utils.get_setting("rec_mode", "balanced")
    pick_blocked = set()
    if utils.get_setting("seasonal_block_order", "1") == "1":
        pick_blocked = {d["id"] for d in out if d["season_blocked"]}
    picks = [p for p in utils.recommend_scores(conn, mode)
             if p["score"] > 10 and (p["can_make"] is None or p["can_make"] > 0)
             and p["dish_id"] not in pick_blocked]
    return jsonify(categories=[dict(c) for c in cats if c["id"] in cat_ids],
                   dishes=out, top_picks=picks[:config.REC_TOP_N],
                   rec_mode=mode,
                   block_order=utils.get_setting("stock_block_order", "0") == "1",
                   season=utils.active_season(),
                   season_block=utils.get_setting("seasonal_block_order", "1") == "1",
                   shop_name=utils.get_setting("shop_name"))


@bp.route("/")
@emp_required
def table_select():
    tables = db.get_db().execute(
        "SELECT * FROM tables ORDER BY sort_order, id").fetchall()
    return render_template("mobile/table_select.html", tables=tables,
                           table_status_label=config.TABLE_STATUS_LABEL,
                           shop_name=utils.get_setting("shop_name"))


@bp.route("/order/<int:table_id>")
@emp_required
def order_page(table_id):
    conn = db.get_db()
    table = conn.execute("SELECT * FROM tables WHERE id=?", (table_id,)).fetchone()
    if not table:
        return render_template("mobile/table_select.html",
                               tables=conn.execute(
                                   "SELECT * FROM tables ORDER BY sort_order, id").fetchall(),
                               table_status_label=config.TABLE_STATUS_LABEL,
                               shop_name=utils.get_setting("shop_name"),
                               error="桌台不存在"), 404
    return render_template("mobile/order.html", table=table,
                           shop_name=utils.get_setting("shop_name"))


@bp.route("/api/tables")
@emp_required
def api_tables():
    tables = db.get_db().execute(
        "SELECT * FROM tables ORDER BY sort_order, id").fetchall()
    return jsonify(tables=[dict(t) for t in tables])


@bp.route("/api/menu")
@emp_required
def api_menu():
    return menu_json()


@bp.route("/api/cart/<int:table_id>", methods=["GET", "POST"])
@emp_required
def api_cart(table_id):
    conn = db.get_db()
    if request.method == "GET":
        items, persons, remark, updated = cart_items(table_id)
        return jsonify(items=items, persons=persons, remark=remark,
                       updated_at=updated)
    # POST：全量保存
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    if not isinstance(items, list):
        return jsonify(ok=False, msg="数据格式错误"), 400
    try:
        persons = max(1, int(data.get("persons", 1)))
    except (TypeError, ValueError):
        persons = 1
    remark = str(data.get("remark", ""))[:200]
    now = utils.now_str()
    conn.execute(
        "INSERT INTO carts(table_id, items, persons, remark, updated_at)"
        " VALUES(?,?,?,?,?) ON CONFLICT(table_id)"
        " DO UPDATE SET items=excluded.items, persons=excluded.persons,"
        " remark=excluded.remark, updated_at=excluded.updated_at",
        (table_id, json.dumps(items, ensure_ascii=False), persons,
         remark, now))
    conn.execute("UPDATE tables SET status='ordering' WHERE id=?", (table_id,))
    conn.commit()
    return jsonify(ok=True, updated_at=now)


def validate_items(items):
    """下单校验：菜品存在、上架、价格有效。返回 (快照明细列表, 总价) 或抛 ValueError"""
    conn = db.get_db()
    if not items:
        raise ValueError("购物车为空，请先点餐")
    avail = utils.dish_availability(conn)
    block = utils.get_setting("stock_block_order", "0") == "1"
    snapshots = []
    for it in items:
        qty = int(it.get("qty", 0))
        if qty <= 0:
            continue
        dish = conn.execute("SELECT * FROM dishes WHERE id=?",
                            (it.get("dish_id"),)).fetchone()
        if not dish or not dish["on_sale"]:
            raise ValueError("菜品「%s」已下架，请移除后重新下单" % it.get("name", "?"))
        a = avail.get(dish["id"])
        if block and a and a["can_make"] is not None and a["can_make"] <= 0:
            raise ValueError("菜品「%s」原料不足，暂时无法制作" % dish["name"])
        # 过季的时令菜：默认不可点（可在设置里关掉）
        if utils.get_setting("seasonal_block_order", "1") == "1" \
                and dish["is_seasonal"] and (dish["season"] or "") not in ("", utils.active_season()):
            raise ValueError("「%s」是%s时令菜，当前已过季" % (dish["name"], dish["season"]))
        # 规格：单价一律由服务端按 dish.price + spec.price_delta 重算，绝不信客户端
        spec_name, spec_delta = "", 0.0
        spec_id = it.get("spec_id")
        if spec_id and dish["price_mode"] != "market":
            sp = conn.execute("SELECT * FROM dish_specs WHERE id=? AND dish_id=?",
                              (spec_id, dish["id"])).fetchone()
            if sp:
                spec_name, spec_delta = sp["name"], utils.money(sp["price_delta"])
        # 套餐子项一律以服务端 combo_items 为准，完全忽略客户端传来的 children。
        # 旧实现直接采信客户端 children，实测可注入
        # {"name":"注入的幽灵菜","price":9999,"qty":5} 并原样写入厨房票据。
        kids = []
        if dish["is_combo"]:
            rows = conn.execute(
                "SELECT ci.qty, d.id, d.name, d.price FROM combo_items ci"
                " JOIN dishes d ON d.id=ci.dish_id"
                " WHERE ci.combo_id=? AND d.on_sale=1 ORDER BY ci.id",
                (dish["id"],)).fetchall()
            kids = [{"dish_id": r["id"], "name": r["name"],
                     "price": utils.money(r["price"]), "qty": r["qty"]}
                    for r in rows]
        snapshots.append({
            "dish_id": dish["id"], "name": dish["name"],
            "price": utils.money(dish["price"] + spec_delta), "qty": qty,
            "spec_name": spec_name, "spec_delta": spec_delta,
            # 时价菜下单时无价，标记待定价，结算前必须补价
            "price_pending": 1 if dish["price_mode"] == "market" else 0,
            "note": str(it.get("note", ""))[:100],
            "children": kids,
        })
    if not snapshots:
        raise ValueError("购物车为空，请先点餐")
    return snapshots


@bp.route("/api/order/<int:table_id>/submit", methods=["POST"])
@emp_required
def order_submit(table_id):
    conn = db.get_db()
    table = conn.execute("SELECT * FROM tables WHERE id=?", (table_id,)).fetchone()
    if not table:
        return jsonify(ok=False, msg="桌台不存在"), 404
    items, persons, remark, _ = cart_items(table_id)
    try:
        snapshots = validate_items(items)
    except ValueError as e:
        return jsonify(ok=False, msg=str(e)), 400
    calc = utils.calc_order(snapshots, use_member=False)
    with conn:
        order_no = utils.make_order_no(conn)   # 同一连接取号，测试与多库场景才不会串
        cur = conn.execute(
            "INSERT INTO orders(order_no, table_id, table_name, persons, status,"
            " total_amount, final_amount, remark, created_at)"
            " VALUES(?,?,?,?,'pending',?,?,?,?)",
            (order_no, table_id, table["name"],
             persons, calc["total"], calc["final"], remark, utils.now_str()))
        order_id = cur.lastrowid
        sort = 0
        parent_ids = {}
        for s in snapshots:
            sort += 1
            cur = conn.execute(
                "INSERT INTO order_items(order_id, dish_id, name, price,"
                " price_pending, spec_name, spec_delta, qty, note,"
                " is_combo_child, sort_order) VALUES(?,?,?,?,?,?,?,?,?,0,?)",
                (order_id, s["dish_id"], s["name"], s["price"],
                 s.get("price_pending", 0), s.get("spec_name", ""),
                 s.get("spec_delta", 0), s["qty"], s["note"], sort))
            parent_ids[id(s)] = cur.lastrowid
            for child in s["children"]:
                sort += 1
                conn.execute(
                    "INSERT INTO order_items(order_id, dish_id, name, price, qty,"
                    " is_combo_child, combo_of, sort_order) VALUES(?,?,?,?,?,1,?,?)",
                    (order_id, child.get("dish_id"), child.get("name"),
                     child.get("price", 0), child.get("qty", 1),
                     parent_ids[id(s)], sort))
        conn.execute("DELETE FROM carts WHERE table_id=?", (table_id,))
        conn.execute("UPDATE tables SET status='ordering' WHERE id=?", (table_id,))
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, "下单", utype, ukey, uname,
                     "订单%s 桌台%s 金额%.2f元" % (order_no, table["name"], calc["final"]),
                     request.remote_addr or "")
    return jsonify(ok=True, order_id=order_id)


@bp.route("/api/table/<int:table_id>/orders")
@emp_required
def api_table_orders(table_id):
    """本桌进行中的订单（供移动端催菜/撤回）"""
    rows = db.get_db().execute(
        "SELECT id, order_no, status, total_amount, created_at, hurry FROM orders"
        " WHERE table_id=? AND status IN ('pending','making')"
        " ORDER BY id DESC LIMIT 10", (table_id,)).fetchall()
    return jsonify(orders=[dict(r) for r in rows])


@bp.route("/api/order/<int:order_id>/hurry", methods=["POST"])
@emp_required
def order_hurry(order_id):
    conn = db.get_db()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        return jsonify(ok=False, msg="订单不存在"), 404
    # 待确认和制作中都能催：真正该催的恰恰是"做了一半还没上"的单
    if row["status"] not in ("pending", "making"):
        return jsonify(ok=False, msg="当前状态不可催菜"), 400
    conn.execute("UPDATE orders SET hurry=1 WHERE id=?", (order_id,))
    utype, ukey, uname = utils.current_user()
    utils.log_op(conn, "状态变更", utype, ukey, uname,
                 "催菜 订单%s" % row["order_no"], request.remote_addr or "")
    conn.commit()
    return jsonify(ok=True)


@bp.route("/api/order/<int:order_id>/withdraw", methods=["POST"])
@emp_required
def order_withdraw(order_id):
    conn = db.get_db()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        return jsonify(ok=False, msg="订单不存在"), 404
    if row["status"] != "pending":
        return jsonify(ok=False, msg="仅待确认订单可撤回，请服务员协助处理"), 400
    conn.execute(
        "UPDATE orders SET status='cancelled', cancelled_at=? WHERE id=?",
        (utils.now_str(), order_id))
    utype, ukey, uname = utils.current_user()
    utils.log_op(conn, "状态变更", utype, ukey, uname,
                 "撤回 订单%s" % row["order_no"], request.remote_addr or "")
    conn.commit()
    return jsonify(ok=True, msg="订单已撤回，可重新点单")
