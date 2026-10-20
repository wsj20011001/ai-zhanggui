# -*- coding: utf-8 -*-
"""用本地 Mock 服务验证「云端模型」全链路，不需要真 Key。

验证三件事：
  1. gateway 能正确解析 OpenAI 兼容响应；
  2. 模型给出越界 SQL（SELECT * FROM members）时，沙箱拦截后能自修复一轮；
  3. 最终结果来自真实查询，不是模型编的。

用法：venv\\Scripts\\python tests\\mock_llm.py
"""
import json
import os
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                  # noqa: E402
from ai import gateway, nl2sql                 # noqa: E402
from config import DB_PATH                     # noqa: E402

GOOD = {
    "sql": "SELECT name, ROUND(SUM(profit),2) 毛利 FROM v_dish_sales "
           "WHERE d >= '2026-09-01' AND d <= '2026-09-30' GROUP BY name "
           "ORDER BY 毛利 DESC LIMIT 5",
    "view": "v_dish_sales", "chart": "bar", "title": "上月毛利 Top5",
    "insight": "上月毛利最高的是白灼基围虾，单月贡献 1.1 万元",
}
BAD = {
    "sql": "SELECT * FROM members", "view": "members", "chart": "table",
    "title": "会员", "insight": "越界查询",
}


class Handler(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        messages = body.get("messages") or []
        text = json.dumps(messages, ensure_ascii=False)
        Handler.calls += 1
        # 第一次返回越界 SQL（用来自修复），第二次返回合法 SQL
        payload = BAD if Handler.calls == 1 else GOOD
        resp = {
            "id": "mock", "object": "chat.completion",
            "choices": [{"index": 0, "message": {
                "role": "assistant",
                "content": "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```",
            }, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 320, "completion_tokens": 90},
        }
        data = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def main():
    srv = HTTPServer(("127.0.0.1", 8777), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    old = {k: conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()["value"]
           for k in ("ai_provider", "ai_cloud_base", "ai_cloud_model")}
    old_key = config.AI_KEY_FILE.read_text(encoding="utf-8") if config.AI_KEY_FILE.exists() else ""
    fails = []
    try:
        conn.execute("UPDATE settings SET value='cloud' WHERE key='ai_provider'")
        conn.execute("UPDATE settings SET value='http://127.0.0.1:8777/v1' WHERE key='ai_cloud_base'")
        conn.execute("UPDATE settings SET value='mock-model' WHERE key='ai_cloud_model'")
        conn.commit()
        gateway.save_api_key("sk-mock")
        conn.execute("DELETE FROM ai_cache")
        conn.commit()

        res = nl2sql.ask(conn, "上个月哪 5 道菜毛利最高")
        print("provider =", res["provider"], "| model =", res["model"])
        print("sql      =", res["sql"])
        print("rows     =", len(res["rows"]), "| 首行 =", res["rows"][0] if res["rows"] else None)
        print("修复轮次 =", Handler.calls - 1)

        if res["provider"] != "cloud":
            fails.append("应走云端通道，实际 %s" % res["provider"])
        if len(res["rows"]) != 5:
            fails.append("应返回 5 行，实际 %d" % len(res["rows"]))
        if "v_dish_sales" not in res["sql"]:
            fails.append("最终 SQL 不是修复后的版本")
        if Handler.calls < 2:
            fails.append("没有触发自修复（模型只被调用 %d 次）" % Handler.calls)
    finally:
        for k, v in old.items():
            conn.execute("UPDATE settings SET value=? WHERE key=?", (v, k))
        conn.commit()
        conn.close()
        gateway.save_api_key(old_key)
        srv.shutdown()

    if fails:
        print("\n失败：")
        for f in fails:
            print("  -", f)
        return 1
    print("\n通过：越界 SQL 被拦截 → 自动修复 → 返回真实查询结果")
    return 0


if __name__ == "__main__":
    sys.exit(main())
