# -*- coding: utf-8 -*-
"""指标口径层 —— 全系统唯一的计算入口。

设计约束（这是本项目最重要的一条纪律）：
1. 任何页面、任何 AI 回答里的数字，都必须通过本模块取得；
2. 本模块只依赖 sqlite3 连接，不依赖 Flask 请求上下文，便于测试与后台任务复用；
3. 金额一律走 utils.money()（Decimal ROUND_HALF_UP），与收银口径保持同源。

对外函数命名以 `q_` 前缀表示"查询"，返回值统一为 dict / list[dict]，
方便直接 JSON 化给前端或喂给模型（模型永远拿不到原始表，只拿这里的结果）。
"""
from datetime import date, datetime, timedelta

from utils import money


# ---------------------------------------------------------------- 基础工具

def _d(x):
    """把 'YYYY-MM-DD' 或 date 统一成 date"""
    if isinstance(x, date):
        return x
    return datetime.strptime(str(x)[:10], "%Y-%m-%d").date()


def _rng(d1, d2):
    return str(d1)[:10], str(d2)[:10]


def _row2dict(row):
    return {k: row[k] for k in row.keys()} if row is not None else None


def date_series(d1, d2):
    """闭区间日期列表"""
    a, b = _d(d1), _d(d2)
    out, cur = [], a
    while cur <= b:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


# ---------------------------------------------------------------- 成本

def dish_cost_map(conn):
    """{dish_id: 单份成本}，套餐按子项展开（与出餐扣料口径一致）"""
    base = {}
    for r in conn.execute(
            "SELECT r.dish_id, SUM(r.qty * i.cost) c FROM dish_recipes r"
            " JOIN ingredients i ON i.id = r.ingredient_id GROUP BY r.dish_id"):
        base[r["dish_id"]] = float(r["c"] or 0)
    out = dict(base)
    for c in conn.execute("SELECT combo_id, dish_id, qty FROM combo_items"):
        child = base.get(c["dish_id"])
        if child is None:
            continue
        out[c["combo_id"]] = out.get(c["combo_id"], 0.0) + child * (c["qty"] or 1)
    return {k: money(v) for k, v in out.items()}


def recipe_requirements(conn):
    """{dish_id: [(ingredient_id, 单份用量)]}，套餐展开子项（备货与可做份数用）"""
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


# ---------------------------------------------------------------- KPI

def q_kpi(conn, d1, d2):
    """区间经营 KPI。所有比率保留 4 位小数（0.xxxx）。"""
    d1, d2 = _rng(d1, d2)
    r = conn.execute(
        "SELECT COALESCE(SUM(revenue),0) revenue, COALESCE(SUM(orders),0) orders,"
        " COALESCE(SUM(persons),0) persons, COALESCE(SUM(discount),0) discount,"
        " COALESCE(SUM(cost),0) cost, COALESCE(SUM(profit),0) profit,"
        " COALESCE(SUM(member_orders),0) member_orders,"
        " COALESCE(SUM(lunch_revenue),0) lunch, COALESCE(SUM(dinner_revenue),0) dinner"
        " FROM daily_facts WHERE d BETWEEN ? AND ?", (d1, d2)).fetchone()
    revenue = money(r["revenue"])
    orders = int(r["orders"] or 0)
    persons = int(r["persons"] or 0)
    discount = money(r["discount"])
    cost = money(r["cost"])
    profit = money(r["profit"])
    return {
        "d1": d1, "d2": d2,
        "revenue": revenue, "orders": orders, "persons": persons,
        "discount": discount, "cost": cost, "profit": profit,
        "avg_price": money(revenue / orders) if orders else 0.0,
        "per_person": money(revenue / persons) if persons else 0.0,
        "discount_rate": round(discount / (revenue + discount), 4) if revenue + discount else 0.0,
        "margin_rate": round(profit / revenue, 4) if revenue else 0.0,
        "member_orders": int(r["member_orders"] or 0),
        "member_rate": round((r["member_orders"] or 0) / orders, 4) if orders else 0.0,
        "lunch": money(r["lunch"]), "dinner": money(r["dinner"]),
    }


