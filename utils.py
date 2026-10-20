# -*- coding: utf-8 -*-
"""通用工具：时间、局域网 IP、密码、金额计算、订单号、备份恢复"""
import hashlib
import hmac
import os
import shutil
import socket
import sqlite3
from datetime import datetime
from pathlib import Path
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from werkzeug.security import check_password_hash, generate_password_hash

from config import BACKUP_DIR, DB_PATH, RESTORE_FLAG, RESTORE_PENDING


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def lan_ip():
    """取局域网 IP：UDP connect 不发包，无外网也可用；失败回退 127.0.0.1"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# ---- 密码 ---------------------------------------------------------------
# 历史版本用裸 SHA-256 存储（无盐、无迭代，彩虹表可反查）。
# 现在改为 werkzeug pbkdf2-sha256 加盐迭代；老哈希在首次成功登录后自动升级。

def hash_pwd(raw):
    """[旧格式] 裸 SHA-256，仅用于兼容校验历史数据，不要再用于新密码"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_pwd_hash(raw):
    """新密码哈希：pbkdf2-sha256 + 随机盐 + 迭代"""
    return generate_password_hash(raw, method="pbkdf2:sha256:260000")


def verify_pwd(stored, raw):
    """校验密码，兼容新旧两种存储格式"""
    if not stored:
        return False
    if stored.startswith(("pbkdf2:", "scrypt:", "argon2")):
        try:
            return check_password_hash(stored, raw)
        except (ValueError, TypeError):
            return False
    return hmac.compare_digest(hash_pwd(raw), stored)


def needs_rehash(stored):
    """老格式（裸 SHA-256）需要升级"""
    return bool(stored) and not stored.startswith(("pbkdf2:", "scrypt:", "argon2"))


OP_TYPES = ["登录", "修改密码", "下单", "新增", "编辑", "删除", "结算",
            "状态变更", "充值", "库存", "备份", "设置", "定价"]


def log_op(conn, op_type, user_type, user_key, user_name, detail, ip=""):
    """通用操作日志（调用方负责 commit）"""
    conn.execute(
        "INSERT INTO op_logs(op_type, user_type, user_key, user_name, detail,"
        " ip, created_at) VALUES(?,?,?,?,?,?,?)",
        (op_type, user_type, user_key or "", user_name or "",
         str(detail)[:200], ip or "", now_str()))


def log_login(conn, user_type, user_key, name, success, ip=""):
    """登录日志：user_type=admin(管理端)/employee(点餐端)"""
    log_op(conn, "登录", user_type, user_key, name,
           "登录成功" if success else "登录失败", ip)


def current_user():
    """当前操作人：根据 session 返回 (user_type, user_key, user_name)"""
    from flask import session
    if session.get("emp_logged_in"):
        return ("employee", session.get("emp_no", ""), session.get("emp_name", ""))
    if session.get("admin_logged_in"):
        u = get_setting("admin_user", "admin")
        return ("admin", u, u)
    return ("unknown", "", "")


def money(x):
    """金额统一两位小数，四舍五入（ROUND_HALF_UP）。

    不能用内置 round()：它是"银行家舍入"（round(2.5) == 2、round(0.125, 2) == 0.12），
    与国内财务口径不一致，2.5 分会被舍成 2 分造成累计少收。
    改用 Decimal 精确进位，避免二进制浮点误差。
    """
    if x is None:
        return 0.0
    try:
        d = Decimal(str(x))
        if not d.is_finite():
            return 0.0
        return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        return 0.0


def parse_full_reduction(spec):
    """解析满减配置 "100:10,200:25" → [(100,10),(200,25)] 按阈值升序"""
    tiers = []
    for part in (spec or "").split(","):
        part = part.strip()
        if ":" not in part:
            continue
        try:
            th, off = part.split(":", 1)
            tiers.append((float(th), float(off)))
        except ValueError:
            continue
    return sorted(tiers)


def get_setting(key, default=""):
    from db import get_db
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def get_settings():
    from db import get_db
    return {r["key"]: r["value"] for r in get_db().execute("SELECT * FROM settings")}


