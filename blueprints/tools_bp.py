# -*- coding: utf-8 -*-
"""决策工具：经营推演器 + CSV 数据导入"""
import io
import os
import uuid
from datetime import date, timedelta

from flask import (Blueprint, Response, flash, jsonify, redirect, render_template,
                   request, session, url_for)

import config
import db
from analytics import etl, importer, metrics, simulate
from auth import admin_required
from utils import current_user, log_op, money

bp = Blueprint("tools", __name__)

IMPORT_DIR = config.DATA_DIR / "imports"


# ---------------------------------------------------------------- 推演器

@bp.route("/analytics/simulator")
@admin_required
def simulator_page():
    conn = db.get_db()
    d2 = date.today() - timedelta(days=1)
    d1 = d2 - timedelta(days=29)
    k = metrics.q_kpi(conn, d1, d2)
    q = metrics.q_dish_quadrant(conn, d1.isoformat(), d2.isoformat())
    initial = simulate.run(conn, days=30, price_pct=0.05, elasticity=0.8,
                           scope="low_margin")
    return render_template("analytics/simulator.html", active_nav="ana_simulator",
                           k=k, quad=q["summary"], initial=initial,
                           formula=simulate.formula_text(initial),
                           app_name=config.APP_NAME)


@bp.route("/api/simulate", methods=["POST"])
@admin_required
def api_simulate():
    conn = db.get_db()
    p = request.get_json(silent=True) or {}
    try:
        res = simulate.run(
            conn, days=int(p.get("days", 30) or 30),
            price_pct=float(p.get("price_pct", 0) or 0),
            elasticity=float(p.get("elasticity", 0.8) or 0.8),
            target_discount=(None if p.get("target_discount") in (None, "")
                             else float(p.get("target_discount"))),
            scope=p.get("scope") or "all")
        res["formula"] = simulate.formula_text(res)
        return jsonify(ok=True, data=res)
    except Exception as e:
        return jsonify(ok=False, msg="推演失败：%s" % e)


# ---------------------------------------------------------------- CSV 导入

def _save_upload(file_storage):
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    path = IMPORT_DIR / ("%s.csv" % token)
    file_storage.save(str(path))
    return token, path


@bp.route("/analytics/import", methods=["GET", "POST"])
@admin_required
def import_page():
    conn = db.get_db()
    result = None
    prev = None
    token = request.form.get("token") or ""

    if request.method == "POST":
        action = request.form.get("action", "upload")
        if action == "upload":
            f = request.files.get("file")
            if not f or not f.filename:
                flash("请选择 CSV 文件", "danger")
                return redirect(url_for("tools.import_page"))
            if not f.filename.lower().endswith((".csv", ".txt")):
                flash("只支持 .csv / .txt（Excel 请先另存为 CSV）", "danger")
                return redirect(url_for("tools.import_page"))
            token, path = _save_upload(f)
            prev = importer.preview(str(path))
            prev["token"] = token
            prev["filename"] = f.filename
            if prev["missing"]:
                flash("这些必填列没有自动识别，请手动指定：%s" % "、".join(prev["missing"]), "warning")
            return render_template("analytics/import.html", active_nav="ana_import",
                                   prev=prev, result=None, fields=importer.FIELDS,
                                   app_name=config.APP_NAME)

        # action == run
        path = IMPORT_DIR / ("%s.csv" % token)
        if not token or not path.exists():
            flash("上传文件已过期，请重新上传", "danger")
            return redirect(url_for("tools.import_page"))
        mapping = {k: (request.form.get("map_" + k) or "").strip() or None
                   for k, _l, _r, _d in importer.FIELDS}
        missing = [label for k, label, req, _ in importer.FIELDS if req and not mapping.get(k)]
        if missing:
            flash("还有必填列没映射：%s" % "、".join(missing), "danger")
            prev = importer.preview(str(path), mapping)
            prev["token"] = token
            return render_template("analytics/import.html", active_nav="ana_import",
                                   prev=prev, result=None, fields=importer.FIELDS,
                                   app_name=config.APP_NAME)

        result = importer.do_import(conn, str(path), mapping, current_user())
        log_op(conn, "新增", *current_user(),
               "CSV 导入：新增 %d 单 / %d 行明细，跳过重复 %d 单，失败 %d 行"
               % (result["orders"], result["items"], result["skipped"], result["bad"]),
               request.remote_addr or "")
        conn.commit()
        try:
            path.unlink(missing_ok=True)     # 导入完即删，不留原始营业数据
        except OSError:
            pass
        flash("导入完成：新增 %d 单，跳过重复 %d 单" % (result["orders"], result["skipped"]),
              "success" if result["orders"] else "warning")
        return render_template("analytics/import.html", active_nav="ana_import",
                               prev=None, result=result, fields=importer.FIELDS,
                               app_name=config.APP_NAME)

    # GET：统计现状，让人知道导入前后差多少
    stats = conn.execute(
        "SELECT COUNT(*) c, MIN(substr(settled_at,1,10)) a, MAX(substr(settled_at,1,10)) b"
        " FROM orders WHERE status='settled'").fetchone()
    return render_template("analytics/import.html", active_nav="ana_import",
                           prev=None, result=None, stats=stats, fields=importer.FIELDS,
                           app_name=config.APP_NAME)


@bp.route("/analytics/import/template")
@admin_required
def import_template():
    data = ("\ufeff" + importer.TEMPLATE).encode("utf-8")
    return Response(data, mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=order_import_template.csv"})
