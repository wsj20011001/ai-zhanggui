# -*- coding: utf-8 -*-
r"""单元测试：金额口径、订单号、口令哈希。
运行：venv\Scripts\python test_units.py
不依赖 pytest，也不碰真实数据库（使用临时库）。
"""
import atexit
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config                      # noqa: E402
import db                          # noqa: E402
import utils                       # noqa: E402

# 把数据库指向临时文件，绝不污染 data/restaurant.db
_TMPDIR = tempfile.mkdtemp(prefix="cgy_test_")
atexit.register(shutil.rmtree, _TMPDIR, ignore_errors=True)   # 退出时自动清理临时库
_TMP = Path(_TMPDIR) / "t.db"
config.DB_PATH = db.DB_PATH = utils.DB_PATH = _TMP

PASS = FAIL = 0


def chk(desc, expected, actual):
    global PASS, FAIL
    if expected == actual:
        PASS += 1
        print("  ✅", desc)
    else:
        FAIL += 1
        print("  ❌", desc, "期望 %r 实际 %r" % (expected, actual))


def main():
    db.init_db()
    from flask import Flask
    app = Flask(__name__)
    app.teardown_appcontext(db.close_db)   # 与真实应用一致，请求结束即释放数据库文件
    ctx = app.app_context()
    ctx.push()
    conn = db.get_db()

    def set_setting(key, val):
        conn.execute("UPDATE settings SET value=? WHERE key=?", (val, key))
        conn.commit()

    print("== 金额舍入（必须四舍五入，不能是 Python 内置的银行家舍入）==")
    chk("money(0.125) == 0.13", 0.13, utils.money(0.125))       # round() 会给 0.12
    chk("money(2.675) == 2.68", 2.68, utils.money(2.675))       # round() 会给 2.67
    chk("money(2.505) == 2.51", 2.51, utils.money(2.505))       # round() 会给 2.5
    chk("money(0.1+0.2) == 0.30", 0.3, utils.money(0.1 + 0.2))
    chk("money(None) == 0.0", 0.0, utils.money(None))
    chk("money('abc') == 0.0", 0.0, utils.money("abc"))

    print("\n== 满减 ==")
    set_setting("full_reduction", "100:10,200:25")
    set_setting("member_discount", "1")
    set_setting("points_money_ratio", "100")
    set_setting("points_earn_per_yuan", "1")
    r = utils.calc_order([{"price": 60, "qty": 1}, {"price": 50, "qty": 1}])
    chk("110 元命中 100:10", 10.0, r["reduction"])
    chk("110 元满减后应付 100.0", 100.0, r["final"])
    r = utils.calc_order([{"price": 60, "qty": 1}])
    chk("60 元不满足门槛，无满减", 0.0, r["reduction"])
    r = utils.calc_order([{"price": 210, "qty": 1}])
    chk("210 元取最高档 200:25", 25.0, r["reduction"])

    print("\n== 会员折扣 ==")
    set_setting("full_reduction", "")      # 清掉上一段的满减，单独验证会员折扣
    set_setting("member_discount", "0.95")
    r = utils.calc_order([{"price": 100, "qty": 1}], use_member=True)
    chk("100 元 95 折 = 优惠 5 元", 5.0, r["member_discount"])
    chk("折后 95 元", 95.0, r["final"])
    r = utils.calc_order([{"price": 100, "qty": 1}], use_member=False)
    chk("非会员不打折", 0.0, r["member_discount"])

    print("\n== 折扣叠加顺序：先满减，再会员折扣 ==")
    set_setting("full_reduction", "100:10")
    set_setting("member_discount", "0.9")
    r = utils.calc_order([{"price": 200, "qty": 1}], use_member=True)
    chk("满减 10 元", 10.0, r["reduction"])
    chk("会员折扣按 (200-10)*10% = 19 元", 19.0, r["member_discount"])
    chk("应付 171 元", 171.0, r["final"])

    print("\n== 积分抵扣 ==")
    set_setting("member_discount", "1")
    set_setting("full_reduction", "")
    set_setting("points_money_ratio", "100")
    r = utils.calc_order([{"price": 50, "qty": 1}], points_to_use=200)
    chk("200 分抵 2 元", 2.0, r["points_discount"])
    chk("实扣 200 分", 200, r["points_actually_used"])
    r = utils.calc_order([{"price": 50, "qty": 1}], points_to_use=999999)
    chk("积分不能超过应付（50 元）", 50.0, r["points_discount"])
    chk("实扣恰好 5000 分", 5000, r["points_actually_used"])
    r = utils.calc_order([{"price": 50, "qty": 1}])
    chk("积分抵扣不会导致负数应付", True, r["final"] >= 0)

    print("\n== 积分获取 ==")
    set_setting("points_earn_per_yuan", "1")
    r = utils.calc_order([{"price": 45.6, "qty": 1}])
    chk("45.6 元得 45 分（向下取整）", 45, r["points_earned"])

    print("\n== 订单号并发安全 ==")
    nos = [utils.make_order_no() for _ in range(120)]
    chk("120 次取号无重复", 120, len(set(nos)))
    chk("单号格式正确", True, all(n.startswith("D") and "-" in n for n in nos)
        and all(len(n.split("-")[1]) == 4 for n in nos))

    print("\n== 口令哈希 ==")
    h = utils.make_pwd_hash("abc123")
    chk("是新格式 pbkdf2", True, h.startswith("pbkdf2:"))
    chk("正确密码通过", True, utils.verify_pwd(h, "abc123"))
    chk("错误密码拒绝", False, utils.verify_pwd(h, "abc124"))
    chk("空哈希拒绝", False, utils.verify_pwd("", "abc123"))
    legacy = utils.hash_pwd("abc123")
    chk("历史 SHA-256 仍可登录", True, utils.verify_pwd(legacy, "abc123"))
    chk("历史哈希被标记需升级", True, utils.needs_rehash(legacy))
    chk("新哈希无需升级", False, utils.needs_rehash(h))
    chk("两次哈希不同（有随机盐）", True, utils.make_pwd_hash("x") != utils.make_pwd_hash("x"))

    print("\n== 时价菜：识别与金额重算 ==")
    set_setting("full_reduction", "100:10,200:25")   # 复位（前面用例改过）
    set_setting("member_discount", "1")
    conn.execute("INSERT INTO orders(id, order_no, table_id, table_name, persons,"
                 " status, total_amount, final_amount, created_at)"
                 " VALUES(9001,'TEST-9001',1,'测试桌',2,'pending',0,0,'2026-01-01 00:00:00')")
    for _id, _nm, _pr, _qty, _pend in ((1, "时价菜", 0, 2, 1), (2, "普通菜", 30, 1, 0)):
        conn.execute("INSERT INTO order_items(order_id, dish_id, name, price,"
                     " price_pending, qty, is_combo_child, sort_order)"
                     " VALUES(9001,?,?,?,?,?,0,?)", (_id, _nm, _pr, _pend, _qty, _id))
    conn.commit()
    chk("能识别未定价条目", 1, len(utils.pending_price_items(conn, 9001)))
    chk("订单被判为含待定价", True, utils.order_has_pending_price(conn, 9001))

    conn.execute("UPDATE order_items SET price=128, price_pending=0"
                 " WHERE order_id=9001 AND price_pending=1")
    calc = utils.recalc_order_amounts(conn, 9001)
    conn.commit()
    chk("补价后重算总额 128×2+30", 286.0, calc["total"])
    row = conn.execute("SELECT total_amount, final_amount FROM orders"
                       " WHERE id=9001").fetchone()
    chk("订单表 total_amount 已同步", 286.0, row["total_amount"])
    chk("订单表 final_amount 含满减 200:25", 261.0, row["final_amount"])
    chk("补价后不再含待定价", False, utils.order_has_pending_price(conn, 9001))
    chk("已定价条目不再被列出", 0, len(utils.pending_price_items(conn, 9001)))

    conn.execute("DELETE FROM order_items WHERE order_id=9001")
    conn.execute("DELETE FROM orders WHERE id=9001")
    conn.commit()

    print("\n== 配方(BOM)：成本、可售份数、扣料 ==")
    conn.execute("INSERT INTO ingredients(name, unit, stock, warn_threshold, cost,"
                 " updated_at) VALUES('测试料A','斤',10,1,20,'2026-01-01 00:00:00')")
    conn.execute("INSERT INTO ingredients(name, unit, stock, warn_threshold, cost,"
                 " updated_at) VALUES('测试料B','个',100,1,2,'2026-01-01 00:00:00')")
    conn.execute("INSERT INTO dishes(name, category_id, price, on_sale, created_at)"
                 " VALUES('测试配菜',1,50,1,'2026-01-01 00:00:00')")
    conn.commit()
    ia = conn.execute("SELECT id FROM ingredients WHERE name='测试料A'").fetchone()["id"]
    ib = conn.execute("SELECT id FROM ingredients WHERE name='测试料B'").fetchone()["id"]
    dd = conn.execute("SELECT id FROM dishes WHERE name='测试配菜'").fetchone()["id"]
    conn.execute("INSERT INTO dish_recipes(dish_id, ingredient_id, qty) VALUES(?,?,?)", (dd, ia, 0.5))
    conn.execute("INSERT INTO dish_recipes(dish_id, ingredient_id, qty) VALUES(?,?,?)", (dd, ib, 3))
    conn.commit()
    req = utils.recipe_requirements(conn)
    chk("配方展开为 2 项", 2, len(req.get(dd, [])))
    a = utils.dish_availability(conn)[dd]
    chk("单份成本 0.5×20 + 3×2 = 16.0", 16.0, a["cost"])
    chk("可做份数 min(10/0.5, 100/3) = 20", 20, a["can_make"])
    chk("瓶颈原料是测试料A", "测试料A", a["limited_by"])
    chk("单份毛利 50-16 = 34.0", 34.0, a["margin"])
    chk("毛利率 68%", 0.68, a["margin_rate"])

    conn.execute("INSERT INTO orders(id, order_no, table_id, table_name, persons,"
                 " status, total_amount, final_amount, created_at)"
                 " VALUES(9002,'TEST-9002',1,'测试桌',2,'done',0,0,'2026-01-01 00:00:00')")
    conn.execute("INSERT INTO order_items(order_id, dish_id, name, price, qty,"
                 " is_combo_child, sort_order) VALUES(9002,?, '测试配菜', 50, 3, 0, 1)", (dd,))
    conn.commit()
    n = utils.consume_stock_for_order(conn, 9002, "TEST-9002")
    conn.commit()
    chk("扣料写入 2 条", 2, n)
    st = {r["name"]: r["stock"] for r in conn.execute("SELECT name, stock FROM ingredients")}
    chk("测试料A 扣 0.5×3 = 1.5", 8.5, round(st["测试料A"], 3))
    chk("测试料B 扣 3×3 = 9", 91.0, round(st["测试料B"], 3))
    chk("幂等：再扣一次返回 0", 0, utils.consume_stock_for_order(conn, 9002, "TEST-9002"))
    st2 = {r["name"]: r["stock"] for r in conn.execute("SELECT name, stock FROM ingredients")}
    chk("库存未被重复扣减", 8.5, round(st2["测试料A"], 3))

    conn.execute("DELETE FROM stock_movements WHERE order_id=9002")
    conn.execute("DELETE FROM order_items WHERE order_id=9002")
    conn.execute("DELETE FROM orders WHERE id=9002")
    conn.execute("DELETE FROM dish_recipes WHERE dish_id=?", (dd,))
    conn.execute("DELETE FROM dishes WHERE id=?", (dd,))
    conn.execute("DELETE FROM ingredients WHERE name IN ('测试料A','测试料B')")
    conn.commit()

    ctx.pop()
    print("\n" + "=" * 46)
    print("结果：通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
