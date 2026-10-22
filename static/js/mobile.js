/* 移动端点餐核心逻辑：左侧分类竖排 + 菜品列表、点选式口味备注、
   购物车全量保存、3s 对账、下单/催菜/撤回 */
let menuData = {categories: [], dishes: []};
let cart = [];            // 本地购物车 [{key, dish_id, name, price, qty, note, children}]
let persons = 1;
let remark = "";
let serverUpdatedAt = ""; // 服务端购物车 updated_at（对账基准）
let activeCat = null;
let pollTimer = null;
let pendingDishId = null; // 备注弹窗待加入的菜品
let spicySel = "";        // 辣度单选
let tagSel = [];          // 口味多选
let specSel = null;       // 当前选中的规格 id

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

/* ---------- 菜单渲染：左侧分类竖排 + 右侧菜品列表 ---------- */
async function loadMenu() {
  const r = await mfetch("/api/menu", {cache: "no-store"});
  menuData = await r.json();
  renderCatRail();
  renderDishes();
}

function renderCatRail() {
  const el = document.getElementById("cat-rail");
  el.innerHTML = menuData.categories.map((c, i) =>
    `<button class="cat-item ${i === 0 ? 'active' : ''}" data-cat="${c.id}">${esc(c.name)}</button>`).join("");
  el.querySelectorAll(".cat-item").forEach(btn => btn.addEventListener("click", () => {
    el.querySelectorAll(".cat-item").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    activeCat = btn.dataset.cat;
    renderDishes();
  }));
  activeCat = menuData.categories.length ? String(menuData.categories[0].id) : null;
}

function renderPicks() {
  const picks = menuData.top_picks || [];
  if (!picks.length) return "";
  const label = {balanced: "均衡", profit: "保利润", clear: "清库存"}[menuData.rec_mode] || "";
  return `<div class="pick-strip">
    <div class="pick-title">🔥 今日主推
      <span class="text-muted small fw-normal">按「${label}」策略 · 兼顾毛利与库存</span></div>
    <div class="pick-cards">
      ${picks.map(p => `<div class="pick-card" data-id="${p.dish_id}">
        <div class="pc-name">${esc(p.name)}</div>
        <div class="pc-reason">${esc(p.reason)}</div>
        <div class="pc-price">¥${Number(p.price).toFixed(2)}</div>
      </div>`).join("")}
    </div></div>`;
}

function renderDishes() {
  const el = document.getElementById("dish-list");
  const list = menuData.dishes.filter(d => String(d.category_id) === activeCat);
  const firstCat = menuData.categories.length ? String(menuData.categories[0].id) : null;
  const picksHtml = (activeCat === firstCat) ? renderPicks() : "";
  el.innerHTML = picksHtml + (list.map(d => {
    const badges = [];
    if (d.is_featured) badges.push('<span class="badge bg-danger ms-1">🪿特色</span>');
    else if (d.is_recommend) badges.push('<span class="badge bg-warning text-dark ms-1">⭐推荐</span>');
    if (d.is_combo) badges.push('<span class="badge bg-info text-dark ms-1">套餐</span>');
    if (d.price_mode === 'market') badges.push('<span class="badge bg-warning text-dark ms-1">时价</span>');
    if (d.warn_flag) badges.push('<span class="badge bg-secondary ms-1">⚠️紧张</span>');
    if (d.season_blocked) badges.push('<span class="badge bg-secondary ms-1">🚫过季</span>');
    else if (d.season) badges.push('<span class="badge bg-success ms-1">🌿' + esc(d.season) + '时令</span>');
    if (d.specs && d.specs.length) badges.push('<span class="badge bg-primary ms-1">可选规格</span>');
    const hasStock = d.can_make !== null && d.can_make !== undefined;
    const out = (hasStock && d.can_make <= 0) || (!hasStock && false) || d.season_blocked;
    if (hasStock && d.can_make > 0 && d.can_make <= 5) {
      badges.push('<span class="badge bg-danger ms-1">仅剩 ' + d.can_make + ' 份</span>');
    }
    if (out) badges.push('<span class="badge bg-dark ms-1">原料不足</span>');
    const thumb = d.image
      ? `<img class="d-thumb" src="/${esc(d.image)}" loading="lazy" alt="">`
      : '<div class="d-thumb-ph">🍽️</div>';
    const stockLine = (hasStock && d.can_make > 0)
      ? `<div class="mt-1 small text-success">✅ 今日还可做 ${d.can_make} 份</div>` : '';
    const blocked = (hasStock && d.can_make <= 0 && menuData.block_order) ||
                    (d.season_blocked && menuData.season_block);
    return `<div class="dish-row${blocked ? ' sold-out' : ''}" data-id="${d.id}" data-out="${blocked ? 1 : 0}">${thumb}
      <div class="d-body">
        <div class="d-name">${esc(d.name)}${badges.join("")}</div>
        <div class="d-flex justify-content-between align-items-center mt-1">
          ${d.price_mode === 'market'
            ? '<span class="d-price text-warning">时价 · 结账时定价</span>'
            : `<span class="d-price">¥${Number(d.price).toFixed(2)}</span>`}
          ${d.is_combo && d.children && d.children.length
            ? `<span class="small text-muted">含 ${d.children.map(c => esc(c.dish_name)).join("、")}</span>` : ''}
        </div>
        ${stockLine}
        ${d.description ? `<div class="d-desc">${esc(d.description)}</div>` : ''}
      </div></div>`;
  }).join("") || '<p class="text-muted text-center w-100 py-4">该分类暂无菜品</p>');
  el.querySelectorAll(".dish-row").forEach(row => row.addEventListener("click", () => {
    if (row.dataset.out === "1") {
      const dd = menuData.dishes.find(x => String(x.id) === String(row.dataset.id));
      toast(dd && dd.season_blocked ? "该菜品已过季，暂时无法下单"
                                    : "该菜品原料不足，暂时无法下单", "warning");
      return;
    }
    openNoteModal(row.dataset.id);
  }));
  el.querySelectorAll(".pick-card").forEach(c =>
    c.addEventListener("click", () => openNoteModal(c.dataset.id)));
}

