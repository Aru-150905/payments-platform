// payments-platform local console — plain JS, no build step, no framework.
// Talks straight to the FastAPI backend (app/main.py's CORSMiddleware is
// wide open specifically so this file can be opened from anywhere: disk,
// a throwaway `python -m http.server`, whatever).

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// --- persisted connection settings ------------------------------------------

const apiBaseInput = $("#api-base");
const apiKeyInput = $("#api-key");
apiBaseInput.value = localStorage.getItem("pp_api_base") || apiBaseInput.value;
apiKeyInput.value = localStorage.getItem("pp_api_key") || apiKeyInput.value;
apiBaseInput.addEventListener("change", () => localStorage.setItem("pp_api_base", apiBaseInput.value.trim()));
apiKeyInput.addEventListener("change", () => localStorage.setItem("pp_api_key", apiKeyInput.value.trim()));

function apiBase() {
  return apiBaseInput.value.trim().replace(/\/+$/, "");
}

// Select-all on focus for number inputs: without this, clicking into a
// field that already holds a value from a previous (especially a FAILED,
// deliberately-not-cleared — see the order/payment submit handlers) attempt
// and typing appends instead of replacing, e.g. an old "10" plus a typed
// "5" silently becoming "105".
document.addEventListener("focusin", (e) => {
  if (e.target.tagName === "INPUT" && e.target.type === "number") {
    e.target.select();
  }
});

// --- toasts -------------------------------------------------------------

function toast(message, kind = "info") {
  const stack = $("#toast-stack");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

// --- API helper -----------------------------------------------------------
//
// Every write endpoint needs X-API-Key (app/api/auth.py); order/payment
// creation additionally needs a fresh Idempotency-Key per logical attempt —
// generated here, once per form submit, never reused across retries within
// the same click.

async function api(method, path, { body, idempotencyKey, query } = {}) {
  let url = apiBase() + path;
  if (query) {
    const qs = new URLSearchParams(
      Object.entries(query).filter(([, v]) => v !== undefined && v !== null && v !== "")
    ).toString();
    if (qs) url += `?${qs}`;
  }

  const headers = { "Content-Type": "application/json", "X-API-Key": apiKeyInput.value.trim() };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

  const res = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }

  if (!res.ok) {
    const detail =
      (data && (data.detail || data.error)) ||
      (typeof data === "string" ? data : `HTTP ${res.status}`);
    const message = Array.isArray(detail)
      ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
      : String(detail);
    throw new Error(message);
  }
  return data;
}

function newIdempotencyKey() {
  return (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`);
}

function shortId(id) {
  return id ? id.slice(0, 8) : "";
}

function money(minor) {
  return (minor / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// --- health polling ---------------------------------------------------------

async function checkHealth() {
  const dot = $("#status-dot");
  const label = $("#last-checked");
  try {
    const res = await fetch(apiBase() + "/health/live");
    if (res.ok) {
      dot.className = "dot up";
      label.textContent = `connected · ${new Date().toLocaleTimeString()}`;
    } else {
      throw new Error("not ok");
    }
  } catch {
    dot.className = "dot down";
    label.textContent = "cannot reach API";
  }
}

// --- tabs -------------------------------------------------------------------

$$(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tab-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    $$(".panel").forEach((p) => p.classList.remove("active"));
    $(`#panel-${btn.dataset.tab}`).classList.add("active");
  });
});

// --- status badges ------------------------------------------------------

const STATUS_CLASS = {
  captured: "ok", filled: "ok",
  authorized: "info", open: "info", partially_filled: "warn",
  voided: "danger", cancelled: "danger", failed: "danger",
  refunded: "warn", initiated: "warn",
};

function badge(status) {
  const cls = STATUS_CLASS[status] || "info";
  return `<span class="badge ${cls}">${status.replace(/_/g, " ")}</span>`;
}

// --- shared state ---------------------------------------------------------

let accounts = [];
let payments = [];
let instruments = [];
let orders = [];
let trades = [];
let selectedInstrumentId = null;

const isPositionAccount = (a) => a.name.startsWith("position:");

