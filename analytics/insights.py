# -*- coding: utf-8 -*-
"""洞察引擎 —— 确定性规则检测器。

核心设计：**洞察由规则产出，不由模型产出**。
模型只负责把 evidence 改写成老板能看懂的一句话；这样即使模型胡说，
数字和判断依然是可复现、可审计的。

每个检测器返回统一结构：
{
  type, severity, title, detail,
  evidence: {metric, current, baseline, change, threshold, sql, ...},
  suggestion, action_type, action_payload
}
"""
from datetime import date, timedelta

import config
from analytics import metrics
from utils import money


def _pct(x):
    return round((x or 0) * 100, 1)


WEEK_CN = "一二三四五六日"


def _weekday_cn(ds):
    from datetime import datetime as _dt
    try:
        return "星期" + WEEK_CN[_dt.strptime(str(ds)[:10], "%Y-%m-%d").weekday()]
    except (ValueError, IndexError):
        return "同星期"


def _get_setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def detect_revenue_drop(conn, d=None):
    """营收异常：与近 4 周同星期均值比较。

    用同星期做基线，而不是"昨天"：餐饮周一和周六本来就不可比，
    拿昨天当基线会天天误报。
    """
    d = d or (date.today() - timedelta(days=1))
    ds = d.isoformat()
    row = conn.execute("SELECT revenue, orders FROM daily_facts WHERE d=?", (ds,)).fetchone()
    if not row or not row["revenue"]:
        return []
    base = metrics.q_same_weekday_baseline(conn, ds, weeks=4, field="revenue")
    if not base:
        return []
    change = (row["revenue"] - base) / base
    if change > -0.12:
        return []
    severity = "critical" if change <= -0.20 else "warning"
    gap = money(base - row["revenue"])
    return [{
        "type": "revenue_drop",
        "severity": severity,
        "title": "%s 营收 %s 元，比同星期基线低 %.0f%%" % (ds[5:], money(row["revenue"]), abs(change) * 100),
        "detail": "近 4 周%s平均 %s 元，昨日少收约 %s 元（%d 单）。"
                  % (_weekday_cn(ds), money(base), gap, row["orders"]),
        "evidence": {
            "metric": "日营收", "current": money(row["revenue"]),
            "baseline": money(base), "change": round(change, 4),
            "threshold": -0.12, "orders": row["orders"],
            "sql": "SELECT revenue FROM daily_facts WHERE d='%s'" % ds,
        },
        "suggestion": "先看是客流少了还是客单价降了：客流降→查天气/竞品/排班；客单降→查是否高单价菜缺货。",
        "action_type": "diagnose_revenue",
        "action_payload": {"d": ds},
    }]


def detect_slow_dishes(conn, limit=3):
    """滞销菜：近 3 天 0 销量，此前 14 天有稳定销量"""
    slow = [s for s in metrics.q_slow_dishes(conn) if s["prev_qty"] >= 8][:limit]
    if not slow:
        return []
    names = "、".join(s["name"] for s in slow)
    return [{
        "type": "slow_dish",
        "severity": "warning",
        "title": "%d 道菜连续 3 天没卖出去" % len(slow),
        "detail": "%s（近 14 天原有稳定销量）。" % names,
        "evidence": {
            "metric": "连续 0 销量天数", "current": 3, "baseline": 0,
            "threshold": 3, "dishes": slow,
            "sql": ("SELECT d.name, SUM(qty) FROM dish_daily_facts f JOIN dishes d"
                    " ON d.id=f.dish_id WHERE f.d < date('now','-3 day') GROUP BY d.id"),
        },
        "suggestion": "三种处理：① 与高毛利菜组套餐清库存；② 点餐端主推位换下来；③ 连续 2 周仍无起色就下架。",
        "action_type": "promote_dishes",
        "action_payload": {"dishes": slow},
    }]


