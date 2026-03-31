// src/components/bots.js
// Bot cards + live market scanner + ETH liquidity monitor

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

// ── Live Scanner ───────────────────────────────────────
async function loadScanner() {
  try {
    const res  = await fetch('http://localhost:8000/api/trades/summary');
    const data = await res.json();

    // Fetch BTC price + scan markets for top edge
    const btcRes   = await fetch('http://localhost:8000/api/market/events?limit=1');
    const scanData = await fetchTopEdge();

    if (scanData) {
      document.getElementById('scanner-btc').textContent =
        `$${scanData.btc.toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
      document.getElementById('scanner-hours').textContent =
        `${scanData.hours.toFixed(1)}h`;

      const edgeEl = document.getElementById('scanner-top-edge');
      if (scanData.topEdge) {
        edgeEl.textContent = `${(scanData.topEdge * 100).toFixed(0)}¢`;
        edgeEl.className   = 'stat-val ' + (scanData.topEdge > 0.08 ? 'pnl-pos' : '');
      } else {
        edgeEl.textContent = '—';
        edgeEl.className   = 'stat-val';
      }

      const signalEl = document.getElementById('scanner-signal');
      if (scanData.signal) {
        const color = scanData.signal.startsWith('BUY') ? 'var(--green)' : 'var(--red)';
        signalEl.innerHTML =
          `<span style="color:${color}">${scanData.signal}</span>`;
      } else {
        signalEl.textContent = 'no edge found';
      }
    }
  } catch (err) {
    console.error('Scanner failed:', err);
  }
}

async function fetchTopEdge() {
  try {
    // Get BTC price and hours to settlement from a simple calculation
    const now        = new Date();
    const settlement = new Date(now);
    settlement.setUTCHours(21, 0, 0, 0);
    if (now >= settlement) settlement.setUTCDate(settlement.getUTCDate() + 1);
    const hours = (settlement - now) / 3_600_000;

    // Fetch BTC price from Kraken via a public endpoint
    const kraken = await fetch('https://api.kraken.com/0/public/Ticker?pair=XBTUSD');
    const kData  = await kraken.json();
    const btc    = parseFloat(kData.result?.XXBTZUSD?.c?.[0] || 0);

    if (!btc) return null;

    // Get today's markets
    // Use EDT (UTC-4) since Kalshi events are named by EDT date
    const edt   = new Date(now.getTime() - 4 * 60 * 60 * 1000);
    const year  = String(edt.getUTCFullYear()).slice(-2);
    const month = edt.toLocaleString('en-US', { month: 'short', timeZone: 'UTC' }).toUpperCase();
    const day   = String(edt.getUTCDate()).padStart(2, '0');
    const event = `KXBTCD-${year}${month}${day}17`;

    const mRes    = await fetch(
      `https://api.elections.kalshi.com/trade-api/v2/markets?limit=100&status=open&event_ticker=${event}`
    );
    const mData   = await mRes.json();
    const markets = mData.markets || [];

    // Find top edge using fair value model
    let topEdge = 0;
    let topSignal = null;

    for (const m of markets) {
      const bid = parseFloat(m.yes_bid_dollars || 0);
      const ask = parseFloat(m.yes_ask_dollars || 0);
      if (!bid && !ask) continue;
      const mid = (bid + ask) / 2;
      if (mid < 0.03 || mid > 0.97) continue;

      const threshold = parseFloat(m.ticker.split('-T').pop());
      if (isNaN(threshold)) continue;

      const fv      = fairValue(btc, threshold, hours);
      const buyEdge = fv - ask;
      const selEdge = bid - fv;

      if (buyEdge > topEdge) {
        topEdge   = buyEdge;
        topSignal = `BUY $${threshold.toLocaleString()} edge=${(buyEdge*100).toFixed(0)}¢`;
      }
      if (selEdge > topEdge) {
        topEdge   = selEdge;
        topSignal = `SELL $${threshold.toLocaleString()} edge=${(selEdge*100).toFixed(0)}¢`;
      }
    }

    return { btc, hours, topEdge: topEdge > 0.04 ? topEdge : null, signal: topSignal };
  } catch (err) {
    console.error('fetchTopEdge failed:', err);
    return null;
  }
}

function fairValue(btc, threshold, hours, vol = 0.56) {
  if (hours <= 0) return btc >= threshold ? 1 : 0;
  const distPct  = (btc - threshold) / threshold * 100;
  const totalVol = vol * Math.sqrt(hours);
  const z        = distPct / totalVol;
  return normalCDF(z);
}

function normalCDF(x) {
  const t    = 1 / (1 + 0.2316419 * Math.abs(x));
  const poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
    + t * (-1.821255978 + t * 1.330274429))));
  const p = 1 - (1 / Math.sqrt(2 * Math.PI)) * Math.exp(-x * x / 2) * poly;
  return x >= 0 ? p : 1 - p;
}

// ── ETH Liquidity Monitor ──────────────────────────────
async function loadEthMonitor() {
  try {
    const r       = await fetch(
      'https://api.elections.kalshi.com/trade-api/v2/markets?limit=100&status=open&series_ticker=KXETH'
    );
    const data    = await r.json();
    const markets = data.markets || [];

    const tradeable = markets.filter(m => {
      const bid = parseFloat(m.yes_bid_dollars || 0);
      const ask = parseFloat(m.yes_ask_dollars || 0);
      const mid = (bid + ask) / 2;
      return mid > 0.05 && mid < 0.95;
    });

    const avgVol = tradeable.length
      ? tradeable.reduce((s, m) => s + parseFloat(m.volume_24h_fp || 0), 0) / tradeable.length
      : 0;

    const marketsEl = document.getElementById('eth-markets');
    const volEl     = document.getElementById('eth-vol');
    const statusEl  = document.getElementById('eth-status');

    if (marketsEl) marketsEl.textContent = tradeable.length;
    if (volEl)     volEl.textContent     = avgVol.toFixed(0);
    if (statusEl) {
      const ready = tradeable.length >= 15 && avgVol >= 500;
      statusEl.textContent = ready ? 'ready' : 'watch';
      statusEl.style.color = ready ? 'var(--green)' : 'var(--text-dim)';
    }
  } catch (err) {
    console.error('ETH monitor failed:', err);
  }
}