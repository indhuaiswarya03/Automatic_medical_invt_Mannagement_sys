const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
const QK = "mic_queue", IK = "mic_inventory";
const getQ = () => { try { return JSON.parse(localStorage.getItem(QK) || "[]"); } catch (e) { return []; } };
const setQ = q => localStorage.setItem(QK, JSON.stringify(q));
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const post = (u, b) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });

function toast(m) {
  const t = document.createElement("div"); t.className = "toast"; t.textContent = m; document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}
function status() {
  const b = $("#net"), on = navigator.onLine;
  if (b) { b.textContent = on ? "ONLINE" : "OFFLINE"; b.className = "net " + (on ? "on" : "off"); }
  const n = getQ().length, p = $("#pending");
  if (p) p.textContent = n ? n + " change(s) waiting to sync" : "";
}
function queue(op) { setQ([...getQ(), op]); status(); showCache(); toast("Offline: saved on this device. It will sync automatically."); }

async function sync() {
  const q = getQ(); if (!navigator.onLine || !q.length) return;
  try {
    const r = await post("/api/offline-sync", { ops: q });
    if (!r.ok) return;
    const d = await r.json(); setQ([]); status();
    toast("Synced " + d.applied + " change(s)" + (d.errors.length ? ", " + d.errors.length + " skipped" : ""));
    setTimeout(() => location.reload(), 1200);
  } catch (e) { /* stay queued */ }
}

function showCache() {
  const box = $("#offline-cache"); if (!box) return;
  if (navigator.onLine) { box.hidden = true; return; }
  let items = []; try { items = JSON.parse(localStorage.getItem(IK) || "[]"); } catch (e) {}
  getQ().filter(o => o.type === "inventory_update").forEach(o => { const i = items.find(x => x.id == o.id); if (i) i.quantity = o.quantity; });
  $("tbody", box).innerHTML = items.map(i => `<tr><td>${esc(i.item_name)}</td><td>${esc(i.category)}</td><td>${esc(i.quantity)}</td><td>${esc(i.minimum_quantity)}</td><td>${esc(i.priority)}</td></tr>`).join("")
    || '<tr><td colspan="5">No cached data yet. Open this page once while online.</td></tr>';
  box.hidden = false;
}

$$("form.qty").forEach(f => f.addEventListener("submit", async e => {
  e.preventDefault();
  const id = +f.dataset.id, quantity = +f.quantity.value;
  try {
    if (!navigator.onLine) throw 0;
    const r = await post("/api/inventory/update", { id, quantity }), d = await r.json();
    if (!r.ok) return toast(d.error || "Unable to update inventory.");
    toast("Quantity updated."); setTimeout(() => location.reload(), 500);
  } catch (err) { queue({ type: "inventory_update", id, quantity }); }
}));

$$("form.temp").forEach(f => f.addEventListener("submit", async e => {
  e.preventDefault();
  const id = +f.dataset.id, temperature = +f.temperature.value;
  try {
    if (!navigator.onLine) throw 0;
    const r = await post("/api/cold-chain", { id, temperature }), d = await r.json();
    if (!r.ok) return toast(d.error || "Unable to update temperature.");
    location.reload();
  } catch (err) { queue({ type: "temperature", id, temperature }); }
}));

const rf = $("#req-form");
if (rf) rf.addEventListener("submit", e => {
  if (navigator.onLine) return;
  e.preventDefault(); queue({ type: "requirement", data: Object.fromEntries(new FormData(rf)) }); rf.reset();
});

const cc = $("#is_cold_chain");
if (cc) { const sh = () => $("#cold-fields").hidden = !cc.checked; cc.addEventListener("change", sh); sh(); }

$$("[data-logout]").forEach(a => a.addEventListener("click", () => {
  if (window.caches) caches.keys().then(k => k.forEach(x => caches.delete(x)));
}));

window.addEventListener("online", () => { status(); showCache(); sync(); });
window.addEventListener("offline", () => { status(); showCache(); });
status(); showCache(); sync();
if (navigator.onLine && ["hospital", "admin"].includes(document.body.dataset.role))
  fetch("/api/inventory").then(r => r.ok ? r.json() : null).then(d => d && localStorage.setItem(IK, JSON.stringify(d.items))).catch(() => {});
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