/* ---------- 口味备注弹窗（点选式，无输入框） ---------- */
function openNoteModal(dishId) {
  const d = menuData.dishes.find(x => String(x.id) === String(dishId));
  if (!d) return;
  pendingDishId = dishId;
  spicySel = "";
  tagSel = [];
  // 规格：默认选中"默认规格"（或第一个）
  const specs = d.specs || [];
  specSel = null;
  const wrap = document.getElementById("nm-spec-wrap");
  const box = document.getElementById("nm-specs");
  if (specs.length) {
    const def = specs.find(s => s.is_default) || specs[0];
    specSel = String(def.id);
    box.innerHTML = specs.map(s => {
      const dl = Number(s.price_delta) || 0;
      const tag = dl === 0 ? "" : (dl > 0 ? " +" + dl.toFixed(2) : " " + dl.toFixed(2));
      return `<button class="chip${String(s.id) === specSel ? " active" : ""}" data-v="${s.id}">${esc(s.name)}${tag}</button>`;
    }).join("");
    box.querySelectorAll(".chip").forEach(c => c.addEventListener("click", () => {
      box.querySelectorAll(".chip").forEach(x => x.classList.remove("active"));
      c.classList.add("active");
      specSel = c.dataset.v;
      updateNotePrice();
    }));
    wrap.style.display = "";
  } else {
    box.innerHTML = "";
    wrap.style.display = "none";
  }
  document.getElementById("nm-title").textContent = "口味备注 · " + d.name;
  document.querySelectorAll("#nm-spicy .chip, #nm-tags .chip").forEach(c => c.classList.remove("active"));
  document.getElementById("note-modal").style.display = "flex";
  updateNotePrice();
}

function updateNotePrice() {
  const d = menuData.dishes.find(x => String(x.id) === String(pendingDishId));
  if (!d) return;
  const el = document.getElementById("nm-price");
  if (d.price_mode === 'market') {
    el.textContent = "时价 · 结账时定价";
    document.getElementById("nm-confirm").textContent = "加入购物车";
    return;
  }
  let p = Number(d.price) || 0;
  const sp = (d.specs || []).find(s => String(s.id) === String(specSel));
  if (sp) p += Number(sp.price_delta) || 0;
  el.textContent = "￥" + p.toFixed(2);
  document.getElementById("nm-confirm").textContent = "加入购物车 ￥" + p.toFixed(2);
}

function noteString() {
  const parts = [];
  if (spicySel) parts.push(spicySel);
  parts.push(...tagSel);
  return parts.join(" ");
}

