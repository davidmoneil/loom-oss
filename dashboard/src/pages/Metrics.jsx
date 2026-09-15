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
import TimeRangeControl, { useTimeRange } from "../components/TimeRangeControl.jsx";
import { api, fmtBucketLabel, pickBucket } from "../api.js";

const QUICK_PICKS = [
  { label: "24h", amount: 24, unit: "hours" },
  { label: "7d", amount: 7, unit: "days" },
  { label: "30d", amount: 30, unit: "days" },
];

export default function Metrics() {
  const range = useTimeRange({ defaultAmount: 7, defaultUnit: "days" });
  const { hours, rangeLabel } = range;
  const { bucket, bucketSeconds } = pickBucket(hours);
  const [series, setSeries] = useState(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const ts = await api.timeseries(hours, bucket);
      setSeries(ts);
      setUpdatedAt(new Date());
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [hours, bucket]);

  useEffect(() => {
    load();
  }, [load]);

  const buckets = series?.buckets || [];
  const costByTime = buckets.map((b) => ({
    label: fmtBucketLabel(b.ts, bucketSeconds),
    cost: b.cost,
  }));
  const tokensOverTime = buckets.map((b) => ({
    label: fmtBucketLabel(b.ts, bucketSeconds),
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
        <TimeRangeControl range={range} quickPicks={QUICK_PICKS} />
      </Header>

      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Chart
          title={`Cost over time (${costMetric ? rangeLabel : "no paid usage"})`}
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
