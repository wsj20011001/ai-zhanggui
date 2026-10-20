# -*- coding: utf-8 -*-
"""菜品管理：分类、菜品 CRUD、图片上传、套餐子项、快速上下架、批量修改"""
import sqlite3
import uuid

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

import config
import db
import utils
from auth import admin_required

bp = Blueprint("dishes", __name__)


# 图片文件头特征：仅看扩展名挡不住"改名成 .png 的脚本/可执行文件"
_IMG_SIGNATURES = ((b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"),
                   (b"GIF87a", "gif"), (b"GIF89a", "gif"))


def _sniff_image(head):
    """按文件头判断真实图片类型，返回 jpg/png/gif/webp 或 ''"""
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    for sig, kind in _IMG_SIGNATURES:
        if head.startswith(sig):
            return kind
    return ""


def save_image(file):
    """保存上传图片，返回相对路径；未上传返回 ''
    校验三件事：扩展名白名单、文件头与扩展名一致、体积上限（由 MAX_CONTENT_LENGTH 兜底）"""
    if not file or not file.filename:
        return ""
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in config.ALLOWED_IMG_EXT:
        raise ValueError("仅支持 jpg/png/gif/webp 图片")
    head = file.stream.read(16)
    try:
        file.stream.seek(0)
    except (AttributeError, OSError):
        pass
    kind = _sniff_image(head)
    if not kind:
        raise ValueError("文件内容不是有效图片（仅接受 jpg/png/gif/webp）")
    if (kind == "jpg" and ext not in ("jpg", "jpeg")) or (kind != "jpg" and ext != kind):
        raise ValueError("图片内容与扩展名不一致，请重新选择文件")
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    fname = "dish_%s.%s" % (uuid.uuid4().hex[:12], ext)
    file.save(str(config.UPLOAD_DIR / fname))
    return "uploads/" + fname


def save_combo(conn, combo_id, dish_ids, form):
    """保存套餐子项。dish_ids: 勾选的子菜品 id 列表；数量从 form 取 combo_qty_<id>，
    按 id 对应，不依赖表单提交顺序。非法 id（外键不存在等）逐项跳过"""
    conn.execute("DELETE FROM combo_items WHERE combo_id=?", (combo_id,))
    for did in dish_ids:
        if not did or not str(did).isdigit():
            continue
        did = int(did)
        try:
            qty = max(1, int(form.get("combo_qty_%s" % did, "1") or 1))
        except (TypeError, ValueError):
            qty = 1
        try:
            conn.execute(
                "INSERT INTO combo_items(combo_id, dish_id, qty) VALUES(?,?,?)",
                (combo_id, did, qty))
        except sqlite3.IntegrityError:
            continue  # 子菜品已被删除等，跳过


@bp.route("/dishes")
@admin_required
def dishes():
    conn = db.get_db()
    cats = conn.execute("SELECT * FROM categories ORDER BY sort_order, id").fetchall()
    q = request.args.get("q", "").strip()
    cat_id = request.args.get("cat", "").strip()
    where, params = "1=1", []
    if q:
        where += " AND d.name LIKE ?"
        params.append("%" + q + "%")
    if cat_id.isdigit():
        where += " AND d.category_id=?"
        params.append(int(cat_id))
    rows = conn.execute(
        "SELECT d.*, c.name cat_name FROM dishes d JOIN categories c"
        " ON c.id=d.category_id WHERE %s ORDER BY c.sort_order, d.sort_order, d.id" % where,
        params).fetchall()
    have = {r["dish_id"] for r in conn.execute(
        "SELECT DISTINCT dish_id FROM dish_recipes")}
    has_spec = {r["dish_id"]: r["c"] for r in conn.execute(
        "SELECT dish_id, COUNT(*) c FROM dish_specs GROUP BY dish_id")}
    return render_template("admin/dishes.html", cats=cats, dishes=rows,
                           sel_cat=cat_id, q=q, have_recipe=have,
                           has_spec=has_spec, season=utils.active_season())


@bp.route("/dishes/add", methods=["GET", "POST"])
@admin_required
def dish_add():
    conn = db.get_db()
    cats = conn.execute("SELECT * FROM categories ORDER BY sort_order, id").fetchall()
    candidates = conn.execute(
        "SELECT * FROM dishes WHERE is_combo=0 ORDER BY category_id, sort_order, id").fetchall()
    if request.method == "POST":
        try:
            name = request.form.get("name", "").strip()
            price_mode = request.form.get("price_mode", "fixed").strip()
            if price_mode not in config.PRICE_MODES:
                price_mode = "fixed"
            season = request.form.get("season", "").strip()
            if season not in config.SEASONS:
                season = ""
            price = float(request.form.get("price", "0") or 0)
            cat_id = int(request.form.get("category_id", "0"))
            if not name or cat_id <= 0:
                raise ValueError("名称与分类必填")
            if not conn.execute("SELECT 1 FROM categories WHERE id=?",
                                (cat_id,)).fetchone():
                raise ValueError("所选分类不存在，请刷新页面后重新选择")
            if price_mode == "market":
                price = 0.0        # 时价菜不预设价格，由收银台结算前补价
            elif price <= 0:
                raise ValueError("固定价菜品必须填写大于 0 的价格")
            image = save_image(request.files.get("image"))
            is_combo = 1 if request.form.get("is_combo") else 0
            conn.execute(
                "INSERT INTO dishes(name, category_id, price, price_mode, image,"
                " description, is_recommend, is_featured, is_combo, warn_flag,"
                " season, is_seasonal, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, cat_id, utils.money(price), price_mode, image,
                 request.form.get("description", "").strip(),
                 1 if request.form.get("is_recommend") else 0,
                 1 if request.form.get("is_featured") else 0,
                 is_combo, 1 if request.form.get("warn_flag") else 0,
                 season, 1 if request.form.get("is_seasonal") else 0,
                 utils.now_str()))
            did = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            if is_combo:
                save_combo(conn, did, request.form.getlist("combo_dish_id"), request.form)
            utype, ukey, uname = utils.current_user()
            utils.log_op(conn, "新增", utype, ukey, uname,
                         "菜品：%s（%.2f元）" % (name, utils.money(price)),
                         request.remote_addr or "")
            conn.commit()
            flash("菜品已添加", "success")
            return redirect(url_for("dishes.dishes"))
        except ValueError as e:
            flash("输入有误：" + str(e), "danger")
        except sqlite3.IntegrityError as e:
            flash("保存失败（数据关联异常）：%s" % e, "danger")
    return render_template("admin/dish_form.html", cats=cats,
                           candidates=candidates, dish=None,
                           combo_map={})


