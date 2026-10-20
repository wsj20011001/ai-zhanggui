# -*- coding: utf-8 -*-
"""基础设置：店铺信息、桌台管理、账号密码、数据备份/恢复"""
import sqlite3
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, url_for

import config
import db
import utils
from auth import admin_required

bp = Blueprint("settings", __name__)


@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        conn = db.get_db()
        for key in ("shop_name", "shop_phone", "shop_address", "member_discount",
                    "points_earn_per_yuan", "points_money_ratio",
                    "full_reduction", "lost_days", "receipt_footer",
                    "rec_mode", "stock_block_order", "current_season",
                    "seasonal_block_order", "auto_open_browser"):
            # UPSERT：老库里没有的设置项（如 rec_mode）也能被正确写入，
            # 用 UPDATE 的话影响 0 行会静默丢失
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, request.form.get(key, "").strip()))
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, "设置", utype, ukey, uname,
                     "店铺设置已更新", request.remote_addr or "")
        conn.commit()
        flash("设置已保存", "success")
        return redirect(url_for("settings.settings"))
    return render_template("admin/settings.html", s=utils.get_settings(),
                           season_now=utils.current_season())


@bp.route("/password", methods=["GET", "POST"])
@admin_required
def password():
    if request.method == "POST":
        s = utils.get_settings()
        old = request.form.get("old_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not utils.verify_pwd(s.get("admin_pwd", ""), old):
            flash("原密码错误", "danger")
        elif len(new) < 6:
            flash("新密码至少 6 位", "danger")
        elif new != confirm:
            flash("两次输入的新密码不一致", "danger")
        else:
            # 账号必须非空：旧实现允许提交空用户名，导致 admin_user 变成 ""，
            # 之后管理端登录直接失效（或被空用户名绕过）
            new_user = request.form.get("username", s.get("admin_user") or "admin").strip()
            if not new_user:
                flash("管理端账号不能为空", "danger")
            else:
                conn = db.get_db()
                conn.execute("UPDATE settings SET value=? WHERE key='admin_pwd'",
                             (utils.make_pwd_hash(new),))
                conn.execute("UPDATE settings SET value=? WHERE key='admin_user'",
                             (new_user,))
                utype, ukey, uname = utils.current_user()
                utils.log_op(conn, "修改密码", utype, ukey, uname,
                             "管理端密码已修改", request.remote_addr or "")
                conn.commit()
                flash("密码已修改", "success")
                return redirect(url_for("settings.password"))
    return render_template("admin/password.html", s=utils.get_settings())


@bp.route("/tables")
@admin_required
def tables():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM tables ORDER BY sort_order, id").fetchall()
    return render_template("admin/tables.html", tables=rows,
                           table_status_label=config.TABLE_STATUS_LABEL,
                           table_types=config.TABLE_TYPES)


@bp.route("/tables/save", methods=["POST"])
@admin_required
def table_save():
    conn = db.get_db()
    tid = request.form.get("id", "").strip()
    name = request.form.get("name", "").strip()
    ttype = request.form.get("type", "大厅").strip()
    capacity = request.form.get("capacity", "4").strip()
    if not name:
        flash("桌台名称不能为空", "danger")
        return redirect(url_for("settings.tables"))
    try:
        capacity = int(capacity)
    except ValueError:
        capacity = 4
    try:
        if tid:
            conn.execute(
                "UPDATE tables SET name=?, type=?, capacity=? WHERE id=?",
                (name, ttype, capacity, tid))
            op_type, detail = "编辑", "桌台：%s（%s·%d人）" % (name, ttype, capacity)
        else:
            conn.execute(
                "INSERT INTO tables(name, type, capacity, sort_order)"
                " VALUES(?,?,?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM tables))",
                (name, ttype, capacity))
            op_type, detail = "新增", "桌台：%s（%s·%d人）" % (name, ttype, capacity)
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, op_type, utype, ukey, uname, detail,
                     request.remote_addr or "")
        conn.commit()
        flash("桌台已保存", "success")
    except sqlite3.IntegrityError:
        flash("桌台名称已存在", "danger")
    return redirect(url_for("settings.tables"))


@bp.route("/tables/<int:tid>/delete", methods=["POST"])
@admin_required
def table_delete(tid):
    conn = db.get_db()
    active = conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE table_id=? AND status"
        " IN ('pending','making','done')", (tid,)).fetchone()["c"]
    history = conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE table_id=?", (tid,)).fetchone()["c"]
    reserves = conn.execute(
        "SELECT COUNT(*) c FROM reservations WHERE table_id=?", (tid,)).fetchone()["c"]
    trow = conn.execute("SELECT name FROM tables WHERE id=?", (tid,)).fetchone()
    if active:
        flash("该桌台有未结订单，不能删除", "danger")
    elif history or reserves:
        flash("该桌台存在历史订单或预订记录，不能删除。如需停用，可修改桌台名称（如「XX停用」）",
              "danger")
    else:
        try:
            conn.execute("DELETE FROM carts WHERE table_id=?", (tid,))
            conn.execute("DELETE FROM tables WHERE id=?", (tid,))
            utype, ukey, uname = utils.current_user()
            utils.log_op(conn, "删除", utype, ukey, uname,
                         "桌台：%s" % (trow["name"] if trow else tid),
                         request.remote_addr or "")
            conn.commit()
            flash("桌台已删除", "success")
        except sqlite3.IntegrityError:
            conn.rollback()
            flash("该桌台存在关联记录，不能删除", "danger")
    return redirect(url_for("settings.tables"))


@bp.route("/backup")
@admin_required
def backup():
    from datetime import datetime
    backups = []
    for f in sorted(config.BACKUP_DIR.glob("restaurant_*.db"), reverse=True):
        backups.append({"name": f.name,
                        "time": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                        "size": f.stat().st_size})
    return render_template("admin/backup.html", backups=backups,
                           pending=config.RESTORE_FLAG.exists())


@bp.route("/backup/run", methods=["POST"])
@admin_required
def backup_run():
    try:
        fname = utils.do_backup()
        conn = db.get_db()
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, "备份", utype, ukey, uname,
                     "备份成功：" + fname, request.remote_addr or "")
        conn.commit()
        flash("备份成功：" + fname, "success")
    except Exception as e:  # pragma: no cover
        flash("备份失败：" + str(e), "danger")
    return redirect(url_for("settings.backup"))


@bp.route("/restore/apply", methods=["POST"])
@admin_required
def restore_apply():
    fname = Path(request.form.get("file", "")).name
    src = config.BACKUP_DIR / fname
    if not fname.startswith("restaurant_") or not src.exists():
        flash("备份文件无效", "danger")
    else:
        utils.request_restore(src)
        conn = db.get_db()
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, "备份", utype, ukey, uname,
                     "提交恢复请求：" + fname + "（重启后生效）",
                     request.remote_addr or "")
        conn.commit()
        flash("已提交恢复请求，下次启动程序时自动恢复", "success")
    return redirect(url_for("settings.backup"))
