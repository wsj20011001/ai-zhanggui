# -*- coding: utf-8 -*-
"""页面冒烟测试：不开浏览器、不发网络请求，直接用 Flask test client 过一遍。

用法：venv\\Scripts\\python tests\\smoke.py
任何模板语法错误、路由 500、context 变量缺失都会在这里暴露。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app          # noqa: E402

ROUTES = [
    "/admin",
    "/admin/ai",
    "/admin/ai/chat",
    "/admin/ai/insights",
    "/admin/ai/settings",
    "/admin/analytics/overview",
    "/admin/analytics/dishes",
    "/admin/analytics/traffic",
    "/admin/analytics/forecast",
    "/admin/analytics/members",
    "/admin/purchase",
    "/admin/ops",
    "/admin/cashier",
    "/admin/orders",
    "/admin/dishes",
    "/admin/members",
    "/admin/stock",
    "/admin/reports/daily",
    "/admin/reports/profit",
    "/admin/banquet",
]


def main():
    app = create_app()
    app.config["TESTING"] = True
    ok, bad = 0, 0
    with app.test_client() as c:
        with c.session_transaction() as s:
            s["admin_logged_in"] = True
        for url in ROUTES:
            try:
                r = c.get(url, follow_redirects=True)
                flag = "OK " if r.status_code == 200 else "BAD"
                if r.status_code == 200:
                    ok += 1
                else:
                    bad += 1
                print("%s %-34s %d  %6d bytes" % (flag, url, r.status_code, len(r.data)))
            except Exception as e:
                bad += 1
                print("ERR %-34s %s: %s" % (url, type(e).__name__, e))
    print("\n通过 %d / 失败 %d" % (ok, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
