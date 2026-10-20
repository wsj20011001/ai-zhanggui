# -*- coding: utf-8 -*-
"""经营推演器（What-if）—— 让老板自己推，而不是只信 AI 的建议。

为什么单独做成纯函数：
- 不依赖模型、不写库、不依赖网络，现场演示不可能翻车；
- 每一步都能把公式摊开给评委看，是可验证的算术，不是"AI 估的"。

模型（全部假设都写在页面上）：
  营收 = 单数 × 客单价，菜品层面用「单价 × 销量」
  调价后销量按价格弹性收缩：qty' = qty × (1 − e × 涨幅)
  毛利变化 = Σ[(新价 − 成本) × 新销量 − (原价 − 成本) × 原销量]
  折扣收回：让利减少额直接计入毛利（假设客流不受影响）
"""
from datetime import date, timedelta

from analytics import metrics
from utils import money


def _scope_items(conn, days, scope):
    """取近 N 天有销量且有配方的菜品，按范围筛选"""
    end = date.today() - timedelta(days=1)
    d1 = end - timedelta(days=days - 1)
    rows = conn.execute(
        "SELECT f.dish_id, f.name, f.category, SUM(f.qty) qty,"
        " SUM(f.amount) amount, SUM(f.cost) cost"
        " FROM dish_daily_facts f"
        " JOIN dish_recipes r ON r.dish_id = f.dish_id"
        " WHERE f.d BETWEEN ? AND ?"
        " GROUP BY f.dish_id, f.name, f.category"
        " HAVING SUM(f.qty) > 0", (d1.isoformat(), end.isoformat())).fetchall()
    items = []
    for r in rows:
        qty = int(r["qty"] or 0)
        amount = float(r["amount"] or 0)
        cost = float(r["cost"] or 0)
        if qty <= 0 or amount <= 0:
            continue
        price = amount / qty
        unit_cost = cost / qty
        items.append({
            "dish_id": r["dish_id"], "name": r["name"], "category": r["category"],
            "qty": qty, "price": price, "unit_cost": unit_cost,
            "margin_rate": (amount - cost) / amount if amount else 0,
        })
    if scope == "low_margin":
        items = [i for i in items if i["margin_rate"] < 0.40]
    elif scope == "top20":
        items.sort(key=lambda x: -x["qty"])
        items = items[:20]
    return items


def run(conn, days=30, price_pct=0.0, elasticity=0.8, target_discount=None,
        scope="all"):
    """返回推演结果 dict。price_pct 是 0~0.15 的小数。"""
    price_pct = max(-0.30, min(0.30, float(price_pct or 0)))
    elasticity = max(0.0, min(3.0, float(elasticity or 0)))
    items = _scope_items(conn, days, scope)

    end = date.today() - timedelta(days=1)
    d1 = end - timedelta(days=days - 1)
    base = metrics.q_kpi(conn, d1, end)

    # ---- 调价效应 ----
    delta_profit = 0.0
    delta_revenue = 0.0
    qty_before = sum(i["qty"] for i in items)
    qty_after = 0.0
    # 供求"弹性临界点"用的线性系数：Δ利润 = A − B×e
    A = B = 0.0
    for i in items:
        p0, c, q = i["price"], i["unit_cost"], i["qty"]
        p1 = p0 * (1 + price_pct)
        q1 = q * (1 - elasticity * price_pct)
        if q1 < 0:
            q1 = 0.0
        delta_profit += (p1 - c) * q1 - (p0 - c) * q
        delta_revenue += p1 * q1 - p0 * q
        qty_after += q1
        A += q * ((p1 - c) - (p0 - c))
        B += q * (p1 - c) * price_pct

    # ---- 折扣效应 ----
    discount_now = base["discount"]
    gross = base["revenue"] + discount_now          # 未打折前销售额
    rate_now = (discount_now / gross) if gross else 0.0
    discount_saving = 0.0
    rate_target = rate_now if target_discount is None else max(0.0, min(0.5, float(target_discount)))
    if gross:
        new_discount = gross * rate_target
        discount_saving = discount_now - new_discount

    total_profit_delta = delta_profit + discount_saving
    new_revenue = base["revenue"] + delta_revenue + discount_saving
    new_profit = base["profit"] + total_profit_delta
    new_margin = (new_profit / new_revenue) if new_revenue else 0.0

    # 销量变化率（按目标菜品的销量算，不是全店客流）
    qty_change = (qty_after - qty_before) / qty_before if qty_before else 0.0

    # 弹性临界点：再加多少弹性，这个方案就变成负收益
    breakeven_elasticity = None
    if price_pct > 0 and B > 0:
        e_star = (A + discount_saving) / B if (A + discount_saving) > 0 else 0.0
        if e_star > elasticity:
            breakeven_elasticity = round(e_star, 2)

    # ---- 滞销菜下架：只报事实，不编收益 ----
    slow = metrics.q_slow_dishes(conn)
    slow_ids = [s["dish_id"] for s in slow]
    req = metrics.recipe_requirements(conn)
    ingredients = set()
    for did in slow_ids:
        for ing_id, _qty in req.get(did, []):
            ingredients.add(ing_id)
    stock_value = 0.0
    if ingredients:
        marks = ",".join("?" * len(ingredients))
        row = conn.execute(
            "SELECT COALESCE(SUM(stock * cost),0) v FROM ingredients WHERE id IN (%s)"
            % marks, list(ingredients)).fetchone()
        stock_value = money(row["v"])

    return {
        "days": days, "scope": scope,
        "inputs": {"price_pct": round(price_pct, 4), "elasticity": elasticity,
                   "target_discount": round(rate_target, 4)},
        "base": base,
        "dish_count": len(items),
        "dish_qty": qty_before,
        "price_effect": money(delta_profit),
        "discount_effect": money(discount_saving),
        "revenue_effect": money(new_revenue - base["revenue"]),
        "profit_effect": money(total_profit_delta),
        "new_revenue": money(new_revenue),
        "new_profit": money(new_profit),
        "new_margin": round(new_margin, 4),
        "margin_delta": round(new_margin - base["margin_rate"], 4),
        "yearly": money(total_profit_delta * 365 / max(days, 1)),
        "qty_change": round(qty_change, 4),
        "breakeven_elasticity": breakeven_elasticity,
        "discount_rate_now": round(rate_now, 4),
        "slow": {"count": len(slow), "ingredients": len(ingredients),
                 "stock_value": stock_value,
                 "names": [s["name"] for s in slow[:5]]},
    }


def formula_text(res):
    """把公式和实际数字拼成人能读的一段，放在页面底部"""
    b = res["base"]
    return (
        "毛利变化 = Σ[(新价 − 成本) × 新销量 − (原价 − 成本) × 原销量] = %s 元；"
        "折扣收回 = 原让利 %s 元 − 新让利 = %s 元；"
        "合计 %s 元 / %d 天，年化 %s 元。"
        "基准：近 %d 天营收 %s 元、毛利 %s 元、毛利率 %.1f%%。"
        % (res["price_effect"], money(b["discount"]), res["discount_effect"],
           res["profit_effect"], res["days"], res["yearly"], res["days"],
           money(b["revenue"]), money(b["profit"]), b["margin_rate"] * 100)
    )