/* ---------- 购物车操作 ---------- */
function cartKey(dishId, note, specId) {
  return "d_" + dishId + "_" + (specId || "") + "_" + (note || "");
}

function addDishCore(dishId, note, specId) {
  const d = menuData.dishes.find(x => String(x.id) === String(dishId));
  if (!d) return;
  const sp = (d.specs || []).find(s => String(s.id) === String(specId));
  const delta = sp ? (Number(sp.price_delta) || 0) : 0;
  const key = cartKey(d.id, note, specId);
  const exist = cart.find(i => i.key === key);
  if (exist) { exist.qty += 1; }
  else {
    cart.push({
      key: key, dish_id: d.id, name: d.name, price: Number(d.price) + delta,
      spec_id: sp ? sp.id : null, spec_name: sp ? sp.name : "",
      is_market: d.price_mode === 'market',
      qty: 1, note: note,
      children: (d.children || []).map(ch => ({
        dish_id: ch.dish_id, name: ch.dish_name, price: Number(ch.dish_price || 0),
        qty: ch.qty || 1
      }))
    });
  }
  saveCart("已加入：").then(() => renderCart());
}

function changeQty(key, delta) {
  const it = cart.find(i => i.key === key);
  if (!it) return;
  it.qty += delta;
  if (it.qty <= 0) cart = cart.filter(i => i.key !== key);
  saveCart().then(() => renderCart());
}

function removeItem(key) {
  cart = cart.filter(i => i.key !== key);
  saveCart().then(() => renderCart());
}

async function saveCart(addMsg) {
  const r = await postJSON(`/api/cart/${TABLE_ID}`, {items: cart, persons: persons, remark: remark});
  if (r.status === 200 && r.data.ok) {
    serverUpdatedAt = r.data.updated_at || serverUpdatedAt;
    if (addMsg) toast(addMsg + (cart.length ? " 共 " + cartCount() + " 份" : "购物车已清空"));
  } else {
    toast("保存失败：" + (r.data.msg || "网络错误"), "danger");
  }
}

function cartCount() { return cart.reduce((s, i) => s + i.qty, 0); }
function cartTotal() { return cart.reduce((s, i) => s + i.price * i.qty, 0); }

function renderCart() {
  const count = cartCount();
  document.getElementById("order-count").textContent = count;
  const bar = document.getElementById("cart-bar");
  bar.style.display = count ? "" : "none";
  document.getElementById("cb-count").textContent = count;
  document.getElementById("cb-total").textContent = cartTotal().toFixed(2);
  document.getElementById("cart-total").textContent = cartTotal().toFixed(2);
  document.getElementById("p-count").textContent = persons;
  // 时价菜提示：下单后由收银台确认价格
  const mCount = cart.filter(i => i.is_market).reduce((s, i) => s + i.qty, 0);
  const warn = document.getElementById("cart-market-warn");
  if (warn) {
    warn.style.display = mCount ? "" : "none";
    warn.textContent = "⚠️ 含 " + mCount + " 份时价菜，下单后由收银台确认价格";
  }
  const list = document.getElementById("cart-list");
  list.innerHTML = cart.map(i => `
    <div class="d-flex align-items-center border-bottom py-2">
      <div class="flex-grow-1">
        <div><b>${esc(i.name)}</b>${i.spec_name ? ' <span class="badge bg-primary">' + esc(i.spec_name) + '</span>' : ''} ${i.is_market
          ? '<span class="text-warning">时价 · 待定</span>'
          : `<span class="text-danger">¥${i.price.toFixed(2)}</span>`}</div>
        ${i.note ? `<div class="small text-warning">〔${esc(i.note)}〕</div>` : ''}
        ${i.children && i.children.length ? `<div class="small text-muted">└ ${i.children.map(c => esc(c.name) + "×" + c.qty).join("、")}</div>` : ''}
      </div>
      <div class="d-flex align-items-center">
        <button class="btn btn-sm btn-outline-secondary" onclick="changeQty('${i.key}', -1)">−</button>
        <span class="mx-2 fw-bold">${i.qty}</span>
        <button class="btn btn-sm btn-outline-secondary" onclick="changeQty('${i.key}', 1)">＋</button>
        <button class="btn btn-sm btn-outline-danger ms-2" onclick="removeItem('${i.key}')">🗑</button>
      </div>
    </div>`).join("") || '<p class="text-muted text-center py-4">购物车是空的，去点点菜吧～</p>';
}

