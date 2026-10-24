/**
 * MediCrisis - Core App Logic, Offline Sync & 3D Interactive Graphics Engine
 */

const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
const QK = "mic_queue", IK = "mic_inventory";
const getQ = () => { try { return JSON.parse(localStorage.getItem(QK) || "[]"); } catch (e) { return []; } };
const setQ = q => localStorage.setItem(QK, JSON.stringify(q));
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const post = (u, b) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });

function toast(m) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = m;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

function status() {
  const b = $("#net"), on = navigator.onLine;
  if (b) {
    b.textContent = on ? "ONLINE" : "OFFLINE";
    b.className = "net " + (on ? "on" : "off");
  }
  const n = getQ().length, p = $("#pending");
  if (p) p.textContent = n ? `${n} change(s) waiting to sync` : "";
}

function queue(op) {
  setQ([...getQ(), op]);
  status();
  showCache();
  toast("Offline: saved on this device. It will sync automatically.");
}

let isSyncing = false;
async function sync() {
  const q = getQ();
  if (!navigator.onLine || !q.length || isSyncing) return;
  isSyncing = true;
  try {
    const r = await post("/api/offline-sync", { ops: q });
    if (!r.ok) { isSyncing = false; return; }
    const d = await r.json();
    setQ([]);
    status();
    toast(`Synced ${d.applied} change(s)` + (d.errors.length ? `, ${d.errors.length} skipped` : ""));
    setTimeout(() => location.reload(), 1200);
  } catch (e) {
    /* stay queued */
  } finally {
    isSyncing = false;
  }
}

function showCache() {
  const box = $("#offline-cache");
  if (!box) return;
  if (navigator.onLine) {
    box.hidden = true;
    return;
  }
  let items = [];
  try { items = JSON.parse(localStorage.getItem(IK) || "[]"); } catch (e) {}
  getQ().filter(o => o.type === "inventory_update").forEach(o => {
    const i = items.find(x => x.id == o.id);
    if (i) i.quantity = o.quantity;
  });
  const tbody = $("tbody", box);
  if (tbody) {
    tbody.innerHTML = items.map(i => `<tr><td>${esc(i.item_name)}</td><td>${esc(i.category)}</td><td>${esc(i.quantity)}</td><td>${esc(i.minimum_quantity)}</td><td>${esc(i.priority)}</td></tr>`).join("")
      || '<tr><td colspan="5">No cached data yet. Open this page once while online.</td></tr>';
  }
  box.hidden = false;
}

