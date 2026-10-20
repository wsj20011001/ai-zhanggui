# -*- coding: utf-8 -*-
"""受控自然语言查询（NL2SQL）。

与"让模型自由写 SQL"的区别：
- 模型只能看见 8 个语义视图，看不到原始表；
- SQL 必须过 sandbox 五道闸才能执行；
- 执行失败只给一次自修复机会，再失败就降级为规则查询；
- 模型输出的 insight 必须引用结果里的数字，前端会把 SQL 摊开给老板看。
"""
import json
import re
from datetime import date, datetime, timedelta

import config
from ai import gateway, sandbox

# 视图说明：既是 prompt 的一部分，也是"能力边界"的书面定义
VIEW_DOC = {
    "v_sales_daily": ("每日经营汇总。列：d(日期), weekday(星期), revenue(营收), orders(单数), "
                      "persons(客流), avg_price(客单价), discount(让利额), discount_rate(折扣率0-1), "
                      "cost(食材成本), profit(毛利), margin_rate(毛利率0-1), member_orders(会员单数), "
                      "member_rate(会员占比)"),
    "v_dish_sales": ("菜品×日期销量。列：d, dish_id, name(菜名), category(分类), qty(份数), "
                     "amount(销售额), cost(成本), profit(毛利), margin_rate(毛利率)"),
    "v_category_sales": ("分类×日期汇总。列：d, category, qty, amount, cost, profit"),
    "v_hour_traffic": ("时段客流。列：d, hour(0-23), orders, persons, revenue"),
    "v_pay_daily": ("支付方式×日期。列：d, pay_method(cash/wechat/alipay/balance/credit), orders, amount"),
    "v_dish_menu": ("当前菜单快照。列：dish_id, name, category, price(售价), on_sale, price_mode, "
                    "is_seasonal, season, cost(单份成本), margin_rate(毛利率), has_recipe"),
    "v_stock_status": ("食材库存快照。列：ingredient_id, name, unit, stock(库存), warn_threshold(预警值), "
                       "cost(单位成本), stock_value(库存金额), expire_at(保质期), below_warn(1=低于预警)"),
    "v_member_rank": ("会员排行。列：member_id, name, level(等级), balance(余额), points(积分), "
                      "total_spend(累计消费), last_visit_at(最近到店), visit_count(消费次数)"),
}

SYSTEM_PROMPT = """你是「AI 掌柜」的数据分析引擎，服务一家中小餐饮门店的老板。

把老板的问题翻译成 **一条 SQLite SELECT 语句**，并给出结论。

硬约束（违反即失败）：
1. 只能查询下面列出的 8 个视图，禁止访问其他任何表；
2. 只能写单条 SELECT，不要 UNION，子查询最多一层；
3. 只能用视图里已有的列名，不要臆造；
4. 日期是 'YYYY-MM-DD' 文本，用 BETWEEN / >= 比较；今天是 {today}；
5. 金额单位元，比率是 0.xxxx 小数；
6. 「最近N天」用 d >= date('{today}', '-N day')；「本月」用 d >= '{month_start}'；
7. 时间跨度大时按天/按周聚合，不要返回上千行。

只输出 JSON，不要任何解释文字：
{{"sql": "SELECT ...", "view": "使用的视图名", "chart": "line|bar|pie|table",
  "title": "不超过14个字的标题",
  "insight": "一句话结论，必须引用查询结果中的具体数字"}}

可用视图：
{views}
"""

FEWSHOT = [
    {"role": "user", "content": "上个月哪 5 道菜毛利最高"},
    {"role": "assistant", "content": '{"sql": "SELECT name, ROUND(SUM(profit),2) profit, ROUND(SUM(amount),2) amount FROM v_dish_sales WHERE d >= \'2026-09-01\' AND d <= \'2026-09-30\' GROUP BY name ORDER BY profit DESC LIMIT 5", "view": "v_dish_sales", "chart": "bar", "title": "上月毛利 Top5 菜品", "insight": "上月毛利最高的是招牌鸡，单月贡献 1.2 万元"}'},
    {"role": "user", "content": "最近 30 天营收趋势"},
    {"role": "assistant", "content": '{"sql": "SELECT d, ROUND(revenue,2) revenue FROM v_sales_daily WHERE d >= date(\'2026-10-20\', \'-30 day\') ORDER BY d", "view": "v_sales_daily", "chart": "line", "title": "近 30 天营收趋势", "insight": "近 30 天营收合计 28.6 万元，周末单日约为工作日的 1.6 倍"}'},
]


class AskFailed(Exception):
    pass


def _views_text():
    return "\n".join("- %s：%s" % (k, v) for k, v in VIEW_DOC.items())