def calc_order(items, member_discount_rate=1.0, points_to_use=0, use_member=False):
    """全系统唯一金额口径。items: [{price, qty}, ...]
    返回 dict: total / reduction / member_discount / points_discount /
               discount_total / final / points_earned / points_actually_used
    """
    total = money(sum(money(i["price"]) * int(i["qty"]) for i in items))
    # 1. 满减：取 total ≥ 阈值的最高档
    reduction = 0.0
    for th, off in parse_full_reduction(get_setting("full_reduction")):
        if total >= th:
            reduction = money(off)
    # 2. 会员折扣
    member_discount = 0.0
    if use_member:
        rate = float(get_setting("member_discount", "1") or 1)
        member_discount = money((total - reduction) * (1 - rate))
    # 3. 积分抵扣（默认 100 分抵 1 元；抵扣额不超过剩余应付）
    points_discount = 0.0
    points_actually_used = 0
    try:
        want_points = max(0, int(points_to_use or 0))
    except (TypeError, ValueError):
        want_points = 0
    if want_points > 0:
        ratio = int(float(get_setting("points_money_ratio", "100") or 100))
        if ratio > 0:
            max_deduct = max(0.0, money(total - reduction - member_discount))
            # 以"分"为单位向下取整，确保抵扣额永远不超过应付
            usable = min(want_points, int(max_deduct * ratio + 1e-9))
            points_discount = money(usable / ratio)
            points_actually_used = usable
    discount_total = money(reduction + member_discount + points_discount)
    final = money(total - discount_total)
    earn_per = float(get_setting("points_earn_per_yuan", "1") or 0)
    points_earned = int(final * earn_per) if final > 0 else 0
    return {
        "total": total, "reduction": reduction, "member_discount": member_discount,
        "points_discount": money(points_discount),
        "discount_total": discount_total, "final": final,
        "points_earned": points_earned,
        "points_actually_used": points_actually_used,
    }


def make_order_no(conn=None):
    """生成订单号 D20261008-0001（日期 + 当日序号）。

    必须用独立的计数表 + 独立短事务取号：
    旧实现用 COUNT(*)+1，并发下多个请求会取到同一个计数，
    撞 orders.order_no 的 UNIQUE 约束直接 500（实测 6 桌并发挂 4 桌）。
    计数表在 SQLite 写锁内自增，天然串行；即使订单后续失败，
    也只是跳号（无副作用），绝不会重号。
    """
    today = datetime.now().strftime("%Y%m%d")
    c = sqlite3.connect(str(DB_PATH), isolation_level="IMMEDIATE", timeout=10.0)
    try:
        c.execute("INSERT INTO counters(name, value) VALUES(?, 1)"
                  " ON CONFLICT(name) DO UPDATE SET value = value + 1", (today,))
        n = c.execute("SELECT value FROM counters WHERE name=?", (today,)).fetchone()[0]
        c.commit()
    finally:
        c.close()
    return "D%s-%04d" % (today, n)


def pending_price_items(conn, order_id):
    """该订单里尚未定价的时价条目"""
    return conn.execute(
        "SELECT * FROM order_items WHERE order_id=? AND price_pending=1"
        " ORDER BY sort_order", (order_id,)).fetchall()


def order_has_pending_price(conn, order_id):
    """订单是否含待定价的时价菜"""
    return conn.execute(
        "SELECT COUNT(*) c FROM order_items WHERE order_id=? AND price_pending=1",
        (order_id,)).fetchone()["c"] > 0


def recalc_order_amounts(conn, order_id):
    """按当前明细重算订单金额（补价后调用）。
    口径与下单时一致：不含会员折扣，满减按当前配置。调用方负责 commit。"""
    items = conn.execute(
        "SELECT price, qty FROM order_items WHERE order_id=? AND is_combo_child=0",
        (order_id,)).fetchall()
    calc = calc_order([{"price": i["price"], "qty": i["qty"]} for i in items])
    conn.execute("UPDATE orders SET total_amount=?, final_amount=? WHERE id=?",
                 (calc["total"], calc["final"], order_id))
    return calc


# ---- 菜品配方(BOM)、成本与推荐 ----------------------------------------

def current_season():
    """按月份推断当季：3-5 春 / 6-8 夏 / 9-11 秋 / 12-2 冬"""
    m = datetime.now().month
    return "春" if m in (3, 4, 5) else "夏" if m in (6, 7, 8) \
        else "秋" if m in (9, 10, 11) else "冬"