@bp.route("/dishes/<int:did>/edit", methods=["GET", "POST"])
@admin_required
def dish_edit(did):
    conn = db.get_db()
    dish = conn.execute("SELECT * FROM dishes WHERE id=?", (did,)).fetchone()
    if not dish:
        flash("菜品不存在", "danger")
        return redirect(url_for("dishes.dishes"))
    cats = conn.execute("SELECT * FROM categories ORDER BY sort_order, id").fetchall()
    candidates = conn.execute(
        "SELECT * FROM dishes WHERE is_combo=0 AND id!=? ORDER BY category_id, sort_order, id",
        (did,)).fetchall()
    combo_items = conn.execute(
        "SELECT * FROM combo_items WHERE combo_id=? ORDER BY id", (did,)).fetchall()
    combo_map = {ci["dish_id"]: ci["qty"] for ci in combo_items}
    if request.method == "POST":
        try:
            name = request.form.get("name", "").strip()
            price_mode = request.form.get("price_mode", "fixed").strip()
            if price_mode not in config.PRICE_MODES:
                price_mode = "fixed"
            season = request.form.get("season", "").strip()
            if season not in config.SEASONS:
                season = ""
            price = float(request.form.get("price", "0") or 0)
            cat_id = int(request.form.get("category_id", "0"))
            if not name or cat_id <= 0:
                raise ValueError("名称与分类必填")
            if not conn.execute("SELECT 1 FROM categories WHERE id=?",
                                (cat_id,)).fetchone():
                raise ValueError("所选分类不存在，请刷新页面后重新选择")
            if price_mode == "market":
                price = 0.0
            elif price <= 0:
                raise ValueError("固定价菜品必须填写大于 0 的价格")
            image = save_image(request.files.get("image")) or dish["image"]
            is_combo = 1 if request.form.get("is_combo") else 0
            conn.execute(
                "UPDATE dishes SET name=?, category_id=?, price=?, price_mode=?,"
                " image=?, description=?, is_recommend=?, is_featured=?,"
                " is_combo=?, warn_flag=?, season=?, is_seasonal=? WHERE id=?",
                (name, cat_id, utils.money(price), price_mode, image,
                 request.form.get("description", "").strip(),
                 1 if request.form.get("is_recommend") else 0,
                 1 if request.form.get("is_featured") else 0,
                 is_combo, 1 if request.form.get("warn_flag") else 0,
                 season, 1 if request.form.get("is_seasonal") else 0, did))
            save_combo(conn, did, request.form.getlist("combo_dish_id"), request.form)
            utype, ukey, uname = utils.current_user()
            utils.log_op(conn, "编辑", utype, ukey, uname,
                         "菜品：%s（%.2f元）" % (name, utils.money(price)),
                         request.remote_addr or "")
            conn.commit()
            flash("菜品已保存", "success")
            return redirect(url_for("dishes.dishes"))
        except ValueError as e:
            flash("输入有误：" + str(e), "danger")
        except sqlite3.IntegrityError as e:
            flash("保存失败（数据关联异常）：%s" % e, "danger")
    return render_template("admin/dish_form.html", cats=cats, dish=dish,
                           candidates=candidates, combo_map=combo_map)


