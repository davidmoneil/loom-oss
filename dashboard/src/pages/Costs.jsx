import { useEffect, useState, useCallback } from "react";
import {
  BarChart,
  Bar,
  PieChart,
  Pie,
  Cell,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from "recharts";
import StatCard from "../components/StatCard.jsx";
import Chart, { CHART_COLORS, axisProps, tooltipStyle } from "../components/Chart.jsx";
import { Header } from "./Overview.jsx";
import { api, fmtNumber, fmtCost, fmtDateShort } from "../api.js";

const RANGES = [
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
  { label: "90d", days: 90 },
];

export default function Costs() {
  const [range, setRange] = useState(RANGES[1]);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api.costs(range.days);
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
  }, [load]);

  const totals = data?.totals || {};
  const byModel = (data?.by_model || []).map((m) => ({
    name: m.model,
    cost: m.cost_usd ?? 0,
    requests: m.requests ?? 0,
    savings: m.savings_usd ?? 0,
  }));
  const bySource = (data?.by_source || []).map((s) => ({
    name: s.source,
    value: s.cost_usd ?? 0,
  }));
  const byDay = (data?.by_day || []).map((d) => ({
    label: fmtDateShort(new Date(d.day).getTime() / 1000),
    cost: d.cost_usd ?? 0,
    requests: d.requests ?? 0,
  }));

  const totalSavings = byModel.reduce((sum, m) => sum + m.savings, 0);

  return (
    <div className="p-6">
      <Header title="Cost Analysis" updatedAt={updatedAt} error={error} onRefresh={load}>
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

      <div className="mt-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="Total Cost" value={fmtCost(totals.cost_usd)} loading={loading} />
        <StatCard label="Requests" value={fmtNumber(totals.requests)} loading={loading} />
        <StatCard
          label="Tokens In / Out"
          value={`${fmtNumber(totals.tokens_in)} / ${fmtNumber(totals.tokens_out)}`}
          loading={loading}
        />
        <StatCard
          label="Compression Savings"
          value={fmtCost(totalSavings)}
          sub={totals.tokens_saved ? `${fmtNumber(totals.tokens_saved)} tokens saved` : undefined}
          loading={loading}
        />
      </div>

      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <Chart title="Daily spend" loading={loading} empty={byDay.length === 0}>
            <BarChart data={byDay}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis dataKey="label" {...axisProps} />
              <YAxis {...axisProps} tickFormatter={(v) => `$${v}`} />
              <Tooltip {...tooltipStyle} formatter={(v, name) => [name === "cost" ? fmtCost(v) : fmtNumber(v)]} />
              <Bar dataKey="cost" name="Cost" fill="#3b82f6" radius={[3, 3, 0, 0]} />
            </BarChart>
          </Chart>
        </div>

        <Chart title="Cost by source" loading={loading} empty={bySource.length === 0}>
          <PieChart>
            <Pie
              data={bySource}
              dataKey="value"
              nameKey="name"
              cx="50%"
              cy="50%"
              innerRadius={55}
              outerRadius={85}
              paddingAngle={2}
            >
              {bySource.map((_, i) => (
                <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
              ))}
            </Pie>
            <Tooltip {...tooltipStyle} formatter={(v) => [fmtCost(v)]} />
            <Legend wrapperStyle={{ fontSize: 12, color: "#9ca3af" }} />
          </PieChart>
        </Chart>
      </div>

      <div className="mt-6 rounded-lg border border-border bg-card p-4">
        <h3 className="mb-3 text-sm font-semibold text-gray-200">Cost by model</h3>
        {loading ? (
          <div className="skeleton h-32 w-full" />
        ) : byModel.length === 0 ? (
          <div className="text-sm text-gray-500">No model cost data</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-border text-xs font-medium uppercase text-gray-400">
                  <th className="pb-2 pr-4">Model</th>
                  <th className="pb-2 pr-4 text-right">Requests</th>
                  <th className="pb-2 pr-4 text-right">Cost</th>
                  <th className="pb-2 text-right">Compression Savings</th>
                </tr>
              </thead>
              <tbody>
                {byModel.map((m) => (
                  <tr key={m.name} className="border-b border-border/50">
                    <td className="py-2 pr-4 font-mono text-white">{m.name}</td>
                    <td className="py-2 pr-4 text-right text-gray-300">{fmtNumber(m.requests)}</td>
                    <td className="py-2 pr-4 text-right text-gray-300">{fmtCost(m.cost)}</td>
                    <td className="py-2 text-right text-green-400">{m.savings > 0 ? fmtCost(m.savings) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
