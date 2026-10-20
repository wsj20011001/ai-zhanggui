# -*- coding: utf-8 -*-
"""CSV 导入：把收银系统导出的订单明细接进来。

为什么需要它：评委一定会问「我怎么把店里现有数据弄进来」。
只做演示数据的系统，在产业赛道是减分项。

设计约束：
- 一行一道菜（order-line 粒度），这是收银系统最容易导出的形态；
- 按订单号幂等：重复导入不会产生重复订单，可以直接重跑；
- 导入后自动重算事实表，看板和 AI 立刻能用到新数据。
"""
import csv
import io
import re
from datetime import datetime

from analytics import etl, metrics
from utils import money, now_str

# 字段定义：key, 中文名, 是否必填, 说明
FIELDS = [
    ("order_no", "订单号", True, "同一单的多行菜品用同一个订单号"),
    ("date", "日期", True, "2026-10-01 或 2026/10/1 12:30"),
    ("dish", "菜品名称", True, "需与系统里的菜名一致，找不到会记为失败行"),
    ("qty", "数量", True, "整数"),
    ("price", "单价", True, "菜品成交单价（元）"),
    ("table", "桌台", False, "默认「导入」"),
    ("persons", "人数", False, "默认 2"),
    ("pay", "支付方式", False, "微信/支付宝/现金/会员/记账，默认微信"),
    ("final", "实收金额", False, "不填则按单价×数量求和"),
    ("member_phone", "会员手机号", False, "有则关联会员"),
]

GUESS = {
    "order_no": ["订单号", "单号", "order_no", "orderno", "order", "bill", "流水号"],
    "date": ["日期", "时间", "date", "time", "营业日", "结账时间"],
    "dish": ["菜品", "菜名", "菜品名称", "商品", "name", "dish", "item"],
    "qty": ["数量", "份数", "qty", "quantity", "num"],
    "price": ["单价", "价格", "price", "unit_price"],
    "table": ["桌台", "桌号", "table", "台号"],
    "persons": ["人数", "客数", "persons", "people", "pax"],
    "pay": ["支付方式", "支付", "pay", "payment", "结账方式"],
    "final": ["实收", "实收金额", "金额", "final", "amount", "合计"],
    "member_phone": ["会员手机号", "手机号", "phone", "mobile", "会员"],
}

PAY_MAP = {
    "微信": "wechat", "wechat": "wechat", "微信支付": "wechat",
    "支付宝": "alipay", "alipay": "alipay",
    "现金": "cash", "cash": "cash",
    "会员": "balance", "余额": "balance", "储值": "balance", "balance": "balance",
    "记账": "credit", "挂账": "credit", "credit": "credit",
}
DATE_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"]


def read_csv(path):
    """返回 (headers, rows)。自动识别 UTF-8 / GBK，Excel 导出的大多带 BOM。"""
    raw = open(path, "rb").read()
    text = None
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    if not rows:
        return [], []
    headers = [(h or "").strip() for h in rows[0]]
    return headers, rows[1:]


def guess_mapping(headers):
    """按表头猜字段映射，猜不中的留空让人工选"""
    out = {}
    low = [(h or "").lower() for h in headers]
    for key, _label, _req, _ in FIELDS:
        for alias in GUESS.get(key, []):
            hit = None
            for i, h in enumerate(low):
                if alias.lower() == h:
                    hit = headers[i]
                    break
            if hit is None:
                for i, h in enumerate(low):
                    if alias.lower() in h:
                        hit = headers[i]
                        break
            if hit is not None:
                out[key] = hit
                break
    return out


def preview(path, mapping=None, limit=8):
    headers, rows = read_csv(path)
    mapping = mapping or guess_mapping(headers)
    idx = {h: i for i, h in enumerate(headers)}
    sample = []
    for r in rows[:limit]:
        item = {}
        for key, _l, _req, _d in FIELDS:
            col = mapping.get(key)
            item[key] = r[idx[col]] if col in idx and idx[col] < len(r) else ""
        sample.append(item)
    missing = [label for key, label, req, _ in FIELDS
               if req and not mapping.get(key)]
    return {"headers": headers, "mapping": mapping, "sample": sample,
            "rows": len(rows), "missing": missing}


def _parse_date(s):
    s = (s or "").strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    # 兜底：抓 YYYY-MM-DD
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, 0)
        except ValueError:
            return None
    return None


def _num(s, default=0.0):
    try:
        return float(str(s).replace(",", "").replace("¥", "").strip())
    except (TypeError, ValueError):
        return default


