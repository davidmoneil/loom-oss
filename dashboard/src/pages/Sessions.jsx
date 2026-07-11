import { useEffect, useState, useCallback } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from "recharts";
import StatCard from "../components/StatCard.jsx";
import Chart, { axisProps, tooltipStyle } from "../components/Chart.jsx";
import { Header } from "./Overview.jsx";
import { api, fmtNumber, fmtCost, fmtTime } from "../api.js";

const RANGES = [
  { label: "24h", hours: 24 },
  { label: "48h", hours: 48 },
  { label: "7d", hours: 168 },
];

export default function Sessions() {
  const [range, setRange] = useState(RANGES[0]);
  const [data, setData] = useState(null);
  const [costs, setCosts] = useState(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [sessions, costData] = await Promise.all([
        api.sessions(range.hours),
        api.costs(Math.max(1, Math.ceil(range.hours / 24))),
      ]);
      setData(sessions);
      setCosts(costData);
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
  const totalCost = costs?.totals?.cost_usd ?? 0;

  const turnBuckets = entries
    .filter((e) => e.turns > 0)
    .map((e) => ({
      id: e.session_id.slice(3, 11),
      turns: e.turns,
    }));

  const byModel = (costs?.by_model || [])
    .filter((m) => m.requests > 0)
    .map((m) => ({
      name: m.model.replace(/^claude-/, ""),
      requests: m.requests,
      cost: m.cost_usd,
    }));

  return (
    <div className="p-6">
      <Header title="Sessions" updatedAt={updatedAt} error={error} onRefresh={load}>
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
        <StatCard label="Sessions" value={fmtNumber(data?.sessions)} loading={loading} />
        <StatCard label="Total Turns" value={fmtNumber(data?.total_turns)} loading={loading} />
        <StatCard label="Total Cost" value={fmtCost(totalCost)} loading={loading} />
        <StatCard
          label="Avg Turns / Session"
          value={
            data?.sessions > 0
              ? (data.total_turns / data.sessions).toFixed(1)
              : "—"
          }
          loading={loading}
        />
      </div>

      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Chart
          title="Turns per session"
          loading={loading}
          empty={turnBuckets.length === 0}
        >
          <BarChart data={turnBuckets}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="id" {...axisProps} />
            <YAxis allowDecimals={false} {...axisProps} />
            <Tooltip {...tooltipStyle} />
            <Bar dataKey="turns" fill="#8b5cf6" radius={[3, 3, 0, 0]} />
          </BarChart>
        </Chart>

        <Chart
          title="Requests by model"
          loading={loading}
          empty={byModel.length === 0}
        >
          <BarChart data={byModel} layout="vertical">
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis type="number" allowDecimals={false} {...axisProps} />
            <YAxis type="category" dataKey="name" width={140} {...axisProps} />
            <Tooltip
              {...tooltipStyle}
              formatter={(v, name) =>
                name === "cost" ? `$${Number(v).toFixed(4)}` : v
              }
            />
            <Bar dataKey="requests" fill="#3b82f6" radius={[0, 3, 3, 0]} />
          </BarChart>
        </Chart>
      </div>

      <div className="mt-6 overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-left text-sm">
          <thead className="bg-card text-xs uppercase tracking-wide text-gray-400">
            <tr>
              <Th>Session</Th>
              <Th>Source</Th>
              <Th>Client</Th>
              <Th>User</Th>
              <Th className="text-right">Turns</Th>
              <Th className="text-right">Last Seen</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {loading ? (
              <SkeletonRows cols={6} />
            ) : entries.length === 0 ? (
              <tr>
                <td colSpan={6} className="p-8 text-center text-gray-500">
                  No sessions in this window
                </td>
              </tr>
            ) : (
              entries.map((e) => (
                <tr key={e.session_id} className="bg-base hover:bg-gray-800/40">
                  <Td>
                    <code className="text-xs text-gray-300">{e.session_id}</code>
                  </Td>
                  <Td>
                    <SourceBadge source={e.source} />
                  </Td>
                  <Td>
                    {e.client_type ? (
                      <ClientBadge type={e.client_type} />
                    ) : (
                      <span className="text-gray-500">—</span>
                    )}
                  </Td>
                  <Td>
                    <span className="text-xs text-gray-400" title={e.user_id || ""}>
                      {e.user_id || "—"}
                    </span>
                  </Td>
                  <Td className="text-right tabular-nums text-gray-200">
                    {e.turns}
                  </Td>
                  <Td className="text-right whitespace-nowrap text-gray-400">
                    {fmtTime(e.last_seen)}
                  </Td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SourceBadge({ source }) {
  const colors =
    source === "default"
      ? "bg-blue-500/15 text-blue-400"
      : "bg-purple-500/15 text-purple-400";
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${colors}`}>
      {source}
    </span>
  );
}

function ClientBadge({ type }) {
  const palette = {
    "claude-code": "bg-green-500/15 text-green-400",
    "sdk-python": "bg-yellow-500/15 text-yellow-400",
    "sdk-node": "bg-cyan-500/15 text-cyan-400",
    api: "bg-gray-500/15 text-gray-400",
  };
  const colors = palette[type] || palette.api;
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${colors}`}>
      {type}
    </span>
  );
}

function Th({ children, className = "" }) {
  return <th className={`px-3 py-2 font-medium ${className}`}>{children}</th>;
}
function Td({ children, className = "" }) {
  return <td className={`px-3 py-2 ${className}`}>{children}</td>;
}
function SkeletonRows({ cols }) {
  return Array.from({ length: 6 }).map((_, i) => (
    <tr key={i} className="bg-base">
      {Array.from({ length: cols }).map((__, j) => (
        <td key={j} className="px-3 py-3">
          <div className="skeleton h-4 w-full" />
        </td>
      ))}
    </tr>
  ));
}
