# -*- coding: utf-8 -*-
"""SQL 沙箱 —— 让模型查数，但不给它改数的能力。

五道闸：
  ① 只读连接      ：query_only=ON，任何写操作直接被 SQLite 拒绝
  ② 视图白名单    ：只允许 v_* 语义视图，原始表（含会员手机号、口令）不可见
  ③ 语法校验      ：只允许单条 SELECT，禁 DDL/DML/ATTACH/PRAGMA/UNION
  ④ 强制约束      ：自动补 LIMIT，超限截断，5 秒超时中断
  ⑤ 结果上限      ：最多 500 行，避免把大结果集塞进 prompt

注意：这里刻意不用"正则黑名单"当唯一防线——黑名单永远会被绕过。
真正的安全来自 ①（数据库层面拒绝写）+ ②（看不到敏感表），
③④⑤ 只是把明显越界的请求挡在更早的地方，给出更友好的错误。
"""
import re
import sqlite3
import time

from config import DB_PATH

# 唯一允许访问的对象：全部是视图，且都是聚合/脱敏后的
ALLOWED_VIEWS = {
    "v_sales_daily", "v_dish_sales", "v_category_sales", "v_hour_traffic",
    "v_pay_daily", "v_dish_menu", "v_stock_status", "v_member_rank",
}

MAX_ROWS = 500
TIMEOUT_SEC = 6.0

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|"
    r"vacuum|reindex|trigger|grant|load_extension|sqlite_master|sqlite_schema)\b",
    re.I)
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)


class SqlRejected(Exception):
    pass


def strip_comments(sql):
    sql = re.sub(r"/\*.*?\*/", " ", sql or "", flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql.strip()


def validate(sql):
    """返回可直接执行的 SQL；不合法则抛 SqlRejected"""
    s = strip_comments(sql)
    if not s:
        raise SqlRejected("空查询")
    if not re.match(r"^select\b", s, re.I):
        raise SqlRejected("只允许 SELECT 查询")
    body = s.strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()     # 只允许一个结尾分号
    if ";" in body:
        raise SqlRejected("不允许一次提交多条语句")
    if _FORBIDDEN.search(body):
        raise SqlRejected("查询包含被禁止的关键字（写操作/系统表）")
    if re.search(r"\bunion\b", body, re.I):
        raise SqlRejected("不支持 UNION，请改用单视图聚合")
    refs = {m.lower() for m in _TABLE_REF.findall(body)}
    if not refs:
        raise SqlRejected("查询必须引用语义视图（v_*）")
    illegal = refs - ALLOWED_VIEWS
    if illegal:
        raise SqlRejected("不允许访问：%s（可用：%s）"
                          % ("、".join(sorted(illegal)), "、".join(sorted(ALLOWED_VIEWS))))
    # 强制行数上限
    m = re.search(r"\blimit\s+(\d+)", body, re.I)
    if m:
        if int(m.group(1)) > MAX_ROWS:
            body = re.sub(r"\blimit\s+\d+", "LIMIT %d" % MAX_ROWS, body, count=1, flags=re.I)
    else:
        body += " LIMIT %d" % MAX_ROWS
    return body


def run(sql, params=()):
    """校验并执行，返回 {columns, rows, truncated, elapsed_ms}"""
    safe = validate(sql)
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    deadline = time.time() + TIMEOUT_SEC
    conn.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 20000)
    try:
        conn.execute("PRAGMA query_only=ON")
        t0 = time.time()
        cur = conn.execute(safe, params)
        rows = cur.fetchmany(MAX_ROWS + 1)
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
        cols = [d[0] for d in (cur.description or [])]
        return {
            "columns": cols,
            "rows": [{c: r[c] for c in cols} for r in rows],
            "truncated": truncated,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "sql": safe,
        }
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e).lower():
            raise SqlRejected("查询超时（超过 %.0f 秒），请缩小时间范围" % TIMEOUT_SEC)
        raise SqlRejected("SQL 执行失败：%s" % e)
    finally:
        conn.close()


def view_schema(conn):
    """给 prompt 用的视图结构：列名 + 注释（列注释从 CREATE VIEW 的语义手工维护）"""
    out = {}
    for v in sorted(ALLOWED_VIEWS):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % v)]
        if cols:
            out[v] = cols
    return out
