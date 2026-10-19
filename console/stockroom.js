let token = "";
let identity = null;
let products = [];
let warehouses = [];
let activeTab = "balances";
let epoch = 0;
let busy = false;
const limit = 25;
const offsets = { balances: 0, reservations: 0, movements: 0 };
const pages = {};
const pendingKeys = new Map();
const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const number = (value) => Number(value || 0).toLocaleString("ru-RU");
const date = (value) =>
  value
    ? new Date(value).toLocaleString("ru-RU", {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
const statuses = {
  active: "Активен",
  confirmed: "Отгружен",
  cancelled: "Отменён",
  expired: "Срок истёк",
};
const movementNames = {
  receipt: "Приход",
  reserve: "Резерв",
  confirm: "Отгрузка",
  cancel: "Отмена",
  expire: "Истечение",
};
function notice(message, error = false) {
  for (const item of products) message = message.replaceAll(item.id, item.sku);
  for (const item of warehouses)
    message = message.replaceAll(item.id, item.code);
  $("notice").hidden = !message;
  $("notice").textContent = message;
  $("notice").dataset.error = String(error);
}
async function api(path, options = {}) {
  if (!token) throw new Error("Сначала подключитесь с ключом.");
  const response = await fetch(path, {
    ...options,
    headers: {
      "X-API-Key": token,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
    signal: AbortSignal.timeout(10000),
  });
  const body = response.status === 204 ? null : await response.json();
  if (!response.ok)
    throw new Error(
      body?.detail || body?.title || `Запрос не выполнен: ${response.status}`,
    );
  return body;
}
async function write(path, body, idempotency = false) {
  const fingerprint = path + JSON.stringify(body);
  if (idempotency && !pendingKeys.has(fingerprint))
    pendingKeys.set(fingerprint, crypto.randomUUID());
  const result = await api(path, {
    method: "POST",
    body: JSON.stringify(body),
    headers: idempotency
      ? { "Idempotency-Key": pendingKeys.get(fingerprint) }
      : {},
  });
  pendingKeys.delete(fingerprint);
  return result;
}
function controls() {
  $("connect").querySelector('button[type="submit"]').disabled = busy;
  $("disconnect").disabled = busy;
  document
    .querySelectorAll("[data-write]")
    .forEach(
      (button) =>
        (button.disabled = busy || !identity || identity.role === "reader"),
    );
  $("refresh").disabled = busy || !identity;
  $("add-line").disabled = busy || !identity || identity.role === "reader";
  const page = pages[activeTab];
  $("previous").disabled = busy || !page || offsets[activeTab] === 0;
  $("next").disabled =
    busy || !page || offsets[activeTab] + limit >= page.total;
  $("page-info").textContent = page
    ? `Строки ${page.total ? offsets[activeTab] + 1 : 0}–${Math.min(offsets[activeTab] + page.items.length, page.total)} из ${page.total}`
    : "Данные не загружены";
}
async function task(action) {
  if (busy) return;
  busy = true;
  controls();
  try {
    await action();
  } catch (error) {
    notice(
      error.name === "TimeoutError" || error.name === "TypeError"
        ? "Сервер не ответил. Проверьте связь и повторите действие: ключ операции сохранён в этой вкладке."
        : error.message,
      true,
    );
  } finally {
    busy = false;
    controls();
  }
}
function fillSelects() {
  for (const [selector, items, label] of [
    [".product-options", products, (p) => `${p.sku} · ${p.name}`],
    [".warehouse-options", warehouses, (w) => `${w.code} · ${w.name}`],
  ]) {
    document.querySelectorAll(selector).forEach((select) => {
      const current = select.value;
      select.replaceChildren(
        new Option(
          items.length ? "Выберите…" : "Сначала добавьте в справочник",
          "",
        ),
        ...items.map((item) => new Option(label(item), item.id)),
      );
      if (items.some((item) => item.id === current)) select.value = current;
    });
  }
}
function addLine() {
  const row = document.createElement("div");
  row.className = "reservation-line stack";
  row.innerHTML =
    '<label>Товар<select class="product-options" name="line_product" required></select></label><label>Склад<select class="warehouse-options" name="line_warehouse" required></select></label><div class="row"><label>Количество<input name="line_quantity" type="number" value="1" min="1" max="1000000000" required></label><button type="button" class="small fit">Убрать</button></div>';
  row.querySelector("button").addEventListener("click", () => {
    if ($("reservation-lines").children.length > 1) row.remove();
    else notice("В резерве должна остаться хотя бы одна позиция.", true);
  });
  $("reservation-lines").append(row);
  fillSelects();
}
function render(name, page) {
  pages[name] = page;
  $(`${name}-empty`).hidden = page.items.length > 0;
  $(`${name}-empty`).textContent = "В этой выборке пока нет записей.";
  if (name === "balances") {
    $("balances-body").innerHTML = page.items
      .map(
        (row) =>
          `<tr><td><strong>${esc(row.product_name)}</strong><span class="subline mono">${esc(row.sku)} / ${esc(row.warehouse_code)}</span></td><td class="numeric available">${number(row.available)}</td><td class="numeric">${number(row.reserved)}</td><td class="numeric">${number(row.on_hand)}</td></tr>`,
      )
      .join("");
    $("position-count").textContent = number(page.items.length);
    $("available-count").textContent = number(
      page.items.reduce((n, r) => n + Number(r.available), 0),
    );
    $("reserved-count").textContent = number(
      page.items.reduce((n, r) => n + Number(r.reserved), 0),
    );
  } else if (name === "reservations") {
    $("reservations-body").innerHTML = page.items
      .map(
        (row) =>
          `<tr><td><strong>${esc(row.order_ref)}</strong><span class="subline">${row.items.map((item) => `${esc(item.sku)} × ${number(item.quantity)}`).join(", ")}</span><span class="subline mono">${esc(row.id.slice(0, 8))}</span></td><td><span class="status ${row.status === "confirmed" ? "good" : row.status === "active" ? "pending" : ""}">${esc(statuses[row.status] || row.status)}</span></td><td class="small-text">${date(row.expires_at)}</td><td class="actions">${row.status === "active" && identity?.role !== "reader" ? `<button class="small" type="button" data-reservation="${esc(row.id)}" data-action="confirm">Отгрузить</button> <button class="small quiet" type="button" data-reservation="${esc(row.id)}" data-action="cancel">Отменить</button>` : "—"}</td></tr>`,
      )
      .join("");
  } else {
    $("movements-body").innerHTML = page.items
      .map(
        (row) =>
          `<tr><td><strong>${esc(movementNames[row.kind] || row.kind)}</strong><span class="subline">${esc(row.reference)} · ${date(row.created_at)}</span></td><td><span class="mono">${esc(row.sku)}</span><span class="subline">${esc(row.warehouse_code)}</span></td><td class="numeric">${Number(row.delta_available) > 0 ? "+" : ""}${number(row.delta_available)}</td><td class="numeric">${Number(row.delta_reserved) > 0 ? "+" : ""}${number(row.delta_reserved)}</td></tr>`,
      )
      .join("");
  }
}
async function loadPage(name) {
  const ticket = epoch;
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offsets[name]),
  });
  if (name === "reservations" && $("reservation-status").value)
    query.set("status", $("reservation-status").value);
  const result = await api(`/api/${name}?${query}`);
  if (ticket === epoch) render(name, result);
}
async function refresh() {
  await Promise.all(["balances", "reservations", "movements"].map(loadPage));
  controls();
}
async function afterCommit(message, updateCatalog = false) {
  notice(message);
  try {
    if (updateCatalog) await loadCatalog();
    await refresh();
  } catch {
    notice(
      message +
        " Изменение сохранено. Список не обновился — нажмите «Обновить данные».",
      true,
    );
  }
}
async function loadCatalog() {
  async function all(path) {
    let list = [],
      offset = 0;
    for (;;) {
      const page = await api(`${path}?limit=100&offset=${offset}`);
      list.push(...page.items);
      offset += page.items.length;
      if (offset >= page.total || !page.items.length) break;
      if (offset >= 10000)
        throw new Error(
          "Справочник слишком большой для этой консоли. Используйте API.",
        );
    }
    return list;
  }
  [products, warehouses] = await Promise.all([
    all("/api/products"),
    all("/api/warehouses"),
  ]);
  fillSelects();
}
$("connect").addEventListener("submit", (event) => {
  event.preventDefault();
  task(async () => {
    token = $("api-key").value.trim();
    epoch++;
    identity = null;
    for (const name of ["balances", "reservations", "movements"]) {
      delete pages[name];
      $(name + "-body").replaceChildren();
      $(name + "-empty").hidden = false;
      $(name + "-empty").textContent = "Данные не загружены.";
    }
    for (const id of ["position-count", "available-count", "reserved-count"])
      $(id).textContent = "—";
    document.body.classList.remove("connected");
    $("identity").textContent = "Подключение…";
    try {
      identity = await api("/api/me");
      document.body.classList.add("connected");
      await loadCatalog();
      await refresh();
      $("identity").textContent =
        `${identity.name} / ${{ reader: "Просмотр", operator: "Оператор", admin: "Администратор" }[identity.role] || identity.role}`;
      $("api-key").value = "";
      $("disconnect").hidden = false;
      notice("Рабочее место подключено. Данные загружены с сервера.");
    } catch (error) {
      token = "";
      identity = null;
      document.body.classList.remove("connected");
      $("identity").textContent = "Подключение не выполнено";
      throw error;
    }
  });
});
$("disconnect").addEventListener("click", () => {
  epoch++;
  token = "";
  identity = null;
  document.body.classList.remove("connected");
  pendingKeys.clear();
  $("identity").textContent = "Ключ хранится только в этой вкладке";
  $("disconnect").hidden = true;
  for (const name of ["balances", "reservations", "movements"]) {
    $(`${name}-body`).replaceChildren();
    $(`${name}-empty`).hidden = false;
    $(`${name}-empty`).textContent =
      "Подключитесь с ключом, чтобы увидеть данные.";
    delete pages[name];
  }
  for (const id of ["position-count", "available-count", "reserved-count"])
    $(id).textContent = "—";
  notice("Рабочее место отключено.");
  controls();
});
$("refresh").addEventListener("click", () =>
  task(async () => {
    await loadCatalog();
    await refresh();
    notice("Данные обновлены.");
  }),
);
document.querySelectorAll("[data-tab]").forEach((button) =>
  button.addEventListener("click", () => {
    activeTab = button.dataset.tab;
    document
      .querySelectorAll("[data-tab]")
      .forEach((tab) =>
        tab.setAttribute("aria-selected", String(tab === button)),
      );
    for (const name of ["balances", "reservations", "movements"])
      $(`${name}-pane`).hidden = name !== activeTab;
    controls();
  }),
);
for (const [id, direction] of [
  ["previous", -1],
  ["next", 1],
])
  $(id).addEventListener("click", () =>
    task(async () => {
      offsets[activeTab] = Math.max(0, offsets[activeTab] + direction * limit);
      await loadPage(activeTab);
    }),
  );
