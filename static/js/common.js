/* 通用工具：fetch 封装、确认、toast */

/* 全局错误兜底：任何 JS 报错都不再"无声无息"，直接弹到屏幕上 */
window.addEventListener("error", (e) => {
  try { toast("页面出错：" + (e.message || "未知错误"), "danger"); } catch (_) {}
});
window.addEventListener("unhandledrejection", (e) => {
  try {
    const r = e.reason;
    toast("操作失败：" + ((r && r.message) ? r.message : r), "danger");
  } catch (_) {}
});

/* ---------- CSRF ----------
   token 由后端 after_request 自动注入到 <meta name="csrf-token">，
   所有非 GET 请求自动带上 X-CSRF-Token 头。 */
function csrfToken() {
  const m = document.querySelector('meta[name="csrf-token"]');
  return m ? m.getAttribute("content") : "";
}

function withCsrf(opts) {
  const o = Object.assign({}, opts || {});
  const method = (o.method || "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
    o.headers = Object.assign({}, o.headers || {}, {"X-CSRF-Token": csrfToken()});
  }
  return o;
}

/* 点餐端专用：401（未登录/登录过期）自动跳转登录页 */
async function mfetch(url, opts) {
  const o = withCsrf(opts || {});
  o.headers = Object.assign({"Content-Type": "application/json"}, o.headers || {});
  const r = await fetch(url, o);
  if (r.status === 401) { location.href = "/login"; throw new Error("请先登录"); }
  return r;
}

async function api(url, opts) {
  const o = withCsrf(opts || {});
  o.headers = Object.assign({"Content-Type": "application/json"}, o.headers || {});
  const r = await fetch(url, o);
  if (r.status === 401) { location.href = "/login"; }
  const data = await r.json().catch(() => ({ok: false, msg: "服务器错误"}));
  return {status: r.status, data: data};
}

function fmtMoney(x) {
  return (Number(x) || 0).toFixed(2);
}

function confirmAction(form, msg) {
  if (confirm(msg || "确认执行此操作？")) { form.submit(); }
}

/* 轻量 toast（移动端使用） */
function toast(msg, type) {
  let el = document.createElement("div");
  el.textContent = msg;
  el.style.cssText = "position:fixed;top:60px;left:50%;transform:translateX(-50%);z-index:9999;" +
    "padding:10px 20px;border-radius:8px;color:#fff;font-size:.9rem;max-width:90vw;" +
    "box-shadow:0 4px 14px rgba(0,0,0,.25);word-break:break-all;" +
    "background:" + (type === "danger" ? "#c0392b" : (type === "warning" ? "#e8a13d" : "#23a55a"));
  el.style.zIndex = 2147483000;      // 必须盖过所有抽屉/弹窗（最高 1070）
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}

/* 表单转 JSON POST（移动端 API 用） */
async function postJSON(url, body) {
  return api(url, {method: "POST", body: JSON.stringify(body)});
}
