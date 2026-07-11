import { useEffect, useState, useCallback, useMemo } from "react";
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
import { api, fmtNumber, fmtTime } from "../api.js";

const RANGES = [
  { label: "1h", hours: 1 },
  { label: "6h", hours: 6 },
  { label: "24h", hours: 24 },
];

export default function Routing() {
  const [range, setRange] = useState(RANGES[2]);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api.routing(range.hours);
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

  const entries = data?.entries || [];
  const byReason = data?.by_reason || {};

  const reasonData = useMemo(
    () => Object.entries(byReason).map(([name, value]) => ({ name, value })).sort((a, b) => b.value - a.value),
    [byReason]
  );

  const modelCounts = useMemo(() => {
    const counts = {};
    entries.forEach((e) => {
      counts[e.model_used] = (counts[e.model_used] || 0) + 1;
    });
    return Object.entries(counts)
      .map(([name, value]) => ({ name, value }))
      .sort((a, b) => b.value - a.value);
  }, [entries]);

  const overridePct = data?.total > 0 ? ((data.overrides / data.total) * 100).toFixed(1) : "0";

  return (
    <div className="p-6">
      <Header title="Routing Decisions" updatedAt={updatedAt} error={error} onRefresh={load}>
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

      {!data?.available && !loading && (
        <div className="mt-6 rounded-lg border border-border bg-card p-8 text-center text-sm text-gray-400">
          Routing data requires storage to be enabled in the gateway config.
        </div>
      )}

      {data?.available && (
        <>
          <div className="mt-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
            <StatCard label="Total Decisions" value={fmtNumber(data.total)} loading={loading} />
            <StatCard label="Unique Models" value={fmtNumber(modelCounts.length)} loading={loading} />
            <StatCard label="Overrides" value={fmtNumber(data.overrides)} sub={`${overridePct}% of decisions`} loading={loading} />
            <StatCard label="Routing Reasons" value={fmtNumber(reasonData.length)} loading={loading} />
          </div>

          <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Chart title="Decisions by model" loading={loading} empty={modelCounts.length === 0}>
              <PieChart>
                <Pie
                  data={modelCounts}
                  dataKey="value"
                  nameKey="name"
                  cx="50%"
                  cy="50%"
                  innerRadius={55}
                  outerRadius={85}
                  paddingAngle={2}
                >
                  {modelCounts.map((_, i) => (
                    <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip {...tooltipStyle} />
                <Legend wrapperStyle={{ fontSize: 12, color: "#9ca3af" }} />
              </PieChart>
            </Chart>

            <Chart title="Routing reasons" loading={loading} empty={reasonData.length === 0}>
              <BarChart data={reasonData} layout="vertical">
                <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                <XAxis type="number" {...axisProps} allowDecimals={false} />
                <YAxis type="category" dataKey="name" {...axisProps} width={120} />
                <Tooltip {...tooltipStyle} />
                <Bar dataKey="value" name="Count" fill="#8b5cf6" radius={[0, 3, 3, 0]} />
              </BarChart>
            </Chart>
          </div>

          <div className="mt-6 rounded-lg border border-border bg-card p-4">
            <h3 className="mb-3 text-sm font-semibold text-gray-200">Recent decisions</h3>
            {loading ? (
              <div className="skeleton h-48 w-full" />
            ) : entries.length === 0 ? (
              <div className="text-sm text-gray-500">No routing decisions in this window</div>
            ) : (
              <div className="max-h-96 overflow-y-auto">
                <table className="w-full text-left text-sm">
                  <thead className="sticky top-0 bg-card">
                    <tr className="border-b border-border text-xs font-medium uppercase text-gray-400">
                      <th className="pb-2 pr-3">Time</th>
                      <th className="pb-2 pr-3">Source</th>
                      <th className="pb-2 pr-3">Task</th>
                      <th className="pb-2 pr-3">Recommended</th>
                      <th className="pb-2 pr-3">Used</th>
                      <th className="pb-2 pr-3">Reason</th>
                      <th className="pb-2 text-right">Determinism</th>
                    </tr>
                  </thead>
                  <tbody>
                    {entries.slice(0, 100).map((d, i) => {
                      const overridden = d.model_recommended && d.model_used && d.model_recommended !== d.model_used;
                      return (
                        <tr key={d.request_id || i} className="border-b border-border/50">
                          <td className="py-2 pr-3 text-xs text-gray-400">{fmtTime(d.timestamp)}</td>
                          <td className="py-2 pr-3 font-mono text-xs text-gray-300">{d.source}</td>
                          <td className="py-2 pr-3 text-gray-300">{d.task_type}</td>
                          <td className="py-2 pr-3 font-mono text-xs text-gray-300">{d.model_recommended}</td>
                          <td className={`py-2 pr-3 font-mono text-xs ${overridden ? "text-yellow-400" : "text-gray-300"}`}>
                            {d.model_used}
                            {overridden && <span className="ml-1 text-[10px] text-yellow-500">(override)</span>}
                          </td>
                          <td className="py-2 pr-3 text-xs text-gray-400">{d.routing_reason}</td>
                          <td className="py-2 text-right text-xs text-gray-400">
                            {d.determinism_score != null ? `${(d.determinism_score * 100).toFixed(0)}%` : "—"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
