// src/services/api.js
//
// All HTTP calls to the Python backend live here.
// One place to change the base URL, add auth headers, handle errors.
// Same pattern as an Angular service or a React API utility file.

const API_BASE = 'http://localhost:8000/api';

const api = {

  // ── Health ────────────────────────────────────────────

  async health() {
    const res = await fetch('http://localhost:8000/health');
    return res.ok;
  },

  // ── Trades ───────────────────────────────────────────

  async getTrades(strategy = null, limit = 50) {
    const params = new URLSearchParams({ limit });
    if (strategy && strategy !== 'all') params.append('strategy', strategy);
    const res = await fetch(`${API_BASE}/trades?${params}`);
    if (!res.ok) throw new Error(`Trades fetch failed: ${res.status}`);
    return res.json();
  },

  async getSummary() {
    const res = await fetch(`${API_BASE}/trades/summary`);
    if (!res.ok) throw new Error(`Summary fetch failed: ${res.status}`);
    return res.json();
  },

  // ── Bots ─────────────────────────────────────────────

  async getBots() {
    const res = await fetch(`${API_BASE}/bots`);
    if (!res.ok) throw new Error(`Bots fetch failed: ${res.status}`);
    return res.json();
  },

  async startBot(strategy, positionSize = 1.0) {
    const res = await fetch(`${API_BASE}/bots/start`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ strategy, position_size: positionSize }),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Failed to start bot');
    }
    return res.json();
  },

  async stopBot(strategy) {
    const res = await fetch(`${API_BASE}/bots/stop/${strategy}`, { method: 'POST' });
    if (!res.ok) throw new Error(`Failed to stop ${strategy} bot`);
    return res.json();
  },

  // ── Markets ───────────────────────────────────────────

  async getMarkets(limit = 15) {
    const res = await fetch(`${API_BASE}/market/events?limit=${limit}`);
    if (!res.ok) throw new Error(`Markets fetch failed: ${res.status}`);
    return res.json();
  },

  async getChartData() {
    const res = await fetch(`${API_BASE}/trades/chart`);
    if (!res.ok) throw new Error(`Chart fetch failed: ${res.status}`);
    return res.json();
  },
};
