import { useEffect, useState, useCallback } from "react";
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
import StatCard from "../components/StatCard.jsx";
import Chart, { CHART_COLORS, axisProps, tooltipStyle } from "../components/Chart.jsx";
import { Header } from "./Overview.jsx";
import TimeRangeControl, { useTimeRange } from "../components/TimeRangeControl.jsx";
import { api, fmtNumber, fmtCost } from "../api.js";

const QUICK_PICKS = [
  { label: "24h", amount: 1, unit: "days" },
  { label: "7d", amount: 7, unit: "days" },
  { label: "30d", amount: 30, unit: "days" },
];

export default function Costs() {
  const range = useTimeRange({ defaultAmount: 1, defaultUnit: "days" });
  const { days } = range;
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const costs = await api.costs(days);
      setData(costs);
      setUpdatedAt(new Date());
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    load();
  }, [load]);

  const totals = data?.totals || {};
  const byModel = (data?.by_model || [])
    .filter((m) => m.requests > 0)
    .map((m) => ({
      name: m.model.replace(/^claude-/, ""),
      cost: m.cost_usd,
      requests: m.requests,
    }));

  const byDay = (data?.by_day || []).map((d) => ({
    date: d.date.slice(5),
    cost: d.cost_usd,
    requests: d.requests,
  }));

  const bySource = (data?.by_source || [])
    .filter((s) => s.requests > 0)
    .map((s) => ({
      name: s.source,
      value: s.cost_usd > 0 ? s.cost_usd : s.requests,
    }));

  return (
    <div className="p-6">
      <Header title="Costs" updatedAt={updatedAt} error={error} onRefresh={load}>
        <TimeRangeControl range={range} quickPicks={QUICK_PICKS} />
      </Header>

      <div className="mt-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="Total Cost" value={fmtCost(totals.cost_usd)} loading={loading} infoKey="costs.totalCost" />
        <StatCard label="Requests" value={fmtNumber(totals.requests)} loading={loading} infoKey="costs.requests" />
        <StatCard
          label="Tokens In"
          value={fmtNumber(totals.tokens_in)}
          loading={loading}
          infoKey="costs.tokensIn"
        />
        <StatCard
          label="Tokens Out"
          value={fmtNumber(totals.tokens_out)}
          loading={loading}
          infoKey="costs.tokensOut"
        />
      </div>

      <div className="mt-4 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard
          label="Tokens Saved"
          value={fmtNumber(totals.tokens_saved)}
          loading={loading}
          infoKey="costs.tokensSaved"
        />
        <StatCard
          label="Savings"
          value={fmtCost(totals.savings_usd)}
          loading={loading}
          infoKey="costs.savings"
        />
      </div>

      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Chart title="Cost by day" loading={loading} empty={byDay.length === 0} infoKey="costs.costByDay">
          <BarChart data={byDay}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="date" {...axisProps} />
            <YAxis {...axisProps} />
            <Tooltip
              {...tooltipStyle}
              formatter={(v) => `$${Number(v).toFixed(4)}`}
            />
            <Bar dataKey="cost" fill="#10b981" radius={[3, 3, 0, 0]} />
          </BarChart>
        </Chart>

        <Chart title="Cost by model" loading={loading} empty={byModel.length === 0} infoKey="costs.costByModel">
          <BarChart data={byModel} layout="vertical">
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis type="number" {...axisProps} />
            <YAxis type="category" dataKey="name" width={140} {...axisProps} />
            <Tooltip
              {...tooltipStyle}
              formatter={(v, name) =>
                name === "cost" ? `$${Number(v).toFixed(4)}` : fmtNumber(v)
              }
            />
            <Bar dataKey="cost" fill="#f59e0b" radius={[0, 3, 3, 0]} />
          </BarChart>
        </Chart>

        <Chart
          title="Daily request volume"
          loading={loading}
          empty={byDay.length === 0}
          infoKey="costs.dailyRequestVolume"
        >
          <AreaChart data={byDay}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="date" {...axisProps} />
            <YAxis allowDecimals={false} {...axisProps} />
            <Tooltip {...tooltipStyle} />
            <Area
              type="monotone"
              dataKey="requests"
              stroke="#3b82f6"
              fill="#3b82f6"
              fillOpacity={0.3}
              strokeWidth={2}
            />
          </AreaChart>
        </Chart>

        <Chart
          title="Distribution by source"
          loading={loading}
          empty={bySource.length === 0}
          infoKey="costs.distributionBySource"
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
