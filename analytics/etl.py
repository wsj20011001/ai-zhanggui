# -*- coding: utf-8 -*-
"""预聚合 ETL：把交易表折叠成日粒度事实表。

为什么要有这一层：
- 看板要画 180 天趋势，直接扫 order_items（十万行）会随数据增长线性变慢；
- 折叠后 daily_facts 只有 180 行，首屏稳定在毫秒级；
- 更重要的是"口径收敛"：报表、看板、AI 读的是同一张事实表，不会各算各的。
"""
from datetime import date, datetime, timedelta

from analytics.metrics import dish_cost_map
from utils import money, now_str


def _range_dates(d1, d2):
    a, b = d1, d2
    out, cur = [], a
    while cur <= b:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def rebuild(conn, d1, d2, verbose=False):
    """重算 [d1, d2] 区间的事实表（先删后建，幂等）"""
    d1s, d2s = str(d1)[:10], str(d2)[:10]
    costs = dish_cost_map(conn)

    conn.execute("DELETE FROM daily_facts WHERE d BETWEEN ? AND ?", (d1s, d2s))
    conn.execute("DELETE FROM dish_daily_facts WHERE d BETWEEN ? AND ?", (d1s, d2s))
    conn.execute("DELETE FROM hour_facts WHERE d BETWEEN ? AND ?", (d1s, d2s))

    # ---- 订单级聚合 ----
    rows = conn.execute(
        "SELECT id, substr(COALESCE(settled_at, created_at), 1, 10) d,"
        "       final_amount, discount_total, persons, member_id,"
        "       CAST(substr(COALESCE(settled_at, created_at), 12, 2) AS INTEGER) hh"
        " FROM orders WHERE status='settled'"
        "   AND substr(COALESCE(settled_at, created_at), 1, 10) BETWEEN ? AND ?",
        (d1s, d2s)).fetchall()

    daily = {}
    hours = {}
    for o in rows:
        d = o["d"]
        cur = daily.setdefault(d, {"revenue": 0.0, "orders": 0, "persons": 0,
                                   "discount": 0.0, "cost": 0.0, "member_orders": 0,
                                   "lunch": 0.0, "dinner": 0.0})
        amt = float(o["final_amount"] or 0)
        cur["revenue"] += amt
        cur["orders"] += 1
        cur["persons"] += int(o["persons"] or 0)
        cur["discount"] += float(o["discount_total"] or 0)
        if o["member_id"]:
            cur["member_orders"] += 1
        if 11 <= (o["hh"] or 0) < 16:
            cur["lunch"] += amt
        elif (o["hh"] or 0) >= 16:
            cur["dinner"] += amt
        hk = (d, o["hh"] or 0)
        h = hours.setdefault(hk, [0, 0, 0.0])
        h[0] += 1
        h[1] += int(o["persons"] or 0)
        h[2] += amt

    # ---- 明细聚合（成本按配方单价 × 数量）----
    items = conn.execute(
        "SELECT substr(o.settled_at, 1, 10) d, oi.dish_id, oi.name, oi.qty,"
        "       oi.price, c.name cat"
        " FROM order_items oi JOIN orders o ON o.id = oi.order_id"
        " JOIN dishes d2 ON d2.id = oi.dish_id"
        " JOIN categories c ON c.id = d2.category_id"
        " WHERE o.status='settled' AND oi.is_combo_child=0 AND oi.dish_id IS NOT NULL"
        "   AND substr(o.settled_at, 1, 10) BETWEEN ? AND ?",
        (d1s, d2s)).fetchall()

    dish = {}
    for it in items:
        key = (it["d"], it["dish_id"])
        qty = int(it["qty"] or 0)
        amount = float(it["price"] or 0) * qty
        cost = costs.get(it["dish_id"], 0.0) * qty if it["dish_id"] in costs else 0.0
        cur = dish.setdefault(key, {"name": it["name"], "cat": it["cat"] or "",
                                    "qty": 0, "amount": 0.0, "cost": 0.0})
        cur["qty"] += qty
        cur["amount"] += amount
        cur["cost"] += cost
        d = it["d"]
        daily[d]["cost"] += cost

    now = now_str()
    for d, v in daily.items():
        conn.execute(
            "INSERT INTO daily_facts(d, revenue, orders, persons, discount, cost, profit,"
            " member_orders, lunch_revenue, dinner_revenue, built_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (d, money(v["revenue"]), v["orders"], v["persons"], money(v["discount"]),
             money(v["cost"]), money(v["revenue"] - v["cost"]), v["member_orders"],
             money(v["lunch"]), money(v["dinner"]), now))
    for (d, did), v in dish.items():
        conn.execute(
            "INSERT INTO dish_daily_facts(d, dish_id, name, category, qty, amount, cost, profit)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (d, did, v["name"], v["cat"], v["qty"], money(v["amount"]),
             money(v["cost"]), money(v["amount"] - v["cost"])))
    for (d, hh), v in hours.items():
        conn.execute("INSERT INTO hour_facts(d, hour, orders, persons, revenue)"
                     " VALUES(?,?,?,?,?)", (d, hh, v[0], v[1], money(v[2])))
    conn.commit()
    if verbose:
        print("  facts rebuilt %s ~ %s：%d 天 / %d 条菜品明细"
              % (d1s, d2s, len(daily), len(dish)))
    return len(daily)


def rebuild_all(conn, days=400, verbose=False):
    """按订单实际范围全量重建（首次生成演示数据后调用）"""
    r = conn.execute("SELECT MIN(substr(COALESCE(settled_at, created_at),1,10)) a,"
                     " MAX(substr(COALESCE(settled_at, created_at),1,10)) b"
                     " FROM orders WHERE status='settled'").fetchone()
    if not r or not r["a"]:
        return 0
    return rebuild(conn, r["a"], r["b"], verbose=verbose)


def ensure_fresh(conn, days=3, verbose=False):
    """增量补齐最近 N 天（收银后立即能看到看板变化，不必等定时任务）"""
    today = date.today()
    missing = []
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        row = conn.execute("SELECT 1 FROM daily_facts WHERE d=?", (d,)).fetchone()
        if not row:
            missing.append(d)
    if not missing:
        return 0
    return rebuild(conn, min(missing), max(missing), verbose=verbose)


def day_gap(conn):
    """事实表里最后一个有数据的日期（用于界面提示"数据截至"）"""
    r = conn.execute("SELECT MAX(d) d FROM daily_facts").fetchone()
    return r["d"] if r else None
