# -*- coding: utf-8 -*-
"""AI 网关：本地 / 云端 / 关闭 三级降级。

为什么不用 SDK：整个项目要能打包成 EXE、在没装任何第三方库的机器上跑，
所以只用标准库 urllib。多一个依赖就多一个现场翻车的可能。

调用顺序由配置的 provider 决定，失败自动降级：
    local（Ollama）→ cloud（OpenAI 兼容）→ 抛 ModelUnavailable（调用方走规则）
"""
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import date

import config


class ModelUnavailable(Exception):
    """模型不可用（未安装 / 断网 / 超时 / 超额），调用方应降级到规则"""


class BudgetExceeded(ModelUnavailable):
    """当日 token 额度用尽"""


def _setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return (row["value"] if row and row["value"] != "" else default)


def api_key():
    """Key 单独存文件，不写数据库：避免备份库被拷走后泄露"""
    try:
        return config.AI_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_api_key(key):
    config.AI_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        config.AI_KEY_FILE.write_text((key or "").strip(), encoding="utf-8")
        return True
    except OSError:
        return False


def _opener(direct=False):
    """direct=True 时绕过系统代理。

    必须这么做的原因：国内很多机器装了全局代理，urllib 会把 127.0.0.1 也交给代理，
    本地 Ollama 就会返回 502 —— 看起来像"模型没装"，其实是网络栈的问题。
    """
    if direct:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _post(url, payload, timeout, headers=None, direct=False):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with _opener(direct).open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _local_chat(conn, messages, json_mode, timeout):
    base = _setting(conn, "ai_ollama_base", config.OLLAMA_BASE).rstrip("/")
    model = _setting(conn, "ai_local_model", config.DEFAULT_LOCAL_MODEL)
    payload = {
        "model": model, "messages": messages, "stream": False,
        "options": {"temperature": config.AI_TEMPERATURE,
                    "num_predict": config.AI_MAX_TOKENS},
        "keep_alive": "30m",
    }
    if json_mode:
        payload["format"] = "json"
    data = _post(base + "/api/chat", payload, timeout, direct=True)
    text = (data.get("message") or {}).get("content", "")
    return {
        "text": text, "provider": "local", "model": model,
        "prompt_tokens": int(data.get("prompt_eval_count") or 0),
        "output_tokens": int(data.get("eval_count") or 0),
    }


