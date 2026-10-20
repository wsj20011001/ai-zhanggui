# -*- coding: utf-8 -*-
"""员工管理（点餐端账号）与操作日志"""
import sqlite3

from flask import Blueprint, flash, redirect, render_template, request, url_for

import db
import utils
from auth import admin_required

bp = Blueprint("employees", __name__)

ROLES = ["服务员", "收银员", "店长"]


@bp.route("/employees")
@admin_required
def employees():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM employees ORDER BY id").fetchall()
    return render_template("admin/employees.html", emps=rows, roles=ROLES)


@bp.route("/employees/save", methods=["POST"])
@admin_required
def employee_save():
    conn = db.get_db()
    eid = request.form.get("id", "").strip()
    no = request.form.get("employee_no", "").strip()
    name = request.form.get("name", "").strip()
    pwd = request.form.get("password", "")
    if not no or not name:
        flash("工号与姓名必填", "danger")
        return redirect(url_for("employees.employees"))
    try:
        if eid.isdigit():
            if pwd:
                if len(pwd) < 6:
                    flash("密码至少 6 位", "danger")
                    return redirect(url_for("employees.employees"))
                conn.execute(
                    "UPDATE employees SET employee_no=?, name=?, pwd=?, role=?,"
                    " remark=? WHERE id=?",
                    (no, name, utils.make_pwd_hash(pwd),
                     request.form.get("role", "服务员").strip(),
                     request.form.get("remark", "").strip(), int(eid)))
            else:
                conn.execute(
                    "UPDATE employees SET employee_no=?, name=?, role=?, remark=?"
                    " WHERE id=?",
                    (no, name, request.form.get("role", "服务员").strip(),
                     request.form.get("remark", "").strip(), int(eid)))
            op_type, detail = "编辑", "员工：%s（工号%s）" % (name, no)
            if pwd:
                utype, ukey, uname = utils.current_user()
                utils.log_op(conn, "修改密码", utype, ukey, uname,
                             "重置员工%s（工号%s）密码" % (name, no),
                             request.remote_addr or "")
        else:
            if len(pwd) < 6:
                flash("新增员工密码至少 6 位", "danger")
                return redirect(url_for("employees.employees"))
            conn.execute(
                "INSERT INTO employees(employee_no, name, pwd, role, remark,"
                " created_at) VALUES(?,?,?,?,?,?)",
                (no, name, utils.make_pwd_hash(pwd),
                 request.form.get("role", "服务员").strip(),
                 request.form.get("remark", "").strip(), utils.now_str()))
            op_type, detail = "新增", "员工：%s（工号%s）" % (name, no)
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, op_type, utype, ukey, uname, detail,
                     request.remote_addr or "")
        conn.commit()
        flash("员工已保存", "success")
    except sqlite3.IntegrityError:
        flash("工号已存在", "danger")
    return redirect(url_for("employees.employees"))


@bp.route("/employees/<int:eid>/toggle", methods=["POST"])
@admin_required
def employee_toggle(eid):
    conn = db.get_db()
    emp = conn.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    if not emp:
        flash("员工不存在", "danger")
    else:
        conn.execute("UPDATE employees SET active = 1 - active WHERE id=?", (eid,))
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, "状态变更", utype, ukey, uname,
                     "员工%s（工号%s）%s" % (emp["name"], emp["employee_no"],
                                       "停用" if emp["active"] else "启用"),
                     request.remote_addr or "")
        conn.commit()
        flash("员工 %s 已%s" % (emp["name"], "停用" if emp["active"] else "启用"),
              "success" if emp["active"] else "warning")
    return redirect(url_for("employees.employees"))


@bp.route("/employees/<int:eid>/delete", methods=["POST"])
@admin_required
def employee_delete(eid):
    conn = db.get_db()
    emp = conn.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    if not emp:
        flash("员工不存在", "danger")
        return redirect(url_for("employees.employees"))
    if conn.execute("SELECT COUNT(*) c FROM employees WHERE active=1").fetchone()["c"] <= 1 and emp["active"]:
        flash("至少保留一名启用员工，不能删除", "danger")
    else:
        conn.execute("DELETE FROM employees WHERE id=?", (eid,))
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, "删除", utype, ukey, uname,
                     "员工：%s（工号%s）" % (emp["name"], emp["employee_no"]),
                     request.remote_addr or "")
        conn.commit()
        flash("员工已删除", "success")
    return redirect(url_for("employees.employees"))


@bp.route("/logs")
@admin_required
def logs():
    """操作日志：全业务类型（登录/修改密码/下单/新增/编辑/删除/结算/状态变更/充值/库存/备份/设置）"""
    conn = db.get_db()
    utype = request.args.get("type", "").strip()      # user_type 终端
    op_type = request.args.get("op_type", "").strip()  # 操作类型
    where, params = "1=1", []
    if utype in ("admin", "employee"):
        where += " AND user_type=?"
        params.append(utype)
    if op_type in utils.OP_TYPES:
        where += " AND op_type=?"
        params.append(op_type)
    rows = conn.execute(
        "SELECT * FROM op_logs WHERE %s ORDER BY id DESC LIMIT 300" % where,
        params).fetchall()
    return render_template("admin/logs.html", logs=rows, utype=utype,
                           op_type=op_type, op_types=utils.OP_TYPES)
