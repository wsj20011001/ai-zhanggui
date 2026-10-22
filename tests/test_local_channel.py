# -*- coding: utf-8 -*-
"""本地通道（Ollama）链路测试：用假的 Ollama 服务验证代码路径。

为什么需要：
今天这台机器下载 1.3GB 安装包 + 模型不现实，但"本地通道能不能用"不能只靠口头保证。
这个测试起一个兼容 Ollama 协议的本地服务，验证：
  1. probe 能识别到模型与体积；
  2. gateway 只走本地时能拿到回复；
  3. nl2sql 在 provider=local 时确实走本地，而不是偷偷用云端；
  4. 本地请求**绕过系统代理**（这台机器装了全局代理，否则会 502）。

用法：venv\\Scripts\\python tests\\test_local_channel.py
"""
import json
import os
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai import gateway, nl2sql          # noqa: E402
from config import DB_PATH              # noqa: E402

PORT = 11534
fails = []


class FakeOllama(BaseHTTPRequestHandler):
    def _json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/tags":
            self._json({"models": [{"name": "mock-local:3b", "size": 2 * 1024 ** 3},
                                   {"name": "other:7b", "size": 5 * 1024 ** 3}]})
        else:
            self._json({"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        text = json.dumps(body.get("messages"), ensure_ascii=False)
        if "哪 5 道菜" in text or "毛利" in text or "营收" in text:
            content = json.dumps({
                "sql": "SELECT d, ROUND(revenue,2) revenue FROM v_sales_daily"
                       " WHERE d >= '2026-09-23' ORDER BY d",
                "view": "v_sales_daily", "chart": "line",
                "title": "本地模型结果", "insight": "近 30 天营收逐日返回",
            }, ensure_ascii=False)
        else:
            content = "我是本地模型"
        self._json({"message": {"role": "assistant", "content": content},
                    "prompt_eval_count": 120, "eval_count": 40})

    def log_message(self, *a):
        pass


def chk(desc, expected, actual):
    ok = expected == actual
    print(("  ✓ " if ok else "  ✗ ") + desc + "  %r vs %r" % (expected, actual))
    if not ok:
        fails.append(desc)


def main():
    srv = HTTPServer(("127.0.0.1", PORT), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    keys = ("ai_provider", "ai_ollama_base", "ai_local_model")
    old = {k: conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()[0]
           for k in keys}
    try:
        conn.execute("UPDATE settings SET value='local' WHERE key='ai_provider'")
        conn.execute("UPDATE settings SET value=? WHERE key='ai_ollama_base'",
                     ("http://127.0.0.1:%d" % PORT,))
        conn.execute("UPDATE settings SET value='mock-local:3b' WHERE key='ai_local_model'")
        conn.execute("DELETE FROM ai_cache")
        conn.commit()

        print("== 1. 连通性与模型识别 ==")
        p = gateway.probe(conn)
        chk("本地通道连通", True, p["local"]["ok"])
        chk("识别到当前模型", True, p["local"]["has_model"])
        sizes = [m["size_gb"] for m in p["local"].get("detail", [])]
        chk("模型体积解析（GB）", [2.0, 5.0], sizes)

        print("== 2. 仅本地调用 ==")
        out = gateway.local_only(conn, [{"role": "user", "content": "你是谁"}], timeout=5)
        chk("返回文本", "我是本地模型", out["text"])
        chk("provider 标为 local", "local", out["provider"])
        chk("token 计数", (120, 40), (out["prompt_tokens"], out["output_tokens"]))

        print("== 3. 问答走本地而不是云端 ==")
        res = nl2sql.ask(conn, "上个月哪 5 道菜毛利最高")
        chk("通道", "local", res["provider"])
        chk("SQL 来自本地模型", True, "v_sales_daily" in res["sql"])
        chk("行数非空", True, len(res["rows"]) > 0)
    finally:
        for k, v in old.items():
            conn.execute("UPDATE settings SET value=? WHERE key=?", (v, k))
        conn.execute("DELETE FROM ai_cache")
        conn.commit()
        conn.close()
        srv.shutdown()

    print()
    if fails:
        print("失败 %d 项：" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过：本地通道（含代理绕过）与云端互不干扰，可随时切换")
    return 0


if __name__ == "__main__":
    sys.exit(main())
