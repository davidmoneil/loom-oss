// Thin fetch wrappers around the Loom gateway API. All endpoints are served
// from the same origin as the dashboard, so relative paths just work.

async function getJSON(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

export const api = {
  metrics: () => getJSON("/api/metrics"),
  timeseries: (hours = 24, bucket = "1h") =>
    getJSON(`/api/metrics/timeseries?hours=${hours}&bucket=${bucket}`),
  models: () => getJSON("/api/models"),
  config: () => getJSON("/api/config"),
  health: () => getJSON("/health"),
  audit: (params = {}) => {
    const q = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== "" && v !== null && v !== undefined) q.set(k, v);
    });
    return getJSON(`/api/audit?${q.toString()}`);
  },
};

export function fmtNumber(n) {
  if (n === null || n === undefined) return "—";
  return Number(n).toLocaleString();
}

export function fmtCost(n) {
  if (!n) return "$0.00";
  if (n < 0.01) return `$${Number(n).toFixed(4)}`;
  return `$${Number(n).toFixed(2)}`;
}

export function fmtLatency(ms) {
  if (!ms) return "—";
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${Math.round(ms)}ms`;
}

export function fmtTime(epochSeconds) {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString();
}
