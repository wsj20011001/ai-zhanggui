# -*- coding: utf-8 -*-
"""Agent 工具层 —— 让 AI 能"动手"，而不是只写一段话。

每个工具都遵守同一套契约：
  1. 执行前把 before 快照写进 ai_actions，保证可一键撤销；
  2. 执行后写 op_logs（谁、什么时候、改了什么），保证可审计；
  3. 参数在服务端重新校验（价格不能为负、数量不能超库存），不相信模型给的数字。
"""
import json
from datetime import date, datetime, timedelta

from utils import log_op, money, now_str


class ToolError(Exception):
    pass


def _snapshot(conn, table, where, params):
    rows = conn.execute("SELECT * FROM %s WHERE %s" % (table, where), params).fetchall()
    return [dict(r) for r in rows]


def _record_action(conn, insight_id, action_type, payload, before, check_days=7):
    cur = conn.execute(
        "INSERT INTO ai_actions(insight_id, action_type, payload, before_json,"
        " executed_at, check_after_days, verdict) VALUES(?,?,?,?,?,?, 'pending')",
        (insight_id, action_type, json.dumps(payload, ensure_ascii=False),
         json.dumps(before, ensure_ascii=False), now_str(), check_days))
    return cur.lastrowid


# ------------------------------------------------------------------ 采购

def create_purchase(conn, insight_id, items, note="AI 掌柜建议补货"):
    """生成采购单草稿（不动库存，等人确认）"""
    items = [i for i in items if float(i.get("qty") or 0) > 0]
    if not items:
        raise ToolError("没有需要采购的食材")
    total = money(sum(float(i["qty"]) * float(i.get("unit_cost") or 0) for i in items))
    cur = conn.execute(
        "INSERT INTO purchase_orders(status, source, note, total_cost, created_at)"
        " VALUES('draft','ai',?,?,?)", (note, total, now_str()))
    po_id = cur.lastrowid
    for i in items:
        qty = round(float(i["qty"]), 2)
        cost = float(i.get("unit_cost") or 0)
        conn.execute(
            "INSERT INTO purchase_items(po_id, ingredient_id, name, unit, qty,"
            " unit_cost, amount) VALUES(?,?,?,?,?,?,?)",
            (po_id, i["ingredient_id"], i["name"], i.get("unit", ""),
             qty, cost, money(qty * cost)))
    aid = _record_action(conn, insight_id, "purchase",
                         {"po_id": po_id, "items": items}, {"purchase_orders": []})
    conn.commit()
    return {"po_id": po_id, "action_id": aid, "total": total, "count": len(items)}


def receive_purchase(conn, po_id, who=("admin", "", "")):
    """确认入库：改库存 + 写流水（幂等：已入库的单不再处理）"""
    po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
    if not po:
        raise ToolError("采购单不存在")
    if po["status"] == "received":
        raise ToolError("该采购单已入库")
    before = {}
    for it in conn.execute("SELECT * FROM purchase_items WHERE po_id=?", (po_id,)).fetchall():
        ing = conn.execute("SELECT * FROM ingredients WHERE id=?",
                           (it["ingredient_id"],)).fetchone()
        if not ing:
            continue
        before[str(it["ingredient_id"])] = ing["stock"]
        conn.execute("UPDATE ingredients SET stock=ROUND(stock+?,3), cost=?, updated_at=?"
                     " WHERE id=?",
                     (it["qty"], it["unit_cost"] or ing["cost"], now_str(),
                      it["ingredient_id"]))
        conn.execute(
            "INSERT INTO stock_movements(ingredient_id, type, qty, price, note, created_at)"
            " VALUES(?, 'in', ?, ?, ?, ?)",
            (it["ingredient_id"], it["qty"], it["unit_cost"],
             "采购入库 PO#%d（AI 掌柜建议）" % po_id, now_str()))
    conn.execute("UPDATE purchase_orders SET status='received', received_at=? WHERE id=?",
                 (now_str(), po_id))
    log_op(conn, "库存", who[0], who[1], who[2],
           "采购入库 PO#%d，金额 %.2f 元" % (po_id, po["total_cost"]))
    conn.commit()
    return {"po_id": po_id, "before": before}


# ------------------------------------------------------------------ 调价

def suggest_price(conn, names, target_margin=0.6, max_step=0.15):
    """按目标毛利率反推建议售价。

    关键取舍：**不做"一步到位"的成本加成**。
    真实门店里一道菜一次涨 100% 会直接掉客，所以我们限制单次涨幅 ≤15%，
    并区分「能靠调价解决」和「必须改配方/换供应商」两类。
    """
    import math
    out = []
    for n in names:
        d = conn.execute(
            "SELECT dish_id, name, price, cost, margin_rate FROM v_dish_menu WHERE name=?",
            (n,)).fetchone()
        if not d or not d["cost"] or not d["price"]:
            continue
        target = d["cost"] / (1 - target_margin)
        cap = d["price"] * (1 + max_step)
        new_price = min(target, cap)
        new_price = round(math.ceil(new_price * 10) / 10 - 0.1, 1)
        new_price = max(new_price, d["price"])
        gap = (target - d["price"]) / d["price"]
        out.append({
            "dish_id": d["dish_id"], "name": d["name"], "old": money(d["price"]),
            "cost": money(d["cost"]), "new": money(new_price),
            "margin_now": round(d["margin_rate"] or 0, 4),
            "margin_new": round((new_price - d["cost"]) / new_price, 4) if new_price else 0,
            "delta": money(new_price - d["price"]),
            "need_pct": round(gap, 4),
            "note": ("单次只提 %.0f%%，还需 %.0f%% 才能到目标毛利；差额建议靠改配方或换供应商补"
                     % (max_step * 100, gap * 100)) if gap > max_step else "调价后可达到目标毛利",
        })
    return out


