# -*- coding: utf-8 -*-
"""演示数据生成器 —— 180 天营业数据（固定随机种子，可复现）

为什么需要它：真实门店库里通常只有几周数据，做不出趋势、环比和预测。
为了让「数据分析 + AI 洞察」可被验证，这里按真实餐饮经营规律合成一套数据：
星期效应、节假日高峰、雨天客流下滑、菜品帕累托分布、会员复购。

同时刻意植入 4 个"故事"，让驾驶舱的洞察是从数据里算出来的，而不是硬编码：
  1. 最近 7 天营收比同星期基线低约 22%
  2. 3 道菜最近 6 天 0 销量（此前销量稳定）
  3. 1 种主力食材库存只够约 1.5 天
  4. 最近 10 天折扣率从约 8% 抬到约 16%

用法：
    python seed_demo.py            # 生成 180 天（会先清空订单/会员/事实表）
    python seed_demo.py --days 90
"""
import argparse
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta

from config import DB_PATH
from utils import money, now_str

SEED = 20261020
SURNAMES = "王李张刘陈杨黄赵吴周徐孙马朱胡林郭何高罗郑梁谢宋唐许韩冯邓曹彭曾萧田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
GIVEN = "伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英华玉兰萍红娥玲芬燕彬鹏辉建国志强晓东雨欣子涵浩然思远嘉怡"

HOLIDAYS = set()
for a, b in [(date(2026, 5, 1), date(2026, 5, 5)),
             (date(2026, 6, 19), date(2026, 6, 21)),
             (date(2026, 9, 25), date(2026, 9, 27)),
             (date(2026, 10, 1), date(2026, 10, 7))]:
    d = a
    while d <= b:
        HOLIDAYS.add(d)
        d += timedelta(days=1)

DOW_FACTOR = {0: 0.72, 1: 0.86, 2: 0.90, 3: 0.94, 4: 1.28, 5: 1.42, 6: 1.18}
PAY_METHODS = [("wechat", 0.54), ("alipay", 0.20), ("cash", 0.14),
               ("balance", 0.07), ("credit", 0.05)]


def _pick_pay(rng):
    x, acc = rng.random(), 0.0
    for m, p in PAY_METHODS:
        acc += p
        if x <= acc:
            return m
    return "wechat"


def _tiers(conn):
    row = conn.execute("SELECT value FROM settings WHERE key='full_reduction'").fetchone()
    spec = row["value"] if row else "100:10,200:25"
    out = []
    for part in (spec or "").split(","):
        if ":" in part:
            try:
                th, off = part.split(":", 1)
                out.append((float(th), float(off)))
            except ValueError:
                pass
    return sorted(out)


def load_menu(conn):
    """读取可用于模拟的菜品。

    只取"有配方 + 价格合理"的菜：没有配方就没有成本，毛利分析会失真；
    高端酒水（茅台/年份红酒）会把客单价拉到不合理区间，演示里排除。
    """
    rng = random.Random(SEED)
    cat_factor = {"春归燕特色": 1.25, "热菜": 1.5, "凉菜": 1.0, "汤品": 0.75,
                  "主食": 0.95, "饮品": 0.7, "海鲜": 0.8, "精致小炒": 1.35,
                  "融合烧菜": 1.3, "赣菜": 1.15, "粤菜": 1.25, "广式靓汤": 0.8,
                  "时令蔬菜": 0.9, "饮料": 0.7}
    skip_cat = {"白酒", "红酒", "茶叶", "饮料"}
    dishes = []
    for d in conn.execute(
            "SELECT d.id, d.name, d.price, d.price_mode, d.is_recommend, d.is_featured,"
            "       c.name cat FROM dishes d JOIN categories c ON c.id = d.category_id"
            " WHERE d.on_sale = 1 AND d.price > 0 AND d.price <= 260"
            "   AND d.id IN (SELECT DISTINCT dish_id FROM dish_recipes)"):
        if d["cat"] in skip_cat:
            continue
        f = cat_factor.get(d["cat"], 0.9)
        w = (rng.paretovariate(1.6) * f
             * (1.45 if d["is_recommend"] else 1.0)
             * (1.35 if d["is_featured"] else 1.0))
        # 价格越高越少人点（需求弹性），避免客单价被贵菜拉飞
        w *= (120.0 / (120.0 + float(d["price"]))) ** 0.55
        dishes.append({"id": d["id"], "name": d["name"], "price": float(d["price"]),
                       "cat": d["cat"], "w": w})
    dishes.sort(key=lambda x: -x["w"])
    return dishes


