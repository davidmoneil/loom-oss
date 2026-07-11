import { useEffect, useState, useCallback } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from "recharts";
import StatCard from "../components/StatCard.jsx";
import Chart, { axisProps, tooltipStyle } from "../components/Chart.jsx";
import { Header } from "./Overview.jsx";
import { api, fmtTime, fmtTimeShort, fmtDateShort } from "../api.js";

const RANGES = [
  { label: "24h", hours: 24 },
  { label: "48h", hours: 48 },
  { label: "7d", hours: 168 },
];

const STATUS_COLORS = {
  ok: "text-green-400",
  warning: "text-yellow-400",
  critical: "text-red-400",
  exhausted: "text-purple-400",
};

function statusBadge(status) {
  const color = STATUS_COLORS[status] ?? "text-gray-400";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full bg-gray-800 px-2.5 py-1 text-xs font-semibold ${color}`}>
      <span className={`h-2 w-2 rounded-full ${color.replace("text-", "bg-")}`} />
      {status ?? "unknown"}
    </span>
  );
}

function pctBar(value, label, color = "#3b82f6") {
  const pct = Math.min(100, Math.max(0, (value ?? 0) * 100));
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs">
        <span className="text-gray-400">{label}</span>
        <span className="font-mono text-white">{pct.toFixed(1)}%</span>
      </div>
      <div className="h-2 w-full rounded-full bg-gray-700">
        <div
          className="h-full rounded-full transition-all"
          style={{ width: `${pct}%`, backgroundColor: pct > 80 ? "#ef4444" : pct > 60 ? "#f59e0b" : color }}
        />
      </div>
    </div>
  );
}

export default function RateLimits() {
  const [range, setRange] = useState(RANGES[1]);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api.rateLimits(range.hours);
      setData(d);
      setUpdatedAt(new Date());
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [range]);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  const current = data?.current;
  const trend = (data?.trend || []).map((t) => ({
    label: range.hours <= 48 ? fmtTimeShort(new Date(t.hour).getTime() / 1000) : fmtDateShort(new Date(t.hour).getTime() / 1000),
    avg_5h: t.avg_5h != null ? +(t.avg_5h * 100).toFixed(1) : null,
    avg_7d: t.avg_7d != null ? +(t.avg_7d * 100).toFixed(1) : null,
    max_5h: t.max_5h != null ? +(t.max_5h * 100).toFixed(1) : null,
    samples: t.samples,
  }));

  return (
    <div className="p-6">
      <Header title="Rate Limits" updatedAt={updatedAt} error={error} onRefresh={load}>
        <div className="flex gap-1 rounded-md border border-border bg-card p-0.5">
          {RANGES.map((r) => (
            <button
              key={r.label}
              onClick={() => setRange(r)}
              className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
                r.label === range.label
                  ? "bg-accent text-white"
                  : "text-gray-400 hover:text-white"
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>
      </Header>

      {!loading && !current && !error && (
        <div className="mt-6 rounded-lg border border-border bg-card p-8 text-center text-sm text-gray-400">
          No rate-limit data recorded yet. Data appears after the gateway proxies requests to a provider that returns utilization headers.
        </div>
      )}

      {current && (
        <>
          <div className="mt-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
            <StatCard
              label="Current Status"
              value={current.unified_status ?? "—"}
              loading={loading}
            />
            <StatCard
              label="Retry After"
              value={current.retry_after ? `${current.retry_after}s` : "None"}
              loading={loading}
            />
            <StatCard
              label="Model"
              value={current.model ?? "—"}
              sub={current.source}
              loading={loading}
            />
            <StatCard
              label="Last Seen"
              value={fmtTime(current.timestamp)}
              loading={loading}
            />
          </div>

          <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div className="rounded-lg border border-border bg-card p-4">
              <h3 className="mb-4 text-sm font-semibold text-gray-200">Current Utilization</h3>
              <div className="space-y-3">
                {pctBar(current.util_5h, "5-hour window")}
                {pctBar(current.util_7d, "7-day window", "#10b981")}
              </div>
              <div className="mt-4 flex items-center gap-3">
                <span className="text-xs text-gray-400">Status 5h:</span>
                {statusBadge(current.status_5h)}
                <span className="text-xs text-gray-400">Status 7d:</span>
                {statusBadge(current.status_7d)}
              </div>
            </div>

            <div className="rounded-lg border border-border bg-card p-4">
              <h3 className="mb-4 text-sm font-semibold text-gray-200">Per-Model Utilization</h3>
              {current.model_util_7d != null ? (
                <div className="space-y-3">
                  {pctBar(current.model_util_7d, current.model_name_7d || current.model, "#8b5cf6")}
                  <div className="flex items-center gap-3">
                    <span className="text-xs text-gray-400">Model status:</span>
                    {statusBadge(current.model_status_7d)}
                  </div>
                </div>
              ) : (
                <div className="text-sm text-gray-500">No per-model data available</div>
              )}
            </div>
          </div>
        </>
      )}

      <div className="mt-6">
        <Chart title={`Utilization trend (${range.label})`} loading={loading} empty={trend.length === 0}>
          <AreaChart data={trend}>
            <defs>
              <linearGradient id="rl5h" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.4} />
                <stop offset="100%" stopColor="#3b82f6" stopOpacity={0} />
              </linearGradient>
              <linearGradient id="rl7d" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#10b981" stopOpacity={0.4} />
                <stop offset="100%" stopColor="#10b981" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="label" {...axisProps} />
            <YAxis domain={[0, 100]} unit="%" {...axisProps} />
            <Tooltip
              {...tooltipStyle}
              formatter={(v) => [`${v}%`]}
            />
            <Area type="monotone" dataKey="avg_5h" name="Avg 5h" stroke="#3b82f6" fill="url(#rl5h)" strokeWidth={2} />
            <Area type="monotone" dataKey="avg_7d" name="Avg 7d" stroke="#10b981" fill="url(#rl7d)" strokeWidth={2} />
            <Area type="monotone" dataKey="max_5h" name="Peak 5h" stroke="#ef4444" fill="none" strokeWidth={1} strokeDasharray="4 4" />
          </AreaChart>
        </Chart>
      </div>
    </div>
  );
}
