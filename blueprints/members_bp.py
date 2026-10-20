# -*- coding: utf-8 -*-
"""会员管理：CRUD、充值、余额/积分调整、流水、流失提醒"""
import sqlite3
from datetime import datetime, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for

import db
import utils
from auth import admin_required

bp = Blueprint("members", __name__)


@bp.route("/members")
@admin_required
def members():
    conn = db.get_db()
    q = request.args.get("q", "").strip()
    where, params = "1=1", []
    if q:
        where += " AND (name LIKE ? OR phone LIKE ?)"
        params += ["%" + q + "%", "%" + q + "%"]
    rows = conn.execute(
        "SELECT * FROM members WHERE %s ORDER BY id DESC LIMIT 500" % where,
        params).fetchall()
    lost_days = int(float(utils.get_setting("lost_days", "90") or 90))
    deadline = (datetime.now() - timedelta(days=lost_days)).strftime("%Y-%m-%d %H:%M:%S")
    lost = [m for m in rows if not m["last_visit_at"] or m["last_visit_at"] < deadline]
    return render_template("admin/members.html", members=rows, q=q, lost=lost)


@bp.route("/members/add", methods=["GET", "POST"])
@admin_required
def member_add():
    if request.method == "POST":
        conn = db.get_db()
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        if not name or not phone:
            flash("姓名与手机号必填", "danger")
        else:
            try:
                balance = utils.money(request.form.get("balance", "0") or 0)
                points = int(request.form.get("points", "0") or 0)
                conn.execute(
                    "INSERT INTO members(name, phone, balance, points, level,"
                    " remark, created_at) VALUES(?,?,?,?,?,?,?)",
                    (name, phone, balance, points,
                     request.form.get("level", "普通会员").strip(),
                     request.form.get("remark", "").strip(), utils.now_str()))
                utype, ukey, uname = utils.current_user()
                utils.log_op(conn, "新增", utype, ukey, uname,
                             "会员：%s（%s）" % (name, phone),
                             request.remote_addr or "")
                conn.commit()
                if balance > 0:
                    mid = conn.execute(
                        "SELECT id FROM members WHERE phone=?", (phone,)).fetchone()["id"]
                    conn.execute(
                        "INSERT INTO member_ledger(member_id, type, amount,"
                        " balance_change, note, created_at)"
                        " VALUES(?, 'recharge', ?, ?, '开卡充值', ?)",
                        (mid, balance, balance, utils.now_str()))
                    conn.commit()
                flash("会员已添加", "success")
                return redirect(url_for("members.members"))
            except sqlite3.IntegrityError:
                flash("手机号已存在", "danger")
    return render_template("admin/member_form.html", member=None)


@bp.route("/members/<int:mid>/edit", methods=["GET", "POST"])
@admin_required
def member_edit(mid):
    conn = db.get_db()
    member = conn.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
    if not member:
        flash("会员不存在", "danger")
        return redirect(url_for("members.members"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        if not name or not phone:
            flash("姓名与手机号必填", "danger")
        else:
            try:
                conn.execute(
                    "UPDATE members SET name=?, phone=?, level=?, remark=? WHERE id=?",
                    (name, phone, request.form.get("level", member["level"]).strip(),
                     request.form.get("remark", "").strip(), mid))
                utype, ukey, uname = utils.current_user()
                utils.log_op(conn, "编辑", utype, ukey, uname,
                             "会员：%s（%s）" % (name, phone),
                             request.remote_addr or "")
                conn.commit()
                flash("会员已保存", "success")
                return redirect(url_for("members.member_detail", mid=mid))
            except sqlite3.IntegrityError:
                flash("手机号已被其他会员使用", "danger")
    return render_template("admin/member_form.html", member=member)


@bp.route("/members/<int:mid>")
@admin_required
def member_detail(mid):
    conn = db.get_db()
    member = conn.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
    if not member:
        flash("会员不存在", "danger")
        return redirect(url_for("members.members"))
    ledger = conn.execute(
        "SELECT * FROM member_ledger WHERE member_id=? ORDER BY id DESC LIMIT 200",
        (mid,)).fetchall()
    stats = conn.execute(
        "SELECT COUNT(*) cnt, SUM(final_amount) amt FROM orders"
        " WHERE member_id=? AND status='settled'", (mid,)).fetchone()
    return render_template("admin/member_detail.html", member=member,
                           ledger=ledger, stats=stats)


@bp.route("/members/<int:mid>/recharge", methods=["POST"])
@admin_required
def member_recharge(mid):
    conn = db.get_db()
    member = conn.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
    if not member:
        flash("会员不存在", "danger")
        return redirect(url_for("members.members"))
    try:
        amount = utils.money(request.form.get("amount", "0") or 0)
        gift_points = int(request.form.get("gift_points", "0") or 0)
        if amount <= 0:
            raise ValueError("充值金额必须大于 0")
        with conn:
            conn.execute(
                "UPDATE members SET balance=ROUND(balance+?,2),"
                " points=points+? WHERE id=?",
                (amount, gift_points, mid))
            conn.execute(
                "INSERT INTO member_ledger(member_id, type, amount, balance_change,"
                " points_change, note, created_at)"
                " VALUES(?, 'recharge', ?, ?, ?, ?, ?)",
                (mid, amount, amount, gift_points,
                 "充值" + ("（赠 %d 分）" % gift_points if gift_points else ""),
                 utils.now_str()))
            utype, ukey, uname = utils.current_user()
            utils.log_op(conn, "充值", utype, ukey, uname,
                         "会员%s 充值%.2f元" % (member["name"], amount),
                         request.remote_addr or "")
        flash("充值成功：%.2f 元" % amount, "success")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(url_for("members.member_detail", mid=mid))


@bp.route("/members/<int:mid>/adjust", methods=["POST"])
@admin_required
def member_adjust(mid):
    conn = db.get_db()
    member = conn.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
    if not member:
        flash("会员不存在", "danger")
        return redirect(url_for("members.members"))
    try:
        d_balance = utils.money(request.form.get("d_balance", "0") or 0)
        d_points = int(request.form.get("d_points", "0") or 0)
        note = request.form.get("note", "").strip() or "手动调整"
        with conn:
            conn.execute(
                "UPDATE members SET balance=ROUND(balance+?,2), points=points+?"
                " WHERE id=?", (d_balance, d_points, mid))
            conn.execute(
                "INSERT INTO member_ledger(member_id, type, amount, balance_change,"
                " points_change, note, created_at)"
                " VALUES(?, 'points_adjust', ?, ?, ?, ?, ?)",
                (mid, d_balance, d_balance, d_points, note, utils.now_str()))
            utype, ukey, uname = utils.current_user()
            utils.log_op(conn, "编辑", utype, ukey, uname,
                         "会员%s 余额调整%+.2f 积分调整%+d（%s）" % (
                             member["name"], d_balance, d_points, note),
                         request.remote_addr or "")
        flash("调整成功", "success")
    except ValueError:
        flash("输入有误", "danger")
    return redirect(url_for("members.member_detail", mid=mid))