@bp.route("/dishes/<int:did>/delete", methods=["POST"])
@admin_required
def dish_delete(did):
    conn = db.get_db()
    drow = conn.execute("SELECT name FROM dishes WHERE id=?", (did,)).fetchone()
    used = conn.execute(
        "SELECT COUNT(*) c FROM orders o JOIN order_items oi ON oi.order_id=o.id"
        " WHERE oi.dish_id=? AND o.status IN ('pending','making','done')",
        (did,)).fetchone()["c"]
    if used:
        conn.execute("UPDATE dishes SET on_sale=0 WHERE id=?", (did,))
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, "状态变更", utype, ukey, uname,
                     "菜品「%s」有进行中订单，自动下架" % (drow["name"] if drow else did),
                     request.remote_addr or "")
        conn.commit()
        flash("该菜品有进行中订单引用，已自动下架", "warning")
    else:
        conn.execute("DELETE FROM combo_items WHERE combo_id=? OR dish_id=?", (did, did))
        conn.execute("DELETE FROM dish_recipes WHERE dish_id=?", (did,))
        conn.execute("DELETE FROM dish_specs WHERE dish_id=?", (did,))
        conn.execute("DELETE FROM dishes WHERE id=?", (did,))
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, "删除", utype, ukey, uname,
                     "菜品：%s" % (drow["name"] if drow else did),
                     request.remote_addr or "")
        conn.commit()
        flash("菜品已删除", "success")
    return redirect(url_for("dishes.dishes"))


@bp.route("/dishes/<int:did>/toggle", methods=["POST"])
@admin_required
def dish_toggle(did):
    conn = db.get_db()
    drow = conn.execute("SELECT name, on_sale FROM dishes WHERE id=?", (did,)).fetchone()
    conn.execute("UPDATE dishes SET on_sale = 1 - on_sale WHERE id=?", (did,))
    utype, ukey, uname = utils.current_user()
    utils.log_op(conn, "状态变更", utype, ukey, uname,
                 "菜品「%s」%s" % (drow["name"] if drow else did,
                              "下架" if drow["on_sale"] else "上架"),
                 request.remote_addr or "")
    conn.commit()
    row = conn.execute("SELECT on_sale FROM dishes WHERE id=?", (did,)).fetchone()
    return jsonify(ok=True, on_sale=row["on_sale"])


