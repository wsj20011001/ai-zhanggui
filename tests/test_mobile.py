# -*- coding: utf-8 -*-
"""点餐端集成测试：补上此前为 0 的移动端覆盖。

覆盖四个真实缺陷（都曾经存在）：
  1. 改就餐人数只重绘不落库 → 订单 persons 恒为 1，污染人均消费与客流；
  2. 整单备注只改内存变量 → 下单后备注为空；
  3. 催菜只允许 pending → 真正该催的 making 被拒；
  4. 购物车读取与服务端不一致（GET 应带回 persons/remark）。

用法：venv\\Scripts\\python tests\\test_mobile.py
测试会在临时库副本上跑，不污染演示数据。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DB_PATH                      # noqa: E402

fails = []


def chk(desc, expected, actual):
    ok = expected == actual
    print(("  ✓ " if ok else "  ✗ ") + desc + "  %r vs %r" % (expected, actual))
    if not ok:
        fails.append(desc)


def main():
    # 用临时库副本：测试会写购物车、下单、改状态
    tmpdir = Path(tempfile.mkdtemp(prefix="mobile_test_"))
    db = tmpdir / "t.db"
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(db))
    src.backup(dst)
    src.close()
    dst.close()

    import config
    old_db = config.DB_PATH
    config.DB_PATH = db
    try:
        from app import create_app
        app = create_app()
        app.config["TESTING"] = True

        with app.test_client() as c:
            with c.session_transaction() as s:
                s["_csrf_token"] = "t"
            H = {"X-CSRF-Token": "t"}

            print("== 1. 员工登录 ==")
            r = c.post("/login", data={"employee_no": "1001", "password": "123456",
                                         "_csrf": "t"}, headers=H)
            chk("登录成功（302 跳桌台）", 302, r.status_code)
            # 登录会 session.clear()（防会话固定），必须重新取令牌
            with c.session_transaction() as sess:
                H = {"X-CSRF-Token": sess.get("_csrf_token") or "t"}

            print("== 2. 菜单与桌台接口 ==")
            chk("菜单 200", 200, c.get("/api/menu").status_code)
            tables = c.get("/api/tables").get_json()
            chk("桌台接口有数据", True, bool(tables))
            rows = tables.get("tables") if isinstance(tables, dict) else tables
            table_id = rows[0]["id"]

            print("== 3. 购物车带人数与备注（缺陷 1、2）==")
            menu = c.get("/api/menu").get_json()
            dish = menu["dishes"][0]          # 菜品是顶层扁平列表，不属于分类对象
            items = [{"key": str(dish.get("id")), "dish_id": dish.get("id"),
                      "name": dish.get("name"), "price": dish.get("price"),
                      "qty": 2, "flavor": ""}]
            r = c.post("/api/cart/%d" % table_id,
                       json={"items": items, "persons": 3, "remark": "少辣，米饭晚点上"},
                       headers=H)
            chk("保存购物车 ok", True, r.get_json().get("ok"))
            back = c.get("/api/cart/%d" % table_id).get_json()
            chk("人数被保存", 3, back.get("persons"))
            chk("备注被保存", "少辣，米饭晚点上", back.get("remark"))

            print("== 4. 下单后订单字段 ==")
            r = c.post("/api/order/%d/submit" % table_id, json={}, headers=H)
            j = r.get_json()
            chk("下单 ok", True, j.get("ok"))
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            o = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 1").fetchone()
            chk("订单 persons=3", 3, o["persons"])
            chk("订单备注写入", "少辣，米饭晚点上", o["remark"])
            oid = o["id"]

            print("== 5. 催菜状态（缺陷 3）==")
            chk("pending 可催", True,
                c.post("/api/order/%d/hurry" % oid, json={}, headers=H).get_json().get("ok"))
            conn.execute("UPDATE orders SET status='making' WHERE id=?", (oid,))
            conn.commit()
            chk("making 也可催（修复前被拒）", True,
                c.post("/api/order/%d/hurry" % oid, json={}, headers=H).get_json().get("ok"))
            conn.execute("UPDATE orders SET status='done' WHERE id=?", (oid,))
            conn.commit()
            chk("done 不可催", False,
                c.post("/api/order/%d/hurry" % oid, json={}, headers=H).get_json().get("ok"))
            conn.close()
    finally:
        config.DB_PATH = old_db
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    if fails:
        print("失败 %d 项：" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过：点餐端人数/备注落库、催菜状态、接口返回一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
