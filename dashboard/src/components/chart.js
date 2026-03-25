let pnlChart = null;

async function loadChart(strategyFilter = 'all') {
  try {
    const data   = await api.getChartData();
    const points = strategyFilter === 'all'
      ? data.points
      : data.points; // strategy filter coming from trades endpoint already

    buildChart(points);
  } catch (e) {
    console.error('Chart load failed:', e);
  }
}

function buildChart(points) {
  const ctx = document.getElementById('pnl-chart').getContext('2d');

  if (!points || points.length === 0) {
    if (pnlChart) pnlChart.destroy();
    pnlChart = null;

    // Draw empty state message on canvas
    ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
    ctx.fillStyle    = '#505060';
    ctx.font         = '11px IBM Plex Mono';
    ctx.textAlign    = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(
      'no settled trades yet — chart will populate after first settlement',
      ctx.canvas.width / 2,
      ctx.canvas.height / 2,
    );
    return;
  }

  const labels     = points.map(p =>
    new Date(p.time).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
  );
  const values     = points.map(p => p.pnl);
  const finalPnl   = values[values.length - 1] ?? 0;
  const netPositive = finalPnl >= 0;
  const lineColor  = netPositive ? '#00e5a0' : '#ff4d6a';
  const fillColor  = netPositive ? 'rgba(0,229,160,0.08)' : 'rgba(255,77,106,0.08)';

  if (pnlChart) pnlChart.destroy();

  pnlChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data:            values,
        borderColor:     lineColor,
        backgroundColor: fillColor,
        borderWidth:     1.5,
        pointRadius:     0,
        pointHoverRadius: 3,
        tension:         0.3,
        fill:            true,
      }],
    },
    options: {
      responsive:          true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1a1a1f',
          borderColor:     '#2a2a35',
          borderWidth:     1,
          titleColor:      '#707088',
          bodyColor:       '#e8e8f0',
          titleFont:       { family: 'IBM Plex Mono', size: 10 },
          bodyFont:        { family: 'IBM Plex Mono', size: 11 },
          callbacks: {
            title: items => {
              const p = points[items[0].dataIndex];
              return new Date(p.time).toLocaleString('en-US', {
                month: 'short', day: 'numeric',
                hour: '2-digit', minute: '2-digit', hour12: false,
              });
            },
            label: item => {
              const p       = points[item.dataIndex];
              const sign    = p.trade_pnl >= 0 ? '+' : '';
              return [
                ` cumulative: $${item.parsed.y.toFixed(4)}`,
                ` trade:      ${sign}$${p.trade_pnl.toFixed(4)}  ${p.side} ${p.market.slice(-12)}`,
              ];
            },
          },
        },
      },
      scales: {
        x: {
          grid:   { color: '#2a2a35', drawBorder: false },
          ticks:  { color: '#505060', font: { family: 'IBM Plex Mono', size: 9 }, maxTicksLimit: 8 },
          border: { display: false },
        },
        y: {
          grid:   { color: '#2a2a35', drawBorder: false },
          ticks:  { color: '#505060', font: { family: 'IBM Plex Mono', size: 9 }, callback: v => `$${v.toFixed(2)}` },
          border: { display: false },
        },
      },
    },
  });
}