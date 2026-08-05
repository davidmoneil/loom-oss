import { useEffect, useState, useCallback } from "react";
import {
  BarChart,
  Bar,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from "recharts";
import StatCard from "../components/StatCard.jsx";
import Chart, { CHART_COLORS, axisProps, tooltipStyle } from "../components/Chart.jsx";
import { Header } from "./Overview.jsx";
import { api, fmtNumber, fmtCost } from "../api.js";

const RANGES = [
  { label: "24h", days: 1 },
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
];

export default function Compression() {
  const [range, setRange] = useState(RANGES[1]);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const summary = await api.compressionMetrics(range.days);
      setData(summary);
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
  }, [load]);

  const totals = data?.totals || {};
  const pctCompressed =
    totals.requests > 0
      ? `${((totals.compressed_requests / totals.requests) * 100).toFixed(0)}%`
      : "—";

  const histogram = (data?.ratio_histogram || []).map((b) => ({
    range: b.range,
    count: b.count,
  }));
  const hasHistogram = histogram.some((b) => b.count > 0);

  const byDay = (data?.by_day || []).map((d) => ({
    date: d.day.slice(5),
    saved: d.tokens_saved,
    requests: d.requests,
    compressed: d.compressed_requests,
  }));

  const byTier = (data?.by_tier || []).filter((t) => t.requests > 0);
  const byModel = (data?.by_model || [])
    .filter((m) => m.tokens_saved > 0)
    .map((m) => ({
      name: m.model.replace(/^claude-/, ""),
      saved: m.tokens_saved,
      usd: m.est_savings_usd,
    }));
  const bySource = (data?.by_source || []).filter((s) => s.requests > 0);

  return (
    <div className="p-6">
      <Header
        title="Compression"
        updatedAt={updatedAt}
        error={error}
        onRefresh={load}
      >
        <div className="flex overflow-hidden rounded-md border border-border">
          {RANGES.map((r) => (
            <button
              key={r.label}
              onClick={() => setRange(r)}
              className={`px-3 py-1.5 text-sm ${
                r.label === range.label
                  ? "bg-accent text-white"
                  : "bg-card text-gray-400 hover:bg-gray-700/50"
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>
      </Header>

      <div className="mt-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard
          label="Tokens Saved"
          value={fmtNumber(totals.tokens_saved)}
          loading={loading}
        />
        <StatCard
          label="Est. Savings"
          value={fmtCost(totals.est_savings_usd)}
          loading={loading}
        />
        <StatCard
          label="Mean Savings"
          value={
            totals.compressed_requests > 0 ? `${totals.mean_savings_pct}%` : "—"
          }
          sub={
            totals.compressed_requests > 0
              ? `median ${totals.median_savings_pct}% · σ ${totals.stdev_savings_pct}%`
              : undefined
          }
          loading={loading}
        />
        <StatCard
          label="Requests Compressed"
          value={pctCompressed}
          sub={
            totals.requests > 0
              ? `${fmtNumber(totals.compressed_requests)} of ${fmtNumber(totals.requests)}`
              : undefined
          }
          loading={loading}
        />
      </div>

      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Chart
          title="Tokens saved by day"
          loading={loading}
          empty={byDay.length === 0}
        >
          <AreaChart data={byDay}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="date" {...axisProps} />
            <YAxis {...axisProps} />
            <Tooltip {...tooltipStyle} formatter={(v) => fmtNumber(v)} />
            <Area
              type="monotone"
              dataKey="saved"
              stroke="#10b981"
              fill="#10b981"
              fillOpacity={0.3}
              strokeWidth={2}
            />
          </AreaChart>
        </Chart>

        <Chart
          title="Savings distribution (per request)"
          loading={loading}
          empty={!hasHistogram}
        >
          <BarChart data={histogram}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="range" {...axisProps} />
            <YAxis allowDecimals={false} {...axisProps} />
            <Tooltip
              {...tooltipStyle}
              formatter={(v) => [`${v} requests`, "count"]}
            />
            <Bar dataKey="count" fill="#3b82f6" radius={[3, 3, 0, 0]} />
          </BarChart>
        </Chart>

        <Chart
          title="Tokens saved by model"
          loading={loading}
          empty={byModel.length === 0}
        >
          <BarChart data={byModel} layout="vertical">
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis type="number" {...axisProps} />
            <YAxis type="category" dataKey="name" width={140} {...axisProps} />
            <Tooltip
              {...tooltipStyle}
              formatter={(v, name) =>
                name === "usd" ? fmtCost(v) : fmtNumber(v)
              }
            />
            <Bar dataKey="saved" fill="#8b5cf6" radius={[0, 3, 3, 0]} />
          </BarChart>
        </Chart>

        <Chart
          title="Compressed vs total requests"
          loading={loading}
          empty={byDay.length === 0}
        >
          <BarChart data={byDay}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="date" {...axisProps} />
            <YAxis allowDecimals={false} {...axisProps} />
            <Tooltip {...tooltipStyle} />
            <Legend wrapperStyle={{ fontSize: 12, color: "#9ca3af" }} />
            <Bar dataKey="requests" fill="#374151" radius={[3, 3, 0, 0]} />
            <Bar dataKey="compressed" fill="#06b6d4" radius={[3, 3, 0, 0]} />
          </BarChart>
        </Chart>
      </div>

      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <BreakdownTable
          title="By tier"
          rows={byTier}
          labelKey="tier"
          loading={loading}
        />
        <BreakdownTable
          title="By source"
          rows={bySource}
          labelKey="source"
          loading={loading}
        />
      </div>
    </div>
  );
}

function BreakdownTable({ title, rows, labelKey, loading }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h3 className="mb-3 text-sm font-semibold text-gray-200">{title}</h3>
      {loading ? (
        <div className="skeleton" style={{ height: 120 }} />
      ) : rows.length === 0 ? (
        <div className="flex h-24 items-center justify-center text-sm text-gray-500">
          No data yet
        </div>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs uppercase text-gray-500">
              <th className="pb-2">{labelKey}</th>
              <th className="pb-2 text-right">Requests</th>
              <th className="pb-2 text-right">Compressed</th>
              <th className="pb-2 text-right">Tokens Saved</th>
              <th className="pb-2 text-right">Mean Savings</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r[labelKey]} className="border-b border-border/40">
                <td className="py-2">
                  <span
                    className="mr-2 inline-block h-2 w-2 rounded-full"
                    style={{
                      backgroundColor: CHART_COLORS[i % CHART_COLORS.length],
                    }}
                  />
                  <span className="text-white">{r[labelKey]}</span>
                </td>
                <td className="py-2 text-right text-gray-300">
                  {fmtNumber(r.requests)}
                </td>
                <td className="py-2 text-right text-gray-300">
                  {fmtNumber(r.compressed_requests)}
                </td>
                <td className="py-2 text-right text-gray-300">
                  {fmtNumber(r.tokens_saved)}
                </td>
                <td className="py-2 text-right text-gray-300">
                  {r.compressed_requests > 0 ? `${r.mean_savings_pct}%` : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
