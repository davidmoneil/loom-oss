import { useEffect, useState, useCallback, useMemo } from "react";
import {
  BarChart,
  Bar,
  AreaChart,
  Area,
  PieChart,
  Pie,
  Cell,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from "recharts";
import Chart, { CHART_COLORS, axisProps, tooltipStyle } from "../components/Chart.jsx";
import { Header } from "./Overview.jsx";
import { api } from "../api.js";

const RANGES = [
  { label: "24h", hours: 24, bucket: "1h" },
  { label: "7d", hours: 168, bucket: "1d" },
  { label: "30d", hours: 720, bucket: "1d" },
];

export default function Metrics() {
  const [range, setRange] = useState(RANGES[1]);
  const [series, setSeries] = useState(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const ts = await api.timeseries(range.hours, range.bucket);
      setSeries(ts);
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

  const fmtBucket = useCallback(
    (ts) => {
      const d = new Date(ts * 1000);
      return range.bucket === "1d"
        ? d.toLocaleDateString([], { month: "short", day: "numeric" })
        : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    },
    [range]
  );

  const buckets = series?.buckets || [];
  const costByTime = buckets.map((b) => ({
    label: fmtBucket(b.ts),
    cost: b.cost,
  }));
  const tokensOverTime = buckets.map((b) => ({
    label: fmtBucket(b.ts),
    tokens_in: b.tokens_in,
    tokens_out: b.tokens_out,
  }));
  const byTaskType = Object.entries(series?.by_task_type || {}).map(
    ([name, n]) => ({ name, count: n })
  );
  const bySource = Object.entries(series?.by_source || {}).map(
    ([name, v]) => ({ name, value: v.cost > 0 ? v.cost : v.requests })
  );
  const costMetric = useMemo(
    () => (series?.buckets || []).some((b) => b.cost > 0),
    [series]
  );

  return (
    <div className="p-6">
      <Header title="Metrics" updatedAt={updatedAt} error={error} onRefresh={load}>
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

      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Chart
          title={`Cost over time (${costMetric ? range.label : "no paid usage"})`}
          loading={loading}
          empty={costByTime.length === 0}
        >
          <BarChart data={costByTime}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="label" {...axisProps} />
            <YAxis {...axisProps} />
            <Tooltip
              {...tooltipStyle}
              formatter={(v) => `$${Number(v).toFixed(4)}`}
            />
            <Bar dataKey="cost" fill="#3b82f6" radius={[3, 3, 0, 0]} />
          </BarChart>
        </Chart>

        <Chart
          title="Token usage over time"
          loading={loading}
          empty={tokensOverTime.length === 0}
        >
          <AreaChart data={tokensOverTime}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="label" {...axisProps} />
            <YAxis {...axisProps} />
            <Tooltip {...tooltipStyle} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Area
              type="monotone"
              dataKey="tokens_in"
              stackId="1"
              stroke="#3b82f6"
              fill="#3b82f6"
              fillOpacity={0.4}
              name="Input"
            />
            <Area
              type="monotone"
              dataKey="tokens_out"
              stackId="1"
              stroke="#10b981"
              fill="#10b981"
              fillOpacity={0.4}
              name="Output"
            />
          </AreaChart>
        </Chart>

        <Chart
          title="Requests by task type"
          loading={loading}
          empty={byTaskType.length === 0}
        >
          <BarChart data={byTaskType} layout="vertical">
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis type="number" allowDecimals={false} {...axisProps} />
            <YAxis type="category" dataKey="name" width={110} {...axisProps} />
            <Tooltip {...tooltipStyle} />
            <Bar dataKey="count" fill="#8b5cf6" radius={[0, 3, 3, 0]} />
          </BarChart>
        </Chart>

        <Chart
          title="Distribution by source"
          loading={loading}
          empty={bySource.length === 0}
        >
          <PieChart>
            <Pie
              data={bySource}
              dataKey="value"
              nameKey="name"
              cx="50%"
              cy="50%"
              outerRadius={90}
              paddingAngle={2}
            >
              {bySource.map((_, i) => (
                <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
              ))}
            </Pie>
            <Tooltip {...tooltipStyle} />
            <Legend wrapperStyle={{ fontSize: 12, color: "#9ca3af" }} />
          </PieChart>
        </Chart>
      </div>
    </div>
  );
}