// A position account's balance_minor is a whole-unit QUANTITY (shares of an
// instrument — see ADR 0008 Decision 4: `currency` there is the instrument
// symbol, not a real currency), not currency minor units. money() dividing
// it by 100 would show "5 shares" as "0.05" — quietly wrong, not just
// cosmetic, since it's the same column a cash balance renders in.
function balanceDisplay(account) {
  return isPositionAccount(account) ? String(account.balance_minor) : money(account.balance_minor);
}

// ============================================================================
// ACCOUNTS
// ============================================================================

async function loadAccounts() {
  accounts = await api("GET", "/accounts");
  renderAccounts();
  fillAccountDropdowns();
}

function renderAccounts() {
  $("#accounts-count").textContent = accounts.length;
  const body = $("#accounts-body");
  if (!accounts.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="5">No accounts yet — create one on the left</td></tr>`;
    return;
  }
  body.innerHTML = accounts.map((a) => `
    <tr>
      <td>${a.owner_id}</td>
      <td>${a.name}${isPositionAccount(a) ? ' <span class="badge info">position</span>' : ""}</td>
      <td>${a.currency}</td>
      <td class="num ${a.balance_minor < 0 ? "neg" : ""}">${balanceDisplay(a)}</td>
      <td class="mono" title="${a.id}">${shortId(a.id)}</td>
    </tr>
  `).join("");
}

function fillAccountDropdowns() {
  const cashOnly = accounts.filter((a) => !isPositionAccount(a));
  const opt = (a) => `<option value="${a.id}">${a.owner_id} — ${a.name} (${a.currency}) — ${shortId(a.id)}</option>`;

  for (const sel of [$("#pay-payer"), $("#pay-payee"), $("#order-account")]) {
    const prev = sel.value;
    sel.innerHTML = cashOnly.length
      ? cashOnly.map(opt).join("")
      : `<option value="">No cash accounts yet</option>`;
    if (cashOnly.some((a) => a.id === prev)) sel.value = prev;
  }
}

$("#form-account").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("POST", "/accounts", {
      body: {
        owner_id: $("#acc-owner").value.trim(),
        name: $("#acc-name").value.trim(),
        account_type: $("#acc-type").value,
        currency: $("#acc-currency").value.trim().toUpperCase(),
        allow_negative: $("#acc-negative").checked,
      },
    });
    toast("Account created", "success");
    $("#form-account").reset();
    $("#acc-name").value = "wallet";
    $("#acc-currency").value = "INR";
    await loadAccounts();
  } catch (err) {
    toast(err.message, "error");
  }
});

// ============================================================================
// PAYMENTS
// ============================================================================

async function loadPayments() {
  payments = await api("GET", "/payments");
  renderPayments();
}

function renderPayments() {
  $("#payments-count").textContent = payments.length;
  const body = $("#payments-body");
  if (!payments.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="5">No payments yet</td></tr>`;
    return;
  }
  body.innerHTML = payments.map((p) => `
    <tr>
      <td>${badge(p.status)}</td>
      <td class="num">${money(p.amount_minor)}</td>
      <td>${p.currency}</td>
      <td class="mono" title="${p.id}">${shortId(p.id)}</td>
      <td>
        ${p.status === "authorized" ? `
          <button class="btn small" data-capture="${p.id}">Capture</button>
          <button class="btn small danger-outline" data-void="${p.id}">Void</button>
        ` : ""}
      </td>
    </tr>
  `).join("");
}

$("#form-payment").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("POST", "/payments", {
      idempotencyKey: newIdempotencyKey(),
      body: {
        payer_account_id: $("#pay-payer").value,
        payee_account_id: $("#pay-payee").value,
        amount_minor: Number($("#pay-amount").value),
        currency: $("#pay-currency").value.trim().toUpperCase(),
      },
    });
    toast("Payment authorized", "success");
    $("#pay-amount").value = "";
    await Promise.all([loadPayments(), loadAccounts()]);
  } catch (err) {
    toast(err.message, "error");
  }
});