def active_season():
    """当前生效季节：设置里指定则优先，否则按月份自动推断"""
    return get_setting("current_season", "").strip() or current_season()


def recipe_requirements(conn):
    """{dish_id: [(ingredient_id, 单份用量), ...]}

    普通菜 = 自身配方；
    套餐   = Σ(子菜配方 × 子菜数量)，与套餐自身配方合并。
    """
    base = {}
    for r in conn.execute("SELECT dish_id, ingredient_id, qty FROM dish_recipes"):
        if r["qty"] and r["qty"] > 0:
            base.setdefault(r["dish_id"], []).append((r["ingredient_id"], float(r["qty"])))
    out = {k: list(v) for k, v in base.items()}
    for c in conn.execute("SELECT combo_id, dish_id, qty FROM combo_items"):
        need = {}
        for iid, q in base.get(c["dish_id"], []):
            need[iid] = need.get(iid, 0.0) + q * (c["qty"] or 1)
        for iid, q in need.items():
            out.setdefault(c["combo_id"], []).append((iid, q))
    return out


def dish_availability(conn):
    """{dish_id: {can_make, cost, margin, margin_rate, limited_by}}

    只包含"已录配方"的菜品。can_make = min(各原料库存 ÷ 单份用量)，
    即"按现有库存最多还能做几份"；无配方的菜不参与缺料限制。
    """
    req = recipe_requirements(conn)
    ing = {r["id"]: r for r in conn.execute(
        "SELECT id, name, unit, stock, cost FROM ingredients")}
    price = {r["id"]: (r["price"] or 0) for r in conn.execute(
        "SELECT id, price FROM dishes")}
    out = {}
    for did, needs in req.items():
        if not needs:
            continue
        can, cost, lim = None, 0.0, ""
        for iid, q in needs:
            i = ing.get(iid)
            if i is None or q <= 0:
                continue
            n = (i["stock"] or 0) / q
            if can is None or n < can:
                can, lim = n, i["name"]
            cost += q * (i["cost"] or 0)
        cost = money(cost)
        p = money(price.get(did, 0))
        out[did] = {
            "can_make": max(0, int(can)) if can is not None else None,
            "cost": cost,
            "margin": money(p - cost),
            "margin_rate": round((p - cost) / p, 4) if p > 0 else 0.0,
            "limited_by": lim,
        }
    return out


def consume_stock_for_order(conn, order_id, order_no="", who=""):
    """出餐/结算时按配方扣料。

    幂等：同一订单只会扣一次（靠 stock_movements.order_id 判断）。
    套餐按子项扣料，套餐主项不重复扣。
    返回扣减的食材条数。
    """
    if conn.execute("SELECT COUNT(*) c FROM stock_movements WHERE order_id=?",
                    (order_id,)).fetchone()["c"]:
        return 0
    now = now_str()
    label = order_no or ("#%d" % order_id)
    rows = conn.execute(
        "SELECT dish_id, qty, is_combo_child FROM order_items"
        " WHERE order_id=? AND dish_id IS NOT NULL", (order_id,)).fetchall()
    n = 0
    for r in rows:
        if not r["is_combo_child"]:
            d = conn.execute("SELECT is_combo FROM dishes WHERE id=?",
                             (r["dish_id"],)).fetchone()
            if d is not None and d["is_combo"]:
                continue                     # 套餐主项：改用子项扣料
        for rec in conn.execute(
                "SELECT ingredient_id, qty FROM dish_recipes WHERE dish_id=?",
                (r["dish_id"],)).fetchall():
            use = round((rec["qty"] or 0) * (r["qty"] or 0), 3)
            if use <= 0:
                continue
            conn.execute("UPDATE ingredients SET stock=ROUND(stock-?,3), updated_at=?"
                         " WHERE id=?", (use, now, rec["ingredient_id"]))
            conn.execute(
                "INSERT INTO stock_movements(ingredient_id, type, qty, price, note,"
                " order_id, created_at) VALUES(?, 'out', ?, 0, ?, ?, ?)",
                (rec["ingredient_id"], use, "出餐扣料 %s" % label, order_id, now))
            n += 1
    return n


