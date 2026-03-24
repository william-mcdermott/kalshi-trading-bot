async function loadMarkets() {
  const list = document.getElementById('market-list');
  try {
    const events  = await api.getMarkets(15);
    const markets = events.flatMap(e =>
      (e.markets || []).map(m => ({ ...m, event_title: e.title }))
    ).slice(0, 20);

    if (!markets.length) {
      list.innerHTML = `<div class="market-loading">no active markets</div>`;
      return;
    }
    list.innerHTML = markets.map(m => {
      const yes = parseFloat(m.yes_price ?? 0);
      const no  = parseFloat(m.no_price  ?? 0);
      const vol = formatVolume(m.volume);
      const q   = m.question || m.event_title || 'Unknown market';
      return `<div class="market-item">
        <div class="market-question">${q}</div>
        <div class="market-prices">
          <span class="price-pill price-yes">YES ${(yes * 100).toFixed(0)}¢</span>
          <span class="price-pill price-no">NO ${(no * 100).toFixed(0)}¢</span>
          <span class="market-vol">${vol}</span>
        </div>
      </div>`;
    }).join('');
  } catch (err) {
    list.innerHTML = `<div class="market-loading" style="color:var(--red)">failed to load markets</div>`;
  }
}

function formatVolume(v) {
  if (!v) return '';
  const n = parseFloat(v);
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000)     return `$${(n / 1_000).toFixed(0)}K`;
  return `$${n.toFixed(0)}`;
}