@bp.route("/dishes/batch", methods=["GET", "POST"])
@admin_required
def dishes_batch():
    conn = db.get_db()
    cats = conn.execute("SELECT * FROM categories ORDER BY sort_order, id").fetchall()
    if request.method == "POST":
        cat_id = request.form.get("category_id", "").strip()
        action = request.form.get("action", "")
        if not cat_id.isdigit():
            flash("请选择分类", "danger")
        else:
            cat_id = int(cat_id)
            if action == "on":
                conn.execute("UPDATE dishes SET on_sale=1 WHERE category_id=?", (cat_id,))
                flash("该分类菜品已全部上架", "success")
            elif action == "off":
                conn.execute("UPDATE dishes SET on_sale=0 WHERE category_id=?", (cat_id,))
                flash("该分类菜品已全部下架", "success")
            elif action == "price":
                try:
                    pct = float(request.form.get("percent", "0") or 0)
                    if not -90 <= pct <= 200:
                        raise ValueError("调整幅度限 -90% ~ +200%")
                    conn.execute(
                        "UPDATE dishes SET price=ROUND(price*(1+?), 2) WHERE category_id=?",
                        (pct / 100.0, cat_id))
                    conn.commit()
                    flash("该分类价格已调整 %+.0f%%" % pct, "success")
                except ValueError as e:
                    flash("输入有误：" + str(e), "danger")
            cname = conn.execute(
                "SELECT name FROM categories WHERE id=?", (cat_id,)).fetchone()
            utype, ukey, uname = utils.current_user()
            utils.log_op(conn, "编辑", utype, ukey, uname,
                         "批量修改分类「%s」：%s" % (
                             cname["name"] if cname else cat_id,
                             {"on": "全部上架", "off": "全部下架", "price": "调价"}.get(action, action)),
                         request.remote_addr or "")
            conn.commit()
            return redirect(url_for("dishes.dishes_batch"))
    return render_template("admin/dishes_batch.html", cats=cats)


@bp.route("/categories")
@admin_required
def categories():
    """分类管理页：列表（含菜品数）+ 新增/重命名 + 删除"""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM dishes d WHERE d.category_id=c.id) dish_count"
        " FROM categories c ORDER BY c.sort_order, c.id").fetchall()
    return render_template("admin/categories.html", cats=rows,
                           roles=config.COURSE_ROLES)


@bp.route("/categories/save", methods=["POST"])
@admin_required
def category_save():
    conn = db.get_db()
    cid = request.form.get("id", "").strip()
    name = request.form.get("name", "").strip()
    if not name:
        flash("分类名称不能为空", "danger")
        return redirect(url_for("dishes.categories"))
    role = request.form.get("course_role", "").strip()
    if role not in config.COURSE_ROLES:
        role = ""
    try:
        if cid.isdigit():
            conn.execute("UPDATE categories SET name=?, course_role=? WHERE id=?",
                         (name, role, int(cid)))
            op_type, detail = "编辑", "分类：%s（%s）" % (name, role or "未设角色")
        else:
            conn.execute(
                "INSERT INTO categories(name, course_role, sort_order)"
                " VALUES(?,?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM categories))",
                (name, role))
            op_type, detail = "新增", "分类：%s（%s）" % (name, role or "未设角色")
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, op_type, utype, ukey, uname, detail,
                     request.remote_addr or "")
        conn.commit()
        flash("分类已保存", "success")
    except sqlite3.IntegrityError:
        flash("分类名称已存在", "danger")
    return redirect(url_for("dishes.categories"))


@bp.route("/categories/add", methods=["POST"])
@admin_required
def category_add():
    name = request.form.get("name", "").strip()
    if name:
        try:
            conn = db.get_db()
            conn.execute(
                "INSERT INTO categories(name, sort_order)"
                " VALUES(?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM categories))",
                (name,))
            conn.commit()
            flash("分类已添加", "success")
        except sqlite3.IntegrityError:
            flash("分类已存在", "danger")
    return redirect(url_for("dishes.dishes"))


@bp.route("/categories/<int:cid>/delete", methods=["POST"])
@admin_required
def category_delete(cid):
    conn = db.get_db()
    crow = conn.execute("SELECT name FROM categories WHERE id=?", (cid,)).fetchone()
    n = conn.execute("SELECT COUNT(*) c FROM dishes WHERE category_id=?", (cid,)).fetchone()["c"]
    if n:
        flash("该分类下还有菜品，不能删除", "danger")
    else:
        conn.execute("DELETE FROM categories WHERE id=?", (cid,))
        utype, ukey, uname = utils.current_user()
        utils.log_op(conn, "删除", utype, ukey, uname,
                     "分类：%s" % (crow["name"] if crow else cid),
                     request.remote_addr or "")
        conn.commit()
        flash("分类已删除", "success")
    return redirect(url_for("dishes.dishes"))

# ---------- 菜品配方（BOM）与批量导入 ----------