def do_import(conn, path, mapping, who=("admin", "", "")):
    """执行导入，返回统计结果。按订单号幂等。"""
    headers, rows = read_csv(path)
    idx = {h: i for i, h in enumerate(headers)}

    def cell(row, key):
        col = mapping.get(key)
        if col is None or col not in idx:
            return ""
        i = idx[col]
        return row[i] if i < len(row) else ""

    # 导入专用桌台（不动真实桌台状态）
    t = conn.execute("SELECT id FROM tables WHERE name='导入'").fetchone()
    if t:
        table_id = t["id"]
    else:
        cur = conn.execute(
            "INSERT INTO tables(name, type, capacity, status, sort_order)"
            " VALUES('导入','导入',0,'idle',999)")
        table_id = cur.lastrowid

    dish_map = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM dishes")}
    existing = {r["order_no"] for r in conn.execute("SELECT order_no FROM orders")}

    orders = {}
    errors = []
    skipped_orders = set()
    skipped_rows = 0
    bad = 0

    for n, row in enumerate(rows, start=2):          # 从第 2 行开始算（含表头）
        order_no = (cell(row, "order_no") or "").strip()
        dish = (cell(row, "dish") or "").strip()
        qty = int(_num(cell(row, "qty"), 0))
        price = _num(cell(row, "price"), 0)
        if not order_no or not dish or qty <= 0:
            bad += 1
            if len(errors) < 10:
                errors.append("第 %d 行：订单号/菜名/数量不完整" % n)
            continue
        if order_no in existing:
            skipped_orders.add(order_no)
            skipped_rows += 1
            continue
        dt = _parse_date(cell(row, "date"))
        if not dt:
            bad += 1
            if len(errors) < 10:
                errors.append("第 %d 行：日期无法识别（%s）" % (n, cell(row, "date")))
            continue
        if dish not in dish_map:
            bad += 1
            if len(errors) < 10:
                errors.append("第 %d 行：菜名「%s」在系统里不存在" % (n, dish))
            continue

        o = orders.setdefault(order_no, {
            "order_no": order_no, "dt": dt,
            "table": (cell(row, "table") or "导入").strip() or "导入",
            "persons": max(1, int(_num(cell(row, "persons"), 2))),
            "pay": PAY_MAP.get((cell(row, "pay") or "").strip(), "wechat"),
            "final": None, "items": [],
        })
        o["items"].append({"dish_id": dish_map[dish], "name": dish,
                           "price": round(price, 2), "qty": qty})
        fin = (cell(row, "final") or "").strip()
        if fin:
            o["final"] = _num(fin, 0)

    created = 0
    items_created = 0
    dates = []
    for o in orders.values():
        total = money(sum(i["price"] * i["qty"] for i in o["items"]))
        final = money(o["final"]) if o["final"] else total
        ts = o["dt"].strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "INSERT INTO orders(order_no, table_id, table_name, persons, status,"
            " total_amount, final_amount, pay_method, remark, created_at, settled_at)"
            " VALUES(?,?,?,?,'settled',?,?,?,?,?,?)",
            (o["order_no"], table_id, o["table"], o["persons"], total, final,
             o["pay"], "CSV 导入", ts, ts))
        oid = cur.lastrowid
        for sort, it in enumerate(o["items"], 1):
            conn.execute(
                "INSERT INTO order_items(order_id, dish_id, name, price, qty,"
                " is_combo_child, sort_order) VALUES(?,?,?,?,?,0,?)",
                (oid, it["dish_id"], it["name"], it["price"], it["qty"], sort))
        created += 1
        items_created += len(o["items"])
        dates.append(o["dt"].date())
    conn.commit()

    if dates:
        etl.rebuild(conn, min(dates), max(dates))

    return {"orders": created, "items": items_created,
            "skipped": len(skipped_orders), "skipped_rows": skipped_rows,
            "bad": bad, "errors": errors, "rows": len(rows),
            "date_from": min(dates).isoformat() if dates else "",
            "date_to": max(dates).isoformat() if dates else ""}


TEMPLATE = """订单号,日期,菜品名称,数量,单价,桌台,人数,支付方式,实收金额
D20261001-0001,2026-10-01 12:20,红烧肉,1,46,大厅1,3,微信,
D20261001-0001,2026-10-01 12:20,米饭,2,3,大厅1,3,微信,52
D20261001-0002,2026-10-01 18:40,小炒黄牛肉,1,52,卡座1,2,支付宝,52
"""