def q_trend(conn, days=30, end=None):
    """近 N 天营收趋势（补全空缺日期，图表不缺口）"""
    end = _d(end or date.today())
    d1 = end - timedelta(days=days - 1)
    rows = {r["d"]: r for r in conn.execute(
        "SELECT d, revenue, orders, persons, profit, margin_rate, discount_rate"
        " FROM v_sales_daily WHERE d BETWEEN ? AND ?",
        (d1.isoformat(), end.isoformat()))}
    out = []
    for d in date_series(d1, end):
        r = rows.get(d)
        out.append({
            "d": d,
            "revenue": money(r["revenue"]) if r else 0.0,
            "orders": int(r["orders"]) if r else 0,
            "persons": int(r["persons"]) if r else 0,
            "margin_rate": float(r["margin_rate"]) if r else 0.0,
            "discount_rate": float(r["discount_rate"]) if r else 0.0,
        })
    return out


def q_same_weekday_baseline(conn, d, weeks=4, field="revenue"):
    """同星期基线：前 N 周同一星期的均值（不含当天）。

    这是"营收异常"检测的分母，比"昨天"更稳（餐饮周一和周六不可比）。
    """
    dd = _d(d)
    vals = []
    for i in range(1, weeks + 1):
        t = (dd - timedelta(days=7 * i)).isoformat()
        row = conn.execute("SELECT %s v FROM daily_facts WHERE d=?" % field, (t,)).fetchone()
        if row and row["v"] is not None:
            vals.append(float(row["v"]))
    if not vals:
        return None
    return sum(vals) / len(vals)


def q_yoy(conn, d):
    """同比：去年同一天（有数据才有意义，演示库里通常为 None）"""
    dd = _d(d)
    try:
        last_year = dd.replace(year=dd.year - 1).isoformat()
    except ValueError:
        last_year = (dd - timedelta(days=365)).isoformat()
    row = conn.execute("SELECT revenue FROM daily_facts WHERE d=?", (last_year,)).fetchone()
    return money(row["revenue"]) if row else None


def q_revenue_decompose(conn, d, weeks=4):
    """把营收变化拆成「客流影响」+「客单价影响」。

    这是老板最想知道的一件事：到底是人少了，还是人没少但不点贵的了。
    营收 = 单数 × 客单价，所以 Δ营收 ≈ Δ单数×基线客单价 + 当前单数×Δ客单价。
    """
    ds = _d(d).isoformat()
    row = conn.execute("SELECT revenue, orders FROM daily_facts WHERE d=?", (ds,)).fetchone()
    if not row or not row["orders"]:
        return None
    base_rev = q_same_weekday_baseline(conn, ds, weeks=weeks, field="revenue")
    base_ord = q_same_weekday_baseline(conn, ds, weeks=weeks, field="orders")
    if not base_rev or not base_ord:
        return None
    cur_avg = row["revenue"] / row["orders"]
    base_avg = base_rev / base_ord
    traffic_effect = (row["orders"] - base_ord) * base_avg
    price_effect = row["orders"] * (cur_avg - base_avg)
    main = "客流" if abs(traffic_effect) >= abs(price_effect) else "客单价"
    return {
        "d": ds, "revenue": money(row["revenue"]), "orders": row["orders"],
        "avg_price": money(cur_avg),
        "base_revenue": money(base_rev), "base_orders": round(base_ord, 1),
        "base_avg_price": money(base_avg),
        "traffic_effect": money(traffic_effect), "price_effect": money(price_effect),
        "main": main,
    }


# ---------------------------------------------------------------- 菜品

def q_dish_sales(conn, d1, d2, limit=500):
    """区间菜品销量与毛利"""
    d1, d2 = _rng(d1, d2)
    rows = conn.execute(
        "SELECT name, category, SUM(qty) qty, SUM(amount) amount,"
        " SUM(cost) cost, SUM(profit) profit"
        " FROM dish_daily_facts WHERE d BETWEEN ? AND ?"
        " GROUP BY dish_id, name, category"
        " ORDER BY qty DESC LIMIT ?", (d1, d2, limit)).fetchall()
    out = []
    for r in rows:
        out.append({
            "name": r["name"], "category": r["category"],
            "qty": int(r["qty"] or 0), "amount": money(r["amount"]),
            "cost": money(r["cost"]), "profit": money(r["profit"]),
            "margin_rate": round(r["profit"] / r["amount"], 4) if r["amount"] else 0.0,
        })
    return out