/* ---------- 3s 对账轮询 ---------- */
async function reconcile() {
  try {
    const r = await mfetch(`/api/cart/${TABLE_ID}`, {cache: "no-store"});
    const data = await r.json();
    if (serverUpdatedAt && data.updated_at && data.updated_at !== serverUpdatedAt) {
      // 其他设备更新了购物车 → 以服务端为准
      cart = Array.isArray(data.items) ? data.items : [];
      persons = data.persons || 1;
      remark = data.remark || "";
      document.getElementById("cart-remark").value = remark;
      renderCart();
      toast("购物车已在其他设备更新");
    }
    serverUpdatedAt = data.updated_at || serverUpdatedAt;
  } catch (e) { /* 忽略 */ }
  pollTimer = setTimeout(reconcile, 3000);
}

/* ---------- 下单 / 催菜 / 撤回 ---------- */
/* 下单：不用原生 confirm（部分浏览器会静默拦截，导致"点了没反应"），
   改成"两次点击确认"，并且无论成功失败都必定给出可见反馈。 */
let submitArmed = 0;

function setSubmitButtons(text, warn, disabled) {
  ["btn-submit", "btn-submit2"].forEach(id => {
    const b = document.getElementById(id);
    if (!b) return;
    b.textContent = text;
    b.disabled = !!disabled;
    b.classList.toggle("btn-warning", !!warn);
    b.classList.toggle("btn-danger", !warn && !disabled);
  });
}

async function submitOrder(ev) {
  if (ev && ev.stopPropagation) ev.stopPropagation();   // 别让点击冒泡到 cart-bar 去开合抽屉
  if (!cart.length) { toast("购物车为空，请先点餐", "danger"); return; }

  const now = Date.now();
  if (now - submitArmed > 4000) {          // 第一次点击 → 进入待确认状态
    submitArmed = now;
    setSubmitButtons("再点一次确认", true, false);
    toast("再点一次「确认下单」提交（" + cartCount() + " 份 · ¥" + cartTotal().toFixed(2) + "）");
    setTimeout(() => {
      if (Date.now() - submitArmed >= 3900) {
        submitArmed = 0;
        setSubmitButtons("确认下单", false, false);
      }
    }, 4000);
    return;
  }

  submitArmed = 0;
  setSubmitButtons("提交中…", true, true);
  try {
    const r = await postJSON(`/api/order/${TABLE_ID}/submit`, {});
    if (r && r.data && r.data.ok) {
      cart = [];
      renderCart();
      const d = document.getElementById("cart-drawer");
      if (d) d.style.display = "none";
      toast("下单成功！后厨已收到 🎉");
    } else {
      toast("下单失败：" + ((r && r.data && r.data.msg) || "未知错误"), "danger");
    }
  } catch (e) {
    toast("网络异常，下单未成功：" + ((e && e.message) ? e.message : e), "danger");
  } finally {
    setSubmitButtons("确认下单", false, false);
  }
}

async function loadMyOrders() {
  try {
    const r = await mfetch(`/api/table/${TABLE_ID}/orders`, {cache: "no-store"});
    const data = await r.json();
    const el = document.getElementById("orders-list");
    const STATUS = {pending: "⏳ 待确认", making: "👨‍🍳 制作中"};
    el.innerHTML = (data.orders || []).map(o => `
      <div class="border rounded p-3 mb-2 ${o.status === 'pending' ? 'bg-light' : ''}">
        <div class="d-flex justify-content-between">
          <b>${esc(o.order_no)}</b><span>${STATUS[o.status] || o.status}</span>
        </div>
        <div class="small text-muted mb-2">下单 ${(o.created_at || "").slice(5, 16)} · ¥${Number(o.total_amount).toFixed(2)}</div>
        ${o.status === 'pending' ? `
        <div class="d-flex gap-2">
          <button class="btn btn-sm btn-warning flex-grow-1" onclick="doHurry(${o.id})">🔔 催菜</button>
          <button class="btn btn-sm btn-outline-danger flex-grow-1" onclick="doWithdraw(${o.id})">↩️ 撤回订单</button>
        </div>` : ''}
      </div>`).join("") || '<p class="text-muted text-center py-4">暂无进行中的订单</p>';
  } catch (e) { /* 忽略 */ }
}

