# -*- coding: utf-8 -*-
"""数据访问层：连接工厂（g 缓存）、建表、迁移、种子数据"""
import sqlite3

from flask import g

from config import DB_PATH
from utils import make_pwd_hash, now_str

SCHEMA_VERSION = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tables (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    type       TEXT NOT NULL DEFAULT '大厅',
    capacity   INTEGER NOT NULL DEFAULT 4,
    status     TEXT NOT NULL DEFAULT 'idle',
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    course_role TEXT NOT NULL DEFAULT '',   -- 宴席角色：凉菜/热菜/主菜/汤/主食/点心/酒水
    sort_order  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dishes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    category_id  INTEGER NOT NULL REFERENCES categories(id),
    price        REAL NOT NULL DEFAULT 0,
    price_mode   TEXT NOT NULL DEFAULT 'fixed',  -- fixed=固定价 / market=时价
    season       TEXT NOT NULL DEFAULT '',       -- 时令季节：春/夏/秋/冬，空=四季供应
    is_seasonal  INTEGER NOT NULL DEFAULT 0,     -- 1=时令菜（过季自动不可点）
    image        TEXT DEFAULT '',
    description  TEXT DEFAULT '',
    is_recommend INTEGER NOT NULL DEFAULT 0,
    is_featured  INTEGER NOT NULL DEFAULT 0,
    is_combo     INTEGER NOT NULL DEFAULT 0,
    on_sale      INTEGER NOT NULL DEFAULT 1,
    warn_flag    INTEGER NOT NULL DEFAULT 0,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dishes_cat ON dishes(category_id);
CREATE TABLE IF NOT EXISTS combo_items (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    combo_id INTEGER NOT NULL REFERENCES dishes(id),
    dish_id  INTEGER NOT NULL REFERENCES dishes(id),
    qty      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_combo_items_combo ON combo_items(combo_id);
CREATE TABLE IF NOT EXISTS carts (
    table_id   INTEGER PRIMARY KEY REFERENCES tables(id),
    items      TEXT NOT NULL DEFAULT '[]',
    persons    INTEGER NOT NULL DEFAULT 1,
    remark     TEXT DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no        TEXT NOT NULL UNIQUE,
    table_id        INTEGER NOT NULL REFERENCES tables(id),
    table_name      TEXT NOT NULL,
    persons         INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'pending',
    total_amount    REAL NOT NULL DEFAULT 0,
    reduction       REAL NOT NULL DEFAULT 0,
    member_discount REAL NOT NULL DEFAULT 0,
    points_discount REAL NOT NULL DEFAULT 0,
    discount_total  REAL NOT NULL DEFAULT 0,
    final_amount    REAL NOT NULL DEFAULT 0,
    pay_method      TEXT DEFAULT '',
    member_id       INTEGER DEFAULT NULL,
    member_name     TEXT DEFAULT '',
    points_earned   INTEGER NOT NULL DEFAULT 0,
    points_used     INTEGER NOT NULL DEFAULT 0,
    hurry           INTEGER NOT NULL DEFAULT 0,
    remark          TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    confirmed_at    TEXT, making_at TEXT, done_at TEXT, settled_at TEXT, cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_table  ON orders(table_id);
CREATE TABLE IF NOT EXISTS order_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id       INTEGER NOT NULL REFERENCES orders(id),
    dish_id        INTEGER DEFAULT NULL,
    name           TEXT NOT NULL,
    price          REAL NOT NULL,
    price_pending  INTEGER NOT NULL DEFAULT 0,   -- 1=时价待定，结算前必须补价
    price_set_by   TEXT DEFAULT '',              -- 补价人
    price_set_at   TEXT DEFAULT NULL,            -- 补价时间
    spec_name      TEXT DEFAULT '',              -- 规格名（如"笋干烧"）
    spec_delta     REAL NOT NULL DEFAULT 0,      -- 规格加价
    qty            INTEGER NOT NULL DEFAULT 1,
    note           TEXT DEFAULT '',
    is_combo_child INTEGER NOT NULL DEFAULT 0,
    combo_of       INTEGER DEFAULT NULL,
    sort_order     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE TABLE IF NOT EXISTS members (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    phone         TEXT NOT NULL UNIQUE,
    balance       REAL NOT NULL DEFAULT 0,
    points        INTEGER NOT NULL DEFAULT 0,
    total_spend   REAL NOT NULL DEFAULT 0,
    level         TEXT NOT NULL DEFAULT '普通会员',
    created_at    TEXT NOT NULL,
    last_visit_at TEXT DEFAULT NULL,
    remark        TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS member_ledger (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id      INTEGER NOT NULL REFERENCES members(id),
    type           TEXT NOT NULL,
    amount         REAL NOT NULL DEFAULT 0,
    balance_change REAL NOT NULL DEFAULT 0,
    points_change  INTEGER NOT NULL DEFAULT 0,
    order_id       INTEGER DEFAULT NULL,
    note           TEXT DEFAULT '',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_member ON member_ledger(member_id);
CREATE TABLE IF NOT EXISTS reservations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    phone        TEXT DEFAULT '',
    reserve_time TEXT NOT NULL,
    persons      INTEGER NOT NULL DEFAULT 1,
    table_id     INTEGER DEFAULT NULL,
    table_name   TEXT DEFAULT '',
    member_id    INTEGER DEFAULT NULL,     -- 关联会员（用于到店提醒时展示画像）
    status       TEXT NOT NULL DEFAULT 'pending',
    notified     INTEGER NOT NULL DEFAULT 0,
    remark       TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    arrived_at   TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_reservations_time ON reservations(reserve_time);
CREATE TABLE IF NOT EXISTS ingredients (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    unit           TEXT NOT NULL DEFAULT '份',
    stock          REAL NOT NULL DEFAULT 0,
    warn_threshold REAL NOT NULL DEFAULT 0,
    cost           REAL NOT NULL DEFAULT 0,   -- 单位成本（元/单位），用于菜品毛利
    expire_at      TEXT DEFAULT NULL,         -- 保质期（可留空）
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS employees (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_no TEXT NOT NULL UNIQUE,      -- 工号（点餐端登录账号）
    name        TEXT NOT NULL,
    pwd         TEXT NOT NULL,             -- pbkdf2-sha256（历史数据为裸 SHA-256，登录后自动升级）
    role        TEXT NOT NULL DEFAULT '服务员',  -- 服务员/收银员/店长
    active      INTEGER NOT NULL DEFAULT 1, -- 1 启用 / 0 停用
    remark      TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_type  TEXT NOT NULL,   -- admin 管理端 / employee 点餐端
    user_key   TEXT NOT NULL,   -- 工号或管理端账号
    name       TEXT DEFAULT '',
    ip         TEXT DEFAULT '',
    success    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_created ON login_logs(created_at);
CREATE TABLE IF NOT EXISTS op_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    op_type    TEXT NOT NULL,   -- 登录/修改密码/下单/新增/编辑/删除/结算/状态变更/充值/库存/备份/设置
    user_type  TEXT NOT NULL,   -- admin 管理端 / employee 点餐端
    user_key   TEXT NOT NULL,   -- 工号或管理端账号
    user_name  TEXT DEFAULT '',
    detail     TEXT DEFAULT '', -- 操作详情（对象名/订单号/金额等）
    ip         TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_op_logs_time ON op_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_op_logs_type ON op_logs(op_type);
CREATE TABLE IF NOT EXISTS dish_specs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dish_id     INTEGER NOT NULL REFERENCES dishes(id),
    name        TEXT NOT NULL,              -- 规格名，如"啤酒烧"/"笋干烧"/"大份"
    price_delta REAL NOT NULL DEFAULT 0,    -- 相对菜价加价（可为 0）
    is_default  INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_specs_dish ON dish_specs(dish_id);
CREATE TABLE IF NOT EXISTS reservation_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    dish_id        INTEGER DEFAULT NULL,
    name           TEXT NOT NULL,
    price          REAL NOT NULL DEFAULT 0,
    qty            INTEGER NOT NULL DEFAULT 1,
    note           TEXT DEFAULT '',
    spec_name      TEXT DEFAULT '',
    sort_order     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_resv_items ON reservation_items(reservation_id);
CREATE TABLE IF NOT EXISTS dish_recipes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dish_id       INTEGER NOT NULL REFERENCES dishes(id),
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    qty           REAL NOT NULL DEFAULT 0,   -- 单份用量（按食材单位）
    note          TEXT DEFAULT '',
    UNIQUE(dish_id, ingredient_id)
);
CREATE INDEX IF NOT EXISTS idx_recipes_dish ON dish_recipes(dish_id);
CREATE INDEX IF NOT EXISTS idx_recipes_ing  ON dish_recipes(ingredient_id);
CREATE TABLE IF NOT EXISTS stock_movements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    type          TEXT NOT NULL,
    qty           REAL NOT NULL,
    price         REAL DEFAULT 0,
    note          TEXT DEFAULT '',
    order_id      INTEGER DEFAULT NULL,      -- 关联订单，便于按单追溯/回冲
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_movements_ing ON stock_movements(ingredient_id);

-- ===================== 分析层：预聚合事实表 =====================
CREATE TABLE IF NOT EXISTS daily_facts (
    d             TEXT PRIMARY KEY,          -- YYYY-MM-DD
    revenue       REAL NOT NULL DEFAULT 0,
    orders        INTEGER NOT NULL DEFAULT 0,
    persons       INTEGER NOT NULL DEFAULT 0,
    discount      REAL NOT NULL DEFAULT 0,
    cost          REAL NOT NULL DEFAULT 0,
    profit        REAL NOT NULL DEFAULT 0,
    member_orders INTEGER NOT NULL DEFAULT 0,
    lunch_revenue REAL NOT NULL DEFAULT 0,   -- 11:00-15:59
    dinner_revenue REAL NOT NULL DEFAULT 0,  -- 16:00-23:59
    built_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dish_daily_facts (
    d        TEXT NOT NULL,
    dish_id  INTEGER NOT NULL,
    name     TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    qty      INTEGER NOT NULL DEFAULT 0,
    amount   REAL NOT NULL DEFAULT 0,
    cost     REAL NOT NULL DEFAULT 0,
    profit   REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (d, dish_id)
);
CREATE INDEX IF NOT EXISTS idx_ddf_dish ON dish_daily_facts(dish_id);
CREATE TABLE IF NOT EXISTS hour_facts (
    d       TEXT NOT NULL,
    hour    INTEGER NOT NULL,
    orders  INTEGER NOT NULL DEFAULT 0,
    persons INTEGER NOT NULL DEFAULT 0,
    revenue REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (d, hour)
);

-- ===================== AI 层：留痕与决策闭环 =====================
CREATE TABLE IF NOT EXISTS ai_conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    question   TEXT NOT NULL,
    answer     TEXT DEFAULT '',
    sql        TEXT DEFAULT '',
    view_name  TEXT DEFAULT '',
    rows_count INTEGER NOT NULL DEFAULT 0,
    provider   TEXT DEFAULT '',
    model      TEXT DEFAULT '',
    prompt_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    ok         INTEGER NOT NULL DEFAULT 1,
    error      TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_conv_time ON ai_conversations(created_at);

CREATE TABLE IF NOT EXISTS ai_insights (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    d              TEXT NOT NULL,
    type           TEXT NOT NULL,          -- revenue_drop / slow_dish / margin / stock / discount
    severity       TEXT NOT NULL DEFAULT 'info',  -- critical / warning / info
    title          TEXT NOT NULL,
    detail         TEXT DEFAULT '',
    evidence       TEXT DEFAULT '{}',      -- JSON：指标、当前值、基线、阈值、SQL
    suggestion     TEXT DEFAULT '',
    action_type    TEXT DEFAULT '',        -- purchase / price / promote / none
    action_payload TEXT DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'open',  -- open / done / ignored
    ai_written     INTEGER NOT NULL DEFAULT 0,    -- 1=文案由模型润色
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_insight_d ON ai_insights(d);
CREATE INDEX IF NOT EXISTS idx_insight_status ON ai_insights(status);

CREATE TABLE IF NOT EXISTS ai_actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    insight_id    INTEGER DEFAULT NULL,
    action_type   TEXT NOT NULL,
    payload       TEXT DEFAULT '{}',
    before_json   TEXT DEFAULT '{}',       -- 执行前快照，用于一键撤销
    executed_at   TEXT NOT NULL,
    undone_at     TEXT DEFAULT NULL,
    check_after_days INTEGER NOT NULL DEFAULT 7,
    baseline_value REAL DEFAULT 0,
    result_value   REAL DEFAULT NULL,
    verdict        TEXT DEFAULT ''          -- effective / ineffective / pending
);
CREATE INDEX IF NOT EXISTS idx_actions_time ON ai_actions(executed_at);

CREATE TABLE IF NOT EXISTS purchase_orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    status     TEXT NOT NULL DEFAULT 'draft',   -- draft / confirmed / received
    source     TEXT NOT NULL DEFAULT 'ai',      -- ai / manual
    note       TEXT DEFAULT '',
    total_cost REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    received_at TEXT DEFAULT NULL
);
CREATE TABLE IF NOT EXISTS purchase_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id         INTEGER NOT NULL REFERENCES purchase_orders(id),
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    name          TEXT NOT NULL,
    unit          TEXT DEFAULT '',
    qty           REAL NOT NULL DEFAULT 0,
    unit_cost     REAL NOT NULL DEFAULT 0,
    amount        REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_po_items ON purchase_items(po_id);

CREATE TABLE IF NOT EXISTS ai_cache (
    k          TEXT PRIMARY KEY,     -- sha256(model + messages)
    v          TEXT NOT NULL,
    hit        INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- ===================== 只读语义视图（AI 唯一可见范围）=====================
CREATE VIEW IF NOT EXISTS v_sales_daily AS
SELECT d,
       CASE CAST(strftime('%w', d) AS INTEGER)
            WHEN 0 THEN '周日' WHEN 1 THEN '周一' WHEN 2 THEN '周二'
            WHEN 3 THEN '周三' WHEN 4 THEN '周四' WHEN 5 THEN '周五'
            ELSE '周六' END AS weekday,
       revenue, orders, persons,
       CASE WHEN orders > 0 THEN ROUND(revenue / orders, 2) ELSE 0 END AS avg_price,
       discount,
       CASE WHEN revenue + discount > 0
            THEN ROUND(discount / (revenue + discount), 4) ELSE 0 END AS discount_rate,
       cost, profit,
       CASE WHEN revenue > 0 THEN ROUND(profit / revenue, 4) ELSE 0 END AS margin_rate,
       member_orders,
       CASE WHEN orders > 0 THEN ROUND(1.0 * member_orders / orders, 4) ELSE 0 END AS member_rate
FROM daily_facts;

CREATE VIEW IF NOT EXISTS v_dish_sales AS
SELECT f.d,
       f.dish_id,
       f.name,
       f.category,
       f.qty,
       f.amount,
       f.cost,
       f.profit,
       CASE WHEN f.amount > 0 THEN ROUND(f.profit / f.amount, 4) ELSE 0 END AS margin_rate
FROM dish_daily_facts f;

CREATE VIEW IF NOT EXISTS v_category_sales AS
SELECT d, category,
       SUM(qty) AS qty, ROUND(SUM(amount), 2) AS amount,
       ROUND(SUM(cost), 2) AS cost, ROUND(SUM(profit), 2) AS profit
FROM dish_daily_facts GROUP BY d, category;

CREATE VIEW IF NOT EXISTS v_hour_traffic AS
SELECT d, hour, orders, persons, revenue FROM hour_facts;

CREATE VIEW IF NOT EXISTS v_pay_daily AS
SELECT substr(COALESCE(settled_at, created_at), 1, 10) AS d,
       pay_method,
       COUNT(*) AS orders,
       ROUND(SUM(final_amount), 2) AS amount
FROM orders
WHERE status = 'settled'
GROUP BY substr(COALESCE(settled_at, created_at), 1, 10), pay_method;

CREATE VIEW IF NOT EXISTS v_dish_menu AS
SELECT d.id AS dish_id, d.name, c.name AS category, d.price,
       d.on_sale, d.price_mode, d.is_seasonal, d.season,
       ROUND(COALESCE(rc.cost, 0), 2) AS cost,
       CASE WHEN d.price > 0 AND rc.cost IS NOT NULL
            THEN ROUND((d.price - rc.cost) / d.price, 4) ELSE NULL END AS margin_rate,
       CASE WHEN rc.cost IS NULL THEN 0 ELSE 1 END AS has_recipe
FROM dishes d
JOIN categories c ON c.id = d.category_id
LEFT JOIN (
    SELECT r.dish_id, SUM(r.qty * i.cost) AS cost
    FROM dish_recipes r JOIN ingredients i ON i.id = r.ingredient_id
    GROUP BY r.dish_id
) rc ON rc.dish_id = d.id;

CREATE VIEW IF NOT EXISTS v_stock_status AS
SELECT i.id AS ingredient_id, i.name, i.unit, i.stock, i.warn_threshold,
       i.cost, ROUND(i.stock * i.cost, 2) AS stock_value, i.expire_at,
       CASE WHEN i.stock < i.warn_threshold THEN 1 ELSE 0 END AS below_warn
FROM ingredients i;

CREATE VIEW IF NOT EXISTS v_member_rank AS
SELECT m.id AS member_id, m.name, m.level, m.balance, m.points,
       ROUND(m.total_spend, 2) AS total_spend,
       m.last_visit_at,
       (SELECT COUNT(*) FROM orders o
         WHERE o.member_id = m.id AND o.status = 'settled') AS visit_count
FROM members m;
"""

DEFAULT_SETTINGS = {
    "shop_name": "春归燕茶餐厅",
    "shop_phone": "",
    "shop_address": "",
    "admin_user": "admin",
    "admin_pwd": "",          # 首次初始化时写入 make_pwd_hash("admin123")
    "member_discount": "0.95",
    "points_earn_per_yuan": "1",
    "points_money_ratio": "100",
    "full_reduction": "100:10,200:25",
    "lost_days": "90",
    "receipt_footer": "谢谢惠顾，欢迎再次光临！",
    "auto_open_browser": "1",      # 启动时自动打开浏览器
    "rec_mode": "balanced",        # 点餐端「今日主推」策略
    "stock_block_order": "0",      # 1=原料不足时直接拦截下单
    # ---- AI 掌柜 ----
    "ai_provider": "local",        # local=本地Ollama / cloud=云端API / off=仅规则
    "ai_local_model": "qwen2.5:7b-instruct-q4_K_M",
    "ai_cloud_base": "https://api.deepseek.com/v1",
    "ai_cloud_model": "deepseek-chat",
    "ai_privacy_mask": "1",        # 1=手机号/姓名脱敏后才允许出网
    "ai_daily_budget": "300000",   # 每日 token 上限
    "shop_seats": "42",            # 桌位数（翻台率分母）
    "target_margin": "0.60",       # 目标毛利率（毛利预警阈值）
    "demo_mode": "0",              # 1=演示数据（界面上会标注）
}

SEED_TABLES = [
    ("包厢1", "包厢", 8), ("包厢2", "包厢", 8), ("卡座1", "卡座", 4),
    ("卡座2", "卡座", 4), ("大厅1", "大厅", 4), ("大厅2", "大厅", 4), ("大厅3", "大厅", 6),
]

SEED_CATEGORIES = ["春归燕特色", "热菜", "凉菜", "汤品", "主食", "饮品"]

SEED_DISHES = [
    # (名称, 分类, 价格, 描述, 推荐, 特色, 套餐)
    ("春归燕招牌鸡", "春归燕特色", 68.0, "散养走地鸡，秘制酱汁", 1, 1, 0),
    ("春归燕狮子头", "春归燕特色", 48.0, "传统淮扬做法，入口即化", 0, 1, 0),
    ("红烧肉", "热菜", 46.0, "肥而不腻，入口软糯", 1, 0, 0),
    ("小炒黄牛肉", "热菜", 52.0, "鲜嫩牛肉，大火快炒", 1, 0, 0),
    ("清炒时蔬", "热菜", 18.0, "当季时令蔬菜", 0, 0, 0),
    ("凉拌黄瓜", "凉菜", 12.0, "爽口开胃", 0, 0, 0),
    ("皮蛋豆腐", "凉菜", 14.0, "嫩豆腐配皮蛋", 0, 0, 0),
    ("紫菜蛋花汤", "汤品", 16.0, "清淡鲜香", 0, 0, 0),
    ("米饭", "主食", 3.0, "五常大米", 0, 0, 0),
    ("葱油拌面", "主食", 15.0, "葱香浓郁", 0, 0, 0),
    ("柠檬水", "饮品", 8.0, "手打柠檬", 0, 0, 0),
    ("酸梅汤", "饮品", 8.0, "古法熬制", 0, 0, 0),
]


def get_db():
    """每请求一个连接，缓存在 g 上，请求结束自动关闭。

    isolation_level="IMMEDIATE"：写事务一开始就拿写锁，
    避免 SQLite "先读后写" 时的锁升级死锁（多台手机同时点餐会踩）。
    WAL  ：读写不互相阻塞，显著提升并发点餐体验。
    """
    if "db" not in g:
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0,
                               isolation_level="IMMEDIATE")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        g.db = conn
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---- 数据库迁移 ---------------------------------------------------------
# 老库（user_version=0）不会因为 CREATE TABLE IF NOT EXISTS 获得新字段，
# 必须靠这里逐版本升级。每个迁移函数必须幂等。

def _has_column(conn, table, col):
    return any(r[1] == col for r in conn.execute("PRAGMA table_info(%s)" % table))


def _migrate_ledger_balance_change(conn):
    """member_ledger 增加 balance_change：真实余额变动额。

    旧版把"消费金额"直接写进 amount（消费记 -final），但只有余额支付才扣余额，
    现金支付时流水显示扣款、余额却不变 —— 无法对账。
    这里同时回填历史数据（按订单实际支付方式推算）。
    """
    if not _has_column(conn, "member_ledger", "balance_change"):
        conn.execute("ALTER TABLE member_ledger ADD COLUMN balance_change"
                     " REAL NOT NULL DEFAULT 0")
    conn.execute(
        "UPDATE member_ledger SET balance_change = CASE"
        "  WHEN type = 'consume' THEN COALESCE(("
        "      SELECT CASE WHEN o.pay_method = 'balance' THEN -o.final_amount"
        "                  ELSE 0 END"
        "      FROM orders o WHERE o.id = member_ledger.order_id), 0)"
        "  ELSE amount END"
        " WHERE balance_change = 0")


def _migrate_counters(conn):
    """订单号计数器表，并从已有订单回填当日最大序号，避免与历史单号重号"""
    conn.execute("CREATE TABLE IF NOT EXISTS counters ("
                 " name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)")
    conn.execute(
        "INSERT INTO counters(name, value)"
        " SELECT substr(order_no, 2, 8), MAX(CAST(substr(order_no, 11) AS INTEGER))"
        " FROM orders"
        " WHERE order_no GLOB 'D[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9]*'"
        " GROUP BY substr(order_no, 2, 8)"
        " ON CONFLICT(name) DO UPDATE SET value = MAX(value, excluded.value)")


def _migrate_bom(conn):
    """菜品配方(BOM)地基：食材成本/保质期 + 扣料可追溯到订单"""
    for table, col, ddl in (
            ("ingredients", "cost", "REAL NOT NULL DEFAULT 0"),
            ("ingredients", "expire_at", "TEXT DEFAULT NULL"),
            ("stock_movements", "order_id", "INTEGER DEFAULT NULL")):
        if not _has_column(conn, table, col):
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, ddl))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_movements_order"
                 " ON stock_movements(order_id)")


def _migrate_season_spec(conn):
    """时令标识 + 菜品规格组（同一主料不同做法的差异化定价）"""
    for table, col, ddl in (
            ("dishes", "season", "TEXT NOT NULL DEFAULT ''"),
            ("dishes", "is_seasonal", "INTEGER NOT NULL DEFAULT 0"),
            ("order_items", "spec_name", "TEXT DEFAULT ''"),
            ("order_items", "spec_delta", "REAL NOT NULL DEFAULT 0")):
        if not _has_column(conn, table, col):
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, ddl))


def _migrate_reservation(conn):
    """预订关联会员 + 预点菜明细 + 分类的宴席角色"""
    if not _has_column(conn, "reservations", "member_id"):
        conn.execute("ALTER TABLE reservations ADD COLUMN member_id INTEGER DEFAULT NULL")
    if not _has_column(conn, "categories", "course_role"):
        conn.execute("ALTER TABLE categories ADD COLUMN course_role"
                     " TEXT NOT NULL DEFAULT ''")
    # 按现有分类名猜一次宴席角色，之后可在分类管理里改
    guess = {"凉菜": "凉菜", "粤菜": "热菜", "赣菜": "热菜", "精致小炒": "热菜",
             "融合烧菜": "热菜", "时令蔬菜": "热菜", "商务宴请": "主菜",
             "广式靓汤": "汤", "主食": "主食", "鲜榨饮料": "酒水", "饮料": "酒水",
             "红酒": "酒水", "白酒": "酒水", "茶叶": "酒水", "茶水": "酒水",
             "海鲜": "主菜"}
    for name, role in guess.items():
        conn.execute("UPDATE categories SET course_role=? WHERE name=? AND course_role=''",
                     (role, name))


def _migrate_price_mode(conn):
    """时价菜支持：dishes.price_mode + order_items 补价留痕字段。

    历史数据：价格<=0 且描述带【时价】标记的识别为时价菜，
    这样之前下架的那 8 道海鲜/河鲜可直接恢复上架，走"下单后补价"流程。
    """
    for table, col, ddl in (
            ("dishes", "price_mode", "TEXT NOT NULL DEFAULT 'fixed'"),
            ("order_items", "price_pending", "INTEGER NOT NULL DEFAULT 0"),
            ("order_items", "price_set_by", "TEXT DEFAULT ''"),
            ("order_items", "price_set_at", "TEXT DEFAULT NULL")):
        if not _has_column(conn, table, col):
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, ddl))
    conn.execute("UPDATE dishes SET price_mode='market'"
                 " WHERE price <= 0 AND description LIKE '%【时价】%'")


def _migrate_ai_layer(conn):
    """v7：分析层与 AI 层。

    表与视图由 SCHEMA 幂等创建（每次启动 executescript 都会补上），
    这里只负责把新配置项写进老库，避免老库因为缺 key 而读不到默认值。
    """
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))


MIGRATIONS = [
    (1, _migrate_ledger_balance_change),
    (2, _migrate_counters),
    (3, _migrate_price_mode),
    (4, _migrate_bom),
    (5, _migrate_season_spec),
    (6, _migrate_reservation),
    (7, _migrate_ai_layer),
]


def migrate(conn):
    """按 PRAGMA user_version 顺序执行迁移，返回执行到的版本号"""
    cur = conn.execute("PRAGMA user_version").fetchone()[0]
    for ver, fn in MIGRATIONS:
        if cur < ver:
            fn(conn)
            conn.execute("PRAGMA user_version = %d" % ver)
            conn.commit()
            cur = ver
    return cur


def init_db(conn=None):
    """建表 + 迁移 + 首次运行填充种子数据；返回 True 表示本次为首次初始化"""
    own = conn is None
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        migrate(conn)
        first = conn.execute("SELECT COUNT(*) c FROM settings").fetchone()[0] == 0
        if first:
            now = now_str()
            seed = dict(DEFAULT_SETTINGS)
            seed["admin_pwd"] = make_pwd_hash("admin123")
            conn.executemany(
                "INSERT INTO settings(key, value) VALUES(?, ?)",
                list(seed.items()))
            for i, (name, ttype, cap) in enumerate(SEED_TABLES):
                conn.execute(
                    "INSERT INTO tables(name, type, capacity, sort_order) VALUES(?,?,?,?)",
                    (name, ttype, cap, i))
            for i, name in enumerate(SEED_CATEGORIES):
                conn.execute(
                    "INSERT INTO categories(name, sort_order) VALUES(?,?)", (name, i))
            for (name, cat, price, desc, rec, feat, combo) in SEED_DISHES:
                cid = conn.execute(
                    "SELECT id FROM categories WHERE name=?", (cat,)).fetchone()["id"]
                conn.execute(
                    "INSERT INTO dishes(name, category_id, price, description,"
                    " is_recommend, is_featured, is_combo, on_sale, created_at)"
                    " VALUES(?,?,?,?,?,?,?,1,?)",
                    (name, cid, price, desc, rec, feat, combo, now))
        # 员工表为空时补种子账号（老库升级同样生效）：1001 / 123456
        if conn.execute("SELECT COUNT(*) c FROM employees").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO employees(employee_no, name, pwd, role, active,"
                " remark, created_at) VALUES('1001','服务员小张',?, '服务员', 1,"
                " '默认演示账号，请及时修改密码', ?)",
                (make_pwd_hash("123456"), now_str()))
        # 历史登录日志一次性迁移到操作日志表（op_logs 为空且 login_logs 有数据时）
        if conn.execute("SELECT COUNT(*) c FROM op_logs").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO op_logs(op_type, user_type, user_key, user_name,"
                " detail, ip, created_at)"
                " SELECT '登录', user_type, user_key, name,"
                " CASE WHEN success THEN '登录成功' ELSE '登录失败' END,"
                " ip, created_at FROM login_logs")
        conn.commit()
        return first
    finally:
        if own:
            conn.close()