@bp.route("/dishes/<int:did>/recipe", methods=["GET", "POST"])
@admin_required
def dish_recipe(did):
    """录入/修改单个菜品的用料配方"""
    conn = db.get_db()
    dish = conn.execute("SELECT * FROM dishes WHERE id=?", (did,)).fetchone()
    if not dish:
        flash("菜品不存在", "danger")
        return redirect(url_for("dishes.dishes"))
    if request.method == "POST":
        n = 0
        try:
            with conn:
                conn.execute("DELETE FROM dish_recipes WHERE dish_id=?", (did,))
                for iid in request.form.getlist("ingredient_id"):
                    if not str(iid).isdigit():
                        continue
                    raw = request.form.get("qty_%s" % iid, "").strip()
                    if raw == "":
                        continue
                    q = float(raw)
                    if q <= 0:
                        continue
                    conn.execute(
                        "INSERT INTO dish_recipes(dish_id, ingredient_id, qty)"
                        " VALUES(?,?,?)", (did, int(iid), round(q, 3)))
                    n += 1
                utype, ukey, uname = utils.current_user()
                utils.log_op(conn, "编辑", utype, ukey, uname,
                             "菜品「%s」配方更新：%d 种原料" % (dish["name"], n),
                             request.remote_addr or "")
        except (ValueError, sqlite3.IntegrityError) as e:
            flash("用量必须是大于 0 的数字（%s）" % e, "danger")
            return redirect(url_for("dishes.dish_recipe", did=did))
        flash("配方已保存：%d 种原料" % n, "success")
        return redirect(url_for("dishes.dish_recipe", did=did))
    ings = conn.execute("SELECT * FROM ingredients ORDER BY id").fetchall()
    cur = {r["ingredient_id"]: r["qty"] for r in conn.execute(
        "SELECT ingredient_id, qty FROM dish_recipes WHERE dish_id=?", (did,))}
    return render_template("admin/recipe.html", dish=dish, ingredients=ings,
                           cur=cur, avail=utils.dish_availability(conn).get(did))