async function doHurry(oid) {
  const r = await postJSON(`/api/order/${oid}/hurry`, {});
  toast(r.data.ok ? "已催菜，后厨马上处理！🔔" : (r.data.msg || "催菜失败"), r.data.ok ? "" : "danger");
  if (r.data.ok) loadMyOrders();
}

async function doWithdraw(oid) {
  if (!confirm("确认撤回此订单？")) return;
  const r = await postJSON(`/api/order/${oid}/withdraw`, {});
  toast(r.data.msg || (r.data.ok ? "已撤回" : "撤回失败"), r.data.ok ? "" : "danger");
  if (r.data.ok) loadMyOrders();
}

/* ---------- 事件绑定 ---------- */
document.addEventListener("DOMContentLoaded", async () => {
  const drawer = document.getElementById("cart-drawer");
  document.getElementById("cart-bar").addEventListener("click", (e) => {
    // 点的是「下单」按钮就不要开合抽屉，否则看起来像"没反应"
    if (e.target && e.target.closest && e.target.closest("#btn-submit")) return;
    if (drawer.style.display === "none") { drawer.style.display = "flex"; renderCart(); }
    else drawer.style.display = "none";
  });
  document.getElementById("btn-close-cart").addEventListener("click", () => drawer.style.display = "none");
  document.getElementById("btn-submit").addEventListener("click", submitOrder);
  document.getElementById("btn-submit2").addEventListener("click", submitOrder);
  document.getElementById("btn-clear").addEventListener("click", async () => {
    if (!cart.length) { toast("购物车本来就是空的"); return; }
    if (!confirm("确认清空购物车？")) return;
    cart = [];
    try { await saveCart(); } catch (e) { toast("清空失败：" + e.message, "danger"); }
    renderCart();
  });
  // 改人数：既重绘也落库（以前只重绘，导致订单 persons 恒为 1，污染人均消费与客流）
  let personsTimer = null;
  function savePersonsSoon() {
    clearTimeout(personsTimer);
    personsTimer = setTimeout(() => { saveCart(); }, 400);
  }
  document.getElementById("p-minus").addEventListener("click", () => {
    if (persons > 1) { persons--; renderCart(); savePersonsSoon(); }
  });
  document.getElementById("p-plus").addEventListener("click", () => {
    if (persons < 50) { persons++; renderCart(); savePersonsSoon(); }
  });
  // 整单备注：change 在失焦时触发，直接落库（以前只改内存变量）
  document.getElementById("cart-remark").addEventListener("change", e => {
    remark = e.target.value;
    saveCart();
  });
  document.getElementById("btn-orders").addEventListener("click", () => {
    document.getElementById("orders-drawer").style.display = "flex";
    loadMyOrders();
  });
  document.getElementById("btn-close-orders").addEventListener("click", () => {
    document.getElementById("orders-drawer").style.display = "none";
  });

  // 备注弹窗：辣度单选
  document.querySelectorAll("#nm-spicy .chip").forEach(c => c.addEventListener("click", () => {
    document.querySelectorAll("#nm-spicy .chip").forEach(x => x.classList.remove("active"));
    c.classList.add("active");
    spicySel = c.dataset.v;
  }));
  // 备注弹窗：口味多选
  document.querySelectorAll("#nm-tags .chip").forEach(c => c.addEventListener("click", () => {
    c.classList.toggle("active");
    tagSel = Array.from(document.querySelectorAll("#nm-tags .chip.active")).map(x => x.dataset.v);
  }));
  document.getElementById("nm-close").addEventListener("click", () => {
    document.getElementById("note-modal").style.display = "none";
  });
  document.getElementById("nm-cancel").addEventListener("click", () => {
    document.getElementById("note-modal").style.display = "none";
  });
  document.getElementById("nm-confirm").addEventListener("click", () => {
    const note = noteString();
    document.getElementById("note-modal").style.display = "none";
    if (pendingDishId) addDishCore(pendingDishId, note, specSel);
  });

  await loadMenu();
  // 初始购物车从服务端拉取
  const r = await mfetch(`/api/cart/${TABLE_ID}`, {cache: "no-store"});
  const data = await r.json();
  cart = Array.isArray(data.items) ? data.items : [];
  persons = data.persons || 1;
  remark = data.remark || "";
  serverUpdatedAt = data.updated_at || "";
  document.getElementById("cart-remark").value = remark;
  renderCart();
  reconcile();
});
