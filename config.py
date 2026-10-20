# -*- coding: utf-8 -*-
"""全局配置：路径常量（含 PyInstaller 打包适配）、端口、状态常量"""
import secrets
import sys
from pathlib import Path

PORT = 5000          # 局域网访问端口
FALLBACK_PORT = 5001  # 端口被占用时的备用端口

# 路径：打包后（frozen）BASE_DIR = exe 所在目录（可写：data/、uploads/、备份）
#       RESOURCE_DIR = _MEIPASS（只读：templates/、static/）
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = BASE_DIR

DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = DATA_DIR / "backups"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
DB_PATH = DATA_DIR / "restaurant.db"
RESTORE_PENDING = DATA_DIR / "restore_pending.db"
RESTORE_FLAG = DATA_DIR / "restore.flag"

TEMPLATE_DIR = RESOURCE_DIR / "templates"
STATIC_DIR = RESOURCE_DIR / "static"


# ---- 会话密钥 ----------------------------------------------------------
# 首次启动随机生成并持久化到 data/secret.key。
# 绝不可硬编码：密钥一旦可预测，任何人都能离线签发 admin_logged_in=true
# 的 session cookie，无需密码直接进入管理端。
SECRET_KEY_FILE = DATA_DIR / "secret.key"


def _load_secret_key():
    try:
        if SECRET_KEY_FILE.exists():
            key = SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
            if len(key) >= 32:
                return key
        key = secrets.token_hex(32)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SECRET_KEY_FILE.write_text(key, encoding="utf-8")
        return key
    except OSError:
        # 目录只读等极端情况：退回进程内随机密钥（重启后需重新登录，
        # 但绝不退化成可预测的值）
        return secrets.token_hex(32)


SECRET_KEY = _load_secret_key()

# ---- 安全相关 ----------------------------------------------------------
MAX_UPLOAD_MB = 5          # 单次上传体积上限（MB）
SESSION_LIFETIME_HOURS = 12  # 登录态有效期

# 订单状态（pending→making→done→settled；cancelled 为撤回/作废）
ORDER_STATUS = ["pending", "making", "done", "settled", "cancelled"]
ORDER_STATUS_LABEL = {
    "pending": "待确认", "making": "制作中", "done": "已完成",
    "settled": "已结算", "cancelled": "已撤回",
}
# 允许被结算的订单状态（cancelled 不可结算）
SETTLEABLE_STATUS = ("pending", "making", "done")

# 桌台状态
TABLE_STATUS_LABEL = {"idle": "空闲", "ordering": "点餐中", "occupied": "占用"}
TABLE_TYPES = ["大厅", "卡座", "包厢"]
# 支付方式
PAY_METHODS = ["cash", "wechat", "alipay", "balance", "credit"]
PAY_METHOD_LABEL = {"cash": "现金", "wechat": "微信", "alipay": "支付宝",
                    "balance": "会员余额", "credit": "记账"}
# 时令：季节取值与宴席角色（用于自动排菜）
SEASONS = ["春", "夏", "秋", "冬"]
COURSE_ROLES = ["凉菜", "热菜", "主菜", "汤", "主食", "点心", "酒水", "其他"]
# 点餐端不展示"过季"的时令菜（可在设置里关掉）
DEFAULT_SEASONAL_BLOCK = "1"
# 自动排菜：各宴席环节的预算占比
COURSE_BUDGET = {"凉菜": 0.15, "热菜": 0.40, "主菜": 0.30, "汤": 0.10, "主食": 0.05}
# 自动排菜：不同人数对应的菜品数量
COURSE_COUNT = {
    "default": {"凉菜": 2, "热菜": 6, "主菜": 1, "汤": 1, "主食": 1},
    "small":   {"凉菜": 2, "热菜": 4, "主菜": 1, "汤": 1, "主食": 1},   # <=4 人
    "large":   {"凉菜": 4, "热菜": 8, "主菜": 2, "汤": 1, "主食": 2},   # >=10 人
}

# 菜品推荐策略：(利润权重, 库存充足权重, 临期紧迫权重)
REC_MODES = {"balanced": (40, 30, 20), "profit": (70, 20, 10), "clear": (20, 30, 50)}
REC_MODE_LABEL = {"balanced": "均衡推荐", "profit": "保利润优先", "clear": "清库存优先"}
REC_STOCK_TARGET = 20   # 库存充足满分对应的可做份数
REC_TOP_N = 6           # 点餐端「今日主推」条数
EXPIRE_SOON_DAYS = 7    # 保质期进入该天数内开始计入"临期紧迫"

# 价格类型：fixed=固定价；market=时价（下单时无价，结算前必须补价）
PRICE_MODES = ["fixed", "market"]
PRICE_MODE_LABEL = {"fixed": "固定价", "market": "时价"}
# 结算风控：实收低于"菜品原价合计"该比例时，必须二次确认
LOW_PRICE_RATIO = 0.6

# 允许的图片扩展名（同时校验文件头）
ALLOWED_IMG_EXT = {"jpg", "jpeg", "png", "gif", "webp"}