$("reservation-status").addEventListener("change", () =>
  task(async () => {
    offsets.reservations = 0;
    await loadPage("reservations");
  }),
);
$("add-line").addEventListener("click", addLine);
for (const [formId, path] of [
  ["product-form", "/api/products"],
  ["warehouse-form", "/api/warehouses"],
])
  $(formId).addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    task(async () => {
      await write(path, Object.fromEntries(new FormData(form)));
      form.reset();
      await afterCommit("Справочник обновлён.", true);
    });
  });
$("receipt-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  task(async () => {
    const data = Object.fromEntries(new FormData(form));
    data.quantity = Number(data.quantity);
    await write("/api/receipts", data, true);
    form.elements.reference.value = "";
    await afterCommit("Приход записан в журнал.");
  });
});
$("reserve-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  task(async () => {
    const data = new FormData(form);
    const items = Array.from($("reservation-lines").children).map((row) => ({
      product_id: row.querySelector("[name=line_product]").value,
      warehouse_id: row.querySelector("[name=line_warehouse]").value,
      quantity: Number(row.querySelector("[name=line_quantity]").value),
    }));
    const result = await write(
      "/api/reservations",
      {
        order_ref: data.get("order_ref"),
        ttl_seconds: Number(data.get("ttl")) * 60,
        items,
      },
      true,
    );
    offsets.reservations = 0;
    await afterCommit(
      `Резерв ${result.order_ref} создан до ${date(result.expires_at)}.`,
    );
  });
});
$("reservations-body").addEventListener("click", (event) => {
  const button = event.target.closest("[data-reservation]");
  if (!button) return;
  task(async () => {
    await write(
      `/api/reservations/${button.dataset.reservation}/${button.dataset.action}`,
      {
        note:
          button.dataset.action === "confirm"
            ? "Отгрузка из рабочего места"
            : "Отмена из рабочего места",
      },
      true,
    );
    await afterCommit(
      button.dataset.action === "confirm"
        ? "Отгрузка подтверждена."
        : "Резерв отменён, товар снова доступен.",
    );
  });
});
addLine();
controls();

// Automatic activation and roving focus for the journal tabs.
const journalTabs = Array.from(document.querySelectorAll('[role="tab"]'));
function tabStops() {
  journalTabs.forEach(
    (tab) =>
      (tab.tabIndex = tab.getAttribute("aria-selected") === "true" ? 0 : -1),
  );
}
journalTabs.forEach((tab, index) => {
  tab.addEventListener("click", tabStops);
  tab.addEventListener("keydown", (event) => {
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % journalTabs.length;
    else if (event.key === "ArrowLeft")
      next = (index + journalTabs.length - 1) % journalTabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = journalTabs.length - 1;
    else return;
    event.preventDefault();
    journalTabs[next].focus();
    journalTabs[next].click();
  });
});
tabStops();
