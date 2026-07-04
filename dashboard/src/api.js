// Thin fetch wrappers around the Loom gateway API. All endpoints are served
// from the same origin as the dashboard, so relative paths just work.

async function getJSON(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

async function patchJSON(path, body) {
  const res = await fetch(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

async function putJSON(path, body) {
  const res = await fetch(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

async function deleteJSON(path) {
  const res = await fetch(path, { method: "DELETE", headers: { Accept: "application/json" } });
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
  scannerRules: () => getJSON("/api/scanner/rules"),
  scannerStats: () => getJSON("/api/scanner/stats"),
  governorSettings: () => getJSON("/api/governor"),
  governorStatus: () => getJSON("/api/governor/status"),
  updateGovernor: (updates) => patchJSON("/api/governor", updates),
  deleteGovernorOverride: (job) =>
    deleteJSON(`/api/governor/class-overrides/${encodeURIComponent(job)}`),
  sessions: (hours = 24) => getJSON(`/api/sessions?hours=${hours}`),
  costs: (days = 30) => getJSON(`/api/costs?days=${days}`),
  updateSourcePolicy: (name, updates) =>
    patchJSON(`/api/config/sources/${encodeURIComponent(name)}`, updates),
  createSourcePolicy: (name, fields) =>
    putJSON(`/api/config/sources/${encodeURIComponent(name)}`, fields),
  deleteSourcePolicy: (name) =>
    deleteJSON(`/api/config/sources/${encodeURIComponent(name)}`),
};

// Display timezone — loaded from server config, cached in module state.
// Defaults to UTC until the config is fetched.
let _displayTimezone = "UTC";

export function setDisplayTimezone(tz) {
  _displayTimezone = tz || "UTC";
}

export function getDisplayTimezone() {
  return _displayTimezone;
}

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
  try {
    return new Date(epochSeconds * 1000).toLocaleString(undefined, {
      timeZone: _displayTimezone,
    });
  } catch {
    return new Date(epochSeconds * 1000).toLocaleString();
  }
}

export function fmtTimeShort(epochSeconds) {
  if (!epochSeconds) return "—";
  try {
    return new Date(epochSeconds * 1000).toLocaleTimeString(undefined, {
      timeZone: _displayTimezone,
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return new Date(epochSeconds * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  }
}

export function fmtDateShort(epochSeconds) {
  if (!epochSeconds) return "—";
  try {
    return new Date(epochSeconds * 1000).toLocaleDateString(undefined, {
      timeZone: _displayTimezone,
      month: "short",
      day: "numeric",
    });
  } catch {
    return new Date(epochSeconds * 1000).toLocaleDateString([], {
      month: "short",
      day: "numeric",
    });
  }
}
