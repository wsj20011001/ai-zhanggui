/* PC 实时订单页：3 秒轮询片段替换（setTimeout 递归防堆积） */
let lastPollHtml = "";
let pollTimer = null;

async function pollOrders(tab) {
  try {
    const r = await fetch("/admin/partial/orders?tab=" + tab, {cache: "no-store"});
    const html = await r.text();
    const tbody = document.getElementById("order-tbody");
    if (tbody && html !== lastPollHtml) {
      tbody.innerHTML = html;
      lastPollHtml = html;
    }
  } catch (e) { /* 网络抖动忽略，下一轮再试 */ }
  pollTimer = setTimeout(() => pollOrders(tab), 3000);
}

function initOrderPolling(tab) {
  if (pollTimer) clearTimeout(pollTimer);
  lastPollHtml = "";
  pollOrders(tab);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { lastPollHtml = ""; }
  });
}