def recommend_scores(conn, mode="balanced", only_on_sale=True):
    """按策略给菜品打推荐分（0~100），降序返回。

    分数 = 基础分 + 利润权重×毛利率 + 库存权重×库存充足度 + 临期权重×紧迫度
    无配方的菜只给基础分（无法评估成本与库存，不参与主推）。
    """
    import datetime as _dt
    from config import REC_MODES, REC_STOCK_TARGET, EXPIRE_SOON_DAYS
    w_profit, w_stock, w_urgent = REC_MODES.get(mode, REC_MODES["balanced"])
    req = recipe_requirements(conn)
    avail = dish_availability(conn)
    ing = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, name, expire_at FROM ingredients")}   # Row -> dict，Row 没有 .get()
    today = _dt.date.today()
    sql = "SELECT id, name, price FROM dishes"
    if only_on_sale:
        sql += " WHERE on_sale=1"
    out = []
    for d in conn.execute(sql).fetchall():
        a = avail.get(d["id"])
        if not a:
            out.append({"dish_id": d["id"], "name": d["name"], "price": d["price"],
                        "score": 10.0, "reason": "未录配方", "can_make": None,
                        "margin": None, "margin_rate": None})
            continue
        score = 10.0
        score += w_profit * max(0.0, min(1.0, a["margin_rate"] or 0))
        cm = a["can_make"]
        score += w_stock * (min(1.0, cm / float(REC_STOCK_TARGET)) if cm else 0.0)
        urgent = 0.0
        for iid, _q in req.get(d["id"], []):
            e = (ing.get(iid) or {}).get("expire_at")
            if not e:
                continue
            try:
                days = (_dt.datetime.strptime(e[:10], "%Y-%m-%d").date() - today).days
            except ValueError:
                continue
            if days <= EXPIRE_SOON_DAYS:
                urgent = max(urgent, 1.0 - max(0, days) / float(EXPIRE_SOON_DAYS + 1))
        score += w_urgent * urgent
        bits = []
        if a["margin_rate"] is not None:
            bits.append("毛利 %d%%" % round(a["margin_rate"] * 100))
        if cm is not None:
            bits.append(("还可做 %d 份" % cm) if cm > 0 else "原料不足")
        if urgent > 0:
            bits.append("原料临期·优先消耗")
        out.append({"dish_id": d["id"], "name": d["name"], "price": d["price"],
                    "score": round(min(100.0, score), 1), "reason": " · ".join(bits),
                    "can_make": cm, "margin": a["margin"],
                    "margin_rate": a["margin_rate"]})
    out.sort(key=lambda x: (-x["score"], x["dish_id"]))
    return out