// Live Digital Clock
function updateClock() {
  const clockEl = $("#live-clock");
  if (!clockEl) return;
  const now = new Date();
  clockEl.textContent = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
setInterval(updateClock, 1000);
updateClock();

// Quantity update form handlers
$$("form.qty[data-id]").forEach(f => f.addEventListener("submit", async e => {
  e.preventDefault();
  const id = +f.dataset.id, quantity = +f.quantity.value;
  try {
    if (!navigator.onLine) throw 0;
    const r = await post("/api/inventory/update", { id, quantity }), d = await r.json();
    if (!r.ok) return toast(d.error || "Unable to update inventory.");
    toast("Quantity updated.");
    setTimeout(() => location.reload(), 500);
  } catch (err) {
    queue({ type: "inventory_update", id, quantity });
  }
}));

// Temperature update form handlers
$$("form.temp").forEach(f => f.addEventListener("submit", async e => {
  e.preventDefault();
  const id = +f.dataset.id, temperature = +f.temperature.value;
  try {
    if (!navigator.onLine) throw 0;
    const r = await post("/api/cold-chain", { id, temperature }), d = await r.json();
    if (!r.ok) return toast(d.error || "Unable to update temperature.");
    location.reload();
  } catch (err) {
    queue({ type: "temperature", id, temperature });
  }
}));

// Requirement offline form handling & date restriction
const rf = $("#req-form");
if (rf) {
  const reqByInput = $('input[type="date"][name="required_by"]', rf);
  if (reqByInput && !reqByInput.getAttribute("min")) {
    const todayStr = new Date().toISOString().split("T")[0];
    reqByInput.setAttribute("min", todayStr);
  }
  rf.addEventListener("submit", e => {
    if (navigator.onLine) return;
    e.preventDefault();
    queue({ type: "requirement", data: Object.fromEntries(new FormData(rf)) });
    rf.reset();
  });
}

// Cold-chain checkbox toggle handler
const cc = $("#is_cold_chain");
if (cc) {
  const sh = () => {
    const fields = $("#cold-fields");
    if (fields) fields.hidden = !cc.checked;
  };
  cc.addEventListener("change", sh);
  sh();
}

// Logout cache cleanup
$$("[data-logout]").forEach(a => a.addEventListener("click", () => {
  if (window.caches) caches.keys().then(k => k.forEach(x => caches.delete(x)));
}));

window.addEventListener("online", () => { status(); showCache(); sync(); });
window.addEventListener("offline", () => { status(); showCache(); });
status();
showCache();
sync();

if (navigator.onLine && ["hospital", "admin"].includes(document.body.dataset.role)) {
  fetch("/api/inventory")
    .then(r => r.ok ? r.json() : null)
    .then(d => d && localStorage.setItem(IK, JSON.stringify(d.items)))
    .catch(() => {});
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

/* ==========================================================================
   Interactive 3D Card Tilt Effect - Disabled to prevent moving cards/forms
   ========================================================================== */
function initTiltEffect() {
  // Disabled: ensures login cards, forms, and detail entry components remain rock-solid and stable
}

/* ==========================================================================
   Three.js 3D Crisis Shield & Hologram Animation Engine
   ========================================================================== */
function initHero3D() {
  const canvas = $("#hero-3d-canvas");
  if (!canvas || typeof THREE === "undefined") return;

  const container = canvas.parentElement;
  const width = container.clientWidth || 400;
  const height = container.clientHeight || 320;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000);
  camera.position.z = 8;

  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setSize(width, height);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

  // Medical Cross 3D Object Group
  const crossGroup = new THREE.Group();

  // Materials with high-tech glowing metallic properties
  const crossMaterial = new THREE.MeshStandardMaterial({
    color: 0x06b6d4,
    metalness: 0.85,
    roughness: 0.2,
    emissive: 0x083344,
    emissiveIntensity: 0.4
  });

  const coreMaterial = new THREE.MeshStandardMaterial({
    color: 0x38bdf8,
    metalness: 0.95,
    roughness: 0.1,
    emissive: 0x0ea5e9,
    emissiveIntensity: 0.6
  });

  // Vertical Bar
  const verticalGeo = new THREE.BoxGeometry(0.85, 2.7, 0.6);
  const vBar = new THREE.Mesh(verticalGeo, crossMaterial);
  crossGroup.add(vBar);

  // Horizontal Bar
  const horizontalGeo = new THREE.BoxGeometry(2.7, 0.85, 0.6);
  const hBar = new THREE.Mesh(horizontalGeo, crossMaterial);
  crossGroup.add(hBar);

  // Glowing Center Core
  const centerGeo = new THREE.BoxGeometry(0.88, 0.88, 0.68);
  const centerMesh = new THREE.Mesh(centerGeo, coreMaterial);
  crossGroup.add(centerMesh);

  scene.add(crossGroup);

  // Holographic Orbiting Particle Ring
  const particleCount = 200;
  const particleGeo = new THREE.BufferGeometry();
  const particlePositions = new Float32Array(particleCount * 3);

  for (let i = 0; i < particleCount; i++) {
    const angle = (i / particleCount) * Math.PI * 2;
    const radius = 2.4 + (Math.random() - 0.5) * 0.7;
    particlePositions[i * 3] = Math.cos(angle) * radius;
    particlePositions[i * 3 + 1] = (Math.random() - 0.5) * 0.8 + Math.sin(angle * 2) * 0.3;
    particlePositions[i * 3 + 2] = Math.sin(angle) * radius;
  }

  particleGeo.setAttribute('position', new THREE.BufferAttribute(particlePositions, 3));
  const particleMat = new THREE.PointsMaterial({
    color: 0x67e8f9,
    size: 0.065,
    transparent: true,
    opacity: 0.85,
    blending: THREE.AdditiveBlending
  });

  const particleRing = new THREE.Points(particleGeo, particleMat);
  scene.add(particleRing);

  // Secondary DNA Particle Helix
  const dnaCount = 120;
  const dnaGeo = new THREE.BufferGeometry();
  const dnaPos = new Float32Array(dnaCount * 3);
  for (let i = 0; i < dnaCount; i++) {
    const t = (i / dnaCount) * Math.PI * 4;
    const r = 1.8;
    dnaPos[i * 3] = Math.cos(t) * r;
    dnaPos[i * 3 + 1] = (i / dnaCount) * 4 - 2;
    dnaPos[i * 3 + 2] = Math.sin(t) * r;
  }
  dnaGeo.setAttribute('position', new THREE.BufferAttribute(dnaPos, 3));
  const dnaMat = new THREE.PointsMaterial({
    color: 0xa855f7,
    size: 0.075,
    transparent: true,
    opacity: 0.7,
    blending: THREE.AdditiveBlending
  });
  const dnaHelix = new THREE.Points(dnaGeo, dnaMat);
  scene.add(dnaHelix);

  // Lighting
  const ambientLight = new THREE.AmbientLight(0xffffff, 0.75);
  scene.add(ambientLight);

  const cyanLight = new THREE.PointLight(0x06b6d4, 2.5, 50);
  cyanLight.position.set(4, 5, 5);
  scene.add(cyanLight);

  const violetLight = new THREE.PointLight(0xa855f7, 2, 50);
  violetLight.position.set(-4, -4, 4);
  scene.add(violetLight);

  // Mouse Interaction Physics
  let targetRotationX = 0;
  let targetRotationY = 0;
  let mouseX = 0;
  let mouseY = 0;

  container.addEventListener("mousemove", e => {
    const rect = container.getBoundingClientRect();
    mouseX = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    mouseY = -(((e.clientY - rect.top) / rect.height) * 2 - 1);
    targetRotationY = mouseX * 1.2;
    targetRotationX = -mouseY * 0.9;
  });

  container.addEventListener("mouseleave", () => {
    targetRotationX = 0;
    targetRotationY = 0;
  });

  // Render loop
  let clock = new THREE.Clock();

  function animate() {
    requestAnimationFrame(animate);
    const elapsedTime = clock.getElapsedTime();

    // Smooth rotational floating
    crossGroup.rotation.y += 0.012;
    crossGroup.rotation.x = THREE.MathUtils.lerp(crossGroup.rotation.x, targetRotationX + Math.sin(elapsedTime * 1.5) * 0.1, 0.05);
    crossGroup.rotation.y = THREE.MathUtils.lerp(crossGroup.rotation.y, crossGroup.rotation.y + targetRotationY * 0.02, 0.05);
    crossGroup.position.y = Math.sin(elapsedTime * 2) * 0.18;

    // Orbit particles
    particleRing.rotation.y -= 0.008;
    particleRing.rotation.z = Math.sin(elapsedTime * 0.8) * 0.15;
    dnaHelix.rotation.y += 0.015;

    renderer.render(scene, camera);
  }
  animate();

  // Responsive Resizing
  window.addEventListener("resize", () => {
    const newWidth = container.clientWidth || 400;
    const newHeight = container.clientHeight || 320;
    camera.aspect = newWidth / newHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(newWidth, newHeight);
  });
}

document.addEventListener("DOMContentLoaded", initHero3D);
setTimeout(initHero3D, 200);