def detect_margin(conn, days=7):
    """毛利预警：近 7 天毛利率低于目标，或成本率环比上升"""
    end = date.today() - timedelta(days=1)
    d1 = (end - timedelta(days=days - 1)).isoformat()
    cur = metrics.q_kpi(conn, d1, end.isoformat())
    if not cur["revenue"]:
        return []
    target = float(_get_setting(conn, "target_margin", "0.6") or 0.6)
    out = []
    if cur["margin_rate"] < target - 0.02:
        out.append({
            "type": "margin",
            "severity": "critical" if cur["margin_rate"] < target - 0.06 else "warning",
            "title": "近 %d 天毛利率 %.1f%%，低于目标 %.0f%%" % (days, cur["margin_rate"] * 100, target * 100),
            "detail": "毛利 %s 元 / 营收 %s 元，食材成本 %s 元。"
                      % (money(cur["profit"]), money(cur["revenue"]), money(cur["cost"])),
            "evidence": {
                "metric": "毛利率", "current": cur["margin_rate"], "baseline": target,
                "change": round(cur["margin_rate"] - target, 4), "threshold": target,
                "window_days": days,
                "sql": "SELECT margin_rate FROM v_sales_daily WHERE d BETWEEN '%s' AND '%s'" % (d1, end.isoformat()),
            },
            "suggestion": "定位是「食材涨价」还是「折扣/赠送变多」：前者调价或换供应商，后者收紧折扣。",
            "action_type": "price_review",
            "action_payload": {},
        })
    # 低毛利高销量菜（真正在赔钱的菜）
    q = metrics.q_dish_quadrant(conn, d1, end.isoformat())
    dogs = [i for i in q["items"]
            if i["quadrant"] == "瘦狗" and i["margin_rate"] < 0.35 and i["qty"] >= 5]
    dogs.sort(key=lambda x: -x["qty"])
    if dogs:
        out.append({
            "type": "low_margin_dish",
            "severity": "info",
            "title": "%d 道菜「卖得多但不赚钱」" % len(dogs),
            "detail": "、".join("%s（毛利 %.0f%%，%d 份）" % (i["name"], i["margin_rate"] * 100, i["qty"])
                              for i in dogs[:3]),
            "evidence": {"metric": "菜品毛利率", "current": dogs[0]["margin_rate"],
                         "baseline": 0.35, "threshold": 0.35, "dishes": dogs[:5],
                         "sql": "SELECT name, margin_rate, qty FROM v_dish_sales"},
            "suggestion": "优先调价 5%~8%，而不是直接下架——销量在，说明客人认，只是定价没跟上成本。",
            "action_type": "price_review",
            "action_payload": {"dishes": [d["name"] for d in dogs[:5]]},
        })
    return out


def detect_stock(conn):
    """库存风险：可支撑天数不足"""
    risk = [r for r in metrics.q_stock_risk(conn, days=14)
            if r["daily_use"] > 0 and r["days_left"] is not None
            and r["days_left"] < config.LOW_STOCK_DAYS * 2]
    if not risk:
        return []
    risk.sort(key=lambda r: r["days_left"])
    top = risk[0]
    need_7 = round(top["daily_use"] * 7 - top["stock"], 1)
    return [{
        "type": "stock",
        "severity": "critical" if top["days_left"] < config.LOW_STOCK_DAYS else "warning",
        "title": "%s 只够 %.1f 天" % (top["name"], top["days_left"]),
        "detail": "日耗约 %.1f %s，现有库存 %.1f %s。按近 14 天销量推算，明天晚市前会断。"
                  % (top["daily_use"], top["unit"], top["stock"], top["unit"]),
        "evidence": {
            "metric": "可支撑天数", "current": top["days_left"],
            "baseline": 7, "threshold": config.LOW_STOCK_DAYS,
            "ingredient": top["name"], "daily_use": top["daily_use"],
            "stock": top["stock"], "suggest_qty": max(need_7, 0),
            "sql": ("SELECT stock FROM ingredients WHERE name='%s'" % top["name"]),
        },
        "suggestion": "按未来 7 天用量补货 %.1f %s，可覆盖到下周同期；若只补 3 天会再断一次。" % (max(need_7, 0), top["unit"]),
        "action_type": "purchase",
        "action_payload": {"ingredient_id": top["ingredient_id"], "name": top["name"],
                           "unit": top["unit"], "qty": max(need_7, 0),
                           "unit_cost": top["cost"]},
    }]


def detect_discount(conn, days=7):
    """折扣健康度：让利比例环比抬升"""
    end = date.today() - timedelta(days=1)
    cur_d1 = (end - timedelta(days=days - 1)).isoformat()
    prev_d2 = end - timedelta(days=days)
    prev_d1 = (prev_d2 - timedelta(days=days - 1)).isoformat()
    cur = metrics.q_kpi(conn, cur_d1, end.isoformat())
    prev = metrics.q_kpi(conn, prev_d1, prev_d2.isoformat())
    if not cur["revenue"] or not prev["revenue"]:
        return []
    change = cur["discount_rate"] - prev["discount_rate"]
    rel = change / prev["discount_rate"] if prev["discount_rate"] else 0
    if change <= 0.015 and rel <= 0.25:
        return []
    extra = money(cur["discount"] - prev["discount_rate"] * cur["revenue"])
    return [{
        "type": "discount",
        "severity": "warning" if change > 0.03 else "info",
        "title": "近 %d 天折扣率 %.1f%%，比前 %d 天高 %.1f 个点"
                 % (days, cur["discount_rate"] * 100, days, change * 100),
        "detail": "让利 %s 元，按之前的折扣水平本可少让 %s 元。" % (money(cur["discount"]), extra),
        "evidence": {
            "metric": "折扣率", "current": cur["discount_rate"],
            "baseline": prev["discount_rate"], "change": round(change, 4),
            "threshold": 0.015, "extra_discount": extra,
            "sql": ("SELECT discount_rate FROM v_sales_daily WHERE d BETWEEN '%s' AND '%s'"
                    % (cur_d1, end.isoformat())),
        },
        "suggestion": "查两件事：员工是否在手动加折扣、会员折扣是否被非会员借用。折扣是利润，不是人情。",
        "action_type": "review_discount",
        "action_payload": {},
    }]


