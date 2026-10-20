# -*- coding: utf-8 -*-
"""采购单：AI 建议 → 人工确认 → 入库（可追溯、可作废）"""
from datetime import date, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for

import config
import db
from ai import tools
from analytics import metrics
from auth import admin_required
from utils import current_user, money

bp = Blueprint("purchase", __name__)


@bp.route("/purchase")
@admin_required
def purchase_list():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM purchase_items i WHERE i.po_id=p.id) n"
        " FROM purchase_orders p ORDER BY p.id DESC LIMIT 50").fetchall()
    return render_template("purchase/list.html", active_nav="purchase", rows=rows,
                           app_name=config.APP_NAME)


@bp.route("/purchase/<int:po_id>")
@admin_required
def purchase_detail(po_id):
    conn = db.get_db()
    po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
    items = conn.execute("SELECT * FROM purchase_items WHERE po_id=? ORDER BY id",
                         (po_id,)).fetchall()
    return render_template("purchase/detail.html", active_nav="purchase",
                           po=po, items=items, app_name=config.APP_NAME)


@bp.route("/purchase/from-forecast", methods=["POST"])
@admin_required
def from_forecast():
    """按"未来 7 天预测用量 − 现有库存"生成采购单草稿"""
    conn = db.get_db()
    need = metrics.q_consumption_need(conn, days=config.FORECAST_HORIZON)
    risk = {r["ingredient_id"]: r for r in metrics.q_stock_risk(conn)}
    items = []
    for n in need:
        r = risk.get(n["ingredient_id"])
        if not r:
            continue
        qty = round(n["need"] - r["stock"], 2)
        if qty <= 0:
            continue
        items.append({"ingredient_id": n["ingredient_id"], "name": n["name"],
                      "unit": n["unit"], "qty": qty, "unit_cost": r["cost"]})
    if not items:
        flash("按当前库存，未来 %d 天无需补货" % config.FORECAST_HORIZON, "info")
        return redirect(url_for("analytics.forecast"))
    try:
        res = tools.create_purchase(conn, None, items,
                                    note="按 %d 天预测用量生成" % config.FORECAST_HORIZON)
        flash("已生成采购单 PO#%d，%d 项，合计 %.2f 元" % (res["po_id"], res["count"], res["total"]),
              "success")
        return redirect(url_for("purchase.purchase_detail", po_id=res["po_id"]))
    except tools.ToolError as e:
        flash(str(e), "danger")
        return redirect(url_for("analytics.forecast"))


@bp.route("/purchase/<int:po_id>/receive", methods=["POST"])
@admin_required
def receive(po_id):
    conn = db.get_db()
    try:
        tools.receive_purchase(conn, po_id, current_user())
        flash("采购单 PO#%d 已入库，库存与流水已更新" % po_id, "success")
    except tools.ToolError as e:
        flash(str(e), "danger")
    return redirect(url_for("purchase.purchase_detail", po_id=po_id))