def seed(conn, days=180):
    rng = random.Random(SEED)
    today = date.today()

    for t in ("order_items", "orders", "member_ledger", "members",
              "daily_facts", "dish_daily_facts", "hour_facts",
              "ai_insights", "ai_actions", "purchase_items", "purchase_orders"):
        conn.execute("DELETE FROM %s" % t)
    conn.execute("UPDATE tables SET status='idle'")
    conn.commit()

    dishes = load_menu(conn)
    if not dishes:
        print("没有可用菜品，请先启动一次 app.py 初始化数据库")
        return
    tiers = _tiers(conn)
    tables = conn.execute("SELECT id, name FROM tables ORDER BY id").fetchall()
    if not tables:
        print("没有桌台数据")
        return

    # ---------- 会员 ----------
    used = set()
    members = []
    for _ in range(220):
        while True:
            phone = "13%d%04d%04d" % (rng.choice([5, 6, 7, 8, 9]),
                                      rng.randint(0, 9999), rng.randint(0, 9999))
            if phone not in used:
                used.add(phone)
                break
        name = rng.choice(SURNAMES) + rng.choice(GIVEN)
        if rng.random() < 0.3:
            name += rng.choice(GIVEN)
        members.append({
            "name": name, "phone": phone,
            "balance": money(rng.choice([0, 0, 0, 100, 200, 500, 800])),
        })

    # ---------- 滞销剧情：挑 3 道热度中段的菜，最近 6 天不再出售 ----------
    mid = dishes[len(dishes) // 3: len(dishes) // 3 + 10]
    slow_ids = {d["id"] for d in mid[:3]}
    slow_start = today - timedelta(days=6)

    start = today - timedelta(days=days - 1)
    orders = []          # (tuple_for_insert, [(dish, qty), ...])
    seq = {}

    for i in range(days):
        day = start + timedelta(days=i)
        ds = day.isoformat()
        factor = 1.55 if day in HOLIDAYS else DOW_FACTOR[day.weekday()]
        factor *= 1 + 0.0009 * i
        if rng.random() < 0.12:
            factor *= 0.80
        drop = 0.82 if (today - day).days <= 6 else 1.0
        n_orders = max(6, int(rng.gauss(34 * factor * drop, 4)))
        if day == today:
            n_orders = 7                     # 今天只营业到午市，数据不完整

        for _ in range(n_orders):
            persons = rng.choices([2, 3, 4, 5, 6, 8], [30, 26, 24, 10, 7, 3])[0]
            n_items = max(1, min(7, int(rng.gauss(1.5 + persons * 0.75, 1.0))))
            lunch = rng.random() < 0.42
            hh = rng.randint(11, 13) if lunch else rng.randint(17, 20)
            if day == today:
                hh = rng.randint(11, 12)          # 今天只到午市
            created = datetime(day.year, day.month, day.day, hh,
                               rng.randint(0, 59), rng.randint(0, 59))
            settled = created + timedelta(minutes=rng.randint(35, 95))

            member_p = 0.46 if (today - day).days <= 9 else 0.31
            member = rng.choice(members) if rng.random() < member_p else None

            pool = [d for d in dishes
                    if not (d["id"] in slow_ids and day >= slow_start)]
            weights = [d["w"] for d in pool]
            picked, total = [], 0.0
            for _k in range(n_items):
                if not pool:
                    break
                d = rng.choices(pool, weights=weights, k=1)[0]
                k = pool.index(d)
                pool.pop(k)
                weights.pop(k)
                qty = 2 if (rng.random() < 0.13 and d["price"] < 40) else 1
                total += d["price"] * qty
                picked.append((d, qty))
            if not picked:
                continue

            reduction = 0.0
            for th, off in tiers:
                if total >= th:
                    reduction = off
            member_discount = money((total - reduction) * 0.05) if member else 0.0
            extra = 12.0 if ((today - day).days <= 9 and rng.random() < 0.45) else 0.0
            discount_total = money(reduction + member_discount + extra)
            final = money(max(0.0, total - discount_total))

            seq[ds] = seq.get(ds, 0) + 1
            order_no = "D%s-%04d" % (day.strftime("%Y%m%d"), seq[ds])
            t = rng.choice(tables)
            row = (order_no, t["id"], t["name"], persons, "settled",
                   money(total), money(reduction), money(member_discount), 0.0,
                   discount_total, final, _pick_pay(rng),
                   member, int(final),
                   created.strftime("%Y-%m-%d %H:%M:%S"),
                   settled.strftime("%Y-%m-%d %H:%M:%S"))
            orders.append((row, picked))

    # ---------- 会员入库 ----------
    member_id = {}
    for m in members:
        cur = conn.execute(
            "INSERT INTO members(name, phone, balance, points, total_spend, level,"
            " created_at, last_visit_at, remark) VALUES(?,?,?,0,0,'普通会员',?,NULL,?)",
            (m["name"], m["phone"], m["balance"],
             (today - timedelta(days=rng.randint(10, 400))).isoformat() + " 12:00:00",
             "演示数据"))
        member_id[id(m)] = cur.lastrowid
    conn.commit()

    # ---------- 订单入库 ----------
    order_rows = []
    for row, _picked in orders:
        r = list(row)
        r[12] = member_id.get(id(r[12])) if r[12] is not None else None
        order_rows.append(tuple(r))
    conn.executemany(
        "INSERT INTO orders(order_no, table_id, table_name, persons, status,"
        " total_amount, reduction, member_discount, points_discount, discount_total,"
        " final_amount, pay_method, member_id, points_earned, created_at, settled_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", order_rows)

    oid = {r["order_no"]: r["id"] for r in conn.execute(
        "SELECT id, order_no FROM orders")}
    item_rows = []
    for row, picked in orders:
        o = oid.get(row[0])
        if not o:
            continue
        for sort, (d, qty) in enumerate(picked, 1):
            item_rows.append((o, d["id"], d["name"], d["price"], qty, sort))
    conn.executemany(
        "INSERT INTO order_items(order_id, dish_id, name, price, qty, sort_order)"
        " VALUES(?,?,?,?,?,?)", item_rows)

    # 会员累计消费与最近到店（按订单重算，保证一致）
    conn.execute(
        "UPDATE members SET total_spend = COALESCE(("
        "  SELECT ROUND(SUM(final_amount),2) FROM orders o"
        "  WHERE o.member_id = members.id AND o.status='settled'),0),"
        " last_visit_at = (SELECT MAX(settled_at) FROM orders o"
        "  WHERE o.member_id = members.id AND o.status='settled')")
    conn.execute("UPDATE members SET level = CASE"
                 " WHEN total_spend >= 6000 THEN '钻石会员'"
                 " WHEN total_spend >= 3000 THEN '金卡会员'"
                 " WHEN total_spend >= 1000 THEN '银卡会员' ELSE '普通会员' END")
    # 制造"高价值会员 60 天未到店"（流失预警）
    conn.execute(
        "UPDATE members SET last_visit_at=? WHERE id IN ("
        " SELECT id FROM members ORDER BY total_spend DESC LIMIT 40 OFFSET 12)",
        ((today - timedelta(days=rng.randint(70, 150))).isoformat() + " 19:30:00",))

    # ---------- 今日在办订单（后厨/订单页不为空） ----------
    for i, st in enumerate(["pending", "making", "done"]):
        n = seq.get(today.isoformat(), 0) + i + 1
        order_no = "D%s-%04d" % (today.strftime("%Y%m%d"), n)
        t = tables[i % len(tables)]
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "INSERT INTO orders(order_no, table_id, table_name, persons, status,"
            " total_amount, final_amount, remark, created_at)"
            " VALUES(?,?,?,?,?,0,0,?,?)",
            (order_no, t["id"], t["name"], 3 + i, st, "演示：在办订单", ts))
        o = cur.lastrowid
        sub = [dishes[0], dishes[1], dishes[5]]
        for sort, d in enumerate(sub, 1):
            conn.execute("INSERT INTO order_items(order_id, dish_id, name, price, qty,"
                         " is_combo_child, sort_order) VALUES(?,?,?,?,1,0,?)",
                         (o, d["id"], d["name"], d["price"], sort))
        conn.execute("UPDATE orders SET total_amount=(SELECT ROUND(SUM(price*qty),2)"
                     " FROM order_items WHERE order_id=?), final_amount=("
                     " SELECT ROUND(SUM(price*qty),2) FROM order_items WHERE order_id=?)"
                     " WHERE id=?", (o, o, o))
        conn.execute("UPDATE tables SET status=? WHERE id=?",
                     ("occupied" if st in ("pending", "making") else "idle", t["id"]))

    conn.commit()

    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('demo_mode','1')")
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('shop_seats','42')")

    # ---------- 成本校准 ----------
    # 原始食材单位成本是示例值，直接算出来的毛利率高达 80%+，不像真实餐厅。
    # 这里按"食品成本率 38%"反推一个整体系数，让毛利、毛利预警看起来可信。
    # 注意：系数是幂等推导的（按当前成本算比例再缩放），重复执行不会越跑越大。
    cal = conn.execute(
        "SELECT COALESCE(SUM(oi.qty * oi.price),0) amt FROM order_items oi"
        " JOIN orders o ON o.id = oi.order_id WHERE o.status='settled'").fetchone()["amt"]
    cost_rows = conn.execute(
        "SELECT oi.dish_id, oi.qty FROM order_items oi"
        " JOIN orders o ON o.id = oi.order_id WHERE o.status='settled'").fetchall()
    from analytics.metrics import dish_cost_map
    cmap = dish_cost_map(conn)
    cost_sum = sum(cmap.get(r["dish_id"], 0.0) * r["qty"] for r in cost_rows)
    if cal and cost_sum:
        ratio = cost_sum / cal
        target_cost_ratio = 0.38
        k = target_cost_ratio / ratio
        conn.execute("UPDATE ingredients SET cost = ROUND(cost * ?, 4)", (k,))
        print("  成本校准：当前成本率 %.1f%% → 目标 38.0%%（系数 %.3f）"
              % (ratio * 100, k))
    conn.commit()

    # 先折叠事实表，再基于真实消耗速度制造"库存只够 1.5 天"的剧情
    from analytics.etl import rebuild_all
    rebuild_all(conn, verbose=True)

    from analytics.metrics import q_stock_risk
    risk = [r for r in q_stock_risk(conn)
            if r["daily_use"] > 0 and r["stock_value"] > 0 and r["cost"] > 50]
    if risk:
        t0 = risk[0]
        new_stock = round(t0["daily_use"] * 1.5, 2)
        conn.execute("UPDATE ingredients SET stock=?, warn_threshold=?, updated_at=?"
                     " WHERE id=?",
                     (new_stock, round(t0["daily_use"] * 1.8, 2), now_str(),
                      t0["ingredient_id"]))
        print("  库存剧情：%s 只剩 %.1f %s（约 1.5 天）"
              % (t0["name"], new_stock, t0["unit"]))
        for r in risk[1:3]:
            conn.execute("UPDATE ingredients SET stock=?, warn_threshold=? WHERE id=?",
                         (round(r["daily_use"] * 2.6, 2), round(r["daily_use"] * 3.0, 2),
                          r["ingredient_id"]))
        conn.commit()

    print("  完成：%d 单 / %d 条明细 / %d 位会员"
          % (len(orders), len(item_rows), len(members)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    args = ap.parse_args()
    if not DB_PATH.exists():
        print("数据库不存在，请先启动一次 app.py 初始化")
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        seed(conn, days=args.days)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