def detect_member_churn(conn):
    """会员流失：高价值会员 60 天未到店"""
    s = metrics.q_member_summary(conn)
    if s["lost_60d"] < 8:
        return []
    return [{
        "type": "member_churn",
        "severity": "info",
        "title": "%d 位会员超过 60 天没来" % s["lost_60d"],
        "detail": "其中包含历史消费较高的会员。会员总数 %d，近 30 天活跃 %d。"
                  % (s["total"], s["active"]),
        "evidence": {"metric": "60 天未到店会员数", "current": s["lost_60d"],
                     "baseline": 8, "threshold": 8,
                     "sql": "SELECT COUNT(*) FROM members WHERE last_visit_at < date('now','-60 day')"},
        "suggestion": "先发给消费额 Top 20% 的那批，用「老菜回归」而不是通用满减——高价值客人对折扣不敏感，对熟悉感敏感。",
        "action_type": "member_recall",
        "action_payload": {"count": s["lost_60d"]},
    }]


DETECTORS = [detect_revenue_drop, detect_slow_dishes, detect_margin,
             detect_stock, detect_discount, detect_member_churn]

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def run(conn, d=None, persist=True):
    """跑全部检测器，按严重度排序返回；可选写入 ai_insights（当日幂等）"""
    out = []
    for fn in DETECTORS:
        try:
            out.extend(fn(conn) or [])
        except Exception as e:            # 单个检测器失败不影响其他
            print("[insight] %s 失败：%s" % (fn.__name__, e))
    out.sort(key=lambda x: (SEVERITY_ORDER.get(x["severity"], 9), x["type"]))
    if persist:
        day = (d or (date.today() - timedelta(days=1)))
        day = day.isoformat() if hasattr(day, "isoformat") else str(day)
        # 今天已经处理过的类型不再重复出现（问题没解决但人已经下过动作，
        # 反复弹同一条会让老板觉得这东西很烦）
        done = {r["type"] for r in conn.execute(
            "SELECT type FROM ai_insights WHERE d=? AND status='done'", (day,))}
        out = [i for i in out if i["type"] not in done]
        conn.execute("DELETE FROM ai_insights WHERE d=? AND status='open'", (day,))
        for it in out:
            import json
            cur = conn.execute(
                "INSERT INTO ai_insights(d, type, severity, title, detail, evidence,"
                " suggestion, action_type, action_payload, status, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?, 'open', datetime('now','localtime'))",
                (day, it["type"], it["severity"], it["title"], it["detail"],
                 json.dumps(it["evidence"], ensure_ascii=False),
                 it["suggestion"], it["action_type"],
                 json.dumps(it["action_payload"], ensure_ascii=False)))
            it["id"] = cur.lastrowid
        conn.commit()
    return out


def brief(conn, d=None):
    """规则版经营简报（不依赖模型）。

    结构固定为「昨天怎么样 → 哪里不对 → 今天做什么」，
    即使完全没有 AI，老板也能读完就知道今天该干嘛。
    """
    d = d or (date.today() - timedelta(days=1))
    ds = d.isoformat()
    k = metrics.q_kpi(conn, ds, ds)
    base = metrics.q_same_weekday_baseline(conn, ds, weeks=4, field="revenue")
    items = run(conn, d=d, persist=False)
    change = ((k["revenue"] - base) / base) if base else 0.0
    lines = []
    if k["orders"]:
        trend_word = "上升" if change >= 0 else "下滑"
        lines.append("昨日营收 %s 元、%d 单，客单价 %s 元，较同星期基线%s %.0f%%。"
                     % (money(k["revenue"]), k["orders"], money(k["avg_price"]),
                        trend_word, abs(change) * 100))
        lines.append("毛利率 %.1f%%，折扣率 %.1f%%。"
                     % (k["margin_rate"] * 100, k["discount_rate"] * 100))
    else:
        lines.append("昨日无结算数据。")
    actions = [i["title"] for i in items[:3]]
    return {"d": ds, "kpi": k, "change": round(change, 4),
            "text": " ".join(lines), "insights": items, "actions": actions}