def _cloud_chat(conn, messages, json_mode, timeout):
    base = _setting(conn, "ai_cloud_base", config.DEFAULT_CLOUD_BASE).rstrip("/")
    model = _setting(conn, "ai_cloud_model", config.DEFAULT_CLOUD_MODEL)
    key = api_key()
    if not key:
        raise ModelUnavailable("未配置云端 API Key")
    payload = {
        "model": model, "messages": messages,
        "temperature": config.AI_TEMPERATURE,
        "max_tokens": config.AI_MAX_TOKENS,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    data = _post(base + "/chat/completions", payload, timeout,
                 headers={"Authorization": "Bearer " + key})
    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content", "")
    usage = data.get("usage") or {}
    return {
        "text": text, "provider": "cloud", "model": model,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
    }


def today_tokens(conn):
    row = conn.execute(
        "SELECT COALESCE(SUM(prompt_tokens + output_tokens),0) t FROM ai_conversations"
        " WHERE substr(created_at,1,10)=?", (date.today().isoformat(),)).fetchone()
    return int(row["t"] or 0)


def chat(conn, messages, json_mode=False, use_cache=True, allow_cloud=True):
    """主入口。返回 dict；不可用时抛 ModelUnavailable。"""
    provider = _setting(conn, "ai_provider", config.DEFAULT_AI_PROVIDER)
    if provider == "off":
        raise ModelUnavailable("已设置为规则模式")

    budget = int(_setting(conn, "ai_daily_budget", str(config.AI_DAILY_TOKEN_BUDGET)) or 0)
    if budget and today_tokens(conn) >= budget:
        raise BudgetExceeded("今日 AI 额度已用完")

    model = _setting(conn, "ai_local_model", config.DEFAULT_LOCAL_MODEL)
    cache_key = hashlib.sha256(
        (provider + "|" + model + "|" + json.dumps(messages, ensure_ascii=False))
        .encode("utf-8")).hexdigest()
    if use_cache:
        row = conn.execute(
            "SELECT v, created_at FROM ai_cache WHERE k=?", (cache_key,)).fetchone()
        if row:
            try:
                age = time.time() - time.mktime(time.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                age = 1e9
            if age < config.AI_CACHE_TTL:
                out = json.loads(row["v"])
                out["cached"] = True
                return out

    # 按"本地优先"的顺序尝试；云端在本机模型失败时兜底
    order = ["local", "cloud"] if provider == "local" else ["cloud", "local"]
    if not allow_cloud:
        order = [p for p in order if p != "cloud"]
    errors = []
    for p in order:
        t0 = time.time()
        try:
            if p == "local":
                out = _local_chat(conn, messages, json_mode, config.AI_CONNECT_TIMEOUT if provider != "local" else config.AI_TIMEOUT)
            else:
                out = _cloud_chat(conn, messages, json_mode, config.AI_TIMEOUT)
            out["latency_ms"] = int((time.time() - t0) * 1000)
            out["cached"] = False
            if use_cache:
                conn.execute("INSERT OR REPLACE INTO ai_cache(k, v, hit, created_at)"
                             " VALUES(?,?,?, datetime('now','localtime'))",
                             (cache_key, json.dumps(out, ensure_ascii=False), 0))
                conn.commit()
            return out
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                OSError, ValueError, KeyError) as e:
            errors.append("%s: %s" % (p, e))
            continue
    raise ModelUnavailable("；".join(errors) or "无可用模型")


def local_only(conn, messages, timeout=None):
    """只走本地通道（设置页的"测试本地模型"用它，避免误用云端）"""
    return _local_chat(conn, messages, False, timeout or config.AI_TIMEOUT)


def probe(conn):
    """连通性自检：给设置页用，避免现场才发现模型没起来"""
    out = {"provider": _setting(conn, "ai_provider", "local"),
           "local": {"ok": False, "msg": ""}, "cloud": {"ok": False, "msg": ""}}
    base = _setting(conn, "ai_ollama_base", config.OLLAMA_BASE).rstrip("/")
    try:
        with _opener(True).open(base + "/api/tags", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        names = [m.get("name", "") for m in data.get("models", [])]
        detail = [{"name": m.get("name", ""),
                   "size_gb": round((m.get("size") or 0) / 1024 ** 3, 1)}
                  for m in data.get("models", [])]
        model = _setting(conn, "ai_local_model", config.DEFAULT_LOCAL_MODEL)
        out["local"] = {"ok": True, "models": names, "detail": detail,
                        "has_model": any(model.split(":")[0] in n for n in names),
                        "msg": "已连接 Ollama（%d 个模型）" % len(names)}
    except Exception as e:
        raw = str(e)
        if "10061" in raw or "refused" in raw.lower() or "积极拒绝" in raw:
            msg = "本机没有 Ollama 服务（%s 无响应）。装好后在这里点「测试本地模型」。" % base
        elif "502" in raw or "Bad Gateway" in raw:
            msg = "请求被系统代理接管了。检查代理设置，或确认程序已更新（本地通道应直连）。"
        elif "timed out" in raw.lower():
            msg = "连接超时：Ollama 可能正在加载模型，稍等几秒再试。"
        else:
            msg = "未连接：%s" % raw[:120]
        out["local"] = {"ok": False, "msg": msg}
    key = api_key()
    if key:
        try:
            base2 = _setting(conn, "ai_cloud_base", config.DEFAULT_CLOUD_BASE).rstrip("/")
            req = urllib.request.Request(base2 + "/models")
            req.add_header("Authorization", "Bearer " + key)
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
            out["cloud"] = {"ok": True, "msg": "Key 可用"}
        except Exception as e:
            out["cloud"] = {"ok": False, "msg": "Key 校验失败：%s" % e}
    else:
        out["cloud"] = {"ok": False, "msg": "未配置 Key"}
    return out
