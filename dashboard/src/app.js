// src/app.js
// App entry point — wires everything together on page load

// ── Clock ──────────────────────────────────────────────
function updateClock() {
  document.getElementById('clock').textContent =
    new Date().toLocaleTimeString('en-US', { hour12: false });
}
setInterval(updateClock, 1000);
updateClock();

// ── Server health check ────────────────────────────────
async function checkServer() {
  const dot    = document.getElementById('server-dot');
  const status = document.getElementById('server-status');
  try {
    const ok = await api.health();
    dot.className      = ok ? 'dot dot--live' : 'dot dot--error';
    status.textContent = ok ? 'connected' : 'backend error';
  } catch {
    dot.className      = 'dot dot--error';
    status.textContent = 'offline';
  }
}

// ── Load everything ────────────────────────────────────
async function loadAll() {
  await Promise.allSettled([
    checkServer(),
    loadTrades(currentFilter ?? 'all'),
    loadBots(),
    loadMarkets(),
  ]);
}

// ── Auto-refresh every 30s ─────────────────────────────
// Keeps the dashboard live without manual refreshes
setInterval(loadAll, 30_000);

// ── Init on page load ──────────────────────────────────
document.addEventListener('DOMContentLoaded', loadAll);