$("#payments-body").addEventListener("click", async (e) => {
  const capture = e.target.dataset.capture;
  const voidId = e.target.dataset.void;
  try {
    if (capture) {
      await api("POST", `/payments/${capture}/capture`);
      toast("Payment captured", "success");
    } else if (voidId) {
      await api("POST", `/payments/${voidId}/void`);
      toast("Payment voided", "success");
    } else {
      return;
    }
    await Promise.all([loadPayments(), loadAccounts()]);
  } catch (err) {
    toast(err.message, "error");
  }
});

// ============================================================================
// TRADING
// ============================================================================

async function loadInstruments() {
  instruments = await api("GET", "/instruments");
  const sel = $("#instrument-select");
  const prev = selectedInstrumentId;
  sel.innerHTML = instruments.length
    ? instruments.map((i) => `<option value="${i.id}">${i.symbol} / ${i.quote_currency}</option>`).join("")
    : `<option value="">No instruments yet</option>`;

  if (instruments.some((i) => i.id === prev)) {
    sel.value = prev;
  } else {
    selectedInstrumentId = instruments[0]?.id || null;
    sel.value = selectedInstrumentId || "";
  }
  await loadTradingData();
}

$("#instrument-select").addEventListener("change", async (e) => {
  selectedInstrumentId = e.target.value || null;
  await loadTradingData();
});

$("#btn-create-instrument").addEventListener("click", async () => {
  const symbol = $("#new-symbol").value.trim().toUpperCase();
  const quote = $("#new-quote-ccy").value.trim().toUpperCase();
  if (!symbol) { toast("Enter a symbol first", "error"); return; }
  try {
    const instrument = await api("POST", "/instruments", { body: { symbol, quote_currency: quote } });
    toast(`Instrument ${instrument.symbol} created`, "success");
    $("#new-symbol").value = "";
    selectedInstrumentId = instrument.id;
    await loadInstruments();
  } catch (err) {
    toast(err.message, "error");
  }
});

async function loadTradingData() {
  if (!selectedInstrumentId) {
    orders = []; trades = [];
    renderOrders(); renderTrades(); renderOrderBook(); renderInstrumentStats();
    return;
  }
  [orders, trades] = await Promise.all([
    api("GET", "/orders", { query: { instrument_id: selectedInstrumentId } }),
    api("GET", "/trades", { query: { instrument_id: selectedInstrumentId } }),
  ]);
  renderOrders();
  renderTrades();
  renderOrderBook();
  renderInstrumentStats();
}

function renderInstrumentStats() {
  const box = $("#instrument-stats");
  if (!selectedInstrumentId) { box.innerHTML = ""; return; }
  const instrument = instruments.find((i) => i.id === selectedInstrumentId);
  const openOrders = orders.filter((o) => o.status === "open" || o.status === "partially_filled");
  const lastTrade = trades[0];
  box.innerHTML = `
    <div class="stat"><div class="label">Symbol</div><div class="value">${instrument?.symbol ?? "—"}</div></div>
    <div class="stat"><div class="label">Last price</div><div class="value">${lastTrade ? money(lastTrade.price_minor) : "—"}</div></div>
    <div class="stat"><div class="label">Resting orders</div><div class="value">${openOrders.length}</div></div>
    <div class="stat"><div class="label">Trades</div><div class="value">${trades.length}</div></div>
  `;
}

function renderOrderBook() {
  const resting = orders.filter((o) => o.status === "open" || o.status === "partially_filled");
  const bids = resting
    .filter((o) => o.side === "buy")
    .sort((a, b) => (b.limit_price_minor - a.limit_price_minor) || (a.sequence - b.sequence));
  const asks = resting
    .filter((o) => o.side === "sell")
    .sort((a, b) => (a.limit_price_minor - b.limit_price_minor) || (a.sequence - b.sequence));

  const maxQty = Math.max(1, ...resting.map((o) => o.quantity - o.filled_quantity));

  const row = (o) => {
    const remaining = o.quantity - o.filled_quantity;
    const pct = Math.round((remaining / maxQty) * 100);
    return `<div class="book-row" style="--pct:${pct}%"><span>${money(o.limit_price_minor)}</span><span>${remaining}</span></div>`;
  };

  $("#book-bids").innerHTML = bids.length ? bids.map(row).join("") : `<p class="hint">No bids</p>`;
  $("#book-asks").innerHTML = asks.length ? asks.map(row).join("") : `<p class="hint">No asks</p>`;
}

