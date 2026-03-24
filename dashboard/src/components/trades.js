let currentFilter = 'all';

async function loadTrades(strategy = 'all') {
  currentFilter = strategy;
  const tbody = document.getElementById('trade-tbody');
  tbody.innerHTML = `<tr><td colspan="8" class="loading-row">loading...</td></tr>`;
  try {
    const [trades, summary] = await Promise.all([
      api.getTrades(strategy),
      api.getSummary(),
    ]);
    renderTradeTable(trades);
    renderSummary(summary);
    buildChart(trades);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="8" class="loading-row" style="color:var(--red)">failed to load — is the backend running?</td></tr>`;
    console.error(err);
  }
}

function renderTradeTable(trades) {
  const tbody = document.getElementById('trade-tbody');
  if (!trades.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="loading-row">no trades yet</td></tr>`;
    return;
  }
  tbody.innerHTML = trades.map(t => {
    const time     = new Date(t.created_at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    const pnlClass = t.pnl > 0 ? 'pnl-pos' : t.pnl < 0 ? 'pnl-neg' : 'pnl-zero';
    const pnlStr   = t.pnl > 0 ? `+$${t.pnl.toFixed(4)}` : `$${t.pnl.toFixed(4)}`;
    const marketShort = t.market_id.replace('market_', '').substring(0, 10);
    return `<tr>
      <td>${time}</td>
      <td><span style="color:var(--amber);font-family:var(--font-mono)">${t.strategy}</span></td>
      <td style="color:var(--text-dim)">${marketShort}…</td>
      <td><span class="badge badge-${t.side.toLowerCase()}">${t.side}</span></td>
      <td>${t.price.toFixed(3)}</td>
      <td>$${t.size.toFixed(2)}</td>
      <td class="${t.filled ? 'filled-yes' : 'filled-no'}">${t.filled ? '✓' : '○'}</td>
      <td class="${pnlClass}">${t.filled ? pnlStr : '—'}</td>
    </tr>`;
  }).join('');
}

function renderSummary(summary) {
  const pnlEl = document.getElementById('kpi-pnl');
  const pnl   = summary.total_pnl ?? 0;
  pnlEl.textContent = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(4);
  pnlEl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
  document.getElementById('kpi-trades').textContent  = summary.total_trades ?? 0;
  document.getElementById('kpi-winrate').textContent = `${summary.win_rate ?? 0}%`;
}

function setStrategyFilter(strategy, el) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  loadTrades(strategy);
}