@bp.route("/recipes/import", methods=["GET", "POST"])
@admin_required
def recipes_import():
    """配方批量导入（CSV）。207 道菜手工录不现实，批量导入是唯一可行路径。"""
    import csv
    import io as _io
    from flask import send_file
    conn = db.get_db()

    def build_csv(rows, name):
        buf = _io.StringIO()
        w = csv.writer(buf)
        w.writerow(["菜品名称", "食材名称", "单份用量"])
        for r in rows:
            w.writerow(r)
        data = ("\ufeff" + buf.getvalue()).encode("utf-8")
        return send_file(_io.BytesIO(data), mimetype="text/csv",
                         as_attachment=True, download_name=name)

    if request.args.get("tpl"):
        return build_csv([["春归燕招牌鸡", "土鸡", "0.5"],
                          ["春归燕招牌鸡", "生姜", "0.02"],
                          ["红烧肉", "五花肉", "0.3"]], "菜品配方模板.csv")
    if request.args.get("export"):
        rows = [[r["d"], r["i"], r["q"]] for r in conn.execute(
            "SELECT d.name d, i.name i, r.qty q FROM dish_recipes r"
            " JOIN dishes d ON d.id=r.dish_id JOIN ingredients i ON i.id=r.ingredient_id"
            " ORDER BY d.name, i.name")]
        return build_csv(rows, "当前菜品配方.csv")

    result = None
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("请选择 CSV 文件", "danger")
        else:
            raw = f.read()
            text = None
            for enc in ("utf-8-sig", "utf-8", "gbk"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                flash("文件编码无法识别，请另存为 UTF-8 或 GBK 的 CSV", "danger")
            else:
                dishes = {r["name"]: r["id"] for r in conn.execute(
                    "SELECT id, name FROM dishes")}
                ings = {r["name"]: r["id"] for r in conn.execute(
                    "SELECT id, name FROM ingredients")}
                plan, bad_dish, bad_ing, bad_qty = {}, [], [], []
                for row in csv.reader(_io.StringIO(text)):
                    row = [c.strip() for c in row]
                    if len(row) < 3 or not row[0] or not row[1]:
                        continue
                    if row[0] in ("菜品名称", "菜品", "dish"):
                        continue
                    if row[0] not in dishes:
                        if row[0] not in bad_dish:
                            bad_dish.append(row[0])
                        continue
                    if row[1] not in ings:
                        if row[1] not in bad_ing:
                            bad_ing.append(row[1])
                        continue
                    try:
                        q = float(row[2])
                    except ValueError:
                        bad_qty.append("%s/%s=%s" % (row[0], row[1], row[2]))
                        continue
                    if q <= 0:
                        bad_qty.append("%s/%s=%s" % (row[0], row[1], row[2]))
                        continue
                    plan.setdefault(dishes[row[0]], []).append((ings[row[1]], q))
                if plan:
                    with conn:
                        for did, items in plan.items():
                            conn.execute("DELETE FROM dish_recipes WHERE dish_id=?", (did,))
                            for iid, q in items:
                                conn.execute(
                                    "INSERT INTO dish_recipes(dish_id, ingredient_id, qty)"
                                    " VALUES(?,?,?)", (did, iid, round(q, 3)))
                        utype, ukey, uname = utils.current_user()
                        utils.log_op(conn, "编辑", utype, ukey, uname,
                                     "配方批量导入：%d 道菜 / %d 条配方（原配方已覆盖）"
                                     % (len(plan), sum(len(v) for v in plan.values())),
                                     request.remote_addr or "")
                result = {"dishes": len(plan),
                          "items": sum(len(v) for v in plan.values()),
                          "bad_dish": bad_dish[:20], "bad_ing": bad_ing[:20],
                          "bad_qty": bad_qty[:20],
                          "n_bad_dish": len(bad_dish), "n_bad_ing": len(bad_ing),
                          "n_bad_qty": len(bad_qty)}
                flash("导入完成：%d 道菜 / %d 条配方" % (result["dishes"], result["items"]),
                      "success" if plan else "warning")
    have = conn.execute("SELECT COUNT(DISTINCT dish_id) FROM dish_recipes").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM dishes").fetchone()[0]
    return render_template("admin/recipes_import.html", result=result,
                           have=have, total=total)

# ---------- 菜品规格（同一主料不同做法 / 份量）----------

@bp.route("/dishes/<int:did>/specs", methods=["GET", "POST"])
@admin_required
def dish_specs(did):
    """规格组：一道菜下挂多个规格，价格 = 菜价 + 规格加价。
    例：鸭 → 笋干烧 +0 / 啤酒烧 +10 / 肚条烧 +25"""
    conn = db.get_db()
    dish = conn.execute("SELECT * FROM dishes WHERE id=?", (did,)).fetchone()
    if not dish:
        flash("菜品不存在", "danger")
        return redirect(url_for("dishes.dishes"))
    if dish["price_mode"] == "market":
        flash("时价菜价格本身待定，不支持规格", "warning")
        return redirect(url_for("dishes.dishes"))
    if request.method == "POST":
        names = request.form.getlist("spec_name")
        deltas = request.form.getlist("spec_delta")
        default_idx = request.form.get("spec_default", "0")
        items = []
        for i, nm in enumerate(names):
            nm = (nm or "").strip()
            if not nm:
                continue
            try:
                dl = utils.money(deltas[i] if i < len(deltas) else 0)
            except (TypeError, ValueError):
                dl = 0.0
            items.append((nm, dl))
        try:
            with conn:
                conn.execute("DELETE FROM dish_specs WHERE dish_id=?", (did,))
                for i, (nm, dl) in enumerate(items):
                    if utils.money(dish["price"]) + dl < 0:
                        raise ValueError("「%s」加价后价格不能为负" % nm)
                    conn.execute(
                        "INSERT INTO dish_specs(dish_id, name, price_delta,"
                        " is_default, sort_order) VALUES(?,?,?,?,?)",
                        (did, nm, dl, 1 if str(i) == str(default_idx) else 0, i))
                utype, ukey, uname = utils.current_user()
                utils.log_op(conn, "编辑", utype, ukey, uname,
                             "菜品「%s」规格更新：%d 个" % (dish["name"], len(items)),
                             request.remote_addr or "")
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(url_for("dishes.dish_specs", did=did))
        flash("规格已保存：%d 个" % len(items), "success")
        return redirect(url_for("dishes.dish_specs", did=did))
    specs = conn.execute("SELECT * FROM dish_specs WHERE dish_id=?"
                         " ORDER BY sort_order, id", (did,)).fetchall()
    return render_template("admin/specs.html", dish=dish, specs=specs)
