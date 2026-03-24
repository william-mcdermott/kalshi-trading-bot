let pnlChart = null;

function buildChart(trades) {
  const sorted = [...trades]
    .filter(t => t.filled)
    .sort((a, b) => new Date(a.created_at) - new Date(b.created_at));

  let cumulative = 0;
  const labels = [];
  const data   = [];

  sorted.forEach(t => {
    cumulative += t.pnl;
    labels.push(new Date(t.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }));
    data.push(parseFloat(cumulative.toFixed(4)));
  });

  const netPositive = cumulative >= 0;
  const lineColor   = netPositive ? '#00e5a0' : '#ff4d6a';
  const fillColor   = netPositive ? 'rgba(0,229,160,0.08)' : 'rgba(255,77,106,0.08)';

  const ctx = document.getElementById('pnl-chart').getContext('2d');
  if (pnlChart) pnlChart.destroy();

  pnlChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data,
        borderColor:     lineColor,
        backgroundColor: fillColor,
        borderWidth:     1.5,
        pointRadius:     0,
        pointHoverRadius:3,
        tension:         0.3,
        fill:            true,
      }],
    },
    options: {
      responsive: true,
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
          callbacks: { label: ctx => ` $${ctx.parsed.y.toFixed(4)}` },
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
