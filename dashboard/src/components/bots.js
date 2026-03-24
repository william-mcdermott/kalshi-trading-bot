async function loadBots() {
  try {
    const bots = await api.getBots();
    bots.forEach(bot => updateBotCard(bot));
    const active = bots.filter(b => b.is_running).length;
    document.getElementById('kpi-active').textContent = active;
  } catch (err) {
    console.error('Failed to load bots:', err);
  }
}

function updateBotCard(bot) {
  const { strategy, is_running, total_trades, total_pnl } = bot;
  const card     = document.getElementById(`bot-${strategy}`);
  const toggle   = document.getElementById(`toggle-${strategy}`);
  const tradesEl = document.getElementById(`${strategy}-trades`);
  const pnlEl    = document.getElementById(`${strategy}-pnl`);
  const statusEl = document.getElementById(`${strategy}-status`);
  if (!card) return;
  card.classList.toggle('active', is_running);
  if (toggle) toggle.checked = is_running;
  if (tradesEl) tradesEl.textContent = total_trades ?? '—';
  if (pnlEl) {
    const pnl = total_pnl ?? 0;
    pnlEl.textContent = `$${pnl.toFixed(3)}`;
    pnlEl.className   = 'stat-val ' + (pnl > 0 ? 'pnl-pos' : pnl < 0 ? 'pnl-neg' : '');
  }
  if (statusEl) {
    statusEl.textContent = is_running ? 'live' : 'offline';
    statusEl.style.color = is_running ? 'var(--green)' : 'var(--text-dim)';
  }
}

async function toggleBot(strategy, shouldStart) {
  const sizeInput = document.getElementById(`size-${strategy}`);
  const size = parseFloat(sizeInput?.value || 1);
  try {
    if (shouldStart) {
      await api.startBot(strategy, size);
      showToast(`${strategy.toUpperCase()} bot started ($${size} size)`, 'success');
    } else {
      await api.stopBot(strategy);
      showToast(`${strategy.toUpperCase()} bot stopped`, 'success');
    }
    await loadBots();
  } catch (err) {
    showToast(err.message, 'error');
    const toggle = document.getElementById(`toggle-${strategy}`);
    if (toggle) toggle.checked = !shouldStart;
  }
}
