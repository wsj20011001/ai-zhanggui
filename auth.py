# -*- coding: utf-8 -*-
"""登录校验：管理端 admin_required / 点餐端 emp_required"""
from functools import wraps

from flask import jsonify, redirect, request, session, url_for


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def emp_required(view):
    """点餐端登录校验：页面重定向到登录页，API 返回 401 JSON（前端据此跳转）"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("emp_logged_in"):
            if request.path.startswith("/api/"):
                return jsonify(ok=False, msg="请先登录"), 401
            return redirect(url_for("mobile.login"))
        return view(*args, **kwargs)
    return wrapped