def plan_banquet(conn, persons, per_person, exclude_roles=(), must_ids=(),
                 mode="balanced"):
    """按人均标准自动排菜。

    思路：先按宴席结构分配预算（凉菜/热菜/主菜/汤/主食），
    再在每个环节内按策略选菜（主菜按价格挑撑场面的，其余按推荐分）。
    只做"建议"，最终由人确认。
    """
    from config import COURSE_BUDGET, COURSE_COUNT
    target = money(persons * per_person)
    if persons <= 4:
        counts = dict(COURSE_COUNT["small"])
    elif persons >= 10:
        counts = dict(COURSE_COUNT["large"])
    else:
        counts = dict(COURSE_COUNT["default"])
    roles = [r for r in counts if r not in exclude_roles]
    wsum = sum(COURSE_BUDGET.get(r, 0.1) for r in roles) or 1.0

    season = active_season()
    block_season = get_setting("seasonal_block_order", "1") == "1"
    avail = dish_availability(conn)
    score = {p["dish_id"]: p for p in recommend_scores(conn, mode)}
    must_ids = set(must_ids)

    pool = {}
    for d in conn.execute(
            "SELECT d.*, c.course_role role FROM dishes d"
            " JOIN categories c ON c.id=d.category_id WHERE d.on_sale=1"):
        role = (d["role"] or "").strip()
        if role not in roles:
            continue
        if d["price_mode"] == "market":
            continue          # 时价菜价格待定，不参与自动排菜
        if block_season and d["is_seasonal"] and (d["season"] or "") not in ("", season):
            continue
        a = avail.get(d["id"])
        if a and a["can_make"] is not None and a["can_make"] <= 0:
            continue
        pool.setdefault(role, []).append(dict(d))

    taken, courses = set(), []
    # 必须包含的菜先占位
    forced = {}
    for role in roles:
        for d in pool.get(role, []):
            if d["id"] in must_ids and d["id"] not in taken:
                forced.setdefault(role, []).append(d)
                taken.add(d["id"])
    for role in roles:
        cand = pool.get(role, [])
        if not cand:
            continue
        # 主菜：挑贵一点的撑场面；其余：按推荐分
        if role == "主菜":
            cand.sort(key=lambda x: -float(x["price"] or 0))
        else:
            cand.sort(key=lambda x: -float(score.get(x["id"], {}).get("score", 0) or 0))
        budget = target * (COURSE_BUDGET.get(role, 0.1) / wsum)
        need = int(counts[role])
        chosen = list(forced.get(role, []))
        used = sum(float(c["price"] or 0) for c in chosen)
        for d in cand:
            if len(chosen) >= need:
                break
            if d["id"] in taken:
                continue
            p = float(d["price"] or 0)
            if chosen and used + p > budget * 1.15:
                continue
            chosen.append(d); used += p; taken.add(d["id"])
        for d in sorted(cand, key=lambda x: float(x["price"] or 0)):
            if len(chosen) >= need:
                break
            if d["id"] in taken:
                continue
            p = float(d["price"] or 0)
            if used + p > budget * 1.35:
                continue
            chosen.append(d); used += p; taken.add(d["id"])
        if not chosen:
            continue
        items = []
        for c in chosen:
            sc = score.get(c["id"], {})
            items.append({
                "dish_id": c["id"], "name": c["name"], "price": money(c["price"]),
                "qty": 1, "role": role,
                "can_make": (avail.get(c["id"]) or {}).get("can_make"),
                "margin_rate": sc.get("margin_rate"),
                "reason": sc.get("reason", ""),
            })
        courses.append({"role": role, "dishes": items,
                        "budget": money(budget),
                        "subtotal": money(sum(i["price"] for i in items))})
    total = money(sum(c["subtotal"] for c in courses))
    return {"target": target, "total": total, "courses": courses,
            "counts": counts, "persons": persons, "per_person": money(per_person),
            "actual_per": money(total / persons) if persons else 0,
            "diff": money(total - target), "mode": mode}


def do_backup():
    """VACUUM INTO 生成一致性备份快照，返回备份文件名"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    fname = "restaurant_%s.db" % datetime.now().strftime("%Y%m%d_%H%M%S")
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("VACUUM INTO ?", (str(BACKUP_DIR / fname),))
    finally:
        conn.close()
    return fname


def apply_restore_pending():
    """应用待恢复备份（在启动时、无连接的情况下调用）。返回提示文本或 None"""
    if RESTORE_FLAG.exists():
        try:
            if RESTORE_PENDING.exists():
                # WAL 模式：必须同时清掉 -wal/-shm，否则旧库的预写日志
                # 会被套用到刚替换进来的新库上，造成数据错乱
                for suffix in ("-wal", "-shm"):
                    try:
                        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
                    except OSError:
                        pass
                os.replace(str(RESTORE_PENDING), str(DB_PATH))
                msg = "备份恢复成功"
            else:
                msg = "恢复文件缺失，已跳过"
        except OSError as e:
            msg = "恢复失败：" + str(e)
        finally:
            RESTORE_FLAG.unlink(missing_ok=True)
            RESTORE_PENDING.unlink(missing_ok=True)
        return msg
    return None


def request_restore(backup_path):
    """复制备份为 restore_pending.db 并写标记，下次启动生效"""
    shutil.copyfile(str(backup_path), str(RESTORE_PENDING))
    RESTORE_FLAG.write_text("pending", encoding="utf-8")


def check_reservations(db):
    """惰性预订扫描：30 分钟内未提醒的置 notified=1 并返回提醒列表；
    60 分钟未到店自动标记为已过期（UI 灰色，不改状态）"""
    now = datetime.now()
    remind, expired = [], []
    rows = db.execute(
        "SELECT * FROM reservations WHERE status='pending'").fetchall()
    for r in rows:
        try:
            t = datetime.strptime(r["reserve_time"], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        diff = (t - now).total_seconds()
        if -3600 < diff <= 1800 and not r["notified"]:
            db.execute("UPDATE reservations SET notified=1 WHERE id=?", (r["id"],))
            remind.append(r)
        if diff < -3600:
            expired.append(r)
    return remind, expired
