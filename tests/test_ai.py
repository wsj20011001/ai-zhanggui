# -*- coding: utf-8 -*-
"""AI 层功能测试：问答、SQL 沙箱、洞察执行、撤销。

无模型也能跑：问答会走规则路径，正好验证"降级可用"。
测试跑在**临时库副本**上：本套件会执行洞察（可能调价、设主推）并生成采购单，
直接在演示库上跑会让驾驶舱的洞察变成"已执行"、菜品价被改，演示数字当场变形。
用法：venv\\Scripts\\python tests\\test_ai.py
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

QUESTIONS = [
    "近 30 天营收趋势",
    "上个月哪 5 道菜毛利最高",
    "现在哪些食材快没了",
    "最近折扣是不是变多了",
    "会员里谁消费最多",
    "最近 30 天各分类卖得怎么样",
    "一天中哪个时段最忙",
]

BLOCKED = [
    "SELECT * FROM members",
    "SELECT * FROM settings",
    "DROP TABLE orders",
    "SELECT 1; DELETE FROM orders",
    "SELECT * FROM v_sales_daily UNION SELECT * FROM settings",
    "SELECT * FROM v_sales_daily;;",
    "ATTACH DATABASE 'x.db' AS x",
    "PRAGMA table_info(users)",
]


def main():
    fails = []

    # ---- 0. 准备临时库副本（必须在 import app / sandbox 之前改 DB_PATH）----
    from config import DB_PATH as SRC_DB
    tmpdir = Path(tempfile.mkdtemp(prefix="ai_test_"))
    db_path = tmpdir / "test.db"
    src = sqlite3.connect(str(SRC_DB))
    dst = sqlite3.connect(str(db_path))
    src.backup(dst)
    src.close()
    dst.close()

    import config
    config.DB_PATH = db_path

    from ai import sandbox
    from app import create_app

    # ---- 1. 沙箱拦截 ----
    for sql in BLOCKED:
        try:
            sandbox.validate(sql)
            fails.append("本应拦截但通过了：%s" % sql)
        except sandbox.SqlRejected:
            pass
    # 合法查询应通过，并自动补 LIMIT
    ok_sql = sandbox.validate("SELECT d, revenue FROM v_sales_daily ORDER BY d DESC")
    assert "LIMIT" in ok_sql.upper(), ok_sql
    print("✓ 沙箱：%d 条越界语句全部拦截，合法查询自动补 LIMIT" % len(BLOCKED))

    # 钉成规则模式：不消耗云端额度（临时库，无需还原）
    _c = sqlite3.connect(str(db_path))
    _c.execute("UPDATE settings SET value='off' WHERE key='ai_provider'")
    _c.commit()
    _c.close()

    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        with c.session_transaction() as s:
            s["admin_logged_in"] = True
            s["_csrf_token"] = "test-token"
        hdr = {"X-CSRF-Token": "test-token"}

        # ---- 2. 问答 ----
        for q in QUESTIONS:
            r = c.post("/api/ai/ask", json={"q": q}, headers=hdr)
            d = r.get_json()
            if not d.get("ok"):
                fails.append("问答失败：%s → %s" % (q, d.get("msg")))
                continue
            data = d["data"]
            print("✓ 问答 %-22s → %d 行 / %s / %s"
                  % (q[:20], len(data["rows"]), data["provider"], data["chart"]))

        # ---- 3. 洞察执行（采购单）----
        r = c.get("/admin/ai", follow_redirects=True)
        if r.status_code != 200:
            fails.append("驾驶舱打不开")

        rows = c.get("/admin/ai/insights").data.decode("utf-8")
        ids = [int(x) for x in re.findall(r"execInsight\((\d+)", rows)]
        ids_again = [int(x) for x in re.findall(
            r"execInsight\((\d+)", c.get("/admin/ai/insights").data.decode("utf-8"))]
        if ids != ids_again:
            fails.append("洞察 id 在刷新后漂移：%s → %s" % (ids[:3], ids_again[:3]))
        else:
            print("✓ 洞察 id 稳定（刷新两次一致）：%s" % ids[:3])
        if not ids:
            fails.append("没有生成洞察，无法测试执行")
        else:
            for iid in ids[:3]:
                r = c.post("/api/ai/insight/%d/execute" % iid, json={}, headers=hdr)
                d = r.get_json()
                print("✓ 执行洞察 #%d → %s" % (iid, ("成功：" + d.get("msg", "")[:40]) if d.get("ok") else d.get("msg")))
                if not d.get("ok"):
                    fails.append("执行洞察 #%d 失败：%s" % (iid, d.get("msg")))

            # 幂等：同一条洞察再执行一次必须被拒（两个标签页各点一次的场景）
            r = c.post("/api/ai/insight/%d/execute" % ids[0], json={}, headers=hdr)
            d = r.get_json()
            if d.get("ok"):
                fails.append("同一条洞察被重复执行了（会生成重复采购单）")
            else:
                print("✓ 重复执行被拒：#%d → %s" % (ids[0], d.get("msg")))

        # ---- 4. 撤销 ----
        r = c.get("/admin/ai", follow_redirects=True)
        txt = r.data.decode("utf-8")
        aids = [int(x) for x in re.findall(r"undoAction\((\d+)", txt)]
        if aids:
            r = c.post("/api/ai/action/%d/undo" % aids[0], json={}, headers=hdr)
            d = r.get_json()
            print("✓ 撤销动作 #%d → %s" % (aids[0], d.get("msg")))
            if not d.get("ok"):
                fails.append("撤销失败：%s" % d.get("msg"))

        # ---- 5. 采购单页面 ----
        for url in ("/admin/purchase", "/admin/ai/chat"):
            rr = c.get(url)
            if rr.status_code != 200:
                fails.append("%s 返回 %d" % (url, rr.status_code))

    shutil.rmtree(tmpdir, ignore_errors=True)
    print("测试跑在临时库副本上，演示库未被修改")

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
