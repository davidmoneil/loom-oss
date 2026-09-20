// Thin fetch wrappers around the Loom gateway API. All endpoints are served
// from the same origin as the dashboard, so relative paths just work.

// Gateway key for the browser session. The API requires it on every /api/*
// call once at least one key exists; stored locally so a page reload keeps it.
const KEY_STORAGE = "loom-gateway-key";

export function getGatewayKey() {
  try {
    return localStorage.getItem(KEY_STORAGE) || "";
  } catch {
    return "";
  }
}

export function setGatewayKey(key) {
  try {
    if (key) localStorage.setItem(KEY_STORAGE, key);
    else localStorage.removeItem(KEY_STORAGE);
  } catch {
    // Private-mode browsers may block storage; the key just won't persist.
  }
}

function authHeaders() {
  const key = getGatewayKey();
  return key ? { "x-loom-gateway-key": key } : {};
}

function fail(path, res) {
  const err = new Error(
    res.status === 401
      ? "Authentication required — set your gateway key in Settings."
      : `${path} -> ${res.status}`
  );
  err.status = res.status;
  return err;
}

async function getJSON(path) {
  const res = await fetch(path, {
    headers: { Accept: "application/json", ...authHeaders() },
  });
  if (!res.ok) throw fail(path, res);
  return res.json();
}

async function sendJSON(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...authHeaders(),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw fail(path, res);
  return res.json();
}

const patchJSON = (path, body) => sendJSON("PATCH", path, body);
const putJSON = (path, body) => sendJSON("PUT", path, body);
const postJSON = (path, body) => sendJSON("POST", path, body);

async function deleteJSON(path) {
  const res = await fetch(path, {
    method: "DELETE",
    headers: { Accept: "application/json", ...authHeaders() },
  });
  if (!res.ok) throw fail(path, res);
  return res.json();
}

export const api = {
  metrics: (hours = 24) => getJSON(`/api/metrics?hours=${hours}`),
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
  auditContent: (requestId) => getJSON(`/api/audit/${requestId}/content`),
  scannerRules: () => getJSON("/api/scanner/rules"),
  scannerStats: () => getJSON("/api/scanner/stats"),
  updateScannerRule: (name, updates) =>
    putJSON(`/api/scanner/rules/${encodeURIComponent(name)}`, updates),
  governorSettings: () => getJSON("/api/governor"),
  governorStatus: () => getJSON("/api/governor/status"),
  updateGovernor: (updates) => patchJSON("/api/governor", updates),
  deleteGovernorOverride: (job) =>
    deleteJSON(`/api/governor/class-overrides/${encodeURIComponent(job)}`),
  rateLimits: (hours = 48) => getJSON(`/api/rate-limits?hours=${hours}`),
  routing: (hours = 24, limit = 200) =>
    getJSON(`/api/routing?hours=${hours}&limit=${limit}`),
  routingTable: () => getJSON("/api/routing/table"),
  sessions: (hours = 24) => getJSON(`/api/sessions?hours=${hours}`),
  costs: (days = 30) => getJSON(`/api/costs?days=${days}`),
  compressionMetrics: (days = 30) => getJSON(`/api/metrics/compression?days=${days}`),
  updateServerConfig: (updates) => patchJSON("/api/config/server", updates),
  updateCompression: (updates) => patchJSON("/api/config/compression", updates),
  updateSourcePolicy: (name, updates) =>
    patchJSON(`/api/config/sources/${encodeURIComponent(name)}`, updates),
  createSourcePolicy: (name, fields) =>
    putJSON(`/api/config/sources/${encodeURIComponent(name)}`, fields),
  deleteSourcePolicy: (name) =>
    deleteJSON(`/api/config/sources/${encodeURIComponent(name)}`),
  createModel: (provider, modelId, fields) =>
    postJSON(
      `/api/config/providers/${encodeURIComponent(provider)}/models/${encodeURIComponent(modelId)}`,
      fields
    ),
  updateModel: (provider, modelId, updates) =>
    putJSON(
      `/api/config/providers/${encodeURIComponent(provider)}/models/${encodeURIComponent(modelId)}`,
      updates
    ),
  deleteModel: (provider, modelId) =>
    deleteJSON(
      `/api/config/providers/${encodeURIComponent(provider)}/models/${encodeURIComponent(modelId)}`
    ),
  gatewayKeys: () => getJSON("/api/config/gateway-keys"),
  createGatewayKey: (name) => postJSON("/api/config/gateway-keys", { name }),
  toggleGatewayKey: (id, enabled) =>
    patchJSON(`/api/config/gateway-keys/${id}`, { enabled }),
  deleteGatewayKey: (id) => deleteJSON(`/api/config/gateway-keys/${id}`),
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

// Chart axis labels for a timeseries bucket. Bucket granularity decides how
// much of the timestamp is worth showing: a 1-day bucket has one point per
// day (date only, time is meaningless); a 1-hour bucket only ever spans a
// single day in practice (time only); anything wider — e.g. the 6h buckets
// used for a 7-day range — needs both, since ticks land on different days.
export function fmtBucketLabel(epochSeconds, bucketSeconds) {
  if (!epochSeconds) return "—";
  const ONE_HOUR = 3600;
  const ONE_DAY = 86400;
  if (bucketSeconds >= ONE_DAY) return fmtDateShort(epochSeconds);
  if (bucketSeconds <= ONE_HOUR) return fmtTimeShort(epochSeconds);
  return `${fmtDateShort(epochSeconds)} ${fmtTimeShort(epochSeconds)}`;
}

// Bucket sizes the server accepts for /api/metrics/timeseries (_BUCKET_SIZES
// in gateway/app.py), smallest first.
const BUCKET_SECONDS = [300, 900, 3600, 21600, 86400];
const BUCKET_PARAM_BY_SECONDS = { 300: "5m", 900: "15m", 3600: "1h", 21600: "6h", 86400: "1d" };

// Picks a bucket size for an arbitrary "last N hours" window: the smallest
// available bucket that keeps the chart under ~50 points, falling back to
// the coarsest bucket (1d) once even that's too fine-grained for the
// window. Lets the time-range control accept any relative window (e.g.
// "last 8 hours") instead of only a fixed list of presets, while keeping
// charts readable and requests cheap at any window size.
export function pickBucket(hours) {
  const totalSeconds = Math.max(Number(hours) || 0, 0) * 3600;
  const seconds =
    BUCKET_SECONDS.find((b) => totalSeconds / b <= 50) ||
    BUCKET_SECONDS[BUCKET_SECONDS.length - 1];
  return { bucket: BUCKET_PARAM_BY_SECONDS[seconds], bucketSeconds: seconds };
}
