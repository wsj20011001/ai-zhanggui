# -*- coding: utf-8 -*-
"""AI 掌柜 · 中小餐饮经营 Agent — 唯一入口
启动：python app.py（或 start.bat）
自动：初始化数据目录/数据库、应用待恢复备份、打印局域网 IP、打开浏览器
"""
import os
import socket
import subprocess
import sys
import threading
import webbrowser

from flask import (Flask, redirect, render_template, request,
                   send_from_directory, session, url_for)

import config
import db
import security
import utils
from auth import admin_required


def create_app():
    app = Flask(__name__,
                template_folder=str(config.TEMPLATE_DIR),
                static_folder=str(config.STATIC_DIR))
    app.secret_key = config.SECRET_KEY

    # ---- 安全与会话配置 ----
    app.config.update(
        MAX_CONTENT_LENGTH=config.MAX_UPLOAD_MB * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",   # 阻断跨站表单携带会话（CSRF 第二道防线）
        SESSION_COOKIE_SECURE=False,     # 局域网 HTTP 部署，置 True 会导致无法登录
        PERMANENT_SESSION_LIFETIME=config.SESSION_LIFETIME_HOURS * 3600,
        JSON_AS_ASCII=False,
        # 静态文件每次都让浏览器回源校验（ETag/Last-Modified）。
        # 否则改了 mobile.js / custom.css 后，手机和电脑会继续用缓存里的旧版本，
        # 表现成"代码明明改了却没生效"。
        SEND_FILE_MAX_AGE_DEFAULT=0,
    )
    security.init_app(app)

    app.teardown_appcontext(db.close_db)

    # 所有模板可直接使用 shop（店铺设置）与 lan_ip（局域网地址）
    @app.context_processor
    def inject_globals():
        return {"shop": utils.get_settings(), "lan_ip": utils.lan_ip(),
                "app_name": config.APP_NAME, "app_sub": config.APP_SUB}

    # ---- 登录 ----
    @app.route("/admin/login", methods=["GET", "POST"])
    def login():
        error = ""
        if request.method == "POST":
            user = request.form.get("username", "").strip()
            pwd = request.form.get("password", "")
            s = utils.get_settings()
            stored = s.get("admin_pwd", "")
            # 用户名必须非空且完全匹配（防止 admin_user 被改成空串后任意用户名可登录）
            ok = bool(s.get("admin_user")) and user == s.get("admin_user") \
                and utils.verify_pwd(stored, pwd)
            conn = db.get_db()
            utils.log_login(conn, "admin", user, user, ok, request.remote_addr or "")
            if ok and utils.needs_rehash(stored):
                # 历史裸 SHA-256 口令：登录成功后自动升级为加盐迭代哈希
                conn.execute("UPDATE settings SET value=? WHERE key='admin_pwd'",
                             (utils.make_pwd_hash(pwd),))
            conn.commit()
            if ok:
                session.clear()             # 防会话固定攻击
                session.permanent = True
                session["admin_logged_in"] = True
                return redirect(url_for("dashboard"))
            error = "账号或密码错误"
        return app.jinja_env.get_or_select_template("login.html").render(error=error)

    @app.route("/admin/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---- 注册蓝图 ----
    from blueprints.mobile_bp import bp as mobile_bp
    from blueprints.settings_bp import bp as settings_bp
    from blueprints.dishes_bp import bp as dishes_bp
    from blueprints.orders_bp import bp as orders_bp
    from blueprints.cashier_bp import bp as cashier_bp
    from blueprints.members_bp import bp as members_bp
    from blueprints.reservations_bp import bp as reservations_bp
    from blueprints.reports_bp import bp as reports_bp
    from blueprints.employees_bp import bp as employees_bp
    from blueprints.ai_bp import bp as ai_bp, api as ai_api_bp
    from blueprints.analytics_bp import bp as analytics_bp
    from blueprints.purchase_bp import bp as purchase_bp
    from blueprints.tools_bp import bp as tools_bp
    app.register_blueprint(mobile_bp)
    app.register_blueprint(ai_api_bp, url_prefix="/api/ai")
    for bp in (settings_bp, dishes_bp, orders_bp, cashier_bp,
               members_bp, reservations_bp, reports_bp, employees_bp,
               ai_bp, analytics_bp, purchase_bp, tools_bp):
        app.register_blueprint(bp, url_prefix="/admin")

    # ---- 统一错误页：给用户一句人话 + 一个出口 ----
    @app.errorhandler(400)
    def _e400(e):
        return render_template("error.html", code=400, title="请求没有通过校验",
                               detail=getattr(e, "description", "参数不合法或会话已过期，请刷新页面后重试。")), 400

    @app.errorhandler(404)
    def _e404(e):
        return render_template("error.html", code=404, title="这个页面不存在",
                               detail="地址可能输错了，或者这个功能已经改名。可以从左侧菜单重新进入。"), 404

    @app.errorhandler(413)
    def _e413(e):
        return render_template("error.html", code=413, title="文件太大了",
                               detail="单个上传文件请控制在 20MB 以内。CSV 导入一般几百 KB 就够。"), 413

    @app.errorhandler(500)
    def _e500(e):
        # 生产环境不把堆栈抛给用户，只记日志
        app.logger.exception("未捕获异常")
        return render_template("error.html", code=500, title="服务出了点问题",
                               detail="已经记录到日志。你的数据不会丢，可以刷新重试；持续出现请联系我们。"), 500

    # ---- 登录后首页：AI 驾驶舱 ----
    @app.route("/admin")
    @admin_required
    def dashboard():
        return redirect(url_for("ai.dashboard"))

    # ---- 原有的运营首页（保留，作为「执行层」入口） ----
    @app.route("/admin/ops")
    @admin_required
    def ops_dashboard():
        conn = db.get_db()
        s = utils.get_settings()
        today = utils.today_str()
        st = conn.execute(
            "SELECT SUM(final_amount) a, COUNT(*) c FROM orders"
            " WHERE status='settled' AND substr(settled_at,1,10)=?", (today,)).fetchone()
        tables = conn.execute("SELECT * FROM tables ORDER BY sort_order, id").fetchall()
        warns = conn.execute(
            "SELECT * FROM ingredients WHERE stock < warn_threshold").fetchall()
        remind, expired = utils.check_reservations(conn)
        conn.commit()
        hurry = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE status='pending' AND hurry=1").fetchone()["c"]
        open_orders = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE status IN ('pending','making','done')").fetchone()["c"]
        return app.jinja_env.get_or_select_template("admin/dashboard.html").render(
            shop=s, today=utils.today_str(), today_amount=st["a"] or 0, today_count=st["c"] or 0,
            tables=tables, table_status_label=config.TABLE_STATUS_LABEL,
            warns=warns, remind=remind, expired=expired,
            hurry=hurry, open_orders=open_orders, lan_ip=utils.lan_ip())

    # ---- 上传图片路由（指向可写的 BASE_DIR/uploads） ----
    @app.route("/uploads/<path:filename>")
    def uploads(filename):
        return send_from_directory(str(config.UPLOAD_DIR), filename)

    return app


def _is_elevated():
    """当前进程是否以管理员权限运行"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _open_browser(url):
    """打开管理端页面。

    坑：如果本程序在管理员权限下运行，直接 webbrowser.open() 会把浏览器
    也拉成管理员实例；浏览器关闭窗口后后台进程仍存活，之后普通权限启动
    就会撞上 Windows 的 UIPI 弹窗（"现有实例正在以提升的权限运行"）。
    所以管理员权限下改为交给 explorer.exe（普通权限的外壳进程）去打开。
    """
    try:
        if _is_elevated():
            subprocess.Popen(["explorer.exe", url])
        else:
            webbrowser.open(url)
    except Exception:
        pass


def _pick_port(preferred):
    """返回第一个可用的端口；都不可用返回 None"""
    for p in (preferred, config.FALLBACK_PORT):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return None


def _serve(app, port):
    """优先 waitress（生产级 WSGI）；未安装时回退 Flask 内置服务器"""
    try:
        from waitress import serve
    except ImportError:
        print("  提示：未安装 waitress，回退 Flask 内置服务器（建议 pip install waitress）")
        app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
        return
    serve(app, host="0.0.0.0", port=port, threads=8, _quiet=True)


def main():
    # Windows 控制台 UTF-8 输出（配合 start.bat 的 chcp 65001）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    # 数据目录与数据库初始化（恢复待应用备份放在最前，此时无连接占用）
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    restore_msg = utils.apply_restore_pending()
    app = create_app()
    with app.app_context():
        first = db.init_db()
        auto_open = utils.get_setting("auto_open_browser", "1") == "1"

    print("=" * 56)
    print("  AI 掌柜 · 中小餐饮经营 Agent")
    print("=" * 56)
    if _is_elevated():
        # 以管理员身份运行会把浏览器也拉成高权限实例，之后普通启动就会
        # 撞上 Windows 的 UIPI 弹窗；这里直接提醒并说明影响
        print("  ⚠️  当前以【管理员权限】运行！")
        print("      建议关掉后用普通双击 start.bat 启动：")
        print("      · 管理员权限下运行会把浏览器也带成高权限实例")
        print("      · 之后普通启动可能弹出 Edge「以提升的权限运行」提示")
        print("      · 日常使用不需要管理员权限")
        print("-" * 56)
    if first:
        print("  首次运行：数据库与示例数据已初始化")
        print("  管理端账号：admin  密码：admin123（请登录后修改）")
        print("  点餐端员工：工号 1001  密码 123456（请在员工管理中修改）")
    if restore_msg:
        print("  " + restore_msg)
    # 支持命令行指定端口：python app.py 5001，以及 --no-browser 跳过自动开浏览器
    _args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_browser = "--no-browser" in sys.argv or os.environ.get("CGY_NO_BROWSER") == "1"
    port = config.PORT
    if _args:
        try:
            port = int(_args[0])
        except ValueError:
            port = config.PORT
    port = _pick_port(port)
    if port is None:
        print("  启动失败：端口 %d 与备用端口 %d 都被占用" % (config.PORT, config.FALLBACK_PORT))
        sys.exit(1)
    if port != config.PORT:
        print("  端口 %d 被占用，已改用 %d" % (config.PORT, port))
    lan = utils.lan_ip()
    print("  管理端：http://127.0.0.1:%d/admin" % port)
    print("  点餐端：http://%s:%d  （手机连同一 WiFi 访问）" % (lan, port))
    print("  首次运行请在 Windows 防火墙弹窗中选择\"允许访问（专用网络）\"")
    print("=" * 56)

    admin_url = "http://127.0.0.1:%d/admin" % port
    if no_browser or not auto_open:
        print("  提示：已关闭自动打开浏览器，请手动访问上面的地址。")
        print("        （可在「店铺设置 → 启动时自动打开浏览器」里改回来）")
    else:
        threading.Timer(1.0, lambda: _open_browser(admin_url)).start()
    try:
        _serve(app, port)
    except OSError as e:
        print("  启动失败：", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
