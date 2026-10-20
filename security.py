# -*- coding: utf-8 -*-
"""CSRF 防护 + 安全响应头

设计：
1. 每个会话一个随机 token（存在 session 中）；
2. HTML 响应由 after_request 统一注入，37 个已有模板一行都不用改，将来新增模板自动覆盖：
   - <meta name="csrf-token" content="...">  供前端 fetch 读取
   - 每个 POST 表单内自动插入 <input type="hidden" name="_csrf">
3. 校验：非安全方法必须携带
   X-CSRF-Token 请求头（JSON/接口）或 _csrf 表单域（普通表单）。
"""
import hmac
import re
import secrets

from flask import abort, jsonify, request, session

TOKEN_KEY = "_csrf_token"
HEADER_NAME = "X-CSRF-Token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_FORM_RE = re.compile(r"<form\b[^>]*>", re.I)
_POST_FORM_RE = re.compile(r"\bmethod\s*=\s*[\"']?post", re.I)


def current_token():
    """取本会话的 CSRF token，没有则生成"""
    t = session.get(TOKEN_KEY)
    if not t:
        t = secrets.token_urlsafe(32)
        session[TOKEN_KEY] = t
    return t


def _same(a, b):
    try:
        return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
    except (AttributeError, TypeError):
        return False


def _supplied_token():
    t = request.headers.get(HEADER_NAME)
    if t:
        return t
    if request.mimetype in ("application/x-www-form-urlencoded",
                            "multipart/form-data"):
        t = request.form.get("_csrf")
        if t:
            return t
    if request.mimetype == "application/json":
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            return data.get("_csrf")
    return None


def _reject():
    msg = "安全校验失败（CSRF token 不匹配或会话已过期），请刷新页面后重试。"
    if request.path.startswith("/api/"):
        return jsonify(ok=False, msg="会话已失效，请刷新页面后重试"), 400
    abort(400, description=msg)


def init_app(app):
    app.jinja_env.globals["csrf_token"] = current_token

    @app.before_request
    def _csrf_protect():
        if request.method in _SAFE_METHODS:
            return None
        supplied = _supplied_token()
        if supplied and _same(str(supplied), current_token()):
            return None
        return _reject()

    @app.after_request
    def _inject_and_harden(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if resp.mimetype != "text/html" or "charset" not in ctype:
            return resp
        try:
            html = resp.get_data(as_text=True)
        except (RuntimeError, UnicodeDecodeError):
            return resp
        if not html:
            return resp
        token = current_token()
        if 'name="csrf-token"' not in html and "<head>" in html:
            html = html.replace(
                "<head>",
                '<head>\n<meta name="csrf-token" content="%s">' % token, 1)
        html = _FORM_RE.sub(
            lambda m: (m.group(0) + '<input type="hidden" name="_csrf" value="%s">' % token)
            if _POST_FORM_RE.search(m.group(0)) else m.group(0),
            html)
        resp.set_data(html)
        return resp