def q_dish_quadrant(conn, d1, d2, min_amount=0.0):
    """菜品工程四象限：销量分位 × 毛利率分位。

    只统计"录了配方 + 有销量"的菜（没有成本就无法判断毛利，强行归类是误导）。
    分界线用中位数，避免被头部爆款拉偏。
    """
    rows = conn.execute(
        "SELECT f.name, f.category, SUM(f.qty) qty, SUM(f.amount) amount,"
        " SUM(f.cost) cost, SUM(f.profit) profit"
        " FROM dish_daily_facts f"
        " JOIN dish_recipes r ON r.dish_id = f.dish_id"
        " WHERE f.d BETWEEN ? AND ? AND f.amount > ?"
        " GROUP BY f.dish_id, f.name, f.category", (*_rng(d1, d2), min_amount)).fetchall()
    items = []
    for r in rows:
        if not r["amount"]:
            continue
        items.append({
            "name": r["name"], "category": r["category"],
            "qty": int(r["qty"] or 0), "amount": money(r["amount"]),
            "profit": money(r["profit"]),
            "margin_rate": round(r["profit"] / r["amount"], 4),
        })
    if not items:
        return {"items": [], "median_qty": 0, "median_margin": 0, "summary": {}}

    qtys = sorted(i["qty"] for i in items)
    margins = sorted(i["margin_rate"] for i in items)
    mq = qtys[len(qtys) // 2]
    mm = margins[len(margins) // 2]
    for i in items:
        high_q, high_m = i["qty"] >= mq, i["margin_rate"] >= mm
        i["quadrant"] = ("明星" if high_q and high_m else
                         "金牛" if high_q else
                         "潜力" if high_m else "瘦狗")
    items.sort(key=lambda x: (-x["amount"], -x["qty"]))
    summary = {}
    for q in ("明星", "金牛", "潜力", "瘦狗"):
        sub = [i for i in items if i["quadrant"] == q]
        summary[q] = {"count": len(sub),
                      "amount": money(sum(i["amount"] for i in sub)),
                      "profit": money(sum(i["profit"] for i in sub))}
    return {"items": items, "median_qty": mq, "median_margin": mm, "summary": summary}


def q_slow_dishes(conn, days=3, lookback=14):
    """滞销候选：近 `days` 天 0 销量，但在售、且更早的 lookback 天里有销量。

    排除两类误报：新上架还没卖过的菜、易拉罐饮料（销量本来就不稳定）。
    """
    today = date.today()
    recent = (today - timedelta(days=days - 1)).isoformat()
    earlier = (today - timedelta(days=days + lookback)).isoformat()
    rows = conn.execute(
        "SELECT d.id, d.name, c.name cat, COALESCE(SUM(f.qty),0) old_qty"
        " FROM dishes d JOIN categories c ON c.id = d.category_id"
        " LEFT JOIN dish_daily_facts f ON f.dish_id = d.id AND f.d BETWEEN ? AND ?"
        " WHERE d.on_sale = 1 AND c.name NOT IN ('饮料','白酒','红酒','茶叶')"
        " GROUP BY d.id, d.name, c.name"
        " HAVING old_qty > 0 AND NOT EXISTS ("
        "   SELECT 1 FROM dish_daily_facts g WHERE g.dish_id = d.id AND g.d BETWEEN ? AND ?)"
        " ORDER BY old_qty DESC LIMIT 20", (earlier, recent, recent, today.isoformat())).fetchall()
    return [{"dish_id": r["id"], "name": r["name"], "category": r["cat"],
             "prev_qty": int(r["old_qty"])} for r in rows]


# ---------------------------------------------------------------- 库存

def q_stock_risk(conn, days=14, horizon=None):
    """食材消耗速度与可支撑天数。

    日耗 = Σ(近 days 天菜品销量 × 配方用量) / days
    可支撑天数 = 当前库存 / 日耗
    """
    horizon = horizon or days
    d1 = (date.today() - timedelta(days=days - 1)).isoformat()
    sold = {r["dish_id"]: float(r["qty"] or 0) for r in conn.execute(
        "SELECT dish_id, SUM(qty) qty FROM dish_daily_facts"
        " WHERE d >= ? GROUP BY dish_id", (d1,))}
    req = recipe_requirements(conn)
    use = {}
    for did, qty in sold.items():
        for iid, per in req.get(did, []):
            use[iid] = use.get(iid, 0.0) + per * qty
    out = []
    for r in conn.execute("SELECT * FROM ingredients ORDER BY stock * cost DESC"):
        total = use.get(r["id"], 0.0)
        daily = total / days if days else 0.0
        days_left = (r["stock"] / daily) if daily > 0 else None
        out.append({
            "ingredient_id": r["id"], "name": r["name"], "unit": r["unit"],
            "stock": round(r["stock"], 2), "daily_use": round(daily, 3),
            "days_left": round(days_left, 1) if days_left is not None else None,
            "warn_threshold": r["warn_threshold"], "cost": r["cost"],
            "stock_value": money((r["stock"] or 0) * (r["cost"] or 0)),
            "expire_at": r["expire_at"],
        })
    return out


def q_consumption_need(conn, days=7):
    """未来 days 天的理论食材需求（按近期日均消耗外推）"""
    risk = q_stock_risk(conn)
    return [{"ingredient_id": r["ingredient_id"], "name": r["name"], "unit": r["unit"],
             "need": round((r["daily_use"] or 0) * days, 2),
             "stock": r["stock"], "cost": r["cost"]}
            for r in risk if (r["daily_use"] or 0) > 0]


# ---------------------------------------------------------------- 客流

def q_hour_profile(conn, days=30):
    """时段客流画像：用于排班与"什么时候最忙"的回答"""
    d1 = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        "SELECT hour, SUM(orders) orders, SUM(persons) persons, SUM(revenue) revenue"
        " FROM hour_facts WHERE d >= ? GROUP BY hour ORDER BY hour", (d1,)).fetchall()
    return [{"hour": r["hour"], "orders": int(r["orders"] or 0),
             "persons": int(r["persons"] or 0), "revenue": money(r["revenue"])}
            for r in rows]


# ---------------------------------------------------------------- 会员

def q_member_summary(conn, days=30):
    d1 = (date.today() - timedelta(days=days - 1)).isoformat()
    total = conn.execute("SELECT COUNT(*) c FROM members").fetchone()["c"]
    active = conn.execute(
        "SELECT COUNT(DISTINCT member_id) c FROM orders"
        " WHERE member_id IS NOT NULL AND status='settled' AND substr(settled_at,1,10) >= ?",
        (d1,)).fetchone()["c"]
    repeat = conn.execute(
        "SELECT COUNT(*) c FROM (SELECT member_id FROM orders"
        " WHERE member_id IS NOT NULL AND status='settled' AND substr(settled_at,1,10) >= ?"
        " GROUP BY member_id HAVING COUNT(*) >= 2)", (d1,)).fetchone()["c"]
    lost = conn.execute(
        "SELECT COUNT(*) c FROM members WHERE last_visit_at IS NOT NULL"
        " AND substr(last_visit_at,1,10) < ?",
        ((date.today() - timedelta(days=60)).isoformat(),)).fetchone()["c"]
    balance = conn.execute("SELECT COALESCE(SUM(balance),0) b FROM members").fetchone()["b"]
    return {"total": total, "active": active, "repeat": repeat, "lost_60d": lost,
            "balance_total": money(balance),
            "repeat_rate": round(repeat / active, 4) if active else 0.0,
            "active_rate": round(active / total, 4) if total else 0.0}


# ---------------------------------------------------------------- 预测

def _fit_forecast(series, horizon):
    """三层分解预测：水平 × 星期因子 × 趋势（纯 Python，不引 numpy）。

    series: [(date_str, value)] 按日期升序
    返回 (预测列表, MAPE, 方法说明)
    数据不足时退化为"均值 × 星期因子"，并明确告知调用方（方法论要诚实）。
    """
    if not series:
        return [], None, "无数据"
    values = [v for _, v in series]
    n = len(values)
    overall = sum(values) / n
    # 星期因子
    dow_sum, dow_cnt = {}, {}
    for ds, v in series:
        w = _d(ds).weekday()
        dow_sum[w] = dow_sum.get(w, 0.0) + v
        dow_cnt[w] = dow_cnt.get(w, 0) + 1
    factor = {w: (dow_sum[w] / dow_cnt[w]) / overall if overall > 0 else 1.0
              for w in dow_sum}
    for w in range(7):
        factor.setdefault(w, 1.0)

    if n < 6:
        level = overall
        trend = 0.0
        method = "均值×星期因子"
    else:
        recent = values[-14:] if n >= 14 else values
        level = sum(recent) / len(recent)
        # 最小二乘斜率（近 14 天），上限 ±3%/天，防止被单日爆单带飞
        m = len(recent)
        xs = list(range(m))
        mx = sum(xs) / m
        my = level
        denom = sum((x - mx) ** 2 for x in xs) or 1
        slope = sum((xs[i] - mx) * (recent[i] - my) for i in range(m)) / denom
        cap = level * 0.03
        trend = max(-cap, min(cap, slope))
        method = "水平×星期因子×趋势"

    last_day = _d(series[-1][0])
    out = []
    for i in range(1, horizon + 1):
        day = last_day + timedelta(days=i)
        # 水平 + 趋势步进，再叠加该星期几的历史因子
        v = max(0.0, (level + trend * i) * factor[day.weekday()])
        out.append({"d": day.isoformat(), "value": money(v),
                    "dow": day.weekday()})
    return out, None, method


def _rolling_mape(series, days=28):
    """滚动一步回测：每天用"此前所有数据"预测当天，取平均绝对百分比误差。

    比"留出最后 7 天"更公平：留出法如果刚好碰上异常周（比如我们刻意植入的
    营收下滑），MAPE 会被单周异常放大到 90%+，看起来像模型坏了。
    """
    if len(series) < 10:
        return None
    start = max(8, len(series) - days)
    errs = []
    for idx in range(start, len(series)):
        pred, _, _ = _fit_forecast(series[:idx], 1)
        actual = series[idx][1]
        if pred and actual > 0:
            errs.append(abs(pred[0]["value"] - actual) / actual)
    return round(sum(errs) / len(errs), 4) if errs else None


def q_forecast_revenue(conn, horizon=7, history=60):
    """营收预测 + 滚动回测 MAPE"""
    end = date.today() - timedelta(days=1)   # 预测从明天开始，用昨天的完整数据
    d1 = (end - timedelta(days=history - 1)).isoformat()
    rows = conn.execute("SELECT d, revenue FROM daily_facts WHERE d BETWEEN ? AND ?"
                        " ORDER BY d", (d1, end.isoformat())).fetchall()
    series = [(r["d"], float(r["revenue"] or 0)) for r in rows]
    fc, _, method = _fit_forecast(series, horizon)
    mape = _rolling_mape(series, days=28) if len(series) >= 21 else None
    return {"forecast": fc, "mape": mape, "method": method,
            "history_days": len(series),
            "sum": money(sum(p["value"] for p in fc))}


def q_forecast_dish(conn, dish_id, horizon=7, history=60):
    end = date.today() - timedelta(days=1)
    d1 = (end - timedelta(days=history - 1)).isoformat()
    rows = conn.execute("SELECT d, qty FROM dish_daily_facts WHERE dish_id=? AND d BETWEEN ? AND ?"
                        " ORDER BY d", (dish_id, d1, end.isoformat())).fetchall()
    series = [(r["d"], float(r["qty"] or 0)) for r in rows]
    fc, _, method = _fit_forecast(series, horizon)
    return [{"d": p["d"], "qty": int(round(p["value"]))} for p in fc]