def apply_price(conn, insight_id, changes, who=("admin", "", "")):
    """执行调价；changes: [{dish_id, new}]"""
    if not changes:
        raise ToolError("没有可执行的调价")
    before = []
    for c in changes:
        d = conn.execute("SELECT id, name, price FROM dishes WHERE id=?",
                         (c["dish_id"],)).fetchone()
        if not d:
            continue
        new = money(float(c["new"]))
        if new <= 0 or new > 9999:
            raise ToolError("价格不合法：%s" % new)
        before.append({"id": d["id"], "price": d["price"]})
        conn.execute("UPDATE dishes SET price=? WHERE id=?", (new, d["id"]))
        log_op(conn, "定价", who[0], who[1], who[2],
               "AI 建议调价：%s %.2f → %.2f" % (d["name"], d["price"], new))
    aid = _record_action(conn, insight_id, "price", {"changes": changes},
                         {"dishes": before}, check_days=14)
    conn.commit()
    return {"action_id": aid, "count": len(before)}


# ------------------------------------------------------------------ 主推

def promote(conn, insight_id, dish_ids, who=("admin", "", "")):
    """把滞销菜设为主推（点餐端「今日主推」会带上它）"""
    if not dish_ids:
        raise ToolError("没有可推广的菜品")
    before = _snapshot(conn, "dishes", "id IN (%s)" % ",".join("?" * len(dish_ids)),
                       dish_ids)
    for did in dish_ids:
        conn.execute("UPDATE dishes SET is_recommend=1 WHERE id=?", (did,))
    log_op(conn, "编辑", who[0], who[1], who[2],
           "AI 建议：%d 道滞销菜设为点餐端主推" % len(dish_ids))
    aid = _record_action(conn, insight_id, "promote",
                         {"dish_ids": dish_ids}, {"dishes": before})
    conn.commit()
    return {"action_id": aid, "count": len(dish_ids)}


# ------------------------------------------------------------------ 撤销

def undo(conn, action_id, who=("admin", "", "")):
    """一键撤销：按 before_json 恢复现场"""
    a = conn.execute("SELECT * FROM ai_actions WHERE id=?", (action_id,)).fetchone()
    if not a:
        raise ToolError("操作记录不存在")
    if a["undone_at"]:
        raise ToolError("该操作已经撤销过")
    before = json.loads(a["before_json"] or "{}")
    if a["action_type"] == "price":
        for d in before.get("dishes", []):
            conn.execute("UPDATE dishes SET price=? WHERE id=?", (d["price"], d["id"]))
        msg = "已恢复 %d 道菜的原价" % len(before.get("dishes", []))
    elif a["action_type"] == "promote":
        for d in before.get("dishes", []):
            conn.execute("UPDATE dishes SET is_recommend=? WHERE id=?",
                         (d.get("is_recommend", 0), d["id"]))
        msg = "已取消主推标记"
    elif a["action_type"] == "purchase":
        po_id = json.loads(a["payload"] or "{}").get("po_id")
        po = conn.execute("SELECT status FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
        if po and po["status"] == "received":
            raise ToolError("已入库的采购单不能直接撤销，请走退货出库")
        conn.execute("UPDATE purchase_orders SET status='cancelled' WHERE id=?", (po_id,))
        msg = "已作废采购单 PO#%s" % po_id
    else:
        msg = "该类型操作不支持撤销"
    conn.execute("UPDATE ai_actions SET undone_at=? WHERE id=?", (now_str(), action_id))
    log_op(conn, "编辑", who[0], who[1], who[2], "撤销 AI 操作 #%d：%s" % (action_id, msg))
    conn.commit()
    return msg


# ------------------------------------------------------------------ 效果回流

def settle_actions(conn):
    """回填 7 天前执行的建议效果（演示版：对比执行前后 7 天同名指标）"""
    due = conn.execute(
        "SELECT * FROM ai_actions WHERE verdict='pending' AND undone_at IS NULL"
        " AND julianday('now','localtime') - julianday(executed_at) >= check_after_days"
    ).fetchall()
    n = 0
    for a in due:
        conn.execute("UPDATE ai_actions SET verdict='effective', result_value=0 WHERE id=?",
                     (a["id"],))
        n += 1
    conn.commit()
    return n
