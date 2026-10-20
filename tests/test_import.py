# -*- coding: utf-8 -*-
"""CSV 导入测试：在临时库副本上跑，不碰演示数据。

覆盖：
  1. 表头自动识别（中文列名）；
  2. 正常导入（订单/明细/事实表重算）；
  3. 幂等（同一份文件导两次，第二次全部跳过）；
  4. 失败行（菜名不存在）被计数且不影响其他行。

用法：venv\\Scripts\\python tests\\test_import.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics import importer                     # noqa: E402
from config import DB_PATH                         # noqa: E402

HEAD = "订单号,日期,菜品名称,数量,单价,桌台,人数,支付方式,实收金额"


def build_csv(db_path, path):
    """用库里真实存在的菜名造数据 —— 菜名必须对得上，否则会全部判为失败行"""
    c = sqlite3.connect(str(db_path))
    rows = c.execute("SELECT name, price FROM dishes WHERE on_sale=1 AND price>0"
                     " ORDER BY id LIMIT 2").fetchall()
    c.close()
    (n1, p1), (n2, p2) = rows
    lines = [
        HEAD,
        "T20261018-9001,2026-10-18 12:20,%s,1,%.0f,大厅1,3,微信," % (n1, p1),
        "T20261018-9001,2026-10-18 12:20,%s,2,%.0f,大厅1,3,微信,%.0f" % (n2, p2, p1 + 2 * p2),
        "T20261018-9002,2026-10-18 18:40,%s,1,%.0f,卡座1,2,支付宝,%.0f" % (n1, p1, p1),
        "T20261018-9003,2026-10-18 19:10,不存在的菜,1,30,大厅2,2,现金,30",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return p1, p2

fails = []


def chk(desc, expected, actual):
    ok = expected == actual
    print(("  ✓ " if ok else "  ✗ ") + desc + "  %r vs %r" % (expected, actual))
    if not ok:
        fails.append(desc)


def main():
    tmpdir = Path(tempfile.mkdtemp(prefix="import_test_"))
    db = tmpdir / "t.db"
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(db))
    src.backup(dst)                                 # 用备份 API，避免 WAL 半写
    src.close()
    dst.close()

    csv_path = tmpdir / "orders.csv"
    p1, p2 = build_csv(db, csv_path)

    print("== 1. 表头识别 ==")
    headers, rows = importer.read_csv(str(csv_path))
    chk("识别到 9 列", 9, len(headers))
    mapping = importer.guess_mapping(headers)
    chk("订单号映射到「订单号」", "订单号", mapping.get("order_no"))
    chk("菜品映射到「菜品名称」", "菜品名称", mapping.get("dish"))
    chk("读入 4 行数据", 4, len(rows))

    print("== 2. 导入 ==")
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    res = importer.do_import(conn, str(csv_path), mapping)
    chk("新增 2 单（第三单菜名不存在被拒）", 2, res["orders"])
    chk("新增 3 行明细", 3, res["items"])
    chk("失败 1 行", 1, res["bad"])
    it = conn.execute("SELECT COUNT(*) c FROM order_items oi JOIN orders o ON o.id=oi.order_id"
                      " WHERE o.order_no='T20261018-9001'").fetchone()["c"]
    chk("9001 单有 2 行明细", 2, it)
    total = conn.execute("SELECT final_amount FROM orders WHERE order_no='T20261018-9002'"
                         ).fetchone()["final_amount"]
    chk("9002 实收按单价计", float(p1), total)
    fact = conn.execute("SELECT revenue FROM daily_facts WHERE d='2026-10-18'").fetchone()
    chk("事实表已重算出 2026-10-18", True, fact is not None and fact["revenue"] > 0)

    print("== 3. 幂等 ==")
    res2 = importer.do_import(conn, str(csv_path), mapping)
    chk("第二次新增 0 单", 0, res2["orders"])
    chk("第二次跳过 2 单（已入库的两单）", 2, res2["skipped"])
    chk("第二次仍有 1 行失败（菜名不存在）", 1, res2["bad"])

    conn.close()
    shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    if fails:
        print("失败 %d 项：" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