function renderOrders() {
  $("#orders-count").textContent = orders.length;
  const body = $("#orders-body");
  if (!orders.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="8">No orders for this instrument yet</td></tr>`;
    return;
  }
  body.innerHTML = orders.map((o) => `
    <tr>
      <td class="mono">${o.sequence}</td>
      <td><span class="badge side-${o.side}">${o.side}</span></td>
      <td>${o.order_type}</td>
      <td class="num">${o.limit_price_minor != null ? money(o.limit_price_minor) : "market"}</td>
      <td class="num">${o.quantity}</td>
      <td class="num">${o.filled_quantity}</td>
      <td>${badge(o.status)}</td>
      <td class="mono" title="${o.id}">${shortId(o.id)}</td>
    </tr>
  `).join("");
}

function renderTrades() {
  $("#trades-count").textContent = trades.length;
  const body = $("#trades-body");
  if (!trades.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="4">No trades for this instrument yet</td></tr>`;
    return;
  }
  body.innerHTML = trades.map((t) => `
    <tr>
      <td class="num">${money(t.price_minor)}</td>
      <td class="num">${t.quantity}</td>
      <td class="mono" title="${t.buy_order_id}">${shortId(t.buy_order_id)}</td>
      <td class="mono" title="${t.sell_order_id}">${shortId(t.sell_order_id)}</td>
    </tr>
  `).join("");
}

// --- order form: side/type segmented toggles -------------------------------

let orderSide = "buy";
let orderType = "limit";

function wireSegmented(containerId, onChange) {
  const container = $(`#${containerId}`);
  $$("button", container).forEach((btn) => {
    btn.addEventListener("click", () => {
      $$("button", container).forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      onChange(btn.dataset.value);
    });
  });
}

wireSegmented("side-toggle", (v) => { orderSide = v; });
wireSegmented("type-toggle", (v) => {
  orderType = v;
  const priceField = $("#price-field");
  const priceInput = $("#order-price");
  if (v === "market") {
    priceField.style.opacity = "0.4";
    priceInput.disabled = true;
    priceInput.value = "";
  } else {
    priceField.style.opacity = "1";
    priceInput.disabled = false;
  }
});

$("#form-order").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!selectedInstrumentId) { toast("Create or select an instrument first", "error"); return; }
  const cashAccount = $("#order-account").value;
  if (!cashAccount) { toast("Create a cash account first", "error"); return; }

  const body = {
    instrument_id: selectedInstrumentId,
    cash_account_id: cashAccount,
    side: orderSide,
    order_type: orderType,
    quantity: Number($("#order-qty").value),
  };
  if (orderType === "limit") {
    body.limit_price_minor = Number($("#order-price").value);
  }

  try {
    const order = await api("POST", "/orders", { idempotencyKey: newIdempotencyKey(), body });
    toast(`Order ${shortId(order.id)} → ${order.status}`, "success");
    $("#order-qty").value = "";
    await Promise.all([loadTradingData(), loadAccounts()]);
  } catch (err) {
    toast(err.message, "error");
  }
});

// --- manual refresh buttons -------------------------------------------------

$$("[data-refresh]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const target = btn.dataset.refresh;
    try {
      if (target === "accounts") await loadAccounts();
      if (target === "payments") await loadPayments();
      if (target === "trading") await loadTradingData();
      toast("Refreshed", "success");
    } catch (err) {
      toast(err.message, "error");
    }
  });
});

// --- boot ---------------------------------------------------------------

async function boot() {
  await checkHealth();
  setInterval(checkHealth, 8000);
  try {
    await Promise.all([loadAccounts(), loadPayments(), loadInstruments()]);
  } catch (err) {
    toast(`Could not load initial data: ${err.message}`, "error");
  }
}

boot();
