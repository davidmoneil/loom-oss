import { useEffect, useState, useCallback } from "react";
import {
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
import { api, fmtNumber, fmtCost, fmtLatency, fmtTimeShort } from "../api.js";

const REFRESH_MS = 30000;
const RANGES = [
  { label: "24h", hours: 24, bucket: "1h" },
  { label: "7d", hours: 168, bucket: "6h" },
  { label: "30d", hours: 720, bucket: "1d" },
];

export default function Overview() {
  const [range, setRange] = useState(RANGES[0]);
  const [metrics, setMetrics] = useState(null);
  const [series, setSeries] = useState(null);
  const [health, setHealth] = useState(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [error, setError] = useState(null);

  const [sessions, setSessions] = useState(null);

  const load = useCallback(async () => {
    try {
      const [m, ts, h, s] = await Promise.all([
        api.metrics(range.hours),
        api.timeseries(range.hours, range.bucket),
        api.health().catch(() => null),
        api.sessions(range.hours).catch(() => null),
      ]);
      setMetrics(m?.metrics ?? {});
      setSeries(ts);
      setHealth(h);
      setSessions(s);
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
    const id = setInterval(load, REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  const m = metrics || {};
  const volume = (series?.buckets || []).map((b) => ({
    label: fmtTimeShort(b.ts),
    requests: b.requests,
  }));
  const byModel = Object.entries(series?.by_model || {}).map(([name, v]) => ({
    name,
    value: v.requests,
  }));

  return (
    <div className="p-6">
      <Header
        title="Overview"
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
          label={`Requests (${range.label})`}
          value={fmtNumber(m.request_count)}
          loading={loading}
        />
        <StatCard
          label="Avg Latency"
          value={fmtLatency(m.avg_latency_ms)}
          loading={loading}
        />
        <StatCard
          label="Cost Today"
          value={fmtCost(m.total_cost)}
          loading={loading}
        />
        <StatCard
          label="Tokens In / Out"
          value={`${fmtNumber(m.tokens_in)} / ${fmtNumber(m.tokens_out)}`}
          loading={loading}
        />
      </div>

      {sessions?.supported && (
        <div className="mt-4 grid grid-cols-2 gap-4 lg:grid-cols-4">
          <StatCard
            label={`Active Sessions (${range.label})`}
            value={fmtNumber(sessions.sessions)}
            loading={loading}
          />
          <StatCard
            label={`Total Turns (${range.label})`}
            value={fmtNumber(sessions.total_turns)}
            loading={loading}
          />
        </div>
      )}

      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <Chart
            title={`Request volume (${range.label})`}
            loading={loading}
            empty={volume.length === 0}
          >
            <AreaChart data={volume}>
              <defs>
                <linearGradient id="vol" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.5} />
                  <stop offset="100%" stopColor="#3b82f6" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis dataKey="label" {...axisProps} />
              <YAxis allowDecimals={false} {...axisProps} />
              <Tooltip {...tooltipStyle} />
              <Area
                type="monotone"
                dataKey="requests"
                stroke="#3b82f6"
                fill="url(#vol)"
                strokeWidth={2}
              />
            </AreaChart>
          </Chart>
        </div>

        <Chart
          title="Model distribution"
          loading={loading}
          empty={byModel.length === 0}
        >
          <PieChart>
            <Pie
              data={byModel}
              dataKey="value"
              nameKey="name"
              cx="50%"
              cy="50%"
              innerRadius={55}
              outerRadius={85}
              paddingAngle={2}
            >
              {byModel.map((_, i) => (
                <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
              ))}
            </Pie>
            <Tooltip {...tooltipStyle} />
            <Legend wrapperStyle={{ fontSize: 12, color: "#9ca3af" }} />
          </PieChart>
        </Chart>
      </div>

      <ProviderHealth health={health} loading={loading} />
      <CompressionPanel compression={health?.compression} loading={loading} />
    </div>
  );
}

const COMPRESSION_TIER_COLORS = {
  light: "bg-green-500/20 text-green-400 border-green-500/30",
  medium: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  heavy: "bg-orange-500/20 text-orange-400 border-orange-500/30",
  extreme: "bg-red-500/20 text-red-400 border-red-500/30",
};

const BLOCK_TYPE_LABELS = {
  tool_result: "Tool Result",
  tool_use: "Tool Use",
  text: "Text",
  message: "Message",
};

function fmtPct(ratio) {
  if (ratio === null || ratio === undefined) return "—";
  return `${(ratio * 100).toFixed(1)}%`;
}

function CompressionPanel({ compression, loading }) {
  if (loading) {
    return (
      <div className="mt-6 rounded-lg border border-border bg-card p-4">
        <h3 className="mb-3 text-sm font-semibold text-gray-200">Compression</h3>
        <div className="skeleton h-24 w-full" />
      </div>
    );
  }

  if (!compression) {
    return null;
  }

  const byBlockType = Object.entries(compression.by_block_type || {});

  return (
    <div className="mt-6 rounded-lg border border-border bg-card p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-gray-200">Compression</h3>
        <div className="flex items-center gap-2">
          <span
            className={`inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-xs font-medium ${
              COMPRESSION_TIER_COLORS[compression.default_tier] ||
              "bg-gray-700/50 text-gray-400 border-border"
            }`}
          >
            <span
              className={`h-2 w-2 rounded-full ${compression.enabled ? "bg-green-400" : "bg-gray-500"}`}
            />
            {compression.default_tier || "unknown"}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="Tokens Before" value={fmtNumber(compression.tokens_before)} />
        <StatCard label="Tokens After" value={fmtNumber(compression.tokens_after)} />
        <StatCard label="Tokens Saved" value={fmtNumber(compression.tokens_saved)} />
        <StatCard label="Compression Ratio" value={fmtPct(compression.compression_ratio)} />
      </div>

      {byBlockType.length > 0 && (
        <div className="mt-4 overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-left text-sm">
            <thead className="bg-card text-xs uppercase tracking-wide text-gray-400">
              <tr>
                <th className="px-3 py-2 font-medium">Block Type</th>
                <th className="px-3 py-2 text-right font-medium">Before</th>
                <th className="px-3 py-2 text-right font-medium">After</th>
                <th className="px-3 py-2 text-right font-medium">Saved</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {byBlockType.map(([type, v]) => (
                <tr key={type} className="bg-base hover:bg-gray-800/40">
                  <td className="px-3 py-2 text-gray-300">
                    {BLOCK_TYPE_LABELS[type] || type}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-gray-400">
                    {fmtNumber(v.tokens_before)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-gray-400">
                    {fmtNumber(v.tokens_after)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-gray-200">
                    {fmtNumber(v.tokens_saved)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ProviderHealth({ health, loading }) {
  const providers = health?.providers || [];
  return (
    <div className="mt-6 rounded-lg border border-border bg-card p-4">
      <h3 className="mb-3 text-sm font-semibold text-gray-200">Provider health</h3>
      {loading ? (
        <div className="skeleton h-10 w-full" />
      ) : providers.length === 0 ? (
        <div className="text-sm text-gray-500">No providers configured</div>
      ) : (
        <div className="flex flex-wrap gap-3">
          {providers.map((p) => (
            <div
              key={p}
              className="flex items-center gap-2 rounded-md border border-border bg-gray-800/50 px-3 py-2"
            >
              <span className="h-2.5 w-2.5 rounded-full bg-green-500" />
              <span className="text-sm font-medium text-gray-200">{p}</span>
            </div>
          ))}
          <Flag label="Routing" on={health?.routing_table_loaded} />
          <Flag label="Compression" on={health?.compression_enabled} />
          <Flag label="Detection" on={health?.detection_enabled} />
        </div>
      )}
    </div>
  );
}

function Flag({ label, on }) {
  return (
    <div className="flex items-center gap-2 rounded-md border border-border bg-gray-800/50 px-3 py-2">
      <span
        className={`h-2.5 w-2.5 rounded-full ${on ? "bg-green-500" : "bg-gray-600"}`}
      />
      <span className="text-sm text-gray-300">{label}</span>
    </div>
  );
}

export function Header({ title, updatedAt, error, onRefresh, children }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h1 className="text-2xl font-semibold text-white">{title}</h1>
      <div className="flex items-center gap-3">
        {children}
        {error ? (
          <span className="text-sm text-red-400">Error: {error}</span>
        ) : updatedAt ? (
          <span className="text-xs text-gray-500">
            Updated {updatedAt.toLocaleTimeString()}
          </span>
        ) : null}
        {onRefresh && (
          <button
            onClick={onRefresh}
            className="rounded-md border border-border bg-card px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700/50"
          >
            Refresh
          </button>
        )}
      </div>
    </div>
  );
}