def _extract_json(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise AskFailed("模型未返回合法 JSON")


def _guess_chart(columns, rows):
    cols = [c.lower() for c in columns]
    has_date = any(c in ("d", "date", "day") for c in cols)
    nums = [c for c in cols if c not in ("d", "weekday", "name", "category", "pay_method", "level")]
    if has_date and nums:
        return "line"
    if len(columns) >= 2 and nums:
        return "bar"
    return "table"


def _period(ql, today):
    """从问题里解析时间范围，返回 (start, end, 中文标签)"""
    if "上个月" in ql or "上月" in ql:
        first = today.replace(day=1)
        end = first - timedelta(days=1)
        return end.replace(day=1), end, "上月"
    if "这个月" in ql or "本月" in ql:
        return today.replace(day=1), today, "本月"
    if "上周" in ql:
        end = today - timedelta(days=today.weekday() + 1)
        return end - timedelta(days=6), end, "上周"
    if "昨天" in ql or "昨日" in ql:
        y = today - timedelta(days=1)
        return y, y, "昨天"
    if "今天" in ql or "今日" in ql:
        return today, today, "今天"
    return today - timedelta(days=29), today, "近30天"


def _rule_answer(conn, q):
    """无模型时的确定性查询：覆盖日常 90% 的问题。

    这不是"降级到不能用的版本"——老板最常问的就是这几类，
    规则路径更快、更准、可解释，而且不花钱。
    判断顺序从具体到宽泛：先认「菜+毛利」，再认「毛利」，
    否则"哪道菜毛利最高"会被当成"整体毛利率"。
    """
    ql = q.lower()
    today = date.today()
    start, end, label = _period(ql, today)
    s, e = start.isoformat(), end.isoformat()

    def wrap(sql, view, chart, title, note):
        return {"sql": sql, "view": view, "chart": chart, "title": title,
                "insight": note, "rule": True}

    if re.search(r"库存|缺货|食材|原料|够.*天", ql):
        return wrap("SELECT name, unit, stock, warn_threshold, stock_value, below_warn"
                    " FROM v_stock_status WHERE below_warn=1 OR stock < warn_threshold*1.5"
                    " ORDER BY stock_value DESC LIMIT 20",
                    "v_stock_status", "table", "库存风险清单",
                    "按库存与预警值对比，以下食材需要补货")
    if re.search(r"会员|复购|老客|回头客|储值", ql):
        return wrap("SELECT name, level, total_spend, visit_count, last_visit_at"
                    " FROM v_member_rank ORDER BY total_spend DESC LIMIT 15",
                    "v_member_rank", "table", "会员消费排行",
                    "按累计消费排序，重点维护前 20% 客群")
    if re.search(r"时段|几点|高峰|翻台|午市|晚市", ql):
        return wrap("SELECT hour, SUM(orders) orders, SUM(persons) persons,"
                    " ROUND(SUM(revenue),2) revenue FROM v_hour_traffic"
                    " WHERE d BETWEEN '%s' AND '%s' GROUP BY hour ORDER BY hour" % (s, e),
                    "v_hour_traffic", "bar", "%s时段分布" % label,
                    "高峰时段决定排班与备料节奏")
    if re.search(r"支付|微信|支付宝|现金|结账方式", ql):
        return wrap("SELECT pay_method, SUM(orders) orders, ROUND(SUM(amount),2) amount"
                    " FROM v_pay_daily WHERE d BETWEEN '%s' AND '%s' GROUP BY pay_method"
                    " ORDER BY amount DESC" % (s, e),
                    "v_pay_daily", "pie", "%s支付方式" % label,
                    "支付结构影响对账效率与手续费")
    if re.search(r"折扣|让利|优惠|满减", ql):
        return wrap("SELECT d, ROUND(discount_rate*100,1) 折扣率, ROUND(discount,2) 让利额"
                    " FROM v_sales_daily WHERE d BETWEEN '%s' AND '%s' ORDER BY d" % (s, e),
                    "v_sales_daily", "line", "%s折扣率" % label,
                    "折扣率持续抬升会直接吃掉毛利")
    if re.search(r"菜|单品|招牌|卖得", ql) and re.search(r"毛利|利润|赚", ql):
        return wrap("SELECT name, SUM(qty) 份数, ROUND(SUM(amount),2) 销售额,"
                    " ROUND(SUM(profit),2) 毛利 FROM v_dish_sales"
                    " WHERE d BETWEEN '%s' AND '%s' GROUP BY name"
                    " ORDER BY 毛利 DESC LIMIT 5" % (s, e),
                    "v_dish_sales", "bar", "%s毛利 Top5" % label,
                    "毛利额 = 销量 × 单份毛利，比毛利率更能反映贡献")
    if re.search(r"菜|销量|爆款|招牌", ql):
        return wrap("SELECT name, SUM(qty) 份数, ROUND(SUM(amount),2) 销售额,"
                    " ROUND(SUM(profit),2) 毛利 FROM v_dish_sales"
                    " WHERE d BETWEEN '%s' AND '%s' GROUP BY name"
                    " ORDER BY 份数 DESC LIMIT 10" % (s, e),
                    "v_dish_sales", "bar", "%s销量 Top10" % label,
                    "销量靠前的菜要保证不断货，它们决定翻台效率")
    if re.search(r"毛利|利润|成本", ql):
        return wrap("SELECT d, ROUND(revenue,2) 营收, ROUND(cost,2) 成本,"
                    " ROUND(profit,2) 毛利, ROUND(margin_rate*100,1) 毛利率"
                    " FROM v_sales_daily WHERE d BETWEEN '%s' AND '%s' ORDER BY d" % (s, e),
                    "v_sales_daily", "line", "%s毛利走势" % label,
                    "毛利率波动通常来自食材涨价或折扣变多")
    if re.search(r"分类|品类|类别|菜系", ql):
        return wrap("SELECT category, SUM(qty) 份数, ROUND(SUM(amount),2) 销售额,"
                    " ROUND(SUM(profit),2) 毛利 FROM v_category_sales"
                    " WHERE d BETWEEN '%s' AND '%s' GROUP BY category"
                    " ORDER BY 销售额 DESC" % (s, e),
                    "v_category_sales", "bar", "%s分类表现" % label,
                    "分类结构决定菜单重心，也影响采购与后厨动线")
    return wrap("SELECT d, ROUND(revenue,2) 营收, orders 单数, ROUND(avg_price,2) 客单价,"
                " ROUND(margin_rate*100,1) 毛利率 FROM v_sales_daily"
                " WHERE d BETWEEN '%s' AND '%s' ORDER BY d" % (s, e),
                "v_sales_daily", "line", "%s经营趋势" % label,
                "近 30 天的营收、单数与客单价走势")


def _run_plan(plan, conn, question, provider, model, latency):
    res = sandbox.run(plan["sql"])
    rows = res["rows"]
    columns = res["columns"]
    chart = plan.get("chart") or _guess_chart(columns, rows)
    if chart not in ("line", "bar", "pie", "table"):
        chart = _guess_chart(columns, rows)
    return {
        "ok": True,
        "question": question,
        "title": plan.get("title") or question[:14],
        "insight": plan.get("insight") or "",
        "sql": res["sql"],
        "view": plan.get("view") or "",
        "columns": columns,
        "rows": rows,
        "chart": chart,
        "provider": provider,
        "model": model,
        "latency_ms": latency,
        "rule": plan.get("rule", False),
        "truncated": res["truncated"],
    }


def ask(conn, question, allow_cloud=True):
    """主入口。永远返回可展示的结果：模型不可用时自动走规则路径。"""
    question = (question or "").strip()
    if not question:
        raise AskFailed("问题不能为空")
    today = date.today()
    sys_prompt = SYSTEM_PROMPT.format(
        today=today.isoformat(), month_start=today.replace(day=1).isoformat(),
        views=_views_text())
    messages = [{"role": "system", "content": sys_prompt}] + FEWSHOT + \
               [{"role": "user", "content": question}]

    provider, model, latency = "rule", "", 0
    try:
        out = gateway.chat(conn, messages, json_mode=True, allow_cloud=allow_cloud)
        plan = _extract_json(out["text"])
        provider, model, latency = out["provider"], out["model"], out["latency_ms"]
        try:
            res = _run_plan(plan, conn, question, provider, model, latency)
            _log(conn, res)
            return res
        except sandbox.SqlRejected as e:
            # 给模型一次自修复机会（这是 NL2SQL 里最有效的提准手段）
            repair = messages + [
                {"role": "assistant", "content": out["text"]},
                {"role": "user", "content": "上一条 SQL 被拒绝：%s。请修正后重新输出同样的 JSON。"
                                            "只能使用允许的视图。" % e}]
            out2 = gateway.chat(conn, repair, json_mode=True, allow_cloud=allow_cloud)
            plan2 = _extract_json(out2["text"])
            res = _run_plan(plan2, conn, question, out2["provider"], out2["model"],
                            latency + out2["latency_ms"])
            _log(conn, res)
            return res
    except (gateway.ModelUnavailable, AskFailed, sandbox.SqlRejected, KeyError) as e:
        err = str(e)
        plan = _rule_answer(conn, question)
        try:
            res = _run_plan(plan, conn, question, "rule", "", 0)
        except sandbox.SqlRejected:
            raise AskFailed("无法理解这个问题，试试换个说法，例如「近 30 天营收趋势」")
        res["fallback_reason"] = err
        _log(conn, res)
        return res


def _log(conn, res):
    try:
        conn.execute(
            "INSERT INTO ai_conversations(question, answer, sql, view_name, rows_count,"
            " provider, model, latency_ms, ok, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,1, datetime('now','localtime'))",
            (res["question"], res.get("insight", ""), res.get("sql", ""),
             res.get("view", ""), len(res.get("rows") or []),
             res.get("provider", ""), res.get("model", ""), res.get("latency_ms", 0)))
        conn.commit()
    except Exception:
        pass
